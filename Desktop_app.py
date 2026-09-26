"""
desktop_app.py — native desktop window for Music Studio.

Drop this file in the SAME folder as app.py (next to subtitle_engine.py,
templates/, static/, etc). Run this instead of app.py to open the app in
its own window instead of a browser tab. Nothing in app.py,
subtitle_engine.py, templates/ or static/ needs to change.

Install once:
    pip install pywebview waitress

Run:
    python desktop_app.py

Notes:
- Windows needs the Microsoft Edge WebView2 Runtime, which ships
  built-in on Windows 10 21H2+ and all of Windows 11. If it's missing,
  pywebview will prompt to install it on first launch.
- ffmpeg still needs to be reachable on PATH, same as before.
- The default admin login (admin / change-me-now) still applies — if
  you want the desktop app to skip the login screen entirely since
  it's just you, say so and I'll wire up an auto-login for single-user
  use.
"""

import socket
import threading
import time

import webview
from waitress import serve

from app import app  # your existing Flask app, completely unchanged


def _free_port() -> int:
    """Ask the OS for an unused local port, so this never collides with
    another instance or with 5000/8000 if something else is using them."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_server(port: int):
    # 127.0.0.1, not 0.0.0.0 — this window only ever talks to itself,
    # so there's no reason to expose it to the rest of the LAN anymore.
    serve(app, host="127.0.0.1", port=port, threads=8)


def _wait_until_up(port: int, timeout: float = 10.0) -> bool:
    """Block until the server actually accepts connections, so the
    window doesn't flash a 'connection refused' page on a slow machine."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def main():
    port = _free_port()

    server_thread = threading.Thread(target=_run_server, args=(port,), daemon=True)
    server_thread.start()

    if not _wait_until_up(port):
        raise RuntimeError("Backend server didn't start in time.")

    window = webview.create_window(
        "🎸 Music Studio",
        f"http://127.0.0.1:{port}/",
        width=1280,
        height=860,
        min_size=(1000, 700),
    )

    # The server thread is a daemon, so it's killed automatically the
    # moment this process exits — i.e. as soon as the window is closed.
    webview.start()


if __name__ == "__main__":
    main()