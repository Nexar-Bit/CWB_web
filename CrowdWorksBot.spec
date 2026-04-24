# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for CrowdWorks Bot desktop application.
#
# Build:
#   pyinstaller CrowdWorksBot.spec --clean --noconfirm
#
# Output: dist\CrowdWorksBot\CrowdWorksBot.exe  (one-directory bundle)
#
# Notes
# -----
# • Playwright browser binaries live in %APPDATA%\ms-playwright and are NOT
#   bundled here.  Run `playwright install chromium` once after deploying.
# • User data (crowdworks_bot.db, new_postings.jsonl, …) is stored next to
#   the .exe thanks to _appdir.py — it is never placed inside the bundle.

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# ── Collect all Playwright Python files (no browser binaries) ────────────────
pw_datas, pw_binaries, pw_hiddenimports = collect_all("playwright")

a = Analysis(
    ["desktop_app.py"],
    pathex=[],
    binaries=pw_binaries,
    datas=[
        # Playwright internal data (driver, etc.)
        *pw_datas,
    ],
    hiddenimports=[
        # Playwright
        *pw_hiddenimports,
        *collect_submodules("playwright"),
        # Tray icon
        "pystray",
        "pystray._win32",
        # Imaging
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageFont",
        "PIL.ImageTk",
        # Networking
        "requests",
        "urllib3",
        # App modules
        "_appdir",
        "db",
        "cword_auth",
        "crowdworks_jobs",
        "proposal_draft",
        "bid_tracking",
        "browser_bid",
        "browser_login",
        "desktop_session",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Web server — not needed for the desktop app
        "fastapi",
        "uvicorn",
        "jinja2",
        "starlette",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CrowdWorksBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no console window — pure GUI app
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icon.ico",        # CW icon
    version_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="CrowdWorksBot",
)
