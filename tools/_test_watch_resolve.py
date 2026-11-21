"""Verify Watch-now path resolution for a FINISHED merged download.

Simulates the reported bug: job.filepath points at the pre-merge audio
stream (deleted after merging); the final merged file is in the folder.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as app  # noqa: E402
from downloader import Job  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="ts_watch_"))
final = tmp / "The Man Who Embarrassed Pro Ultimate Chicken Horse [2azUcsMVfc4].mkv"
final.write_bytes(b"0" * 1024)          # the merged file that exists
stale = tmp / "The Man ... [2azUcsMVfc4].f251-20.webm"  # deleted stream
# (do NOT create it — it was deleted by the merger)

job = Job(url="https://youtu.be/2azUcsMVfc4?si=x",
          preset="Best quality",
          output_dir=str(tmp))
job.filepath = str(stale)              # stale, does not exist
job.part_file = str(stale)[:-len(".webm")] + ".mp4"  # also stale
job.title = "The Man Who Embarrassed Pro Ultimate Chicken Horse"

got = app.MainWindow._resolve_media(None, job)  # method uses no self state
print("resolved:", got)
assert got and Path(got).exists(), "FAIL: could not resolve the final file"
assert Path(got) == final, f"FAIL: resolved to wrong file: {got}"
print("WATCH-RESOLVE-OK")
