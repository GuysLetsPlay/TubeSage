"""Twitch archive discovery and scheduled six-hour VOD downloads."""
from __future__ import annotations

import json
import re
import subprocess
import time
from subprocess import TimeoutExpired
from datetime import date, datetime, time as clock_time
import math
from pathlib import Path

from PySide6.QtCore import QThread, Signal

import ytdlp_manager as mgr

SEGMENT_SECONDS = 6 * 60 * 60
CHECKPOINT_SECONDS = 15 * 60
YOUTUBE_TITLE_LIMIT = 100


def clean_filename_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "VOD")
    return re.sub(r"\s+", " ", value).strip(" .") or "VOD"


def safe_name(value: str) -> str:
    return clean_filename_component(value)[:100] or "VOD"


def segment_title_stem(vod: dict, segment: int) -> str:
    """Build a YouTube-ready filename title, preserving both ID markers."""
    prefix = f"E{segment + 1} - "
    numeric_vod_id = str(vod.get("id", "")).removeprefix("v")
    suffix = f" VOD ID {numeric_vod_id}"
    if vod.get("stream_id"):
        suffix += f" Stream ID {clean_filename_component(str(vod['stream_id']))}"
    title = clean_filename_component(str(vod.get("title", "VOD")))
    available = YOUTUBE_TITLE_LIMIT - len(prefix) - len(suffix)
    if len(title) > available:
        keep = max(0, available - 3)
        front = (keep + 1) // 2
        back = keep - front
        title = title[:front] + "..." + (title[-back:] if back else "")
    return f"{prefix}{title}{suffix}"


