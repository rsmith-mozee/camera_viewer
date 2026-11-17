#!/usr/bin/env python3
"""
Axis Camera Viewer - native desktop window.

Same idea as axis_viewer.py (SSH tunnel through the onboard PC, Digest auth to the
Axis F44 units) but instead of a browser dashboard it shows the feeds in a native
Tkinter window: one tab per unit, a grid of channels inside each.

Requires Pillow (for JPEG decoding):  pip install pillow
Everything else is standard library + the built-in ssh.exe. run.bat installs Pillow
automatically on first launch.

Shared plumbing (config, SSH tunnel, digest opener) is imported from axis_viewer.py.
"""

import io
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from PIL import Image, ImageTk

import axis_viewer as core


# --------------------------------------------------------------------------- #
# MJPEG frame extraction                                                       #
# --------------------------------------------------------------------------- #
def extract_jpegs(buf):
    """Pull every complete JPEG (SOI..EOI) out of buf; return (frames, remainder)."""
    frames = []
    while True:
        start = buf.find(b"\xff\xd8")
        end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
        if start >= 0 and end >= 0:
            frames.append(buf[start:end + 2])
            buf = buf[end + 2:]
        else:
            break
    return frames, buf


# --------------------------------------------------------------------------- #
# One video tile                                                               #
# --------------------------------------------------------------------------- #
class VideoPanel:
    def __init__(self, parent, title):
        self.title = title
        self.frame = ttk.Frame(parent, relief="solid", borderwidth=1)
        self.image_label = tk.Label(self.frame, background="#000")
        self.image_label.pack(fill="both", expand=True)
        self.caption = ttk.Label(self.frame, text=f"{title}  ·  connecting",
                                 anchor="w", padding=(6, 2))
        self.caption.pack(fill="x")

        self._lock = threading.Lock()
        self._latest = None          # newest decoded PIL image
        self._status = "connecting"
        self._photo = None           # keep a reference so Tk doesn't GC it

    def push(self, pil_img):
        with self._lock:
            self._latest = pil_img

    def set_status(self, s):
        self._status = s

    def take(self):
        with self._lock:
            img, self._latest = self._latest, None
        return img, self._status


