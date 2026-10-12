# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the THIN launcher (launcher.py).
# Build (from the project root):   pyinstaller --noconfirm --clean MusicStudio.spec
# build_installer.bat does that for you after collecting everything it needs.
#
# Replaces the old MUSIC-STUDIO-WebbApp.spec, which froze torch/demucs/transformers INTO the exe
# (~500 MB, CUDA only). Now torch & co. are installed on the user's PC at first run (GPU or CPU build).
#
# What goes into the build:
#   * launcher.py -> MusicStudio.exe             (stdlib + tkinter only, a few MB)
#   * "payload"   -> copied by the launcher to %LOCALAPPDATA%\MusicStudio\app:
#        every *.py in the project root (app.py, subtitle_engine.py, user_store.py, MUSIC.py,
#        midi_refine.py ...), requirements*.txt, *.pdf manuals,
#        templates/ static/ assets/ docs/, runtime/ (ffmpeg.exe, ffprobe.exe), prereqs/ (vc_redist.x64.exe)
# NOT shipped on purpose: users.json(.key), lyrics_ai.env (API keys), uploads/, outputs/, jobs/
import os
from pathlib import Path

ROOT = Path(SPECPATH)

NOT_PAYLOAD_FILES = {"launcher.py", "check_requirements.py", "runtime_hook.py",
                     "verify_dist.py", "collect_prereqs.py"}
DATA_DIRS = ("templates", "static", "assets", "docs", "runtime", "prereqs")
SKIP_PARTS = {"__pycache__", ".git", ".buildenv", "venv", ".venv", "_autosite"}


def add_tree(folder: Path, dest_root: str, out: list):
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in SKIP_PARTS]
        rel = Path(dirpath).relative_to(folder)
        for fn in filenames:
            if fn.endswith((".pyc", ".pyo")):
                continue
            out.append((str(Path(dirpath) / fn), str(Path(dest_root) / rel)))


payload = []
for p in sorted(ROOT.glob("*.py")):
    if p.name not in NOT_PAYLOAD_FILES:
        payload.append((str(p), "payload"))
for name in ("requirements.txt", "requirements-optional.txt"):
    if (ROOT / name).exists():
        payload.append((str(ROOT / name), "payload"))
for p in sorted(ROOT.glob("*.pdf")):
    payload.append((str(p), "payload"))
for d in DATA_DIRS:
    if (ROOT / d).is_dir():
        add_tree(ROOT / d, f"payload/{d}", payload)

if not any(src.endswith("app.py") for src, _ in payload):
    raise SystemExit("app.py not found next to MusicStudio.spec - run the build from the project root.")

icon_file = None
for cand in (ROOT / "static" / "icon.ico", ROOT / "assets" / "app.ico", ROOT / "app.ico"):
    if cand.exists():
        icon_file = cand
        break

a = Analysis(
    ["launcher.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=payload,
    hiddenimports=["tkinter", "tkinter.ttk", "tkinter.messagebox"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["torch", "torchaudio", "numpy", "scipy", "librosa", "matplotlib", "pandas",
              "flask", "werkzeug", "waitress", "demucs", "transformers", "PIL", "cv2"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MusicStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX causes antivirus false positives
    console=False,                   # windowed: setup/control windows only, no black console
    icon=str(icon_file) if icon_file else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MusicStudio",              # -> dist\MusicStudio\MusicStudio.exe
)
