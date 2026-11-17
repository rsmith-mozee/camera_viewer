# Mozee Camera Viewer

A small Windows app to watch all three Axis F44 camera units on the shuttle at once.

It tunnels to each unit **through the onboard PC** (`192.168.0.50`) over SSH, then
shows every channel's live MJPEG feed. The camera login is added for you, so the
video just appears — no per-unit password prompts.

Two versions, same tunnel/auth under the hood:

| | Launcher | Notes |
| --- | --- | --- |
| **Desktop window** (default) | `run.bat` | Native Tkinter window, a tab per unit. Needs `Pillow`, installed automatically on first run. |
| **Browser dashboard** | `run_web.bat` | Opens the feeds in your browser. Zero pip installs - standard library only. |

Both use only the standard library and the `ssh.exe` that ships with Windows 10/11
(the desktop version additionally uses `Pillow` to decode the video).

## Run it

1. Plug the laptop into the vehicle's camera switch (and ideally turn Wi-Fi **off**,
   so the laptop's two networks don't both sit on `192.168.0.x`).
2. Double-click **`run.bat`** for the desktop window (or `run_web.bat` for the browser).
3. Type the camera **root password** (the one set during the reset) and click **Connect**.
4. An SSH window opens — type the **PC password** there if prompted (see "No password
   prompts" below to skip this). The feeds appear once the tunnel is up.

Click **Disconnect** (or close the window) to tear down the tunnel.

The desktop window shows one tab per unit, each with its channels in a grid; a tile
labelled *connecting* / *reconnecting* is still trying. Resize the window to scale the video.

## What you see

Three sections (`.90`, `.100`, `.110`), each with its four channels. A striped tile is
reconnecting; it retries on its own. Based on what we found, roughly two channels per
unit carry a live camera and the others are unused — those show as a static/!blank tile.

## Settings (`config.json`)

Edit `config.json` next to the app to change anything:

| Key | Meaning |
| --- | --- |
| `use_ssh` | `true` = tunnel through the PC (default). `false` = connect straight to the cameras (only works with Wi-Fi off). |
| `pc_host` / `pc_user` / `pc_port` | The onboard PC to tunnel through. |
| `ssh_bind` | Optional local IP to pin SSH to the Ethernet (`-b`), if Wi-Fi keeps stealing the route. |
| `cam_user` | Camera login user (`root`). |
| `proxy_port` | Base local port for the dashboard. Each unit also uses the next port up (e.g. 8899, 8900, 8901 for three units) so the browser's ~6-streams-per-origin limit is per unit, not shared. |
| `stream_query` | Axis `video.cgi` params. Drop `resolution=...` if a unit rejects it. |
| `units` | Each unit's name, IP, the local tunnel port, and which channels to show. |

The camera password is entered at runtime and **never written to disk**.

## No password prompts (optional, one-time)

To skip the PC-password step every time, set up an SSH key once:

```bat
ssh-keygen -t ed25519 -f "%USERPROFILE%\.ssh\id_ed25519" -N ""
type "%USERPROFILE%\.ssh\id_ed25519.pub" | ssh lmuser@192.168.0.50 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

After that the tunnel connects with no prompt.

## Troubleshooting

- **SSH window closes immediately / "Tunnel failed":** wrong PC password, changed host
  key (run `ssh-keygen -R 192.168.0.50` then retry), or the laptop can't reach `192.168.0.50`.
- **Tiles never load (502):** the camera password is wrong, or a unit is at a different IP
  than `config.json` says. Check `http://127.0.0.1:18100/` etc. after connecting.
- **Everything times out:** laptop isn't on the vehicle network, or Wi-Fi is hijacking the
  `192.168.0.x` route — turn Wi-Fi off or set `ssh_bind` to the Ethernet IP.

See `../CAMERA_ACCESS.md` for how the cameras themselves were set up.
