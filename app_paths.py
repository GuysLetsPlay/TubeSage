"""Resolve bundled resources and writable per-user application data."""
from __future__ import annotations

import os
import sys
from pathlib import Path


RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TubeSage"
else:
    # Keep existing source checkouts self-contained and compatible with their
    # current settings, download engine, and knowledge files.
    DATA_DIR = RESOURCE_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)
