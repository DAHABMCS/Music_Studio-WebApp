# Runtime hook: make the bundled ffmpeg.exe discoverable by shutil.which("ffmpeg").
import os
import sys

_base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
_exe_dir = os.path.dirname(sys.executable)
os.environ["PATH"] = os.pathsep.join(
    [_base, _exe_dir, os.environ.get("PATH", "")]
)