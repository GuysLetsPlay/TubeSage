import sys
sys.path.insert(0, r"C:\Dev\TubeSage")
import subprocess, tempfile, os
from pathlib import Path
from downloader import Job

out = Path(tempfile.mkdtemp(prefix="tubesage_test_"))
job = Job(url="https://www.youtube.com/watch?v=jNQXAC9IVRw",  # "Me at the zoo", 19s
          preset="480p", output_dir=str(out),
          embed_thumbnail=False, embed_metadata=False)
args = [str(Path(r"C:\Dev\TubeSage\bin\yt-dlp.exe"))] + job.build_args()
print("COMMAND:", " ".join(args))
proc = subprocess.run(args, capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=240,
                      creationflags=subprocess.CREATE_NO_WINDOW)
tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
print("\n".join(tail))
files = list(out.glob("*"))
print("exit:", proc.returncode, "files:", [f.name for f in files])
print("DOWNLOAD TEST", "OK" if files else "FAILED")
