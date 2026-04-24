"""
CrowdWorks Bot — web UI (FastAPI + Jinja2) aligned with the desktop app layout,
colours, SQLite data, and the same main sections: Dashboard, Accounts, Jobs, Settings, Log.

Run: uvicorn app:app --reload --host 127.0.0.1 --port 8000
  Or: python app.py
"""

from __future__ import annotations

import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import db
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from web_template_helpers import (
    bid_status_label,
    fmt_pay,
    is_new_job,
    job_row_class,
)

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
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_scrape_lock = threading.Lock()

AI_MODELS = [
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-4.1",
    "gpt-4-turbo",
    "o1-mini",
    "o3-mini",
]

WEB_TOKEN = (os.environ.get("CWORD_WEB_TOKEN") or "").strip()
CWORD_ACCESS_COOKIE = "cword_web"
CWORD_COOKIE_MAX_AGE = 30 * 24 * 3600


@asynccontextmanager
async def _lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="CrowdWorks Bot", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates.env.globals["job_public_url"] = job_public_url
FEEDS_CATEGORY_MENU = category_feeds_ordered()
FEED_MENU_LABEL_BY_SLUG = feed_menu_labels_by_slug()


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


def data_file_updated_utc() -> str | None:
    if not DATA_FILE.is_file():
        return None
    ts = DATA_FILE.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ctx(request: Request, current_page: str, **kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "request": request,
        "current_page": current_page,
        "status_message": kwargs.pop("status_message", "Ready."),
    }
    base.update(kwargs)
    return base


def _build_job_rows(
    acc_id: int | None,
) -> list[dict]:
    jobs = load_jobs()
    bid_statuses = db.get_bid_statuses(acc_id)
    bid_errors = db.get_bid_errors(acc_id)
    if acc_id is not None:
        acc = db.get_account(acc_id)
        acc_col = acc["name"] if acc else "—"
    else:
        acc_col = "—"
    new_sess: set[str] = set()
    rows: list[dict] = []
    for job in jobs:
        jid = str(job.get("job_offer_id") or "")
        st = bid_statuses.get(jid, "")
        err = bid_errors.get(jid, "") if st == "failed" else ""
        is_n = is_new_job(job, new_sess)
        rows.append(
            {
                **job,
                "_acc_col": acc_col,
                "_bid_status": st,
                "_bid_error": err,
                "_is_new": is_n,
                "_bid_label": bid_status_label(st),
                "_row_class": f"jobs-data-row {job_row_class(st, is_n)}",
                "_badge": "★ NEW PROJECT" if is_n else "",
                "_cat": str(
                    job.get("feed_menu_label")
                    or FEED_MENU_LABEL_BY_SLUG.get(str(job.get("feed_slug") or ""))
                    or job.get("feed_slug")
                    or "—"
                ),
                "_pay": fmt_pay(job),
            }
        )
    return rows


# ── Pydantic ──────────────────────────────────────────────────────────────────


class DraftProposalBody(BaseModel):
    api_key: str = ""
    model: str = "gpt-4o-mini"
    fetch_full_description: bool = True
    extra_prompt: str = Field(default="", max_length=12000)
    account_id: int | None = None


class CheckSessionJson(BaseModel):
    session_id: str = ""
    save: bool = False


class SubmitBidBody(BaseModel):
    session_id: str = ""
    account_id: int | None = None
    message: str
    price: str = ""


class BidMarkBody(BaseModel):
    mark: bool
    account_id: int | None = None


class VerifyRawBody(BaseModel):
    session_id: str = ""


class RetryBody(BaseModel):
    account_id: int | None = None


# ── Auth (token gate) ─────────────────────────────────────────────────────────

