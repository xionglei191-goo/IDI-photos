"""Visible local server launcher; no scheduled startup or background installation."""
import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from .config import Settings
from .db import now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    os.environ["IDI_PORT"] = str(args.port)
    settings = Settings.from_env()
    local_url = f"http://{'127.0.0.1' if settings.host == '0.0.0.0' else settings.host}:{args.port}"
    settings.prepare()
    lock = (settings.data_dir / "service.lock").open("a+b")
    if lock.tell() == 0:
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise SystemExit("This IDI archive already has a running service.")
    try:
        with socket.socket() as probe:
            probe.bind((settings.host, args.port))
    except OSError:
        lock.close()
        raise SystemExit(f"Port {args.port} is already in use.")
    record = settings.data_dir / "service.json"
    record.write_text(json.dumps({"pid": os.getpid(), "host": settings.host, "port": args.port, "data_dir": str(settings.data_dir), "executable": sys.executable, "base_executable": getattr(sys, "_base_executable", sys.executable), "started_at": now()}, indent=2), encoding="utf-8")
    if args.open_browser:
        def open_when_ready():
            for _ in range(100):
                try:
                    with urllib.request.urlopen(local_url + "/api/health", timeout=1) as response:
                        if json.loads(response.read()).get("application") == "IDI":
                            webbrowser.open(local_url)
                            return
                except (OSError, ValueError):
                    pass
                time.sleep(.2)
        threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        uvicorn.run("idi.api:create_app", factory=True, host=settings.host, port=args.port, proxy_headers=False)
    finally:
        if record.is_file() and json.loads(record.read_text(encoding="utf-8")).get("pid") == os.getpid():
            record.unlink()
        lock.close()


if __name__ == "__main__":
    main()
