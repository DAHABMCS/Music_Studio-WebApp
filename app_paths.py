r"""
app_paths.py - the ONE place that decides where Music Studio keeps its files.

  PROGRAM folder : code, templates, assets, manuals, ffmpeg.
                   Read-only once installed (e.g. C:\Program Files\MusicStudio).
  DATA folder    : users.json (+ users.json.key and its backups), uploads,
                   outputs, jobs, saved AI key, secret key, logs.
                   Always writable.

app.py and the user-manager GUI both import this module, so they can never
disagree about where users.json lives.

DATA folder location
  1. %MUSIC_STUDIO_DATA_DIR% if set  (use this for a shared / server install,
     e.g. C:\ProgramData\MusicStudio - set it machine-wide so BOTH programs
     see it)
  2. Windows : %LOCALAPPDATA%\MusicStudio
     macOS   : ~/Library/Application Support/MusicStudio
     Linux   : $XDG_DATA_HOME/MusicStudio  (default ~/.local/share/MusicStudio)
"""

import os
import sys
import shutil
import secrets
from pathlib import Path

APP_NAME = "MusicStudio"


def program_dir() -> Path:
    """Folder the program (or the frozen .exe) lives in."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Writable folder for everything the app creates. Created if missing."""
    override = os.environ.get("MUSIC_STUDIO_DATA_DIR", "").strip()
    if override:
        d = Path(override)
    elif sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        d = (Path(base) if base else Path.home() / "AppData" / "Local") / APP_NAME
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME")
        d = (Path(base) if base else Path.home() / ".local" / "share") / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_secret_key() -> str:
    """
    Flask session-signing key.
    Order: %MUSIC_STUDIO_SECRET_KEY%, then <data>/secret_key.txt (created with a
    random value on first run), so the key is never the same on two installs and
    never sits in the source code.
    """
    env = os.environ.get("MUSIC_STUDIO_SECRET_KEY", "").strip()
    if env:
        return env
    f = data_dir() / "secret_key.txt"
    try:
        if f.exists():
            k = f.read_text(encoding="utf-8").strip()
            if len(k) >= 32:
                return k
        k = secrets.token_hex(32)
        f.write_text(k, encoding="utf-8")
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
        return k
    except OSError:
        # Data folder not writable: still run, but logins won't survive a restart.
        return secrets.token_hex(32)


# --------------------------------------------------------------------------
# One-time move from the OLD layout (everything next to the program)
# --------------------------------------------------------------------------
_LEGACY_DIRS = ("uploads", "outputs", "jobs")
_LEGACY_FILES = ("lyrics_ai.env",)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _move_or_copy_dir(src: Path, dst: Path) -> str:
    if dst.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)   # merge
        return "copied"
    try:
        os.replace(src, dst)                            # instant on same drive
        return "moved"
    except OSError:
        shutil.copytree(src, dst)                       # e.g. read-only source
        return "copied"


def migrate_legacy_data(old_dir: Path, new_dir: Path):
    """
    If an old-style install left users.json (and friends) next to the program and
    the new data folder has no users.json yet, bring them over.
    users.json is copied LAST, so its presence means the migration finished.
    Returns a list of human-readable messages (empty = nothing to do).
    Never deletes anything it could not move.
    """
    old_dir, new_dir = Path(old_dir), Path(new_dir)
    msgs = []
    if _same(old_dir, new_dir):
        return msgs
    new_users = new_dir / "users.json"
    old_users = old_dir / "users.json"
    if new_users.exists() or not old_users.exists():
        return msgs

    new_dir.mkdir(parents=True, exist_ok=True)

    for name in _LEGACY_DIRS:
        src = old_dir / name
        if src.is_dir():
            try:
                how = _move_or_copy_dir(src, new_dir / name)
                msgs.append(f"[migrate] {how} {src} -> {new_dir / name}")
            except Exception as e:
                msgs.append(f"[migrate] WARNING could not bring over {src}: {e}")

    for name in _LEGACY_FILES:
        src = old_dir / name
        if src.is_file() and not (new_dir / name).exists():
            try:
                shutil.copy2(src, new_dir / name)
                msgs.append(f"[migrate] copied {src}")
            except Exception as e:
                msgs.append(f"[migrate] WARNING could not copy {src}: {e}")

    # users.json.key and any timestamped backups first, users.json itself last
    extras = [p for p in sorted(old_dir.glob("users.json*")) if p.name != "users.json"]
    try:
        for p in extras:
            if p.is_file():
                shutil.copy2(p, new_dir / p.name)
        shutil.copy2(old_users, new_users)
        msgs.append(f"[migrate] copied users file(s) from {old_dir} -> {new_dir}")
    except Exception as e:
        msgs.append(f"[migrate] WARNING could not copy users file(s): {e}")
    return msgs
