"""
CrowdWorks 応募メッセージ草案 — OpenAI API（ユーザーが用意したキー）。

公開の案件ページから JobPosting の詳細を取得してプロンプトに含められます。
実際の「応募する」送信はクラウドワークス上で手動で行ってください。
"""

from __future__ import annotations

import html as html_lib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from crowdworks_jobs import fetch_html, job_public_url

from _appdir import APP_DIR
SETTINGS_PATH = APP_DIR / "local_settings.json"
EXTRA_PROMPT_MAX_CHARS = 12000

# ── Legacy plain-text system prompt (kept for reference) ─────────────────────
SYSTEM_PROMPT_JA = """あなたはクラウドワークスで案件に応募するフリーランスです。
出力は「応募フォームのメッセージ欄」にそのまま貼れる日本語の本文のみにしてください。
次の構成を自然な文章で含めてください（見出しラベルは付けず、段落で書く）:
1) 提示する契約金額の考え方と見積の内訳（募集の報酬レンジ内であること）
2) 簡潔な自己紹介
3) 関連スキル・経験・過去の似た案件があれば具体的に
4) 案件内容への理解と質問・提案、着手可能時期
依頼文の法的・倫理的配慮（許可された範囲の取得、相手サイトの利用規約遵守など）に触れてください。
嘘の実績や過度な約束は書かないでください。"""

# ── Structured JSON system prompt ────────────────────────────────────────────
# Used when price is negotiable / hourly / unknown — AI determines the price.
SYSTEM_PROMPT_BID_JSON = """あなたはクラウドワークスで案件に応募するフリーランスです。
以下の案件情報をすべて読み込み、応募フォームに入力する値を **JSONのみ** で返してください。
余分な説明・コードブロック記号（```）は絶対に付けないこと。JSONオブジェクトのみを出力してください。

出力形式（キーを省略しないこと）:
{
  "message": "応募フォームのメッセージ欄に直接貼る日本語の本文（段落あり・自然な文章）",
  "price": "提案金額（数字のみ・円単位の整数）",
  "delivery_days": "納品までの日数（整数のみ）"
}

message に含めること（見出しラベルなし、段落で書くこと）:
1) 自己紹介（実績・スキル・類似案件の経験）
2) 案件への理解、具体的なアプローチや提案
3) 提示金額の根拠・内訳と作業スコープ
4) 着手可能日と納品スケジュール（案件の期限・valid_through を必ず考慮）
5) 法的・倫理的な配慮（規約遵守、許可範囲での作業）

price（金額が交渉制・不明・時給制の場合）:
  - 時給制の場合: 実績・スキルに見合う妥当な時給（整数・円）。pay_min〜pay_max を参考にすること
  - 固定報酬で金額交渉の場合: 作業スコープを踏まえた妥当な整数（円）
  - 金額が完全に不明な場合: 0

delivery_days:
  - valid_through（納品期限）から今日を引いた日数を整数で返す
  - 不明な場合は 7"""

# Used when the fixed minimum budget is known — AI writes only the message and
# delivery days; the pre-set price is injected directly into the prompt.
SYSTEM_PROMPT_BID_FIXED = """あなたはクラウドワークスで案件に応募するフリーランスです。
以下の案件情報をすべて読み込み、応募フォームに入力する値を **JSONのみ** で返してください。
余分な説明・コードブロック記号（```）は絶対に付けないこと。JSONオブジェクトのみを出力してください。

出力形式（キーを省略しないこと）:
{
  "message": "応募フォームのメッセージ欄に直接貼る日本語の本文（段落あり・自然な文章）",
  "price": "プロンプト内の「応募金額（確定）」に記載された数値をそのままコピーすること",
  "delivery_days": "納品までの日数（整数のみ）"
}

message に含めること（見出しラベルなし、段落で書くこと）:
1) 自己紹介（実績・スキル・類似案件の経験）
2) 案件への理解、具体的なアプローチや提案
3) 「応募金額（確定）」に示した金額でなぜその金額を提示するかの根拠・内訳
4) 着手可能日と納品スケジュール（案件の期限・valid_through を必ず考慮）
5) 法的・倫理的な配慮（規約遵守、許可範囲での作業）

price: プロンプト内の「応募金額（確定）」の数値を**そのまま**返すこと。変更・推測しないこと。

delivery_days:
  - valid_through（納品期限）から今日を引いた日数を整数で返す
  - 不明な場合は 7"""


