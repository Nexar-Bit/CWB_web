"""
Browser-based CrowdWorks login test using Playwright.

Opens a visible Chromium window, performs the login, and prints
step-by-step events to stdout so they appear in the uvicorn CLI.
All errors (JS exceptions, console errors, network failures) are captured.
"""
from __future__ import annotations

import time
from typing import Any

CW_LOGIN_URL = "https://crowdworks.jp/login"
_CW_DOMAIN = "crowdworks.jp"
_TIMEOUT_MS = 30_000


def _log(msg: str) -> None:
    print(f"[browser-login] {msg}", flush=True)


def _is_cw_url(url: str) -> bool:
    return _CW_DOMAIN in url


def test_login_with_browser(
    email: str,
    password: str,
    *,
    headless: bool = False,
    slow_mo: int = 400,
    timeout_ms: int = _TIMEOUT_MS,
) -> dict[str, Any]:
    """
    Open a Chromium browser, navigate to CrowdWorks login,
    fill credentials, submit, and report the outcome.

    All significant browser events are printed to stdout for CLI monitoring.
    Returns {"ok": True} on success or {"ok": False, "error": "..."} on failure.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
    except ImportError:
        return {
            "ok": False,
            "error": (
                "playwright is not installed. "
                "Run: pip install playwright && playwright install chromium"
            ),
        }

    _log("=" * 60)
    _log("Browser login test starting")
    _log(f"  Target URL : {CW_LOGIN_URL}")
    _log(f"  Email      : {email}")
    _log(f"  Headless   : {headless}")
    _log("=" * 60)

    result: dict[str, Any] = {"ok": False, "error": "unknown"}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            slow_mo=slow_mo,
            args=["--window-size=1280,800"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        # ── Event listeners ──────────────────────────────────────────────────

        def on_console(msg: Any) -> None:
            # Log only warnings and errors to keep output focused
            if msg.type in ("warning", "error"):
                _log(f"[console.{msg.type}] {msg.text[:200]}")

        def on_page_error(err: Any) -> None:
            _log(f"[JS-exception] {str(err)[:300]}")

        def on_request_failed(req: Any) -> None:
            if _is_cw_url(req.url):
                _log(f"[request-failed] {req.method} {req.url[:100]} — {req.failure}")

        def on_response(res: Any) -> None:
            # Log non-2xx/3xx responses from CrowdWorks only
            if _is_cw_url(res.url) and res.status >= 400:
                _log(f"[HTTP-{res.status}] {res.url[:100]}")

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)
        page.on("response", on_response)

        try:
            # ── Step 1: Navigate to login page ───────────────────────────────
            _log("Step 1 — Navigating to login page…")
            page.goto(CW_LOGIN_URL, wait_until="load", timeout=timeout_ms)
            _log(f"         Page title : {page.title()!r}")
            _log(f"         Current URL: {page.url}")

            # ── Step 2: Wait for the form to be ready ────────────────────────
            _log("Step 2 — Waiting for login form…")
            page.wait_for_selector('input[name="username"]', timeout=timeout_ms)
            _log("         Form is visible.")

            # ── Step 3: Fill credentials ─────────────────────────────────────
            _log("Step 3 — Filling email…")
            page.fill('input[name="username"]', email)

            _log("Step 4 — Filling password…")
            page.fill('input[name="password"]', password)

            # ── Step 5: Submit ───────────────────────────────────────────────
            _log("Step 5 — Clicking login button…")
            page.click('button[type="submit"]')

            # ── Step 6: Wait for navigation after submit ─────────────────────
            _log("Step 6 — Waiting for post-submit navigation…")
            try:
                page.wait_for_url(
                    lambda url: "crowdworks.jp" in url and "/login" not in url,
                    timeout=timeout_ms,
                )
            except PlaywrightTimeout:
                # Still on the login URL — credentials may be wrong or WAF blocked
                pass

            final_url = page.url
            _log(f"         Final URL  : {final_url}")
            _log(f"         Page title : {page.title()!r}")

            # ── Step 7: Determine outcome ────────────────────────────────────
            _log("Step 7 — Checking login outcome…")

            if "/login" in final_url:
                # Landed back on the login page — find an error message
                error_text = ""
                for selector in [
                    "[class*='error']",
                    "[class*='alert']",
                    ".banner",
                    "[role='alert']",
                ]:
                    try:
                        el = page.query_selector(selector)
                        if el:
                            t = el.inner_text().strip()
                            if t:
                                error_text = t
                                break
                    except Exception:
                        pass

                msg = error_text or (
                    "Still on login page after submit. "
                    "Credentials may be incorrect, or a WAF challenge blocked the request."
                )
                _log(f"RESULT: FAILED — {msg}")
                result = {"ok": False, "error": msg}

            else:
                page_html = page.content()
                if (
                    "ログアウト" in page_html
                    or "/logout" in page_html
                    or "mypage" in final_url
                    or "dashboard" in final_url
                ):
                    _log("RESULT: SUCCESS — user is authenticated on CrowdWorks.")
                    result = {"ok": True}
                else:
                    _log(f"RESULT: AMBIGUOUS — URL changed but login state unclear.")
                    result = {
                        "ok": False,
                        "error": f"Login result unclear. Final URL: {final_url}",
                    }

            # Keep browser open briefly so user can see the result
            _log("         Keeping browser open for 4 seconds…")
            time.sleep(4)

        except PlaywrightTimeout as exc:
            _log(f"RESULT: TIMEOUT — {exc}")
            result = {"ok": False, "error": f"Timeout waiting for page: {exc}"}
        except Exception as exc:
            _log(f"RESULT: ERROR — {type(exc).__name__}: {exc}")
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            _log("Closing browser.")
            try:
                browser.close()
            except Exception:
                pass

    _log("=" * 60)
    _log(f"Login test finished — ok={result.get('ok')}")
    if not result.get("ok"):
        _log(f"Error detail: {result.get('error')}")
    _log("=" * 60)
    return result
