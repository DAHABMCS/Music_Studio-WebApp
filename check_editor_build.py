#!/usr/bin/env python3
"""
check_editor_build.py - proves every setup uses the LATEST studio_audio_editor.py

Run from the project root (the folder that holds studio_audio_editor.py):
        python check_editor_build.py
        python check_editor_build.py "D:\\other\\place"        <- optional extra folders to scan

It compares the SHA-256 of your source file with
  1. every other copy of studio_audio_editor.py it finds (project folders, MusicStudio payload, %LOCALAPPDATA%)
  2. every editor_build.txt stamp written by StudioEditor.spec (dist, build output, installed programs)
and prints UP TO DATE or STALE for each. Exit code 0 = all current, 1 = something is stale.
"""
import hashlib
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "studio_audio_editor.py"
SKIP_DIRS = {".venv", "venv", "node_modules", ".git", "__pycache__", ".idea", "site-packages"}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def walk(base):
    for dp, dn, fn in os.walk(base):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            yield Path(dp) / f


def main():
    if not SRC.is_file():
        print(f"ERROR: {SRC} not found - put this script in the project root.")
        return 2
    src_hash = sha(SRC)
    print(f"SOURCE  {SRC}\n        sha256 {src_hash[:8]}...  (build id {src_hash[:8]})\n")

    bases = [ROOT] + [Path(a) for a in sys.argv[1:]]
    for env in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        v = os.environ.get(env)
        if v:
            for sub in ("MusicStudio", "StudioEditor", "Studio Editor"):
                if (Path(v) / sub).is_dir():
                    bases.append(Path(v) / sub)
    seen, stale, ok = set(), 0, 0

    print("== copies of studio_audio_editor.py ==")
    for base in bases:
        for p in walk(base):
            if p.name.lower() == "studio_audio_editor.py" and p.resolve() != SRC and p.resolve() not in seen:
                seen.add(p.resolve())
                same = sha(p) == src_hash
                ok, stale = (ok + 1, stale) if same else (ok, stale + 1)
                print(f"  {'UP TO DATE' if same else 'STALE     '}  {p}")
    if not seen:
        print("  (no other copies found)")

    print("\n== build stamps (editor_build.txt inside built / installed apps) ==")
    stamps = set()
    for base in bases:
        for p in walk(base):
            if p.name == "editor_build.txt" and p.resolve() != (ROOT / "editor_build.txt").resolve() \
                    and p.resolve() not in stamps:
                stamps.add(p.resolve())
                try:
                    first = p.read_text(encoding="utf-8").splitlines()[0].strip()
                except Exception:
                    first = ""
                same = first == src_hash
                ok, stale = (ok + 1, stale) if same else (ok, stale + 1)
                print(f"  {'UP TO DATE' if same else 'STALE     '}  {p}   (built from {first[:8] or '?'})")
    if not stamps:
        print("  (none found - rebuild with the new StudioEditor.spec so builds carry a stamp)")

    launcher = ROOT / "launcher.py"
    if launcher.is_file():
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', launcher.read_text(encoding="utf-8", errors="replace"))
        if m:
            print(f"\nlauncher.py APP_VERSION = {m.group(1)}   <- BUMP it whenever the editor changes, otherwise "
                  "installed users are not re-synced to the new payload.")

    print(f"\nSUMMARY: {ok} current, {stale} STALE")
    if stale:
        print("Fix: copy the latest studio_audio_editor.py to the project root, run pyinstaller StudioEditor.spec "
              "(--clean), rebuild MusicStudio.spec, rebuild the installer (.iss), then run this check again.")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
