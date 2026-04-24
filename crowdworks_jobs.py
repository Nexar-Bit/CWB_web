"""
CrowdWorks public job list scraper.

Reads embedded JSON from the #vue-container data attribute on HTML pages
such as https://crowdworks.jp/public/jobs/search (and filtered variants).

Use --preset new-postings to poll the four newest-first watch lists (page 1
only per list) for newly posted jobs.

Use --preset new-postings --loop --output out.jsonl --interval-seconds 300 to
scrape continuously (Ctrl+C to stop).

Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import html as html_lib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable


VUE_DATA_RE = re.compile(
    r'<div[^>]*\bid="vue-container"[^>]*\bdata="([^"]*)"',
    re.IGNORECASE,
)

DEFAULT_URL = "https://crowdworks.jp/public/jobs/search"

# First page only: newest-first lists for monitoring newly posted jobs.
# menu_label / menu_order: short names and order for the web UI category dropdown.
NEW_POSTING_FEEDS: list[dict[str, Any]] = [
    {
        "slug": "ec_online_store",
        "label": "EC Site & Online Store Development",
        "menu_label": "EC",
        "menu_order": 3,
        "url": "https://crowdworks.jp/public/jobs/search?category_id=235&order=new",
    },
    {
        "slug": "system_development",
        "label": "System Development",
        "menu_label": "System",
        "menu_order": 1,
        "url": "https://crowdworks.jp/public/jobs/search?category_id=226&order=new",
    },
    {
        "slug": "website_web_design",
        "label": "Website Creation & Web Design",
        "menu_label": "Website",
        "menu_order": 4,
        "url": "https://crowdworks.jp/public/jobs/search?category_id=230&order=new",
    },
    {
        "slug": "ai_machine_learning",
        "label": "AI (Artificial Intelligence) & Machine Learning",
        "menu_label": "AI",
        "menu_order": 2,
        "url": "https://crowdworks.jp/public/jobs/group/ai_machine_learning",
    },
]


def category_feeds_ordered() -> list[dict[str, Any]]:
    """Feeds sorted for category menus (System, AI, EC, Website)."""
    return sorted(NEW_POSTING_FEEDS, key=lambda f: int(f["menu_order"]))


def feed_menu_labels_by_slug() -> dict[str, str]:
    return {str(f["slug"]): str(f["menu_label"]) for f in NEW_POSTING_FEEDS}


def job_public_url(job_offer_id: object) -> str:
    return f"https://crowdworks.jp/public/jobs/{int(job_offer_id)}"


def load_jobs_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def build_url_with_page(base_url: str, page: int) -> str:
    parts = urllib.parse.urlsplit(base_url)
    qs = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    qs["page"] = [str(page)]
    new_query = urllib.parse.urlencode(qs, doseq=True)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def fetch_html(url: str, timeout: float) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ja,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_vue_payload(html: str) -> dict[str, Any]:
    m = VUE_DATA_RE.search(html)
    if not m:
        raise ValueError(
            'Could not find <div id="vue-container" data="..."> payload in HTML.'
        )
    raw = html_lib.unescape(m.group(1))
    return json.loads(raw)


def _payment_flat(payment: dict[str, Any] | None) -> dict[str, Any]:
    if not payment:
        return {"payment_type": None, "pay_min": None, "pay_max": None}
    if "hourly_payment" in payment:
        h = payment["hourly_payment"] or {}
        return {
            "payment_type": "hourly",
            "pay_min": h.get("min_hourly_wage"),
            "pay_max": h.get("max_hourly_wage"),
        }
    if "fixed_price_payment" in payment:
        f = payment["fixed_price_payment"] or {}
        return {
            "payment_type": "fixed",
            "pay_min": f.get("min_budget"),
            "pay_max": f.get("max_budget"),
        }
    return {"payment_type": "unknown", "pay_min": None, "pay_max": None}


def _entry_flat(entry: dict[str, Any] | None) -> dict[str, Any]:
    if not entry:
        return {
            "num_contracts": None,
            "project_contract_hope_number": None,
            "num_application_conditions": None,
        }
    pe = entry.get("project_entry") or entry
    return {
        "num_contracts": pe.get("num_contracts"),
        "project_contract_hope_number": pe.get("project_contract_hope_number"),
        "num_application_conditions": pe.get("num_application_conditions"),
    }


def flatten_offer(row: dict[str, Any]) -> dict[str, Any]:
    jo = row.get("job_offer") or {}
    cl = row.get("client") or {}
    pay = _payment_flat(row.get("payment"))
    ent = _entry_flat(row.get("entry"))
    out: dict[str, Any] = {
        "job_offer_id": jo.get("id"),
        "title": jo.get("title"),
        "description_digest": jo.get("description_digest"),
        "category_id": jo.get("category_id"),
        "genre": jo.get("genre"),
        "skills": jo.get("skills"),
        "options": jo.get("options"),
        "status": jo.get("status"),
        "expired_on": jo.get("expired_on"),
        "last_released_at": jo.get("last_released_at"),
        "is_login_required": jo.get("is_login_required"),
        "client_user_id": cl.get("user_id"),
        "client_username": cl.get("username"),
        "client_user_picture_url": cl.get("user_picture_url"),
        "is_employer_certification": cl.get("is_employer_certification"),
    }
    out.update(pay)
    out.update(ent)
    return out


def iter_job_pages(
    base_url: str,
    *,
    start_page: int,
    max_pages: int | None,
    delay_s: float,
    timeout: float,
) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    page_index = start_page
    pages_done = 0

    while True:
        url = build_url_with_page(base_url, page_index)
        html = fetch_html(url, timeout=timeout)
        payload = parse_vue_payload(html)
        sr = payload.get("searchResult") or {}
        meta = sr.get("page") or {}
        offers = sr.get("job_offers") or []

        yield payload, {"fetched_url": url, "job_offers": offers, "page_meta": meta}

        current = int(meta.get("current_page") or page_index)
        total = int(meta.get("total_page") or current)

        pages_done += 1
        if max_pages is not None and pages_done >= max_pages:
            break
        if current >= total:
            break

        if delay_s > 0:
            time.sleep(delay_s)
        page_index = current + 1


def write_json(path: str, documents: list[dict[str, Any]], *, pretty: bool) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        if pretty:
            json.dump(documents, f, ensure_ascii=False, indent=2)
        else:
            json.dump(documents, f, ensure_ascii=False, separators=(",", ":"))


def write_jsonl(path: str, documents: Iterable[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for doc in documents:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")


def append_jsonl_unique(
    path: str | Path,
    new_docs: list[dict[str, Any]],
    key: str = "job_offer_id",
) -> list[dict[str, Any]]:
    """Append *new_docs* to a JSONL file, skipping any whose *key* already exists.

    Returns the subset of *new_docs* that were actually written (truly new).
    """
    p = Path(path)
    existing_keys: set[str] = set()
    if p.is_file():
        for row in load_jobs_jsonl(p):
            v = row.get(key)
            if v is not None:
                existing_keys.add(str(v))

    truly_new = [
        d for d in new_docs
        if str(d.get(key)) not in existing_keys and d.get(key) is not None
    ]
    if truly_new:
        with p.open("a", encoding="utf-8", newline="\n") as f:
            for doc in truly_new:
                f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return truly_new


def scrape_first_page(
    listing_url: str,
    *,
    timeout: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Return (job_offers rows, page_meta, fetched_url) for page 1."""
    for _payload, ctx in iter_job_pages(
        listing_url,
        start_page=1,
        max_pages=1,
        delay_s=0.0,
        timeout=timeout,
    ):
        return ctx["job_offers"], ctx["page_meta"], ctx["fetched_url"]
    return [], {}, listing_url


