"""TubeSage — a friendly Windows GUI for yt-dlp, oriented at YTDLnis.

Tabs: Download | Queue | History | Settings
Error Doctor: hybrid error helper (auto Telegram+GitHub search, optional
deep search via a visible CLI agent, feedback-based knowledge base).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from PySide6.QtCore import (QByteArray, QObject, QPoint, QSize, Qt, QThread,
                            QTimer, QUrl, Signal, QDate)
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPen, QPixmap,
                           QPolygon)
from PySide6.QtNetwork import (QNetworkAccessManager, QNetworkReply,
                               QNetworkRequest)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QStackedWidget, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QDateEdit,
    QHeaderView, QDoubleSpinBox, QTimeEdit, QSpinBox)
from PySide6.QtWidgets import QScrollArea

from app_paths import DATA_DIR
import downloader
import error_doctor
import knowledge
import ytdlp_manager as mgr
import twitch_archive
from downloader import DownloadWorker, Job, MetaWorker
from twitch_archive import ArchiveDownloadWorker, VodSearchWorker

# file types the "watch now" folder-scan considers playable
MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".mp3",
              ".m4a", ".opus", ".ogg", ".wav", ".flac"}
from error_doctor import extract_signature
from agent_launcher import detect_cli, launch_deep_search

SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "output_dir": str(downloader.DEFAULT_OUTPUT),
    "cli_agent": "auto",
    "tg_group": error_doctor.DEFAULT_GROUP,
    "tg_topic": error_doctor.DEFAULT_TOPIC,
    "preset": "Best quality",
    "download_speed_limit_kbs": 0,
    "history": [],          # [{title, filepath, when}]
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return s


def save_settings(s: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False),
                             encoding="utf-8")


SETTINGS = load_settings()


# ----------------------------------------------------------- thumbnails ----
THUMB_CACHE = DATA_DIR / "thumb_cache"


def video_id_from_url(url: str) -> str:
    """Extract the video id from a YouTube-ish URL ('[id]' naming source)."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{6,})", url or "")
    return m.group(1) if m else ""


def video_id_from_path(path: str) -> str:
    """Extract the video id yt-dlp appended as '[id]' to a filename."""
    m = re.search(r"\[([\w-]{6,})\]", Path(path or "").name)
    return m.group(1) if m else ""


