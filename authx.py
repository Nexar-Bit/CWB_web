"""
Password helpers for multi-user web auth. Uses the bcrypt package directly.
"""

from __future__ import annotations

import re

import bcrypt

_ROUNDS = 12


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(
        (plain or "").encode("utf-8"),
        bcrypt.gensalt(rounds=_ROUNDS),
    ).decode("ascii")


def verify_password(plain: str, password_hash: str) -> bool:
    if not plain or not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            (plain or "").encode("utf-8"),
            (password_hash or "").encode("ascii"),
        )
    except (ValueError, TypeError):
        return False


def validate_email(s: str) -> bool:
    s = (s or "").strip()
    return bool(
        s
        and len(s) <= 254
        and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+", s)
    )


def validate_password(plain: str) -> str | None:
    """Return error message or None if ok."""
    if len(plain) < 8:
        return "Password must be at least 8 characters."
    if len(plain) > 200:
        return "Password is too long."
    return None
