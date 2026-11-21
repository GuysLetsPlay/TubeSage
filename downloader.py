"""Download queue engine: runs yt-dlp.exe as a subprocess and parses progress.

One DownloadWorker per job. All UI updates happen through Qt signals.
"""
from __future__ import annotations

import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

import ytdlp_manager as mgr

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = Path.home() / "Videos" / "TubeSage"

# [download]  42.3% of    3.42MiB at  913.55KiB/s ETA 00:02
PROG_RE = re.compile(
    r"\[download\]\s+(?P<pct>[\d.]+)%\s+of\s+(?P<size>~?\s*[\d.]+\w+)"
    r"(?:\s+at\s+(?P<speed>[\d.]+\w+/s|Unknown))?(?:\s+ETA\s+(?P<eta>[\d:]+))?")

# yt-dlp format presets: (label, format_spec, extra args, audio-only)
PRESETS = [
    ("Best quality",     "bv*+ba/b", [], False),
    ("1080p",            "bv*[height<=1080]+ba/b[height<=1080]", [], False),
    ("720p",             "bv*[height<=720]+ba/b[height<=720]", [], False),
    ("480p",             "bv*[height<=480]+ba/b[height<=480]", [], False),
    ("Audio only (MP3)", "ba", ["-x", "--audio-format", "mp3"], True),
    ("Audio only (M4A)", "ba", ["-x", "--audio-format", "m4a"], True),
    ("Audio only (Opus)","ba", ["-x", "--audio-format", "opus"], True),
]


@dataclass
class Job:
    url: str
    preset: str = "Best quality"
    output_dir: str = str(DEFAULT_OUTPUT)
    playlist: bool = False
    subtitles: bool = False
    embed_thumbnail: bool = True
    embed_metadata: bool = True
    sponsorblock: bool = False
    cookies_file: str = ""
    extra_args: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    title: str = ""
    state: str = "queued"      # queued|downloading|finished|error|cancelled
    pct: float = 0.0
    speed: str = ""
    eta: str = ""
    log_tail: list[str] = field(default_factory=list)
    error_text: str = ""
    command: str = ""
    started_at: float = 0.0
    filepath: str = ""
    # -- dream features -------------------------------------------------
    watch_while: bool = False   # open the growing .part file in a media player
    resume: bool = False        # reuse partial files when the download failed
    watch_opened: bool = False  # player already auto-opened once
    part_file: str = ""         # first "[download] Destination:" (the video stream)

    def build_args(self) -> list[str]:
        fmt, extra, audio = next(
            (f, e, a) for label, f, e, a in PRESETS if label == self.preset)
        if self.watch_while and not audio:
            fmt = progressive_first(fmt)
        args = [
            "--newline", "--progress",
            "-f", fmt,
            "-o", str(Path(self.output_dir) / "%(title)s [%(id)s].%(ext)s"),
            *mgr.ensure_ffmpeg_flag(),
            *mgr.ensure_js_flag(),
            *extra,
        ]
        if self.resume:
            # reuse partial .part files / fragment caches from a failed run
            args += ["--continue", "--live-from-start",
                     "--file-access-retries", "30"]
        if not self.playlist:
            args.append("--no-playlist")
        if self.subtitles:
            args += ["--write-subs", "--sub-langs", "en.*,de.*", "--embed-subs"]
        if self.embed_thumbnail:
            args += ["--embed-thumbnail"]
        if self.embed_metadata:
            args += ["--embed-metadata"]
        if self.sponsorblock:
            args += ["--sponsorblock-remove", "default"]
        if self.cookies_file:
            args += ["--cookies", self.cookies_file]
        if self.extra_args:
            args += self.extra_args.split()
        args.append(self.url)
        return args


def progressive_first(fmt: str) -> str:
    """Rewrite a format spec so single-file (progressive) formats are preferred.

    A progressive format (video+audio in one file) grows as ONE .part file,
    which can be opened in a media player while the download is still running.
    Merged formats (bv*+ba) only produce the final file at the very end.
    """
    m = re.search(r"height<=(\d+)", fmt)
    if m:
        h = m.group(1)
        return f"b[height<={h}]/bv*[height<={h}]+ba/b[height<={h}]"
    if fmt == "bv*+ba/b":
        return "b/bv*+ba/b"
    return fmt


def finalize_stale_part(job: Job) -> bool:
    """Recover a completed-but-locked .part file before a resume retry.

    Symptom: the download finished byte-wise but the final rename failed
    ("Unable to rename file ... WinError 32", often antivirus); every later
    resume then dies with "HTTP Error 416: Requested range not satisfiable"
    because yt-dlp cannot tell the file is already complete. Renaming it
    ourselves lets the next retry say "has already been downloaded" and go
    straight to merging. Only triggered by those exact error signatures, so
    genuinely partial files are never touched.
    """
    if not (job.resume and job.part_file):
        return False
    et = job.error_text or ""
    if ("Unable to rename file" not in et
            and "Requested range not satisfiable" not in et):
        return False
    part = Path(job.part_file + ".part")
    final = Path(job.part_file)
    try:
        if part.exists() and not final.exists():
            try:
                part.replace(final)
            except OSError:
                # the .part may still be share-locked by another process;
                # copy our own complete copy in via a temp file instead
                import shutil
                tmp = final.with_suffix(final.suffix + ".tubesage_tmp")
                shutil.copy2(part, tmp)
                tmp.replace(final)
            return True
    except OSError:
        pass
    return False


