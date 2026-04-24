"""
CrowdWorks Bot — web UI (FastAPI + Jinja2) aligned with the desktop app layout,
colours, SQLite data, and the same main sections: Dashboard, Accounts, Jobs, Settings, Log.

Run: uvicorn app:app --reload --host 127.0.0.1 --port 8000
  Or: python app.py
"""

from __future__ import annotations

import concurrent.futures
import os
import secrets
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field as _dc_field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import authx
import db
from fastapi import FastAPI, Form, Query, Request
from starlette.middleware.sessions import SessionMiddleware
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

# Signed cookie session (email/password). Use a long random string in production.
def _session_secret() -> str:
    s = (os.environ.get("SESSION_SECRET") or "").strip()
    if len(s) >= 32:
        return s
    return "cword-dev-sessions-CHANGE_ME-" + "x" * 16

ALLOW_REGISTER = (os.environ.get("ALLOW_REGISTER", "1") or "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


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
        return False
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


def get_effective_user_id(request: Request) -> int | None:
    raw = request.session.get("user_id")
    if raw is not None:
        try:
            u = int(raw)
            if u < 1:
                return None
        except (TypeError, ValueError):
            return None
        else:
            return u
    if _request_authorized(request):
        return int(db.DESKTOP_USER_ID)
    return None


def _is_public_path(path: str) -> bool:
    p = (path or "/").rstrip("/") or "/"
    if p in ("/login", "/register", "/favicon.ico", "/health", "/auth/token"):
        return True
    if p.startswith("/static"):
        return True
    return False


def _u(request: Request) -> int:
    return int(request.state.user_id)


def load_jobs() -> list[dict]:
    return load_jobs_jsonl(DATA_FILE)


def data_file_updated_utc() -> str | None:
    if not DATA_FILE.is_file():
        return None
    ts = DATA_FILE.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ctx(request: Request, current_page: str, **kwargs: object) -> dict[str, object]:
    auth_email = ""
    uid = getattr(request.state, "user_id", None)
    if uid is not None:
        row = db.user_by_id(int(uid))
        if row:
            auth_email = (str(row.get("email") or ""))[:80]
    base: dict[str, object] = {
        "request": request,
        "current_page": current_page,
        "status_message": kwargs.pop("status_message", "Ready."),
        "auth_email": auth_email,
    }
    base.update(kwargs)
    return base


def _build_job_rows(
    user_id: int,
    acc_id: int | None,
) -> list[dict]:
    jobs = load_jobs()
    bid_statuses = db.get_bid_statuses(user_id, acc_id)
    bid_errors = db.get_bid_errors(user_id, acc_id)
    if acc_id is not None:
        acc = db.get_account(user_id, acc_id)
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


# ── Access (web login + optional deploy token) ──────────────────────────────


@app.get("/auth/token", response_class=HTMLResponse)
def auth_token_gate(
    request: Request,
    token: str = "",
    nxt: str = Query("/", alias="next", max_length=2500),
) -> object:
    if not WEB_TOKEN:
        return RedirectResponse("/login", status_code=302)
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
            f'<p>Example: <code>/auth/token?token=…&amp;next={nq}</code></p></body></html>',
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


@app.get("/login", response_class=HTMLResponse)
def page_login(
    request: Request,
    nxt: str = Query("/", alias="next", max_length=2500),
    error: str = "",
) -> object:
    if getattr(request.state, "user_id", None) is not None:
        return RedirectResponse(_safe_next(nxt), status_code=302)
    return templates.TemplateResponse(
        request,
        "pages/login.html",
        {
            "request": request,
            "page_title": "Sign in",
            "next_url": _safe_next(nxt),
            "error": (error or "").strip(),
            "web_token_help": bool(WEB_TOKEN),
        },
    )


@app.post("/login", response_class=HTMLResponse)
def post_login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    nxt: str = Form("/"),
) -> object:
    e = (email or "").strip().lower()
    p = password or ""
    nxt2 = _safe_next(nxt)
    if e in ("__local@desktop",) or e.startswith("__local@"):
        return RedirectResponse(
            f"/login?error=Invalid%20credentials&next={quote(nxt2, safe='')}",
            status_code=303,
        )
    row = db.user_by_email(e)
    if not row or not authx.verify_password(p, str(row.get("password_hash") or "")):
        return RedirectResponse(
            f"/login?error=Invalid%20email%20or%20password&next={quote(nxt2, safe='')}",
            status_code=303,
        )
    if int(row["id"]) == int(db.DESKTOP_USER_ID):
        return RedirectResponse(
            f"/login?error=That%20account%20is%20for%20the%20local%20desktop%20app%20only&next={quote(nxt2, safe='')}",
            status_code=303,
        )
    request.session["user_id"] = int(row["id"])
    return RedirectResponse(nxt2, status_code=303)


