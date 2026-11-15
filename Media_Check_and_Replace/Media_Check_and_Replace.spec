# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for "Media Check and Replace"  ->  single windowed .exe
#
# FOLDER LAYOUT (put these files together, then build):
#     Media_Check_and_Replace.py
#     Media_Check_and_Replace.spec      <- this file
#     icon.ico                          <- exe icon (optional)
#     ffmpeg.exe, ffprobe.exe           <- optional, see below
#
# BUILD (from a Command Prompt in that folder):
#     python -m pip install -U pyinstaller yt-dlp
#     pyinstaller --clean --noconfirm Media_Check_and_Replace.spec
#
# RESULT:  dist\Media Check and Replace.exe
#
# FFMPEG:
#   * If ffmpeg.exe AND ffprobe.exe sit next to this spec file when you build,
#     they are packed INSIDE the exe (the exe becomes ~150 MB but is fully
#     standalone, nothing else to install).
#   * If they are not there, the exe is small, and ffmpeg.exe / ffprobe.exe
#     must be placed next to the finished exe (or be in the system PATH).
#
# yt-dlp is frozen into the exe at build time. YouTube changes often, so
# rebuild now and then after:  python -m pip install -U yt-dlp

import os
from PyInstaller.utils.hooks import collect_all

APP_NAME = "Media Check and Replace"
SCRIPT = "Media_Check_and_Replace.py"
HERE = os.path.abspath(SPECPATH)

datas, binaries, hiddenimports = [], [], []

# yt-dlp loads its site extractors dynamically, so pull in everything it has.
# certifi supplies the HTTPS certificates; yt_dlp_ejs is used by newer yt-dlp.
for pkg in ("yt_dlp", "certifi", "yt_dlp_ejs"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:  # package not installed -> skip it
        print(f"[spec] skipping {pkg}: {exc}")

# Optionally bundle FFmpeg inside the exe
for exe in ("ffmpeg.exe", "ffprobe.exe"):
    path = os.path.join(HERE, exe)
    if os.path.exists(path):
        binaries.append((path, "."))
        print(f"[spec] bundling {exe}")
    else:
        print(f"[spec] {exe} not found next to the spec: it must sit next to "
              f"the finished exe or be in PATH")

icon_path = os.path.join(HERE, "icon.ico")
icon = icon_path if os.path.exists(icon_path) else None

a = Analysis(
    [os.path.join(HERE, SCRIPT)],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # big libraries this program never uses (keeps the exe smaller)
    excludes=["numpy", "scipy", "pandas", "matplotlib", "PIL", "IPython",
              "pytest", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    icon=icon,
    console=False,          # windowed program: no black console box
    debug=False,
    strip=False,
    upx=False,              # UPX often triggers antivirus false alarms
    runtime_tmpdir=None,
    disable_windowed_traceback=False,
)