def _scrape_one_feed(
    feed: dict[str, Any],
    *,
    max_pages: int,
    delay_s: float,
    timeout: float,
    include_raw: bool,
    stop_at_ids: set[str] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Fetch up to *max_pages* pages for a single feed entry.

    Called by :func:`scrape_new_postings_feeds` in its thread pool so that
    all feeds are fetched concurrently.  Returns ``(flat_rows, summary)``.

    Thread-safe: uses only local variables and read-only shared arguments.
    ``urllib.request.urlopen`` creates independent connections per call.
    """
    feed_offers: list[dict[str, Any]] = []
    last_meta: dict[str, Any] = {}
    last_url = feed["url"]
    hit_known = False

    for _payload, ctx in iter_job_pages(
        feed["url"],
        start_page=1,
        max_pages=max_pages,
        delay_s=delay_s,       # inter-page delay kept to avoid hammering one endpoint
        timeout=timeout,
    ):
        last_meta = ctx["page_meta"]
        last_url  = ctx["fetched_url"]
        for row in ctx["job_offers"]:
            flat = flatten_offer(row)
            flat["feed_slug"]       = feed["slug"]
            flat["feed_label"]      = feed["label"]
            flat["feed_menu_label"] = feed["menu_label"]
            flat["feed_url"]        = feed["url"]
            flat["fetched_url"]     = last_url
            if include_raw:
                flat["_raw"] = row
            feed_offers.append(flat)
            if stop_at_ids and str(flat.get("job_offer_id")) in stop_at_ids:
                hit_known = True
        if hit_known:
            break

    summary: dict[str, Any] = {
        "feed_slug":   feed["slug"],
        "feed_label":  feed["label"],
        "fetched_url": last_url,
        "offers":      len(feed_offers),
        "page_meta":   last_meta,
    }
    return feed_offers, summary


def scrape_new_postings_feeds(
    *,
    delay_s: float = 1.0,
    timeout: float = 60.0,
    include_raw: bool = False,
    max_pages: int = 3,
    stop_at_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Fetch up to *max_pages* pages for every NEW_POSTING_FEEDS entry **in
    parallel** — one thread per feed — then merge results in original feed
    order.

    If *stop_at_ids* is provided, each feed thread stops scanning as soon as
    it encounters a job whose ``job_offer_id`` is in the set, so routine
    scrape cycles that are already caught-up finish quickly.

    Returns ``(flat_job_rows, feed_summaries)``.
    """
    n_feeds = len(NEW_POSTING_FEEDS)

    # Results keyed by feed slug so we can restore original order after
    # concurrent completion.
    results: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    errors:  dict[str, Exception] = {}

    with ThreadPoolExecutor(max_workers=n_feeds, thread_name_prefix="feed") as pool:
        future_to_slug = {
            pool.submit(
                _scrape_one_feed,
                feed,
                max_pages=max_pages,
                delay_s=delay_s,
                timeout=timeout,
                include_raw=include_raw,
                stop_at_ids=stop_at_ids,
            ): feed["slug"]
            for feed in NEW_POSTING_FEEDS
        }

        for future in as_completed(future_to_slug):
            slug = future_to_slug[future]
            try:
                results[slug] = future.result()
            except Exception as exc:          # network / parse error for one feed
                errors[slug] = exc

    if errors:
        # Surface the first error (others were collected, not lost)
        first_slug, first_exc = next(iter(errors.items()))
        raise RuntimeError(
            f"Feed '{first_slug}' failed: {first_exc}"
            + (f" (and {len(errors)-1} more)" if len(errors) > 1 else "")
        ) from first_exc

    # Merge in original feed declaration order
    collected: list[dict[str, Any]] = []
    feed_summaries: list[dict[str, Any]] = []
    for feed in NEW_POSTING_FEEDS:
        offers, summary = results[feed["slug"]]
        collected.extend(offers)
        feed_summaries.append(summary)

    return collected, feed_summaries


def write_csv(path: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("")
        return
    columns = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {}
            for k, v in r.items():
                if isinstance(v, (list, dict)) and v is not None:
                    flat[k] = json.dumps(v, ensure_ascii=False)
                else:
                    flat[k] = v
            w.writerow(flat)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--preset",
        choices=("none", "new-postings"),
        default="none",
        help=(
            "new-postings: fetch page 1 only for the four newest-sorted watch lists "
            "(EC / system / web / AI). Ignores --url/--start-page/--max-pages."
        ),
    )
    p.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Job list URL (query string preserved; page= is managed). Default: {DEFAULT_URL}",
    )
    p.add_argument("--start-page", type=int, default=1, help="First page index (1-based).")
    p.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Stop after this many successfully fetched pages (default: 1).",
    )
    p.add_argument(
        "--max-pages-total",
        type=int,
        default=None,
        help="If set, stop when reaching this total_page from the site (safety cap).",
    )
    p.add_argument("--delay", type=float, default=1.0, help="Seconds between page fetches.")
    p.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout seconds.")
    p.add_argument(
        "--format",
        choices=("json", "jsonl", "csv"),
        default="jsonl",
        help="Output serialization.",
    )
    p.add_argument("--output", "-o", required=True, help="Output file path.")
    p.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON (only for --format json).",
    )
    p.add_argument(
        "--include-raw",
        action="store_true",
        help="Include the nested raw row under key '_raw' for each offer.",
    )
    p.add_argument(
        "--loop",
        action="store_true",
        help="With --preset new-postings: repeat scraping until Ctrl+C (writes --output each cycle).",
    )
    p.add_argument(
        "--interval-seconds",
        type=float,
        default=300.0,
        help="With --loop: wait this many seconds after each scrape before the next (default: 300).",
    )
    args = p.parse_args(argv)

    if args.loop and args.preset != "new-postings":
        p.error("--loop requires --preset new-postings")

    interval = max(15.0, float(args.interval_seconds))

    def run_one_scrape() -> tuple[int, list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]]:
        """Returns (exit_code, collected, last_meta, feed_summaries)."""
        collected: list[dict[str, Any]] = []
        last_meta: dict[str, Any] | None = None
        feed_summaries: list[dict[str, Any]] = []

        try:
            if args.preset == "new-postings":
                collected, feed_summaries = scrape_new_postings_feeds(
                    delay_s=args.delay,
                    timeout=args.timeout,
                    include_raw=args.include_raw,
                )
            else:
                for _payload, ctx in iter_job_pages(
                    args.url,
                    start_page=args.start_page,
                    max_pages=args.max_pages,
                    delay_s=args.delay,
                    timeout=args.timeout,
                ):
                    last_meta = ctx["page_meta"]
                    if args.max_pages_total is not None:
                        total_site_pages = int(last_meta.get("total_page") or 0)
                        if total_site_pages > args.max_pages_total:
                            print(
                                f"Aborting: site reports total_page={total_site_pages} "
                                f"which exceeds --max-pages-total={args.max_pages_total}",
                                file=sys.stderr,
                            )
                            return 2, collected, last_meta, feed_summaries

                    for row in ctx["job_offers"]:
                        flat = flatten_offer(row)
                        if args.include_raw:
                            flat["_raw"] = row
                        collected.append(flat)
        except urllib.error.HTTPError as e:
            print(f"HTTP error: {e}", file=sys.stderr)
            return 1, collected, last_meta, feed_summaries
        except urllib.error.URLError as e:
            print(f"Network error: {e}", file=sys.stderr)
            return 1, collected, last_meta, feed_summaries
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1, collected, last_meta, feed_summaries

        if args.format == "jsonl":
            write_jsonl(args.output, collected)
        elif args.format == "json":
            write_json(args.output, collected, pretty=args.pretty)
        else:
            write_csv(args.output, collected)

        if args.preset == "new-postings":
            print(
                json.dumps(
                    {
                        "written": len(collected),
                        "feeds": feed_summaries,
                    },
                    ensure_ascii=False,
                )
            )
        elif last_meta is not None:
            print(
                json.dumps(
                    {
                        "written": len(collected),
                        "last_page_meta": last_meta,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(json.dumps({"written": 0}, ensure_ascii=False))
        return 0, collected, last_meta, feed_summaries

    if args.loop:
        print(
            json.dumps(
                {
                    "loop": True,
                    "interval_seconds": interval,
                    "output": args.output,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        while True:
            code, _collected, _lm, _fs = run_one_scrape()
            if code == 2:
                return code
            if code != 0:
                print(
                    json.dumps(
                        {"warning": "scrape_failed_will_retry", "exit_code": code},
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print(json.dumps({"stopped": True, "reason": "KeyboardInterrupt"}), file=sys.stderr)
                return 0

    code, _collected, _lm, _fs = run_one_scrape()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
