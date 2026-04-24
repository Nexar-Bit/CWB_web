"""
CrowdWorks Bot — multi-account desktop application.

Architecture
------------
* Configuration (OpenAI key, prompts) stored in SQLite (crowdworks_bot.db).
* Multiple CrowdWorks accounts, each with its own session cookie and prompt.
* Bot loop scrapes listings periodically; for each new job it generates a
  proposal via OpenAI and auto-submits a bid for every enabled account.

Navigation (sidebar)
--------------------
  Dashboard  — stats overview + recent activity
  Accounts   — add / edit / verify / delete CrowdWorks accounts
  Jobs       — browse scraped listings, manual bid marking
  Settings   — OpenAI key, scrape intervals, prompt library
  Log        — full bot activity log
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, W, X, Y, messagebox
from tkinter.scrolledtext import ScrolledText
import tkinter as tk
from tkinter import ttk

try:
    import pystray
    from PIL import Image, ImageDraw
    _TRAY_AVAILABLE = True
except ImportError:
    _TRAY_AVAILABLE = False

import browser_bid
import cword_auth
import db
from db import DESKTOP_USER_ID as _U
from crowdworks_jobs import (
    NEW_POSTING_FEEDS,
    append_jsonl_unique,
    category_feeds_ordered,
    feed_menu_labels_by_slug,
    job_public_url,
    load_jobs_jsonl,
    scrape_new_postings_feeds,
    write_jsonl,
)

from _appdir import APP_DIR
BASE_DIR = APP_DIR
DATA_FILE = BASE_DIR / "new_postings.jsonl"

_POPEN_KW: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)

# ── Colour palette ─────────────────────────────────────────────────────────────
_C = {
    # Sidebar / navigation
    "nav_bg":    "#0f172a",   # slate-900
    "nav_fg":    "#94a3b8",   # slate-400
    "nav_selbg": "#1e293b",   # slate-800  (active row background)
    "nav_sel":   "#3b82f6",   # blue-500   (left accent bar)
    "nav_selfg": "#f1f5f9",   # slate-100  (active text)
    "nav_sep":   "#1e293b",   # separator
    # Header
    "hdr_bg":    "#0f172a",
    "hdr_fg":    "#f1f5f9",
    # Page content
    "page_bg":   "#f1f5f9",   # slate-100
    "card_bg":   "#ffffff",
    "border":    "#e2e8f0",   # slate-200
    "border2":   "#cbd5e1",   # slate-300
    # Status colours
    "green":     "#10b981",   # emerald-500
    "green_dk":  "#059669",   # emerald-600
    "red":       "#ef4444",   # red-500
    "red_dk":    "#dc2626",   # red-600
    "yellow":    "#f59e0b",   # amber-500
    "blue":      "#3b82f6",   # blue-500
    "blue_dk":   "#2563eb",   # blue-600
    "gray":      "#64748b",   # slate-500
    # Text
    "text":      "#0f172a",   # slate-900
    "text2":     "#334155",   # slate-700
    "text3":     "#64748b",   # slate-500
}

_NAV_ITEMS = ["Dashboard", "Accounts", "Jobs", "Settings", "Log"]

_NAV_ICONS = {
    "Dashboard": "◈",
    "Accounts":  "⊕",
    "Jobs":      "▤",
    "Settings":  "⚙",
    "Log":       "☰",
}

_FT_BODY  = ("Segoe UI", 10)
_FT_SMALL = ("Segoe UI", 9)
_FT_BOLD  = ("Segoe UI", 10, "bold")
_FT_H1    = ("Segoe UI", 16, "bold")
_FT_H2    = ("Segoe UI", 12, "bold")
_FT_NAV   = ("Segoe UI", 10)


# ── Small UI helpers ───────────────────────────────────────────────────────────

def _lbl(parent, text: str = "", *, font=_FT_BODY,
         fg: str | None = None, bg: str | None = None, **kw) -> tk.Label:
    return tk.Label(
        parent, text=text, font=font,
        fg=fg or _C["text2"], bg=bg or _C["page_bg"], **kw,
    )


def _card(parent) -> tk.Frame:
    """White card frame with a subtle border."""
    return tk.Frame(
        parent, bg=_C["card_bg"],
        highlightbackground=_C["border"], highlightthickness=1,
    )


def _section_title(parent, text: str) -> tk.Label:
    """Bold section title with left-colored dot."""
    return tk.Label(
        parent, text=text,
        font=_FT_H1, fg=_C["text"], bg=_C["page_bg"],
    )


class TreeviewTooltip:
    """Hover tooltip for a ttk.Treeview.

    *get_tip(iid)* is called with the row's iid each time the pointer moves.
    Return a non-empty string to show a tip, or ``None``/``""`` to hide it.
    """

    _PAD = 8   # inner padding (px)

    def __init__(self, tree: ttk.Treeview, get_tip) -> None:
        self._tree    = tree
        self._get_tip = get_tip
        self._tip_win: tk.Toplevel | None = None
        self._last_iid: str = ""

        tree.bind("<Motion>",  self._on_motion,  add=True)
        tree.bind("<Leave>",   self._hide,        add=True)
        tree.bind("<Button>",  self._hide,        add=True)
        tree.bind("<Destroy>", self._hide,        add=True)

    # ── event handlers ────────────────────────────────────────────────

    def _on_motion(self, event: tk.Event) -> None:
        iid = self._tree.identify_row(event.y)
        if iid == self._last_iid:
            return
        self._last_iid = iid
        self._hide()
        if not iid:
            return
        text = self._get_tip(iid)
        if text:
            self._show(event.x_root, event.y_root, text)

    def _show(self, rx: int, ry: int, text: str) -> None:
        self._hide()
        tw = tk.Toplevel(self._tree)
        tw.wm_overrideredirect(True)
        tw.wm_attributes("-topmost", True)
        tw.configure(bg=_C["text"])

        # Outer dark border frame
        border = tk.Frame(tw, bg=_C["text"], padx=1, pady=1)
        border.pack(fill=BOTH, expand=True)

        inner = tk.Frame(border, bg=_C["nav_selbg"])
        inner.pack(fill=BOTH, expand=True)

        # Header: "Bid failed"
        tk.Label(
            inner,
            text="  ✗  Bid Failed",
            font=("Segoe UI", 9, "bold"),
            bg=_C["red"],
            fg="white",
            anchor="w",
            padx=self._PAD, pady=4,
        ).pack(fill=X)

        # Body: error message (wrap at ~420 px)
        tk.Label(
            inner,
            text=text,
            font=_FT_SMALL,
            bg=_C["nav_selbg"],
            fg=_C["hdr_fg"],
            anchor="w",
            justify="left",
            wraplength=420,
            padx=self._PAD + 2, pady=self._PAD,
        ).pack(fill=X)

        tw.update_idletasks()
        # Position: prefer below-right of cursor; flip left if too close to edge
        sw = tw.winfo_screenwidth()
        tw_w = tw.winfo_width()
        x = rx + 14 if rx + 14 + tw_w < sw else rx - tw_w - 4
        tw.wm_geometry(f"+{x}+{ry + 18}")
        self._tip_win = tw

    def _hide(self, _event: tk.Event | None = None) -> None:
        if self._tip_win:
            try:
                self._tip_win.destroy()
            except tk.TclError:
                pass
            self._tip_win = None
        self._last_iid = ""


def _fmt_pay(job: dict) -> str:
    pt = job.get("payment_type")
    lo, hi = job.get("pay_min"), job.get("pay_max")
    if pt == "hourly":
        return f"時給 {lo or '?'}–{hi or '?'}"
    if pt == "fixed":
        return f"固定 {lo or '?'}–{hi or '?'}"
    return "—"


# ── Main application ───────────────────────────────────────────────────────────

class CrowdWorksBot(tk.Tk):

    # ──────────────────────────────────────────────────────────────────
    # Initialisation
    # ──────────────────────────────────────────────────────────────────

    def __init__(self) -> None:
        super().__init__()
        db.init()

        self.title("CrowdWorks Bot")
        self.geometry("1280x780")
        self.minsize(900, 580)

        self._menu_labels = feed_menu_labels_by_slug()
        self.jobs: list[dict] = []
        self._filtered_jobs: list[dict] = []
        self._status_var = tk.StringVar(value="Ready.")
        self._current_page = "Dashboard"

        # Bot state
        self._bot_running = False
        self._bot_stop_event = threading.Event()
        self._bot_thread: threading.Thread | None = None
        self._seen_job_ids: set[str] = set()
        self._new_session_ids: set[str] = set()   # jobs found since this app launch
        self._bot_processed = 0
        self._bid_lock = threading.Lock()   # guards _bot_processed & _seen_job_ids

        # Priority bid queue: items are (priority, seq, job, acc, openai_key)
        # priority = −unix_timestamp of last_released_at → newest job = lowest value
        # seq      = monotonically increasing counter to break ties (FIFO within same priority)
        self._bid_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._bid_seq   = 0                           # guarded by _bid_lock
        self._bid_workers: list[threading.Thread] = []

        self._configure_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._init_tray()
        self._navigate("Dashboard")

    # ──────────────────────────────────────────────────────────────────
    # Styles
    # ──────────────────────────────────────────────────────────────────

    def _configure_styles(self) -> None:
        s = ttk.Style(self)
        s.theme_use("clam")

        # Treeview
        s.configure("Treeview",
            background=_C["card_bg"],
            foreground=_C["text"],
            rowheight=30,
            fieldbackground=_C["card_bg"],
            borderwidth=0,
            font=_FT_BODY,
        )
        s.configure("Treeview.Heading",
            background=_C["page_bg"],
            foreground=_C["text2"],
            font=("Segoe UI", 9, "bold"),
            borderwidth=0,
            relief="flat",
            padding=(8, 7),
        )
        s.map("Treeview",
            background=[("selected", _C["blue"])],
            foreground=[("selected", "white")],
        )
        s.map("Treeview.Heading",
            background=[("active", _C["border"])],
            relief=[("active", "flat")],
        )

        # TButton
        s.configure("TButton",
            background=_C["card_bg"],
            foreground=_C["text2"],
            bordercolor=_C["border2"],
            relief="flat",
            padding=(10, 5),
            font=_FT_BODY,
        )
        s.map("TButton",
            background=[("active", _C["page_bg"]), ("pressed", _C["border"])],
            bordercolor=[("focus", _C["blue"]), ("active", _C["blue"])],
        )

        # Primary blue button style
        s.configure("Primary.TButton",
            background=_C["blue"],
            foreground="white",
            bordercolor=_C["blue_dk"],
            relief="flat",
            padding=(12, 6),
            font=_FT_BOLD,
        )
        s.map("Primary.TButton",
            background=[("active", _C["blue_dk"]), ("pressed", _C["blue_dk"])],
        )

        # Entry
        s.configure("TEntry",
            fieldbackground=_C["card_bg"],
            bordercolor=_C["border2"],
            insertcolor=_C["text"],
            selectbackground=_C["blue"],
            relief="flat",
            padding=(6, 5),
            font=_FT_BODY,
        )
        s.map("TEntry",
            bordercolor=[("focus", _C["blue"])],
            fieldbackground=[("readonly", _C["page_bg"])],
        )

        # Spinbox
        s.configure("TSpinbox",
            fieldbackground=_C["card_bg"],
            bordercolor=_C["border2"],
            insertcolor=_C["text"],
            relief="flat",
            padding=(5, 4),
            font=_FT_BODY,
        )
        s.map("TSpinbox",
            bordercolor=[("focus", _C["blue"])],
        )

        # Combobox
        s.configure("TCombobox",
            fieldbackground=_C["card_bg"],
            bordercolor=_C["border2"],
            selectbackground=_C["blue"],
            relief="flat",
            padding=(5, 4),
            font=_FT_BODY,
        )
        s.map("TCombobox",
            bordercolor=[("focus", _C["blue"])],
            fieldbackground=[("readonly", _C["card_bg"])],
        )

        # Scrollbars — slim and modern
        for orient in ("Vertical", "Horizontal"):
            s.configure(f"{orient}.TScrollbar",
                background=_C["border"],
                troughcolor=_C["page_bg"],
                bordercolor=_C["page_bg"],
                arrowcolor=_C["gray"],
                relief="flat",
                width=8,
            )
            s.map(f"{orient}.TScrollbar",
                background=[("active", _C["border2"]), ("pressed", _C["gray"])],
            )

        # Checkbutton
        s.configure("TCheckbutton",
            background=_C["card_bg"],
            foreground=_C["text"],
            font=_FT_BODY,
        )
        s.map("TCheckbutton",
            background=[("active", _C["card_bg"])],
        )

        # Label frame
        s.configure("TLabelframe",
            background=_C["card_bg"],
            bordercolor=_C["border"],
            relief="flat",
        )
        s.configure("TLabelframe.Label",
            background=_C["card_bg"],
            foreground=_C["text2"],
            font=_FT_BOLD,
        )

    # ──────────────────────────────────────────────────────────────────
    # Tray icon
    # ──────────────────────────────────────────────────────────────────

    def _init_tray(self) -> None:
        self._tray_icon: "pystray.Icon | None" = None  # type: ignore[name-defined]
        self.bind("<Unmap>", self._on_unmap)

    def _on_unmap(self, event: tk.Event) -> None:
        if str(event.widget) == str(self):
            self.after(80, self._check_minimize)

    def _check_minimize(self) -> None:
        if self.state() == "iconic":
            self._minimize_to_tray()

    def _minimize_to_tray(self) -> None:
        self.withdraw()
        if self._tray_icon is not None:
            return
        if not _TRAY_AVAILABLE:
            self.iconify()
            return

        size = 64
        img = Image.new("RGB", (size, size), _C["blue"])
        d = ImageDraw.Draw(img)
        d.ellipse([3, 3, size - 4, size - 4], fill=_C["blue"])
        d.rectangle([14, 20, 20, 44], fill="white")
        d.rectangle([26, 20, 32, 44], fill="white")
        d.polygon([(20, 20), (26, 44), (32, 20)], fill="white")
        d.rectangle([36, 20, 50, 44], fill="white", outline=_C["blue"])
        d.rectangle([38, 22, 48, 42], fill=_C["blue"])
        d.arc([36, 25, 50, 39], 0, 180, fill="white", width=3)

        running = self._bot_running

        def _show(icon: "pystray.Icon", _item) -> None:  # type: ignore[name-defined]
            icon.stop()
            self._tray_icon = None
            self.after(0, self._restore_window)

        def _toggle(_icon, _item) -> None:
            self.after(0, self._toggle_bot)

        def _exit(icon: "pystray.Icon", _item) -> None:  # type: ignore[name-defined]
            icon.stop()
            self._tray_icon = None
            self.after(0, self._on_close)

        menu = pystray.Menu(  # type: ignore[name-defined]
            pystray.MenuItem("Show Window", _show, default=True),  # type: ignore[name-defined]
            pystray.Menu.SEPARATOR,  # type: ignore[name-defined]
            pystray.MenuItem(  # type: ignore[name-defined]
                "Stop Bot" if running else "Start Bot", _toggle
            ),
            pystray.Menu.SEPARATOR,  # type: ignore[name-defined]
            pystray.MenuItem("Exit", _exit),  # type: ignore[name-defined]
        )
        icon = pystray.Icon("crowdworks_bot", img, "CrowdWorks Bot", menu)  # type: ignore[name-defined]
        self._tray_icon = icon
        threading.Thread(target=icon.run, daemon=True).start()

    def _restore_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    # ──────────────────────────────────────────────────────────────────
    # UI: skeleton
    # ──────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._build_header()
        pane = tk.Frame(self, bg=_C["page_bg"])
        pane.pack(fill=BOTH, expand=True)
        self._build_sidebar(pane)
        self._build_pages(pane)
        self._build_statusbar()

    # ── Header ────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=_C["hdr_bg"], height=60)
        hdr.pack(fill=X)
        hdr.pack_propagate(False)

        # Left: logo area
        logo_area = tk.Frame(hdr, bg=_C["hdr_bg"])
        logo_area.pack(side=LEFT, padx=(20, 0))

        tk.Label(
            logo_area, text="●",
            font=("Segoe UI", 11),
            bg=_C["hdr_bg"], fg=_C["blue"],
        ).pack(side=LEFT, padx=(0, 7))

        tk.Label(
            logo_area, text="CrowdWorks Bot",
            font=("Segoe UI", 13, "bold"),
            bg=_C["hdr_bg"], fg=_C["hdr_fg"],
        ).pack(side=LEFT)

        badge = tk.Label(
            logo_area, text=" v2 ",
            font=("Segoe UI", 7, "bold"),
            bg=_C["nav_selbg"], fg=_C["nav_fg"],
            padx=3, pady=1,
        )
        badge.pack(side=LEFT, padx=(8, 0))

        # Right: bot status label + start/stop button
        self._hdr_bot_lbl = tk.Label(
            hdr, text="", font=_FT_SMALL,
            bg=_C["hdr_bg"], fg=_C["gray"],
        )
        self._hdr_bot_lbl.pack(side=RIGHT, padx=(0, 18))

        self._btn_bot = tk.Button(
            hdr, text="▶  Start Bot",
            font=_FT_BOLD, bg=_C["green"], fg="white",
            activebackground=_C["green_dk"], activeforeground="white",
            relief="flat", bd=0, padx=20, pady=7,
            cursor="hand2", command=self._toggle_bot,
        )
        self._btn_bot.pack(side=RIGHT, padx=(0, 12), pady=11)

    # ── Sidebar ───────────────────────────────────────────────────────

    def _build_sidebar(self, parent: tk.Frame) -> None:
        sidebar = tk.Frame(parent, bg=_C["nav_bg"], width=200)
        sidebar.pack(side=LEFT, fill=Y)
        sidebar.pack_propagate(False)

        # Sidebar section header (matches app header height)
        logo_row = tk.Frame(sidebar, bg=_C["nav_bg"], height=60)
        logo_row.pack(fill=X)
        logo_row.pack_propagate(False)
        tk.Label(
            logo_row, text="CW",
            font=("Segoe UI", 15, "bold"),
            bg=_C["nav_bg"], fg=_C["blue"],
        ).pack(side=LEFT, padx=(20, 5), pady=16)
        tk.Label(
            logo_row, text="AutoBid",
            font=("Segoe UI", 10),
            bg=_C["nav_bg"], fg=_C["nav_fg"],
        ).pack(side=LEFT, pady=16)

        tk.Frame(sidebar, bg=_C["nav_sep"], height=1).pack(fill=X, pady=(0, 8))

        self._nav_btns: dict[str, tk.Button] = {}
        self._nav_indicators: dict[str, tk.Frame] = {}

        for name in _NAV_ITEMS:
            row = tk.Frame(sidebar, bg=_C["nav_bg"])
            row.pack(fill=X, pady=1)

            # Left accent bar (3 px)
            indicator = tk.Frame(row, width=3, bg=_C["nav_bg"])
            indicator.pack(side=LEFT, fill=Y)
            indicator.pack_propagate(False)

            icon = _NAV_ICONS.get(name, "")
            btn = tk.Button(
                row, text=f"  {icon}  {name}",
                anchor="w", font=_FT_NAV,
                bg=_C["nav_bg"], fg=_C["nav_fg"],
                activebackground=_C["nav_selbg"], activeforeground=_C["nav_selfg"],
                relief="flat", bd=0, cursor="hand2",
                padx=8, pady=10,
                command=lambda n=name: self._navigate(n),
            )
            btn.pack(fill=X)
            self._nav_btns[name] = btn
            self._nav_indicators[name] = indicator

        # Bottom hint
        tk.Frame(sidebar, bg=_C["nav_bg"]).pack(fill=BOTH, expand=True)
        tk.Label(
            sidebar, text="⊟  Minimize → Tray",
            font=("Segoe UI", 7), fg="#475569",
            bg=_C["nav_bg"],
        ).pack(pady=(0, 10))

    # ── Page container + pages ────────────────────────────────────────

    def _build_pages(self, parent: tk.Frame) -> None:
        container = tk.Frame(parent, bg=_C["page_bg"])
        container.pack(side=LEFT, fill=BOTH, expand=True)

        self._pages: dict[str, tk.Frame] = {}
        for name in _NAV_ITEMS:
            page = tk.Frame(container, bg=_C["page_bg"])
            page.place(relwidth=1, relheight=1)
            self._pages[name] = page

        self._build_dashboard_page()
        self._build_accounts_page()
        self._build_jobs_page()
        self._build_settings_page()
        self._build_log_page()

    # ── Status bar ────────────────────────────────────────────────────

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, bg=_C["hdr_bg"], height=26)
        bar.pack(fill=X, side="bottom")
        bar.pack_propagate(False)

        self._status_dot = tk.Label(
            bar, text="●", font=("Segoe UI", 8),
            bg=_C["hdr_bg"], fg=_C["gray"],
        )
        self._status_dot.pack(side=LEFT, padx=(12, 4), pady=4)

        tk.Label(
            bar, textvariable=self._status_var,
            font=_FT_SMALL, bg=_C["hdr_bg"], fg=_C["nav_fg"],
        ).pack(side=LEFT, pady=4)

    # ──────────────────────────────────────────────────────────────────
    # Navigation
    # ──────────────────────────────────────────────────────────────────

    def _navigate(self, page: str) -> None:
        for name, btn in self._nav_btns.items():
            active = name == page
            btn.config(
                bg=_C["nav_selbg"] if active else _C["nav_bg"],
                fg=_C["nav_selfg"] if active else _C["nav_fg"],
                font=("Segoe UI", 10, "bold") if active else _FT_NAV,
            )
            self._nav_indicators[name].config(
                bg=_C["nav_sel"] if active else _C["nav_bg"]
            )
        self._pages[page].lift()
        self._current_page = page
        {
            "Dashboard": self._refresh_dashboard,
            "Accounts":  self._refresh_accounts,
            "Jobs":      self._refresh_jobs,
            "Settings":  self._refresh_settings,
            "Log":       self._refresh_log,
        }[page]()

    # ──────────────────────────────────────────────────────────────────
    # Dashboard page
    # ──────────────────────────────────────────────────────────────────

    def _build_dashboard_page(self) -> None:
        page = self._pages["Dashboard"]
        _hdr_row = tk.Frame(page, bg=_C["page_bg"])
        _hdr_row.pack(fill=X, padx=24, pady=(22, 14))
        tk.Frame(_hdr_row, bg=_C["blue"], width=4).pack(side=LEFT, fill=Y, padx=(0, 10))
        _lbl(_hdr_row, "Dashboard", font=_FT_H1, fg=_C["text"]).pack(side=LEFT, anchor=W)

        # Stat cards
        stats_row = tk.Frame(page, bg=_C["page_bg"])
        stats_row.pack(fill=X, padx=24, pady=(0, 18))

        _STAT_META = {
            "accounts":     ("Active Accounts", _C["blue"]),
            "jobs":         ("Total Jobs",       _C["green"]),
            "bids_session": ("Bids (session)",   _C["yellow"]),
            "bot_status":   ("Bot Status",       _C["gray"]),
        }
        self._stat_vars: dict[str, tk.StringVar] = {}
        for key, (label, accent) in _STAT_META.items():
            var = tk.StringVar(value="—")
            self._stat_vars[key] = var
            card = _card(stats_row)
            card.pack(side=LEFT, fill=X, expand=True, padx=(0, 14))
            # Accent top border (simulate with thin frame)
            tk.Frame(card, bg=accent, height=3).pack(fill=X)
            _lbl(card, label, font=_FT_SMALL,
                 fg=_C["text3"], bg=_C["card_bg"]).pack(anchor=W, padx=16, pady=(12, 0))
            tk.Label(
                card, textvariable=var,
                font=("Segoe UI", 24, "bold"),
                bg=_C["card_bg"], fg=_C["text"],
            ).pack(anchor=W, padx=16, pady=(4, 14))

        # Recent activity
        act = _card(page)
        act.pack(fill=BOTH, expand=True, padx=24, pady=(0, 22))
        _lbl(act, "Recent Activity", font=_FT_H2,
             bg=_C["card_bg"]).pack(anchor=W, padx=16, pady=(12, 6))

        cols = ("time", "account", "level", "message")
        _dash_frame = tk.Frame(act, bg=_C["card_bg"])
        _dash_frame.pack(fill=BOTH, expand=True, padx=(16, 8), pady=(0, 14))
        _dash_frame.columnconfigure(0, weight=1)
        _dash_frame.rowconfigure(0, weight=1)
        self._dash_tree = ttk.Treeview(_dash_frame, columns=cols, show="headings", height=14)
        for col, lbl, w, stretch in [
            ("time",    "Time",    155, False),
            ("account", "Account",  95, False),
            ("level",   "Level",    68, False),
            ("message", "Message", 800, True),
        ]:
            self._dash_tree.heading(col, text=lbl)
            self._dash_tree.column(col, width=w, stretch=stretch)
        self._dash_tree.tag_configure("error",   foreground=_C["red"])
        self._dash_tree.tag_configure("success", foreground=_C["green"])
        self._dash_tree.tag_configure("warning", foreground=_C["yellow"])

        vsb = ttk.Scrollbar(_dash_frame, orient="vertical",   command=self._dash_tree.yview)
        hsb = ttk.Scrollbar(_dash_frame, orient="horizontal", command=self._dash_tree.xview)
        self._dash_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._dash_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

    def _refresh_dashboard(self) -> None:
        accounts = db.list_accounts(_U)
        active = sum(1 for a in accounts if a["enabled"])
        total_jobs = len(load_jobs_jsonl(DATA_FILE)) if DATA_FILE.exists() else 0

        self._stat_vars["accounts"].set(f"{active} / {len(accounts)}")
        self._stat_vars["jobs"].set(str(total_jobs))
        self._stat_vars["bids_session"].set(str(self._bot_processed))
        self._stat_vars["bot_status"].set("Running" if self._bot_running else "Stopped")

        logs = db.list_logs(_U,limit=20)
        self._dash_tree.delete(*self._dash_tree.get_children())
        for entry in logs:
            ts = (entry.get("created_at") or "")[:16]
            acc  = entry.get("account_name") or "System"
            lvl  = (entry.get("level") or "info")
            self._dash_tree.insert(
                "", END,
                values=(ts, acc, lvl.upper(), entry.get("message") or ""),
                tags=(lvl,),
            )

    # ──────────────────────────────────────────────────────────────────
    # Accounts page
    # ──────────────────────────────────────────────────────────────────

    def _build_accounts_page(self) -> None:
        page = self._pages["Accounts"]
        _hr = tk.Frame(page, bg=_C["page_bg"])
        _hr.pack(fill=X, padx=24, pady=(22, 14))
        tk.Frame(_hr, bg=_C["blue"], width=4).pack(side=LEFT, fill=Y, padx=(0, 10))
        _lbl(_hr, "Accounts", font=_FT_H1, fg=_C["text"]).pack(side=LEFT, anchor=W)

        toolbar = tk.Frame(page, bg=_C["page_bg"])
        toolbar.pack(fill=X, padx=24, pady=(0, 10))
        for lbl, cmd in [
            ("+ Add Account",     lambda: self._open_account_dialog()),
            ("Edit",              lambda: self._open_account_dialog(edit=True)),
            ("Delete",            self._delete_selected_account),
            ("Verify Session",    self._verify_selected_account),
            ("Enable / Disable",  self._toggle_account_enabled),
        ]:
            ttk.Button(toolbar, text=lbl, command=cmd).pack(side=LEFT, padx=(0, 6))

        card = _card(page)
        card.pack(fill=BOTH, expand=True, padx=24, pady=(0, 22))

        cols = ("name", "username", "prompt", "status", "enabled", "bids", "last_verified")
        _acc_frame = tk.Frame(card, bg=_C["card_bg"])
        _acc_frame.pack(fill=BOTH, expand=True, padx=8, pady=8)
        _acc_frame.columnconfigure(0, weight=1)
        _acc_frame.rowconfigure(0, weight=1)
        self._acc_tree = ttk.Treeview(_acc_frame, columns=cols, show="headings", height=20)
        for col, lbl, w, stretch in [
            ("name",          "Name",          140, False),
            ("username",      "CW Username",   130, False),
            ("prompt",        "Prompt",        140, False),
            ("status",        "Status",         90, False),
            ("enabled",       "Enabled",        65, False),
            ("bids",          "Total Bids",     75, False),
            ("last_verified", "Last Verified",  180, True),
        ]:
            self._acc_tree.heading(col, text=lbl)
            self._acc_tree.column(col, width=w, stretch=stretch)
        self._acc_tree.tag_configure("active",     foreground=_C["green"])
        self._acc_tree.tag_configure("expired",    foreground=_C["red"])
        self._acc_tree.tag_configure("unverified", foreground=_C["gray"])
        self._acc_tree.tag_configure("disabled",   foreground=_C["gray"])
        self._acc_tree.bind("<Double-1>", lambda _: self._open_account_dialog(edit=True))

        vsb = ttk.Scrollbar(_acc_frame, orient="vertical",   command=self._acc_tree.yview)
        hsb = ttk.Scrollbar(_acc_frame, orient="horizontal", command=self._acc_tree.xview)
        self._acc_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._acc_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

    def _refresh_accounts(self) -> None:
        self._acc_tree.delete(*self._acc_tree.get_children())
        for acc in db.list_accounts(_U):
            status  = acc.get("status") or "unverified"
            enabled = "Yes" if acc["enabled"] else "No"
            tag     = status if acc["enabled"] else "disabled"
            lv      = (acc.get("last_verified") or "Never")[:16]
            self._acc_tree.insert(
                "", END, iid=str(acc["id"]),
                values=(
                    acc["name"],
                    acc["cw_username"] or "—",
                    acc.get("prompt_name") or "— (none)",
                    status.capitalize(),
                    enabled,
                    db.count_bids(_U,acc["id"]),
                    lv,
                ),
                tags=(tag,),
            )

    def _selected_account_id(self) -> int | None:
        sel = self._acc_tree.selection()
        return int(sel[0]) if sel else None

    def _open_account_dialog(self, *, edit: bool = False) -> None:
        acc_id = self._selected_account_id() if edit else None
        if edit and acc_id is None:
            messagebox.showinfo("Edit Account", "Select an account first.")
            return
        acc = db.get_account(_U,acc_id) if acc_id else None

        win = tk.Toplevel(self)
        win.title("Edit Account" if edit else "Add Account")
        win.geometry("520x340")
        win.minsize(460, 300)
        win.resizable(True, False)
        win.grab_set()

        f = ttk.Frame(win, padding=(22, 18))
        f.pack(fill=BOTH, expand=True)
        f.columnconfigure(0, minsize=130)
        f.columnconfigure(1, weight=1)   # entry column stretches

        # Name
        ttk.Label(f, text="Display Name").grid(row=0, column=0, sticky=W, pady=7)
        name_var = tk.StringVar(value=acc["name"] if acc else "")
        ttk.Entry(f, textvariable=name_var).grid(row=0, column=1, columnspan=2,
                                                  padx=8, sticky="ew")

        # Session
        ttk.Label(f, text="_cw_session_id").grid(row=1, column=0, sticky=W, pady=7)
        session_var = tk.StringVar(value=acc["session_id"] if acc else "")
        session_entry = ttk.Entry(f, textvariable=session_var, show="*")
        session_entry.grid(row=1, column=1, padx=8, sticky="ew")
        def _toggle_session() -> None:
            session_entry.config(show="" if session_entry.cget("show") == "*" else "*")
        ttk.Button(f, text="Show/Hide", command=_toggle_session).grid(row=1, column=2, padx=(4, 0))

        # Prompt
        ttk.Label(f, text="Prompt").grid(row=2, column=0, sticky=W, pady=7)
        prompts = db.list_prompts(_U)
        prompt_opts = ["(none)"] + [f"[{p['id']}] {p['name']}" for p in prompts]
        prompt_var = tk.StringVar(value="(none)")
        if acc and acc.get("prompt_id"):
            matched = next(
                (f"[{p['id']}] {p['name']}" for p in prompts if p["id"] == acc["prompt_id"]),
                "(none)",
            )
            prompt_var.set(matched)
        ttk.Combobox(f, textvariable=prompt_var, values=prompt_opts,
                     state="readonly").grid(row=2, column=1, columnspan=2,
                                            padx=8, sticky="ew")

        # Enabled
        enabled_var = tk.BooleanVar(value=bool(acc["enabled"]) if acc else True)
        ttk.Checkbutton(f, text="Enabled", variable=enabled_var).grid(
            row=3, column=1, sticky=W, padx=8, pady=7)

        # Verify status label
        verify_lbl = ttk.Label(f, text="", foreground="gray")
        verify_lbl.grid(row=4, column=0, columnspan=3, sticky=W, pady=4)

        def _verify() -> None:
            sid = session_var.get().strip()
            if not sid:
                verify_lbl.config(text="Paste a session ID first.", foreground=_C["yellow"])
                return
            verify_lbl.config(text="Checking…", foreground=_C["gray"])
            win.update_idletasks()
            def _work() -> None:
                result = cword_auth.check_session(sid)
                def _done() -> None:
                    if result.get("ok"):
                        uname = result.get("username", "")
                        verify_lbl.config(
                            text=f"Session active — {uname}" if uname else "Session active",
                            foreground=_C["green"],
                        )
                        if not name_var.get().strip() and uname:
                            name_var.set(uname)
                    else:
                        verify_lbl.config(
                            text=result.get("error", "Verification failed."),
                            foreground=_C["red"],
                        )
                win.after(0, _done)
            threading.Thread(target=_work, daemon=True).start()

        def _save() -> None:
            name = name_var.get().strip()
            sid  = session_var.get().strip()
            if not sid:
                messagebox.showerror("Account", "Session ID is required.", parent=win)
                return
            pid: int | None = None
            pv = prompt_var.get()
            if pv and pv != "(none)":
                try:
                    pid = int(pv.split("]")[0].lstrip("["))
                except (ValueError, IndexError):
                    pass
            enabled = int(enabled_var.get())
            if acc_id:
                db.update_account(_U,acc_id,
                                  name=name or f"Account {acc_id}",
                                  session_id=sid,
                                  prompt_id=pid,
                                  enabled=enabled)
            else:
                new_id = db.add_account(_U,name or "New Account", sid,
                                        prompt_id=pid, enabled=enabled)
                db.add_log(_U,f"Account '{name or 'New Account'}' added.", level="info",
                           account_id=new_id)
            win.destroy()
            self._refresh_accounts()

        btn_row = ttk.Frame(f)
        btn_row.grid(row=5, column=0, columnspan=3, sticky=W, pady=(14, 0))
        ttk.Button(btn_row, text="Verify Session", command=_verify).pack(side=LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=win.destroy).pack(side=LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Save", command=_save).pack(side=LEFT)

    def _verify_selected_account(self) -> None:
        acc_id = self._selected_account_id()
        if acc_id is None:
            messagebox.showinfo("Verify", "Select an account first.")
            return
        acc = db.get_account(_U,acc_id)
        if not acc:
            return
        self._status_var.set(f"Verifying session for '{acc['name']}'…")

        def _work() -> None:
            result = cword_auth.check_session(acc["session_id"])
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if result.get("ok"):
                uname = result.get("username", "")
                db.update_account(_U,acc_id, status="active",
                                  cw_username=uname, last_verified=now)
                db.add_log(_U,f"Session verified for '{acc['name']}' ({uname})",
                           level="success", account_id=acc_id)
                self._ui(lambda: self._status_var.set(f"Session active — {uname}"))
            else:
                db.update_account(_U,acc_id, status="expired", last_verified=now)
                db.add_log(_U,f"Session expired for '{acc['name']}': {result.get('error')}",
                           level="error", account_id=acc_id)
                self._ui(lambda: self._status_var.set("Session expired."))
            self._ui(self._refresh_accounts)

        threading.Thread(target=_work, daemon=True).start()

    def _delete_selected_account(self) -> None:
        acc_id = self._selected_account_id()
        if acc_id is None:
            messagebox.showinfo("Delete", "Select an account first.")
            return
        acc = db.get_account(_U,acc_id)
        if not acc:
            return
        if not messagebox.askyesno(
            "Delete Account",
            f"Delete '{acc['name']}'?\nAll bid records for this account will also be removed.",
        ):
            return
        db.delete_account(_U,acc_id)
        self._refresh_accounts()

    def _toggle_account_enabled(self) -> None:
        acc_id = self._selected_account_id()
        if acc_id is None:
            messagebox.showinfo("Enable/Disable", "Select an account first.")
            return
        acc = db.get_account(_U,acc_id)
        if not acc:
            return
        db.update_account(_U,acc_id, enabled=0 if acc["enabled"] else 1)
        self._refresh_accounts()

    # ──────────────────────────────────────────────────────────────────
    # Jobs page
    # ──────────────────────────────────────────────────────────────────

    _NEW_HOURS = 24   # jobs posted within this many hours are considered "new"

    def _is_new_job(self, job: dict) -> bool:
        """Return True when a job should display the NEW PROJECT badge.

        A job is considered new when either:
        • its ID was discovered during the current bot session, OR
        • its ``last_released_at`` timestamp is within the last 24 hours.
        """
        jid = str(job.get("job_offer_id") or "")
        if jid in self._new_session_ids:
            return True
        raw_ts = job.get("last_released_at") or ""
        if raw_ts:
            try:
                dt = datetime.fromisoformat(str(raw_ts))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if datetime.now(tz=timezone.utc) - dt < timedelta(hours=self._NEW_HOURS):
                    return True
            except (ValueError, TypeError):
                pass
        return False

    def _build_jobs_page(self) -> None:
        page = self._pages["Jobs"]
        _hr = tk.Frame(page, bg=_C["page_bg"])
        _hr.pack(fill=X, padx=24, pady=(22, 14))
        tk.Frame(_hr, bg=_C["blue"], width=4).pack(side=LEFT, fill=Y, padx=(0, 10))
        _lbl(_hr, "Jobs", font=_FT_H1, fg=_C["text"]).pack(side=LEFT, anchor=W)

        # Filter bar — row 1: dropdowns + search
        fbar = tk.Frame(page, bg=_C["page_bg"])
        fbar.pack(fill=X, padx=24, pady=(0, 4))

        _lbl(fbar, "Account", bg=_C["page_bg"]).pack(side=LEFT, padx=(0, 4))
        self._job_acc_var = tk.StringVar(value="All accounts")
        self._job_acc_cb = ttk.Combobox(fbar, textvariable=self._job_acc_var,
                                         state="readonly", width=16)
        self._job_acc_cb.pack(side=LEFT, padx=(0, 14))
        self._job_acc_cb.bind("<<ComboboxSelected>>", lambda _: self._apply_job_filters())

        _lbl(fbar, "Category", bg=_C["page_bg"]).pack(side=LEFT, padx=(0, 4))
        cats = ["All"] + [str(f["menu_label"]) for f in category_feeds_ordered()]
        self._job_cat_var = tk.StringVar(value="All")
        cat_cb = ttk.Combobox(fbar, textvariable=self._job_cat_var,
                               values=cats, state="readonly", width=13)
        cat_cb.pack(side=LEFT, padx=(0, 14))
        cat_cb.bind("<<ComboboxSelected>>", lambda _: self._apply_job_filters())

        _lbl(fbar, "Search", bg=_C["page_bg"]).pack(side=LEFT, padx=(0, 4))
        self._job_search_var = tk.StringVar()
        self._job_search_var.trace_add("write", lambda *_: self._apply_job_filters())
        # Search entry grows to fill remaining space in the filter row
        _search_entry = ttk.Entry(fbar, textvariable=self._job_search_var, width=22)
        _search_entry.pack(side=LEFT, padx=(0, 14), fill=X, expand=True)

        _lbl(fbar, "Bid Status", bg=_C["page_bg"]).pack(side=LEFT, padx=(0, 4))
        self._job_bid_var = tk.StringVar(value="All")
        bid_cb = ttk.Combobox(
            fbar, textvariable=self._job_bid_var,
            values=("All", "Not bid yet", "Submitted", "Already Bid", "Failed"),
            state="readonly", width=13,
        )
        bid_cb.pack(side=LEFT, padx=(0, 0))
        bid_cb.bind("<<ComboboxSelected>>", lambda _: self._apply_job_filters())

        # Filter bar — row 2: action buttons
        fbar2 = tk.Frame(page, bg=_C["page_bg"])
        fbar2.pack(fill=X, padx=24, pady=(0, 10))

        ttk.Button(fbar2, text="Open in browser",
                   command=self._open_selected_job).pack(side=LEFT, padx=(0, 6))
        ttk.Button(fbar2, text="Mark bid placed",
                   command=lambda: self._mark_selected_bid(True)).pack(side=LEFT, padx=(0, 4))
        ttk.Button(fbar2, text="Unmark bid",
                   command=lambda: self._mark_selected_bid(False)).pack(side=LEFT, padx=(0, 4))
        ttk.Button(fbar2, text="↺ Retry Failed",
                   command=self._retry_selected_bid).pack(side=LEFT)

        # Table
        card = _card(page)
        card.pack(fill=BOTH, expand=True, padx=24, pady=(0, 22))

        cols = ("account", "category", "bid_status", "job_id",
                "title", "badge", "client", "pay", "released")
        _job_frame = tk.Frame(card, bg=_C["card_bg"])
        _job_frame.pack(fill=BOTH, expand=True, padx=8, pady=8)
        _job_frame.columnconfigure(0, weight=1)
        _job_frame.rowconfigure(0, weight=1)
        self._job_tree = ttk.Treeview(_job_frame, columns=cols, show="headings",
                                       selectmode="browse", height=18)
        for col, lbl, w, stretch in [
            ("account",    "Account",    95, False),
            ("category",   "Category",   90, False),
            ("bid_status", "Bid Status", 100, False),
            ("job_id",     "ID",          90, False),
            ("title",      "Title",      300, True),
            ("badge",      "",            76, False),
            ("client",     "Client",     110, False),
            ("pay",        "Pay",        110, False),
            ("released",   "Released",   165, False),
        ]:
            self._job_tree.heading(col, text=lbl)
            self._job_tree.column(col, width=w, stretch=stretch)
        self._job_tree.tag_configure("bid_success",      background="#dcfce7")  # green-100
        self._job_tree.tag_configure("bid_failed",       background="#fee2e2")  # red-100
        self._job_tree.tag_configure("bid_already",      background="#dbeafe")  # blue-100
        self._job_tree.tag_configure("new_job",          background="#fef9c3")  # amber-100
        # Combined tags: success/already_bid+new → stronger tint
        self._job_tree.tag_configure("bid_success_new",  background="#bbf7d0")  # green-200
        self._job_tree.tag_configure("bid_failed_new",   background="#fecaca")  # red-200
        self._job_tree.tag_configure("bid_already_new",  background="#bfdbfe")  # blue-200
        self._job_tree.bind("<Double-1>", lambda _: self._open_selected_job())
        self._job_tree.bind("<Return>",   lambda _: self._open_selected_job())

        vsb = ttk.Scrollbar(_job_frame, orient="vertical",   command=self._job_tree.yview)
        hsb = ttk.Scrollbar(_job_frame, orient="horizontal", command=self._job_tree.xview)
        self._job_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._job_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        def _job_tip(iid: str) -> str:
            try:
                job = self._filtered_jobs[int(iid)]
            except (ValueError, IndexError):
                return ""
            err = job.get("_bid_error") or ""
            if err:
                acc = job.get("_acc_name_for_tip", "")
                return f"{acc + ': ' if acc else ''}{err}" if err else ""
            return ""

        TreeviewTooltip(self._job_tree, _job_tip)

    def _refresh_jobs(self) -> None:
        self.jobs = load_jobs_jsonl(DATA_FILE) if DATA_FILE.exists() else []
        accounts = db.list_accounts(_U)
        acc_opts = ["All accounts"] + [f"[{a['id']}] {a['name']}" for a in accounts]
        self._job_acc_cb["values"] = acc_opts
        if self._job_acc_var.get() not in acc_opts:
            self._job_acc_var.set("All accounts")
        self._apply_job_filters()

    def _resolve_job_acc_id(self) -> int | None:
        val = self._job_acc_var.get()
        if val == "All accounts":
            return None
        try:
            return int(val.split("]")[0].lstrip("["))
        except (ValueError, IndexError):
            return None

    def _apply_job_filters(self) -> None:
        acc_id       = self._resolve_job_acc_id()
        bid_statuses = db.get_bid_statuses(_U,acc_id)   # {job_id: 'success'|'failed'}
        bid_errors   = db.get_bid_errors(_U,acc_id)     # {job_id: error_msg}

        slug_filter: str | None = None
        cat_val = self._job_cat_var.get()
        if cat_val != "All":
            for f in category_feeds_ordered():
                if str(f["menu_label"]) == cat_val:
                    slug_filter = str(f["slug"])
                    break

        q          = self._job_search_var.get().strip().lower()
        bid_filter = self._job_bid_var.get()

        # Resolve account display name once
        if acc_id is not None:
            acc = db.get_account(_U,acc_id)
            acc_col = acc["name"] if acc else "—"
        else:
            acc_col = "—"

        self._filtered_jobs = []
        for job in self.jobs:
            if slug_filter and job.get("feed_slug") != slug_filter:
                continue
            blob = f"{job.get('title') or ''} {job.get('client_username') or ''} {job.get('job_offer_id') or ''}".lower()
            if q and q not in blob:
                continue

            jid_str    = str(job.get("job_offer_id"))
            bid_status = bid_statuses.get(jid_str, "")   # "", "success", "failed"

            if bid_filter == "Not bid yet" and bid_status:
                continue
            if bid_filter == "Submitted" and bid_status != "success":
                continue
            if bid_filter == "Already Bid" and bid_status != "already_bid":
                continue
            if bid_filter == "Failed" and bid_status != "failed":
                continue

            job["_bid_status"] = bid_status
            job["_bid_error"]  = bid_errors.get(jid_str, "") if bid_status == "failed" else ""
            self._filtered_jobs.append(job)

        # ── Render tree ───────────────────────────────────────────────────────
        _BID_LABEL = {
            "success":     "✓ Submitted",
            "already_bid": "◎ Already Bid",
            "failed":      "✗ Failed",
        }
        # Tag matrix: (bid_status, is_new) → tag name
        _TAG_MAP = {
            ("success",     False): "bid_success",
            ("success",     True):  "bid_success_new",
            ("already_bid", False): "bid_already",
            ("already_bid", True):  "bid_already_new",
            ("failed",      False): "bid_failed",
            ("failed",      True):  "bid_failed_new",
            ("",            True):  "new_job",
            ("",            False): "",
        }

        self._job_tree.delete(*self._job_tree.get_children())
        for i, job in enumerate(self._filtered_jobs):
            cat = job.get("feed_menu_label") or self._menu_labels.get(
                str(job.get("feed_slug") or ""), job.get("feed_slug") or "—",
            )
            bid_status = job.get("_bid_status", "")
            bid_label  = _BID_LABEL.get(bid_status, "—")
            is_new     = self._is_new_job(job)
            badge_text = "★ NEW PROJECT" if is_new else ""
            row_tag    = _TAG_MAP.get((bid_status, is_new), "")

            self._job_tree.insert(
                "", END, iid=str(i),
                values=(
                    acc_col,
                    cat,
                    bid_label,
                    job.get("job_offer_id"),
                    (job.get("title") or "")[:200],
                    badge_text,
                    job.get("client_username") or "—",
                    _fmt_pay(job),
                    job.get("last_released_at") or "—",
                ),
                tags=(row_tag,) if row_tag else (),
            )

        note = f"  ·  bot running ({self._bot_processed} auto-bid)" if self._bot_running else ""
        self._status_var.set(
            f"Jobs: {len(self._filtered_jobs)} shown / {len(self.jobs)} total{note}"
        )

    def _selected_job(self) -> dict | None:
        sel = self._job_tree.selection()
        if not sel:
            return None
        i = int(sel[0])
        return self._filtered_jobs[i] if 0 <= i < len(self._filtered_jobs) else None

    def _open_selected_job(self) -> None:
        job = self._selected_job()
        if not job:
            messagebox.showinfo("Open", "Select a job row first.")
            return
        jid = job.get("job_offer_id")
        if jid:
            webbrowser.open(job_public_url(jid))

    def _mark_selected_bid(self, mark: bool) -> None:
        job = self._selected_job()
        if not job:
            messagebox.showinfo("Bid mark", "Select a job row first.")
            return
        jid = str(job.get("job_offer_id") or "")
        if not jid:
            return
        if mark:
            db.record_bid(_U,jid, job_title=job.get("title") or "", status="success")
        else:
            db.delete_bid(_U,None, jid)
        self._apply_job_filters()

    def _retry_selected_bid(self) -> None:
        """Re-queue (or directly run) a bid for the selected job when its status is 'failed'.

        Behaviour:
        • Deletes the failed bid record so has_bid() returns False again.
        • If the bot is running  → enqueues the job into the priority bid queue for
          each relevant account (the worker thread picks it up automatically).
        • If the bot is stopped  → spawns a one-shot daemon thread that calls
          _process_job_for_account() directly, then refreshes the table.
        Account scope follows the Account filter: a specific account if one is
        selected, otherwise every enabled account that had a failed bid for this job.
        """
        job = self._selected_job()
        if not job:
            messagebox.showinfo("Retry", "Select a job row first.")
            return

        bid_status = job.get("_bid_status", "")
        if bid_status == "already_bid":
            messagebox.showinfo(
                "Retry",
                "This job is marked '◎ Already Bid' — the account has already\n"
                "applied to it on CrowdWorks. No retry is possible.",
            )
            return
        if bid_status != "failed":
            messagebox.showinfo(
                "Retry",
                "Only jobs with '✗ Failed' bid status can be retried.\n"
                "Select a red-highlighted row first.",
            )
            return

        jid = str(job.get("job_offer_id") or "")
        if not jid:
            return

        # ── Determine which accounts to retry for ─────────────────────────────
        acc_id   = self._resolve_job_acc_id()
        accounts = db.list_accounts(_U)

        if acc_id is not None:
            # Specific account selected in the filter dropdown
            retry_accounts = [a for a in accounts if a["id"] == acc_id]
        else:
            # "All accounts" — retry every enabled account that has a failed bid
            retry_accounts = [
                a for a in accounts
                if a["enabled"]
                and db.get_bid_statuses(_U,a["id"]).get(jid) == "failed"
            ]
            if not retry_accounts:
                # Fallback: all enabled accounts (no prior bid record found)
                retry_accounts = [a for a in accounts if a["enabled"]]

        if not retry_accounts:
            messagebox.showinfo("Retry", "No enabled accounts found to retry with.")
            return

        openai_key = db.get_setting(_U,"openai_api_key")
        if not openai_key:
            messagebox.showerror(
                "Retry",
                "OpenAI API key is not configured.\nGo to Settings → OpenAI API Key.",
            )
            return

        title = (job.get("title") or "")[:80]

        # ── Clear failed records so the worker doesn't skip this job ──────────
        for acc in retry_accounts:
            db.delete_bid(_U,acc["id"], jid)

        db.add_log(_U,
            f"Manual retry requested for job {jid}: {title} "
            f"({len(retry_accounts)} account(s)).",
            level="info",
        )

        # ── Dispatch ──────────────────────────────────────────────────────────
        if self._bot_running:
            priority = -time.time()   # treat as "just now" — after any queued auto-bids
            for acc in retry_accounts:
                with self._bid_lock:
                    seq = self._bid_seq
                    self._bid_seq += 1
                self._bid_queue.put((priority, seq, job, acc, openai_key))
            n = len(retry_accounts)
            self._status_var.set(
                f"Retry queued: job {jid} for {n} account(s) — "
                "bot will process it shortly."
            )
        else:
            # Bot is stopped — run in a disposable daemon thread
            def _run_retry(
                _job=job, _accounts=retry_accounts, _key=openai_key, _jid=jid
            ) -> None:
                for _acc in _accounts:
                    try:
                        self._process_job_for_account(_job, _acc, _key)
                    except Exception as exc:
                        db.add_log(_U,
                            f"[{_acc['name']}] Retry error for job {_jid}: {exc}",
                            level="error",
                            account_id=_acc["id"],
                        )
                self._ui(self._refresh_jobs_quiet)

            n = len(retry_accounts)
            threading.Thread(
                target=_run_retry, daemon=True, name=f"retry-{jid}"
            ).start()
            self._status_var.set(
                f"Retrying job {jid} for {n} account(s)… "
                "(browser will open)"
            )

        self._apply_job_filters()

    # ──────────────────────────────────────────────────────────────────
    # Settings page
    # ──────────────────────────────────────────────────────────────────

    def _build_settings_page(self) -> None:
        page = self._pages["Settings"]
        _hr = tk.Frame(page, bg=_C["page_bg"])
        _hr.pack(fill=X, padx=24, pady=(22, 14))
        tk.Frame(_hr, bg=_C["blue"], width=4).pack(side=LEFT, fill=Y, padx=(0, 10))
        _lbl(_hr, "Settings", font=_FT_H1, fg=_C["text"]).pack(side=LEFT, anchor=W)

        # Scrollable canvas so page content doesn't get clipped
        canvas = tk.Canvas(page, bg=_C["page_bg"], highlightthickness=0)
        vsb = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=RIGHT, fill=Y)
        canvas.pack(side=LEFT, fill=BOTH, expand=True)

        inner = tk.Frame(canvas, bg=_C["page_bg"])
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_canvas_resize(e: tk.Event) -> None:
            canvas.itemconfig(win_id, width=e.width)

        def _on_inner_resize(_e: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        canvas.bind("<Configure>", _on_canvas_resize)
        inner.bind("<Configure>", _on_inner_resize)

        def _scroll_mousewheel(e: tk.Event) -> None:
            canvas.yview_scroll(-1 * (e.delta // 120), "units")

        canvas.bind("<MouseWheel>", _scroll_mousewheel)

        # ── Global settings card ──────────────────────────────────────
        gc = _card(inner)
        gc.pack(fill=X, padx=24, pady=(0, 18))
        _lbl(gc, "Global Settings", font=_FT_H2, bg=_C["card_bg"]).pack(
            anchor=W, padx=16, pady=(14, 8))

        gf = tk.Frame(gc, bg=_C["card_bg"])
        gf.pack(fill=X, padx=16, pady=(0, 6))
        gf.columnconfigure(0, minsize=230)
        gf.columnconfigure(1, weight=1)   # entry column stretches with the window
        gf.columnconfigure(2, minsize=160)

        _lbl(gf, "OpenAI API Key", bg=_C["card_bg"]).grid(
            row=0, column=0, sticky=W, pady=7)
        self._sett_oai_var = tk.StringVar(value=db.get_setting(_U,"openai_api_key"))
        oai_entry = tk.Entry(gf, textvariable=self._sett_oai_var,
                             show="*", font=_FT_BODY)
        oai_entry.grid(row=0, column=1, padx=8, sticky="ew")
        def _toggle_oai() -> None:
            oai_entry.config(show="" if oai_entry.cget("show") == "*" else "*")
        ttk.Button(gf, text="Show/Hide", command=_toggle_oai).grid(
            row=0, column=2, padx=(4, 0), sticky=W)

        _lbl(gf, "Scrape Interval (s, min 10)", bg=_C["card_bg"]).grid(
            row=1, column=0, sticky=W, pady=7)
        self._sett_interval_var = tk.StringVar(
            value=db.get_setting(_U,"scrape_interval", "30"))
        ttk.Spinbox(gf, from_=10, to=3600, increment=10,
                    textvariable=self._sett_interval_var, width=10).grid(
            row=1, column=1, padx=8, sticky=W)

        _lbl(gf, "Delay between feeds (s)", bg=_C["card_bg"]).grid(
            row=2, column=0, sticky=W, pady=7)
        self._sett_delay_var = tk.StringVar(
            value=db.get_setting(_U,"feed_delay", "1.0"))
        ttk.Spinbox(gf, from_=0, to=10, increment=0.5,
                    textvariable=self._sett_delay_var, width=10).grid(
            row=2, column=1, padx=8, sticky=W)

        _lbl(gf, "Max pages per feed (≥1)", bg=_C["card_bg"]).grid(
            row=3, column=0, sticky=W, pady=7)
        self._sett_max_pages_var = tk.StringVar(
            value=db.get_setting(_U,"max_scrape_pages", "3"))
        ttk.Spinbox(gf, from_=1, to=20, increment=1,
                    textvariable=self._sett_max_pages_var, width=10).grid(
            row=3, column=1, padx=8, sticky=W)
        _lbl(gf, "(higher = finds older jobs; slower scrape)", font=_FT_SMALL,
             bg=_C["card_bg"], fg=_C["gray"]).grid(
            row=3, column=2, padx=(6, 0), sticky=W)

        _lbl(gf, "Max parallel bids (≥1)", bg=_C["card_bg"]).grid(
            row=4, column=0, sticky=W, pady=7)
        self._sett_max_bids_var = tk.StringVar(
            value=db.get_setting(_U,"max_parallel_bids", "10"))
        ttk.Spinbox(gf, from_=1, to=50, increment=1,
                    textvariable=self._sett_max_bids_var, width=10).grid(
            row=4, column=1, padx=8, sticky=W)
        _lbl(gf, "(concurrent browser windows; more = faster, more RAM)", font=_FT_SMALL,
             bg=_C["card_bg"], fg=_C["gray"]).grid(
            row=4, column=2, padx=(6, 0), sticky=W)

        _lbl(gf, "Bid Age Limit (hours)", bg=_C["card_bg"]).grid(
            row=5, column=0, sticky=W, pady=7)
        self._sett_bid_max_age_var = tk.StringVar(
            value=db.get_setting(_U,"bid_max_age_hours", "48"))
        ttk.Spinbox(gf, from_=1, to=720, increment=1,
                    textvariable=self._sett_bid_max_age_var, width=10).grid(
            row=5, column=1, padx=8, sticky=W)
        _lbl(gf, "(only bid on jobs posted within this many hours)", font=_FT_SMALL,
             bg=_C["card_bg"], fg=_C["gray"]).grid(
            row=5, column=2, padx=(6, 0), sticky=W)

        self._sett_show_browser_var = tk.BooleanVar(
            value=db.get_setting(_U,"show_browser", "1") == "1")
        ttk.Checkbutton(
            gf,
            text="Show browser for monitoring  (uncheck to run headlessly in background)",
            variable=self._sett_show_browser_var,
        ).grid(row=6, column=1, columnspan=2, sticky=W, padx=8, pady=7)

        _lbl(gf, "Bid Price % of Budget", bg=_C["card_bg"]).grid(
            row=7, column=0, sticky=W, pady=7)
        _pct_frame = tk.Frame(gf, bg=_C["card_bg"])
        _pct_frame.grid(row=7, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
        _pct_frame.columnconfigure(0, weight=1)
        try:
            _pct_init = max(0, min(100, int(db.get_setting(_U,"bid_price_pct", "0") or "0")))
        except ValueError:
            _pct_init = 0
        self._sett_bid_pct_var = tk.IntVar(value=_pct_init)
        tk.Scale(
            _pct_frame, from_=0, to=100, orient="horizontal",
            variable=self._sett_bid_pct_var,
            resolution=1, tickinterval=25,
            bg=_C["card_bg"], fg=_C["text"],
            troughcolor=_C["border"], highlightthickness=0,
            activebackground=_C["blue"],
        ).grid(row=0, column=0, sticky="ew")
        _lbl(_pct_frame, "  0 % = min budget  ·  100 % = max budget",
             font=_FT_SMALL, fg=_C["gray"], bg=_C["card_bg"]).grid(
            row=0, column=1, padx=(4, 0), sticky=W)

        _lbl(gf, "AI Model", bg=_C["card_bg"]).grid(
            row=8, column=0, sticky=W, pady=7)
        _AI_MODELS = [
            "gpt-4o-mini",
            "gpt-4o",
            "gpt-4.1-mini",
            "gpt-4.1",
            "gpt-4-turbo",
            "o1-mini",
            "o3-mini",
        ]
        self._sett_model_var = tk.StringVar(
            value=db.get_setting(_U,"openai_model", "gpt-4o-mini"))
        model_cb = ttk.Combobox(
            gf, textvariable=self._sett_model_var,
            values=_AI_MODELS, width=20,
        )
        model_cb.grid(row=8, column=1, padx=8, sticky=W)
        _lbl(gf, "(model used to generate bid proposals)", font=_FT_SMALL,
             bg=_C["card_bg"], fg=_C["gray"]).grid(
            row=8, column=2, padx=(6, 0), sticky=W)

        ttk.Button(gc, text="Save Settings", command=self._save_settings).pack(
            anchor=W, padx=16, pady=(4, 14))

        # ── Prompt library card ───────────────────────────────────────
        pc = _card(inner)
        pc.pack(fill=X, padx=24, pady=(0, 24))

        ph = tk.Frame(pc, bg=_C["card_bg"])
        ph.pack(fill=X, padx=16, pady=(14, 8))
        _lbl(ph, "Prompt Library", font=_FT_H2, bg=_C["card_bg"]).pack(side=LEFT)

        pt = tk.Frame(pc, bg=_C["card_bg"])
        pt.pack(fill=X, padx=16, pady=(0, 8))
        for lbl, cmd in [
            ("+ Add Prompt", lambda: self._open_prompt_dialog()),
            ("Edit",         lambda: self._open_prompt_dialog(edit=True)),
            ("Delete",       self._delete_selected_prompt),
        ]:
            ttk.Button(pt, text=lbl, command=cmd).pack(side=LEFT, padx=(0, 6))

        pf = tk.Frame(pc, bg=_C["card_bg"])
        pf.pack(fill=X, padx=16, pady=(0, 14))
        pf.columnconfigure(0, weight=1)
        pf.rowconfigure(0, weight=1)

        cols = ("id", "name", "preview")
        self._prompt_tree = ttk.Treeview(pf, columns=cols, show="headings", height=8)
        for col, lbl, w, stretch in [
            ("id",      "ID",       40, False),
            ("name",    "Name",    180, False),
            ("preview", "Content Preview", 700, True),
        ]:
            self._prompt_tree.heading(col, text=lbl)
            self._prompt_tree.column(col, width=w, stretch=stretch)
        self._prompt_tree.bind("<Double-1>", lambda _: self._open_prompt_dialog(edit=True))

        pvsb = ttk.Scrollbar(pf, orient="vertical",   command=self._prompt_tree.yview)
        phsb = ttk.Scrollbar(pf, orient="horizontal", command=self._prompt_tree.xview)
        self._prompt_tree.configure(yscrollcommand=pvsb.set, xscrollcommand=phsb.set)
        self._prompt_tree.grid(row=0, column=0, sticky="nsew")
        pvsb.grid(row=0, column=1, sticky="ns")
        phsb.grid(row=1, column=0, sticky="ew")

    def _refresh_settings(self) -> None:
        self._sett_oai_var.set(db.get_setting(_U,"openai_api_key"))
        self._sett_interval_var.set(db.get_setting(_U,"scrape_interval", "30"))
        self._sett_delay_var.set(db.get_setting(_U,"feed_delay", "1.0"))
        self._sett_max_pages_var.set(db.get_setting(_U,"max_scrape_pages", "3"))
        self._sett_max_bids_var.set(db.get_setting(_U,"max_parallel_bids", "10"))
        self._sett_bid_max_age_var.set(db.get_setting(_U,"bid_max_age_hours", "48"))
        self._sett_show_browser_var.set(db.get_setting(_U,"show_browser", "1") == "1")
        try:
            self._sett_bid_pct_var.set(
                max(0, min(100, int(db.get_setting(_U,"bid_price_pct", "0") or "0"))))
        except ValueError:
            self._sett_bid_pct_var.set(0)
        self._sett_model_var.set(db.get_setting(_U,"openai_model", "gpt-4o-mini"))
        self._refresh_prompts()

    def _save_settings(self) -> None:
        db.set_setting(_U,"openai_api_key", self._sett_oai_var.get().strip())
        try:
            db.set_setting(_U,"scrape_interval", str(max(10, int(self._sett_interval_var.get()))))
        except ValueError:
            pass
        try:
            db.set_setting(_U,"feed_delay", str(max(0.0, float(self._sett_delay_var.get()))))
        except ValueError:
            pass
        try:
            db.set_setting(_U,"max_scrape_pages", str(max(1, int(self._sett_max_pages_var.get()))))
        except ValueError:
            pass
        try:
            db.set_setting(_U,"max_parallel_bids", str(max(1, int(self._sett_max_bids_var.get()))))
        except ValueError:
            pass
        try:
            db.set_setting(_U,"bid_max_age_hours", str(max(1, int(self._sett_bid_max_age_var.get()))))
        except ValueError:
            pass
        db.set_setting(_U,"show_browser", "1" if self._sett_show_browser_var.get() else "0")
        db.set_setting(_U,"bid_price_pct", str(max(0, min(100, self._sett_bid_pct_var.get()))))
        model_val = self._sett_model_var.get().strip()
        if model_val:
            db.set_setting(_U,"openai_model", model_val)
        self._status_var.set("Settings saved.")

    def _refresh_prompts(self) -> None:
        self._prompt_tree.delete(*self._prompt_tree.get_children())
        for p in db.list_prompts(_U):
            preview = (p["content"] or "").replace("\n", " ")[:140]
            self._prompt_tree.insert("", END, iid=str(p["id"]),
                                     values=(p["id"], p["name"], preview))

    def _selected_prompt_id(self) -> int | None:
        sel = self._prompt_tree.selection()
        return int(sel[0]) if sel else None

    def _open_prompt_dialog(self, *, edit: bool = False) -> None:
        pid = self._selected_prompt_id() if edit else None
        if edit and pid is None:
            messagebox.showinfo("Edit Prompt", "Select a prompt first.")
            return
        prompt = db.get_prompt(_U,pid) if pid else None

        win = tk.Toplevel(self)
        win.title("Edit Prompt" if edit else "Add Prompt")
        win.geometry("640x460")
        win.resizable(True, True)
        win.grab_set()

        f = ttk.Frame(win, padding=(18, 14))
        f.pack(fill=BOTH, expand=True)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(1, weight=1)

        ttk.Label(f, text="Name").grid(row=0, column=0, sticky=W, pady=7)
        name_var = tk.StringVar(value=prompt["name"] if prompt else "")
        ttk.Entry(f, textvariable=name_var, width=44).grid(
            row=0, column=1, padx=8, sticky="ew")

        ttk.Label(f, text="Content").grid(row=1, column=0, sticky="nw", pady=7)
        content_txt = ScrolledText(f, height=18, wrap=tk.WORD, font=_FT_BODY)
        content_txt.grid(row=1, column=1, padx=8, sticky="nsew", pady=7)
        if prompt:
            content_txt.insert("1.0", prompt["content"])

        def _save() -> None:
            name    = name_var.get().strip()
            content = content_txt.get("1.0", "end-1c").strip()
            if not name:
                messagebox.showerror("Prompt", "Name is required.", parent=win)
                return
            if not content:
                messagebox.showerror("Prompt", "Content is required.", parent=win)
                return
            if pid:
                db.update_prompt(_U,pid, name, content)
            else:
                db.add_prompt(_U,name, content)
            win.destroy()
            self._refresh_prompts()

        br = ttk.Frame(f)
        br.grid(row=2, column=0, columnspan=2, sticky=W, pady=(10, 0))
        ttk.Button(br, text="Cancel", command=win.destroy).pack(side=LEFT, padx=(0, 8))
        ttk.Button(br, text="Save", command=_save).pack(side=LEFT)

    def _delete_selected_prompt(self) -> None:
        pid = self._selected_prompt_id()
        if pid is None:
            messagebox.showinfo("Delete Prompt", "Select a prompt first.")
            return
        if not messagebox.askyesno(
            "Delete Prompt",
            "Delete this prompt?\nAccounts using it will lose the assignment.",
        ):
            return
        db.delete_prompt(_U,pid)
        self._refresh_prompts()

    # ──────────────────────────────────────────────────────────────────
    # Log page
    # ──────────────────────────────────────────────────────────────────

    def _build_log_page(self) -> None:
        page = self._pages["Log"]
        _hr = tk.Frame(page, bg=_C["page_bg"])
        _hr.pack(fill=X, padx=24, pady=(22, 14))
        tk.Frame(_hr, bg=_C["blue"], width=4).pack(side=LEFT, fill=Y, padx=(0, 10))
        _lbl(_hr, "Activity Log", font=_FT_H1, fg=_C["text"]).pack(side=LEFT, anchor=W)

        toolbar = tk.Frame(page, bg=_C["page_bg"])
        toolbar.pack(fill=X, padx=24, pady=(0, 10))
        ttk.Button(toolbar, text="Refresh",
                   command=self._refresh_log).pack(side=LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Clear All Logs",
                   command=self._clear_log).pack(side=LEFT)

        card = _card(page)
        card.pack(fill=BOTH, expand=True, padx=24, pady=(0, 22))

        cols = ("time", "account", "level", "message")
        _log_frame = tk.Frame(card, bg=_C["card_bg"])
        _log_frame.pack(fill=BOTH, expand=True, padx=8, pady=8)
        _log_frame.columnconfigure(0, weight=1)
        _log_frame.rowconfigure(0, weight=1)
        self._log_tree = ttk.Treeview(_log_frame, columns=cols, show="headings", height=24)
        for col, lbl, w, stretch in [
            ("time",    "Time",    160, False),
            ("account", "Account",  95, False),
            ("level",   "Level",    68, False),
            ("message", "Message", 800, True),
        ]:
            self._log_tree.heading(col, text=lbl)
            self._log_tree.column(col, width=w, stretch=stretch)
        self._log_tree.tag_configure("error",   foreground=_C["red"])
        self._log_tree.tag_configure("success", foreground=_C["green"])
        self._log_tree.tag_configure("warning", foreground=_C["yellow"])

        vsb = ttk.Scrollbar(_log_frame, orient="vertical",   command=self._log_tree.yview)
        hsb = ttk.Scrollbar(_log_frame, orient="horizontal", command=self._log_tree.xview)
        self._log_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._log_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

    def _refresh_log(self) -> None:
        logs = db.list_logs(_U,limit=300)
        self._log_tree.delete(*self._log_tree.get_children())
        for entry in logs:
            ts   = (entry.get("created_at") or "")[:19]
            acc  = entry.get("account_name") or "System"
            lvl  = (entry.get("level") or "info")
            self._log_tree.insert(
                "", END,
                values=(ts, acc, lvl.upper(), entry.get("message") or ""),
                tags=(lvl,),
            )

    def _clear_log(self) -> None:
        if not messagebox.askyesno("Clear Logs", "Delete all log entries?"):
            return
        db.clear_logs(_U)
        self._refresh_log()

    # ──────────────────────────────────────────────────────────────────
    # Bot
    # ──────────────────────────────────────────────────────────────────

    def _toggle_bot(self) -> None:
        if self._bot_running:
            self._stop_bot()
        else:
            self._start_bot()

    def _start_bot(self) -> None:
        if not db.get_setting(_U,"openai_api_key"):
            messagebox.showerror(
                "Bot",
                "OpenAI API key is required.\n"
                "Go to Settings and save your key first.",
            )
            return

        accounts = [a for a in db.list_accounts(_U) if a["enabled"]]
        if not accounts:
            messagebox.showerror(
                "Bot",
                "No enabled accounts found.\n"
                "Add at least one account in the Accounts page.",
            )
            return

        has_prompt = any(a.get("prompt_content") for a in accounts)
        if not has_prompt:
            if not messagebox.askyesno(
                "Bot — No prompt assigned",
                "None of the enabled accounts has a prompt assigned.\n"
                "The bot will skip bid generation until prompts are assigned.\n\n"
                "Start anyway?",
            ):
                return

        # Seed seen IDs so old listings are not reprocessed
        existing = load_jobs_jsonl(DATA_FILE) if DATA_FILE.exists() else []
        self._seen_job_ids = {
            str(j["job_offer_id"]) for j in existing if j.get("job_offer_id") is not None
        }
        self._bot_processed = 0

        # Flush any stale items left from a previous session
        while not self._bid_queue.empty():
            try:
                self._bid_queue.get_nowait()
            except queue.Empty:
                break
        self._bid_workers.clear()

        try:
            max_workers = max(1, int(db.get_setting(_U,"max_parallel_bids", "10")))
        except ValueError:
            max_workers = 10

        self._bot_running = True
        self._bot_stop_event.clear()
        self._btn_bot.config(text="■  Stop Bot",
                             bg=_C["red"], activebackground=_C["red_dk"])
        self._hdr_bot_lbl.config(text="● Running", fg=_C["green"])
        self._status_dot.config(fg=_C["green"])
        self._status_var.set("Bot started — monitoring for new CrowdWorks jobs…")
        db.add_log(_U,"Bot started.", level="info")

        # Start persistent worker threads — they pull from _bid_queue in priority
        # order (newest project first) until the stop event is set.
        for i in range(max_workers):
            t = threading.Thread(
                target=self._bid_worker,
                name=f"bid-worker-{i}",
                daemon=True,
            )
            t.start()
            self._bid_workers.append(t)

        self._bot_thread = threading.Thread(target=self._bot_loop, daemon=True)
        self._bot_thread.start()

    def _stop_bot(self) -> None:
        self._bot_running = False
        self._bot_stop_event.set()
        self._btn_bot.config(text="▶  Start Bot",
                             bg=_C["green"], activebackground=_C["green_dk"])
        self._hdr_bot_lbl.config(text="", fg=_C["gray"])
        self._status_dot.config(fg=_C["gray"])
        # Drain the queue so workers unblock quickly
        while not self._bid_queue.empty():
            try:
                self._bid_queue.get_nowait()
                self._bid_queue.task_done()
            except queue.Empty:
                break
        msg = f"Bot stopped — {self._bot_processed} proposal(s) submitted this session."
        self._status_var.set(msg)
        db.add_log(_U,f"Bot stopped. {self._bot_processed} bids submitted this session.",
                   level="info")

    def _bid_worker(self) -> None:
        """Persistent worker thread.

        Pulls (priority, seq, job, acc, openai_key) items from _bid_queue and
        processes them in priority order — lowest value first, which means the
        newest project (most-negative timestamp) is always handled first.

        When a new scrape cycle adds projects with a more recent timestamp, they
        jump ahead of anything already waiting in the queue.
        """
        while not self._bot_stop_event.is_set():
            try:
                _pri, _seq, job, acc, openai_key = self._bid_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._process_job_for_account(job, acc, openai_key)
            except Exception as exc:
                jid = job.get("job_offer_id", "?")
                db.add_log(_U,
                    f"[{acc['name']}] Unexpected error in bid worker "
                    f"for job {jid}: {exc}",
                    level="error",
                )
            finally:
                self._bid_queue.task_done()
            self._ui(self._refresh_jobs_quiet)

    def _bot_loop(self) -> None:
        """Background daemon thread: scrape feeds → detect new jobs → enqueue bids."""
        # Bid on existing eligible jobs before the scrape loop starts so the
        # bot does not ignore projects that were already in the list.
        self._enqueue_existing_jobs()

        while not self._bot_stop_event.is_set():
            # ── Read settings ──────────────────────────────────────────────
            try:
                interval = max(10, int(db.get_setting(_U,"scrape_interval", "30")))
            except ValueError:
                interval = 30
            try:
                delay = max(0.0, float(db.get_setting(_U,"feed_delay", "1.0")))
            except ValueError:
                delay = 1.0
            try:
                max_pages = max(1, int(db.get_setting(_U,"max_scrape_pages", "3")))
            except ValueError:
                max_pages = 3
            try:
                max_bids = max(1, int(db.get_setting(_U,"max_parallel_bids", "10")))
            except ValueError:
                max_bids = 10

            openai_key = db.get_setting(_U,"openai_api_key")
            accounts   = [a for a in db.list_accounts(_U) if a["enabled"]]

            # ── Scrape feeds (parallel — one thread per feed) ──────────────
            self._ui(lambda p=max_pages: self._status_var.set(
                f"Bot: scraping {len(NEW_POSTING_FEEDS)} feeds in parallel "
                f"(up to {p} page(s) each)…"
            ))
            try:
                jobs, _ = scrape_new_postings_feeds(
                    delay_s=delay,
                    timeout=90.0,
                    include_raw=False,
                    max_pages=max_pages,
                    stop_at_ids=set(self._seen_job_ids),
                )
                new_jobs      = append_jsonl_unique(DATA_FILE, jobs)
                biddable_jobs: list[dict] = []   # NEW PROJECTS only (filled below)
                self._ui(self._refresh_jobs_quiet)   # show newly written jobs immediately

                if new_jobs:
                    # ── Classify scraped jobs as "new" or "old backlog" ───────
                    # _new_session_ids is the single source of truth that drives
                    # BOTH the "★ NEW PROJECT" badge in the Jobs table AND the
                    # bid task list.  Only recently posted jobs enter this set;
                    # old backlog entries (first run / cleared JSONL) are stored
                    # for deduplication but never badged or bid on.
                    try:
                        bid_max_age = max(
                            1, int(db.get_setting(_U,"bid_max_age_hours", "48"))
                        )
                    except ValueError:
                        bid_max_age = 48

                    biddable_jobs: list[dict] = []
                    skipped_old  = 0

                    for job in new_jobs:
                        jid   = str(job.get("job_offer_id"))
                        title = (job.get("title") or "")[:80]

                        # Determine recency from last_released_at
                        is_recent = True
                        raw_ts = job.get("last_released_at") or ""
                        if raw_ts:
                            try:
                                dt = datetime.fromisoformat(str(raw_ts))
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                age_h = (
                                    datetime.now(tz=timezone.utc) - dt
                                ).total_seconds() / 3600
                                if age_h > bid_max_age:
                                    is_recent = False
                            except (ValueError, TypeError):
                                pass   # unparseable timestamp → assume recent

                        with self._bid_lock:
                            self._seen_job_ids.add(jid)   # always deduplicate
                            if is_recent:
                                # Only recent jobs get the badge & bid
                                self._new_session_ids.add(jid)

                        if is_recent:
                            biddable_jobs.append(job)
                            db.add_log(_U,
                                f"New project detected: [{jid}] {title}",
                                level="info",
                            )
                            self._notify_system(
                                "New CrowdWorks project", f"ID {jid}: {title}"
                            )
                            self._ui(self.bell)
                        else:
                            skipped_old += 1
                            db.add_log(_U,
                                f"Old backlog job ignored: [{jid}] {title} "
                                f"(posted >{bid_max_age}h ago)",
                                level="info",
                            )

                    # Refresh immediately so badges + new rows are visible now
                    self._ui(self._refresh_jobs_quiet)

                    if skipped_old:
                        self._ui(lambda s=skipped_old, a=bid_max_age:
                            self._status_var.set(
                                f"Bot: {s} old job(s) skipped "
                                f"(posted >{a}h ago — not eligible for bidding)"
                            )
                        )

                    # ── Enqueue NEW PROJECT rows into the priority bid queue ───
                    # Jobs are ordered newest-first (top of table = highest
                    # priority).  If a later scrape cycle finds an even newer
                    # project, its more-negative priority value causes it to
                    # jump ahead of anything already waiting.
                    n_enqueued = 0
                    for job in biddable_jobs:           # already sorted newest→oldest
                        raw_ts = job.get("last_released_at") or ""
                        try:
                            dt = datetime.fromisoformat(str(raw_ts))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            priority = -dt.timestamp()  # negative → newest = highest priority
                        except (ValueError, TypeError):
                            priority = -time.time()     # unknown age → treat as now

                        for acc in accounts:
                            if self._bot_stop_event.is_set():
                                break
                            with self._bid_lock:
                                seq = self._bid_seq
                                self._bid_seq += 1
                            self._bid_queue.put(
                                (priority, seq, job, acc, openai_key)
                            )
                            n_enqueued += 1

                    if n_enqueued:
                        self._ui(
                            lambda n=n_enqueued, nb=len(biddable_jobs):
                            self._status_var.set(
                                f"Bot: {nb} NEW PROJECT(s) enqueued — "
                                f"{n} bid task(s) added to priority queue "
                                f"(newest first)…"
                            )
                        )
                        db.add_log(_U,
                            f"{n_enqueued} bid task(s) enqueued "
                            f"({len(biddable_jobs)} new project(s)).",
                            level="info",
                        )

                # ── Refresh UI & status ────────────────────────────────────
                self._ui(self._refresh_jobs_quiet)
                n_scraped = len(new_jobs)
                n_new     = len(biddable_jobs)
                msg = (
                    f"Bot: {n_scraped} scraped · {n_new} NEW PROJECT(s) · "
                    f"{self._bot_processed} auto-bid total · "
                    f"next scrape in {interval}s"
                )
                self._ui(lambda m=msg: self._status_var.set(m))

            except Exception as exc:
                err = f"Scrape error: {exc}"
                db.add_log(_U,err, level="error")
                self._ui(lambda m=err: self._status_var.set(f"Bot: {m}"))

            self._bot_stop_event.wait(timeout=interval)

    def _process_job_for_account(
        self, job: dict, acc: dict, openai_key: str
    ) -> None:
        """Generate and submit a bid for one (job, account) pair."""
        acc_id   = acc["id"]
        acc_name = acc["name"]
        jid      = str(job.get("job_offer_id"))
        title    = (job.get("title") or "")[:80]

        if db.has_bid(_U,acc_id, jid):
            return

        prompt = acc.get("prompt_content") or ""
        if not prompt:
            db.add_log(_U,
                f"[{acc_name}] No prompt assigned — skipping job {jid}.",
                level="warning", account_id=acc_id,
            )
            return
        if not openai_key:
            db.add_log(_U,"OpenAI key missing — cannot generate proposal.",
                       level="error", account_id=acc_id)
            return

        # Launch browser: it will visit the job-details page, analyze with OpenAI,
        # then fill and submit the proposal form.
        self._ui(lambda a=acc_name, j=jid: self._status_var.set(
            f"Bot [{a}]: opening browser for job {j} (details → AI → bid)…"
        ))
        db.add_log(_U,
            f"[{acc_name}] Launching browser for job {jid}: {title}…",
            level="info", account_id=acc_id,
        )

        def _browser_event(msg: str) -> None:
            """Forward each browser log line to the DB and status bar."""
            db.add_log(_U,
                f"[{acc_name}] browser: {msg}",
                level="info", account_id=acc_id,
            )
            self._ui(lambda m=f"[{acc_name}] {msg}": self._status_var.set(m))

        # headless = True when "Show browser" is OFF (background mode).
        # Default "1" keeps the original behaviour (visible browser) when the
        # user has never explicitly saved the setting.
        headless   = db.get_setting(_U,"show_browser", "1") != "1"
        model_slug = db.get_setting(_U,"openai_model", "gpt-4o-mini") or "gpt-4o-mini"
        try:
            bid_price_pct = max(0, min(100, int(db.get_setting(_U,"bid_price_pct", "0") or "0")))
        except ValueError:
            bid_price_pct = 0
        bid_result = browser_bid.submit_bid_via_browser(
            acc["session_id"],
            int(jid),
            job,
            openai_key,
            extra_prompt=prompt,
            model=model_slug,
            headless=headless,
            bid_price_pct=bid_price_pct,
            on_event=_browser_event,
        )

        if bid_result.get("ok"):
            url = bid_result.get("url", "")
            db.record_bid(_U,jid, job_title=title, result_url=url, account_id=acc_id)
            db.update_account(_U,acc_id, status="active")
            with self._bid_lock:
                self._bot_processed += 1
                n_done = self._bot_processed
            db.add_log(_U,
                f"[{acc_name}] Bid submitted for job {jid}: {url}",
                level="success", account_id=acc_id,
            )
            self._ui(lambda a=acc_name, j=jid, n=n_done: self._status_var.set(
                f"Bot [{a}]: bid submitted for job {j} (total: {n})"
            ))
        elif bid_result.get("already_bid"):
            # Job was already bid on (e.g. DB was deleted and bot retried).
            # Record as 'already_bid' so the bot never attempts this again.
            err = bid_result.get("error", "Already bid on this project.")
            db.record_bid(_U,
                jid, job_title=title, result_url="",
                account_id=acc_id, status="already_bid",
            )
            db.add_log(_U,
                f"[{acc_name}] Job {jid} already bid on — marked accordingly.",
                level="info", account_id=acc_id,
            )
            self._ui(lambda a=acc_name, j=jid: self._status_var.set(
                f"Bot [{a}]: job {j} was already bid on — skipped."
            ))
        else:
            err = bid_result.get("error", "Unknown browser bid error.")
            db.record_bid(_U,
                jid, job_title=title, result_url="",
                account_id=acc_id, status="failed", error_msg=err,
            )
            db.add_log(_U,
                f"[{acc_name}] Bid failed for job {jid}: {err}",
                level="error", account_id=acc_id,
            )
            self._ui(lambda m=err: self._status_var.set(f"Bot bid error: {m}"))
            if any(kw in err.lower() for kw in ("session", "login", "expired", "redirect")):
                db.update_account(_U,acc_id, status="expired")

    def _enqueue_existing_jobs(self) -> None:
        """Enqueue pre-existing JSONL jobs that are still within bid_max_age_hours
        and have not yet been bid on.  Called once from _bot_loop on startup so
        that accounts are not limited to only *newly discovered* jobs.
        """
        existing = load_jobs_jsonl(DATA_FILE) if DATA_FILE.exists() else []
        if not existing:
            return

        try:
            bid_max_age = max(1, int(db.get_setting(_U,"bid_max_age_hours", "48")))
        except ValueError:
            bid_max_age = 48

        openai_key = db.get_setting(_U,"openai_api_key")
        accounts   = [a for a in db.list_accounts(_U) if a["enabled"]]
        if not accounts or not openai_key:
            return

        now = datetime.now(tz=timezone.utc)
        n_enqueued = 0

        for job in existing:
            if self._bot_stop_event.is_set():
                break

            jid = str(job.get("job_offer_id") or "")
            if not jid or jid == "None":
                continue

            # Age gate
            priority  = -now.timestamp()
            eligible  = True
            raw_ts    = job.get("last_released_at") or ""
            if raw_ts:
                try:
                    dt = datetime.fromisoformat(str(raw_ts))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age_h = (now - dt).total_seconds() / 3600
                    if age_h > bid_max_age:
                        eligible = False
                    else:
                        priority = -dt.timestamp()
                except (ValueError, TypeError):
                    pass  # unparseable → treat as eligible

            if not eligible:
                continue

            # Enqueue per-account where a bid hasn't been placed yet
            for acc in accounts:
                if self._bot_stop_event.is_set():
                    break
                if db.has_bid(_U,acc["id"], jid):
                    continue

                with self._bid_lock:
                    self._new_session_ids.add(jid)
                    seq = self._bid_seq
                    self._bid_seq += 1

                self._bid_queue.put((priority, seq, job, acc, openai_key))
                n_enqueued += 1

        if n_enqueued:
            db.add_log(_U,
                f"Startup: {n_enqueued} bid task(s) enqueued from existing "
                f"eligible jobs (posted within {bid_max_age}h).",
                level="info",
            )
            self._ui(self._refresh_jobs_quiet)

    def _refresh_jobs_quiet(self) -> None:
        """Reload jobs from disk and re-apply filters without resetting dropdowns."""
        self.jobs = load_jobs_jsonl(DATA_FILE) if DATA_FILE.exists() else []
        self._apply_job_filters()

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    def _ui(self, fn) -> None:
        """Schedule a callable on the Tk main thread (thread-safe)."""
        self.after(0, fn)

    def _notify_system(self, title: str, message: str) -> None:
        """Non-blocking Windows balloon notification via PowerShell."""
        try:
            t = title.replace('"', "").replace("'", "")
            m = message.replace('"', "").replace("'", "")
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                "$n.Visible=$true;"
                f'$n.ShowBalloonTip(8000,"{t}","{m}",'
                "[System.Windows.Forms.ToolTipIcon]::Info);"
                "Start-Sleep 9;$n.Dispose()"
            )
            subprocess.Popen(
                ["powershell", "-WindowStyle", "Hidden", "-NoProfile", "-Command", ps],
                **_POPEN_KW,
            )
        except Exception:
            pass

    def _on_close(self) -> None:
        if self._bot_running:
            self._stop_bot()
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
        self.destroy()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    app = CrowdWorksBot()
    app.mainloop()


if __name__ == "__main__":
    main()
