"""
Resolves the application data directory at runtime.

When running as a PyInstaller bundle (sys.frozen is True) the data files
(crowdworks_bot.db, new_postings.jsonl, …) must live next to the .exe so they
persist across restarts — not inside the temporary _MEIPASS extraction folder
that PyInstaller re-creates on every launch.

When running as a plain Python script the data directory is the folder that
contains this file (i.e. the project root), preserving the original behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # Bundled by PyInstaller: place user data next to the executable
    APP_DIR: Path = Path(sys.executable).resolve().parent
else:
    # Normal script execution: project root (same folder as this file)
    APP_DIR = Path(__file__).resolve().parent
