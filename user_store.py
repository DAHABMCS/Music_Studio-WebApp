"""
user_store.py — encrypted-at-rest storage for users.json.

Shared by app.py (the Flask login server) and User_Management.py (the
Tkinter admin GUI), so both programs read/write the SAME encrypted file
the SAME way and can never disagree about the format.

WHAT THIS PROTECTS AGAINST
    Someone opening users.json in a text editor, or any other script
    calling json.load() on it, sees only unreadable ciphertext — not
    usernames, roles, or password hashes.

WHAT THIS DOES NOT PROTECT AGAINST
    Anyone with full filesystem access to BOTH users.json AND its key
    file (users.json.key, sitting right next to it) can still decrypt
    it — the key has to live somewhere these two programs can read it,
    or they couldn't read it either. This is encryption-at-rest for
    casual/opportunistic access, not a substitute for OS-level account
    separation and file permissions if multiple people share the machine.

Dependencies:
    pip install cryptography --break-system-packages

Format on disk:
    users.json       -> binary Fernet token (NOT valid JSON — this is
                         expected and is the point)
    users.json.key   -> the raw Fernet key (url-safe base64), created
                         automatically on first save, permissioned
                         0600 (owner read/write only) where the OS
                         supports it.

Migration:
    If users.json already exists as plain JSON (old format), load_users()
    detects that automatically, reads it once, and the next save_users()
    call transparently re-writes it encrypted. Nothing is lost.
"""

import json
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _key_path(users_path) -> Path:
    return Path(str(users_path) + ".key")


def _lock_down(path: Path):
    """Best-effort: make the key file readable/writable by the owner only.
    Fully reliable on Linux/macOS; on Windows this clears the read-only
    bit at best — real multi-user protection there needs NTFS ACLs."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def _load_or_create_key(users_path) -> bytes:
    kp = _key_path(users_path)
    if kp.exists():
        return kp.read_bytes().strip()
    key = Fernet.generate_key()
    kp.write_bytes(key)
    _lock_down(kp)
    return key


def load_users(users_path) -> dict:
    """Read and decrypt users.json. Returns {} if the file doesn't exist
    or is empty. Transparently handles a pre-existing PLAINTEXT users.json
    (old format) so nothing breaks on upgrade."""
    users_path = Path(users_path)
    if not users_path.exists():
        return {}

    raw = users_path.read_bytes()
    if not raw.strip():
        return {}

    kp = _key_path(users_path)
    if kp.exists():
        try:
            key = _load_or_create_key(users_path)
            decrypted = Fernet(key).decrypt(raw)
            return json.loads(decrypted.decode("utf-8"))
        except InvalidToken:
            # Key file exists but doesn't match this data — don't silently
            # eat the error, the caller needs to know something is wrong
            # rather than getting an empty user list.
            raise ValueError(
                f"Could not decrypt {users_path}: the key in {kp} does not "
                "match this file. If you have a backup key, restore it; "
                "otherwise this file cannot be recovered."
            )

    # No key file yet: this is either a brand-new install, or an old
    # plaintext users.json from before encryption was added. Try plain
    # JSON first (migration path) before giving up.
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError(
            f"{users_path} is not valid JSON and no matching key file "
            f"({kp}) was found — cannot read it."
        )


def save_users(users_path, users: dict) -> None:
    """Encrypt `users` and write it to users_path, with a timestamped
    backup of whatever was there before (also encrypted, since it's
    written as raw bytes)."""
    users_path = Path(users_path)

    if users_path.exists():
        from datetime import datetime
        backup = users_path.with_name(
            users_path.name + f".bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        backup.write_bytes(users_path.read_bytes())

    key = _load_or_create_key(users_path)
    payload = json.dumps(users, indent=2).encode("utf-8")
    token = Fernet(key).encrypt(payload)

    tmp_path = users_path.with_suffix(users_path.suffix + ".tmp")
    tmp_path.write_bytes(token)
    os.replace(tmp_path, users_path)