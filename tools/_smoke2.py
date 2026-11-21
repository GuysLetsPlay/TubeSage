import sys
sys.path.insert(0, r"C:\Dev\TubeSage")
sys.argv = ["tubesage"]
import main
app = main.QApplication(sys.argv)
w = main.MainWindow()
w.show()
assert hasattr(w, "chk_watch") and hasattr(w, "chk_resume")
from downloader import Job
j = Job(url="x", watch_while=True, resume=True)
a = j.build_args()
assert a.count("--continue") == 1 and "--live-from-start" in a
assert a[a.index("-f") + 1].startswith("b[height") is False  # Best stays simple
j2 = Job(url="x", preset="720p", watch_while=True)
assert j2.build_args()[3].startswith("b[height<=720]/"), j2.build_args()[3]
print("GUI SMOKE OK")

