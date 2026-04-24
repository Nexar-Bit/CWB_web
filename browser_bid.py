"""
Playwright-based bid submission for CrowdWorks Bot.

Workflow
--------
Step 0  Navigate to the public job-details page so the operator can visually
        verify the project.  Extract live deadline / budget from the page DOM
        and ld+json, then enrich the scraped job dict with those values.
Step 1  Call OpenAI (via proposal_draft.generate_bid_fields) with the enriched
        job data to produce {"message", "price", "delivery_days"}.
Step 2  Navigate to the proposal form.
Step 3  Locate and fill the message textarea.
Step 4  Fill the price / hourly-rate field.
Step 5  Fill the delivery-days field (if present).
Step 6  Click submit.
Step 7  Wait for the confirmation page and detect the outcome.

Key design choices
------------------
* wait_until="load" (not "networkidle") avoids hangs from analytics scripts.
* HTTP status is checked immediately after navigation; 403/404 short-circuits.
* Hostname-based URL filter prevents analytics query-strings from matching CW.
* "Target page … closed" is caught and surfaced as a clear user-friendly error.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

CW_BASE   = "https://crowdworks.jp"
CW_DOMAIN = "crowdworks.jp"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)

# ── Selector lists (tried in order; first visible + enabled element wins) ──────

_MSG_SELECTORS: list[str] = [
    'textarea[name="proposal[message]"]',
    'textarea[id*="message"]',
    'textarea[placeholder*="メッセージ"]',
    'textarea[placeholder*="message" i]',
    '.proposal-form textarea',
    'form textarea',
]

_PRICE_SELECTORS: list[str] = [
    # CrowdWorks actual field names (fixed-price / negotiable)
    'input[name="proposal[contract_price]"]',
    'input[name="proposal[price]"]',
    'input[name="proposal[budget]"]',
    # id-based fallbacks
    'input[id*="contract_price"]',
    'input[id*="price"]',
    'input[id*="budget"]',
    # class-based / structural fallbacks
    '[class*="contract"] input[type="number"]',
    '[class*="price"] input[type="number"]',
    '.proposal-price input',
    'input[type="number"]',          # last resort
]

_HOURLY_RATE_SELECTORS: list[str] = [
    'input[name="proposal[hourly_price]"]',
    'input[name="proposal[contract_price]"]',   # CW sometimes reuses this for hourly
    'input[name*="hourly"]',
    'input[id*="hourly"]',
]

# Payment-type keywords that indicate the price field may be absent (negotiation-based)
_NEGOTIABLE_PAYMENT_TYPES = {"negotiation", "consult", "discussion", "negotiate", "相談"}

# ── "Already bid" detection ───────────────────────────────────────────────────
# CrowdWorks shows one of these phrases when a user has already applied to a job.
# Checked against the raw page HTML after navigating to the proposal form.
_ALREADY_BID_JP: tuple[str, ...] = (
    "すでに応募しています",
    "すでに応募済",
    "応募済みです",
    "この案件には既に応募",
    "既に応募済",
    "既にこの案件に応募",
    "再応募はできません",
    "応募済みのため",
)


def _is_already_bid(html: str, url: str) -> bool:
    """Return True when the page clearly indicates the account already bid on this job."""
    if any(sig in html for sig in _ALREADY_BID_JP):
        return True
    # CrowdWorks sometimes redirects the proposals/new page to the job's public
    # page (e.g. /public/jobs/12345) when the user has already applied.
    if "proposals/new" not in url and "/proposals" not in url and "/public/jobs/" in url:
        return True
    return False

# Radio buttons for "how to present contract price"
# CrowdWorks shows: ● 契約金額を提示  ○ 相談してから金額を提案
_RADIO_SHOW_PRICE: list[str] = [
    'input[type="radio"][value="0"]',          # "show price" is often value 0
    'input[type="radio"][value="fixed"]',
    'input[type="radio"][value="show"]',
]
_RADIO_NEGOTIATE: list[str] = [
    'input[type="radio"][value="1"]',          # "negotiate" is often value 1
    'input[type="radio"][value="negotiation"]',
    'input[type="radio"][value="discuss"]',
    'input[type="radio"][value="consult"]',
]

_DELIVERY_SELECTORS: list[str] = [
    'input[name="proposal[term_of_delivery]"]',
    'input[name*="delivery"]',
    'input[name*="term"]',
    'input[id*="delivery"]',
    'input[id*="term"]',
    'input[placeholder*="日"]',
]

_SUBMIT_SELECTORS: list[str] = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("応募する")',
    'button:has-text("応募")',
    'button:has-text("送信する")',
    'button:has-text("送信")',
    '.proposal-submit-btn',
    '[data-submit]',
]


def _is_cw_host(url: str) -> bool:
    """True only when the URL's *hostname* belongs to crowdworks.jp."""
    try:
        host = urlparse(url).netloc
        return host == CW_DOMAIN or host.endswith("." + CW_DOMAIN)
    except Exception:
        return False