class MetaWorker(QThread):
    """Fetch title/uploader for a URL."""
    got_meta = Signal(dict)
    meta_error = Signal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self):
        exe = mgr.ytdlp_path()
        if not exe.exists():
            self.meta_error.emit("yt-dlp.exe not installed yet (see Settings).")
            return
        cmd = [str(exe), "--dump-single-json", "--no-playlist",
               *mgr.ensure_ffmpeg_flag(), *mgr.ensure_js_flag(), self.url]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as exc:
            self.meta_error.emit(str(exc))
            return
        if proc.returncode != 0:
            self.meta_error.emit((proc.stderr or proc.stdout).strip()[-1500:])
            return
        try:
            import json
            info = json.loads(proc.stdout)
            self.got_meta.emit({
                "title": info.get("title", ""),
                "uploader": info.get("uploader", ""),
                "duration": info.get("duration", 0),
                "thumbnail": info.get("thumbnail", ""),
                "webpage_url": info.get("webpage_url", self.url),
            })
        except Exception as exc:
            self.meta_error.emit(f"Could not parse metadata: {exc}")



class DownloadWorker(QThread):
    """Runs one yt-dlp job; emits progress and completion."""
    progress = Signal(str, float, str, str)     # id, pct, speed, eta
    state_changed = Signal(str, str)            # id, state
    log_line = Signal(str, str)                 # id, line
    finished_ok = Signal(str, str)              # id, filepath
    failed = Signal(str, str)                   # id, error text

    def __init__(self, job: Job, parent=None):
        super().__init__(parent)
        self.job = job
        self._proc: subprocess.Popen | None = None
        self._cancel = False

    def cancel(self):
        self._cancel = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass

    def run(self):
        job = self.job
        exe = mgr.ytdlp_path()
        if not exe.exists():
            job.state = "error"
            job.error_text = "yt-dlp.exe is not installed (Settings tab)."
            self.failed.emit(job.id, job.error_text)
            return
        args = [str(exe)] + job.build_args()
        job.command = " ".join(args)
        job.state = "downloading"
        job.started_at = time.time()
        if finalize_stale_part(job):
            self.log_line.emit(
                job.id,
                "[TubeSage] Recovered a completed partial file — continuing.")
        self.state_changed.emit(job.id, job.state)
        Path(job.output_dir).mkdir(parents=True, exist_ok=True)
        try:
            self._proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as exc:
            job.state, job.error_text = "error", str(exc)
            self.failed.emit(job.id, job.error_text)
            return
        last_path = ""
        for line in self._proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if not line:
                continue
            job.log_tail.append(line)
            del job.log_tail[:-200]
            self.log_line.emit(job.id, line)
            m = PROG_RE.search(line)
            if m:
                job.pct = float(m.group("pct"))
                job.speed = m.group("speed") or ""
                job.eta = m.group("eta") or ""
                self.progress.emit(job.id, job.pct, job.speed, job.eta)
            if line.startswith("[download] Destination:"):
                last_path = line.split("Destination:", 1)[-1].strip()
                # remember the first destination (the video stream) so the
                # "watch while downloading" feature can find its .part file
                if not job.part_file:
                    job.part_file = last_path
            # the FINAL file: post-processing steps rename/re-mux it, and
            # yt-dlp prints the result in several different formats
            if " Destination: " in line and not line.startswith("[download]"):
                last_path = line.split(" Destination: ", 1)[-1].strip()
            q = re.search(r'"([^"]+)"', line)
            if q and ("Merging formats into" in line
                      or "Adding metadata to" in line
                      or "Adding thumbnail to" in line
                      or "Remuxing" in line):
                last_path = q.group(1)
            m2 = re.search(r"\[download\] (.+) has already been downloaded", line)
            if m2:
                last_path = m2.group(1).strip()
            if self._cancel:
                self._proc.kill()
                job.state = "cancelled"
                self.state_changed.emit(job.id, job.state)
                return
        code = self._proc.wait()
        if code == 0:
            job.state = "finished"
            job.pct = 100.0
            job.filepath = last_path
            self.progress.emit(job.id, 100.0, "", "")
            self.state_changed.emit(job.id, job.state)
            self.finished_ok.emit(job.id, last_path)
        else:
            job.state = "error"
            job.error_text = "\n".join(job.log_tail[-30:]) or f"exit code {code}"
            self.state_changed.emit(job.id, job.state)
            self.failed.emit(job.id, job.error_text)