@app.get("/register", response_class=HTMLResponse)
def page_register(
    request: Request,
    error: str = Query("", max_length=500),
) -> object:
    if not ALLOW_REGISTER:
        return HTMLResponse("Registration is disabled on this instance.", status_code=403)
    if getattr(request.state, "user_id", None):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(
        request,
        "pages/register.html",
        {
            "request": request,
            "page_title": "Create account",
            "error": (error or "").strip(),
        },
    )


@app.post("/register", response_class=HTMLResponse)
def post_register(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
) -> object:
    if not ALLOW_REGISTER:
        return HTMLResponse("Registration is disabled on this instance.", status_code=403)
    e = (email or "").strip()
    if not authx.validate_email(e):
        return RedirectResponse(
            "/register?error=Enter%20a%20valid%20email%20address.",
            status_code=303,
        )
    err = authx.validate_password(password or "")
    if err:
        from urllib.parse import quote as _q

        return RedirectResponse(f"/register?error={_q(err)}", status_code=303)
    if (password or "") != (password2 or ""):
        return RedirectResponse("/register?error=Passwords%20do%20not%20match.", status_code=303)
    if db.user_by_email(e):
        return RedirectResponse(
            "/register?error=That%20email%20is%20already%20registered.",
            status_code=303,
        )
    h = authx.hash_password((password or "").strip())
    uid = db.user_register(e.lower(), h)
    request.session["user_id"] = int(uid)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout", response_class=HTMLResponse)
def logout(request: Request) -> object:
    request.session.clear()
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("cword_session", path="/")
    if WEB_TOKEN and (request.cookies.get(CWORD_ACCESS_COOKIE) or "").strip():
        r.set_cookie(
            CWORD_ACCESS_COOKIE,
            max_age=0,
            path="/",
            samesite="lax",
            httponly=True,
            value="",
        )
    return r


# ── Page routes (desktop parity) ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root() -> object:
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
def page_dashboard(request: Request) -> object:
    u = _u(request)
    accts = db.list_accounts(u)
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
            recent_logs=db.list_logs(u, limit=20),
        ),
    )


@app.get("/accounts", response_class=HTMLResponse)
def page_accounts(request: Request, message: str = "", error: str = "") -> object:
    u = _u(request)
    accts = db.list_accounts(u)
    bid_counts = {a["id"]: db.count_bids(u, a["id"]) for a in accts}
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
    u = _u(request)
    return templates.TemplateResponse(
        request,
        "pages/account_form.html",
        _ctx(
            request,
            "Accounts",
            page_title="Add Account",
            account=None,
            prompts=db.list_prompts(u),
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
    u = _u(request)
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
                "prompts": db.list_prompts(u),
                "form_action": "/accounts",
                "error": "Session ID is required.",
            },
        )
    new_id = db.add_account(
        u,
        (name or "New Account").strip() or "New Account",
        session_id.strip(),
        prompt_id=pid,
        enabled=en,
    )
    db.add_log(u, f"Account added (id {new_id}).", level="info")
    return RedirectResponse("/accounts?message=Saved", status_code=303)


@app.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
def page_account_edit(request: Request, account_id: int) -> object:
    u = _u(request)
    acc = db.get_account(u, account_id)
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
            prompts=db.list_prompts(u),
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
    u = _u(request)
    acc = db.get_account(u, account_id)
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
                "prompts": db.list_prompts(u),
                "form_action": f"/accounts/{account_id}/update",
                "error": "Session ID is required.",
            },
        )
    en = 1 if enabled in ("1", "on", "true") else 0
    db.update_account(
        u,
        account_id,
        name=(name or acc["name"]).strip(),
        session_id=session_id.strip(),
        prompt_id=_get_prompt_id(prompt_id),
        enabled=en,
    )
    return RedirectResponse("/accounts?message=Updated", status_code=303)


