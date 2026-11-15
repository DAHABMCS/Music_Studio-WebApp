# -*- mode: python ; coding: utf-8 -*-
<<<<<<< HEAD
"""
MusicStudio.spec  -  builds launcher.py into  dist\\MusicStudio\\MusicStudio.exe

MusicStudio.exe is a THIN launcher (tkinter + standard library only). PyTorch, Whisper, Demucs and the
other big libraries are NOT frozen into it - launcher.py installs them into a private venv on first run.

What this spec does
  1. Freezes launcher.py            -> MusicStudio.exe
  2. Ships the app files as a "payload" folder inside the bundle (dist\\MusicStudio\\_internal\\payload),
     which is exactly where launcher.py looks (payload_dir() = sys._MEIPASS / "payload"):
        app.py, templates\\, static\\, assets\\, requirements*.txt, helper modules ...
        runtime\\ffmpeg.exe, runtime\\ffprobe.exe   <- made by collect_prereqs.py, used by ensure_ffmpeg()
        prereqs\\vc_redist.x64.exe                  <- made by collect_prereqs.py, used by ensure_vcredist()
     (only those files - runtime\\ may also hold big things like an ACE-Step install, which must NOT ship)
     Setup copies everything except runtime\\ and prereqs\\ to %LOCALAPPDATA%\\MusicStudio\\app.
  3. Ships EVERY top-level .py of the project except the build tools listed in TOOL_SCRIPTS - verify_dist.py
     checks that the payload mirrors the project root - plus any local module/package app.py imports.

The audio editor and the User Manager are separate apps: StudioEditor.spec and Securit_Management.spec.
"""
import ast
from pathlib import Path

ROOT = Path(SPECPATH).resolve()          # folder that contains this .spec (the project root)

# ---- what goes into the payload -------------------------------------------------------------
PAYLOAD_FILES = ["app.py", "requirements.txt", "requirements-optional.txt"]
PAYLOAD_DIRS = ["templates", "static", "assets"]
# top-level .py files that are build tools / the launcher itself and are NOT part of the app payload
TOOL_SCRIPTS = {"launcher.py", "collect_prereqs.py", "check_requirements.py", "verify_dist.py", "runtime_hook.py"}   # = NOT_PAYLOAD in verify_dist.py
# installer helpers launcher.py reads from the payload (NOT copied into the app folder).
# Listed file by file on purpose: runtime\ can also contain a whole ACE-Step install.
HELPER_FILES = {"runtime": ["ffmpeg.exe", "ffprobe.exe"],
                "prereqs": ["vc_redist.x64.exe"]}

REQUIRED_FILES = ["launcher.py", "app.py", "requirements.txt"]
REQUIRED_DIRS = ["templates", "static"]


def _local_imports(start):
    """Project-root modules / packages imported (directly or indirectly) from `start`."""
    files, dirs, todo = set(), set(), [start]
    while todo:
        cur = todo.pop()
        try:
            tree = ast.parse((ROOT / cur).read_text(encoding="utf-8-sig", errors="replace"))
        except Exception as ex:                                   # unreadable / syntax error -> skip
            print(f"[MusicStudio.spec] WARNING: could not scan {cur}: {ex}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for n in names:
                if (ROOT / (n + ".py")).is_file() and (n + ".py") not in files and (n + ".py") != "launcher.py":
                    files.add(n + ".py")
                    todo.append(n + ".py")
                elif (ROOT / n / "__init__.py").is_file():
                    dirs.add(n)
    return files, dirs


problems = [f"missing file: {f}" for f in REQUIRED_FILES if not (ROOT / f).is_file()]
problems += [f"missing folder: {d}\\" for d in REQUIRED_DIRS if not (ROOT / d).is_dir()]
if problems:
    raise SystemExit("[MusicStudio.spec] cannot build - run this from the project root:\n  " + "\n  ".join(problems))

auto_files, auto_dirs = _local_imports("app.py")
root_py = sorted(f.name for f in ROOT.glob("*.py") if f.name not in TOOL_SCRIPTS)

datas = []
shipped = []
for f in dict.fromkeys(PAYLOAD_FILES + root_py + sorted(auto_files)):
    if f not in TOOL_SCRIPTS and (ROOT / f).is_file():
        datas.append((str(ROOT / f), "payload"))
        shipped.append(f)
for d in dict.fromkeys(PAYLOAD_DIRS + sorted(auto_dirs)):
    if (ROOT / d).is_dir():
        size = sum(x.stat().st_size for x in (ROOT / d).rglob("*") if x.is_file()) / 1048576
        if size > 300:
            print(f"[MusicStudio.spec] WARNING: {d}\\ is {size:.0f} MB - is everything in it really needed?")
        datas.append((str(ROOT / d), "payload/" + d))
        shipped.append(d + "\\")
for folder, names in HELPER_FILES.items():
    for n in names:
        if (ROOT / folder / n).is_file():
            datas.append((str(ROOT / folder / n), "payload/" + folder))
            shipped.append(f"{folder}\\{n}")
        else:
            print(f"[MusicStudio.spec] note: {folder}\\{n} not found - run collect_prereqs.py "
                  "(setup will download it instead)")
print("[MusicStudio.spec] payload:", ", ".join(shipped))

ICON = str(ROOT / "guitar.ico") if (ROOT / "guitar.ico").is_file() else None

a = Analysis(
    ['launcher.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=['tkinter', 'tkinter.ttk', 'tkinter.messagebox'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # the launcher must stay small: everything heavy is installed into the private venv at first run
        'numpy', 'scipy', 'torch', 'torchaudio', 'flask', 'matplotlib', 'pandas', 'sklearn',
        'librosa', 'soundfile', 'sounddevice', 'PIL', 'demucs', 'whisper',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython', 'notebook',
    ],
    noarchive=False,
    optimize=0,
=======
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
>>>>>>> 8d8f2974f9a335c664739db6340522fd15b773fa
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
<<<<<<< HEAD
    name='MusicStudio',                  # build_installer.bat and installer.iss expect MusicStudio.exe
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                       # GUI launcher: no black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
=======
    name="MusicStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX causes antivirus false positives
    console=False,                   # windowed: setup/control windows only, no black console
    icon=str(icon_file) if icon_file else None,
)

>>>>>>> 8d8f2974f9a335c664739db6340522fd15b773fa
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
<<<<<<< HEAD
    upx_exclude=[],
    name='MusicStudio',                  # -> dist\MusicStudio\
)
=======
    name="MusicStudio",              # -> dist\MusicStudio\MusicStudio.exe
)
>>>>>>> 8d8f2974f9a335c664739db6340522fd15b773fa
