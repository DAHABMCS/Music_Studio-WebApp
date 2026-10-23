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
#        every *.py in the project root (app.py, transcription_extras.py, subtitle_engine.py,
#        user_store.py, MUSIC.py, midi_refine.py ...), requirements*.txt, *.pdf manuals,
#        hiddenimports.json, templates/ static/ assets/ docs/, runtime/ (ffmpeg.exe, ffprobe.exe),
#        prereqs/ (vc_redist.x64.exe), extras/
# NOT shipped on purpose: users.json(.key), lyrics_ai.env (API keys), uploads/, outputs/, jobs/

import os
from pathlib import Path

ROOT = Path(SPECPATH)

NOT_PAYLOAD_FILES = {"launcher.py", "check_requirements.py", "runtime_hook.py",
                     "verify_dist.py", "collect_prereqs.py"}
DATA_DIRS = ("templates", "static", "assets", "docs", "runtime", "prereqs", "extras")
SKIP_PARTS = {"__pycache__", ".git", ".buildenv", "venv", ".venv", "_autosite"}

# Files that must be in payload/ but are not *.py and are not in one of the DATA_DIRS.
PAYLOAD_EXTRA_FILES = (
    "requirements.txt",
    "requirements-optional.txt",
    "runtime.txt",
    "hiddenimports.json",
)


def add_tree(folder: Path, dest_root: str, out: list):
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in SKIP_PARTS]
        rel = Path(dirpath).relative_to(folder)
        for fn in filenames:
            if fn.endswith((".pyc", ".pyo")):
                continue
            out.append((str(Path(dirpath) / fn), str(Path(dest_root) / rel)))


payload = []
# Every project *.py goes into payload/ EXCEPT the build-tool scripts.
for p in sorted(ROOT.glob("*.py")):
    if p.name not in NOT_PAYLOAD_FILES:
        payload.append((str(p), "payload"))

# Config / manifest files that must ride along.
for name in PAYLOAD_EXTRA_FILES:
    f = ROOT / name
    if f.exists():
        payload.append((str(f), "payload"))

# PDF manuals at the project root.
for p in sorted(ROOT.glob("*.pdf")):
    payload.append((str(p), "payload"))

# Whole directories.
for d in DATA_DIRS:
    if (ROOT / d).is_dir():
        add_tree(ROOT / d, f"payload/{d}", payload)

# Safety check: refuse to build a payload with no app.py.
if not any(src.endswith("app.py") for src, _ in payload):
    raise SystemExit(
        "app.py not found next to MusicStudio.spec - run the build from the project root."
    )
# Safety check: refuse to build without the new module.
if not any(src.endswith("transcription_extras.py") for src, _ in payload):
    raise SystemExit(
        "transcription_extras.py not found next to MusicStudio.spec - the Full Transcription "
        "feature will be missing. Put the file in the project root and rebuild."
    )

icon_file = None
for cand in (ROOT / "static" / "icon.ico", ROOT / "assets" / "app.ico", ROOT / "app.ico",
             ROOT / "guitar.ico"):
    if cand.exists():
        icon_file = cand
        break

a = Analysis(
    ["launcher.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=payload,
    # The launcher is pure stdlib + tkinter, but a few of these are imported
    # dynamically by the launcher's self-check logic.
    hiddenimports=[
        "tkinter",
        "tkinter.ttk",
        "tkinter.messagebox",
        "tkinter.simpledialog",
        "tkinter.filedialog",
    ],
    hookspath=[],
    runtime_hooks=[],
    # These are installed at runtime by the launcher, not bundled.
    excludes=["torch", "torchaudio", "numpy", "scipy", "librosa", "matplotlib", "pandas",
              "flask", "werkzeug", "waitress", "demucs", "transformers", "PIL", "cv2",
              "pretty_midi", "mido", "music21"],
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