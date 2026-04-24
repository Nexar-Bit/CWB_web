"""
Local-only record of which job offers you have already bid on (manual tracking).

CrowdWorks bidding must be done in the browser on their site; this file only
stores your own marks. Data: applied_jobs.json next to the project root.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def _read_jobs_map(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in jobs.items():
        if isinstance(v, dict):
            out[str(k)] = v
    return out


def _write_jobs_map(path: Path, jobs_map: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "jobs": jobs_map}
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_applied_map(path: Path) -> dict[str, dict[str, Any]]:
    with _lock:
        return _read_jobs_map(path)


def job_is_applied(applied_map: dict[str, dict[str, Any]], job_offer_id: object) -> bool:
    try:
        jid = str(int(job_offer_id))
    except (TypeError, ValueError):
        return False
    ent = applied_map.get(jid)
    return bool(ent and ent.get("applied"))


def set_job_applied(path: Path, job_offer_id: int, applied: bool) -> None:
    with _lock:
        m = _read_jobs_map(path)
        jid = str(int(job_offer_id))
        if applied:
            m[jid] = {
                "applied": True,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        else:
            m.pop(jid, None)
        _write_jobs_map(path, m)


def enrich_jobs_with_bid_flag(
    jobs: list[dict[str, Any]],
    applied_map: dict[str, dict[str, Any]],
) -> None:
    """Mutates each job dict with bid_applied: bool."""
    for job in jobs:
        job["bid_applied"] = job_is_applied(applied_map, job.get("job_offer_id"))