# --------------------------------------------------------------------------- #
# App                                                                          #
# --------------------------------------------------------------------------- #
class DesktopApp:
    def __init__(self):
        self.state = core.ProxyState()
        self.cfg = self.state.cfg
        self.tunnel = None
        self.panels = []
        self.workers = []
        self.stop_event = threading.Event()
        self.rendering = False

        self.root = tk.Tk()
        self.root.title("Mozee Camera Viewer (desktop)")
        self.root.geometry("1100x760")
        self.root.minsize(560, 420)
        self._build_ui()

    # -- UI ---------------------------------------------------------------- #
    def _build_ui(self):
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill="x")

        ttk.Label(bar, text="Camera password:").pack(side="left")
        self.pw_var = tk.StringVar()
        self.pw_entry = ttk.Entry(bar, textvariable=self.pw_var, show="*", width=20)
        self.pw_entry.pack(side="left", padx=(6, 12))
        self.pw_entry.bind("<Return>", lambda e: self.connect())

        self.use_ssh_var = tk.BooleanVar(value=bool(self.cfg.get("use_ssh", True)))
        ttk.Checkbutton(bar, text="Tunnel via PC", variable=self.use_ssh_var).pack(side="left")

        self.connect_btn = ttk.Button(bar, text="Connect", command=self.connect)
        self.connect_btn.pack(side="left", padx=(12, 4))
        self.disconnect_btn = ttk.Button(bar, text="Disconnect", command=self.disconnect,
                                         state="disabled")
        self.disconnect_btn.pack(side="left")

        self.status_var = tk.StringVar(value="Idle.")
        ttk.Label(bar, textvariable=self.status_var, foreground="#2a6").pack(side="left", padx=14)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.pw_entry.focus_set()

    def set_status(self, msg, ok=True):
        def _do():
            self.status_var.set(msg)
        try:
            self.root.after(0, _do)
        except RuntimeError:
            pass

    # -- connect / disconnect --------------------------------------------- #
    def connect(self):
        if not self.pw_var.get():
            messagebox.showwarning("Password needed", "Enter the camera root password first.")
            return
        self.cfg["use_ssh"] = bool(self.use_ssh_var.get())
        self.state.cam_password = self.pw_var.get()
        self.connect_btn.configure(state="disabled")
        self.stop_event = threading.Event()
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        try:
            if self.cfg["use_ssh"]:
                self.set_status("Starting SSH tunnel... (enter PC password in the SSH window if asked)")
                self.tunnel = core.Tunnel(self.cfg)
                self.tunnel.start()
                ok, msg = self.tunnel.wait_ready()
                if not ok:
                    self.set_status("Tunnel failed: " + msg, ok=False)
                    self.tunnel.stop()
                    self.root.after(0, lambda: self.connect_btn.configure(state="normal"))
                    return
            else:
                self.set_status("Direct mode (no SSH) - make sure Wi-Fi is off.")
            self.state.rebuild()
            self.root.after(0, self._build_video_and_start)
        except Exception as e:
            self.set_status("Error: " + str(e), ok=False)
            self.root.after(0, lambda: self.connect_btn.configure(state="normal"))

    def _build_video_and_start(self):
        # Clear any previous tabs/panels.
        for tab in self.nb.tabs():
            self.nb.forget(tab)
        self.panels = []
        self.workers = []

        for i, u in enumerate(self.cfg["units"]):
            tab = ttk.Frame(self.nb, padding=6)
            self.nb.add(tab, text=u["name"])
            cols = 2
            for c, ch in enumerate(u["channels"]):
                panel = VideoPanel(tab, f'{u["name"]} · camera {ch}')
                r, col = divmod(c, cols)
                panel.frame.grid(row=r, column=col, sticky="nsew", padx=4, pady=4)
                tab.columnconfigure(col, weight=1)
                tab.rowconfigure(r, weight=1)
                self.panels.append(panel)
                t = threading.Thread(target=self._stream_worker,
                                     args=(panel, i, ch), daemon=True)
                t.start()
                self.workers.append(t)

        self.set_status("Connected - %d feeds." % len(self.panels))
        self.disconnect_btn.configure(state="normal")
        if not self.rendering:
            self.rendering = True
            self._render_loop()

    def disconnect(self):
        self.stop_event.set()
        if self.tunnel:
            self.tunnel.stop()
            self.tunnel = None
        for tab in self.nb.tabs():
            self.nb.forget(tab)
        self.panels = []
        self.set_status("Disconnected.")
        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")

    def on_close(self):
        try:
            self.stop_event.set()
            if self.tunnel:
                self.tunnel.stop()
        finally:
            self.root.destroy()

    # -- streaming --------------------------------------------------------- #
    def _stream_worker(self, panel, unit_idx, cam):
        host, port = self.state.targets[unit_idx]
        query = self.cfg.get("stream_query", "").strip()
        url = f"http://{host}:{port}/axis-cgi/mjpg/video.cgi?camera={cam}"
        if query:
            url += "&" + query

        while not self.stop_event.is_set():
            try:
                panel.set_status("connecting")
                resp = self.state.opener.open(url, timeout=12)
                panel.set_status("live")
                buf = b""
                while not self.stop_event.is_set():
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    buf += chunk
                    frames, buf = extract_jpegs(buf)
                    if frames:
                        try:
                            img = Image.open(io.BytesIO(frames[-1]))
                            img.load()
                            panel.push(img)
                        except Exception:
                            pass
                    if len(buf) > 4_000_000:        # safety against a runaway buffer
                        buf = buf[-200_000:]
                try:
                    resp.close()
                except Exception:
                    pass
            except Exception:
                panel.set_status("reconnecting")
            # brief pause before reconnecting (interruptible)
            for _ in range(30):
                if self.stop_event.is_set():
                    return
                time.sleep(0.1)

    def _render_loop(self):
        for p in self.panels:
            img, status = p.take()
            if img is not None:
                w = max(p.image_label.winfo_width(), 160)
                h = max(p.image_label.winfo_height(), 90)
                disp = img.copy()
                disp.thumbnail((w, h))
                try:
                    photo = ImageTk.PhotoImage(disp)
                    p.image_label.configure(image=photo)
                    p._photo = photo
                except Exception:
                    pass
            p.caption.configure(text=f"{p.title}  ·  {status}")
        if self.panels:
            self.root.after(60, self._render_loop)
        else:
            self.rendering = False

    def run(self):
        self.root.mainloop()


def _selftest():
    # Build two real JPEGs, splice with multipart noise, confirm extraction + decode.
    bufs = []
    for color in ((200, 30, 30), (30, 30, 200)):
        b = io.BytesIO()
        Image.new("RGB", (32, 24), color).save(b, format="JPEG")
        bufs.append(b.getvalue())
    stream = (b"--boundary\r\nContent-Type: image/jpeg\r\n\r\n" + bufs[0] +
              b"\r\n--boundary\r\nContent-Type: image/jpeg\r\n\r\n" + bufs[1] + b"\r\n")
    frames, rest = extract_jpegs(stream)
    assert len(frames) == 2, f"expected 2 frames, got {len(frames)}"
    for f in frames:
        Image.open(io.BytesIO(f)).load()
    # Also confirm the shared config/opener wiring still builds.
    st = core.ProxyState()
    st.cam_password = "x"
    st.rebuild()
    assert len(st.targets) == len(st.cfg["units"])
    print("selftest OK: extracted %d JPEG frames, decoded them, %d units wired."
          % (len(frames), len(st.targets)))


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        DesktopApp().run()
