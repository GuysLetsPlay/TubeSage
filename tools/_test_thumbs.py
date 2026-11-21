"""Headless test for the thumbnail/grid views (Queue + History) — no network."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.argv = ["tubesage"]
import main  # noqa: E402
from downloader import Job  # noqa: E402
from PySide6.QtWidgets import QListWidget  # noqa: E402

# --- id extraction -------------------------------------------------------
assert main.video_id_from_url(
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
assert main.video_id_from_url(
    "https://youtu.be/2azUcsMVfc4?si=x") == "2azUcsMVfc4"
assert main.video_id_from_url("https://example.com/not-youtube") == ""
assert main.video_id_from_path(
    r"C:\v\The Man [2azUcsMVfc4].f251-20.webm") == "2azUcsMVfc4"
print("id extraction OK")

app = main.QApplication(sys.argv)
w = main.MainWindow()

# --- placeholder pixmap + thumb cache ------------------------------------
pm = w.thumbs.placeholder()
assert not pm.isNull() and pm.width() == 320
got = []
w.thumbs.get("", lambda p: got.append(p))          # empty id → placeholder
assert got and not got[0].isNull()
# cached-file path: write a tiny valid PNG into the cache under a fake id
png = pm.scaled(4, 4).toImage()
import PySide6.QtCore as qc
buf = qc.QBuffer(); buf.open(qc.QIODevice.WriteOnly)
pm.scaled(4, 4).save(buf, "PNG")
fake = main.THUMB_CACHE / "fakeid123.jpg"
fake.write_bytes(bytes(buf.data()))
got2 = []
w.thumbs.get("fakeid123", lambda p: got2.append(p))   # disk-cache hit
assert got2 and not got2[0].isNull() and "fakeid123" in w.thumbs.mem
print("thumb cache OK")

# --- queue view toggle ----------------------------------------------------
assert w.queue_stack.count() == 2
w._set_queue_view(True)
assert w.queue_stack.currentIndex() == 1
assert w.btn_queue_view.text() == "☰ List"
w._toggle_queue_view()
assert w.queue_stack.currentIndex() == 0
assert w.btn_queue_view.text() == "▦ Thumbnails"
assert main.SETTINGS["queue_view"] == "list"
print("queue toggle OK")

# --- grid card population (no real download started) ----------------------
tmp = Path(tempfile.mkdtemp(prefix="ts_grid_"))
job = Job(url="https://youtu.be/2azUcsMVfc4", output_dir=str(tmp),
          title="The Man Who Embarrassed Pro Ultimate Chicken Horse")
w.jobs[job.id] = job
item = main.QListWidgetItem()
item.setSizeHint(qc.QSize(184, 190))
item.setIcon(w.thumbs.placeholder_icon if hasattr(
    w.thumbs, "placeholder_icon") else w.thumbs.placeholder())
w.queue_grid.insertItem(0, item)
w.grid_items[job.id] = item
w._grid_sync(job.id)
assert job.title[:20] in item.text() and "queued" in item.text()
job.state, job.pct, job.speed, job.eta = "downloading", 42.4, "3.2MiB/s", "01:10"
w._grid_sync(job.id)
assert "42 %" in item.text() and "3.2MiB/s" in item.text()
job.state = "finished"
w._grid_sync(job.id)
assert "finished" in item.text()
print("grid card sync OK")

# --- history view toggle + icons ------------------------------------------
h0 = main.SETTINGS.get("history", [])
main.SETTINGS["history"] = [{
    "title": "The Man Who Embarrassed Pro Ultimate Chicken Horse",
    "filepath": r"C:\v\The Man [2azUcsMVfc4].mkv",
    "when": "2026-09-02 20:57"}]
w._refresh_history()
it = w.history_list.item(0)
assert it is not None
assert it.data(qc.Qt.UserRole) == r"C:\v\The Man [2azUcsMVfc4].mkv"
assert not it.icon().isNull()
w._set_history_view(True)
assert w.history_list.viewMode() == QListWidget.IconMode
assert main.SETTINGS["history_view"] == "thumbs"
w._refresh_history()          # re-entry with thumbs mode on
assert w.history_list.item(0).sizeHint().height() >= 200
w._toggle_history_view()
assert w.history_list.viewMode() == QListWidget.ListMode
main.SETTINGS["history"] = h0
main.save_settings(main.SETTINGS)
print("history toggle OK")

print("THUMBS-TEST-OK")
