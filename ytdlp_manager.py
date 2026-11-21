"""Manage yt-dlp.exe and ffmpeg for TubeSage.

All functions are synchronous and safe to call from worker threads.
Binaries live in <project>/bin so the app is self-contained.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

from app_paths import DATA_DIR

BIN_DIR = DATA_DIR / "bin"

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

UA = {"User-Agent": "TubeSage/1.0 (+https://localhost)"}


def ytdlp_path() -> Path:
    return BIN_DIR / "yt-dlp.exe"


def ytdlp_available() -> bool:
    return ytdlp_path().exists()


def ffmpeg_dir() -> Path:
    # gyan.dev zip extracts to ffmpeg-<version>-essentials_build/
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    for child in BIN_DIR.glob("ffmpeg-*-essentials_build"):
        return child
    return BIN_DIR / "ffmpeg"


def ffmpeg_bin() -> Path:
    d = ffmpeg_dir()
    exe = d / "bin" / "ffmpeg.exe"
    return exe if exe.exists() else d / "ffmpeg.exe"


def ffmpeg_available() -> bool:
    return ffmpeg_bin().exists()


def _download(url: str, dest: Path, progress_cb=None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress_cb and total:
                progress_cb(done, total)


def install_ytdlp(progress_cb=None) -> Path:
    """Download yt-dlp.exe if missing. Returns its path."""
    dest = ytdlp_path()
    if dest.exists():
        return dest
    _download(YTDLP_URL, dest.with_suffix(".part"), progress_cb)
    os.replace(dest.with_suffix(".part"), dest)
    return dest


def update_ytdlp(progress_cb=None) -> tuple[bool, str]:
    """Self-update yt-dlp.exe via its built-in updater."""
    dest = ytdlp_path()
    if not dest.exists():
        install_ytdlp(progress_cb)
    proc = subprocess.run(
        [str(dest), "-U"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    ok = proc.returncode == 0
    return ok, (proc.stdout + proc.stderr).strip()


def install_ffmpeg(progress_cb=None) -> Path:
    """Download + extract ffmpeg essentials build. Returns ffmpeg.exe path."""
    if ffmpeg_available():
        return ffmpeg_bin()
    zip_path = BIN_DIR / "ffmpeg.zip"
    _download(FFMPEG_URL, zip_path.with_suffix(".part"), progress_cb)
    os.replace(zip_path.with_suffix(".part"), zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(BIN_DIR)
    zip_path.unlink(missing_ok=True)
    # flatten: move inner bin contents next to the extracted folder
    d = ffmpeg_dir()
    inner_bin = d / "bin"
    if not (d / "bin").exists() and inner_bin.exists() is False:
        pass
    return ffmpeg_bin()


def ytdlp_version() -> str:
    if not ytdlp_available():
        return "not installed"
    try:
        proc = subprocess.run(
            [str(ytdlp_path()), "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return proc.stdout.strip() or "unknown"
    except Exception as exc:  # pragma: no cover
        return f"error: {exc}"


def ensure_ffmpeg_flag() -> list[str]:
    """yt-dlp args pointing at our bundled ffmpeg, if present.
    yt-dlp wants the binary itself or its direct containing folder."""
    if ffmpeg_available():
        return ["--ffmpeg-location", str(ffmpeg_bin().parent)]
    return []


def find_js_runtime() -> str | None:
    """yt-dlp (2025+) uses a JS runtime for YouTube extraction.
    Only deno is enabled by default; if Node.js is present, use it."""
    import shutil
    return shutil.which("node")


def ensure_js_flag() -> list[str]:
    node = find_js_runtime()
    return ["--js-runtimes", f"node:{node}"] if node else []


def find_system_ytdlp() -> str | None:
    found = shutil.which("yt-dlp")
    return found
