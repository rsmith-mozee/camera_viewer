#!/usr/bin/env python3
"""
Axis Camera Viewer - Mozee shuttle

A small Windows GUI app that tunnels (over SSH, through the onboard PC) to each
Axis F44 camera unit on the vehicle and shows every channel's live MJPEG feed in
a browser dashboard.

Design goals:
  * No pip dependencies - standard library only (works on any Python 3.8+,
    including the very new 3.15 where paramiko/Pillow wheels may not exist yet).
  * SSH tunnel through the onboard PC (192.168.0.50) is the reliable path to the
    cameras; the laptop's own dual-NIC routing is bypassed entirely.
  * A tiny local proxy injects HTTP Digest auth (root + password) so the browser
    shows the video with no per-unit login prompts.

How it works:
  Tkinter control window  ->  starts `ssh -N -L ...` tunnels to the PC
                          ->  starts a local HTTP proxy (this process)
                          ->  opens http://127.0.0.1:<proxy_port>/ in the browser
  Browser <img> tags  ->  local proxy  ->  (ssh tunnel)  ->  camera MJPEG

Config lives in config.json next to this file; edit it to change IPs, channels,
credentials location, etc. The camera password is entered at runtime and never
written to disk.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "use_ssh": True,                  # tunnel via the PC; set false to hit cameras directly (Wi-Fi off)
    "pc_host": "192.168.0.50",
    "pc_port": 22,
    "pc_user": "lmuser",
    "ssh_bind": "",                   # optional: local IPv4 to bind ssh to (-b), e.g. the Ethernet IP
    "cam_user": "root",
    "proxy_port": 8899,
    "stream_query": "resolution=640x480&fps=10",   # Axis video.cgi params; trim if a unit rejects them
    "units": [
        {"name": "Unit .90",  "ip": "192.168.0.90",  "local_port": 18090, "channels": [1, 2, 3, 4]},
        {"name": "Unit .100", "ip": "192.168.0.100", "local_port": 18100, "channels": [1, 2, 3, 4]},
        {"name": "Unit .110", "ip": "192.168.0.110", "local_port": 18110, "channels": [1, 2, 3, 4]},
    ],
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            cfg.update(user)
        except Exception as e:
            print("Could not read config.json, using defaults:", e)
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print("Could not save config.json:", e)


def port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Local proxy: serves the dashboard and relays each camera's MJPEG with digest #
# auth added, so the browser never has to log in.                             #
# --------------------------------------------------------------------------- #
class ProxyState:
    """Shared between the GUI and the HTTP handler."""
    def __init__(self):
        self.cfg = load_config()
        self.cam_password = ""
        self.opener = None          # urllib opener carrying the digest credentials
        self.targets = {}           # unit index -> (host, port) the proxy connects to
        self.base_port = int(self.cfg.get("proxy_port", 8899))

    def rebuild(self):
        """Compute per-unit targets and an opener preloaded with digest creds."""
        cfg = self.cfg
        pw_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        self.targets = {}
        for i, u in enumerate(cfg["units"]):
            if cfg["use_ssh"]:
                host, port = "127.0.0.1", int(u["local_port"])
            else:
                host, port = u["ip"], 80
            self.targets[i] = (host, port)
            base = f"http://{host}:{port}"
            pw_mgr.add_password(None, base, cfg["cam_user"], self.cam_password)
        handler = urllib.request.HTTPDigestAuthHandler(pw_mgr)
        # Also allow Basic as a fallback; Axis uses Digest but this is harmless.
        basic = urllib.request.HTTPBasicAuthHandler(pw_mgr)
        self.opener = urllib.request.build_opener(handler, basic)


def make_handler(state: ProxyState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass  # keep the console quiet

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._serve_dashboard()
            elif parsed.path == "/stream":
                self._serve_stream(urllib.parse.parse_qs(parsed.query))
            else:
                self.send_error(404, "Not found")

        def _serve_dashboard(self):
            html = build_dashboard_html(state.cfg, state.base_port).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(html)

        def _serve_stream(self, q):
            try:
                unit = int(q.get("unit", ["-1"])[0])
                cam = int(q.get("camera", ["1"])[0])
            except ValueError:
                self.send_error(400, "Bad params")
                return
            if unit not in state.targets:
                self.send_error(404, "Unknown unit")
                return
            host, port = state.targets[unit]
            query = state.cfg.get("stream_query", "").strip()
            url = f"http://{host}:{port}/axis-cgi/mjpg/video.cgi?camera={cam}"
            if query:
                url += "&" + query
            try:
                resp = state.opener.open(url, timeout=12)
            except Exception as e:
                self.send_error(502, f"Camera error: {e}")
                return
            ctype = resp.headers.get("Content-Type", "multipart/x-mixed-replace")
            try:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # browser closed the tab / reloaded
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

    return Handler


def build_dashboard_html(cfg, base_port):
    cards = []
    for i, u in enumerate(cfg["units"]):
        tiles = []
        # Each unit streams from its own port so the browser's ~6-connections-per-origin
        # limit is per unit, not shared across all 12 tiles.
        unit_port = base_port + i
        for ch in u["channels"]:
            src = f"http://127.0.0.1:{unit_port}/stream?unit={i}&camera={ch}"
            tiles.append(
                f'''
                <figure class="tile">
                  <img data-src="{src}" alt="{u['name']} camera {ch}"
                       onerror="reloadImg(this)" onload="this.classList.add('live')">
                  <figcaption>{u['name']} &middot; camera {ch}</figcaption>
                </figure>'''
            )
        cards.append(
            f'<section class="unit"><h2>{u["name"]} '
            f'<span class="ip">{u["ip"]}</span></h2>'
            f'<div class="grid">{"".join(tiles)}</div></section>'
        )
    body = "\n".join(cards)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mozee Camera Viewer</title>
<style>
  :root {{ color-scheme: dark; --bg:#0f1115; --card:#191c22; --line:#2a2f3a;
           --text:#e6e8ec; --muted:#9aa3b2; --live:#39d98a; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
          font:14px/1.4 system-ui, Segoe UI, Roboto, sans-serif; }}
  header {{ padding:14px 20px; border-bottom:1px solid var(--line);
            display:flex; align-items:center; gap:12px; position:sticky; top:0;
            background:var(--bg); z-index:5; }}
  header h1 {{ font-size:16px; margin:0; font-weight:650; }}
  header .hint {{ color:var(--muted); font-size:12px; }}
  main {{ padding:16px 20px 40px; }}
  .unit {{ margin-bottom:26px; }}
  .unit h2 {{ font-size:14px; margin:0 0 10px; font-weight:600; }}
  .unit .ip {{ color:var(--muted); font-weight:400; margin-left:6px; font-size:12px; }}
  .grid {{ display:grid; gap:12px;
           grid-template-columns:repeat(auto-fill, minmax(320px, 1fr)); }}
  .tile {{ margin:0; background:var(--card); border:1px solid var(--line);
           border-radius:10px; overflow:hidden; }}
  .tile img {{ display:block; width:100%; aspect-ratio:16/9; object-fit:cover;
               background:#000 repeating-linear-gradient(45deg,#111 0 10px,#161616 10px 20px); }}
  .tile img.live {{ background:#000; }}
  .tile figcaption {{ padding:7px 10px; font-size:12px; color:var(--muted);
                      border-top:1px solid var(--line); display:flex;
                      justify-content:space-between; }}
</style>
</head>
<body>
<header>
  <h1>Mozee Camera Viewer</h1>
  <span class="hint">Live feeds via the onboard PC tunnel. If a tile is striped it is reconnecting.</span>
</header>
<main>
{body}
</main>
<script>
  // Lazy-start every stream, and auto-reconnect any that drop.
  function start(img) {{
    var base = img.getAttribute('data-src');
    img.src = base + '&t=' + Date.now();
  }}
  function reloadImg(img) {{
    img.classList.remove('live');
    setTimeout(function() {{ start(img); }}, 3000);
  }}
  document.querySelectorAll('img[data-src]').forEach(start);
</script>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# SSH tunnel management                                                        #
# --------------------------------------------------------------------------- #
class Tunnel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None

    def start(self):
        cfg = self.cfg
        cmd = ["ssh", "-N",
               "-o", "ServerAliveInterval=30",
               "-o", "ServerAliveCountMax=3",
               "-o", "ExitOnForwardFailure=yes",
               "-p", str(cfg["pc_port"])]
        if cfg.get("ssh_bind"):
            cmd += ["-b", cfg["ssh_bind"]]
        for u in cfg["units"]:
            cmd += ["-L", f'{u["local_port"]}:{u["ip"]}:80']
        cmd.append(f'{cfg["pc_user"]}@{cfg["pc_host"]}')
        # New console so the user can type the PC password (unless SSH keys are set up).
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_CONSOLE
        self.proc = subprocess.Popen(cmd, creationflags=flags)
        return cmd

    def wait_ready(self, timeout=40):
        """Poll every forwarded local port until all are open (or timeout)."""
        deadline = time.time() + timeout
        ports = [int(u["local_port"]) for u in self.cfg["units"]]
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                return False, "SSH exited (wrong password, host key, or no route?)"
            if all(port_open("127.0.0.1", p) for p in ports):
                return True, "ready"
            time.sleep(1.0)
        return False, "Timed out waiting for the tunnel. Check the SSH window."

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None


# --------------------------------------------------------------------------- #
# GUI                                                                          #
# --------------------------------------------------------------------------- #
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox
    import webbrowser

    state = ProxyState()
    cfg = state.cfg
    tunnel = {"obj": None}
    httpd = {"objs": []}

    root = tk.Tk()
    root.title("Mozee Camera Viewer")
    root.geometry("460x440")
    root.minsize(440, 420)

    frm = ttk.Frame(root, padding=14)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Axis Camera Viewer", font=("Segoe UI", 13, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
    ttk.Label(frm, text="Tunnels to the 3 units via the onboard PC and opens the feeds.",
              foreground="#666").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

    rows = {}

    def field(label, key, row, show=None, width=26):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=str(cfg.get(key, "")))
        ent = ttk.Entry(frm, textvariable=var, width=width, show=show)
        ent.grid(row=row, column=1, sticky="we", pady=3)
        rows[key] = var
        return var

    use_ssh_var = tk.BooleanVar(value=bool(cfg.get("use_ssh", True)))
    ttk.Checkbutton(frm, text="Tunnel through onboard PC (recommended)",
                    variable=use_ssh_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 4))

    field("PC host", "pc_host", 3)
    field("PC user", "pc_user", 4)
    field("SSH bind IP (optional)", "ssh_bind", 5)
    field("Camera user", "cam_user", 6)

    ttk.Label(frm, text="Camera password").grid(row=7, column=0, sticky="w", pady=3)
    cam_pw_var = tk.StringVar()
    cam_pw_entry = ttk.Entry(frm, textvariable=cam_pw_var, width=26, show="*")
    cam_pw_entry.grid(row=7, column=1, sticky="we", pady=3)

    frm.columnconfigure(1, weight=1)

    status = tk.StringVar(value="Idle.")
    status_lbl = ttk.Label(frm, textvariable=status, foreground="#2a6", wraplength=420)
    status_lbl.grid(row=9, column=0, columnspan=2, sticky="w", pady=(10, 6))

    log = tk.Text(frm, height=6, width=50, state="disabled",
                  background="#111418", foreground="#cdd3dc", relief="flat")
    log.grid(row=10, column=0, columnspan=2, sticky="nsew", pady=(2, 8))
    frm.rowconfigure(10, weight=1)

    def logline(msg):
        # May be called from a worker thread; Tkinter must be touched on the main thread.
        def _do():
            log.configure(state="normal")
            log.insert("end", msg + "\n")
            log.see("end")
            log.configure(state="disabled")
        try:
            root.after(0, _do)
        except RuntimeError:
            pass  # app closing

    def set_status(msg, ok=True):
        def _do():
            status.set(msg)
            status_lbl.configure(foreground="#2a6" if ok else "#c44")
        try:
            root.after(0, _do)
        except RuntimeError:
            pass
        logline(msg)

    btns = ttk.Frame(frm)
    btns.grid(row=11, column=0, columnspan=2, sticky="we")
    connect_btn = ttk.Button(btns, text="Connect")
    connect_btn.pack(side="left")
    open_btn = ttk.Button(btns, text="Open Viewer", state="disabled")
    open_btn.pack(side="left", padx=6)
    disconnect_btn = ttk.Button(btns, text="Disconnect", state="disabled")
    disconnect_btn.pack(side="left")

    def collect_config():
        cfg["use_ssh"] = bool(use_ssh_var.get())
        for key, var in rows.items():
            val = var.get().strip()
            if key in ("pc_port",):
                try:
                    cfg[key] = int(val)
                except ValueError:
                    pass
            else:
                cfg[key] = val
        state.cam_password = cam_pw_var.get()
        save_config({k: cfg[k] for k in cfg})  # password is not in cfg, so not saved

    def start_proxy():
        state.rebuild()
        state.base_port = int(cfg["proxy_port"])
        servers = []
        # One listener per unit (base_port, base_port+1, ...) so each unit's streams
        # get their own browser connection pool. Any port can serve any unit's stream;
        # the dashboard just points each unit's tiles at its own port.
        for k in range(len(cfg["units"])):
            port = state.base_port + k
            srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            servers.append(srv)
        httpd["objs"] = servers

    def do_connect():
        if not cam_pw_var.get():
            messagebox.showwarning("Password needed", "Enter the camera root password first.")
            return
        collect_config()
        connect_btn.configure(state="disabled")

        def worker():
            try:
                if cfg["use_ssh"]:
                    set_status("Starting SSH tunnel... (type the PC password in the SSH window if asked)")
                    t = Tunnel(cfg)
                    tunnel["obj"] = t
                    cmd = t.start()
                    logline("ssh " + " ".join(cmd[1:]))
                    ok, msg = t.wait_ready()
                    if not ok:
                        set_status("Tunnel failed: " + msg, ok=False)
                        t.stop()
                        root.after(0, lambda: connect_btn.configure(state="normal"))
                        return
                    set_status("Tunnel up.")
                else:
                    set_status("Direct mode (no SSH). Make sure Wi-Fi is off so routing is clean.")
                start_proxy()
                url = f"http://127.0.0.1:{cfg['proxy_port']}/"
                set_status("Connected. Opening viewer: " + url)
                webbrowser.open(url)
                root.after(0, lambda: (open_btn.configure(state="normal"),
                                       disconnect_btn.configure(state="normal")))
            except Exception as e:
                set_status("Error: " + str(e), ok=False)
                root.after(0, lambda: connect_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def do_open():
        webbrowser.open(f"http://127.0.0.1:{cfg['proxy_port']}/")

    def do_disconnect():
        for srv in httpd.get("objs", []):
            try:
                srv.shutdown()
            except Exception:
                pass
        httpd["objs"] = []
        if tunnel["obj"]:
            tunnel["obj"].stop()
            tunnel["obj"] = None
        set_status("Disconnected.")
        connect_btn.configure(state="normal")
        open_btn.configure(state="disabled")
        disconnect_btn.configure(state="disabled")

    def on_close():
        try:
            do_disconnect()
        finally:
            root.destroy()

    connect_btn.configure(command=do_connect)
    open_btn.configure(command=do_open)
    disconnect_btn.configure(command=do_disconnect)
    root.protocol("WM_DELETE_WINDOW", on_close)

    cam_pw_entry.focus_set()
    root.mainloop()


if __name__ == "__main__":
    # Allow `python axis_viewer.py --selftest` to check the proxy/dashboard without a GUI or cameras.
    if "--selftest" in sys.argv:
        st = ProxyState()
        st.cam_password = "x"
        st.rebuild()
        html = build_dashboard_html(st.cfg, st.base_port)
        assert "Mozee Camera Viewer" in html and "unit=0" in html
        # unit 1 tiles should point at base_port+1 (per-unit origin)
        assert f"127.0.0.1:{st.base_port + 1}" in html
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
        assert "<img" in page
        srv.shutdown()
        print("selftest OK: dashboard served, %d units, proxy wired." % len(st.cfg["units"]))
    else:
        run_gui()
