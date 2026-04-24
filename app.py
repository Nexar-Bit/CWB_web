"""
CrowdWorks scraped jobs: small web UI (FastAPI + Jinja2).

Run (local): uvicorn app:app --reload --host 127.0.0.1 --port 8000
  Or: python app.py  (listens on PORT env or 8000; for Render, set start command to python app.py)

Run (public host): set CWORD_WEB_TOKEN to a long random string, then e.g.:
  uvicorn app:app --host 0.0.0.0 --port 8000 --proxy-headers
  (Render: PORT is set automatically; use python app.py or uvicorn with $PORT)
Put HTTPS and rate limits in a reverse proxy (Caddy, nginx) or a PaaS.

When CWORD_WEB_TOKEN is set, every route except GET /health and /auth/login requires
either: Authorization: Bearer <token>, header X-Access-Token, or a one-time
GET /auth/login?token=...&next=... (sets an HttpOnly cookie for the browser).
Optional: CWORD_FORCE_SECURE_COOKIES=1 to set the Secure flag on that cookie
(use behind HTTPS or when the reverse proxy sets X-Forwarded-Proto).

Never expose a server that stores CrowdWorks session (local_settings.json) or
OPENAI_API_KEY to the public internet without access control. The desktop app
uses Playwright; the web app does not need browsers on the server.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from bid_tracking import enrich_jobs_with_bid_flag, load_applied_map, set_job_applied
import cword_auth
import proposal_draft
from crowdworks_jobs import (
    category_feeds_ordered,
    feed_menu_labels_by_slug,
    job_public_url,
    load_jobs_jsonl,
    scrape_new_postings_feeds,
    write_jsonl,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "new_postings.jsonl"
APPLIED_FILE = BASE_DIR / "applied_jobs.json"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="CrowdWorks job list")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

_scrape_lock = threading.Lock()


templates.env.globals["job_public_url"] = job_public_url

FEEDS_CATEGORY_MENU = category_feeds_ordered()
FEED_MENU_LABEL_BY_SLUG = feed_menu_labels_by_slug()

# Optional gate for a publicly reachable install (set CWORD_WEB_TOKEN in the environment).
WEB_TOKEN = (os.environ.get("CWORD_WEB_TOKEN") or "").strip()
CWORD_ACCESS_COOKIE = "cword_web"
CWORD_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days


def _force_secure_cookies() -> bool:
    return (os.environ.get("CWORD_FORCE_SECURE_COOKIES", "").strip().lower() in ("1", "true", "yes"))


def _safe_next(val: str) -> str:
    s = (unquote(val) or "/").strip() or "/"
    if not s.startswith("/") or s.startswith("//"):
        return "/"
    return s[:2000]


def _request_authorized(request: Request) -> bool:
    if not WEB_TOKEN:
        return True
    auth = (request.headers.get("authorization") or "").strip()
    if auth[:7].lower() == "bearer " and secrets.compare_digest(
        auth[7:].strip(), WEB_TOKEN
    ):
        return True
    x = (request.headers.get("x-access-token") or "").strip()
    if x and secrets.compare_digest(x, WEB_TOKEN):
        return True
    c = (request.cookies.get(CWORD_ACCESS_COOKIE) or "").strip()
    if c and secrets.compare_digest(c, WEB_TOKEN):
        return True
    return False


def load_jobs() -> list[dict]:
    return load_jobs_jsonl(DATA_FILE)


def jobs_for_view() -> list[dict]:
    jobs = load_jobs()
    enrich_jobs_with_bid_flag(jobs, load_applied_map(APPLIED_FILE))
    return jobs


def data_file_updated_utc() -> str | None:
    if not DATA_FILE.is_file():
        return None
    ts = DATA_FILE.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class BidMarkBody(BaseModel):
    applied: bool


class DraftProposalBody(BaseModel):
    api_key: str = ""
    model: str = "gpt-4o-mini"
    fetch_full_description: bool = True
    extra_prompt: str = Field(
        default="",
        max_length=12000,
        description="Optional instructions appended to the user message for proposal generation.",
    )


class SessionCookieBody(BaseModel):
    session_id: str = ""
    save: bool = False


class SubmitBidBody(BaseModel):
    session_id: str = ""
    message: str
    price: str = ""


@app.get("/auth/login", response_class=HTMLResponse)
def auth_login(
    request: Request,
    token: str = "",
    nxt: str = Query("/", alias="next", max_length=2500),
) -> object:
    if not WEB_TOKEN:
        return RedirectResponse("/", status_code=302)
    nxt2 = _safe_next(nxt)
    if not token or not token.strip():
        nq = quote(nxt2, safe="")
        return HTMLResponse(
            "<!DOCTYPE html><html lang=\"en\">"
            '<head><meta charset="utf-8"/><title>Access</title>'
            "<style>body{font-family:system-ui,sans-serif;max-width:36rem;margin:2rem auto;padding:0 1rem;}"
            "code{word-break:break-all}</style></head><body>"
            "<h1>Access required</h1>"
            "<p>This instance uses <code>CWORD_WEB_TOKEN</code>. Use the one-time link from your server "
            "(<code>…/auth/login?token=…</code> with optional <code>next=</code> path), "
            "or <code>Authorization: Bearer</code> / <code>X-Access-Token</code> for API clients.</p>"
            f'<p>Example: <code>/auth/login?token=…&amp;next={nq}</code></p></body></html>',
        )
    t = token.strip()
    if not secrets.compare_digest(t, WEB_TOKEN):
        return HTMLResponse("Invalid token", status_code=401)
    r = RedirectResponse(url=nxt2, status_code=303)
    r.set_cookie(
        CWORD_ACCESS_COOKIE,
        WEB_TOKEN,
        httponly=True,
        samesite="lax",
        max_age=CWORD_COOKIE_MAX_AGE,
        path="/",
        secure=_force_secure_cookies(),
    )
    return r


@app.get("/")
def index(request: Request, refreshed: int = 0) -> object:
    jobs = jobs_for_view()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "jobs": jobs,
            "feeds_category_menu": FEEDS_CATEGORY_MENU,
            "feed_menu_labels": FEED_MENU_LABEL_BY_SLUG,
            "data_updated": data_file_updated_utc(),
            "refreshed": bool(refreshed),
        },
    )


@app.post("/refresh")
def refresh(
    request: Request,
    delay: float = Form(1.0),
) -> object:
    delay = max(0.0, min(delay, 30.0))
    try:
        with _scrape_lock:
            jobs, _summaries = scrape_new_postings_feeds(
                delay_s=delay,
                timeout=90.0,
                include_raw=False,
            )
            write_jsonl(str(DATA_FILE), jobs)
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "jobs": jobs_for_view(),
                "feeds_category_menu": FEEDS_CATEGORY_MENU,
                "feed_menu_labels": FEED_MENU_LABEL_BY_SLUG,
                "data_updated": data_file_updated_utc(),
                "refreshed": False,
                "error": f"{type(e).__name__}: {e}",
            },
            status_code=500,
        )
    return RedirectResponse(url="/?refreshed=1", status_code=303)


@app.post("/api/jobs/{job_id}/applied")
def mark_job_applied(job_id: int, body: BidMarkBody) -> JSONResponse:
    try:
        set_job_applied(APPLIED_FILE, job_id, body.applied)
    except (TypeError, ValueError, OSError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "job_id": job_id, "applied": body.applied})


@app.get("/api/jobs")
def api_jobs() -> JSONResponse:
    return JSONResponse(jobs_for_view())


@app.post("/api/jobs/{job_id}/draft-proposal")
def draft_proposal(job_id: int, body: DraftProposalBody) -> JSONResponse:
    jobs = load_jobs()
    job = next(
        (j for j in jobs if int(j.get("job_offer_id") or -1) == job_id),
        None,
    )
    if job is None:
        return JSONResponse({"ok": False, "error": "job not found in data file"}, status_code=404)
    key = (body.api_key or "").strip() or (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        return JSONResponse(
            {"ok": False, "error": "Missing API key (send api_key in JSON or set OPENAI_API_KEY)."},
            status_code=400,
        )
    try:
        text = proposal_draft.generate_proposal_draft(
            job,
            key,
            model=body.model.strip() or "gpt-4o-mini",
            fetch_full_description=body.fetch_full_description,
            extra_prompt=body.extra_prompt,
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {e}"},
            status_code=502,
        )
    prop_url = f"https://crowdworks.jp/proposals/new?job_offer_id={job_id}"
    return JSONResponse({"ok": True, "text": text, "proposal_url": prop_url})


@app.get("/api/settings/session")
def get_session_status() -> JSONResponse:
    """Return whether a saved _cw_session_id cookie exists."""
    session_id = proposal_draft.load_saved_session_id()
    return JSONResponse({"ok": True, "has_session": bool(session_id)})


@app.post("/api/settings/check-session")
def check_session(body: SessionCookieBody) -> JSONResponse:
    """
    Verify a _cw_session_id cookie against CrowdWorks.
    Uses the provided session_id, or falls back to the one saved in local_settings.json.
    If body.save is True and a session_id was provided, persist it to disk.
    """
    session_id = body.session_id.strip()
    if not session_id:
        session_id = proposal_draft.load_saved_session_id()
    if not session_id:
        return JSONResponse(
            {"ok": False, "error": "No session cookie provided or saved."},
            status_code=400,
        )
    if body.save and body.session_id.strip():
        try:
            proposal_draft.save_session_id_to_disk(body.session_id.strip())
        except OSError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        result = cword_auth.check_session(session_id)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {e}"},
            status_code=502,
        )
    if result.get("ok"):
        return JSONResponse({"ok": True, "message": result["message"]})
    return JSONResponse(
        {"ok": False, "error": result.get("error", "Session invalid.")},
        status_code=401,
    )


@app.post("/api/jobs/{job_id}/submit-bid")
def submit_bid(job_id: int, body: SubmitBidBody) -> JSONResponse:
    session_id = body.session_id.strip() or proposal_draft.load_saved_session_id()
    if not session_id:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "No session cookie available. "
                    "Paste your _cw_session_id in the CrowdWorks session section and verify it first."
                ),
            },
            status_code=400,
        )
    if not body.message.strip():
        return JSONResponse({"ok": False, "error": "Proposal message is required."}, status_code=400)
    try:
        session = cword_auth.session_from_cookie(session_id)
        final_url = cword_auth.submit_bid(session, job_id, body.message, price=body.price)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=502)
    return JSONResponse({"ok": True, "url": final_url})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if WEB_TOKEN:

    class _CwordWebAccessMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            p = (request.url.path or "/").rstrip("/") or "/"
            if p in ("/health", "/auth/login"):
                return await call_next(request)
            if _request_authorized(request):
                return await call_next(request)
            accept = (request.headers.get("accept") or "").lower()
            if p.startswith("/api/") or "application/json" in accept:
                return JSONResponse(
                    {
                        "detail": (
                            "Not authenticated. Set CWORD_WEB_TOKEN and use /auth/login?token=…, "
                            "or send Authorization: Bearer or X-Access-Token."
                        )
                    },
                    status_code=401,
                )
            here = request.url.path
            if request.url.query:
                here = f"{here}?{request.url.query}"
            return RedirectResponse(
                url=f"/auth/login?next={quote(here, safe='')}",
                status_code=302,
            )

    app.add_middleware(_CwordWebAccessMiddleware)


if __name__ == "__main__":
    import uvicorn

    _port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=_port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