def format_timestamp(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def vod_folder(root: str, vod: dict) -> Path:
    return Path(root) / f"{safe_name(vod.get('title', 'VOD'))} [{vod.get('id', '')}]"


def segment_files(folder: Path, vod: dict, segment: int) -> list[Path]:
    """Return existing final or partial files for one segment, including legacy names."""
    vod_id = str(vod.get("id", ""))
    prefixes = (f"E{segment + 1} - ", f"Episode {segment + 1} - ")
    numeric_id = vod_id.removeprefix("v")
    markers = (f"[{vod_id}]", f"[VOD ID {numeric_id}]", f"VOD ID {numeric_id}")
    try:
        return [path for path in folder.iterdir()
                if path.is_file() and path.name.startswith(prefixes)
                and any(marker in path.name for marker in markers)]
    except OSError:
        return []


def checkpoint_workspace(folder: Path, segment: int) -> Path:
    return folder / ".tubesage-checkpoints" / f"segment-{segment + 1:03d}"


def completed_checkpoints(folder: Path, segment: int) -> int:
    workspace = checkpoint_workspace(folder, segment)
    try:
        return sum(1 for path in workspace.glob("checkpoint-*.mp4")
                   if path.is_file() and path.stat().st_size > 0)
    except OSError:
        return 0


def segment_checkpoint_count(vod: dict, segment: int) -> int:
    duration = int(vod.get("duration") or SEGMENT_SECONDS)
    start = segment * SEGMENT_SECONDS
    finish = min(duration, start + SEGMENT_SECONDS)
    return max(1, math.ceil((finish - start) / CHECKPOINT_SECONDS))


def normalize_segment_names(folder: Path, vod: dict) -> None:
    """Normalize existing files to the 100-character segment naming format.

    Partial files are renamed as well, using the same basename yt-dlp will use
    on resume. Callers must ensure yt-dlp is not currently writing this VOD.
    """
    for segment in range(max(1, (int(vod.get("duration") or SEGMENT_SECONDS)
                              + SEGMENT_SECONDS - 1) // SEGMENT_SECONDS)):
        for path in segment_files(folder, vod, segment):
            suffix = ""
            base = path.name
            for ending in (".part", ".ytdl"):
                if base.endswith(ending):
                    suffix = ending + suffix
                    base = base[:-len(ending)]
            media_ext = Path(base).suffix
            if not media_ext:
                continue
            target = path.with_name(f"{segment_title_stem(vod, segment)}{media_ext}{suffix}")
            if target == path:
                continue
            if target.exists():
                continue
            try:
                path.rename(target)
            except OSError:
                pass


def migrate_legacy_segment_names(folder: Path, vod: dict) -> None:
    """Rename old Episode N outputs so yt-dlp can resume under the E<N> template."""
    vod_id = str(vod.get("id", ""))
    try:
        paths = list(folder.iterdir())
    except OSError:
        return
    for path in paths:
        if not path.is_file():
            continue
        marker = re.escape(f"[{vod_id}]")
        if not re.search(marker, path.name):
            continue
        match = re.match(r"^(?:Episode |E)(\d+) - (.+)$", path.name)
        if not match:
            continue
        suffix = f"VOD ID {vod_id.removeprefix('v')}"
        body = re.sub(marker + r"(?=\.)", suffix, match.group(2))
        target = path.with_name(f"E{match.group(1)} - {body}")
        if not target.exists():
            try:
                path.rename(target)
            except OSError:
                pass


def parse_date(raw: str) -> date | None:
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str) and raw.isdigit() and len(raw) >= 10:
        try:
            return datetime.fromtimestamp(int(raw)).date()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(raw[:10])
        except (TypeError, ValueError):
            return None


class VodSearchWorker(QThread):
    found = Signal(list)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, streamer: str, start_date: str, end_date: str = "", parent=None):
        super().__init__(parent)
        self.streamer = streamer.strip().lstrip("@").lower()
        self.start_date = date.fromisoformat(start_date)
        self.end_date = date.fromisoformat(end_date) if end_date else None
        self._cancel = False
        self._proc: subprocess.Popen | None = None

    def cancel(self):
        self._cancel = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass

    def run(self):
        if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", self.streamer):
            self.failed.emit("Enter a Twitch channel login, such as trimax.")
            return
        if self.end_date and self.end_date < self.start_date:
            self.failed.emit("The end date must be on or after the start date.")
            return
        exe = mgr.ytdlp_path()
        if not exe.exists():
            self.failed.emit("yt-dlp is not installed yet. Finish setup in TubeSage first.")
            return
        url = f"https://www.twitch.tv/{self.streamer}/videos?filter=archives&sort=time"
        date_span = ((self.end_date or date.today()) - self.start_date).days + 1
        playlist_limit = min(1000, max(100, math.ceil(date_span * 4)))
        self.progress.emit(f"Checking Twitch archives for {self.streamer}…")
        # Do not use --flat-playlist here. Twitch's flat entries omit upload_date,
        # so filtering them by date drops every VOD. Expanding playlist entries
        # makes yt-dlp resolve the dates and durations needed by the archive UI.
        args = [str(exe), "--dump-single-json", "--yes-playlist",
                "--playlist-end", str(playlist_limit), "--no-warnings", url]
        try:
            self._proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True,
                                          encoding="utf-8", errors="replace",
                                          creationflags=subprocess.CREATE_NO_WINDOW)
            while True:
                try:
                    output, _ = self._proc.communicate(timeout=0.2)
                    break
                except TimeoutExpired:
                    if self._cancel:
                        self._proc.kill()
            proc = type("Result", (), {"returncode": self._proc.returncode,
                                        "stdout": output or "", "stderr": ""})()
            self._proc = None
        except Exception as exc:
            self.failed.emit(f"Twitch search failed: {exc}")
            return
        if proc.returncode:
            self.failed.emit((proc.stderr or proc.stdout).strip()[-2500:]
                             or "Twitch did not return an archive list.")
            return
        if self._cancel:
            return
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            self.failed.emit(f"Could not read Twitch's VOD list: {exc}")
            return
        entries = result.get("entries") or [result]
        output = []
        for entry in entries:
            if not entry or not entry.get("id"):
                continue
            uploaded = parse_date(entry.get("upload_date") or entry.get("release_timestamp")
                                   or entry.get("timestamp"))
            if uploaded is None or uploaded < self.start_date:
                continue
            if self.end_date and uploaded > self.end_date:
                continue
            vid = str(entry["id"])
            output.append({
                "id": vid,
                "title": entry.get("title") or f"Twitch VOD {vid}",
                "streamer": self.streamer,
                "date": uploaded.isoformat(),
                "duration": int(entry.get("duration") or 0),
                "url": entry.get("webpage_url") or f"https://www.twitch.tv/videos/{vid}",
                "status": "found",
                "segments_done": [],
                "error": "",
                "selected": False,
            })
            if entry.get("live_status") == "is_live":
                output[-1]["status"] = "live"
        output.sort(key=lambda item: (item["date"], item["id"]))
        self.found.emit(output)