@app.get("/auth/login", response_class=HTMLResponse)
def auth_login(
    request: Request,
    token: str = "",
    nxt: str = Query("/", alias="next", max_length=2500),
) -> object:
    if not WEB_TOKEN:
        return RedirectResponse("/dashboard", status_code=302)
    nxt2 = _safe_next(nxt)
    if not token or not token.strip():
        nq = quote(nxt2, safe="")
        return HTMLResponse(
            "<!DOCTYPE html><html lang=\"en\">"
            '<head><meta charset="utf-8"/><title>Access</title>'
            "<style>body{font-family:system-ui,sans-serif;max-width:36rem;margin:2rem auto;padding:0 1rem;}"
            "code{word-break:break-all}</style></head><body>"
            "<h1>Access required</h1>"
            "<p>This instance uses <code>CWORD_WEB_TOKEN</code>…</p>"
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


# ── Page routes (desktop parity) ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root() -> object:
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
def page_dashboard(request: Request) -> object:
    accts = db.list_accounts()
    active = sum(1 for a in accts if a["enabled"])
    total = len(accts)
    total_jobs = len(load_jobs()) if DATA_FILE.is_file() else 0
    return templates.TemplateResponse(
        request,
        "pages/dashboard.html",
        _ctx(
            request,
            "Dashboard",
            page_title="Dashboard — CrowdWorks Bot",
            active_accounts=active,
            total_accounts=total,
            total_jobs=total_jobs,
            bids_session=0,
            bot_status="Web (auto-bid: desktop only)",
            recent_logs=db.list_logs(limit=20),
        ),
    )


@app.get("/accounts", response_class=HTMLResponse)
def page_accounts(request: Request, message: str = "", error: str = "") -> object:
    accts = db.list_accounts()
    bid_counts = {a["id"]: db.count_bids(a["id"]) for a in accts}
    return templates.TemplateResponse(
        request,
        "pages/accounts.html",
        _ctx(
            request,
            "Accounts",
            page_title="Accounts",
            accounts=accts,
            bid_counts=bid_counts,
            message=message,
            error=error,
        ),
    )


def _get_prompt_id(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


@app.get("/accounts/new", response_class=HTMLResponse)
def page_account_new(request: Request) -> object:
    return templates.TemplateResponse(
        request,
        "pages/account_form.html",
        _ctx(
            request,
            "Accounts",
            page_title="Add Account",
            account=None,
            prompts=db.list_prompts(),
            form_action="/accounts",
        ),
    )


@app.post("/accounts", response_class=HTMLResponse)
def post_account_new(
    request: Request,
    name: str = Form(""),
    session_id: str = Form(""),
    prompt_id: str = Form(""),
    enabled: str | None = Form(None),
) -> object:
    pid = _get_prompt_id(prompt_id)
    en = 1 if enabled in ("1", "on", "true") else 0
    if not (session_id or "").strip():
        return templates.TemplateResponse(
            request,
            "pages/account_form.html",
            {
                "request": request,
                "current_page": "Accounts",
                "status_message": "Error.",
                "account": None,
                "prompts": db.list_prompts(),
                "form_action": "/accounts",
                "error": "Session ID is required.",
            },
        )
    new_id = db.add_account(
        (name or "New Account").strip() or "New Account",
        session_id.strip(),
        prompt_id=pid,
        enabled=en,
    )
    db.add_log(f"Account added (id {new_id}).", level="info")
    return RedirectResponse("/accounts?message=Saved", status_code=303)


@app.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
def page_account_edit(request: Request, account_id: int) -> object:
    acc = db.get_account(account_id)
    if not acc:
        return RedirectResponse("/accounts?error=Not%20found", status_code=302)
    return templates.TemplateResponse(
        request,
        "pages/account_form.html",
        _ctx(
            request,
            "Accounts",
            page_title="Edit Account",
            account=acc,
            prompts=db.list_prompts(),
            form_action=f"/accounts/{account_id}/update",
        ),
    )


@app.post("/accounts/{account_id}/update", response_class=HTMLResponse)
def post_account_update(
    request: Request,
    account_id: int,
    name: str = Form(""),
    session_id: str = Form(""),
    prompt_id: str = Form(""),
    enabled: str | None = Form(None),
) -> object:
    acc = db.get_account(account_id)
    if not acc:
        return RedirectResponse("/accounts?error=Not%20found", status_code=302)
    if not (session_id or "").strip():
        a2 = {**acc, "name": name, "session_id": session_id}
        return templates.TemplateResponse(
            request,
            "pages/account_form.html",
            {
                "request": request,
                "current_page": "Accounts",
                "status_message": "Error.",
                "account": a2,
                "prompts": db.list_prompts(),
                "form_action": f"/accounts/{account_id}/update",
                "error": "Session ID is required.",
            },
        )
    en = 1 if enabled in ("1", "on", "true") else 0
    db.update_account(
        account_id,
        name=(name or acc["name"]).strip(),
        session_id=session_id.strip(),
        prompt_id=_get_prompt_id(prompt_id),
        enabled=en,
    )
    return RedirectResponse("/accounts?message=Updated", status_code=303)


@app.post("/accounts/{account_id}/delete", response_class=HTMLResponse)
def post_account_delete(account_id: int) -> object:
    if db.get_account(account_id):
        db.delete_account(account_id)
    return RedirectResponse("/accounts?message=Deleted", status_code=303)


@app.post("/accounts/{account_id}/verify", response_class=HTMLResponse)
def post_account_verify(request: Request, account_id: int) -> object:
    acc = db.get_account(account_id)
    if not acc:
        return RedirectResponse("/accounts?error=Not%20found", status_code=302)
    res = cword_auth.check_session(acc["session_id"])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if res.get("ok"):
        uname = res.get("username", "")
        db.update_account(
            account_id, status="active", cw_username=uname or acc.get("cw_username") or "",
            last_verified=now,
        )
        db.add_log(
            f"Session verified for '{acc['name']}' ({uname}).",
            level="success",
            account_id=account_id,
        )
    else:
        db.update_account(account_id, status="expired", last_verified=now)
        db.add_log(
            f"Session check failed for '{acc['name']}': {res.get('error', '')}",
            level="error",
            account_id=account_id,
        )
    return RedirectResponse("/accounts?message=Verify%20done", status_code=303)


@app.post("/accounts/{account_id}/toggle", response_class=HTMLResponse)
def post_account_toggle(account_id: int) -> object:
    acc = db.get_account(account_id)
    if acc:
        db.update_account(account_id, enabled=0 if acc["enabled"] else 1)
    return RedirectResponse("/accounts", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def page_jobs(
    request: Request,
    acc: int | None = Query(None, description="Account filter"),
    refreshed: int = 0,
    error: str = "",
) -> object:
    def _f_delay() -> str:
        try:
            v = float(db.get_setting("feed_delay", "1.0") or 1.0)
            return f"{v:.1f}" if v == int(v) else str(v)
        except ValueError:
            return "1.0"

    job_rows = _build_job_rows(acc)
    alist = db.list_accounts()
    return templates.TemplateResponse(
        request,
        "pages/jobs.html",
        _ctx(
            request,
            "Jobs",
            page_title="Jobs",
            job_rows=job_rows,
            accounts_list=alist,
            acc_filter=acc,
            feeds_category_menu=FEEDS_CATEGORY_MENU,
            data_updated=data_file_updated_utc(),
            refreshed=bool(refreshed),
            error=error,
            default_feed_delay=_f_delay(),
            default_model=db.get_setting("openai_model", "gpt-4o-mini"),
        ),
    )


@app.post("/refresh", response_class=HTMLResponse)
def post_refresh(
    request: Request,
    delay: float = Form(1.0),
) -> object:
    # Prefer saved feed delay if form sends nothing meaningful
    try:
        dcfg = float(db.get_setting("feed_delay", "1.0") or 1.0)
    except ValueError:
        dcfg = 1.0
    if delay is None or delay < 0:
        delay = dcfg
    delay = max(0.0, min(float(delay), 30.0))
    try:
        with _scrape_lock:
            jobs, _s = scrape_new_postings_feeds(
                delay_s=delay, timeout=90.0, include_raw=False
            )
            write_jsonl(str(DATA_FILE), jobs)
        db.add_log(f"Scrape complete ({len(jobs)} jobs) — web refresh.", level="info")
    except Exception as e:
        db.add_log(f"Scrape error: {e!s}", level="error")
        return templates.TemplateResponse(
            request,
            "pages/jobs.html",
            _ctx(
                request,
                "Jobs",
                error=f"{type(e).__name__}: {e}",
                job_rows=_build_job_rows(None),
                accounts_list=db.list_accounts(),
                acc_filter=None,
                feeds_category_menu=FEEDS_CATEGORY_MENU,
                data_updated=data_file_updated_utc(),
                refreshed=False,
                default_feed_delay="1.0",
                default_model=db.get_setting("openai_model", "gpt-4o-mini"),
            ),
            status_code=500,
        )
    return RedirectResponse("/jobs?refreshed=1", status_code=303)


def _settings_dict() -> dict[str, str]:
    d = {
        "openai_api_key": db.get_setting("openai_api_key", ""),
        "scrape_interval": db.get_setting("scrape_interval", "30"),
        "feed_delay": db.get_setting("feed_delay", "1.0"),
        "max_scrape_pages": db.get_setting("max_scrape_pages", "3"),
        "max_parallel_bids": db.get_setting("max_parallel_bids", "10"),
        "bid_max_age_hours": db.get_setting("bid_max_age_hours", "48"),
        "show_browser": db.get_setting("show_browser", "1"),
        "bid_price_pct": db.get_setting("bid_price_pct", "0"),
        "openai_model": db.get_setting("openai_model", "gpt-4o-mini"),
    }
    return d


@app.get("/settings", response_class=HTMLResponse)
def page_settings(
    request: Request, message: str = "", error: str = ""
) -> object:
    return templates.TemplateResponse(
        request,
        "pages/settings.html",
        _ctx(
            request,
            "Settings",
            page_title="Settings",
            s=_settings_dict(),
            ai_models=AI_MODELS,
            prompts=db.list_prompts(),
            message=message,
            error=error,
        ),
    )


@app.post("/settings/save", response_class=HTMLResponse)
def post_settings_save(
    openai_api_key: str = Form(""),
    scrape_interval: str = Form("30"),
    feed_delay: str = Form("1.0"),
    max_scrape_pages: str = Form("3"),
    max_parallel_bids: str = Form("10"),
    bid_max_age_hours: str = Form("48"),
    show_browser: str = Form(""),
    bid_price_pct: str = Form("0"),
    openai_model: str = Form("gpt-4o-mini"),
) -> object:
    db.set_setting("openai_api_key", (openai_api_key or "").strip())
    try:
        db.set_setting("scrape_interval", str(max(10, int(scrape_interval))))
    except ValueError:
        pass
    try:
        db.set_setting("feed_delay", str(max(0.0, float(feed_delay))))
    except ValueError:
        pass
    try:
        db.set_setting("max_scrape_pages", str(max(1, int(max_scrape_pages))))
    except ValueError:
        pass
    try:
        db.set_setting("max_parallel_bids", str(max(1, int(max_parallel_bids))))
    except ValueError:
        pass
    try:
        db.set_setting("bid_max_age_hours", str(max(1, int(bid_max_age_hours))))
    except ValueError:
        pass
    db.set_setting("show_browser", "1" if show_browser in ("1", "on") else "0")
    try:
        p = int(bid_price_pct)
        db.set_setting("bid_price_pct", str(max(0, min(100, p))))
    except ValueError:
        pass
    m = (openai_model or "").strip()
    if m:
        db.set_setting("openai_model", m)
    db.add_log("Settings saved (web).", level="info")
    return RedirectResponse("/settings?message=Saved", status_code=303)


@app.get("/prompts/new", response_class=HTMLResponse)
def page_prompt_new(request: Request) -> object:
    return templates.TemplateResponse(
        request,
        "pages/prompt_form.html",
        _ctx(
            request,
            "Settings",
            page_title="Add Prompt",
            prompt=None,
            form_action="/prompts",
        ),
    )


@app.post("/prompts", response_class=HTMLResponse)
def post_prompt_new(
    request: Request, name: str = Form(""), content: str = Form("")
) -> object:
    n, c = (name or "").strip(), (content or "").strip()
    if not n or not c:
        return templates.TemplateResponse(
            request,
            "pages/prompt_form.html",
            {
                "request": request,
                "current_page": "Settings",
                "status_message": "Error",
                "prompt": None,
                "form_action": "/prompts",
                "error": "Name and content are required.",
            },
        )
    db.add_prompt(n, c)
    return RedirectResponse("/settings?message=Prompt%20added", status_code=303)


@app.get("/prompts/{pid}/edit", response_class=HTMLResponse)
def page_prompt_edit(request: Request, pid: int) -> object:
    p = db.get_prompt(pid)
    if not p:
        return RedirectResponse("/settings?error=notfound", status_code=302)
    return templates.TemplateResponse(
        request,
        "pages/prompt_form.html",
        _ctx(
            request,
            "Settings",
            page_title="Edit Prompt",
            prompt=p,
            form_action=f"/prompts/{pid}/update",
        ),
    )


@app.post("/prompts/{pid}/update", response_class=HTMLResponse)
def post_prompt_update(
    request: Request, pid: int, name: str = Form(""), content: str = Form("")
) -> object:
    if not db.get_prompt(pid):
        return RedirectResponse("/settings?error=notfound", status_code=302)
    n, c = (name or "").strip(), (content or "").strip()
    if n and c:
        db.update_prompt(pid, n, c)
    return RedirectResponse("/settings?message=Prompt%20updated", status_code=303)


@app.post("/prompts/{pid}/delete", response_class=HTMLResponse)
def post_prompt_delete(pid: int) -> object:
    if db.get_prompt(pid):
        db.delete_prompt(pid)
    return RedirectResponse("/settings?message=Deleted", status_code=303)


@app.get("/log", response_class=HTMLResponse)
def page_log(request: Request) -> object:
    return templates.TemplateResponse(
        request,
        "pages/log.html",
        _ctx(
            request,
            "Log",
            page_title="Activity Log",
            logs=db.list_logs(limit=300),
        ),
    )


@app.post("/log/clear", response_class=HTMLResponse)
def post_log_clear() -> object:
    db.clear_logs()
    db.add_log("Logs cleared (web).", level="info")
    return RedirectResponse("/log", status_code=303)


# ── JSON API ─────────────────────────────────────────────────────────────────


@app.get("/api/jobs")
def api_jobs_list() -> JSONResponse:
    return JSONResponse(
        {
            "jobs": _build_job_rows(None),
        }
    )


def _openai_key(body_key: str) -> str:
    k = (body_key or "").strip() or (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not k:
        k = (db.get_setting("openai_api_key") or "").strip()
    return k


@app.post("/api/verify-session-raw")
def api_verify_raw(body: VerifyRawBody) -> JSONResponse:
    sid = (body.session_id or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    r = cword_auth.check_session(sid)
    if r.get("ok"):
        return JSONResponse(
            {
                "ok": True,
                "message": r.get("message", "Session is valid."),
            }
        )
    return JSONResponse({"ok": False, "error": r.get("error", "invalid")}, status_code=401)


@app.post("/api/accounts/{aid}/check-session")
def api_check_account_session(aid: int, body: CheckSessionJson) -> JSONResponse:
    acc = db.get_account(aid)
    if not acc:
        return JSONResponse({"ok": False, "error": "no account"}, status_code=404)
    sid = (body.session_id or "").strip() or (acc.get("session_id") or "")
    if not sid:
        return JSONResponse(
            {"ok": False, "error": "No session id."}, status_code=400
        )
    if body.save and (body.session_id or "").strip():
        db.update_account(
            aid, session_id=body.session_id.strip(), last_verified=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        res = cword_auth.check_session(body.session_id.strip())
    else:
        res = cword_auth.check_session(sid)
    if res.get("ok"):
        un = res.get("username", "")
        if un:
            db.update_account(aid, cw_username=un, status="active", last_verified=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return JSONResponse(
            {
                "ok": True,
                "message": res.get("message", f"OK ({un})" if un else "OK"),
            }
        )
    return JSONResponse({"ok": False, "error": res.get("error", "?")}, status_code=401)


@app.post("/api/jobs/{job_id}/bid-mark")
def api_bid_mark(job_id: int, body: BidMarkBody) -> JSONResponse:
    jid = str(job_id)
    if body.mark:
        t = next(
            (j.get("title") for j in load_jobs() if str(j.get("job_offer_id")) == jid),
            "",
        )
        db.record_bid(
            jid,
            job_title=str(t or "")[:200],
            status="success",
            account_id=body.account_id,
        )
        db.add_log(
            f"Mark bid placed for job {jid} (web).", level="info", account_id=body.account_id
        )
    else:
        db.delete_bid(body.account_id, jid)
        db.add_log(f"Unmark bid for job {jid} (web).", level="info", account_id=body.account_id)
    return JSONResponse({"ok": True})


@app.post("/api/jobs/{job_id}/retry-failed")
def api_retry_failed(job_id: int, body: RetryBody) -> JSONResponse:
    jid = str(job_id)
    st = db.get_bid_statuses(body.account_id).get(jid, "")
    if st == "already_bid":
        return JSONResponse(
            {
                "ok": False,
                "error": "This job is already_bid on CrowdWorks. No retry.",
            },
        )
    if st != "failed":
        return JSONResponse(
            {
                "ok": False,
                "error": "Only failed bids can be cleared (or clear mark first).",
            },
        )
    accounts: list[dict] = []
    if body.account_id is not None:
        a = db.get_account(body.account_id)
        if a:
            accounts = [a]
    else:
        for a2 in db.list_accounts():
            if a2["enabled"] and db.get_bid_statuses(a2["id"]).get(jid) == "failed":
                accounts.append(a2)
        if not accounts:
            accounts = [a for a in db.list_accounts() if a["enabled"]]
    if not accounts:
        return JSONResponse({"ok": False, "error": "No enabled account."}, status_code=400)
    for a in accounts:
        db.delete_bid(a["id"], jid)
    msg = "Failed records cleared. Re-draft and submit on the web, or use the desktop for full Playwright auto-bid."
    db.add_log(
        f"Web retry: cleared failed bid for job {jid} ({len(accounts)} acc).", level="info"
    )
    return JSONResponse({"ok": True, "message": msg})


@app.post("/api/jobs/{job_id}/draft-proposal")
def draft_proposal_api(job_id: int, body: DraftProposalBody) -> JSONResponse:
    jobs = load_jobs()
    job = next(
        (j for j in jobs if int(j.get("job_offer_id") or -1) == job_id), None
    )
    if job is None:
        return JSONResponse(
            {"ok": False, "error": "job not found in data file"}, status_code=404
        )
    key = _openai_key(body.api_key)
    if not key:
        return JSONResponse(
            {
                "ok": False,
                "error": "Missing API key (UI, OPENAI_API_KEY, or Settings).",
            },
            status_code=400,
        )
    model = (body.model or "").strip() or db.get_setting("openai_model", "gpt-4o-mini")
    extra = (body.extra_prompt or "").strip()
    if body.account_id is not None:
        acc = db.get_account(int(body.account_id))
        if acc and (acc.get("prompt_content") or "").strip():
            extra = (str(acc.get("prompt_content", "")) + "\n" + extra).strip()
    try:
        text = proposal_draft.generate_proposal_draft(
            job,
            key,
            model=model,
            fetch_full_description=body.fetch_full_description,
            extra_prompt=extra,
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=502
        )
    u = f"https://crowdworks.jp/proposals/new?job_offer_id={job_id}"
    return JSONResponse({"ok": True, "text": text, "proposal_url": u})


@app.post("/api/jobs/{job_id}/submit-bid")
def submit_bid_api(job_id: int, body: SubmitBidBody) -> JSONResponse:
    sid = (body.session_id or "").strip()
    if body.account_id is not None:
        acc = db.get_account(int(body.account_id))
        if acc and not sid:
            sid = (acc.get("session_id") or "").strip()
    if not sid:
        return JSONResponse(
            {
                "ok": False,
                "error": "No session. Pick an account with a saved _cw_session_id, or paste override.",
            },
            status_code=400,
        )
    if not (body.message or "").strip():
        return JSONResponse(
            {"ok": False, "error": "Proposal message is required."}, status_code=400
        )
    try:
        s = cword_auth.session_from_cookie(sid)
        u = cword_auth.submit_bid(
            s, job_id, body.message, price=body.price or ""
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=502
        )
    if body.account_id is not None and u:
        try:
            t0 = next(
                (
                    j.get("title")
                    for j in load_jobs()
                    if int(j.get("job_offer_id") or 0) == int(job_id)
                ),
                "",
            )
            db.record_bid(
                str(job_id),
                job_title=str(t0 or "")[:200],
                result_url=str(u),
                account_id=body.account_id,
                status="success",
            )
        except (TypeError, OSError) as e:
            db.add_log(
                f"Web submit: bid posted but could not log to DB: {e}",
                level="warning",
                account_id=body.account_id,
            )
    return JSONResponse({"ok": True, "url": u})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if WEB_TOKEN:

    class _CwordWebAccessMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            p = (request.url.path or "/").rstrip("/") or "/"
            raw = request.url.path or ""
            if raw.startswith("/static/") or raw == "/static":
                return await call_next(request)
            if p in ("/health", "/auth/login"):
                return await call_next(request)
            if _request_authorized(request):
                return await call_next(request)
            accept = (request.headers.get("accept") or "").lower()
            if p.startswith("/api/") or "application/json" in accept:
                return JSONResponse(
                    {
                        "detail": "Not authenticated. CWORD_WEB_TOKEN, Bearer, or /auth/login.",
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