@app.post("/accounts/{account_id}/delete", response_class=HTMLResponse)
def post_account_delete(request: Request, account_id: int) -> object:
    u = _u(request)
    if db.get_account(u, account_id):
        db.delete_account(u, account_id)
    return RedirectResponse("/accounts?message=Deleted", status_code=303)


@app.post("/accounts/{account_id}/verify", response_class=HTMLResponse)
def post_account_verify(request: Request, account_id: int) -> object:
    u = _u(request)
    acc = db.get_account(u, account_id)
    if not acc:
        return RedirectResponse("/accounts?error=Not%20found", status_code=302)
    res = cword_auth.check_session(acc["session_id"])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if res.get("ok"):
        uname = res.get("username", "")
        db.update_account(
            u,
            account_id, status="active", cw_username=uname or acc.get("cw_username") or "",
            last_verified=now,
        )
        db.add_log(
            u,
            f"Session verified for '{acc['name']}' ({uname}).",
            level="success",
            account_id=account_id,
        )
    else:
        db.update_account(u, account_id, status="expired", last_verified=now)
        db.add_log(
            u,
            f"Session check failed for '{acc['name']}': {res.get('error', '')}",
            level="error",
            account_id=account_id,
        )
    return RedirectResponse("/accounts?message=Verify%20done", status_code=303)


@app.post("/accounts/{account_id}/toggle", response_class=HTMLResponse)
def post_account_toggle(request: Request, account_id: int) -> object:
    u = _u(request)
    acc = db.get_account(u, account_id)
    if acc:
        db.update_account(u, account_id, enabled=0 if acc["enabled"] else 1)
    return RedirectResponse("/accounts", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def page_jobs(
    request: Request,
    acc: int | None = Query(None, description="Account filter"),
    refreshed: int = 0,
    error: str = "",
) -> object:
    u = _u(request)
    def _f_delay() -> str:
        try:
            v = float(db.get_setting(u, "feed_delay", "1.0") or 1.0)
            return f"{v:.1f}" if v == int(v) else str(v)
        except ValueError:
            return "1.0"

    job_rows = _build_job_rows(u, acc)
    alist = db.list_accounts(u)
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
            default_model=db.get_setting(u, "openai_model", "gpt-4o-mini"),
        ),
    )


@app.post("/refresh", response_class=HTMLResponse)
def post_refresh(
    request: Request,
    delay: float = Form(1.0),
) -> object:
    u = _u(request)
    # Prefer saved feed delay if form sends nothing meaningful
    try:
        dcfg = float(db.get_setting(u, "feed_delay", "1.0") or 1.0)
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
        db.add_log(u, f"Scrape complete ({len(jobs)} jobs) — web refresh.", level="info")
    except Exception as e:
        db.add_log(u, f"Scrape error: {e!s}", level="error")
        return templates.TemplateResponse(
            request,
            "pages/jobs.html",
            _ctx(
                request,
                "Jobs",
                error=f"{type(e).__name__}: {e}",
                job_rows=_build_job_rows(u, None),
                accounts_list=db.list_accounts(u),
                acc_filter=None,
                feeds_category_menu=FEEDS_CATEGORY_MENU,
                data_updated=data_file_updated_utc(),
                refreshed=False,
                default_feed_delay="1.0",
                default_model=db.get_setting(u, "openai_model", "gpt-4o-mini"),
            ),
            status_code=500,
        )
    return RedirectResponse("/jobs?refreshed=1", status_code=303)


def _settings_dict(user_id: int) -> dict[str, str]:
    d = {
        "openai_api_key": db.get_setting(user_id, "openai_api_key", ""),
        "scrape_interval": db.get_setting(user_id, "scrape_interval", "30"),
        "feed_delay": db.get_setting(user_id, "feed_delay", "1.0"),
        "max_scrape_pages": db.get_setting(user_id, "max_scrape_pages", "3"),
        "max_parallel_bids": db.get_setting(user_id, "max_parallel_bids", "10"),
        "bid_max_age_hours": db.get_setting(user_id, "bid_max_age_hours", "48"),
        "show_browser": db.get_setting(user_id, "show_browser", "1"),
        "bid_price_pct": db.get_setting(user_id, "bid_price_pct", "0"),
        "openai_model": db.get_setting(user_id, "openai_model", "gpt-4o-mini"),
    }
    return d