def placeholder_pixmap(w: int = 320, h: int = 180) -> QPixmap:
    """Dark frame with a play triangle — shown until a thumbnail loads."""
    pm = QPixmap(w, h)
    pm.fill(QColor("#1e1e1e"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor("#3a3a3a"), 2))
    p.drawRoundedRect(4, 4, w - 8, h - 8, 10, 10)
    s = h // 4
    x, y = w // 2 - s // 2, h // 2 - s // 2
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#5a5a5a"))
    p.drawPolygon(QPolygon([QPoint(x, y), QPoint(x, y + s),
                            QPoint(x + s, y + s // 2)]))
    p.end()
    return pm


class ThumbCache(QObject):
    """Video thumbnails for the grid views.

    Loads https://i.ytimg.com/vi/<id>/mqdefault.jpg asynchronously via
    QtNetwork, with an in-memory dict and an on-disk cache folder so
    previously seen videos render instantly and offline.
    Non-YouTube ids (or offline machines) get a placeholder pixmap.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mem: dict[str, QPixmap] = {}
        self._ph: QPixmap | None = None
        self.nam = QNetworkAccessManager(self)
        try:
            THUMB_CACHE.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def placeholder(self) -> QPixmap:
        if self._ph is None:
            self._ph = placeholder_pixmap()
        return self._ph

    def get(self, vid: str, cb) -> None:
        """Call cb(QPixmap) with the thumbnail for vid (async for fetches)."""
        if not vid:
            cb(self.placeholder())
            return
        pm = self.mem.get(vid)
        if pm is not None:
            cb(pm)
            return
        f = THUMB_CACHE / f"{vid}.jpg"
        if f.exists():
            pm = QPixmap(str(f))
            if not pm.isNull():
                self.mem[vid] = pm
                cb(pm)
                return
            try:
                f.unlink()
            except OSError:
                pass
        reply = self.nam.get(QNetworkRequest(
            QUrl(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")))
        reply.finished.connect(lambda _=False, r=reply: self._done(r, vid, cb))

    def _done(self, reply: QNetworkReply, vid: str, cb) -> None:
        ok = reply.error() == QNetworkReply.NetworkError.NoError
        data = bytes(reply.readAll())
        reply.deleteLater()
        pm = QPixmap()
        if ok and data:
            pm.loadFromData(data)
        if not pm.isNull():
            try:
                (THUMB_CACHE / f"{vid}.jpg").write_bytes(data)
            except OSError:
                pass
        else:
            pm = self.placeholder()
        self.mem[vid] = pm
        cb(pm)


# ------------------------------------------------------------ bootstrap ----
class BootstrapWorker(QThread):
    """First-run: download yt-dlp.exe (and optionally ffmpeg)."""
    step = Signal(str)
    done = Signal(bool, str)

    def __init__(self, with_ffmpeg: bool = False, parent=None):
        super().__init__(parent)
        self.with_ffmpeg = with_ffmpeg

    def run(self):
        try:
            self.step.emit("Downloading yt-dlp.exe …")
            mgr.install_ytdlp()
            self.step.emit(f"yt-dlp ready ({mgr.ytdlp_version()})")
            if self.with_ffmpeg:
                self.step.emit("Downloading ffmpeg (≈90 MB) …")
                mgr.install_ffmpeg()
                self.step.emit("ffmpeg ready")
            self.done.emit(True, "Setup complete.")
        except Exception as exc:
            self.done.emit(False, f"Setup failed: {exc}")


class AutoSearchWorker(QThread):
    """Runs the Telegram+GitHub auto search off the UI thread."""
    progress = Signal(str)
    finished_search = Signal(object)   # error_doctor.DoctorReport

    def __init__(self, error_text: str, group: str, topic: str, parent=None):
        super().__init__(parent)
        self.error_text = error_text
        self.group = group
        self.topic = topic

    def run(self):
        try:
            error_doctor.DEFAULT_GROUP = self.group
            error_doctor.DEFAULT_TOPIC = self.topic
            report = error_doctor.run_auto_search(
                self.error_text,
                progress_cb=lambda msg: self.progress.emit(msg))
        except Exception as exc:
            report = error_doctor.DoctorReport(
                error_text=self.error_text,
                signature=extract_signature(self.error_text),
                results=[error_doctor.SearchResult(
                    "github", "Search crashed", str(exc))])
        self.finished_search.emit(report)



# ------------------------------------------------------- error doctor ----
class ErrorDoctorDialog(QDialog):
    """Hybrid error helper: KB -> auto search -> CLI agent -> feedback."""

    def __init__(self, error_text: str, yt_command: str, parent=None):
        super().__init__(parent)
        self.error_text = error_text
        self.yt_command = yt_command
        self.signature = extract_signature(error_text)
        self.report: error_doctor.DoctorReport | None = None
        self.worker: AutoSearchWorker | None = None
        self.current_entry: dict | None = None

        self.setWindowTitle("TubeSage Error Doctor")
        self.resize(860, 640)
        lay = QVBoxLayout(self)

        head = QLabel("🩺 Error Doctor — find out what went wrong and how to fix it")
        head.setStyleSheet("font-size: 16px; font-weight: bold;")
        lay.addWidget(head)

        err_box = QPlainTextEdit()
        err_box.setPlainText(error_text[-2500:])
        err_box.setReadOnly(True)
        err_box.setMaximumHeight(110)
        err_box.setFont(QFont("Consolas", 9))
        lay.addWidget(QLabel("Error output:"))
        lay.addWidget(err_box)

        # KB matches
        self.kb_list = QListWidget()
        self.kb_list.setMaximumHeight(150)
        lay.addWidget(QLabel("Known solutions (community-confirmed first):"))
        lay.addWidget(self.kb_list)
        self.kb_list.itemDoubleClicked.connect(self._show_kb_solution)

        # search results
        self.results_list = QListWidget()
        self.results_list.itemDoubleClicked.connect(self._open_result)
        lay.addWidget(QLabel("Telegram (YTDLNIS group) + yt-dlp GitHub results — double-click to open:"))
        lay.addWidget(self.results_list)

        self.status = QLabel("Ready.")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        self.btn_search = QPushButton("🔍 Find a fix (Telegram + GitHub)")
        self.btn_search.clicked.connect(self.run_auto_search)
        self.btn_deep = QPushButton("🤖 Deep Search (opens AI CLI)")
        self.btn_deep.clicked.connect(self.deep_search)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.reject)
        btns.addWidget(self.btn_search)
        btns.addWidget(self.btn_deep)
        btns.addStretch(1)
        btns.addWidget(btn_close)
        lay.addLayout(btns)

        # feedback buttons for the currently shown solution
        fb_row = QHBoxLayout()
        self.btn_worked = QPushButton("✅ This fixed it")
        self.btn_failed = QPushButton("❌ Didn't help")
        self.btn_worked.clicked.connect(lambda: self._feedback(True))
        self.btn_failed.clicked.connect(lambda: self._feedback(False))
        fb_row.addWidget(self.btn_worked)
        fb_row.addWidget(self.btn_failed)
        fb_row.addStretch(1)
        lay.addLayout(fb_row)

        # poll for solutions written by the CLI agent
        self.poll = QTimer(self)
        self.poll.timeout.connect(self._import_pending)
        self.poll.start(3000)

        self._load_kb()
        self.run_auto_search()   # hybrid: auto search always runs first


    # ---------------------------------------------------------- KB -----
    def _load_kb(self):
        self.kb_list.clear()
        matches = knowledge.find_matches(self.error_text, self.signature)
        self.current_entry = None
        if not matches:
            QListWidgetItem("No known solution for this error yet.", self.kb_list)
            return
        for m in matches[:5]:
            badge = {"confirmed": "✅ confirmed", "rejected": "❌ rejected",
                     "new": "new"}.get(m["status"], m["status"])
            QListWidgetItem(
                f"[{badge}] (match {int(m['score']*100)}%, worked {m['worked']}×) "
                f"{m['signature'][:90]}  —  double-click to view",
                self.kb_list)
        self.current_entry = matches[0]
        self._show_kb_solution(self.kb_list.item(0))

    def _show_kb_solution(self, item):
        matches = knowledge.find_matches(self.error_text, self.signature)
        if not matches:
            return
        idx = self.kb_list.row(item)
        if 0 <= idx < len(matches):
            self.current_entry = matches[idx]
            self.status.setText(
                f"💡 Solution (source: {matches[idx]['source']}):\n\n"
                + matches[idx]["solution_md"][:2000])

    def _feedback(self, worked: bool):
        if not self.current_entry:
            QMessageBox.information(self, "Error Doctor",
                                    "No solution to rate yet.")
            return
        if worked:
            knowledge.mark_worked(self.current_entry["id"])
            self.status.setText("🎉 Great! Saved as a confirmed fix — it will "
                                "be offered first next time.")
        else:
            knowledge.mark_failed(self.current_entry["id"])
            self.status.setText("Noted. Try the Deep Search for a thorough "
                                "step-by-step investigation.")

    # -------------------------------------------------- auto search -----
    def run_auto_search(self):
        self.btn_search.setEnabled(False)
        self.results_list.clear()
        self.status.setText("Searching YTDLNIS Telegram group and yt-dlp GitHub issues …")
        self.worker = AutoSearchWorker(self.error_text,
                                       SETTINGS.get("tg_group", "YTDLNIS"),
                                       SETTINGS.get("tg_topic", "general"))
        self.worker.progress.connect(self.status.setText)
        self.worker.finished_search.connect(self._search_done)
        self.worker.start()

    def _search_done(self, report):
        self.report = report
        self.btn_search.setEnabled(True)
        self.results_list.clear()
        if report.queries:
            QListWidgetItem(f"Search queries used: {' | '.join(report.queries)}",
                            self.results_list)
        for r in report.results[:40]:
            icon = "💬" if r.source == "telegram" else "🐙"
            item = QListWidgetItem(f"{icon} {r.title}\n    {r.body[:180]}")
            item.setData(Qt.UserRole, r.url)
            self.results_list.addItem(item)
        self.status.setText(
            "Double-click a result to open it, or use Deep Search for a "
            "guided, thorough answer. If the AI agent writes a fix, it "
            "appears in 'Known solutions' automatically.")

    def _open_result(self, item):
        url = item.data(Qt.UserRole)
        if url:
            webbrowser.open(url)

    # -------------------------------------------------- deep search -----
    def deep_search(self):
        ok, msg = launch_deep_search(
            self.error_text, self.yt_command, self.signature,
            SETTINGS.get("cli_agent", "auto"))
        if ok:
            self.status.setText("🤖 " + msg)
        else:
            QMessageBox.warning(self, "Deep Search", msg)

    def _import_pending(self):
        try:
            imported = knowledge.import_pending()
        except Exception:
            return
        if imported:
            self._load_kb()
            self.status.setText("🤖 The AI agent finished and its solution was "
                                "imported — see 'Known solutions' above. Try the "
                                "fix, then rate it with ✅/❌.")



# ---------------------------------------------------------- main window ----
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TubeSage — download anything with yt-dlp")
        self.resize(900, 620)
        self.jobs: dict[str, Job] = {}
        self.workers: dict[str, DownloadWorker] = {}
        self.rows: dict[str, QTreeWidgetItem] = {}
        self.grid_items: dict[str, QListWidgetItem] = {}
        self.thumbs = ThumbCache(self)
        self.bootstrap: BootstrapWorker | None = None
        self.meta_worker: MetaWorker | None = None
        self.archive_search: VodSearchWorker | None = None
        self.archive_worker: ArchiveDownloadWorker | None = None
        self._resume_after_pause = False
        self.archive_timer = QTimer(self)
        self.archive_timer.setInterval(30_000)
        self.archive_timer.timeout.connect(self._archive_tick)
        self.archive_scan_timer = QTimer(self)
        self.archive_scan_timer.setInterval(60 * 60 * 1000)
        self.archive_scan_timer.timeout.connect(self._archive_search)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_download_tab(), "⬇ Download")
        self.tabs.addTab(self._build_queue_tab(), "📥 Queue")
        self.tabs.addTab(self._build_history_tab(), "🕘 History")
        self.tabs.addTab(self._build_settings_tab(), "⚙ Settings")
        self._restore_archive_controls()
        self._refresh_archive_table()
        self.archive_timer.start()
        if SETTINGS.get("twitch_archive", {}).get("auto_refresh", True):
            self.archive_scan_timer.start()
            if SETTINGS.get("twitch_archive", {}).get("streamer"):
                QTimer.singleShot(1500, self._archive_search)

        self.statusBar().showMessage("Ready.")
        if not mgr.ytdlp_available():
            self._bootstrap(False)
        elif not mgr.ffmpeg_available():
            self.statusBar().showMessage(
                "Tip: install ffmpeg (Settings) for merging high-quality video+audio.")

    # ------------------------------------------------------ download ----
    def _build_download_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Paste a video / playlist / audio URL here …")
        btn_fetch = QPushButton("Preview")
        btn_fetch.clicked.connect(self.fetch_meta)
        btn_paste = QPushButton("Paste")
        btn_paste.clicked.connect(lambda: self.url_edit.paste())
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(btn_paste)
        url_row.addWidget(btn_fetch)
        lay.addLayout(url_row)

        self.meta_label = QLabel("")
        self.meta_label.setWordWrap(True)
        lay.addWidget(self.meta_label)

        form = QFormLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems([p[0] for p in downloader.PRESETS])
        self.preset_combo.setCurrentText(SETTINGS.get("preset", "Best quality"))
        form.addRow("Quality / format:", self.preset_combo)

        self.download_speed_limit = QSpinBox()
        self.download_speed_limit.setRange(0, 100000)
        self.download_speed_limit.setSingleStep(100)
        self.download_speed_limit.setSuffix(" KB/s (0 = unlimited)")
        self.download_speed_limit.setValue(int(SETTINGS.get("download_speed_limit_kbs", 0)))
        self.download_speed_limit.setToolTip(
            "Applies to TubeSage downloads, including the Twitch archive. "
            "Pause and resume an active archive to apply a changed limit.")
        self.download_speed_limit.valueChanged.connect(self._save_speed_limit)
        form.addRow("Maximum download speed:", self.download_speed_limit)

        self.out_edit = QLineEdit(SETTINGS.get("output_dir", ""))
        btn_out = QPushButton("…")
        btn_out.clicked.connect(self._pick_out)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(btn_out)
        form.addRow("Save to:", out_row)

        self.cookies_edit = QLineEdit(SETTINGS.get("cookies_file", ""))
        self.cookies_edit.setPlaceholderText("optional — cookies.txt for age-restricted / bot-checked videos")
        btn_ck = QPushButton("…")
        btn_ck.clicked.connect(self._pick_cookies)
        ck_row = QHBoxLayout()
        ck_row.addWidget(self.cookies_edit, 1)
        ck_row.addWidget(btn_ck)
        form.addRow("Cookies:", ck_row)

        self.extra_edit = QLineEdit(SETTINGS.get("extra_args", ""))
        self.extra_edit.setPlaceholderText("extra yt-dlp arguments (advanced), e.g. --proxy http://…")
        form.addRow("Extra args:", self.extra_edit)
        lay.addLayout(form)

        checks = QHBoxLayout()
        self.chk_playlist = QCheckBox("Whole playlist")
        self.chk_subs = QCheckBox("Subtitles")
        self.chk_thumb = QCheckBox("Thumbnail"); self.chk_thumb.setChecked(True)
        self.chk_meta = QCheckBox("Metadata"); self.chk_meta.setChecked(True)
        self.chk_sb = QCheckBox("Skip sponsors (SponsorBlock)")
        for c in (self.chk_playlist, self.chk_subs, self.chk_thumb,
                  self.chk_meta, self.chk_sb):
            checks.addWidget(c)
        lay.addLayout(checks)

        # dream features
        dream = QHBoxLayout()
        self.chk_watch = QCheckBox("👀 Watch while downloading")
        self.chk_watch.setChecked(bool(SETTINGS.get("watch_while", False)))
        self.chk_watch.setToolTip(
            "Start watching the video while it is still downloading.\n"
            "Uses single-file (progressive) formats when available so the file\n"
            "grows as you watch. Best with VLC/mpv; the download continues in\n"
            "the background no matter what.")
        self.chk_resume = QCheckBox("🔁 Continue failed downloads (reuse partial files)")
        self.chk_resume.setChecked(bool(SETTINGS.get("resume", False)))
        self.chk_resume.setToolTip(
            "If a download errors out, Retry reuses what was already on disk\n"
            "instead of starting over. Also grabs livestreams from the start\n"
            "(--live-from-start). Same folder + same quality = resume.")
        dream.addWidget(self.chk_watch)
        dream.addWidget(self.chk_resume)
        dream.addStretch(1)
        lay.addLayout(dream)

        dl_row = QHBoxLayout()
        btn_dl = QPushButton("⬇ Download")
        btn_dl.setStyleSheet("font-weight: bold; padding: 8px 24px;")
        btn_dl.clicked.connect(self.start_download)
        btn_doc = QPushButton("🩺 Error Doctor…")
        btn_doc.clicked.connect(lambda: self.open_doctor("", ""))
        dl_row.addWidget(btn_dl)
        dl_row.addWidget(btn_doc)
        dl_row.addStretch(1)
        lay.addLayout(dl_row)
        lay.addStretch(1)
        return w


    def _pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "Choose output folder",
                                             self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _pick_cookies(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose cookies.txt",
                                           "", "cookies.txt (*.txt);;All files (*)")
        if f:
            self.cookies_edit.setText(f)

    def fetch_meta(self):
        url = self.url_edit.text().strip()
        if not url:
            return
        self.meta_label.setText("Fetching info …")
        self.meta_worker = MetaWorker(url)
        self.meta_worker.got_meta.connect(self._meta_ok)
        self.meta_worker.meta_error.connect(self._meta_fail)
        self.meta_worker.start()

    def _meta_ok(self, info: dict):
        dur = info.get("duration") or 0
        m, s = divmod(int(dur), 60)
        self.meta_label.setText(
            f"🎬 {info.get('title', '?')}   ·   {info.get('uploader', '?')}"
            + (f"   ·   {m}:{s:02d}" if dur else ""))

    def _meta_fail(self, err: str):
        self.meta_label.setText("⚠ Could not fetch info — see Error Doctor.")
        self.open_doctor(err, "")

    def start_download(self):
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.information(self, "TubeSage", "Paste a URL first.")
            return
        job = Job(
            url=url,
            preset=self.preset_combo.currentText(),
            output_dir=self.out_edit.text().strip() or str(downloader.DEFAULT_OUTPUT),
            speed_limit_kbs=self.download_speed_limit.value(),
            playlist=self.chk_playlist.isChecked(),
            subtitles=self.chk_subs.isChecked(),
            embed_thumbnail=self.chk_thumb.isChecked(),
            embed_metadata=self.chk_meta.isChecked(),
            sponsorblock=self.chk_sb.isChecked(),
            cookies_file=self.cookies_edit.text().strip(),
            extra_args=self.extra_edit.text().strip(),
            watch_while=self.chk_watch.isChecked(),
            resume=self.chk_resume.isChecked(),
            title=url,
        )
        SETTINGS["watch_while"] = job.watch_while
        SETTINGS["resume"] = job.resume
        save_settings(SETTINGS)
        self._enqueue(job)
        self.tabs.setCurrentIndex(1)   # show queue


    # --------------------------------------------------------- queue ----
    def _build_queue_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.queue_stack = QStackedWidget()
        self.queue_tree = QTreeWidget()
        self.queue_tree.setHeaderLabels(
            ["Video", "Progress", "Speed", "ETA", "Status"])
        self.queue_tree.setRootIsDecorated(False)
        self.queue_tree.setColumnWidth(0, 340)
        self.queue_tree.itemDoubleClicked.connect(self._queue_double)
        self.queue_grid = QListWidget()
        self.queue_grid.setViewMode(QListWidget.IconMode)
        self.queue_grid.setIconSize(QSize(160, 90))
        self.queue_grid.setGridSize(QSize(184, 190))
        self.queue_grid.setResizeMode(QListWidget.Adjust)
        self.queue_grid.setMovement(QListWidget.Static)
        self.queue_grid.setWordWrap(True)
        self.queue_grid.setTextElideMode(Qt.ElideNone)
        self.queue_grid.itemDoubleClicked.connect(self._grid_double)
        self.queue_stack.addWidget(self.queue_tree)     # index 0: list view
        self.queue_stack.addWidget(self.queue_grid)     # index 1: thumbnails
        lay.addWidget(self.queue_stack)
        btns = QHBoxLayout()
        for label, cb in (
                ("❌ Cancel selected", self._cancel_selected),
                ("🔁 Retry selected", self._retry_selected),
                ("▶ Watch now", self._watch_selected),
                ("🩺 Find a fix (error)", self._fix_selected),
                ("📁 Open output folder", self._open_out_dir)):
            b = QPushButton(label)
            b.clicked.connect(cb)
            btns.addWidget(b)
        self.btn_queue_view = QPushButton()
        self.btn_queue_view.clicked.connect(self._toggle_queue_view)
        btns.addStretch(1)
        btns.addWidget(self.btn_queue_view)
        lay.addLayout(btns)
        self._set_queue_view(SETTINGS.get("queue_view") == "thumbs")
        return w

    def _set_queue_view(self, thumbs: bool):
        self.queue_stack.setCurrentIndex(1 if thumbs else 0)
        self.btn_queue_view.setText("☰ List" if thumbs else "▦ Thumbnails")
        self.btn_queue_view.setToolTip(
            "Switch between the list view and YTDLnis-style thumbnail cards.")
        SETTINGS["queue_view"] = "thumbs" if thumbs else "list"
        save_settings(SETTINGS)

    def _toggle_queue_view(self):
        self._set_queue_view(self.queue_stack.currentIndex() == 0)

    # -- thumbnail card helpers ------------------------------------------
    def _grid_status(self, job: Job) -> str:
        if job.state == "downloading":
            return f"{job.pct:.0f} % · {job.speed or '…'} · ETA {job.eta or '?'}"
        if job.state == "finished":
            return "finished ✔"
        if job.state == "error":
            return "ERROR — double-click for the Error Doctor"
        return job.state

    def _grid_sync(self, jid: str):
        """Keep a job's thumbnail card (title + status) up to date."""
        job = self.jobs.get(jid)
        item = self.grid_items.get(jid)
        if not job or not item:
            return
        item.setText(f"{job.title or job.url}\n{self._grid_status(job)}")
        item.setToolTip(f"{job.title}\n{job.url}\n→ {job.output_dir}")

    def _load_grid_thumb(self, jid: str):
        job = self.jobs.get(jid)
        if not job:
            return
        vid = video_id_from_url(job.url)
        self.thumbs.get(
            vid, lambda pm, jid=jid: self._grid_icon(jid, pm))

    def _grid_icon(self, jid: str, pm: QPixmap):
        item = self.grid_items.get(jid)
        if item is not None:
            item.setIcon(QIcon(pm))

    def _jid_by_grid(self, item: QListWidgetItem) -> str | None:
        for jid, it in self.grid_items.items():
            if it is item:
                return jid
        return None

    def _grid_double(self, item: QListWidgetItem):
        jid = self._jid_by_grid(item)
        job = self.jobs.get(jid) if jid else None
        if not job:
            return
        if job.state == "error":
            self.open_doctor(job.error_text, job.command)
        elif job.state in ("finished", "downloading"):
            self._open_watch(job)

    def _enqueue(self, job: Job):
        self.jobs[job.id] = job
        row = self.rows.get(job.id)
        if row is None:
            row = QTreeWidgetItem([job.title, "0 %", "", "", "queued"])
            self.queue_tree.insertTopLevelItem(0, row)
            self.rows[job.id] = row
        else:                       # retried job: reuse its existing row
            row.setText(1, "0 %")
            row.setText(2, "")
            row.setText(3, "")
            row.setText(4, "queued")
        gi = self.grid_items.get(job.id)
        if gi is None:
            gi = QListWidgetItem()
            gi.setSizeHint(QSize(184, 190))
            gi.setIcon(QIcon(self.thumbs.placeholder()))
            self.queue_grid.insertItem(0, gi)
            self.grid_items[job.id] = gi
            self._load_grid_thumb(job.id)
        self._grid_sync(job.id)
        worker = DownloadWorker(job)
        self.workers[job.id] = worker
        worker.progress.connect(self._on_progress)
        worker.state_changed.connect(self._on_state)
        worker.failed.connect(self._on_failed)
        worker.finished_ok.connect(self._on_finished)
        worker.start()

    def _on_progress(self, jid: str, pct: float, speed: str, eta: str):
        row = self.rows.get(jid)
        if row:
            row.setText(1, f"{pct:.1f} %")
            row.setText(2, speed)
            row.setText(3, eta)
        self._grid_sync(jid)
        job = self.jobs.get(jid)
        if job and job.watch_while and not job.watch_opened \
                and pct >= 2.0 and job.part_file:
            job.watch_opened = True
            self._open_watch(job)

    def _on_state(self, jid: str, state: str):
        row = self.rows.get(jid)
        job = self.jobs.get(jid)
        if row:
            if state == "downloading" and job and job.resume:
                row.setText(4, "downloading ▶ (continuing)")
            else:
                row.setText(4, state)
        if row and job and row.text(0) == job.url and job.title != job.url:
            row.setText(0, job.title)
        self._grid_sync(jid)

    def _on_failed(self, jid: str, error_text: str):
        row = self.rows.get(jid)
        if row:
            row.setText(4, "ERROR — double-click for the Error Doctor")
        self._grid_sync(jid)
        job = self.jobs.get(jid)
        if job:
            self.open_doctor(error_text, job.command)

    def _on_finished(self, jid: str, filepath: str):
        row = self.rows.get(jid)
        if row:
            row.setText(4, "finished ✔")
        self._grid_sync(jid)
        job = self.jobs.get(jid)
        if job:
            # yt-dlp sometimes reports the pre-merge stream path (which is
            # deleted right after) — resolve to the real final file
            if not filepath or not os.path.exists(filepath):
                filepath = self._resolve_media(job)
            if filepath and os.path.exists(filepath):
                job.filepath = filepath
                import time as _t
                SETTINGS.setdefault("history", []).insert(0, {
                    "title": job.title, "filepath": filepath,
                    "url": job.url,
                    "when": _t.strftime("%Y-%m-%d %H:%M")})
                save_settings(SETTINGS)
                self._refresh_history()

    def _selected_jid(self) -> str | None:
        item = self.queue_tree.currentItem()
        for jid, row in self.rows.items():
            if row is item:
                return jid
        return None

    def _queue_double(self, item, _col):
        for jid, row in self.rows.items():
            if row is item and self.jobs[jid].state == "error":
                job = self.jobs[jid]
                self.open_doctor(job.error_text, job.command)
                return

    def _cancel_selected(self):
        jid = self._selected_jid()
        if jid and self.workers[jid].job.state == "downloading":
            self.workers[jid].cancel()

    def _retry_selected(self):
        jid = self._selected_jid()
        job = self.jobs.get(jid) if jid else None
        if job and job.state in ("error", "cancelled"):
            # re-apply the current home-screen options (keep output_dir —
            # resume only works if partial files stay in the same folder)
            job.preset = self.preset_combo.currentText()
            job.watch_while = self.chk_watch.isChecked()
            job.resume = self.chk_resume.isChecked()
            job.pct = 0.0
            job.watch_opened = False    # part_file is kept on purpose — the
            self._enqueue(job)          # stale-part recovery may need it

    def _fix_selected(self):
        jid = self._selected_jid()
        if jid and self.jobs[jid].state == "error":
            job = self.jobs[jid]
            self.open_doctor(job.error_text, job.command)

    def _open_out_dir(self):
        jid = self._selected_jid()
        d = self.jobs[jid].output_dir if jid else self.out_edit.text()
        webbrowser.open(d or str(downloader.DEFAULT_OUTPUT))

    # ---------------------------------------------- watch while downloading --
    def _watch_selected(self):
        jid = self._selected_jid()
        if jid:
            self._open_watch(self.jobs[jid])

    def _resolve_media(self, job: Job) -> str:
        """Best-effort locate a playable file for a job.

        Order: the growing .part file (watch-while), the recorded final file,
        then a scan of the output folder — newest matching media file first.
        This heals cases where the recorded path is stale (yt-dlp deletes the
        separate audio/video streams after merging, and post-processors rename
        the result).
        """
        cands: list[str] = []
        if job.part_file:
            cands += [job.part_file + ".part", job.part_file]
        if job.filepath:
            cands.append(job.filepath)
        for c in cands:
            if c and os.path.exists(c):
                return c
        try:
            entries = sorted(Path(job.output_dir).glob("*"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return ""
        # the video id yt-dlp appends as "[id]" — from the URL
        vid = video_id_from_url(job.url)
        title = (job.title or "").strip()
        media = [p for p in entries if p.is_file()
                 and p.suffix.lower() in MEDIA_EXTS
                 and not p.name.endswith(".part")]
        for p in media:                       # best: matches id or title
            if (vid and vid in p.name) or (title and title[:40] in p.name):
                return str(p)
        if media:                             # next: newest finished media file
            return str(media[0])
        parts = [p for p in entries            # last: growing .part file
                 if p.is_file() and p.name.endswith(".part")]
        return str(parts[0]) if parts else ""

    def _open_watch(self, job: Job):
        """Open the video in a media player while it is still downloading.

        Prefers the growing .part file of the video stream; falls back to the
        finished file. The download is NOT stopped — it keeps running.
        """
        target = self._resolve_media(job)
        if not target:
            QMessageBox.information(
                self, "Watch now",
                "Nothing to watch yet — the video data appears a few seconds "
                "after the download starts. Try again in a moment.")
            return
        player = _find_player()
        try:
            if player:
                subprocess.Popen([player, target],
                                 creationflags=subprocess.DETACHED_PROCESS)
            else:
                os.startfile(target)   # no .part association — usually fails
        except OSError:
            QMessageBox.warning(
                self, "Watch now",
                "No media player found that can open a file while it is "
                "downloading.\nInstalling VLC (free) makes this work: "
                "https://www.videolan.org/")

    # ------------------------------------------------------- history ----
    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.history_list = QListWidget()
        self.history_list.setWordWrap(True)
        self.history_list.itemDoubleClicked.connect(self._open_history_item)
        lay.addWidget(self.history_list)
        btns = QHBoxLayout()
        b_open = QPushButton("📁 Open selected")
        b_open.clicked.connect(self._open_history_item)
        b_clr = QPushButton("🗑 Clear history")
        b_clr.clicked.connect(self._clear_history)
        self.btn_hist_view = QPushButton()
        self.btn_hist_view.clicked.connect(self._toggle_history_view)
        btns.addWidget(b_open)
        btns.addWidget(b_clr)
        btns.addStretch(1)
        btns.addWidget(self.btn_hist_view)
        lay.addLayout(btns)
        self._set_history_view(SETTINGS.get("history_view") == "thumbs")
        self._refresh_history()
        return w

    def _set_history_view(self, thumbs: bool):
        if thumbs:
            self.history_list.setViewMode(QListWidget.IconMode)
            self.history_list.setIconSize(QSize(160, 90))
            self.history_list.setGridSize(QSize(184, 210))
            self.history_list.setResizeMode(QListWidget.Adjust)
            self.history_list.setMovement(QListWidget.Static)
            self.history_list.setTextElideMode(Qt.ElideNone)
            for i in range(self.history_list.count()):
                self.history_list.item(i).setSizeHint(QSize(184, 210))
        else:
            self.history_list.setViewMode(QListWidget.ListMode)
            self.history_list.setIconSize(QSize(24, 24))
            self.history_list.setGridSize(QSize())
            for i in range(self.history_list.count()):
                self.history_list.item(i).setSizeHint(QSize())
        self.btn_hist_view.setText("☰ List" if thumbs else "▦ Thumbnails")
        self.btn_hist_view.setToolTip(
            "Switch between the list view and YTDLnis-style thumbnail cards.")
        SETTINGS["history_view"] = "thumbs" if thumbs else "list"
        save_settings(SETTINGS)

    def _toggle_history_view(self):
        self._set_history_view(
            self.history_list.viewMode() != QListWidget.IconMode)

    def _refresh_history(self):
        self.history_list.clear()
        for h in SETTINGS.get("history", []):
            item = QListWidgetItem(f"{h.get('when', '')} — {h.get('title', '')}\n"
                                   f"    {h.get('filepath', '')}",
                                   self.history_list)
            path = h.get("filepath", "")
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            item.setIcon(QIcon(self.thumbs.placeholder()))
            vid = video_id_from_url(h.get("url", "")) \
                or video_id_from_path(path)
            if vid:
                def _set_icon(pm: QPixmap, it=item):
                    try:
                        it.setIcon(QIcon(pm))
                    except RuntimeError:
                        pass        # item was cleared meanwhile
                self.thumbs.get(vid, _set_icon)
        if self.history_list.viewMode() == QListWidget.IconMode:
            for i in range(self.history_list.count()):
                self.history_list.item(i).setSizeHint(QSize(184, 210))

    def _open_history_item(self, item=None):
        item = item or self.history_list.currentItem()
        if item:
            path = item.data(Qt.UserRole) or item.text().split("\n    ")[-1]
            if not os.path.exists(path):
                QMessageBox.information(
                    self, "Open",
                    "This file no longer exists (it may have been the "
                    "pre-merge audio/video stream that yt-dlp deletes after "
                    "merging):\n" + path)
                return
            webbrowser.open(path)

    def _clear_history(self):
        SETTINGS["history"] = []
        save_settings(SETTINGS)
        self._refresh_history()

    # ------------------------------------------------------ settings ----
    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        eng = QGroupBox("Download engine")
        el = QVBoxLayout(eng)
        self.ytdlp_label = QLabel()
        self.ffmpeg_label = QLabel()
        el.addWidget(self.ytdlp_label)
        el.addWidget(self.ffmpeg_label)
        row = QHBoxLayout()
        b_yt = QPushButton("Install / Update yt-dlp")
        b_yt.clicked.connect(lambda: self._bootstrap(False))
        b_ff = QPushButton("Install ffmpeg")
        b_ff.clicked.connect(lambda: self._bootstrap(True))
        row.addWidget(b_yt)
        row.addWidget(b_ff)
        row.addStretch(1)
        el.addLayout(row)
        lay.addWidget(eng)

        archive = QGroupBox("Twitch VOD archive → YouTube Studio")
        al = QVBoxLayout(archive)
        archive_form = QFormLayout()
        self.archive_streamer = QLineEdit()
        self.archive_streamer.setPlaceholderText("Twitch channel login, for example trimax")
        archive_form.addRow("Twitch streamer:", self.archive_streamer)

        self.archive_start = QDateEdit(QDate.currentDate())
        self.archive_start.setCalendarPopup(True)
        self.archive_start.setDisplayFormat("yyyy-MM-dd")
        archive_form.addRow("Start date (inclusive):", self.archive_start)

        end_row = QHBoxLayout()
        self.archive_range_enabled = QCheckBox("Use a fixed date range (uncheck to keep searching forward)")
        self.archive_end = QDateEdit(QDate.currentDate())
        self.archive_end.setCalendarPopup(True)
        self.archive_end.setDisplayFormat("yyyy-MM-dd")
        self.archive_end.setEnabled(False)
        self.archive_range_enabled.toggled.connect(self.archive_end.setEnabled)
        end_row.addWidget(self.archive_range_enabled)
        end_row.addWidget(self.archive_end)
        archive_form.addRow("Date range:", end_row)

        folder_row = QHBoxLayout()
        self.archive_folder = QLineEdit(str(downloader.DEFAULT_OUTPUT / "Twitch Archive"))
        self.archive_folder.setReadOnly(True)
        choose_folder = QPushButton("Browse…")
        choose_folder.clicked.connect(self._choose_archive_folder)
        folder_row.addWidget(self.archive_folder, 1)
        folder_row.addWidget(choose_folder)
        archive_form.addRow("Archive folder:", folder_row)

        window_row = QHBoxLayout()
        self.archive_start_time = QTimeEdit()
        self.archive_start_time.setDisplayFormat("HH:mm")
        self.archive_end_time = QTimeEdit()
        self.archive_end_time.setDisplayFormat("HH:mm")
        self.archive_start_time.setTime(self.archive_start_time.time().fromString("06:00", "HH:mm"))
        self.archive_end_time.setTime(self.archive_end_time.time().fromString("23:00", "HH:mm"))
        window_row.addWidget(QLabel("From"))
        window_row.addWidget(self.archive_start_time)
        window_row.addWidget(QLabel("to"))
        window_row.addWidget(self.archive_end_time)
        archive_form.addRow("Download window:", window_row)

        self.archive_limit = QDoubleSpinBox()
        self.archive_limit.setRange(1, 100000)
        self.archive_limit.setDecimals(0)
        self.archive_limit.setSuffix(" GB")
        self.archive_limit.setValue(500)
        archive_form.addRow("Storage allowance:", self.archive_limit)
        self.archive_auto_refresh = QCheckBox("Automatically refresh this Twitch archive every hour")
        self.archive_auto_refresh.setChecked(True)
        archive_form.addRow("Automatic search:", self.archive_auto_refresh)
        al.addLayout(archive_form)

        archive_help = QLabel(
            "TubeSage lists public Twitch archive VODs. Expand a VOD to see its six-hour segments. "
            "Completed segment files are checked automatically; check Uploaded after YouTube Studio, "
            "or use that segment's Delete button. Pause preserves the partial file, and Resume continues it. "
            "Set the maximum speed in KB/s, or use 0 for unlimited speed. Install ffmpeg in the Download engine section.")
        archive_help.setWordWrap(True)
        al.addWidget(archive_help)
        archive_actions = QHBoxLayout()
        find_vods = QPushButton("Find VODs now")
        find_vods.clicked.connect(self._archive_search)
        download_selected = QPushButton("Download selected")
        download_selected.clicked.connect(self._archive_tick)
        self.archive_pause_button = QPushButton("Pause downloads")
        self.archive_pause_button.clicked.connect(self._archive_pause)
        self.archive_resume_button = QPushButton("Resume downloads")
        self.archive_resume_button.clicked.connect(self._archive_resume)
        open_studio = QPushButton("Open YouTube Studio")
        open_studio.clicked.connect(lambda: webbrowser.open("https://studio.youtube.com/"))
        archive_actions.addWidget(find_vods)
        archive_actions.addWidget(download_selected)
        archive_actions.addWidget(self.archive_pause_button)
        archive_actions.addWidget(self.archive_resume_button)
        archive_actions.addWidget(open_studio)
        archive_actions.addStretch(1)
        al.addLayout(archive_actions)
        self.archive_status = QLabel("No Twitch archive search yet.")
        self.archive_status.setWordWrap(True)
        al.addWidget(self.archive_status)
        self.archive_table = QTreeWidget()
        self.archive_table.setColumnCount(5)
        self.archive_table.setHeaderLabels(["VOD / Segment", "Stream date", "Downloaded / Status",
                                            "Uploaded / Details", "Actions"])
        self.archive_table.setSelectionBehavior(QTreeWidget.SelectRows)
        self.archive_table.setSelectionMode(QTreeWidget.SingleSelection)
        self.archive_table.setEditTriggers(QTreeWidget.NoEditTriggers)
        self.archive_table.setUniformRowHeights(False)
        self.archive_table.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.archive_table.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.archive_table.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.archive_table.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.archive_table.header().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.archive_table.itemChanged.connect(self._archive_item_changed)
        self.archive_table.itemDoubleClicked.connect(self._archive_open_twitch)
        al.addWidget(self.archive_table, 1)
        archive_buttons = QHBoxLayout()
        for title, callback in (("Select all", self._archive_select_all),
                                ("Clear selection", self._archive_clear_selection)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            archive_buttons.addWidget(button)
        archive_buttons.addStretch(1)
        al.addLayout(archive_buttons)
        file_buttons = QHBoxLayout()
        for title, callback in (("Open Twitch VOD", self._archive_open_twitch),
                                ("Open VOD folder", self._archive_open_folder),
                                ("Retry failed", self._archive_retry_failed)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            file_buttons.addWidget(button)
        al.addLayout(file_buttons)
        lay.addWidget(archive, 1)

        doc = QGroupBox("Error Doctor")
        dl = QFormLayout(doc)
        self.agent_combo = QComboBox()
        self.agent_combo.addItems(["auto", "codex", "cline", "opencode"])
        self.agent_combo.setCurrentText(SETTINGS.get("cli_agent", "auto"))
        dl.addRow("CLI agent for Deep Search:", self.agent_combo)
        self.tg_edit = QLineEdit(SETTINGS.get("tg_group", "YTDLNIS"))
        dl.addRow("Telegram group:", self.tg_edit)
        self.tg_topic_edit = QLineEdit(SETTINGS.get("tg_topic", "general"))
        dl.addRow("Telegram topic:", self.tg_topic_edit)
        kb = QLabel(str(knowledge.STORE))
        kb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dl.addRow("Knowledge base:", kb)
        lay.addWidget(doc)

        btn_save = QPushButton("💾 Save settings")
        btn_save.clicked.connect(self._save_all)
        lay.addWidget(btn_save)
        lay.addStretch(1)
        self._refresh_engine_labels()
        return w

    def _refresh_engine_labels(self):
        self.ytdlp_label.setText(f"yt-dlp: {mgr.ytdlp_version()}")
        self.ffmpeg_label.setText(
            "ffmpeg: installed ✔" if mgr.ffmpeg_available()
            else "ffmpeg: not installed (needed for high-quality merging & MP3)")

    def _save_all(self):
        SETTINGS["cli_agent"] = self.agent_combo.currentText()
        SETTINGS["tg_group"] = self.tg_edit.text().strip() or "YTDLNIS"
        SETTINGS["tg_topic"] = self.tg_topic_edit.text().strip() or "general"
        SETTINGS["output_dir"] = self.out_edit.text().strip()
        SETTINGS["cookies_file"] = self.cookies_edit.text().strip()
        SETTINGS["extra_args"] = self.extra_edit.text().strip()
        SETTINGS["preset"] = self.preset_combo.currentText()
        SETTINGS["download_speed_limit_kbs"] = self.download_speed_limit.value()
        self._save_archive_settings()
        save_settings(SETTINGS)
        if self.archive_auto_refresh.isChecked():
            self.archive_scan_timer.start()
        else:
            self.archive_scan_timer.stop()
        self.statusBar().showMessage("Settings saved.", 4000)

    def _save_speed_limit(self, value: int):
        SETTINGS["download_speed_limit_kbs"] = int(value)
        save_settings(SETTINGS)

    # ------------------------------------------------ Twitch archive ----
    def _archive_defaults(self) -> dict:
        today = QDate.currentDate()
        return {
            "streamer": "", "start_date": today.addDays(-30).toString("yyyy-MM-dd"),
            "end_date": today.toString("yyyy-MM-dd"), "date_mode": "range",
            "output_dir": str(downloader.DEFAULT_OUTPUT / "Twitch Archive"),
            "start_time": "06:00", "end_time": "23:00", "max_gb": 500,
            "paused": False,
            "auto_refresh": True, "vods": [],
        }

    def _archive_settings(self) -> dict:
        state = self._archive_defaults()
        stored = SETTINGS.get("twitch_archive", {})
        state.update(stored)
        if state.get("streamer", "").strip().lower() in ("trimax", "trymax"):
            state["streamer"] = "trymacs"
        if "date_mode" not in stored:
            # The previous finder returned no dated entries. Start its first
            # successful search with the useful recent 30-day range.
            state["start_date"] = self._archive_defaults()["start_date"]
            state["end_date"] = self._archive_defaults()["end_date"]
            state["date_mode"] = "range"
        return state

    def _restore_archive_controls(self):
        a = self._archive_settings()
        for vod in a.get("vods", []):
            vod.setdefault("segments_done", [])
            vod.setdefault("segments_uploaded", [])
            folder = twitch_archive.vod_folder(a.get("output_dir", ""), vod)
            twitch_archive.migrate_legacy_segment_names(folder, vod)
            done = {int(i) for i in vod.get("segments_done", [])}
            uploaded = {int(i) for i in vod.get("segments_uploaded", [])}
            count = max(1, (int(vod.get("duration") or twitch_archive.SEGMENT_SECONDS)
                           + twitch_archive.SEGMENT_SECONDS - 1) // twitch_archive.SEGMENT_SECONDS)
            if vod.get("status") == "done":
                done.update(range(count))
                uploaded.update(range(count))
            for segment in range(count):
                files = twitch_archive.segment_files(folder, vod, segment)
                if any(not path.name.endswith((".part", ".ytdl")) for path in files):
                    done.add(segment)
            vod["segments_done"] = sorted(done)
            vod["segments_uploaded"] = sorted(uploaded)
            if vod.get("status") == "downloading":
                vod["status"] = "paused"
                vod["error"] = "Paused when TubeSage closed; will resume automatically"
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)
        self.archive_streamer.setText(a.get("streamer", ""))
        defaults = self._archive_defaults()
        self.archive_start.setDate(QDate.fromString(a.get("start_date", defaults["start_date"]), "yyyy-MM-dd"))
        end = a.get("end_date", "")
        if a.get("date_mode", "range") == "range":
            self.archive_range_enabled.setChecked(True)
            self.archive_end.setDate(QDate.fromString(end or defaults["end_date"], "yyyy-MM-dd"))
        else:
            self.archive_range_enabled.setChecked(False)
            self.archive_end.setDate(QDate.fromString(end or defaults["end_date"], "yyyy-MM-dd"))
        self.archive_folder.setText(a.get("output_dir", self._archive_defaults()["output_dir"]))
        self.archive_start_time.setTime(self.archive_start_time.time().fromString(a.get("start_time", "06:00"), "HH:mm"))
        self.archive_end_time.setTime(self.archive_end_time.time().fromString(a.get("end_time", "23:00"), "HH:mm"))
        self.archive_limit.setValue(float(a.get("max_gb", 500)))
        self._set_archive_pause_button(bool(a.get("paused", False)))
        self.archive_auto_refresh.setChecked(bool(a.get("auto_refresh", True)))

    def _save_archive_settings(self):
        a = self._archive_settings()
        a.update({
            "streamer": self.archive_streamer.text().strip().lstrip("@"),
            "start_date": self.archive_start.date().toString("yyyy-MM-dd"),
            "end_date": self.archive_end.date().toString("yyyy-MM-dd") if self.archive_range_enabled.isChecked() else "",
            "date_mode": "range" if self.archive_range_enabled.isChecked() else "ongoing",
            "output_dir": self.archive_folder.text().strip(),
            "start_time": self.archive_start_time.time().toString("HH:mm"),
            "end_time": self.archive_end_time.time().toString("HH:mm"),
            "max_gb": self.archive_limit.value(),
            "paused": self._archive_settings().get("paused", False),
            "auto_refresh": self.archive_auto_refresh.isChecked(),
        })
        if hasattr(self, "archive_table"):
            for vod in a.get("vods", []):
                if vod.get("status") == "storage-full":
                    vod["status"] = "queued"
                    vod["error"] = ""
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)

    def _choose_archive_folder(self):
        selected = QFileDialog.getExistingDirectory(self, "Choose Twitch VOD folder",
                                                    self.archive_folder.text())
        if selected:
            self.archive_folder.setText(selected)
            self._save_archive_settings()

    def _archive_search(self):
        if self.archive_search and self.archive_search.isRunning():
            return
        self._save_archive_settings()
        a = self._archive_settings()
        if not a["streamer"]:
            self.archive_status.setText("Enter a Twitch channel login first.")
            return
        self.archive_search = VodSearchWorker(a["streamer"], a["start_date"], a["end_date"], self)
        self.archive_search.progress.connect(self.archive_status.setText)
        self.archive_search.failed.connect(self._archive_search_failed)
        self.archive_search.found.connect(self._archive_search_finished)
        self.archive_search.start()

    def _archive_search_failed(self, error: str):
        self.archive_status.setText(f"Twitch search failed: {error}")

    def _archive_search_finished(self, found: list):
        a = self._archive_settings()
        existing = {str(v.get("id")): v for v in a.get("vods", [])}
        added = 0
        for vod in found:
            old = existing.get(str(vod["id"]))
            if old:
                is_live_now = vod.get("status") == "live"
                was_live = old.get("status") == "live"
                vod.update(old)
                if is_live_now and old.get("status") not in ("done", "downloaded"):
                    vod["status"] = "live"
                elif was_live and not is_live_now:
                    vod["status"] = "found"
                    vod["error"] = ""
            else:
                added += 1
            existing[str(vod["id"])] = vod
        a["vods"] = sorted(existing.values(), key=lambda v: (v.get("date", ""), v.get("id", "")))
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)
        self._refresh_archive_table()
        self.archive_status.setText(
            f"Found {len(found)} VODs in this date selection; {added} new. "
            "Select the recordings you want, then choose Download selected.")

    def _refresh_archive_table(self):
        if not hasattr(self, "archive_table"):
            return
        vods = self._archive_settings().get("vods", [])
        expanded = {str(self.archive_table.topLevelItem(i).data(0, Qt.UserRole))
                    for i in range(self.archive_table.topLevelItemCount())
                    if self.archive_table.topLevelItem(i).isExpanded()}
        self.archive_table.clear()
        self.archive_table.blockSignals(True)
        for vod in vods:
            status = vod.get("status", "queued")
            status_label = {
                "downloaded": "Downloaded",
                "done": "Uploaded",
                "downloading": "Downloading",
                "paused": "Paused",
                "error": "Error",
                "storage-full": "Storage full",
                "live": "Live now",
                "found": "Found - select to download",
            }.get(status, "Queued")
            details = vod.get("error", "")
            if status == "downloaded":
                details = "Ready to upload in YouTube Studio"
            elif status == "done":
                details = "All segments uploaded"
            vod_id = str(vod.get("id", ""))
            done = {int(i) for i in vod.get("segments_done", [])}
            count = max(1, (int(vod.get("duration") or twitch_archive.SEGMENT_SECONDS)
                           + twitch_archive.SEGMENT_SECONDS - 1) // twitch_archive.SEGMENT_SECONDS)
            parent = QTreeWidgetItem([str(vod.get("title", "")), str(vod.get("date", "")),
                                      status_label, f"{len(done)}/{count} segments downloaded",
                                      details or "Segments"])
            parent.setData(0, Qt.UserRole, vod_id)
            parent.setFlags(parent.flags() | Qt.ItemIsUserCheckable)
            default_selected = status in ("queued", "paused", "found")
            parent.setCheckState(0, Qt.Checked if vod.get("selected", default_selected)
                                 else Qt.Unchecked)
            parent.setToolTip(0, vod.get("url", ""))
            self.archive_table.addTopLevelItem(parent)
            folder = twitch_archive.vod_folder(self._archive_settings().get("output_dir", ""), vod)
            uploaded = {int(i) for i in vod.get("segments_uploaded", [])}
            for segment in range(count):
                files = twitch_archive.segment_files(folder, vod, segment)
                downloaded = segment in done or any(
                    not path.name.endswith((".part", ".ytdl")) for path in files)
                if downloaded:
                    done.add(segment)
                label = f"E{segment + 1}"
                label += f" - {files[0].name}" if files else " - not downloaded"
                child = QTreeWidgetItem([label, "", "", "", ""])
                child.setData(0, Qt.UserRole, vod_id)
                child.setData(0, Qt.UserRole + 1, segment)
                child.setFlags(child.flags() & ~Qt.ItemIsSelectable)
                parent.addChild(child)
                downloaded_box = QCheckBox("Downloaded")
                downloaded_box.setChecked(downloaded)
                downloaded_box.setEnabled(False)
                downloaded_box.setToolTip("Checked automatically when the segment file is complete")
                downloaded_box.toggled.connect(
                    lambda checked, vid=vod_id, seg=segment: self._archive_set_segment_downloaded(
                        vid, seg, checked))
                self.archive_table.setItemWidget(child, 2, downloaded_box)
                uploaded_box = QCheckBox("Uploaded")
                uploaded_box.setChecked(segment in uploaded)
                uploaded_box.setEnabled(downloaded)
                uploaded_box.toggled.connect(
                    lambda checked, vid=vod_id, seg=segment: self._archive_set_segment_uploaded(
                        vid, seg, checked))
                self.archive_table.setItemWidget(child, 3, uploaded_box)
                delete_button = QPushButton("Delete")
                delete_button.setEnabled(self.archive_worker is None
                                         or not self.archive_worker.isRunning())
                delete_button.clicked.connect(
                    lambda _checked=False, vid=vod_id, seg=segment:
                    self._archive_delete_segment(vid, seg))
                self.archive_table.setItemWidget(child, 4, delete_button)
            vod["segments_done"] = sorted(done)
            parent.setExpanded(vod_id in expanded)
        self.archive_table.blockSignals(False)

    def _archive_item_changed(self, item):
        if item.column() != 0 or item.parent() is not None:
            return
        vod_id = str(item.data(Qt.UserRole))
        a = self._archive_settings()
        for vod in a.get("vods", []):
            if str(vod.get("id")) == vod_id:
                vod["selected"] = item.checkState() == Qt.Checked
                break
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)

    def _archive_find_vod(self, vod_id: str):
        a = self._archive_settings()
        vod = next((v for v in a.get("vods", []) if str(v.get("id")) == str(vod_id)), None)
        return a, vod

    def _archive_set_segment_downloaded(self, vod_id: str, segment: int, downloaded: bool):
        a, vod = self._archive_find_vod(vod_id)
        if not vod:
            return
        done = {int(i) for i in vod.get("segments_done", [])}
        if downloaded:
            files = twitch_archive.segment_files(twitch_archive.vod_folder(a["output_dir"], vod),
                                                 vod, segment)
            if not any(not path.name.endswith((".part", ".ytdl")) for path in files):
                QMessageBox.information(self, "TubeSage", "The completed segment file was not found.")
                self._refresh_archive_table()
                return
            done.add(segment)
        else:
            done.discard(segment)
        vod["segments_done"] = sorted(done)
        count = max(1, (int(vod.get("duration") or twitch_archive.SEGMENT_SECONDS)
                       + twitch_archive.SEGMENT_SECONDS - 1) // twitch_archive.SEGMENT_SECONDS)
        if all(i in done for i in range(count)):
            vod["status"] = "downloaded"
            vod["selected"] = False
        elif vod.get("status") in ("downloaded", "done"):
            vod["status"] = "queued"
            vod["selected"] = True
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)
        self._refresh_archive_table()

    def _archive_set_segment_uploaded(self, vod_id: str, segment: int, uploaded: bool):
        a, vod = self._archive_find_vod(vod_id)
        if not vod:
            return
        segments = {int(i) for i in vod.get("segments_uploaded", [])}
        if uploaded:
            done = {int(i) for i in vod.get("segments_done", [])}
            files = twitch_archive.segment_files(twitch_archive.vod_folder(a["output_dir"], vod),
                                                 vod, segment)
            if segment not in done or not any(
                    not path.name.endswith((".part", ".ytdl")) for path in files):
                QMessageBox.information(self, "TubeSage", "Mark the completed segment as downloaded first.")
                self._refresh_archive_table()
                return
            segments.add(segment)
        else:
            segments.discard(segment)
        vod["segments_uploaded"] = sorted(segments)
        count = max(1, (int(vod.get("duration") or twitch_archive.SEGMENT_SECONDS)
                       + twitch_archive.SEGMENT_SECONDS - 1) // twitch_archive.SEGMENT_SECONDS)
        if all(i in segments for i in range(count)) and vod.get("status") == "downloaded":
            vod["status"] = "done"
        elif vod.get("status") == "done":
            vod["status"] = "downloaded"
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)
        self._refresh_archive_table()

    def _archive_delete_segment(self, vod_id: str, segment: int):
        a, vod = self._archive_find_vod(vod_id)
        if not vod:
            return
        if self.archive_worker and self.archive_worker.isRunning():
            QMessageBox.information(self, "TubeSage", "Pause downloads before deleting a segment.")
            return
        files = twitch_archive.segment_files(twitch_archive.vod_folder(a["output_dir"], vod),
                                             vod, segment)
        if not files:
            return
        confirm = QMessageBox.question(self, "Delete segment files?",
                                       "Permanently delete these files?\n\n" +
                                       "\n".join(path.name for path in files))
        if confirm != QMessageBox.Yes:
            return
        for path in files:
            try:
                path.unlink()
            except OSError as exc:
                QMessageBox.warning(self, "TubeSage", f"Could not delete {path.name}: {exc}")
        done = {int(i) for i in vod.get("segments_done", [])}
        uploaded = {int(i) for i in vod.get("segments_uploaded", [])}
        if segment not in uploaded:
            done.discard(segment)
            vod["segments_done"] = sorted(done)
        if vod.get("status") in ("downloaded", "done") and segment not in uploaded:
            vod["status"] = "queued"
            vod["selected"] = True
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)
        self._refresh_archive_table()

    def _archive_set_selection(self, selected: bool):
        a = self._archive_settings()
        for vod in a.get("vods", []):
            if vod.get("status") not in ("downloaded", "done", "live"):
                vod["selected"] = selected
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)
        self._refresh_archive_table()

    def _archive_select_all(self):
        self._archive_set_selection(True)

    def _archive_clear_selection(self):
        self._archive_set_selection(False)

    def _selected_archive_vod(self):
        item = self.archive_table.currentItem()
        if item is None:
            return None
        if item.parent() is not None:
            item = item.parent()
        vod_id = item.data(0, Qt.UserRole)
        return next((v for v in self._archive_settings().get("vods", [])
                     if str(v.get("id")) == str(vod_id)), None)

    def _archive_open_twitch(self, *_args):
        vod = self._selected_archive_vod()
        if vod and vod.get("url"):
            webbrowser.open(vod["url"])

    def _archive_open_folder(self):
        vod = self._selected_archive_vod()
        if not vod:
            return
        folder = twitch_archive.vod_folder(self.archive_folder.text(), vod)
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    def _archive_retry_failed(self):
        for vod in self._archive_settings().get("vods", []):
            if vod.get("status") == "error":
                vod["status"] = "queued"
                vod["selected"] = True
                vod["error"] = ""
        self._save_archive_settings()
        self._refresh_archive_table()

    def _archive_tick(self):
        if self.archive_worker and self.archive_worker.isRunning():
            return
        a = self._archive_settings()
        if a.get("paused", False):
            return
        pending = [dict(v) for v in a.get("vods", [])
                   if v.get("selected", v.get("status") in ("queued", "paused"))
                   and v.get("status") not in ("live", "error")]
        if not pending:
            return
        self._save_archive_settings()
        a = self._archive_settings()
        self.archive_worker = ArchiveDownloadWorker(
            pending, a["output_dir"], a["start_time"], a["end_time"],
            float(a["max_gb"]), int(SETTINGS.get("download_speed_limit_kbs", 0)), self)
        self.archive_worker.progress.connect(self.archive_status.setText)
        self.archive_worker.changed.connect(self._archive_state_changed)
        self.archive_worker.segment_done.connect(self._archive_segment_done)
        self.archive_worker.stopped.connect(self._archive_worker_stopped)
        self.archive_worker.finished.connect(self._archive_worker_finished)
        self.archive_worker.start()

    def _set_archive_pause_button(self, paused: bool):
        self.archive_pause_button.setEnabled(not paused)
        self.archive_resume_button.setEnabled(paused)

    def _archive_pause(self):
        a = self._archive_settings()
        a["paused"] = True
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)
        self._set_archive_pause_button(True)
        if self.archive_worker and self.archive_worker.isRunning():
            self.archive_worker.pause()
        else:
            self.archive_status.setText("Downloads paused. Choose Resume downloads to continue.")

    def _archive_resume(self):
        a = self._archive_settings()
        a["paused"] = False
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)
        self._set_archive_pause_button(False)
        self.archive_status.setText("Downloads resumed.")
        if self.archive_worker and self.archive_worker.isRunning():
            self._resume_after_pause = True
        else:
            self._archive_tick()

    def _archive_worker_stopped(self):
        if self._archive_settings().get("paused", False):
            self.archive_status.setText("Downloads paused. Choose Resume downloads to continue.")

    def _archive_worker_finished(self):
        if self._resume_after_pause and not self._archive_settings().get("paused", False):
            self._resume_after_pause = False
            QTimer.singleShot(0, self._archive_tick)

    def _archive_segment_done(self, vod_id: str, segment: int):
        a = self._archive_settings()
        for vod in a.get("vods", []):
            if str(vod.get("id")) == str(vod_id):
                done = set(vod.get("segments_done", []))
                done.add(int(segment))
                vod["segments_done"] = sorted(done)
                break
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)
        self._refresh_archive_table()

    def _archive_state_changed(self, vod_id: str, state: str, detail: str):
        a = self._archive_settings()
        for vod in a.get("vods", []):
            if str(vod.get("id")) == str(vod_id):
                vod["status"] = state
                vod["error"] = detail if state in ("error", "storage-full") else ""
                if state in ("downloaded", "done", "live"):
                    vod["selected"] = False
                break
        SETTINGS["twitch_archive"] = a
        save_settings(SETTINGS)
        self._refresh_archive_table()

    def closeEvent(self, event):
        if self.archive_worker and self.archive_worker.isRunning():
            self.archive_worker.cancel()
            self.archive_worker.wait(5000)
        if self.archive_search and self.archive_search.isRunning():
            self.archive_search.cancel()
            self.archive_search.wait(5000)
        self._save_archive_settings()
        event.accept()

    # ------------------------------------------------------ bootstrap ----
    def _bootstrap(self, with_ffmpeg: bool):
        self.statusBar().showMessage("Downloading engine …")
        self.bootstrap = BootstrapWorker(with_ffmpeg)
        self.bootstrap.step.connect(self.statusBar().showMessage)
        self.bootstrap.done.connect(self._bootstrap_done)
        self.bootstrap.start()

    def _bootstrap_done(self, ok: bool, msg: str):
        self.statusBar().showMessage(msg, 6000)
        self._refresh_engine_labels()
        if not ok:
            QMessageBox.warning(self, "TubeSage", msg)

    # -------------------------------------------------------- doctor ----
    def open_doctor(self, error_text: str, yt_command: str):
        dlg = ErrorDoctorDialog(error_text or "(no error text — describe "
                                "what went wrong)", yt_command, self)
        dlg.exec()

# --------------------------------------------------------------- main ----
def _find_player() -> str | None:
    """A media player that tolerates half-written files (VLC/mpv are ideal)."""
    for name in ("vlc", "mpv"):
        p = shutil.which(name)
        if p:
            return p
    for c in (r"C:\Program Files\VideoLAN\VLC\vlc.exe",
              r"C:\Program Files\MPV\mpv.exe",
              r"C:\Program Files (x86)\Windows Media Player\wmplayer.exe",
              r"C:\Program Files\Windows Media Player\wmplayer.exe"):
        if os.path.exists(c):
            return c
    return None


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("TubeSage")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())