def _extract_live_details(page: Any, _log: Callable) -> dict[str, str]:
    """
    Pull budget and deadline from the currently-open job-detail page.

    Tries three strategies in order:
    1. ``application/ld+json`` JobPosting block (most structured).
    2. Visible text of the ``#job_offer_detail`` section (regex).
    3. Full body text (fallback regex).

    Returns a dict with any subset of: ``budget``, ``deadline``.
    """
    live: dict[str, str] = {}

    # Strategy 1 — ld+json via page.evaluate
    try:
        ld_raw = page.evaluate("""
            () => {
                for (const s of document.querySelectorAll(
                        'script[type="application/ld+json"]')) {
                    try {
                        const d = JSON.parse(s.textContent);
                        const find = (o) => {
                            if (!o || typeof o !== 'object') return null;
                            if (o['@type'] === 'JobPosting') return o;
                            for (const v of Object.values(o)) {
                                const r = find(v);
                                if (r) return r;
                            }
                            return null;
                        };
                        const jp = find(d);
                        if (jp) return JSON.stringify(jp);
                    } catch(e) {}
                }
                return null;
            }
        """)
        if ld_raw:
            jp = json.loads(ld_raw)
            vt = jp.get("validThrough")
            if vt:
                live["deadline"] = str(vt)
                _log(f"         [ld+json] 納品期限: {vt}")
            salary = jp.get("baseSalary") or jp.get("estimatedSalary")
            if isinstance(salary, dict):
                val = salary.get("value") or {}
                if isinstance(val, dict):
                    mn = val.get("minValue")
                    mx = val.get("maxValue")
                    if mn or mx:
                        live["budget"] = f"{mn}〜{mx} {salary.get('currency','JPY')}"
                        _log(f"         [ld+json] 予算: {live['budget']}")
    except Exception as ex:
        _log(f"         ld+json extraction skipped: {ex}")

    if live.get("budget") and live.get("deadline"):
        return live   # fast path — have everything we need

    # Strategy 2 — visible text scrape
    body_text = ""
    for selector in ["#job_offer_detail", "main", "body"]:
        try:
            el = page.query_selector(selector)
            if el:
                body_text = el.inner_text(timeout=5_000)
                break
        except Exception:
            pass

    if body_text:
        if not live.get("budget"):
            bm = re.search(
                r'(?:予算|報酬|金額)[^\d\n]*?([\d,]+)\s*円(?:[〜~\-–]+\s*([\d,]+)\s*円)?',
                body_text,
            )
            if bm:
                lo = bm.group(1)
                hi = bm.group(2) or ""
                live["budget"] = f"{lo}〜{hi}円".rstrip("〜円") + "円" if hi else f"{lo}円"
                _log(f"         [text] 予算: {live['budget']}")

        if not live.get("deadline"):
            dm = re.search(
                r'(?:期日|納品期限|期限)[^\d\n]*?(\d+日|\d{4}年\d+月\d+日|\d{4}-\d{2}-\d{2})',
                body_text,
            )
            if dm:
                live["deadline"] = dm.group(1)
                _log(f"         [text] 期日: {live['deadline']}")

    return live


