import sys
sys.path.insert(0, r"C:\Dev\TubeSage")
sys.argv = ["t"]
import main
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
app = QApplication([])
w = main.MainWindow()
res = []
w.thumbs.get("2azUcsMVfc4", lambda pm: res.append(pm))
QTimer.singleShot(20000, app.quit)
t = QTimer(); t.timeout.connect(lambda: app.quit() if res else None); t.start(200)
app.exec()
cached = (main.THUMB_CACHE / "2azUcsMVfc4.jpg").exists()
print("live thumb:", "OK" if res and not res[0].isNull() else "FAIL", "| cached:", cached, "| size:", (res[0].width(), res[0].height()) if res else None)
