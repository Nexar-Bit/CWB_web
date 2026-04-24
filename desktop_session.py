"""Signed on-disk session for the desktop app (stdlib only)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

from cword_auth import session_secret

MAX_AGE_SEC = 60 * 60 * 24 * 14


def _sign(username: str, issued: float, secret: str) -> str:
    msg = f"{username}\n{issued:.6f}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def save_desktop_session(username: str, path: Path) -> None:
    secret = session_secret()
    issued = time.time()
    payload = {
        "username": username,
        "issued": issued,
        "sig": _sign(username, issued, secret),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def load_desktop_session(path: Path) -> str | None:
    if not path.is_file():
        return None
    secret = session_secret()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        username = str(data["username"]).strip()
        issued = float(data["issued"])
        sig = str(data["sig"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not username or time.time() - issued > MAX_AGE_SEC:
        return None
    if not hmac.compare_digest(sig, _sign(username, issued, secret)):
        return None
    return username


def clear_desktop_session(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
