import sys
sys.path.insert(0, r"C:\Dev\TubeSage")
import ytdlp_manager as m
p = m.install_ffmpeg()
print("ffmpeg installed:", p)