def schedule_open(start: str, end: str, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    try:
        begins = clock_time.fromisoformat(start)
        finishes = clock_time.fromisoformat(end)
    except (TypeError, ValueError):
        return True
    current = now.time().replace(tzinfo=None)
    if begins == finishes:
        return True
    if begins < finishes:
        return begins <= current < finishes
    return current >= begins or current < finishes


def directory_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def stop_process_tree(process: subprocess.Popen | None) -> None:
    if not process or process.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


class ArchiveDownloadWorker(QThread):
    changed = Signal(str, str, str)  # VOD id, state, message
    segment_started = Signal(str, int, int)  # VOD id, zero-based segment, total segments
    segment_done = Signal(str, int)
    progress = Signal(str)
    stopped = Signal()

    def __init__(self, vods: list[dict], output_root: str, start_time: str,
                 end_time: str, max_gb: float, speed_limit_kbs: int = 0, parent=None):
        super().__init__(parent)
        self.vods = vods
        self.output_root = Path(output_root)
        self.start_time = start_time
        self.end_time = end_time
        self.max_bytes = max(0, int(max_gb * 1024**3))
        self.speed_limit_kbs = max(0, int(speed_limit_kbs))
        self._cancel = False
        self._pause_requested = False
        self._proc: subprocess.Popen | None = None

    def cancel(self):
        self._cancel = True
        stop_process_tree(self._proc)

    def pause(self):
        self._pause_requested = True
        stop_process_tree(self._proc)

    def _wait_for_window(self) -> bool:
        while (not self._cancel and not self._pause_requested
               and not schedule_open(self.start_time, self.end_time)):
            self.progress.emit(f"Download window closed; resumes at {self.start_time}.")
            for _ in range(20):
                if self._cancel or self._pause_requested:
                    return False
                time.sleep(3)
        return not self._cancel and not self._pause_requested

    def _run_process(self, args: list[str], cwd: Path | None = None):
        """Run yt-dlp/ffmpeg and stop promptly at a window boundary."""
        failed = ""
        window_closed = False
        try:
            self._proc = subprocess.Popen(
                args, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW)
            if self._cancel or self._pause_requested:
                stop_process_tree(self._proc)
            last_size_check = time.monotonic()
            while (self._proc.poll() is None and not self._cancel
                   and not self._pause_requested):
                now = time.monotonic()
                if (self.max_bytes and now - last_size_check >= 30
                        and directory_size(self.output_root) >= self.max_bytes):
                    stop_process_tree(self._proc)
                    self._proc.wait()
                    failed = "Storage allowance reached"
                    break
                if now - last_size_check >= 30:
                    last_size_check = now
                if not schedule_open(self.start_time, self.end_time):
                    stop_process_tree(self._proc)
                    self._proc.wait()
                    window_closed = True
                    break
                line = self._proc.stdout.readline() if self._proc.stdout else ""
                if line and line.strip():
                    self.progress.emit(line.strip()[-220:])
            return self._proc.wait(), failed, window_closed
        except Exception as exc:
            return 1, str(exc), window_closed
        finally:
            self._proc = None

    def _download_checkpoint(self, exe: Path, vod: dict, segment: int,
                             checkpoint: int, start: int, finish: int,
                             workspace: Path):
        output = workspace / f"checkpoint-{checkpoint:03d}.mp4"
        if output.is_file() and output.stat().st_size:
            return 0, "", False
        output.unlink(missing_ok=True)
        # The interrupted ffmpeg output is not a resumable MP4; preserve earlier
        # checkpoints and redo only this short time slice.
        for suffix in (".mp4.part", ".mp4.ytdl", ".part", ".ytdl"):
            (workspace / f"checkpoint-{checkpoint:03d}{suffix}").unlink(missing_ok=True)
        template = str(workspace / f"checkpoint-{checkpoint:03d}.%(ext)s")
        args = [str(exe), "--newline", "--continue", "--no-playlist",
                "--download-sections", f"*{format_timestamp(start)}-{format_timestamp(finish)}",
                "--force-keyframes-at-cuts", "--remux-video", "mp4",
                "-f", "bv*+ba/b", "-o", template,
                *mgr.ensure_ffmpeg_flag(), *mgr.ensure_js_flag(), vod["url"]]
        if self.speed_limit_kbs:
            args[1:1] = ["--limit-rate", str(self.speed_limit_kbs * 1000)]
        self.progress.emit(f"{vod['title']} — E{segment + 1}, checkpoint {checkpoint + 1}")
        return self._run_process(args)

    def _assemble_segment(self, vod: dict, segment: int, chunks: list[Path],
                          folder: Path, workspace: Path):
        concat_list = workspace / "concat.txt"
        concat_list.write_text("".join(f"file '{chunk.name}'\n" for chunk in chunks),
                               encoding="utf-8")
        staging = workspace / "assembled.mp4"
        staging.unlink(missing_ok=True)
        args = [str(mgr.ffmpeg_bin()), "-y", "-hide_banner", "-f", "concat",
                "-safe", "1", "-i", concat_list.name, "-map", "0", "-c", "copy",
                "-movflags", "+faststart", staging.name]
        code, failed, window_closed = self._run_process(args, cwd=workspace)
        if self._cancel or self._pause_requested or window_closed:
            staging.unlink(missing_ok=True)
            return False, failed, window_closed
        if code:
            return False, failed or f"Could not assemble E{segment + 1} (ffmpeg exit {code}).", False
        target = folder / f"{segment_title_stem(vod, segment)}.mp4"
        if target.exists():
            staging.unlink(missing_ok=True)
            return False, f"Completed output already exists: {target.name}", False
        staging.replace(target)
        concat_list.unlink(missing_ok=True)
        for chunk in chunks:
            chunk.unlink(missing_ok=True)
        (workspace / "legacy-uncheckpointed.mp4.part").unlink(missing_ok=True)
        try:
            workspace.rmdir()
            workspace.parent.rmdir()
        except OSError:
            pass
        return True, "", False

    def _preserve_legacy_partial(self, folder: Path, vod: dict, segment: int,
                                 workspace: Path) -> None:
        """Keep an old monolithic partial; it cannot be safely split/resumed."""
        legacy = workspace / "legacy-uncheckpointed.mp4.part"
        if legacy.exists():
            return
        for path in segment_files(folder, vod, segment):
            if path.name.endswith((".part", ".ytdl")):
                try:
                    path.rename(legacy)
                    self.progress.emit(
                        f"Saved the old E{segment + 1} partial; checkpointed progress starts now.")
                    return
                except OSError:
                    return

    def run(self):
        exe = mgr.ytdlp_path()
        if not exe.exists():
            self.progress.emit("yt-dlp is not installed. Install it in Settings.")
            self.stopped.emit()
            return
        if not mgr.ffmpeg_available():
            self.progress.emit("Install ffmpeg in Settings to download VOD segments.")
            self.stopped.emit()
            return
        self.output_root.mkdir(parents=True, exist_ok=True)
        for vod in self.vods:
            if self._cancel or self._pause_requested:
                break
            if self.max_bytes and directory_size(self.output_root) >= self.max_bytes:
                for candidate in self.vods:
                    self.changed.emit(candidate["id"], "storage-full", "Storage allowance reached")
                self.progress.emit("Storage allowance reached. Increase it or free space to continue.")
                break
            folder = vod_folder(str(self.output_root), vod)
            folder.mkdir(parents=True, exist_ok=True)
            duration = int(vod.get("duration") or SEGMENT_SECONDS)
            count = max(1, (duration + SEGMENT_SECONDS - 1) // SEGMENT_SECONDS)
            self.changed.emit(vod["id"], "downloading", "Downloading six-hour segments")
            failed = ""
            segment_order = list(range(count))
            priority_segment = vod.get("priority_segment")
            if isinstance(priority_segment, int) and 0 <= priority_segment < count:
                segment_order.remove(priority_segment)
                segment_order.insert(0, priority_segment)
            for segment in segment_order:
                if self._cancel or not self._wait_for_window():
                    break
                if segment in vod.get("segments_done", []) or segment in vod.get("segments_uploaded", []):
                    continue
                if self.max_bytes and directory_size(self.output_root) >= self.max_bytes:
                    failed = "Storage allowance reached"
                    break
                start = segment * SEGMENT_SECONDS
                finish = min(duration, start + SEGMENT_SECONDS)
                final_output = folder / f"{segment_title_stem(vod, segment)}.mp4"
                if final_output.is_file() and final_output.stat().st_size > 0:
                    if segment not in vod.setdefault("segments_done", []):
                        vod["segments_done"].append(segment)
                    self.segment_done.emit(str(vod["id"]), segment)
                    continue
                workspace = checkpoint_workspace(folder, segment)
                workspace.mkdir(parents=True, exist_ok=True)
                self._preserve_legacy_partial(folder, vod, segment, workspace)
                checkpoint_count = segment_checkpoint_count(vod, segment)
                chunks = [workspace / f"checkpoint-{index:03d}.mp4"
                          for index in range(checkpoint_count)]
                self.progress.emit(f"{vod['title']} - segment {segment + 1}/{count}")
                self.segment_started.emit(str(vod["id"]), segment, count)
                window_closed = False
                segment_assembled = False
                for checkpoint, chunk_path in enumerate(chunks):
                    if self._cancel or not self._wait_for_window():
                        break
                    if chunk_path.is_file() and chunk_path.stat().st_size > 0:
                        continue
                    chunk_start = start + checkpoint * CHECKPOINT_SECONDS
                    chunk_finish = min(finish, chunk_start + CHECKPOINT_SECONDS)
                    code, checkpoint_error, window_closed = self._download_checkpoint(
                        exe, vod, segment, checkpoint, chunk_start, chunk_finish, workspace)
                    if checkpoint_error == "Storage allowance reached":
                        failed = checkpoint_error
                        break
                    if self._cancel or self._pause_requested or window_closed:
                        break
                    if code:
                        failed = checkpoint_error or (
                            f"yt-dlp stopped at E{segment + 1} checkpoint {checkpoint + 1} "
                            f"(exit {code})")
                        break
                    if self.max_bytes and directory_size(self.output_root) >= self.max_bytes:
                        failed = "Storage allowance reached"
                        break
                if (not failed and not self._cancel and not self._pause_requested
                        and not window_closed
                        and all(path.is_file() and path.stat().st_size > 0 for path in chunks)):
                    assembled, assemble_error, window_closed = self._assemble_segment(
                        vod, segment, chunks, folder, workspace)
                    segment_assembled = assembled
                    if not assembled and assemble_error:
                        failed = assemble_error
                if self._cancel or self._pause_requested:
                    break
                if window_closed or not schedule_open(self.start_time, self.end_time):
                    break
                if failed == "Storage allowance reached":
                    for candidate in self.vods:
                        self.changed.emit(candidate["id"], "storage-full", "Storage allowance reached")
                    break
                if failed:
                    break
                if not segment_assembled:
                    break
                vod.setdefault("segments_done", []).append(segment)
                self.segment_done.emit(str(vod["id"]), segment)
            if self._cancel:
                self.changed.emit(vod["id"], "paused", "Paused; incomplete checkpoint will resume")
                break
            if self._pause_requested:
                self.changed.emit(vod["id"], "paused", "Paused by you; resume to continue")
                break
            if failed:
                state = "storage-full" if failed == "Storage allowance reached" else "error"
                self.changed.emit(vod["id"], state, failed)
                if state == "storage-full":
                    break
            elif len(set(vod.get("segments_done", []))) >= count:
                if all(index in vod.get("segments_uploaded", []) for index in range(count)):
                    self.changed.emit(vod["id"], "done", "All segments uploaded")
                else:
                    self.changed.emit(vod["id"], "downloaded", "Ready for manual YouTube Studio upload")
            else:
                self.changed.emit(vod["id"], "paused", "Download window closed; checkpoint will resume")
        self.stopped.emit()
