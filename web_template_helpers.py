"""Shared layout / job display helpers for the web UI (parity with desktop_app)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# Mirrors desktop 24h "new project" window
_NEW_HOURS = 24

_BID_LABELS = {
    "success": "✓ Submitted",
    "already_bid": "◎ Already Bid",
    "failed": "✗ Failed",
}

_ROW_CLASSES: dict[tuple[str, bool], str] = {
    ("success", False): "tr-bid tr-bid--success",
    ("success", True): "tr-bid tr-bid--success tr-bid--new",
    ("already_bid", False): "tr-bid tr-bid--already",
    ("already_bid", True): "tr-bid tr-bid--already tr-bid--new",
    ("failed", False): "tr-bid tr-bid--failed",
    ("failed", True): "tr-bid tr-bid--failed tr-bid--new",
    ("", True): "tr-bid tr-bid--fresh",
    ("", False): "tr-bid",
}


def fmt_pay(job: dict[str, Any]) -> str:
    pt = job.get("payment_type")
    lo, hi = job.get("pay_min"), job.get("pay_max")
    if pt == "hourly":
        return f"時給 {lo or '?'}–{hi or '?'}"
    if pt == "fixed":
        return f"固定 {lo or '?'}–{hi or '?'}"
    return "—"


def is_new_job(job: dict[str, Any], new_session_ids: set[str] | None = None) -> bool:
    """New badge: session discovery set or last_released within _NEW_HOURS (matches desktop)."""
    jid = str(job.get("job_offer_id") or "")
    if new_session_ids and jid in new_session_ids:
        return True
    raw_ts = job.get("last_released_at") or ""
    if not raw_ts:
        return False
    try:
        dt = datetime.fromisoformat(str(raw_ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if datetime.now(tz=timezone.utc) - dt < timedelta(hours=_NEW_HOURS):
            return True
    except (ValueError, TypeError):
        pass
    return False


def job_row_class(bid_status: str, is_new: bool) -> str:
    st = bid_status or ""
    if (st, is_new) in _ROW_CLASSES:
        return _ROW_CLASSES[(st, is_new)]
    if is_new:
        return "tr-bid tr-bid--fresh"
    return "tr-bid"


def bid_status_label(bid_status: str) -> str:
    if not bid_status:
        return "—"
    return _BID_LABELS.get(bid_status, "—")
