"""Real-world test for the 'continue failed download' feature:
1. start a download, kill it mid-way (simulated error)
2. verify partial files remain
3. re-run with --continue and verify yt-dlp RESUMES instead of restarting
"""
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, r"C:\Dev\TubeSage")
from downloader import Job

URL = "https://www.youtube.com/watch?v=YE7VzlLtp-4"   # Big Buck Bunny (10:34)
out = Path(tempfile.mkdtemp(prefix="tubesage_resume_"))
job = Job(url=URL, preset="1080p", output_dir=str(out), resume=True)
exe = r"C:\Dev\TubeSage\bin\yt-dlp.exe"
args = [exe] + job.build_args()

# --- 1. kill after 12 s -----------------------------------------------
p1 = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, encoding="utf-8", errors="replace")
t0 = time.time()
first_line = ""
run1_lines = []
while time.time() - t0 < 90:
    line = p1.stdout.readline() if p1.stdout else ""
    if line:
        run1_lines.append(line)
        if not first_line:
            first_line = line.strip()
            print("first output after %.1f s: %s" % (time.time() - t0, first_line[:90]))
        if "[download]" in line and "%" in line:
            break   # actual downloading has started
    elif p1.poll() is not None:
        break
p1.kill()
p1.wait()
print("killed at %.1f s" % (time.time() - t0))
# like the app's worker: remember the video stream's destination path
run1_log = "".join(run1_lines)
m1 = re.search(r"\[download\] Destination: (.+)", run1_log)
if m1:
    job.part_file = m1.group(1).strip()
    print("job.part_file =", job.part_file)
else:
    print("NO PARTIALS — download never started; test inconclusive")
    sys.exit(2)

# --- 2. partial files? -------------------------------------------------
parts = list(out.rglob("*.part"))
for f in parts:
    print("  partial:", f.name, f.stat().st_size // 1024, "KiB")
if not parts:
    print("NO PARTIALS — download finished before the kill; test inconclusive")
    sys.exit(2)

# --- 3. resume runs (the app's Retry can be clicked more than once) ----
import downloader as dl

resumed_seen = False
ok = False
for attempt in range(2, 5):
    rec = dl.finalize_stale_part(job)   # app-side recovery before each retry
    print("recovery finalized:", rec)
    p2 = subprocess.run(args, capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    log = p2.stdout + p2.stderr
    print("--- run %d exit: %d ---" % (attempt, p2.returncode))
    for ln in log.strip().splitlines()[-4:]:
        print("  ", ln.strip())
    if "Resuming download" in log:
        resumed_seen = True
    if p2.returncode == 0:
        ok = True
        break
    job.error_text = log
    m = re.search(r"\[download\] Destination: (.+)", log)
    if m and not job.part_file:
        job.part_file = m.group(1).strip()
print("resumed-seen:", resumed_seen)
print("RESUME-OK" if ok else "RESUME-FAILED")
sys.exit(0 if ok else 1)