def _fill_price_vue(page: Any, el: Any, value: str) -> None:
    """Fill a Vue.js number input robustly.

    Plain ``fill()`` sometimes fails to trigger Vue v-model reactivity on
    number inputs because Vue wraps the native setter.  This function:
    1. Uses Playwright ``fill()`` (fastest path).
    2. Falls back to the native ``HTMLInputElement.value`` setter via
       ``page.evaluate`` — bypassing the Vue wrapper — then dispatches
       both ``input`` and ``change`` events so Vue picks up the change.
    3. As a last resort, selects all text and types digit-by-digit.
    """
    # Path 1: standard fill
    try:
        el.scroll_into_view_if_needed()
        el.click()
        el.fill(value)
        return
    except Exception:
        pass

    # Path 2: native setter + Vue event dispatch
    try:
        page.evaluate(
            """([el, v]) => {
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, v);
                el.dispatchEvent(new Event('input',  { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            [el, value],
        )
        return
    except Exception:
        pass

    # Path 3: keyboard typing
    try:
        el.click()
        el.press("Control+a")
        el.press("Delete")
        el.type(value, delay=40)
    except Exception:
        pass


def submit_bid_via_browser(
    session_id: str,
    job_id: int,
    job_scraped: dict[str, Any],
    openai_key: str,
    extra_prompt: str = "",
    *,
    model: str = "gpt-4o-mini",
    headless: bool = False,
    slow_mo: int = 80,
    timeout_ms: int = 60_000,
    linger_ms: int = 500,
    bid_price_pct: int = 0,
    on_event: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Open a Chromium browser, authenticate with the session cookie, navigate
    to the project-details page, analyze it with OpenAI, then fill and submit
    the proposal form.

    Parameters
    ----------
    session_id  : ``_cw_session_id`` cookie value for authentication.
    job_id      : CrowdWorks job offer ID.
    job_scraped : Flat row from the feed scraper (pay_min, pay_max, title …).
    openai_key  : OpenAI API key used to generate bid fields.
    extra_prompt: User-supplied persona / instructions forwarded to the AI.
    model         : OpenAI model slug (default "gpt-4o-mini").
    headless      : Run without a visible window (default False).
    slow_mo       : Milliseconds inserted between each Playwright action.
    timeout_ms    : Maximum wait per step (ms).
    linger_ms     : How long to keep the browser open after the result is known.
    bid_price_pct : 0–100 — where in the pay_min…pay_max range to set the bid price.
                    0 = pay_min (minimum budget), 100 = pay_max (maximum budget).
    on_event    : Optional callback(str) for every log line.

    Returns
    -------
    {"ok": True,  "url": <final_url>}        on success
    {"ok": False, "error": <description>}    on failure
    """

    def _log(msg: str) -> None:
        print(f"[BrowserBid] {msg}", flush=True)
        if on_event:
            try:
                on_event(msg)
            except Exception:
                pass

    # ── Import Playwright ──────────────────────────────────────────────────────
    try:
        from playwright.sync_api import (  # type: ignore[import]
            sync_playwright,
            TimeoutError as PWTimeout,
        )
    except ImportError:
        return {
            "ok": False,
            "error": (
                "playwright is not installed.\n"
                "Run:  pip install playwright\n"
                "Then: playwright install chromium"
            ),
        }

    # ── Import proposal_draft locally to avoid module-level circular deps ──────
    try:
        import proposal_draft as _pd
    except ImportError as exc:
        return {"ok": False, "error": f"proposal_draft import failed: {exc}"}

    proposal_url = f"{CW_BASE}/proposals/new?job_offer_id={job_id}"
    job_url      = f"{CW_BASE}/public/jobs/{job_id}"

    _log("=" * 60)
    _log("Browser bid starting")
    _log(f"  Job ID       : {job_id}")
    _log(f"  Job title    : {(job_scraped.get('title') or '')[:80]}")
    _log(f"  Proposal URL : {proposal_url}")
    mode_label = "background" if headless else "monitoring (visible)"
    _log(f"  Mode         : {mode_label}   slow_mo: {slow_mo} ms")
    _log(f"  Price target : {bid_price_pct}% of budget range")
    _log("=" * 60)

    result: dict[str, Any] = {"ok": False, "error": "unknown"}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            slow_mo=slow_mo,
            args=["--window-size=1280,900"],
        )
        ctx = browser.new_context(
            user_agent=_UA,
            locale="ja-JP",
            viewport={"width": 1280, "height": 900},
        )

        # Inject session cookie before the first navigation
        ctx.add_cookies([
            {
                "name":     "_cw_session_id",
                "value":    session_id,
                "domain":   CW_DOMAIN,
                "path":     "/",
                "httpOnly": True,
                "secure":   True,
                "sameSite": "Lax",
            }
        ])

        page = ctx.new_page()

        # ── Event listeners ────────────────────────────────────────────────────
        def on_console(msg: Any) -> None:
            if msg.type in ("warning", "error"):
                _log(f"[console.{msg.type}] {msg.text[:200]}")

        def on_page_error(err: Any) -> None:
            _log(f"[JS-exception] {str(err)[:300]}")

        def on_request_failed(req: Any) -> None:
            if _is_cw_host(req.url):
                _log(f"[request-failed] {req.method} {req.url[:100]} — {req.failure}")

        def on_response(res: Any) -> None:
            if _is_cw_host(res.url) and res.status >= 400:
                _log(f"[HTTP-{res.status}] {res.url[:100]}")

        page.on("console",       on_console)
        page.on("pageerror",     on_page_error)
        page.on("requestfailed", on_request_failed)
        page.on("response",      on_response)

        try:
            # ── Step 0: Visit job-details page ────────────────────────────────
            _log(f"Step 0 — Viewing project details page: {job_url}")
            live: dict[str, str] = {}
            try:
                det_resp = page.goto(job_url, wait_until="load", timeout=timeout_ms)
                _log(f"         HTTP status : {det_resp.status if det_resp else '?'}")
                _log(f"         Title       : {page.title()!r}")

                live = _extract_live_details(page, _log)

                # ── Early-exit: deadline already passed ───────────────────
                deadline_str = live.get("deadline") or ""
                if deadline_str:
                    try:
                        dl = datetime.fromisoformat(deadline_str)
                        if dl.tzinfo is None:
                            dl = dl.replace(tzinfo=timezone.utc)
                        if dl < datetime.now(tz=timezone.utc):
                            result = {
                                "ok": False,
                                "error": (
                                    f"Job {job_id} deadline has already passed "
                                    f"({deadline_str}) — skipping bid."
                                ),
                            }
                            _log(f"RESULT: SKIPPED — {result['error']}")
                            time.sleep(linger_ms / 1000)
                            browser.close()
                            return result
                    except (ValueError, TypeError):
                        pass   # unparseable deadline → continue

                time.sleep(0.3)   # brief pause so page fully renders

            except PWTimeout:
                _log("         Job-details page timed out — continuing anyway.")
            except Exception as ex:
                _log(f"         Could not load job-details page: {ex}")

            # ── Step 1: Generate bid fields with OpenAI ────────────────────────
            _log("Step 1 — Generating bid fields with OpenAI…")
            job_enriched = {
                **job_scraped,
                **({k: v for k, v in {
                    "_live_budget":   live.get("budget", ""),
                    "_live_deadline": live.get("deadline", ""),
                }.items() if v}),
            }
            try:
                bid_fields = _pd.generate_bid_fields(
                    job_enriched,
                    openai_key,
                    model=model,
                    # Skip the HTTP fetch — we already have ld+json from the browser
                    fetch_full_description=False,
                    extra_prompt=extra_prompt,
                    bid_price_pct=bid_price_pct,
                )
            except Exception as exc:
                result = {"ok": False, "error": f"OpenAI error: {exc}"}
                _log(f"RESULT: FAILED — {result['error']}")
                time.sleep(linger_ms / 1000)
                browser.close()
                return result

            message       = bid_fields.get("message", "")
            price         = bid_fields.get("price", "")
            delivery_days = bid_fields.get("delivery_days", "")
            price_mode    = bid_fields.get("price_mode", "unknown")

            price_label = (
                f"{price}円 [scraped pay_min — fixed minimum]"
                if price_mode == "fixed_minimum"
                else f"{price or '(empty)'}円 [AI negotiated]"
            )
            _log(f"         Message       : {len(message)} chars")
            _log(f"         Price         : {price_label}")
            _log(f"         Delivery days : {delivery_days or '(empty)'}")

            if not message:
                result = {"ok": False, "error": "OpenAI returned an empty proposal message."}
                _log(f"RESULT: FAILED — {result['error']}")
                time.sleep(linger_ms / 1000)
                browser.close()
                return result

            # ── Step 2: Open the proposal form ────────────────────────────────
            _log(f"Step 2 — Navigating to proposal form: {proposal_url}")
            try:
                nav_resp = page.goto(
                    proposal_url,
                    wait_until="load",
                    timeout=timeout_ms,
                )
            except PWTimeout:
                result = {
                    "ok": False,
                    "error": "Proposal page load timed out — the site may be unreachable.",
                }
                _log(f"RESULT: FAILED — {result['error']}")
                time.sleep(linger_ms / 1000)
                browser.close()
                return result

            _log(f"         Title      : {page.title()!r}")
            _log(f"         Current URL: {page.url}")

            # Check HTTP status
            if nav_resp is not None:
                http_status = nav_resp.status
                _log(f"         HTTP status: {http_status}")

                if http_status == 403:
                    # A 403 on proposals/new most commonly means the account
                    # already bid on this job.  Check page content to confirm.
                    _html_403 = page.content()
                    if _is_already_bid(_html_403, page.url):
                        result = {
                            "ok": False,
                            "already_bid": True,
                            "error": f"Already bid on this project (HTTP 403).",
                        }
                        _log(f"RESULT: ALREADY BID — {result['error']}")
                    else:
                        result = {
                            "ok": False,
                            "error": (
                                f"HTTP 403 — job {job_id} is not accessible. "
                                "The job may have expired, been deleted, already been bid on, "
                                "or the session cookie is invalid."
                            ),
                        }
                        _log(f"RESULT: FAILED — {result['error']}")
                    time.sleep(linger_ms / 1000)
                    browser.close()
                    return result

                if http_status == 404:
                    result = {
                        "ok": False,
                        "error": f"HTTP 404 — job {job_id} does not exist.",
                    }
                    _log(f"RESULT: FAILED — {result['error']}")
                    time.sleep(linger_ms / 1000)
                    browser.close()
                    return result

                if http_status >= 400:
                    result = {
                        "ok": False,
                        "error": f"HTTP {http_status} — server error on proposal page.",
                    }
                    _log(f"RESULT: FAILED — {result['error']}")
                    time.sleep(linger_ms / 1000)
                    browser.close()
                    return result

            # Detect session expiry via redirect to login
            if "/login" in page.url:
                result = {
                    "ok": False,
                    "error": "Session expired — browser was redirected to the login page.",
                }
                _log(f"RESULT: FAILED — {result['error']}")
                time.sleep(linger_ms / 1000)
                browser.close()
                return result

            # Detect "already bid" immediately after navigation (URL redirect or
            # page text) so we don't waste an AI call or attempt form filling.
            _nav_html = page.content()
            if _is_already_bid(_nav_html, page.url):
                result = {
                    "ok": False,
                    "already_bid": True,
                    "error": "Already bid on this project (detected before form fill).",
                }
                _log(f"RESULT: ALREADY BID — {result['error']}")
                time.sleep(linger_ms / 1000)
                browser.close()
                return result

            # ── Step 3: Locate message textarea ───────────────────────────────
            _log("Step 3 — Locating proposal message field…")
            msg_el = None
            for sel in _MSG_SELECTORS:
                try:
                    page.wait_for_selector(sel, timeout=3_000)
                    candidate = page.query_selector(sel)
                    if candidate and candidate.is_visible():
                        msg_el = candidate
                        _log(f"         Found with: {sel}")
                        break
                except PWTimeout:
                    pass

            if msg_el is None:
                # Check the page content now — if CrowdWorks is showing an
                # "already applied" message the form simply doesn't render.
                _no_form_html = page.content()
                if _is_already_bid(_no_form_html, page.url):
                    result = {
                        "ok": False,
                        "already_bid": True,
                        "error": "Already bid on this project (proposal form not shown).",
                    }
                    _log(f"RESULT: ALREADY BID — {result['error']}")
                else:
                    result = {
                        "ok": False,
                        "error": (
                            "Proposal message field not found. "
                            "The job may be closed, already bid on, "
                            "or the page layout has changed."
                        ),
                    }
                    _log(f"RESULT: FAILED — {result['error']}")
                time.sleep(linger_ms / 1000)
                browser.close()
                return result

            # ── Step 4: Fill message ──────────────────────────────────────────
            _log("Step 4 — Filling proposal message…")
            msg_el.scroll_into_view_if_needed()
            msg_el.click()
            msg_el.fill(message)
            _log(f"         Filled {len(message)} chars.")

            # ── Step 5: Handle contract-price radio then fill amount ──────────
            #
            # CrowdWorks proposal form has a radio group:
            #   ● 契約金額を提示         → show the price input field
            #   ○ 相談してから金額を提案  → hide price field; discuss after award
            #
            # Payment models:
            #   Fixed (固定報酬制)  → select "show price", fill with pay_min
            #   Hourly (時給制)     → select "show price", fill hourly_price field
            #   Negotiable (相談)   → select "discuss first" radio; no price needed
            payment_type = str(job_scraped.get("payment_type") or "").lower()
            is_hourly     = "hourly" in payment_type
            is_negotiable = any(kw in payment_type for kw in _NEGOTIABLE_PAYMENT_TYPES)

            _log(
                f"Step 5 — Contract amount (税別): {price or '(none)'}円  "
                f"[type={payment_type or '?'}, mode={price_mode}, "
                f"negotiable={is_negotiable}]…"
            )

            # ── 5a: Select the appropriate radio button ────────────────────────
            if is_negotiable:
                # Click "相談してから金額を提案" to suppress the price requirement
                clicked_radio = False
                for sel in _RADIO_NEGOTIATE:
                    try:
                        page.wait_for_selector(sel, timeout=2_000)
                        rb = page.query_selector(sel)
                        if rb and rb.is_visible():
                            rb.click()
                            _log(f"         Clicked '相談してから金額を提案' radio ({sel}).")
                            clicked_radio = True
                            break
                    except (PWTimeout, Exception):
                        pass
                # Try label-based click as fallback
                if not clicked_radio:
                    try:
                        page.get_by_text("相談してから金額を提案", exact=False).first.click()
                        _log("         Clicked '相談してから金額を提案' via label text.")
                    except Exception:
                        pass
                _log("         Negotiable job — price field not required.")

            elif price:
                # Ensure "契約金額を提示" radio is selected (it usually is by default,
                # but we confirm to avoid the form defaulting to negotiation mode)
                for sel in _RADIO_SHOW_PRICE:
                    try:
                        rb = page.query_selector(sel)
                        if rb and rb.is_visible() and not rb.is_checked():
                            rb.click()
                            _log(f"         Selected '契約金額を提示' radio ({sel}).")
                        break
                    except Exception:
                        pass

                # ── 5b: Fill the price field ───────────────────────────────────
                selectors_to_try = (
                    _HOURLY_RATE_SELECTORS + _PRICE_SELECTORS
                    if is_hourly
                    else _PRICE_SELECTORS + _HOURLY_RATE_SELECTORS
                )
                filled_price = False

                # Give Vue time to render the price field after radio selection
                for wait_sel in selectors_to_try[:3]:
                    try:
                        page.wait_for_selector(wait_sel, timeout=2_000)
                        break
                    except PWTimeout:
                        pass

                # ① Try all CSS selectors with Vue-compatible fill
                for sel in selectors_to_try:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible() and el.is_enabled():
                            _fill_price_vue(page, el, str(price))
                            _log(f"         Price filled ({sel}).")
                            filled_price = True
                            break
                    except Exception:
                        pass

                # ② Label-based fallback
                if not filled_price:
                    try:
                        label_el = page.get_by_text("契約金額", exact=False).first
                        if label_el.is_visible():
                            parent = label_el.evaluate_handle(
                                "el => el.closest('div,li,tr,section') "
                                "      || el.parentElement"
                            )
                            inp = parent.query_selector(
                                "input[type='number'], input[type='text']"
                            )
                            if inp and inp.is_visible():
                                _fill_price_vue(page, inp, str(price))
                                _log("         Price filled via 契約金額 label.")
                                filled_price = True
                    except Exception as ex:
                        _log(f"         Label-based fill skipped: {ex}")

                # ③ JS scan: find any visible, enabled number input on the form
                if not filled_price:
                    try:
                        found = page.evaluate(
                            """(value) => {
                                const inputs = document.querySelectorAll(
                                    'form input[type="number"], form input[type="text"]'
                                );
                                for (const inp of inputs) {
                                    const r = inp.getBoundingClientRect();
                                    if (r.width === 0 || r.height === 0) continue;
                                    if (inp.disabled || inp.readOnly) continue;
                                    const setter = Object.getOwnPropertyDescriptor(
                                        window.HTMLInputElement.prototype, 'value'
                                    ).set;
                                    setter.call(inp, value);
                                    inp.dispatchEvent(new Event('input',  { bubbles: true }));
                                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                                    return true;
                                }
                                return false;
                            }""",
                            str(price),
                        )
                        if found:
                            _log("         Price filled via JS form scan.")
                            filled_price = True
                    except Exception as ex:
                        _log(f"         JS scan fill skipped: {ex}")

                if not filled_price:
                    _log(
                        "         WARNING: could not fill contract amount field. "
                        "The form layout may have changed — check the open browser."
                    )

            else:
                _log("         No price value from AI — skipping price field.")

            # ── Step 6: Fill delivery days (if field present) ─────────────────
            if delivery_days:
                _log(f"Step 6 — Filling delivery days ({delivery_days})…")
                filled_delivery = False
                for sel in _DELIVERY_SELECTORS:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.triple_click()
                        el.fill(delivery_days)
                        _log(f"         Delivery days filled ({sel}).")
                        filled_delivery = True
                        break
                if not filled_delivery:
                    _log("         Delivery-days field not found — skipping.")
            else:
                _log("Step 6 — No delivery days from AI, skipping.")

            # ── Step 7: Click submit ──────────────────────────────────────────
            _log("Step 7 — Locating and clicking submit button…")
            submitted = False
            for sel in _SUBMIT_SELECTORS:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible() and btn.is_enabled():
                        _log(f"         Clicking: {sel}")
                        btn.click()
                        submitted = True
                        break
                except Exception as exc:
                    _log(f"         Selector {sel!r} skipped: {exc}")

            if not submitted:
                result = {
                    "ok": False,
                    "error": "No enabled submit button found on the proposal form.",
                }
                _log(f"RESULT: FAILED — {result['error']}")
                time.sleep(linger_ms / 1000)
                browser.close()
                return result

            # ── Step 8: Wait for post-submit page ────────────────────────────
            _log("Step 8 — Waiting for submission response…")
            try:
                page.wait_for_load_state("load", timeout=timeout_ms)
            except PWTimeout:
                _log("         Load timeout after submit — checking URL anyway.")

            final_url = page.url
            _log(f"         Final URL  : {final_url}")
            _log(f"         Page title : {page.title()!r}")

            # ── Step 9: Detect outcome ────────────────────────────────────────
            _log("Step 9 — Checking submission outcome…")
            page_html = page.content()

            error_signals = [
                "エラー" in page_html and "proposals/new" in final_url,
                "error" in final_url,
            ]
            success_signals = [
                "proposals/" in final_url and "new" not in final_url,
                "応募完了" in page_html,
                "ありがとう" in page_html,
            ]

            if any(error_signals):
                err_text = ""
                for err_sel in ["[class*='error']", "[class*='alert']", "[role='alert']"]:
                    el = page.query_selector(err_sel)
                    if el:
                        t = el.inner_text().strip()
                        if t:
                            err_text = t
                            break
                # Check whether the server error is an "already bid" message.
                if _is_already_bid(page_html, final_url) or any(
                    sig in err_text for sig in _ALREADY_BID_JP
                ):
                    result = {
                        "ok": False,
                        "already_bid": True,
                        "error": err_text or "Already bid on this project (server rejected re-submission).",
                    }
                    _log(f"RESULT: ALREADY BID — {result['error']}")
                else:
                    result = {
                        "ok": False,
                        "error": err_text or f"Server returned an error. URL: {final_url}",
                    }
                    _log(f"RESULT: FAILED — {result['error']}")
            elif any(success_signals):
                result = {"ok": True, "url": final_url}
                _log("RESULT: SUCCESS — bid submitted.")
            else:
                result = {"ok": True, "url": final_url}
                _log(f"RESULT: SUBMITTED (outcome ambiguous) — URL: {final_url}")

            _log(f"         Keeping browser open for {linger_ms} ms…")
            time.sleep(linger_ms / 1000)

        except PWTimeout as exc:
            result = {"ok": False, "error": f"Playwright timeout: {exc}"}
            _log(f"RESULT: TIMEOUT — {exc}")
        except Exception as exc:
            err_str = str(exc)
            if "closed" in err_str.lower() and (
                "target" in err_str.lower() or "browser" in err_str.lower()
            ):
                result = {
                    "ok": False,
                    "error": "Browser window was closed before the operation completed.",
                }
            else:
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            _log(f"RESULT: ERROR — {result['error']}")
        finally:
            _log("Closing browser.")
            try:
                browser.close()
            except Exception:
                pass

    _log("=" * 60)
    _log(f"Browser bid finished — ok={result.get('ok')}")
    if not result.get("ok"):
        _log(f"Error detail: {result.get('error')}")
    _log("=" * 60)
    return result