def load_local_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.is_file():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_local_settings(data: dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_saved_openai_key() -> str:
    return str(load_local_settings().get("openai_api_key") or "")


def save_openai_key_to_disk(api_key: str) -> None:
    data = load_local_settings()
    data["openai_api_key"] = api_key
    save_local_settings(data)


def load_saved_extra_prompt() -> str:
    return str(load_local_settings().get("proposal_extra_prompt") or "")


def save_extra_prompt_to_disk(text: str) -> None:
    data = load_local_settings()
    data["proposal_extra_prompt"] = text
    save_local_settings(data)


def load_saved_cw_credentials() -> tuple[str, str]:
    """Return (email, password) stored in local_settings.json."""
    s = load_local_settings()
    return str(s.get("crowdworks_email") or ""), str(s.get("crowdworks_password") or "")


def save_cw_credentials_to_disk(email: str, password: str) -> None:
    data = load_local_settings()
    data["crowdworks_email"] = email
    data["crowdworks_password"] = password
    save_local_settings(data)


def load_saved_session_id() -> str:
    """Return the stored _cw_session_id cookie value, or empty string."""
    return str(load_local_settings().get("cw_session_id") or "")


def save_session_id_to_disk(session_id: str) -> None:
    """Persist a _cw_session_id cookie value in local_settings.json."""
    data = load_local_settings()
    data["cw_session_id"] = session_id
    save_local_settings(data)


def _ld_find_jobposting(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        if obj.get("@type") == "JobPosting":
            return obj
        for v in obj.values():
            r = _ld_find_jobposting(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _ld_find_jobposting(item)
            if r is not None:
                return r
    return None


def _html_description_to_plain(html_desc: str) -> str:
    t = html_lib.unescape(html_desc)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</p>\s*", "\n\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def fetch_job_ld_full(job_offer_id: int, *, timeout: float = 60.0) -> dict[str, Any]:
    """
    Fetch the public job page and extract all available fields from the
    ``application/ld+json`` JobPosting block.

    Returns a dict with any subset of:
        description   – plain-text job description
        valid_through – ISO-8601 deadline string (e.g. "2025-06-01T00:00:00+09:00")
        salary_min    – minimum budget/hourly-rate (number or None)
        salary_max    – maximum budget/hourly-rate (number or None)
        salary_unit   – "HOUR", "MONTH", "" etc.
        currency      – usually "JPY"
        title         – job title from ld+json
    """
    html = fetch_html(job_public_url(job_offer_id), timeout=timeout)
    for m in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        jp = _ld_find_jobposting(data)
        if not jp:
            continue

        result: dict[str, Any] = {}

        # Description
        desc = jp.get("description")
        if isinstance(desc, str) and desc.strip():
            result["description"] = _html_description_to_plain(desc)

        # Deadline
        vt = jp.get("validThrough")
        if vt:
            result["valid_through"] = str(vt)

        # Title
        title = jp.get("title")
        if title:
            result["title"] = str(title)

        # Salary / budget
        salary = jp.get("baseSalary") or jp.get("estimatedSalary")
        if isinstance(salary, dict):
            result["currency"] = str(salary.get("currency") or "JPY")
            value = salary.get("value") or {}
            if isinstance(value, dict):
                result["salary_min"] = value.get("minValue")
                result["salary_max"] = value.get("maxValue")
                result["salary_unit"] = str(value.get("unitText") or "")
            elif isinstance(value, (int, float)):
                result["salary_min"] = value
                result["salary_max"] = value

        return result

    return {}


def fetch_jobposting_description(job_offer_id: int, *, timeout: float = 60.0) -> str | None:
    """Legacy helper — returns description text only. Use fetch_job_ld_full() for richer data."""
    return fetch_job_ld_full(job_offer_id, timeout=timeout).get("description")


def format_job_for_prompt(
    job: dict[str, Any],
    *,
    ld_details: dict[str, Any] | None = None,
    full_description: str | None = None,
    extra_instructions: str | None = None,
) -> str:
    """
    Build the user-turn content that is sent to OpenAI.

    Parameters
    ----------
    job             : Flat row from the scraper (pay_min, pay_max, expired_on …).
    ld_details      : Rich dict from ``fetch_job_ld_full`` (valid_through, salary …).
    full_description: Plain-text description (deprecated; ignored if ld_details has one).
    extra_instructions: User-supplied bid prompt / persona instructions.
    """
    ld = ld_details or {}

    # Prefer ld+json salary over scraper data when available
    salary_min = ld.get("salary_min") or job.get("pay_min")
    salary_max = ld.get("salary_max") or job.get("pay_max")
    salary_unit = ld.get("salary_unit") or ""
    # Prefer ld+json deadline; fall back to scraper's expired_on
    deadline = ld.get("valid_through") or job.get("expired_on") or "(不明)"
    description = ld.get("description") or full_description or ""

    lines = [
        "## 案件データ（スクレイプ＋公開ページ取得）",
        f"- 案件ID       : {job.get('job_offer_id')}",
        f"- タイトル     : {ld.get('title') or job.get('title')}",
        f"- クライアント : {job.get('client_username')}",
        f"- 報酬タイプ   : {job.get('payment_type')} {salary_unit}".strip(),
        f"- 予算レンジ   : min={salary_min}  max={salary_max}  (円)",
        f"- 納品期限     : {deadline}",
        f"- 掲載・更新   : {job.get('last_released_at')}",
        f"- 概要（一覧） :\n{job.get('description_digest') or '（なし）'}",
    ]

    # Live details extracted from browser page (if available)
    live_budget   = job.get("_live_budget", "")
    live_deadline = job.get("_live_deadline", "")
    if live_budget or live_deadline:
        lines.append("")
        lines.append("## ページ上の表示値（ブラウザ取得）")
        if live_budget:
            lines.append(f"- 予算（表示）  : {live_budget}")
        if live_deadline:
            lines.append(f"- 期日（表示）  : {live_deadline}")

    if description:
        lines.extend(
            [
                "",
                "## 仕事の詳細（公開ページ JobPosting より。HTML除去済み）",
                description[:12000],
            ]
        )

    lines.extend(
        [
            "",
            "## 応募画面の参考",
            "固定報酬の場合は pay_min〜pay_max 内で price を設定し、メッセージで内訳を説明してください。",
            f"応募URL: https://crowdworks.jp/proposals/new?job_offer_id={job.get('job_offer_id')}",
        ]
    )

    # Pre-set price (fixed minimum budget — must be reflected in the message)
    preset_price = str(job.get("_preset_price") or "").strip()
    if preset_price:
        lines.extend(
            [
                "",
                "## 応募金額（確定・変更不可）",
                f"{preset_price}円（予算レンジの設定値より自動計算）",
                "この金額をそのまま price フィールドに返し、メッセージ内でも同額に言及すること。",
            ]
        )

    extra = (extra_instructions or "").strip()
    if extra:
        lines.extend(
            [
                "",
                "## ユーザーからの追加指示（応募メッセージ・金額・納期に反映すること）",
                extra[:EXTRA_PROMPT_MAX_CHARS],
            ]
        )

    return "\n".join(lines)


def _determine_fixed_price(job: dict[str, Any], bid_price_pct: int = 0) -> str:
    """
    Return the bid price for fixed-price jobs as a digit-only string.

    ``bid_price_pct`` (0–100) controls where in the pay_min…pay_max range
    the price is placed:
        0   → pay_min  (minimum budget — the old default)
        100 → pay_max  (maximum budget)
        50  → midpoint between min and max

    Returns an empty string for hourly, negotiable, or jobs with no known
    budget — meaning the AI will determine the price instead.
    """
    payment_type = str(job.get("payment_type") or "").lower()

    # Hourly jobs: time-based rate is always AI-determined
    if "hourly" in payment_type:
        return ""

    # Resolve pay_min from scraper data or ld+json enrichment
    val_min: int | None = None
    for key in ("pay_min", "_ld_salary_min"):
        raw = job.get(key)
        if raw is None:
            continue
        try:
            v = int(float(str(raw)))
            if v > 0:
                val_min = v
                break
        except (ValueError, TypeError):
            pass

    if val_min is None:
        return ""  # negotiable / unknown — let AI decide

    # If percentage is 0 or pay_max is unavailable, return pay_min directly
    if bid_price_pct <= 0:
        return str(val_min)

    # Resolve pay_max
    val_max: int | None = None
    for key in ("pay_max", "_ld_salary_max"):
        raw = job.get(key)
        if raw is None:
            continue
        try:
            v = int(float(str(raw)))
            if v > val_min:
                val_max = v
                break
        except (ValueError, TypeError):
            pass

    if val_max is None or val_max <= val_min:
        return str(val_min)  # no valid range — fall back to min

    pct = max(0, min(100, bid_price_pct))
    price = int(val_min + (val_max - val_min) * pct / 100)
    return str(price)


def openai_chat_completion(
    api_key: str,
    messages: list[dict[str, str]],
    *,
    model: str = "gpt-4o-mini",
    timeout: float = 120.0,
) -> str:
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0.6,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"OpenAI HTTP {e.code}: {err_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI network error: {e}") from e

    try:
        return str(payload["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected OpenAI response: {payload!r}") from e


def generate_bid_fields(
    job: dict[str, Any],
    api_key: str,
    *,
    model: str = "gpt-4o-mini",
    fetch_full_description: bool = True,
    timeout_fetch: float = 60.0,
    extra_prompt: str = "",
    bid_price_pct: int = 0,
) -> dict[str, str]:
    """
    Fetch the project details page, analyze deadline / budget with OpenAI,
    and return all bid-form fields.

    Parameters
    ----------
    job        : Flat scraper row.  May include ``_live_budget`` and
                 ``_live_deadline`` keys added by the browser step.
    api_key    : OpenAI API key.
    model      : OpenAI model slug.
    fetch_full_description : When True (default) fetches ld+json from the
                 public job page to enrich the prompt with full description,
                 live deadline, and salary data.
    extra_prompt  : User-supplied persona / instructions.
    bid_price_pct : 0–100.  0 = pay_min, 100 = pay_max.  Controls where in
                    the budget range the fixed bid price is placed.

    Returns
    -------
    dict with keys:
        "message"       – proposal body text (Japanese)
        "price"         – bid price, digits only, e.g. "50000"
        "delivery_days" – estimated days until delivery, digits only, e.g. "7"
    """
    if not api_key.strip():
        raise ValueError("OpenAI API key is empty.")

    jid = job.get("job_offer_id")
    ld: dict[str, Any] = {}
    if fetch_full_description and jid is not None:
        ld = fetch_job_ld_full(int(jid), timeout=timeout_fetch)

    # Merge ld+json salary into job so _determine_fixed_price can use it
    job_enriched: dict[str, Any] = {**job}
    if ld.get("salary_min") is not None and not job_enriched.get("pay_min"):
        job_enriched["_ld_salary_min"] = ld["salary_min"]
    if ld.get("salary_max") is not None and not job_enriched.get("pay_max"):
        job_enriched["pay_max"] = ld["salary_max"]

    # ── Price path decision ───────────────────────────────────────────────────
    # Fixed-price jobs: price is calculated from pay_min…pay_max using the
    # configured percentage (0 % = min, 100 % = max).
    # Hourly / negotiable / unknown → let AI determine the price.
    fixed_price = _determine_fixed_price(job_enriched, bid_price_pct=bid_price_pct)
    price_mode  = "fixed_pct" if fixed_price else "ai_negotiated"

    # Embed the pre-set price in the job dict so format_job_for_prompt can
    # surface it as a clear constraint in the prompt.
    if fixed_price:
        job_enriched["_preset_price"] = fixed_price

    user_content = format_job_for_prompt(
        job_enriched,
        ld_details=ld,
        extra_instructions=extra_prompt,
    )

    system_prompt = (
        SYSTEM_PROMPT_BID_FIXED if fixed_price else SYSTEM_PROMPT_BID_JSON
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content},
    ]
    raw = openai_chat_completion(api_key.strip(), messages, model=model)

    # ── Parse the JSON response ───────────────────────────────────────────────
    text = raw.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    data: dict[str, Any] = {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        obj_m = re.search(r"\{[\s\S]*\}", text)
        if obj_m:
            try:
                data = json.loads(obj_m.group(0))
            except json.JSONDecodeError:
                pass

    msg  = str(data.get("message") or "").strip()
    days = re.sub(r"[^\d]", "", str(data.get("delivery_days") or ""))

    # ── Price resolution ──────────────────────────────────────────────────────
    if fixed_price:
        # Always use the scraped minimum — AI cannot override this
        price = fixed_price
    else:
        # AI-negotiated: use whatever the model returned
        price = re.sub(r"[^\d]", "", str(data.get("price") or ""))

    # Last-resort fallback: if JSON parsing completely failed, use the raw text
    if not msg:
        msg = raw.strip()

    return {
        "message":       msg,
        "price":         price,
        "delivery_days": days,
        "price_mode":    price_mode,   # informational, logged by browser_bid
    }


def generate_proposal_draft(
    job: dict[str, Any],
    api_key: str,
    *,
    model: str = "gpt-4o-mini",
    fetch_full_description: bool = True,
    timeout_fetch: float = 60.0,
    extra_prompt: str = "",
) -> str:
    """Backward-compatible wrapper — returns the proposal message string only."""
    return generate_bid_fields(
        job, api_key,
        model=model,
        fetch_full_description=fetch_full_description,
        timeout_fetch=timeout_fetch,
        extra_prompt=extra_prompt,
    )["message"]
