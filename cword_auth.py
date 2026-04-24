"""
CrowdWorks login and bid submission.

Uses requests.Session to handle cookies automatically.
All network calls are synchronous; run in a thread from the UI layer.
"""

from __future__ import annotations

import re

import requests

CW_BASE = "https://crowdworks.jp"
LOGIN_URL = f"{CW_BASE}/login"
PROPOSALS_URL = f"{CW_BASE}/proposals"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": _USER_AGENT,
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return s


def _extract_csrf(html: str) -> str:
    """Return CSRF token from Rails meta tag or hidden input."""
    m = re.search(
        r'<meta[^>]+name=["\']?csrf-token["\']?[^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    m = re.search(
        r'<input[^>]+name=["\']authenticity_token["\'][^>]+value=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    raise ValueError("CSRF token not found — the page structure may have changed.")


def _is_logged_in(html: str) -> bool:
    return "ログアウト" in html or "/logout" in html or "sign_out" in html


def session_from_cookie(session_id: str) -> requests.Session:
    """
    Return a requests.Session pre-loaded with an existing _cw_session_id cookie.
    Useful to skip the login form when a valid browser session is already known.
    """
    s = _make_session()
    s.cookies.set("_cw_session_id", session_id, domain="crowdworks.jp", path="/")
    return s


def check_session(session_id: str) -> dict:
    """
    Verify whether a _cw_session_id cookie is still authenticated.

    CrowdWorks is a Vue SPA — the server HTML never contains a logout link
    because Vue renders it client-side.  Instead we read the server-rendered
    _railsSettings JS object that is always embedded in every page:

        const _railsSettings = { user_id: "", user_name: "", ... };

    When authenticated the server fills in a non-empty user_id; when the
    session is invalid or expired it remains an empty string.

    Returns {"ok": True, "message": "..."} or {"ok": False, "error": "..."}.
    """
    if not session_id:
        return {"ok": False, "error": "Session ID is empty."}
    s = session_from_cookie(session_id)
    try:
        r = s.get(CW_BASE + "/", timeout=20, allow_redirects=True)
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Network error: {exc}"}

    if "/login" in r.url:
        return {"ok": False, "error": "Session has expired — redirected to login page."}

    m_id = re.search(r'user_id:\s*"([^"]*)"', r.text)
    m_name = re.search(r'user_name:\s*"([^"]*)"', r.text)
    if m_id:
        uid = m_id.group(1).strip()
        uname = m_name.group(1).strip() if m_name else ""
        if uid:
            display = uname or uid
            return {
                "ok": True,
                "message": f"Session is active — logged in as user ID {uid}.",
                "username": display,
            }
        return {"ok": False, "error": "Session is not authenticated (user_id is empty)."}

    return {
        "ok": False,
        "error": "Could not find _railsSettings in the response — page structure may have changed.",
    }


def login(email: str, password: str) -> requests.Session:
    """
    Login to CrowdWorks.
    Returns an authenticated requests.Session on success.
    Raises ValueError if credentials are rejected or the login page is unreachable.
    """
    if not email or not password:
        raise ValueError("Email and password are required.")
    session = _make_session()
    r = session.get(LOGIN_URL, timeout=20)
    r.raise_for_status()
    token = _extract_csrf(r.text)
    r2 = session.post(
        LOGIN_URL,
        data={
            "authenticity_token": token,
            "username": email,
            "password": password,
            "redirect_to": "/",
            "enable_auto_login": "1",
        },
        timeout=20,
        allow_redirects=True,
    )
    r2.raise_for_status()
    if not _is_logged_in(r2.text):
        raise ValueError(
            "Login failed — CrowdWorks did not accept the credentials. "
            "Please check your email and password."
        )
    return session


def _parse_form_action(html: str, default: str) -> str:
    """Extract the form action URL for the proposal form."""
    m = re.search(
        r'<form[^>]+action=["\']([^"\']*proposals[^"\']*)["\']',
        html,
        re.IGNORECASE,
    )
    if not m:
        return default
    action = m.group(1)
    return action if action.startswith("http") else CW_BASE + action


def submit_bid(
    session: requests.Session,
    job_id: int,
    message: str,
    price: str = "",
) -> str:
    """
    Submit a bid proposal for job_id using an authenticated session.
    Returns the final URL after redirect (typically the proposal confirmation page).
    Raises ValueError on any error.
    """
    new_url = f"{CW_BASE}/proposals/new?job_offer_id={job_id}"
    r = session.get(new_url, timeout=20)
    r.raise_for_status()

    if "proposals/new" not in r.url and "proposal" not in r.text.lower():
        raise ValueError(
            "Could not open the proposal form. "
            "The job may be closed, already bid on, or inaccessible."
        )

    token = _extract_csrf(r.text)
    action = _parse_form_action(r.text, PROPOSALS_URL)

    data: dict[str, str] = {
        "authenticity_token": token,
        "proposal[job_offer_id]": str(job_id),
        "proposal[message]": message,
    }
    if price:
        data["proposal[price]"] = price.strip()

    r2 = session.post(action, data=data, timeout=20, allow_redirects=True)
    r2.raise_for_status()

    # Check for a known error in the response
    if "エラー" in r2.text and "proposal" in r2.url:
        err_m = re.search(r'class=["\']error["\'][^>]*>([^<]+)<', r2.text)
        hint = err_m.group(1).strip() if err_m else "unknown error on submission page"
        raise ValueError(f"CrowdWorks returned a form error: {hint}")

    return r2.url