@app.get("/settings", response_class=HTMLResponse)
def page_settings(
    request: Request, message: str = "", error: str = ""
) -> object:
    u = _u(request)
    return templates.TemplateResponse(
        request,
        "pages/settings.html",
        _ctx(
            request,
            "Settings",
            page_title="Settings",
            s=_settings_dict(u),
            ai_models=AI_MODELS,
            prompts=db.list_prompts(u),
            message=message,
            error=error,
        ),
    )


@app.post("/settings/save", response_class=HTMLResponse)
def post_settings_save(
    request: Request,
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
    u = _u(request)
    db.set_setting(u, "openai_api_key", (openai_api_key or "").strip())
    try:
        db.set_setting(u, "scrape_interval", str(max(10, int(scrape_interval))))
    except ValueError:
        pass
    try:
        db.set_setting(u, "feed_delay", str(max(0.0, float(feed_delay))))
    except ValueError:
        pass
    try:
        db.set_setting(u, "max_scrape_pages", str(max(1, int(max_scrape_pages))))
    except ValueError:
        pass
    try:
        db.set_setting(u, "max_parallel_bids", str(max(1, int(max_parallel_bids))))
    except ValueError:
        pass
    try:
        db.set_setting(u, "bid_max_age_hours", str(max(1, int(bid_max_age_hours))))
    except ValueError:
        pass
    db.set_setting(u, "show_browser", "1" if show_browser in ("1", "on") else "0")
    try:
        p = int(bid_price_pct)
        db.set_setting(u, "bid_price_pct", str(max(0, min(100, p))))
    except ValueError:
        pass
    m = (openai_model or "").strip()
    if m:
        db.set_setting(u, "openai_model", m)
    db.add_log(u, "Settings saved (web).", level="info")
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
    u = _u(request)
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
    db.add_prompt(u, n, c)
    return RedirectResponse("/settings?message=Prompt%20added", status_code=303)


@app.get("/prompts/{pid}/edit", response_class=HTMLResponse)
def page_prompt_edit(request: Request, pid: int) -> object:
    u = _u(request)
    p = db.get_prompt(u, pid)
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
    u = _u(request)
    if not db.get_prompt(u, pid):
        return RedirectResponse("/settings?error=notfound", status_code=302)
    n, c = (name or "").strip(), (content or "").strip()
    if n and c:
        db.update_prompt(u, pid, n, c)
    return RedirectResponse("/settings?message=Prompt%20updated", status_code=303)


@app.post("/prompts/{pid}/delete", response_class=HTMLResponse)
def post_prompt_delete(request: Request, pid: int) -> object:
    u = _u(request)
    if db.get_prompt(u, pid):
        db.delete_prompt(u, pid)
    return RedirectResponse("/settings?message=Deleted", status_code=303)


@app.get("/log", response_class=HTMLResponse)
def page_log(request: Request) -> object:
    u = _u(request)
    return templates.TemplateResponse(
        request,
        "pages/log.html",
        _ctx(
            request,
            "Log",
            page_title="Activity Log",
            logs=db.list_logs(u, limit=300),
        ),
    )


@app.post("/log/clear", response_class=HTMLResponse)
def post_log_clear(request: Request) -> object:
    u = _u(request)
    db.clear_logs(u)
    db.add_log(u, "Logs cleared (web).", level="info")
    return RedirectResponse("/log", status_code=303)


# ── JSON API ─────────────────────────────────────────────────────────────────


@app.get("/api/jobs")
def api_jobs_list(request: Request) -> JSONResponse:
    u = _u(request)
    return JSONResponse(
        {
            "jobs": _build_job_rows(u, None),
        }
    )


def _openai_key(user_id: int, body_key: str) -> str:
    k = (body_key or "").strip() or (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not k:
        k = (db.get_setting(user_id, "openai_api_key") or "").strip()
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
def api_check_account_session(
    request: Request, aid: int, body: CheckSessionJson
) -> JSONResponse:
    u = _u(request)
    acc = db.get_account(u, aid)
    if not acc:
        return JSONResponse({"ok": False, "error": "no account"}, status_code=404)
    sid = (body.session_id or "").strip() or (acc.get("session_id") or "")
    if not sid:
        return JSONResponse(
            {"ok": False, "error": "No session id."}, status_code=400
        )
    if body.save and (body.session_id or "").strip():
        db.update_account(
            u,
            aid, session_id=body.session_id.strip(), last_verified=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        res = cword_auth.check_session(body.session_id.strip())
    else:
        res = cword_auth.check_session(sid)
    if res.get("ok"):
        un = res.get("username", "")
        if un:
            db.update_account(u, aid, cw_username=un, status="active", last_verified=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return JSONResponse(
            {
                "ok": True,
                "message": res.get("message", f"OK ({un})" if un else "OK"),
            }
        )
    return JSONResponse({"ok": False, "error": res.get("error", "?")}, status_code=401)


@app.post("/api/jobs/{job_id}/bid-mark")
def api_bid_mark(request: Request, job_id: int, body: BidMarkBody) -> JSONResponse:
    u = _u(request)
    jid = str(job_id)
    if body.mark:
        t = next(
            (j.get("title") for j in load_jobs() if str(j.get("job_offer_id")) == jid),
            "",
        )
        db.record_bid(
            u,
            jid,
            job_title=str(t or "")[:200],
            status="success",
            account_id=body.account_id,
        )
        db.add_log(
            u,
            f"Mark bid placed for job {jid} (web).", level="info", account_id=body.account_id
        )
    else:
        db.delete_bid(u, body.account_id, jid)
        db.add_log(u, f"Unmark bid for job {jid} (web).", level="info", account_id=body.account_id)
    return JSONResponse({"ok": True})


@app.post("/api/jobs/{job_id}/retry-failed")
def api_retry_failed(request: Request, job_id: int, body: RetryBody) -> JSONResponse:
    u = _u(request)
    jid = str(job_id)
    st = db.get_bid_statuses(u, body.account_id).get(jid, "")
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
        a = db.get_account(u, body.account_id)
        if a:
            accounts = [a]
    else:
        for a2 in db.list_accounts(u):
            if a2["enabled"] and db.get_bid_statuses(u, a2["id"]).get(jid) == "failed":
                accounts.append(a2)
        if not accounts:
            accounts = [a for a in db.list_accounts(u) if a["enabled"]]
    if not accounts:
        return JSONResponse({"ok": False, "error": "No enabled account."}, status_code=400)
    for a in accounts:
        db.delete_bid(u, a["id"], jid)
    msg = "Failed records cleared. Re-draft and submit on the web, or use the desktop for full Playwright auto-bid."
    db.add_log(
        u,
        f"Web retry: cleared failed bid for job {jid} ({len(accounts)} acc).", level="info"
    )
    return JSONResponse({"ok": True, "message": msg})


@app.post("/api/jobs/{job_id}/draft-proposal")
def draft_proposal_api(request: Request, job_id: int, body: DraftProposalBody) -> JSONResponse:
    u = _u(request)
    jobs = load_jobs()
    job = next(
        (j for j in jobs if int(j.get("job_offer_id") or -1) == job_id), None
    )
    if job is None:
        return JSONResponse(
            {"ok": False, "error": "job not found in data file"}, status_code=404
        )
    key = _openai_key(u, body.api_key)
    if not key:
        return JSONResponse(
            {
                "ok": False,
                "error": "Missing API key (UI, OPENAI_API_KEY, or Settings).",
            },
            status_code=400,
        )
    model = (body.model or "").strip() or db.get_setting(u, "openai_model", "gpt-4o-mini")
    extra = (body.extra_prompt or "").strip()
    if body.account_id is not None:
        acc = db.get_account(u, int(body.account_id))
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
    proposal_url = f"https://crowdworks.jp/proposals/new?job_offer_id={job_id}"
    return JSONResponse({"ok": True, "text": text, "proposal_url": proposal_url})


@app.post("/api/jobs/{job_id}/submit-bid")
def submit_bid_api(request: Request, job_id: int, body: SubmitBidBody) -> JSONResponse:
    user_id = _u(request)
    sid = (body.session_id or "").strip()
    if body.account_id is not None:
        acc = db.get_account(user_id, int(body.account_id))
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
        bid_url = cword_auth.submit_bid(
            s, job_id, body.message, price=body.price or ""
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=502
        )
    if body.account_id is not None and bid_url:
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
                user_id,
                str(job_id),
                job_title=str(t0 or "")[:200],
                result_url=str(bid_url),
                account_id=body.account_id,
                status="success",
            )
        except (TypeError, OSError) as e:
            db.add_log(
                user_id,
                f"Web submit: bid posted but could not log to DB: {e}",
                level="warning",
                account_id=body.account_id,
            )
    return JSONResponse({"ok": True, "url": bid_url})


# ── Bot (per-user background loop) ───────────────────────────────────────────

@dataclass
class _BotRun:
    running: bool = False
    thread: threading.Thread | None = None
    processed: int = 0
    status: str = "Stopped"
    last_scrape_at: str = ""
    stop_event: threading.Event = _dc_field(default_factory=threading.Event)


_bot_runs: dict[int, _BotRun] = {}
_bot_lock = threading.Lock()


def _get_or_create_run(user_id: int) -> _BotRun:
    if user_id not in _bot_runs:
        _bot_runs[user_id] = _BotRun()
    return _bot_runs[user_id]


def _bot_loop(user_id: int, run: _BotRun) -> None:
    run.status = "Starting…"
    db.add_log(user_id, "Bot started (web).", level="info")

    while not run.stop_event.is_set():
        try:
            # Read live settings each iteration
            interval    = max(10,  int(db.get_setting(user_id, "scrape_interval", "30")))
            delay       = max(0.0, float(db.get_setting(user_id, "feed_delay", "1.0")))
            max_bids    = max(1,   int(db.get_setting(user_id, "max_parallel_bids", "10")))
            bid_max_age = max(1,   int(db.get_setting(user_id, "bid_max_age_hours", "48")))
            model       = db.get_setting(user_id, "openai_model", "gpt-4o-mini") or "gpt-4o-mini"
            openai_key  = (db.get_setting(user_id, "openai_api_key") or "").strip() \
                          or (os.environ.get("OPENAI_API_KEY") or "").strip()

            # ── Scrape ────────────────────────────────────────────────────
            run.status = "Scraping jobs…"
            with _scrape_lock:
                try:
                    jobs, _  = scrape_new_postings_feeds(delay_s=delay, timeout=120.0, include_raw=False)
                    write_jsonl(str(DATA_FILE), jobs)
                    run.last_scrape_at = datetime.now().strftime("%H:%M:%S")
                    db.add_log(user_id, f"Bot: scraped {len(jobs)} jobs.", level="info")
                except Exception as exc:
                    db.add_log(user_id, f"Bot scrape error: {exc}", level="error")
                    run.status = f"Scrape error — retry in {interval}s"
                    run.stop_event.wait(interval)
                    continue

            if run.stop_event.is_set():
                break

            if not openai_key:
                db.add_log(user_id, "Bot: OpenAI key not set — skipping bids.", level="warning")
                run.status = f"No API key — waiting {interval}s"
                run.stop_event.wait(interval)
                continue

            # ── Auto-bid for each enabled account ─────────────────────────
            accounts = [a for a in db.list_accounts(user_id) if a["enabled"]]
            cutoff   = datetime.now(tz=timezone.utc) - timedelta(hours=bid_max_age)

            def _bid_account(acc: dict) -> int:
                """Returns count of bids submitted for this account in this cycle."""
                sid = (acc.get("session_id") or "").strip()
                if not sid:
                    return 0
                count = 0
                extra_prompt = (acc.get("prompt_content") or "").strip()
                for job in jobs:
                    if run.stop_event.is_set():
                        break
                    jid = str(job.get("job_offer_id") or "")
                    if not jid:
                        continue
                    # Age filter
                    try:
                        rel = job.get("last_released_at") or ""
                        if rel:
                            jdt = datetime.fromisoformat(rel.replace("Z", "+00:00"))
                            if jdt.tzinfo is None:
                                jdt = jdt.replace(tzinfo=timezone.utc)
                            if jdt < cutoff:
                                continue
                    except Exception:
                        pass
                    if db.has_bid(user_id, acc["id"], jid):
                        continue
                    # Generate proposal
                    try:
                        text = proposal_draft.generate_proposal_draft(
                            job, openai_key, model=model,
                            fetch_full_description=True, extra_prompt=extra_prompt,
                        )
                    except Exception as exc:
                        db.record_bid(user_id, jid, account_id=acc["id"],
                                      status="failed", error_msg=str(exc)[:300])
                        db.add_log(user_id, f"Bot: proposal error job {jid}: {exc}",
                                   level="error", account_id=acc["id"])
                        continue
                    # Submit bid
                    try:
                        sess = cword_auth.session_from_cookie(sid)
                        url  = cword_auth.submit_bid(sess, int(jid), text)
                        db.record_bid(user_id, jid,
                                      job_title=(job.get("title") or "")[:200],
                                      result_url=str(url or ""),
                                      account_id=acc["id"], status="success")
                        db.add_log(user_id, f"Bot: bid submitted job {jid}.",
                                   level="success", account_id=acc["id"])
                        count += 1
                    except Exception as exc:
                        msg  = str(exc)
                        stat = "already_bid" if "already" in msg.lower() else "failed"
                        db.record_bid(user_id, jid, account_id=acc["id"],
                                      status=stat, error_msg=msg[:300])
                        lv   = "info" if stat == "already_bid" else "error"
                        db.add_log(user_id, f"Bot: submit error job {jid}: {msg}",
                                   level=lv, account_id=acc["id"])
                return count

            if accounts:
                run.status = f"Bidding ({len(accounts)} accounts)…"
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_bids, len(accounts))) as pool:
                    results = list(pool.map(_bid_account, accounts))
                cycle_bids = sum(results)
                run.processed += cycle_bids
                if cycle_bids:
                    db.add_log(user_id, f"Bot: {cycle_bids} bid(s) submitted this cycle.", level="info")

            run.status = f"Waiting {interval}s… ({run.processed} bids total)"
            run.stop_event.wait(interval)

        except Exception as exc:
            db.add_log(user_id, f"Bot loop error: {exc}", level="error")
            run.stop_event.wait(30)

    run.running = False
    run.status  = "Stopped"
    db.add_log(user_id, f"Bot stopped (web). {run.processed} bids submitted.", level="info")


@app.post("/api/bot/start")
def api_bot_start(request: Request) -> JSONResponse:
    uid = _u(request)
    with _bot_lock:
        run = _get_or_create_run(uid)
        if run.running:
            return JSONResponse({"ok": True, "running": True, "status": run.status, "already": True})
        run.stop_event.clear()
        run.running   = True
        run.processed = 0
        run.status    = "Starting…"
        t = threading.Thread(target=_bot_loop, args=(uid, run), daemon=True, name=f"bot-u{uid}")
        run.thread = t
        t.start()
    return JSONResponse({"ok": True, "running": True, "status": "Starting…"})


@app.post("/api/bot/stop")
def api_bot_stop(request: Request) -> JSONResponse:
    uid = _u(request)
    with _bot_lock:
        run = _bot_runs.get(uid)
        if not run or not run.running:
            return JSONResponse({"ok": True, "running": False, "status": "Stopped"})
        run.stop_event.set()
        run.status = "Stopping…"
    return JSONResponse({"ok": True, "running": True, "status": "Stopping…"})


@app.get("/api/bot/status")
def api_bot_status(request: Request) -> JSONResponse:
    uid = _u(request)
    run = _bot_runs.get(uid)
    return JSONResponse({
        "running":        run.running        if run else False,
        "status":         run.status         if run else "Stopped",
        "processed":      run.processed      if run else 0,
        "last_scrape_at": run.last_scrape_at if run else "",
    })


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class _CwordWebAccessMiddleware(BaseHTTPMiddleware):
    """Require session login (or optional CWORD_WEB_TOKEN) for the app; public routes bypass."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path or "/"
        uid = get_effective_user_id(request)
        if uid is not None:
            request.state.user_id = int(uid)
        if _is_public_path(path):
            return await call_next(request)
        if uid is not None:
            return await call_next(request)
        accept = (request.headers.get("accept") or "").lower()
        p = (path or "/").rstrip("/") or "/"
        if p.startswith("/api/") or "application/json" in accept:
            return JSONResponse(
                {
                    "detail": "Not authenticated. Use /login or (when configured) CWORD_WEB_TOKEN. "
                    "Set Bearer / cookie /x-access-token.",
                },
                status_code=401,
            )
        here = path
        if request.url.query:
            here = f"{here}?{request.url.query}"
        return RedirectResponse(
            url=f"/login?next={quote(here, safe='')}",
            status_code=302,
        )


# Inner auth first, then Session (outer) so the session cookie is parsed before we run.
app.add_middleware(_CwordWebAccessMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    same_site="lax",
    max_age=CWORD_COOKIE_MAX_AGE,
    https_only=bool(_force_secure_cookies()),
    session_cookie="cword_session",
)


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
