"""Check that the 'continue failed download' flags are accepted for normal videos."""
import subprocess
import sys

EXE = r"C:\Dev\TubeSage\bin\yt-dlp.exe"
cmd = [EXE, "--live-from-start", "--keep-fragments", "--continue",
       "--js-runtimes", "node", "--simulate", "--no-playlist",
       "-f", "b[height<=720]/bv*[height<=720]+ba/b[height<=720]",
       "https://www.youtube.com/watch?v=YE7VzlLtp-4"]
p = subprocess.run(cmd, capture_output=True, text=True)
print("exit:", p.returncode)
out = (p.stdout + p.stderr).strip().splitlines()
for line in out[:4]:
    print(" ", line)
print("SELECTED-FORMAT-OK" if p.returncode == 0 else "FLAGS-FAILED")
sys.exit(0 if p.returncode == 0 else 1)
