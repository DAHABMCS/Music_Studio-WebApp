import os
import sys
import time
import uuid
import json
import hmac
import hashlib
import shutil
import socket
import threading
import subprocess
import tempfile
from pathlib import Path
from functools import wraps

from flask import (Flask, render_template, request, jsonify,
                   send_from_directory, session, redirect, url_for)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from subtitle_engine import SubtitleEngine
from user_store import load_users as _load_users_enc, save_users as _save_users_enc

# ============================================================
# APP CONFIG
# ============================================================
app = Flask(__name__)
app.secret_key = "change-this-secret-key-to-something-random"
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# Keep the user logged in across browser restarts.
from datetime import timedelta
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

# ------------------------------------------------------------
# Where are we running from?
#   Dev:    __file__ is app.py, and runtime/ is a sibling folder.
#   Frozen: sys.frozen=True, sys.executable is the .exe, and runtime/
#           was copied next to it by the PyInstaller build. Do NOT use
#           __file__ when frozen — it points inside _internal/.
# ------------------------------------------------------------
def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

BASE_DIR   = _app_root()
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
ASSETS_DIR = BASE_DIR / "assets"
USERS_FILE = BASE_DIR / "users.json"
JOBS_DIR   = BASE_DIR / "jobs"


for d in (UPLOAD_DIR, OUTPUT_DIR, ASSETS_DIR, JOBS_DIR):
    d.mkdir(exist_ok=True)

# Create a default user file if none exists.
# NOTE: change this password immediately after first login.
# users.json is encrypted at rest (see user_store.py) — this writes the
# encrypted form directly, plus its key file (users.json.key), on first run.
DEFAULT_ADMIN_PASSWORD = "change-me-now"
if not USERS_FILE.exists():
    _save_users_enc(USERS_FILE, {
        "admin": {
            "password": generate_password_hash(DEFAULT_ADMIN_PASSWORD),
            "role": "admin",
        }
    })

LOCK = threading.Lock()


# ============================================================
# JOB PERSISTENCE
# ------------------------------------------------------------
# JOBS used to live only in memory, so a server restart wiped every
# in-flight job. It's now persisted so a resumed browser session (or
# a server restart) can pick a job back up.
#
# IMPORTANT: this used to write the ENTIRE JOBS dict — every job from
# every user over the last 24h — to one shared jobs.json file, on
# every single progress checkpoint (see the old _p() callbacks: every
# 10% they called _save_jobs()). Two problems with that:
#   1. One job's checkpoint write re-serialized ALL other jobs' data
#      too, so writes got slower and slower the longer the server had
#      been running and the more job history had piled up — which is
#      exactly the "it didn't used to take this long" symptom.
#   2. The persisted data included the FULL generated SRT transcript
#      text inline (JOBS[job_id]["srt"]), which is pure dead weight on
#      disk — app.js's restoreSession() already re-reads the real SRT
#      file from disk via /api/output_preview, it never reads this
#      field back out of the snapshot.
#
# Fix: each job gets its own small file (jobs/<job_id>.json), so a
# checkpoint only ever writes that one job's small metadata blob —
# writes stay cheap and constant-time regardless of how much job
# history has accumulated. The bulky "srt" text field is stripped
# before writing to disk (still kept in the in-memory JOBS dict for
# the live polling preview during the current process's lifetime).
# ============================================================
_PERSIST_EXCLUDE_KEYS = {"srt"}  # bulky, and not needed on disk — see note above


def _load_jobs():
    jobs = {}
    cutoff = time.time() - 86400
    for f in JOBS_DIR.glob("*.json"):
        try:
            v = json.loads(f.read_text())
            if isinstance(v, dict) and v.get("_ts", 0) > cutoff:
                jobs[f.stem] = v
            else:
                f.unlink(missing_ok=True)  # stale — clean it up
        except Exception as e:
            print(f"[jobs] failed to load {f.name}: {e}")
    return jobs


def _save_job(job_id):
    """Persist ONLY this one job's metadata to its own small file.
    Caller must hold LOCK. O(1) in the size of job history, unlike the
    old whole-dict-every-time approach."""
    try:
        job = JOBS.get(job_id)
        if job is None:
            return
        to_write = {k: v for k, v in job.items() if k not in _PERSIST_EXCLUDE_KEYS}
        target = JOBS_DIR / f"{job_id}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(to_write, default=str))
        tmp.replace(target)
    except Exception as e:
        print(f"[jobs] failed to persist {job_id}: {e}")


JOBS = _load_jobs()


# ============================================================
# LOCAL-MACHINE / NATIVE-FOLDER HELPERS (for /api/browse/<kind>)
# ============================================================
def _local_ip_addresses():
    """Every IP address this machine can be reached at, so we can tell
    whether an incoming request originated on this same machine (as
    opposed to another device on the LAN hitting the server's IP)."""
    ips = {"127.0.0.1", "::1"}
    try:
        hostname = socket.gethostname()
        ips.add(socket.gethostbyname(hostname))
        for info in socket.getaddrinfo(hostname, None):
            ips.add(info[4][0])
    except Exception:
        pass
    return ips


_LOCAL_IPS = _local_ip_addresses()


def _is_local_request():
    return request.remote_addr in _LOCAL_IPS


def _win_bring_explorer_to_front(folder_name, timeout=4.0):
    """Windows only. After os.startfile() the Explorer window often opens
    BEHIND the app (Windows' foreground lock) and just flashes in the
    taskbar. Find the window by its folder name and raise it."""
    try:
        import ctypes
        from ctypes import wintypes as wt
        user32 = ctypes.windll.user32
        enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        want = (folder_name or "").lower()

        def _find():
            hits = []

            def _cb(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                if cls.value != "CabinetWClass":          # an Explorer window
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if want and want in buf.value.lower():
                    hits.append(hwnd)
                return True

            user32.EnumWindows(enum_proc(_cb), 0)
            return hits[0] if hits else None

        hwnd = None
        deadline = time.time() + timeout
        while time.time() < deadline and not hwnd:
            hwnd = _find()
            if not hwnd:
                time.sleep(0.15)
        if not hwnd:
            return
        # A tap of ALT unlocks SetForegroundWindow for this process.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)                    # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    except Exception as e:
        print(f"[browse] could not raise the Explorer window: {e}", flush=True)


def _open_native_folder(path):
    """Open `path` in the OS's native file explorer on THIS machine.
    Only ever call this after confirming the request is local — see
    _is_local_request(). Returns True on success."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # noqa: F821 (Windows-only builtin)
            # Windows will not let a background program take the screen, so
            # Explorer would only FLASH in the taskbar. Bring its window up.
            threading.Thread(target=_win_bring_explorer_to_front,
                             args=(Path(path).name,), daemon=True).start()
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=True)
        else:
            subprocess.run(["xdg-open", str(path)], check=True)
        return True
    except Exception:
        return False


# ============================================================
# USER / AUTH HELPERS
# ------------------------------------------------------------
# users.json is encrypted at rest — see user_store.py (shared with
# User_Management.py, so both programs always agree on the format).
# ============================================================
def load_users():
    try:
        return _load_users_enc(USERS_FILE)
    except Exception:
        return {}


def save_users(users: dict):
    _save_users_enc(USERS_FILE, users)


def get_role(stored) -> str:
    """Legacy plain-string entries default to 'user'."""
    if isinstance(stored, dict):
        return stored.get("role", "user")
    return "user"


def verify_password(password: str, stored) -> bool:
    """
    Accepts either:
      - plain string:  "admin": "admin"
      - dict:          "admin": {"password": "...", "role": "..."}
        where password is either plaintext or a Werkzeug scrypt hash.
    """
    if isinstance(stored, str):
        return hmac.compare_digest(stored, password)

    if isinstance(stored, dict):
        hashed = stored.get("password", "")
        if not hashed:
            return False

        if hashed.startswith("scrypt:"):
            try:
                method, salt, hashval = hashed.split("$", 2)
                _, n, r, p = method.split(":")
                n, r, p = int(n), int(r), int(p)
                derived = hashlib.scrypt(
                    password.encode("utf-8"),
                    salt=salt.encode("utf-8"),
                    n=n, r=r, p=p,
                    dklen=len(hashval) // 2,
                    maxmem=132 * n * r * 2,
                ).hex()
                return hmac.compare_digest(derived, hashval)
            except Exception:
                return False

        # plaintext inside the dict
        return hmac.compare_digest(hashed, password)

    return False


def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("user"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("user"):
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            return jsonify(error="Admin access required"), 403
        return f(*a, **k)
    return wrapper


def user_dirs(username: str):
    """Return (uploads/<user>, outputs/<user>) creating both if needed."""
    u = UPLOAD_DIR / username
    o = OUTPUT_DIR / username
    u.mkdir(parents=True, exist_ok=True)
    o.mkdir(parents=True, exist_ok=True)
    return u, o


def _owns_job(job: dict) -> bool:
    """A user may only see their own jobs (admins see everything)."""
    if session.get("role") == "admin":
        return True
    owner = job.get("user")
    return owner is None or owner == session.get("user")


# ============================================================
# AUTH ROUTES
# ============================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    # Already logged in? Go straight to the dashboard.
    if session.get("user"):
        if request.method == "POST" and request.is_json:
            return jsonify(ok=True, redirect=url_for("dashboard"))
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        # login.html's fetch() posts JSON; support classic form posts too.
        if request.is_json:
            data = request.get_json(silent=True) or {}
        else:
            data = request.form

        u = (data.get("username") or "").strip()
        p = (data.get("password") or "").strip()

        user = load_users().get(u)
        if user is not None and verify_password(p, user):
            session.permanent = True          # survive browser restart
            session["user"] = u
            session["role"] = get_role(user)
            if request.is_json:
                return jsonify(ok=True, redirect=url_for("dashboard"))
            return redirect(url_for("dashboard"))

        error = "Invalid username or password"
        if request.is_json:
            return jsonify(ok=False, error=error), 401

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# ADMIN: USER MANAGEMENT
# ============================================================
@app.route("/admin")
@admin_required
def admin_page():
    return render_template("admin.html", user=session["user"])


@app.route("/admin/api/users")
@admin_required
def admin_list_users():
    users = load_users()
    out = [{"username": name, "role": get_role(stored)}
           for name, stored in users.items()]
    return jsonify(users=sorted(out, key=lambda x: x["username"]))


@app.route("/admin/api/users", methods=["POST"])
@admin_required
def admin_add_user():
    data = request.get_json(force=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "user")

    if not username or not password:
        return jsonify(error="username and password are required"), 400
    if role not in ("user", "admin"):
        return jsonify(error="role must be 'user' or 'admin'"), 400

    users = load_users()
    if username in users:
        return jsonify(error="User already exists"), 400

    users[username] = {
        "password": generate_password_hash(password),
        "role": role,
    }
    save_users(users)
    return jsonify(ok=True, username=username, role=role)


@app.route("/admin/api/users/<username>", methods=["DELETE"])
@admin_required
def admin_delete_user(username):
    users = load_users()
    if username not in users:
        return jsonify(error="User not found"), 404
    if username == session["user"]:
        return jsonify(error="You cannot delete your own account"), 400

    remaining_admins = sum(
        1 for name, stored in users.items()
        if name != username and get_role(stored) == "admin"
    )
    if get_role(users[username]) == "admin" and remaining_admins == 0:
        return jsonify(error="Cannot delete the last admin account"), 400

    del users[username]
    save_users(users)
    return jsonify(ok=True)


@app.route("/admin/api/users/<username>/reset_password", methods=["POST"])
@admin_required
def admin_reset_password(username):
    data = request.get_json(force=True) or {}
    new_password = data.get("password", "")
    if not new_password:
        return jsonify(error="password is required"), 400

    users = load_users()
    if username not in users:
        return jsonify(error="User not found"), 404

    role = get_role(users[username])
    users[username] = {
        "password": generate_password_hash(new_password),
        "role": role,
    }
    save_users(users)
    return jsonify(ok=True)


# ============================================================
# MAIN PAGES
# ============================================================
@app.route("/")
def landing():
    """Public landing page. If already logged in, skip to dashboard."""
    if session.get("user"):
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/dashboard")
@login_required
def dashboard():
    """Protected app — where the actual work happens."""
    return render_template("dashboard.html", user=session["user"])


# ============================================================
# STATIC ASSETS
# ============================================================
@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(ASSETS_DIR, filename)

# ============================================================
# USER MANUAL (opens inline in a new browser tab)
# ============================================================
MANUAL_NAME = "Music_Studio_User_Manual.pdf"

@app.route("/help")
@login_required
def help_manual():
    # Look next to the exe/app first, then in assets/ and docs/
    for folder in (BASE_DIR, ASSETS_DIR, BASE_DIR / "docs"):
        if (folder / MANUAL_NAME).is_file():
            return send_from_directory(folder, MANUAL_NAME,
                                       mimetype="application/pdf",
                                       as_attachment=False)
    return "User manual not found. Place Music_Studio_User_Manual.pdf next to the app.", 404

# ============================================================
# STARTUP GUIDE (button details & flow charts — opened from "Guide me")
# ============================================================
STARTUP_GUIDE_NAME = "startup_Guide.pdf"

@app.route("/startup-guide")
@login_required
def startup_guide():
    # Same lookup order as the user manual: next to the exe/app first,
    # then assets/ and docs/
    for folder in (BASE_DIR, ASSETS_DIR, BASE_DIR / "docs"):
        if (folder / STARTUP_GUIDE_NAME).is_file():
            return send_from_directory(folder, STARTUP_GUIDE_NAME,
                                       mimetype="application/pdf",
                                       as_attachment=False)
    return "Startup guide not found. Place startup_Guide.pdf next to the app.", 404


# ============================================================
# FILE BROWSER (upload/output listings used by the dashboard UI)
# ============================================================
@app.route("/api/list_files")
@login_required
def list_files():
    updir, _ = user_dirs(session["user"])
    files = []
    for p in sorted(updir.glob("*"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file():
            files.append({
                "name":    p.name,
                "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
                "path":    str(p),
            })
    return jsonify(files=files)


@app.route("/api/list_outputs")
@login_required
def list_outputs():
    _, outdir = user_dirs(session["user"])
    files = []
    for p in sorted(outdir.rglob("*"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file():
            files.append({
                "name":    p.name,
                "rel":     str(p.relative_to(outdir)),
                "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
            })
    return jsonify(files=files)


@app.route("/api/files")
@login_required
def api_files():
    """Used by app.js — returns uploads + outputs."""
    updir, outdir = user_dirs(session["user"])
    return jsonify(
        uploads=sorted(p.name for p in updir.iterdir() if p.is_file()),
        outputs=sorted(p.name for p in outdir.iterdir() if p.is_file()),
    )


@app.route("/api/pick_input", methods=["POST"])
@login_required
def pick_input():
    name = request.get_json(force=True).get("name")
    if not name:
        return jsonify(error="no name"), 400
    updir, _ = user_dirs(session["user"])
    path = updir / name
    if not path.exists():
        return jsonify(error="not found"), 404
    return jsonify(path=str(path), name=name)


# ============================================================
# UPLOAD
# ============================================================
@app.route("/api/upload", methods=["POST"])
@login_required
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="No file"), 400

    updir, _ = user_dirs(session["user"])
    safe = secure_filename(f.filename)
    dest = updir / safe
    f.save(dest)

    return jsonify(
        file_id=uuid.uuid4().hex[:8],
        name=safe,
        filename=safe,
        path=safe,                                   # relative to user's upload dir
        size=dest.stat().st_size,
        size_mb=round(dest.stat().st_size / (1024 * 1024), 2),
        srt_path=f"{Path(safe).stem}.srt",
        # Complete path on the server where the SRT will be written
        # (see _run_pipeline: outputs/<user>/SRT/<stem>/<stem>.srt).
        srt_full_path=str((OUTPUT_DIR / session["user"] / "SRT" / Path(safe).stem
                           / f"{Path(safe).stem}.srt").resolve()),
    )


# ============================================================
# GENERATE (runs Create_SRT.SubtitleGenerator headless)
# ============================================================
@app.route("/api/generate", methods=["POST"])
@login_required
def generate():
    data = request.get_json(force=True)
    input_path = data.get("input_path")
    if not input_path or not Path(input_path).exists():
        return jsonify(error="Invalid input file"), 400

    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "progress": 0,
            "status": "Ready",
            "srt": "",
            "output": "",
            "input": input_path,
            "user": session["user"],
            "_ts": time.time(),
        }
        _save_job(job_id)

    opts = {
        "language":       data.get("language", "auto"),
        "model":          data.get("model", "large-v3"),
        "chord_method":   data.get("chords", "advanced"),
        "isolate_vocals": bool(data.get("isolate_vocals", True)),
        "extract_guitar": bool(data.get("extract_guitar", False)),
    }

    threading.Thread(
        target=_run_pipeline, args=(job_id, opts), daemon=True
    ).start()

    return jsonify(job_id=job_id)


# Alias so app.js's /api/generate_srt works too
@app.route("/api/generate_srt", methods=["POST"])
@login_required
def generate_srt_alias():
    data = request.get_json(force=True) or {}
    updir, _ = user_dirs(session["user"])
    input_path = str(updir / data.get("path", ""))
    if not Path(input_path).exists():
        return jsonify(error="File not found"), 404

    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "progress": 0,
            "status":   "Ready",
            "srt":      "",
            "output":   "",
            "input":    input_path,
            "user":     session["user"],
            "_ts":      time.time(),
        }
        _save_job(job_id)
    opts = {
        "language":       data.get("language", "auto"),
        "model":          data.get("model", "large-v3"),
        "chord_method":   data.get("chord_method", "advanced"),
        "isolate_vocals": bool(data.get("isolate_vocals", True)),
        "extract_guitar": bool(data.get("extract_guitar", False)),
    }

    threading.Thread(
        target=_run_pipeline, args=(job_id, opts), daemon=True
    ).start()

    return jsonify(job_id=job_id)


def _run_pipeline(job_id, opts):
    """
    Runs the pure-logic SubtitleEngine (no Tkinter) on the input file.
    Reports progress into JOBS.
    """
    try:
        engine = SubtitleEngine()

        with LOCK:
            input_path = JOBS[job_id]["input"]
            user = JOBS[job_id].get("user", "shared")

        stem = Path(input_path).stem
        song_folder = OUTPUT_DIR / user / "SRT" / stem
        song_folder.mkdir(parents=True, exist_ok=True)

        out_path = song_folder / f"{stem}.srt"

        def _p(value, status):
            with LOCK:
                JOBS[job_id]["progress"] = float(value)
                JOBS[job_id]["status"]   = status
                JOBS[job_id]["_ts"]      = time.time()
                # persist every 10% to avoid disk thrash
                if int(value) % 10 == 0:
                    _save_job(job_id)

        srt_text = engine.generate_srt(
            str(input_path), str(out_path),
            model_size=opts["model"],
            language=opts["language"],
            isolate_vocals=opts["isolate_vocals"],
            progress_cb=_p,
        )

        with LOCK:
            JOBS[job_id]["srt"]      = srt_text
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["status"]   = "Complete!"
            JOBS[job_id]["output"]   = str(out_path)
            JOBS[job_id]["_ts"]      = time.time()
            _save_job(job_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"]   = f"Error: {e}"
            JOBS[job_id]["progress"] = 0
            JOBS[job_id]["_ts"]      = time.time()
            _save_job(job_id)


@app.route("/api/generate_karaoke", methods=["POST"])
@login_required
def generate_karaoke():
    return _generate_video(request, keep_vocals=False)


@app.route("/api/generate_lyric_video", methods=["POST"])
@login_required
def generate_lyric_video():
    return _generate_video(request, keep_vocals=True)


def _generate_video(req, keep_vocals: bool):
    """
    Shared handler for karaoke (no vocals) and lyric video (vocals kept).
    Confirmed against the real subtitle_engine.py: SubtitleEngine has
    build_karaoke_video(background_path, audio_path, srt_path,
    video_output_path, background_is_video, progress_cb) — it does NOT
    remove vocals itself, it just muxes whatever audio you hand it. So
    for karaoke we run separate_vocals_stem() first (Demucs) and hand it
    the resulting no_vocals.wav; for lyric video we hand it the original
    audio untouched.
    """
    data = req.get_json(force=True) or {}
    updir, outdir = user_dirs(session["user"])

    input_path = updir / data.get("path", "")
    if not input_path.exists():
        return jsonify(error="Input file not found"), 404

    srt_name = data.get("srt", "")
    srt_path = outdir / srt_name
    if not srt_path.exists():
        return jsonify(error="SRT not found — generate subtitles first"), 404

    background = data.get("background")
    background_path = None
    if background:
        background_path = updir / background
        if not background_path.exists():
            return jsonify(error="Background file not found"), 404

    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "progress": 0,
            "status":   "Ready",
            "srt":      "",
            "output":   "",
            "input":    str(input_path),
            "user":     session["user"],
            "_ts":      time.time(),
        }
        _save_job(job_id)

    threading.Thread(
        target=_run_video_job,
        args=(job_id, str(input_path), str(srt_path),
              str(background_path) if background_path else None,
              keep_vocals),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id)


# ============================================================
# LYRIC SHEET PDF  (second PDF produced by the lyrics export)
# ------------------------------------------------------------
# The existing export (SubtitleEngine.export_lyrics_and_mp3) makes the
# chord-annotated lyrics PDF. This builds an ADDITIONAL, publisher-style
# lyric sheet from the same SRT: title, artist, key/tempo/length line,
# labelled sections (Verse / Chorus / Bridge) in a left gutter, and a
# credits + rights footer. Output: <stem>_lyric_sheet.pdf next to the
# other lyrics outputs.
# ============================================================
import re as _re
import difflib as _difflib
from datetime import datetime as _dt


_TITLE_WORDS = (r"karaoke|instrumental|backing\s*track|no[\s_]?vocals?|minus[\s_]?one|"
                r"sing[\s_]?along|lyrics?")
_TITLE_SUFFIX_RE = _re.compile(
    r"[\s_\-\u2013\u2014:|]*(" + _TITLE_WORDS + r")([\s_]*(version|track|video|mix))?[\s_\-:|]*$", _re.I)
_TITLE_PREFIX_RE = _re.compile(
    r"^[\s_\-:|]*(" + _TITLE_WORDS + r")([\s_]*(version|track|video|mix))?[\s_]*[\-\u2013\u2014:|]+[\s_]*", _re.I)
_TITLE_BRACKET_RE = _re.compile(
    r"[\s_]*[\[(][^\])]*\b(" + _TITLE_WORDS + r")\b[^\])]*[\])]", _re.I)


def _display_title(stem):
    """File name -> song title for PDF headings. The karaoke export names its file
    '<song> karaoke'; words like karaoke / instrumental / backing track / lyrics are
    never part of the printed title (trailing, leading, or in (brackets))."""
    original = str(stem).replace("_", " ").strip()
    t = original
    prev = None
    while prev != t:
        prev = t
        t = _TITLE_BRACKET_RE.sub("", t)
        t = _TITLE_PREFIX_RE.sub("", t)
        t = _TITLE_SUFFIX_RE.sub("", t).strip()
    return t or original


_CLOSING_LINE_RE = _re.compile(
    r"(thank(s| you)( so much)?( very much)? for (watching|listening|viewing)|"
    r"(like|please)[ ,&and]*subscribe|subscribe to|sing[ -]?along|"
    r"karaoke (version|track|video)|backing track|instrumental version)", _re.I)


def _is_marker_line(text):
    """[Instrumental], (music), 'Thank you for watching!' ... : not lyrics."""
    t = text.strip()
    if _re.fullmatch(r"[\[(].*[\])]", t):
        return True
    return len(t) <= 70 and bool(_CLOSING_LINE_RE.search(t))


def _parse_srt_blocks(srt_text):
    """Minimal standalone SRT parser -> [(start_sec, end_sec, text)]."""
    def _t(s):
        h, m, rest = s.strip().replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)

    out = []
    for block in _re.split(r"\r?\n\s*\r?\n", srt_text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        for i, l in enumerate(lines):
            if "-->" in l:
                try:
                    a, b = l.split("-->")
                    text = " ".join(x.strip() for x in lines[i + 1:])
                    text = _re.sub(r"<[^>]+>", "", text).strip()
                    if text:
                        out.append((_t(a), _t(b), text))
                except Exception:
                    pass
                break
    return out


def _group_into_sections(blocks, gap=5.5, max_lines=12):
    """Split cues into stanzas on silent gaps, then label them.
    Stanzas that (near-)repeat are Chorus; a one-off stanza between two
    choruses after at least two verses is Bridge; the rest are Verse N."""
    stanzas, cur = [], []
    for i, (s, e, t) in enumerate(blocks):
        if cur and (s - blocks[i - 1][1] > gap or len(cur) >= max_lines):
            stanzas.append(cur)
            cur = []
        cur.append(t)
    if cur:
        stanzas.append(cur)
    merged = []
    for st in stanzas:                      # a lone line belongs to the stanza before it
        if merged and len(st) == 1:
            merged[-1].extend(st)
        else:
            merged.append(st)
    stanzas = merged
    norm = [" ".join(st).lower() for st in stanzas]
    is_chorus = [False] * len(stanzas)
    for i in range(len(stanzas)):
        for j in range(i + 1, len(stanzas)):
            if _difflib.SequenceMatcher(None, norm[i], norm[j]).ratio() >= 0.75:
                is_chorus[i] = is_chorus[j] = True

    labelled, verse_n = [], 0
    chorus_idx = [i for i, c in enumerate(is_chorus) if c]
    for i, st in enumerate(stanzas):
        if is_chorus[i]:
            labelled.append(("Chorus", st))
        elif (verse_n >= 2 and chorus_idx and chorus_idx[0] < i < chorus_idx[-1]
              and len(st) <= 4):
            labelled.append(("Bridge", st))
        else:
            verse_n += 1
            labelled.append((f"Verse {verse_n}", st))
    return labelled


def _estimate_key_tempo(audio_path, max_seconds=120):
    """Best-effort key + tempo with librosa. Returns (key_str|None, bpm|None)."""
    try:
        import numpy as np
        import librosa
        y, sr = librosa.load(audio_path, sr=22050, mono=True, duration=max_seconds)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = int(round(float(np.atleast_1d(tempo)[0])))
        chroma = librosa.feature.chroma_cqt(y=librosa.effects.harmonic(y), sr=sr).mean(axis=1)
        maj = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
        mnr = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
        names = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
        best = (-2, None)
        for k in range(12):
            for prof, mode in ((maj, "major"), (mnr, "minor")):
                c = np.corrcoef(chroma, np.roll(prof, k))[0, 1]
                if c > best[0]:
                    best = (c, f"{names[k]} {mode}")
        return best[1], bpm
    except Exception as e:
        print(f"[lyric_sheet] key/tempo estimate skipped: {e}")
        return None, None


def _pick_unicode_fonts():
    """Register fonts for the lyric sheet. Returns
    (regular, bold, italic, sans, has_phonogram_symbol).
    Search order: fonts shipped in <app>/assets/fonts, Linux DejaVu,
    Windows Times New Roman/Arial (cover Latin, Greek, Cyrillic, Arabic,
    Hebrew), macOS. The phonogram sign (U+2117) is drawn from a separate
    symbol font ("LS-Sym") only if one on this PC really contains it;
    otherwise the sheet prints "(P)". Built-in Times/Helvetica is the
    last resort, so this never fails."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    win = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    bundled = ASSETS_DIR / "fonts"
    dejavu = Path("/usr/share/fonts/truetype/dejavu")
    families = [
        (bundled / "DejaVuSerif.ttf", bundled / "DejaVuSerif-Bold.ttf",
         bundled / "DejaVuSerif-Italic.ttf", bundled / "DejaVuSans.ttf"),
        (dejavu / "DejaVuSerif.ttf", dejavu / "DejaVuSerif-Bold.ttf",
         dejavu / "DejaVuSerif-Italic.ttf", dejavu / "DejaVuSans.ttf"),
        (win / "times.ttf", win / "timesbd.ttf", win / "timesi.ttf", win / "arial.ttf"),
        (Path("/Library/Fonts/Times New Roman.ttf"), Path("/Library/Fonts/Times New Roman Bold.ttf"),
         Path("/Library/Fonts/Times New Roman Italic.ttf"), Path("/Library/Fonts/Arial.ttf")),
    ]
    names = ("LS-Serif", "LS-Serif-B", "LS-Serif-I", "LS-Sans")
    result = ("Times-Roman", "Times-Bold", "Times-Italic", "Helvetica")
    for fam in families:
        if not fam[0].is_file():
            continue
        try:
            for nm, path, fallback in zip(names, fam, (fam[0], fam[0], fam[0], fam[0])):
                pdfmetrics.registerFont(TTFont(nm, str(path if path.is_file() else fallback)))
            result = names
            break
        except Exception as e:
            print(f"[lyric_sheet] font family skipped ({fam[0]}): {e}")

    has_p = False
    sym_candidates = [bundled / "DejaVuSans.ttf", dejavu / "DejaVuSans.ttf",
                      win / "seguisym.ttf", win / "arialuni.ttf", win / "segoeui.ttf",
                      Path("/Library/Fonts/Arial Unicode.ttf")]
    for f in sym_candidates:
        try:
            if f.is_file():
                tt = TTFont("LS-Sym", str(f))
                if 0x2117 in tt.face.charToGlyph:
                    pdfmetrics.registerFont(tt)
                    has_p = True
                    break
        except Exception:
            continue
    return (*result, has_p)


def export_lyric_sheet_pdf(srt_path, audio_path, stem, out_path, meta=None):
    """Build the lyric-sheet PDF. `meta` may supply: title, artist, key,
    tempo, duration (seconds), writers, producer, publisher, label, year.
    Credit lines with no data are left out rather than printed blank."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame,
                                    Paragraph, Spacer, Table, TableStyle,
                                    HRFlowable, KeepTogether)
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from xml.sax.saxutils import escape

    meta = dict(meta or {})
    blocks = _parse_srt_blocks(Path(srt_path).read_text(encoding="utf-8"))
    if not blocks:
        raise RuntimeError("No cues found in SRT — cannot build lyric sheet.")
    blocks = [b for b in blocks if not _is_marker_line(b[2])]
    if not blocks:
        raise RuntimeError("Only instrumental markers found - no lyrics to print.")
    sections = _group_into_sections(blocks)

    key, bpm = meta.get("key"), meta.get("tempo")
    if not (key and bpm):
        k2, b2 = _estimate_key_tempo(audio_path)
        key, bpm = key or k2, bpm or b2
    dur = meta.get("duration") or blocks[-1][1]
    dur_s = f"{int(dur // 60)}:{int(dur % 60):02d}"

    title = meta.get("title") or _display_title(stem)
    artist = meta.get("artist") or ""
    year = meta.get("year") or _dt.now().year

    F, FB, FI, FS, has_p = _pick_unicode_fonts()
    ink, soft, faint = colors.HexColor("#1a1a1a"), colors.HexColor("#555555"), colors.HexColor("#888888")
    accent = colors.HexColor("#185FA5")

    st_title = ParagraphStyle("t", fontName=FB, fontSize=26, leading=31, alignment=TA_CENTER, textColor=ink)
    st_artist = ParagraphStyle("a", fontName=FS, fontSize=11, leading=14, alignment=TA_CENTER, textColor=soft)
    st_meta = ParagraphStyle("m", fontName=FS, fontSize=8.5, leading=11, alignment=TA_CENTER, textColor=faint)
    st_lbl = ParagraphStyle("l", fontName=FS, fontSize=8, leading=13, textColor=faint)
    st_lbl_ch = ParagraphStyle("lc", parent=st_lbl, textColor=accent)
    st_body = ParagraphStyle("b", fontName=F, fontSize=12, leading=21, textColor=ink)
    st_chor = ParagraphStyle("c", parent=st_body, fontName=FB)
    st_brdg = ParagraphStyle("br", parent=st_body, fontName=FI, textColor=soft)
    st_note = ParagraphStyle("n", fontName=FS, fontSize=8.5, leading=12, textColor=faint)
    st_credit = ParagraphStyle("cr", fontName=FS, fontSize=8.5, leading=13, textColor=soft)
    st_credit_r = ParagraphStyle("crr", parent=st_credit, alignment=TA_RIGHT)

    story = [Paragraph(escape(title), st_title)]
    if artist:
        story += [Spacer(1, 4), Paragraph(escape(artist), st_artist)]
    bits = []
    if key: bits.append(f"Key: {key}")
    if bpm: bits.append(f"Tempo: {bpm} BPM")
    bits.append(dur_s)
    story += [Spacer(1, 6), Paragraph(" &middot; ".join(bits), st_meta), Spacer(1, 18)]

    rows = []
    for i, (label, lines) in enumerate(sections):
        is_ch = label == "Chorus"
        body_style = st_chor if is_ch else (st_brdg if label == "Bridge" else st_body)
        lines = [escape(l) for l in lines]
        body = Paragraph("<br/>".join(lines), body_style)
        rows.append([Paragraph(label.upper(), st_lbl_ch if is_ch else st_lbl), body])

    tbl = Table(rows, colWidths=[24 * mm, None])
    ts = [("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
          ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]
    for r, (label, _) in enumerate(sections):
        if label == "Chorus":
            ts += [("LINEBEFORE", (1, r), (1, r), 1.2, accent), ("LEFTPADDING", (1, r), (1, r), 8)]
    tbl.setStyle(TableStyle(ts))
    story.append(tbl)

    left, right = [], []
    if meta.get("writers"):   left.append(f"Words and music by {escape(meta['writers'])}")
    if meta.get("producer"):  left.append(f"Produced by {escape(meta['producer'])}")
    if meta.get("publisher"): right.append(f"Published by {escape(meta['publisher'])}")
    if meta.get("label"):
        right.append(('<font name="LS-Sym">\u2117</font>' if has_p else "(P)") + f" {year} {escape(meta['label'])}")
    footer = [Spacer(1, 14), HRFlowable(width="100%", thickness=0.5, color=faint), Spacer(1, 6)]
    if left or right:
        footer.append(Table([[Paragraph("<br/>".join(left), st_credit),
                              Paragraph("<br/>".join(right), st_credit_r)]],
                            colWidths=[None, None],
                            style=[("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        footer.append(Spacer(1, 6))
    footer.append(Paragraph("All rights reserved. Used by permission. "
                            "Reproduction without license is prohibited.", st_note))
    story.append(KeepTogether(footer))

    label_txt = meta.get("label") or ""
    def _page(c, d):
        c.saveState()
        c.setFont(FS, 7.5); c.setFillColor(faint)
        if label_txt:
            c.drawString(20 * mm, A4[1] - 12 * mm, label_txt.upper())
            c.setStrokeColor(faint); c.setLineWidth(0.4)
            c.line(20 * mm, A4[1] - 14 * mm, A4[0] - 20 * mm, A4[1] - 14 * mm)
        c.drawCentredString(A4[0] / 2, 10 * mm, str(d.page))
        c.restoreState()

    doc = BaseDocTemplate(str(out_path), pagesize=A4, title=f"{title} - lyric sheet",
                          leftMargin=20 * mm, rightMargin=20 * mm,
                          topMargin=24 * mm, bottomMargin=18 * mm)
    doc.addPageTemplates([PageTemplate(id="p", onPage=_page,
                          frames=[Frame(20 * mm, 18 * mm, A4[0] - 40 * mm, A4[1] - 42 * mm, id="f",
                                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)])])
    doc.build(story)
    return len(sections)


_SHEET_META_KEYS = ("title", "artist", "key", "tempo", "writers",
                    "producer", "publisher", "label", "year")


def _sheet_meta_from_request(data):
    """Optional lyric-sheet fields from the POST body (all may be omitted)."""
    meta = {k: str(data[k]).strip() for k in _SHEET_META_KEYS
            if data.get(k) not in (None, "")}
    if "tempo" in meta:
        try:
            meta["tempo"] = int(float(meta["tempo"]))
        except ValueError:
            meta.pop("tempo")
    return meta


def _make_lyric_sheet(srt_path, input_path, stem, folder, meta):
    """Build the extra PDF. Never lets a failure here kill the main export."""
    out = Path(folder) / f"{stem}_lyric_sheet.pdf"
    try:
        export_lyric_sheet_pdf(srt_path, input_path, stem, out, meta)
        return str(out)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[lyric_sheet] skipped: {e}", flush=True)
        return None


@app.route("/api/export_lyrics", methods=["POST"])
@login_required
def generate_lyrics():
    """Export Lyrics (PDF+MP3): chord-annotated lyrics PDF + an MP3 copy
    of the track. Needs an existing SRT (for the cues/timing) — same
    precondition as the karaoke/lyric-video routes."""
    data = request.get_json(force=True) or {}
    updir, outdir = user_dirs(session["user"])

    input_path = updir / data.get("path", "")
    if not input_path.exists():
        return jsonify(error="Input file not found"), 404

    srt_name = data.get("srt", "")
    srt_path = outdir / srt_name
    if not srt_path.exists():
        return jsonify(error="SRT not found — generate subtitles first"), 404

    chord_method = data.get("chord_method", "advanced")
    sheet_meta = _sheet_meta_from_request(data)

    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "progress": 0,
            "status":   "Ready",
            "srt":      "",
            "output":   "",
            "input":    str(input_path),
            "user":     session["user"],
            "_ts":      time.time(),
        }
        _save_job(job_id)

    threading.Thread(
        target=_run_lyrics_job,
        args=(job_id, str(input_path), str(srt_path), chord_method, sheet_meta),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id)


def _run_lyrics_job(job_id, input_path, srt_path, chord_method, sheet_meta=None):
    try:
        engine = SubtitleEngine()

        with LOCK:
            user = JOBS[job_id].get("user", "shared")

        def _p(value, status):
            with LOCK:
                JOBS[job_id]["progress"] = float(value)
                JOBS[job_id]["status"]   = status
                JOBS[job_id]["_ts"]      = time.time()
                if int(value) % 10 == 0:
                    _save_job(job_id)

        _p(5, "Reading subtitles...")
        srt_text = Path(srt_path).read_text(encoding="utf-8")
        cues = SubtitleEngine._parse_srt_cues(srt_text)
        if not cues:
            raise RuntimeError("No cues found in SRT — nothing to export.")

        stem = Path(input_path).stem
        lyrics_folder = OUTPUT_DIR / user / "Lyrics" / stem
        lyrics_folder.mkdir(parents=True, exist_ok=True)

        pdf_path = lyrics_folder / f"{stem}_lyrics.pdf"
        mp3_path = lyrics_folder / f"{stem}.mp3"

        detected, total = engine.export_lyrics_and_mp3(
            input_path, cues, _display_title(stem),
            str(pdf_path), str(mp3_path),
            chord_method=chord_method,
            status_callback=lambda msg: _p(50, msg),
        )

        # Additional PDF: publisher-style lyric sheet (see export_lyric_sheet_pdf)
        _p(90, "Building lyric sheet PDF...")
        sheet_pdf = _make_lyric_sheet(srt_path, input_path, stem, lyrics_folder, sheet_meta)

        with LOCK:
            JOBS[job_id]["progress"]   = 100
            JOBS[job_id]["status"]     = "Complete!"
            JOBS[job_id]["output"]     = str(pdf_path)
            JOBS[job_id]["pdf"]        = str(pdf_path)
            JOBS[job_id]["sheet_pdf"]  = sheet_pdf
            JOBS[job_id]["mp3"]        = str(mp3_path)
            JOBS[job_id]["chords_detected"] = f"{detected}/{total}"
            JOBS[job_id]["_ts"]        = time.time()
            _save_job(job_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"]   = f"Error: {e}"
            JOBS[job_id]["progress"] = 0
            JOBS[job_id]["_ts"]      = time.time()
            _save_job(job_id)


@app.route("/api/export_guitar_tab", methods=["POST"])
@login_required
def generate_tab():
    """Export Guitar Solo Tab (PDF). Optional start_sec/end_sec select the
    solo range; omitted end_sec means 'to the end of the track'. Honors
    the dashboard's Extract Guitar checkbox, sent by app.js as
    use_demucs, to decide whether Demucs isolates the guitar stem first."""
    data = request.get_json(force=True) or {}
    updir, outdir = user_dirs(session["user"])

    input_path = updir / data.get("path", "")
    if not input_path.exists():
        return jsonify(error="Input file not found"), 404

    start_sec = float(data.get("start_sec", 0.0))
    end_sec = data.get("end_sec")
    end_sec = float(end_sec) if end_sec is not None else None
    use_demucs = bool(data.get("use_demucs", True))

    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "progress": 0,
            "status":   "Ready",
            "srt":      "",
            "output":   "",
            "input":    str(input_path),
            "user":     session["user"],
            "_ts":      time.time(),
        }
        _save_job(job_id)

    threading.Thread(
        target=_run_tab_job,
        args=(job_id, str(input_path), start_sec, end_sec, use_demucs),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id)


def _run_tab_job(job_id, input_path, start_sec, end_sec, use_demucs):
    try:
        engine = SubtitleEngine()

        with LOCK:
            user = JOBS[job_id].get("user", "shared")

        def _p(value, status):
            with LOCK:
                JOBS[job_id]["progress"] = float(value)
                JOBS[job_id]["status"]   = status
                JOBS[job_id]["_ts"]      = time.time()
                if int(value) % 10 == 0:
                    _save_job(job_id)

        resolved_end = end_sec
        if resolved_end is None:
            _p(2, "Checking track length...")
            resolved_end = engine.get_audio_duration(input_path)

        stem = Path(input_path).stem
        tabs_folder = OUTPUT_DIR / user / "Tabs" / stem
        tabs_folder.mkdir(parents=True, exist_ok=True)

        pdf_path = tabs_folder / f"{stem}_solo_tab.pdf"

        engine.export_guitar_tab_pipeline(
            input_path, start_sec, resolved_end, stem, str(pdf_path),
            use_demucs=use_demucs,
            progress_cb=_p,
        )

        with LOCK:
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["status"]   = "Complete!"
            JOBS[job_id]["output"]   = str(pdf_path)
            JOBS[job_id]["pdf"]      = str(pdf_path)
            JOBS[job_id]["_ts"]      = time.time()
            _save_job(job_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"]   = f"Error: {e}"
            JOBS[job_id]["progress"] = 0
            JOBS[job_id]["_ts"]      = time.time()
            _save_job(job_id)


@app.route("/api/export_lyrics_and_tab", methods=["POST"])
@login_required
def generate_lyrics_and_tab():
    """Export Lyrics & Tab (PDF+MP3): the dashboard's "Export Lyrics"
    and "Export Guitar Solo Tab" buttons were merged into one, so this
    runs both pipelines back-to-back as a single job. Needs an
    existing SRT (for the lyrics half), same precondition as the old
    /api/export_lyrics. Honors the Extract Guitar checkbox for the tab
    half, same as the old /api/export_guitar_tab."""
    data = request.get_json(force=True) or {}
    updir, outdir = user_dirs(session["user"])

    input_path = updir / data.get("path", "")
    if not input_path.exists():
        return jsonify(error="Input file not found"), 404

    srt_name = data.get("srt", "")
    srt_path = outdir / srt_name
    if not srt_path.exists():
        return jsonify(error="SRT not found — generate subtitles first"), 404

    chord_method = data.get("chord_method", "advanced")
    start_sec = float(data.get("start_sec", 0.0))
    end_sec = data.get("end_sec")
    end_sec = float(end_sec) if end_sec is not None else None
    use_demucs = bool(data.get("use_demucs", True))
    sheet_meta = _sheet_meta_from_request(data)

    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "progress": 0,
            "status":   "Ready",
            "srt":      "",
            "output":   "",
            "input":    str(input_path),
            "user":     session["user"],
            "_ts":      time.time(),
        }
        _save_job(job_id)

    threading.Thread(
        target=_run_lyrics_and_tab_job,
        args=(job_id, str(input_path), str(srt_path), chord_method,
              start_sec, end_sec, use_demucs, sheet_meta),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id)


def _run_lyrics_and_tab_job(job_id, input_path, srt_path, chord_method,
                             start_sec, end_sec, use_demucs, sheet_meta=None):
    """Runs the lyrics export (0-50% of the progress bar) then the
    guitar tab export (50-100%). Outputs still land in their original,
    separate folders — Lyrics/<stem>/ and Tabs/<stem>/ — so the
    combined "Browse Lyrics & Tab Folder" button can just list both
    (see browse_folder's 'lyrics_tab' kind)."""
    try:
        engine = SubtitleEngine()

        with LOCK:
            user = JOBS[job_id].get("user", "shared")

        def _p(value, status):
            with LOCK:
                JOBS[job_id]["progress"] = float(value)
                JOBS[job_id]["status"]   = status
                JOBS[job_id]["_ts"]      = time.time()
                if int(value) % 10 == 0:
                    _save_job(job_id)

        stem = Path(input_path).stem

        # ---- Part 1: Export Lyrics (PDF + MP3) — 0% to 50% ----
        _p(2, "Reading subtitles...")
        srt_text = Path(srt_path).read_text(encoding="utf-8")
        cues = SubtitleEngine._parse_srt_cues(srt_text)
        if not cues:
            raise RuntimeError("No cues found in SRT — nothing to export.")

        lyrics_folder = OUTPUT_DIR / user / "Lyrics" / stem
        lyrics_folder.mkdir(parents=True, exist_ok=True)
        lyrics_pdf = lyrics_folder / f"{stem}_lyrics.pdf"
        lyrics_mp3 = lyrics_folder / f"{stem}.mp3"

        detected, total = engine.export_lyrics_and_mp3(
            input_path, cues, _display_title(stem),
            str(lyrics_pdf), str(lyrics_mp3),
            chord_method=chord_method,
            status_callback=lambda msg: _p(25, msg),
        )
        _p(40, "Building lyric sheet PDF...")
        sheet_pdf = _make_lyric_sheet(srt_path, input_path, stem, lyrics_folder, sheet_meta)
        _p(50, "Lyrics done — starting guitar solo tab...")

        # ---- Part 2: Export Guitar Solo Tab (PDF) — 50% to 100% ----
        resolved_end = end_sec
        if resolved_end is None:
            _p(52, "Checking track length...")
            resolved_end = engine.get_audio_duration(input_path)

        tabs_folder = OUTPUT_DIR / user / "Tabs" / stem
        tabs_folder.mkdir(parents=True, exist_ok=True)
        tab_pdf = tabs_folder / f"{stem}_solo_tab.pdf"

        engine.export_guitar_tab_pipeline(
            input_path, start_sec, resolved_end, stem, str(tab_pdf),
            use_demucs=use_demucs,
            # engine calls this with its own 0-100 scale for the tab
            # stage alone — rescale it into the 50-100 half we own.
            progress_cb=lambda value, status: _p(50 + float(value) / 2, status),
        )

        with LOCK:
            JOBS[job_id]["progress"]   = 100
            JOBS[job_id]["status"]     = "Complete!"
            JOBS[job_id]["output"]     = str(lyrics_pdf)
            JOBS[job_id]["pdf"]        = str(lyrics_pdf)
            JOBS[job_id]["sheet_pdf"]  = sheet_pdf
            JOBS[job_id]["mp3"]        = str(lyrics_mp3)
            JOBS[job_id]["tab_pdf"]    = str(tab_pdf)
            JOBS[job_id]["chords_detected"] = f"{detected}/{total}"
            JOBS[job_id]["_ts"]        = time.time()
            _save_job(job_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"]   = f"Error: {e}"
            JOBS[job_id]["progress"] = 0
            JOBS[job_id]["_ts"]      = time.time()
            _save_job(job_id)


# ============================================================
# FULL TRANSCRIPTION  (TAB + MIDI + Chords via MUSIC.py)
# ------------------------------------------------------------
# Ported from the desktop app's run_full_music_transcription /
# _process_full_music_transcription. Same 8-stage MUSIC.py pipeline,
# re-plumbed onto the JOBS dict + _p(progress, status) pattern used by
# every other job in this file.
#
# Output layout:
#   outputs/<user>/Transcription/<song>/
#       01_INPUT_AUDIO.wav
#       02_INSTRUMENTAL_STEM.wav
#       01_GUITAR_SOLO_TAB.pdf
#       02_RHYTHM_CHORDS.pdf
#       03_GUITAR_SOLO.mid
#       04_RHYTHM_CHORDS.mid
#       TRANSCRIPTION_REPORT.txt
#
# Matches the "Browse Transcription" button in dashboard.html, which
# already requests /api/browse/transcription and looks in the
# Transcription/ folder.
# ============================================================

@app.route("/api/full_transcription", methods=["POST"])
@login_required
def full_transcription():
    data = request.get_json(force=True) or {}
    updir, _ = user_dirs(session["user"])

    input_path = updir / data.get("path", "")
    if not input_path.exists():
        return jsonify(error="Input file not found"), 404

    # Fail fast with a clear message if MUSIC.py is missing or the
    # optional deps it needs aren't installed — otherwise the worker
    # thread dies after the UI has already gone into "processing".
    music_py = BASE_DIR / "MUSIC.py"
    if not music_py.exists():
        return jsonify(error=(
            "MUSIC.py not found next to app.py. "
            "Full transcription requires it."
        )), 500

    missing = []
    for mod in ("numpy", "librosa", "soundfile", "music21", "reportlab"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return jsonify(error=(
            "Full transcription requires these packages: "
            + ", ".join(missing)
            + ". Install with: pip install " + " ".join(missing)
        )), 500

    if shutil.which("ffmpeg") is None:
        return jsonify(error=(
            "ffmpeg is required for full transcription and wasn't found on PATH."
        )), 500

    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "progress": 0,
            "status":   "Ready",
            "srt":      "",
            "output":   "",
            "input":    str(input_path),
            "user":     session["user"],
            "_ts":      time.time(),
        }
        _save_job(job_id)

    threading.Thread(
        target=_run_full_transcription_job,
        args=(job_id, str(input_path)),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id)


def _run_full_transcription_job(job_id, input_path):
    """
    Worker for /api/full_transcription. Mirrors the desktop app's
    _process_full_music_transcription: dynamic-imports MUSIC.py from
    the project root and runs its 8 stages, reporting progress into
    JOBS the same way every other job here does.
    """
    import importlib.util

    music_py = BASE_DIR / "MUSIC.py"

    try:
        spec = importlib.util.spec_from_file_location("music_pipeline", str(music_py))
        music_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(music_module)

        with LOCK:
            user = JOBS[job_id].get("user", "shared")

        def _p(value, status):
            with LOCK:
                JOBS[job_id]["progress"] = float(value)
                JOBS[job_id]["status"]   = status
                JOBS[job_id]["_ts"]      = time.time()
                if int(value) % 10 == 0:
                    _save_job(job_id)

        stem = Path(input_path).stem
        out_dir = OUTPUT_DIR / user / "Transcription" / stem
        out_dir.mkdir(parents=True, exist_ok=True)

        input_path_obj = Path(input_path)

        # --- Stage 1: convert to WAV -----------------------------
        _p(5, "Converting input to WAV...")
        wav_file = out_dir / "01_INPUT_AUDIO.wav"
        music_module.convert_input_to_wav(input_path_obj, wav_file)

        # --- Stage 2: source separation (optional) ---------------
        # New: beat-aligned polyphonic transcription (midi_refine.py).
        # If it (or its dependencies) isn't available we fall back to the
        # original pyin pipeline below, so nothing breaks.
        refine = None
        try:
            _rspec = importlib.util.spec_from_file_location(
                "midi_refine", str(BASE_DIR / "midi_refine.py"))
            refine = importlib.util.module_from_spec(_rspec)
            sys.modules["midi_refine"] = refine
            _rspec.loader.exec_module(refine)
        except Exception as _re:
            print(f"[full_transcription] midi_refine unavailable, "
                  f"using legacy melody path: {_re}")
            refine = None

        guitar_stem = drums_stem = None
        if refine is not None:
            _p(15, "Isolating guitar (Demucs 6-stem)...")
            guitar_stem, drums_stem = refine.separate_guitar_stems(
                wav_file, out_dir, log=print)

        separated = guitar_stem
        if separated is None:
            _p(15, "Separating instruments with Demucs...")
            try:
                separated = music_module.separate_sources(wav_file, out_dir)
            except Exception as sep_err:
                print(f"[full_transcription] source separation failed, "
                      f"continuing with full mix: {sep_err}")
                separated = None

        analysis_audio = separated if separated else wav_file
        stem_copy = out_dir / "02_INSTRUMENTAL_STEM.wav"
        try:
            shutil.copy2(analysis_audio, stem_copy)
        except Exception:
            pass

        # --- Stage 3: tempo + duration ---------------------------
        _p(30, "Detecting tempo...")
        tempo_bpm = music_module.detect_tempo(analysis_audio)
        total_duration_seconds = music_module.get_audio_duration_seconds(analysis_audio)

        # --- Stage 4: melody -> TAB ------------------------------
        refined = None
        if refine is not None:
            _p(45, "Transcribing guitar notes (beat-aligned, polyphonic)...")
            try:
                refined = refine.refine_transcription(
                    analysis_audio, out_dir,
                    drums_path=drums_stem,
                    tempo_hint=tempo_bpm,
                    progress=lambda m: _p(55, m),
                    log=print,
                )
                tempo_bpm = refined["tempo_bpm"]
                tab_notes = music_module.make_tab_notes(refined["lead_notes"])
                print(f"[full_transcription] refined: {refined['n_lead']} solo "
                      f"notes, {refined['n_rhythm']} rhythm notes, "
                      f"{refined['tempo_bpm']} BPM, grid={refined['grid']}")
            except Exception as _rf:
                import traceback
                traceback.print_exc()
                print(f"[full_transcription] refined transcription failed, "
                      f"falling back to legacy pyin: {_rf}")
                refined = None

        if refined is None:
            # extract_melody's librosa.pyin call is a single, long, opaque
            # blocking call — it can take several minutes on a full song with
            # no way to report progress from inside it. Previously the status
            # text just sat frozen on "Extracting guitar melody..." the whole
            # time, which is indistinguishable from having actually hung. A
            # lightweight heartbeat thread updates elapsed time in the status
            # text every few seconds while it runs, so it's visibly alive.
            _p(45, "Extracting guitar melody (can take several minutes)...")
            _heartbeat_stop = threading.Event()

            def _melody_heartbeat():
                start = time.time()
                while not _heartbeat_stop.wait(5):
                    elapsed = int(time.time() - start)
                    mm, ss = divmod(elapsed, 60)
                    _p(45, f"Extracting guitar melody... ({mm}m {ss:02d}s elapsed)")

            _hb_thread = threading.Thread(target=_melody_heartbeat, daemon=True)
            _hb_thread.start()
            try:
                raw_notes = music_module.extract_melody(analysis_audio)
            finally:
                _heartbeat_stop.set()
                _hb_thread.join(timeout=1)

            melody_notes = music_module.smooth_melody(raw_notes)
            tab_notes    = music_module.make_tab_notes(melody_notes)

        # --- Stage 5: chords -------------------------------------
        _p(60, "Detecting chords...")
        chords = music_module.detect_chords(analysis_audio, tempo_bpm)

        # --- Stage 6: MIDIs --------------------------------------
        # (already written by midi_refine when it succeeded)
        if refined is None:
            _p(70, "Writing guitar MIDI...")
            guitar_midi = out_dir / "03_GUITAR_SOLO.mid"
            music_module.create_guitar_midi(
                tab_notes, tempo_bpm, guitar_midi,
                total_duration_seconds=total_duration_seconds,
            )

            _p(75, "Writing chord MIDI...")
            chord_midi = out_dir / "04_RHYTHM_CHORDS.mid"
            music_module.create_chord_midi(
                chords, tempo_bpm, chord_midi,
                total_duration_seconds=total_duration_seconds,
            )

        # --- Stage 7: PDFs ---------------------------------------
        _p(85, "Generating guitar TAB PDF...")
        guitar_pdf = out_dir / "01_GUITAR_SOLO_TAB.pdf"
        music_module.draw_guitar_tab_pdf(tab_notes, tempo_bpm, guitar_pdf)

        _p(88, "Generating chord/rhythm PDF...")
        chord_pdf = out_dir / "02_RHYTHM_CHORDS.pdf"
        music_module.draw_chord_pdf(chords, tempo_bpm, chord_pdf)

        # --- Stage 7b: piano sheet (needs MuseScore installed) ---
        # Wrapped in try/except and allowed to fail without failing the
        # whole job: MuseScore is an external dependency the person may
        # not have installed yet (see find_musescore_executable()'s
        # clear error message), and a full transcription without a
        # piano sheet is still a useful result — better to hand back
        # everything else than lose the whole job over one missing PDF.
        piano_pdf = out_dir / "05_PIANO_SHEET.pdf"
        piano_pdf_ok = False
        try:
            _p(91, "Generating piano sheet music (via MuseScore)...")
            music_module.create_piano_sheet_pdf(
                tab_notes, chords, tempo_bpm, piano_pdf,
                total_duration_seconds=total_duration_seconds,
                title=f"{_display_title(stem)} — Piano Arrangement",
            )
            piano_pdf_ok = True
        except Exception as piano_err:
            print(f"[full_transcription] piano sheet PDF failed, "
                  f"continuing without it: {piano_err}")
            _p(91, f"Piano sheet skipped: {piano_err}")

        # --- Stage 7c: fingerstyle (Travis picking) guitar TAB ----
        fingerstyle_pdf = out_dir / "06_FINGERSTYLE_GUITAR_TAB.pdf"
        fingerstyle_pdf_ok = False
        try:
            _p(95, "Generating fingerstyle guitar TAB...")
            fingerstyle_events = music_module.build_fingerstyle_arrangement(
                tab_notes, chords, tempo_bpm,
            )
            music_module.draw_fingerstyle_tab_pdf(
                fingerstyle_events, tempo_bpm, fingerstyle_pdf,
                title=f"{_display_title(stem)} — Fingerstyle Guitar TAB (Travis Picking)",
            )
            fingerstyle_pdf_ok = True
        except Exception as fs_err:
            print(f"[full_transcription] fingerstyle TAB PDF failed, "
                  f"continuing without it: {fs_err}")
            _p(95, f"Fingerstyle TAB skipped: {fs_err}")

        # --- Stage 8: text report --------------------------------
        _p(97, "Writing report...")
        report_file = out_dir / "TRANSCRIPTION_REPORT.txt"
        music_module.create_report(
            report_file, input_path_obj, tempo_bpm, tab_notes, chords,
            total_duration_seconds=total_duration_seconds,
        )

        # --- Done ------------------------------------------------
        skipped = []
        if not piano_pdf_ok:
            skipped.append("piano sheet")
        if not fingerstyle_pdf_ok:
            skipped.append("fingerstyle guitar TAB")
        final_status = "Complete!" if not skipped else f"Complete (skipped: {', '.join(skipped)})"

        with LOCK:
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["status"]   = final_status
            JOBS[job_id]["output"]   = str(out_dir)
            JOBS[job_id]["folder"]   = str(out_dir)
            JOBS[job_id]["files"]    = [
                p.name for p in sorted(out_dir.iterdir()) if p.is_file()
            ]
            JOBS[job_id]["_ts"]      = time.time()
            _save_job(job_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"]   = f"Error: {e}"
            JOBS[job_id]["progress"] = 0
            JOBS[job_id]["_ts"]      = time.time()
            _save_job(job_id)


def _extract_audio_if_needed(input_path):
    """Extracts a .wav from a video file via ffmpeg; passes audio files through untouched."""
    if input_path.lower().endswith(SubtitleEngine.VIDEO_EXTENSIONS):
        if shutil.which('ffmpeg') is None:
            raise RuntimeError("ffmpeg is required to extract audio from video.")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
        tmp.close()
        cmd = ['ffmpeg', '-y', '-i', input_path, '-vn',
               '-acodec', 'pcm_s16le', '-ar', '44100', '-ac', '2', tmp.name]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise RuntimeError(f"ffmpeg failed to extract audio: {proc.stderr[-800:]}")
        return tmp.name, True
    return input_path, False


def _run_video_job(job_id, input_path, srt_path, background_path, keep_vocals):
    temp_audio_path = None
    demucs_out_dir = None
    try:
        engine = SubtitleEngine()

        with LOCK:
            user = JOBS[job_id].get("user", "shared")

        def _p(value, status):
            with LOCK:
                JOBS[job_id]["progress"] = float(value)
                JOBS[job_id]["status"]   = status
                JOBS[job_id]["_ts"]      = time.time()
                if int(value) % 10 == 0:
                    _save_job(job_id)

        _p(5, "Preparing audio...")
        audio_path, is_temp = _extract_audio_if_needed(input_path)
        if is_temp:
            temp_audio_path = audio_path

        if keep_vocals:
            audio_for_video = audio_path
        else:
            _p(20, "Removing vocals (Demucs)...")
            # separate_vocals_stem's status_callback takes a single message
            # string, not (progress, message) — wrap accordingly.
            vocals_path, demucs_out_dir = engine.separate_vocals_stem(
                audio_path, status_callback=lambda msg: _p(35, msg)
            )
            audio_for_video = str(Path(vocals_path).parent / "no_vocals.wav")
            if not Path(audio_for_video).exists():
                raise RuntimeError("Demucs did not produce an instrumental (no_vocals.wav) track.")

        stem = Path(input_path).stem
        # Both karaoke and lyric videos live under one shared top-level
        # "Karaoke" folder (per-song subfolders inside it), since the
        # dashboard only has a single "Browse Karaoke Folder" button —
        # there's no separate browse button for lyric videos, so keeping
        # them in a different top-level folder would make them
        # unreachable from the UI. The filename suffix still tells them
        # apart.
        video_folder = OUTPUT_DIR / user / "Karaoke" / stem
        video_folder.mkdir(parents=True, exist_ok=True)

        suffix = "_lyric_video.mp4" if keep_vocals else "_karaoke.mp4"
        out_path = video_folder / f"{stem}{suffix}"

        _p(60, "Rendering video (ffmpeg)...")
        engine.build_karaoke_video(
            background_path=background_path,
            audio_path=audio_for_video,
            srt_path=srt_path,
            video_output_path=str(out_path),
            progress_cb=_p,
        )

        with LOCK:
            JOBS[job_id]["progress"]    = 100
            JOBS[job_id]["status"]      = "Complete!"
            JOBS[job_id]["output"]      = str(out_path)
            JOBS[job_id]["video"]       = str(out_path)
            JOBS[job_id]["keep_vocals"] = keep_vocals
            JOBS[job_id]["_ts"]         = time.time()
            _save_job(job_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"]   = f"Error: {e}"
            JOBS[job_id]["progress"] = 0
            JOBS[job_id]["_ts"]      = time.time()
            _save_job(job_id)
    finally:
        if temp_audio_path and os.path.exists(temp_audio_path):
            try:
                os.unlink(temp_audio_path)
            except OSError:
                pass
        if demucs_out_dir:
            shutil.rmtree(demucs_out_dir, ignore_errors=True)
            with LOCK:
                JOBS[job_id]["_ts"] = time.time()
                _save_job(job_id)


# ============================================================
# JOB STATUS / DOWNLOADS
# ============================================================
@app.route("/api/status/<job_id>")
@login_required
def status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Unknown job"), 404
    if not _owns_job(job):
        return jsonify(error="Forbidden"), 403
    return jsonify(**job)


# app.js polls /api/job/<id> — alias
@app.route("/api/job/<job_id>")
@login_required
def job_status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Unknown job"), 404
    if not _owns_job(job):
        return jsonify(error="Forbidden"), 403

    # SRTs are written nested (e.g. "<stem>/SRT/<stem>.srt"), not flat in
    # outputs/<user>/, so app.js needs that relative path — not just the
    # bare filename — or the later karaoke/lyric-video lookups will 404.
    srt_rel = None
    if job.get("output") and str(job["output"]).endswith(".srt"):
        try:
            _, outdir = user_dirs(job.get("user", "shared"))
            srt_rel = str(Path(job["output"]).resolve().relative_to(outdir.resolve()))
        except Exception:
            srt_rel = Path(job["output"]).name  # fallback, better than nothing

    # Complete (absolute) path of the SRT file, for the GUI's "Copy path".
    srt_abs = None
    if job.get("output") and str(job["output"]).endswith(".srt"):
        try:
            srt_abs = str(Path(job["output"]).resolve())
        except Exception:
            srt_abs = str(job["output"])

    # translate to shape app.js expects
    return jsonify(
        id=job_id,
        status=("done" if job["progress"] >= 100 and "Error" not in job["status"] else
                ("error" if "Error" in job["status"] else "running")),
        progress=job["progress"],
        message=job["status"],
        result={
            "srt": srt_rel,
            "srt_abs": srt_abs,
            "preview": job.get("srt", "")[:8000],
            "video": job["output"] if "video" in job and not job.get("keep_vocals") else None,
            "lyric_video": job["output"] if "video" in job and job.get("keep_vocals") else None,
            "pdf": job.get("pdf"),
            "sheet_pdf": job.get("sheet_pdf"),
            "mp3": job.get("mp3"),
            "tab_pdf": job.get("tab_pdf"),
            "folder": job.get("folder"),
            "files":  job.get("files"),
            "song":   job.get("song"),
            "tracks": job.get("tracks"),
        },
        error=None if "Error" not in job["status"] else job["status"],
    )


@app.route("/api/download/<job_id>")
@login_required
def download(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job or not job["output"]:
        return jsonify(error="No output"), 404
    if not _owns_job(job):
        return jsonify(error="Forbidden"), 403

    p = Path(job["output"])
    return send_from_directory(p.parent, p.name, as_attachment=True)


@app.route("/api/download_output/<path:rel>")
@login_required
def download_output(rel):
    _, outdir = user_dirs(session["user"])
    # Prevent path traversal outside the user's own outputs folder
    target = (outdir / rel).resolve()
    if outdir.resolve() not in target.parents and target != outdir.resolve():
        return jsonify(error="Invalid path"), 400
    return send_from_directory(outdir, rel, as_attachment=True)


# ============================================================
# CREATE SONG  (lyrics + style -> generated song audio)
# ------------------------------------------------------------
# The "Create Song" button opens a two-panel modal in the dashboard
# (Lyrics / Style) and posts both here. This is plumbed onto the same
# JOBS + _p(progress, status) pattern as every other action button, so
# app.js's existing polling/status/Stop/Browse machinery works for it
# unchanged.
#
# generate_song_audio() below routes to one of two free, open-source,
# no-API-key backends, selected by the Backend dropdown in the Create
# Song modal:
#
#   ACE-Step 1.5 (https://github.com/ace-step/ACE-Step-1.5) — lyrics IN,
#   sung vocals OUT. Matches everything the Lyrics/Style/Singer panels
#   ask for. It now runs as a SEPARATE REST API server that this app
#   calls over HTTP (so it can live on this PC, or on a free Colab GPU).
#   This is the default / recommended choice.
#
#   MusicGen (Meta, via Hugging Face transformers) — INSTRUMENTAL ONLY.
#   It has no lyrics or vocals input at all — text style description
#   in, instrumental music out. Included as a second option because
#   it's smaller/faster/more mature than ACE-Step, for quick style/
#   instrument testing where you don't need the vocals yet.
#
# ---- ONE-TIME SETUP (do this on the machine running app.py) ----
#   # ACE-Step 1.5 (run in its OWN folder/terminal, not this app's venv):
#   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
#   git clone https://github.com/ACE-Step/ACE-Step-1.5.git
#   cd ACE-Step-1.5
#   uv sync
#   uv run acestep-api          # serves http://127.0.0.1:8001
#   # first run auto-downloads the models (several GB).
#   # Then start this app as usual. To point at a different server
#   # (e.g. a Colab tunnel URL), set before starting app.py:
#   #   set ACE_STEP_API_URL=https://xxxx.ngrok-free.app
#   #   set ACE_STEP_API_KEY=...    (only if you enabled a key on the server)
#
#   # MusicGen:
#   pip install transformers torch            # CPU build of torch is fine
#   pip install scipy                          # used to write the .wav
#   # first generation call auto-downloads facebook/musicgen-small
#   # (~1.2GB) from Hugging Face — needs internet on that first run only.
#
# SubtitleEngine (subtitle_engine.py) is unrelated to either of these —
# it only ANALYZES existing audio (transcribing, stems, chords); it has
# no generation capability, so Create Song doesn't touch it.
# ============================================================

# ACE-Step 1.5 runs as its own REST API server (see setup notes above).
# Override the address with the ACE_STEP_API_URL environment variable —
# e.g. to a Colab/ngrok URL — without editing this file.
#
# IMPORTANT: The API key is read from ACE_STEP_API_KEY first (the name
# music_studio_config.bat exports), then from ACESTEP_API_KEY (the name
# the .bat's source variable uses), and finally falls back to the key
# that was baked into music_studio_config.bat. Without this fallback,
# a frozen EXE launched from Explorer never sees the env vars the .bat
# set, and every ACE-Step call returns HTTP 401.
ACE_STEP_API_URL = (
    os.environ.get("ACE_STEP_API_URL")
    or os.environ.get("ACESTEP_API_URL")
    or "http://127.0.0.1:8001"
).rstrip("/")

ACE_STEP_API_KEY = (
    os.environ.get("ACE_STEP_API_KEY")
    or os.environ.get("ACESTEP_API_KEY")
    or "lCwmNfgmkZvRZfbq39brshmiMvBaTLkjqtQOOa-nPEc"
)

# Kept short on purpose — CPU inference time scales with this. Raise it
# once you've confirmed timing/quality on your machine.
ACE_STEP_DURATION_SECONDS = float(os.environ.get("ACE_STEP_DURATION_SECONDS", "0") or 0)
# ^ 0 = automatic: picked from how much lyric text there is (30s..360s).
#   Set ACE_STEP_DURATION_SECONDS=45 (etc.) to force a fixed length.
ACE_STEP_INFER_STEPS = 8            # turbo model: 8 is the recommended value
ACE_STEP_TIMEOUT_SECONDS = 90 * 60  # give up waiting after this long
ACE_STEP_POLL_SECONDS = 3

# --- Chunked generation (keeps every request short enough for CPU) ---------
# The lyrics are split at section boundaries ([verse], [chorus], ...) into
# chunks of roughly ACE_STEP_CHUNK_SECONDS of audio each, generated one after
# another with the SAME style/seed, then joined with a short crossfade.
# (That is the OLD method; see SEAMLESS LONG SONGS below for the default.)
# Songs that already fit in one chunk are generated in a single request.
ACE_STEP_CHUNK_SECONDS = float(os.environ.get("ACE_STEP_CHUNK_SECONDS", "60") or 60)
ACE_STEP_SEED = int(os.environ.get("ACE_STEP_SEED", "12345") or 12345)
ACE_STEP_BPM = os.environ.get("ACE_STEP_BPM", "").strip()        # e.g. "96" (optional)
ACE_STEP_KEY = os.environ.get("ACE_STEP_KEY", "").strip()        # e.g. "A minor" (optional)
ACE_STEP_CROSSFADE_SECONDS = float(os.environ.get("ACE_STEP_CROSSFADE_SECONDS", "1.0") or 1.0)
ACE_STEP_CHUNK_RETRIES = 1


# ============================================================
# ACE-STEP SERVER CHECK + AUTO-START
# ------------------------------------------------------------
# Every AI button that needs ACE-Step (Create Song, backing-track cover,
# ...) first checks that the ACE-Step server is answering. If it is NOT,
# the job status shows "ACE-Step is not running - starting ACE-Step
# start-up..." and this app launches it (start_ace_step.bat, or
# 'uv run acestep-api' inside the ACE-Step-1.5 folder), then waits until
# it answers before continuing. Auto-start only happens when
# ACE_STEP_API_URL points at THIS PC (127.0.0.1 / localhost); for a remote
# or Colab URL the app just reports that the server is unreachable.
#   - /api/ace_status : used by the dashboard banner
#   - /api/ace_start  : starts ACE-Step (dashboard 'Start server' button)
#   - _require_ace_running() : called before every song request
#
# Optional environment variables:
#   ACE_STEP_AUTOSTART=0   turn auto-start off
#   ACE_STEP_START_CMD     full command to run instead of the .bat / uv
#   ACE_STEP_DIR           path of the ACE-Step-1.5 folder
# ============================================================

ACE_START_HINT = (
    "Start ACE-Step on the PC that hosts it (run start_ace_step.bat, or "
    "'cd ACE-Step-1.5' then 'uv run acestep-api'), wait until it prints "
    "'Uvicorn running on http://127.0.0.1:8001' (about 3 minutes on CPU), "
    "then try again. Don't close its window while songs are being generated."
)


def _ace_port_open() -> bool:
    """True if something is listening on the ACE-Step port (it may still be
    loading models, or busy generating, and not answer /health yet)."""
    try:
        from urllib.parse import urlparse
        u = urlparse(ACE_STEP_API_URL)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except Exception:
        return False


def _ace_is_running() -> bool:
    try:
        _ace_http("GET", "/health", timeout=2)
        return True
    except Exception:
        return False


ACE_AUTOSTART = os.environ.get("ACE_STEP_AUTOSTART", "1").strip() != "0"
ACE_STARTING_MESSAGE = ("ACE-Step server is not running. Starting ACE-Step start-up now - "
                        "please wait (about 3 minutes the first time)...")
_ACE_START_LOCK = threading.Lock()
_ACE_PROC = None          # the Popen of the ACE-Step process WE launched
_ACE_STARTED_AT = 0.0


def _ace_url_is_local() -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(ACE_STEP_API_URL).hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
    except Exception:
        return False


def _ace_find_launcher():
    """Work out HOW to start ACE-Step on this PC.
    Returns (argv_list, working_dir) or None if nothing was found."""
    custom = os.environ.get("ACE_STEP_START_CMD", "").strip()
    if custom:
        return (custom, str(BASE_DIR))          # a string -> run through the shell

    roots = [BASE_DIR, BASE_DIR.parent, Path.cwd()]
    if os.environ.get("MUSIC_STUDIO_INSTALL_DIR"):          # set by launcher.py (installed build)
        roots.append(Path(os.environ["MUSIC_STUDIO_INSTALL_DIR"]))

    # 1) start_ace_step.bat (or .sh) next to the app
    names = ["start_ace_step.bat"] if sys.platform.startswith("win") else \
            ["start_ace_step.sh", "start_ace_step.bat"]
    for r in roots:
        for n in names:
            f = r / n
            if f.is_file():
                if f.suffix.lower() == ".bat":
                    return (["cmd", "/c", str(f)], str(f.parent))
                return (["bash", str(f)], str(f.parent))

    # 2) the ACE-Step-1.5 folder -> 'uv run acestep-api'
    dirs = []
    if os.environ.get("ACE_STEP_DIR"):
        dirs.append(Path(os.environ["ACE_STEP_DIR"]))
    for r in roots:
        dirs.append(r / "ACE-Step-1.5")
    for d in dirs:
        if d.is_dir():
            uv = shutil.which("uv") or "uv"
            return ([uv, "run", "acestep-api"], str(d))
    return None


def _ace_start_server():
    """Launch ACE-Step in its own window. Never starts a second copy.
    Returns (ok, message)."""
    global _ACE_PROC, _ACE_STARTED_AT
    if _ace_is_running() or _ace_port_open():
        return True, "ACE-Step is already running / loading."
    if not ACE_AUTOSTART:
        return False, "auto-start is turned off (ACE_STEP_AUTOSTART=0)"
    if not _ace_url_is_local():
        return False, f"ACE-Step is on another machine ({ACE_STEP_API_URL}); start it there"

    with _ACE_START_LOCK:
        # another request may have started it while we waited for the lock
        if _ace_is_running() or _ace_port_open():
            return True, "ACE-Step is already running / loading."
        if (_ACE_PROC is not None and _ACE_PROC.poll() is None
                and time.time() - _ACE_STARTED_AT < 900):
            return True, "ACE-Step start-up is already in progress."

        found = _ace_find_launcher()
        if not found:
            return False, ("start_ace_step.bat / the ACE-Step-1.5 folder was not found "
                           "next to the app")
        cmd, cwd = found
        try:
            kw = dict(cwd=cwd, shell=isinstance(cmd, str))
            if sys.platform.startswith("win"):
                # its own console window, so the user can see progress and
                # closing this app does not kill ACE-Step
                kw["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            else:
                kw["start_new_session"] = True
                kw["stdout"] = open(BASE_DIR / "ace_step_server.log", "ab")
                kw["stderr"] = subprocess.STDOUT
            _ACE_PROC = subprocess.Popen(cmd, **kw)
            _ACE_STARTED_AT = time.time()
            print(f"[ACE] Starting ACE-Step: {cmd} (in {cwd})", flush=True)
            return True, "ACE-Step start-up launched."
        except Exception as e:
            return False, f"could not launch ACE-Step ({e})"


@app.route("/api/ace_status")
@login_required
def ace_status():
    """Used by the dashboard to show a warning banner if ACE-Step isn't reachable."""
    starting = bool(_ACE_PROC is not None and _ACE_PROC.poll() is None
                    and time.time() - _ACE_STARTED_AT < 900)
    try:
        _ace_http("GET", "/health", timeout=5)
        return jsonify(running=True, starting=False, url=ACE_STEP_API_URL)
    except Exception as e:
        return jsonify(running=False, starting=starting or _ace_port_open(),
                       url=ACE_STEP_API_URL, error=str(e),
                       message=ACE_STARTING_MESSAGE if starting else "",
                       hint=ACE_START_HINT)


@app.route("/api/ace_start", methods=["POST"])
@login_required
def ace_start():
    """Start ACE-Step on this PC (if it is not already running)."""
    if _ace_is_running():
        return jsonify(running=True, starting=False, message="ACE-Step is already running.")
    ok, info = _ace_start_server()
    if not ok:
        return jsonify(running=False, starting=False, error=info, hint=ACE_START_HINT), 400
    return jsonify(running=False, starting=True, message=ACE_STARTING_MESSAGE, info=info)


def _require_ace_running(progress=None, max_wait=900):
    """Called before each song request. Returns when ACE-Step answers.
    If it is not running, tell the user ('starting ACE-Step start-up') and
    start it, then wait for it to finish loading its models."""
    if _ace_is_running():
        return

    launched = False
    if not _ace_port_open():
        print("[ACE] " + ACE_STARTING_MESSAGE, flush=True)
        if progress:
            progress(18, ACE_STARTING_MESSAGE)
        ok, info = _ace_start_server()
        if not ok:
            raise RuntimeError(
                f"ACE-Step is not running at {ACE_STEP_API_URL} and could not be "
                f"started automatically: {info}. " + ACE_START_HINT)
        launched = True

    t0, last = time.time(), 0
    while time.time() - t0 < max_wait:
        if _ace_is_running():
            if progress:
                progress(18, "ACE-Step is ready.")
            return
        # if the process we launched crashed straight away, stop waiting
        proc = _ACE_PROC
        if (launched and proc is not None and proc.poll() not in (None, 0)
                and not _ace_port_open()):
            raise RuntimeError(
                f"ACE-Step start-up failed (exit code {proc.poll()}). "
                "Look at its window / ace_step_server.log for the error. " + ACE_START_HINT)
        if progress and time.time() - last >= 10:
            last = time.time()
            what = "start-up" if launched else "to finish loading"
            progress(18, f"Waiting for ACE-Step {what}... {int(time.time() - t0)}s")
        time.sleep(2)
    raise RuntimeError(
        f"ACE-Step did not answer at {ACE_STEP_API_URL} after "
        f"{max_wait // 60} minutes. Check its window for errors.")


def generate_song_audio(lyrics, style, instruments, singer, out_path,
                         backend="ace", progress_cb=None, language="", ref_audio=None):
    """Generate a song and write it to out_path. `backend` selects which
    engine does the work — routed here from the dashboard's Backend
    dropdown in the Create Song modal:

      "ace"      -> generate_song_audio_ace(...)       lyrics + vocals
      "musicgen" -> generate_song_audio_musicgen(...)  instrumental only
    """
    if backend == "musicgen":
        return generate_song_audio_musicgen(style, instruments, out_path,
                                             progress_cb=progress_cb)
    # Check the ACE-Step server first; if it is down, say so and start it.
    _require_ace_running(progress_cb)
    return generate_song_audio_ace(lyrics, style, instruments, singer, out_path,
                                    progress_cb=progress_cb, language=language,
                                    ref_audio=ref_audio)


def _ace_http(method, path, payload=None, timeout=60, base_url=None):
    """Tiny stdlib HTTP helper for the ACE-Step API. Returns raw bytes."""
    import urllib.request
    import urllib.error

    headers = {
        "Content-Type": "application/json",
        "ngrok-skip-browser-warning": "1",   # harmless unless behind free ngrok
    }
    if ACE_STEP_API_KEY:
        headers["Authorization"] = f"Bearer {ACE_STEP_API_KEY}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request((base_url or ACE_STEP_API_URL) + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"ACE-Step server returned HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Can't reach the ACE-Step server at {ACE_STEP_API_URL} ({e.reason}). "
            "Start it in its own terminal with: cd ACE-Step-1.5 && uv run acestep-api "
            "(see the comment block above generate_song_audio() for setup)."
        )


def _ace_post_multipart(path, fields, files, timeout=600):
    """POST multipart/form-data to the ACE-Step API. Used when audio has to be
    UPLOADED to the server: a backing track (cover mode) or a voice reference.
    Uploading (instead of sending a file path) works whether ACE-Step runs on
    this PC or on a remote machine / Colab tunnel.
      fields: {name: value}      files: {form_field_name: local_file_path}
    Returns the raw response bytes, like _ace_http()."""
    import urllib.request
    import urllib.error
    import mimetypes

    boundary = "----MusicStudio" + uuid.uuid4().hex
    bnd = boundary.encode("ascii")
    crlf = b"\r\n"
    body = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        body += b"--" + bnd + crlf
        body += f'Content-Disposition: form-data; name="{name}"'.encode("utf-8") + crlf + crlf
        body += str(value).encode("utf-8") + crlf
    for name, fpath in files.items():
        fpath = Path(fpath)
        ctype = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"
        body += b"--" + bnd + crlf
        body += (f'Content-Disposition: form-data; name="{name}"; '
                 f'filename="{fpath.name}"').encode("utf-8") + crlf
        body += f"Content-Type: {ctype}".encode("ascii") + crlf + crlf
        body += fpath.read_bytes() + crlf
    body += b"--" + bnd + b"--" + crlf

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "ngrok-skip-browser-warning": "1",
    }
    if ACE_STEP_API_KEY:
        headers["Authorization"] = f"Bearer {ACE_STEP_API_KEY}"
    req = urllib.request.Request(ACE_STEP_API_URL + path, data=bytes(body),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"ACE-Step server returned HTTP {e.code}: {msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Can't reach the ACE-Step server at {ACE_STEP_API_URL} ({e.reason}).")


import re as _re


_TAG_TYPOS = {
    "instramental": "instrumental", "instrumantal": "instrumental", "instrumetal": "instrumental",
    "intrumental": "instrumental", "instrumenal": "instrumental", "insrumental": "instrumental",
    "guatar": "guitar", "gitar": "guitar", "guiter": "guitar", "guittar": "guitar",
    "saxaphone": "saxophone", "saxophon": "saxophone", "saxofone": "saxophone",
    "violen": "violin", "pianno": "piano", "drumms": "drums",
}
_COMMAND_WORDS = ("inst", "solo", "intro", "outro", "break", "guitar", "sax", "piano", "drum",
                  "violin", "bass", "string", "synth", "trumpet", "flute", "organ", "oud",
                  "riff", "fill", "fade", "build", "drop")


def _fix_tag_typos(tag):
    """Bracket commands like [Instramental - Guatar Solo] are read by the model,
    so obvious misspellings of musical words are corrected."""
    def fix(m):
        w = m.group(0)
        r = _TAG_TYPOS.get(w.lower())
        if not r:
            return w
        return r.capitalize() if w[:1].isupper() else r
    return _re.sub(r"[A-Za-z]+", fix, tag)


def _clean_lyrics_for_ace(lyrics):
    """Reshape typed lyrics into what ACE-Step sings well:
      * section tags on their own line ([verse], [chorus] ...)
      * short sung lines (~7 words) instead of one long blob
      * an "[Instrumental]"-style tag that has lyrics under it is really
        a sung section, so it becomes [verse]/[chorus] — leaving it as
        instrumental tells the model NOT to sing those words.
    """
    tag_re = _re.compile(r"^\s*\[(.+?)\]\s*$")
    sections = []          # [tag_or_None, [text pieces]]
    for line in lyrics.replace("\r", "").split("\n"):
        # Typos must not turn into sung words: a line with no letters at all
        # (a stray "\" or "-") is dropped, and a line that starts with "["
        # is always a section tag, even if it is like "[Intro]- Guitar Solo]".
        if not _re.search(r"\w", line):
            continue
        if line.lstrip().startswith("[") and "]" in line:
            inner = _re.sub(r"[\[\]]+", " ", line)
            line = "[" + _re.sub(r"\s*[-–—]\s*", " - ", _re.sub(r"\s+", " ", inner)).strip(" -") + "]"
        m = tag_re.match(line)
        if m:
            sections.append([_fix_tag_typos(m.group(1).strip()), []])
        elif line.strip():
            if not sections:
                sections.append([None, []])
            sections[-1][1].append(line.strip())

    known_sung = {"verse", "chorus", "pre-chorus", "prechorus", "bridge", "outro", "hook"}
    cycle = ["verse", "chorus"]
    n_sung = 0
    out = []
    for tag, pieces in sections:
        text = " ".join(pieces).strip()
        low = (tag or "").lower()
        # "Chorus - Group vocals" / "Verse 2: soft" -> head word "chorus"/"verse"
        head = _re.split(r"\s*[-:–—]\s*", low, maxsplit=1)[0]
        base = _re.sub(r"[\s\d]+$", "", _re.sub(r"\s*[\(\[].*$", "", head))   # "verse 2 (soft)" -> "verse"
        if text:
            if base in known_sung:
                final = tag
            else:                                       # None / instrumental / unknown
                final = cycle[n_sung % 2]
                # A bracket COMMAND (e.g. "[Guitar solo]") with words under it:
                # keep the command as its own line, then sing the words.
                if tag and any(w in low for w in _COMMAND_WORDS):
                    out.append(f"[{tag}]")
            n_sung += 1
            # Keep the user's own short lines as they are; only re-wrap
            # over-long lines (a blob of text on one line can't be sung).
            lines = []
            for piece in pieces:
                pw = piece.split()
                if len(pw) <= 10:
                    lines.append(piece)
                    continue
                cur = []
                for w in pw:
                    cur.append(w)
                    if len(cur) >= 7 or (len(cur) >= 3 and w[-1] in ",.;:!?"):
                        lines.append(" ".join(cur)); cur = []
                if cur:
                    lines.append(" ".join(cur))
            out.append(f"[{final}]\n" + "\n".join(lines))
        else:
            if tag is not None:
                out.append(f"[{tag}]")
    return "\n\n".join(out) if out else lyrics


def _normalize_language(value):
    """The Create Song language choice -> an ACE-Step language code.
    "", "auto", None or anything unrecognised -> "" (= detect from the lyrics)."""
    v = str(value or "").strip().lower()
    if v in ("", "auto"):
        return ""
    return v if _re.fullmatch(r"[a-z]{2,3}", v) else ""


def _needed_seconds(lyrics_for_model, language=""):
    """Song length (seconds) needed to actually sing all the lyrics.
    A sung line takes longer than its word count suggests (held notes, a
    breath between lines), and Arabic words are long and stretched when sung.
    Too short a duration = rushed, garbled or missing vocals.
    Tune with ACE_STEP_SECONDS_PER_WORD (e.g. 0.9 if lyrics still get cut off,
    0.55 if the song still has too much empty music)."""
    forced = os.environ.get("ACE_STEP_SECONDS_PER_WORD", "").strip()
    if forced:
        wps = float(forced)
    else:
        wps = 1.1 if _guess_vocal_language(lyrics_for_model, language) == "ar" else 0.7
    # Short intro + short outro, a brief breath between sections, and a solo /
    # instrumental section only as long as it really needs to be.
    # Tune with ACE_STEP_INTRO_OUTRO_SECONDS (default 8 for both together),
    # ACE_STEP_SECTION_GAP_SECONDS (default 2) and ACE_STEP_SOLO_SECONDS (default 15).
    total = float(os.environ.get("ACE_STEP_INTRO_OUTRO_SECONDS", "8") or 8)
    gap = float(os.environ.get("ACE_STEP_SECTION_GAP_SECONDS", "2") or 2)
    solo = float(os.environ.get("ACE_STEP_SOLO_SECONDS", "15") or 15)
    for line in lyrics_for_model.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            low = line.lower()
            if "solo" in low:
                total += solo                      # a real solo: reasonable length
            elif "intro" in low or "outro" in low:
                total += 6                         # short intro / outro, even if "instrumental"
            elif "inst" in low or "break" in low:
                total += solo
            else:
                total += gap
            continue
        total += max(2.2, len(line.split()) * wps + 0.6)
    return float(round(total))


ACE_MIN_SONG_SECONDS = float(os.environ.get("ACE_STEP_MIN_SECONDS", "180") or 180)   # 3 min
ACE_MAX_SONG_SECONDS = float(os.environ.get("ACE_STEP_MAX_SECONDS", "360") or 360)   # 6 min


def _auto_duration(lyrics_for_model, language=""):
    """Song length: what the lyrics need, kept between 3 and 6 minutes
    (ACE_STEP_MIN_SECONDS / ACE_STEP_MAX_SECONDS). Instrumental-only requests
    don't get the 3-minute minimum."""
    needed = _needed_seconds(lyrics_for_model, language)
    if lyrics_for_model.strip().lower() == "[instrumental]":
        return float(max(30, min(ACE_MAX_SONG_SECONDS, needed)))
    return float(max(ACE_MIN_SONG_SECONDS, min(ACE_MAX_SONG_SECONDS, needed)))


def _guess_vocal_language(text, language=""):
    """Pick ACE-Step's vocal_language. Order: the language chosen in the
    Create Song window, then the ACE_STEP_VOCAL_LANGUAGE env var, then the
    script the lyrics are written in. Wrong language = weak or missing
    vocals, so this matters."""
    if language:
        return language
    forced = os.environ.get("ACE_STEP_VOCAL_LANGUAGE", "").strip()
    if forced:
        return forced
    counts = {"ar": 0, "zh": 0, "ja": 0, "ko": 0, "ru": 0}
    for ch in text:
        o = ord(ch)
        if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F:
            counts["ar"] += 1
        elif 0x4E00 <= o <= 0x9FFF:
            counts["zh"] += 1
        elif 0x3040 <= o <= 0x30FF:
            counts["ja"] += 1
        elif 0xAC00 <= o <= 0xD7AF:
            counts["ko"] += 1
        elif 0x0400 <= o <= 0x04FF:
            counts["ru"] += 1
    best = max(counts, key=counts.get)
    if counts["ja"]:            # Japanese mixes kanji with kana
        return "ja"
    return best if counts[best] >= 3 else "en"


def _ace_generate_one(lyrics, style, instruments, singer, out_path, progress_cb=None,
                      seed=None, bpm=None, key_scale=None, language="", duration=None,
                      src_audio=None, ref_audio=None, cover_strength=None,
                      infer_steps=None):
    """Generate ONE piece of audio (a whole short song, or one chunk of a long
    song) and write it to out_path, by calling a running ACE-Step 1.5 REST
    API server (local or remote).

    Optional audio inputs (uploaded to the server as multipart):
      src_audio  -> the user's own backing track. Switches ACE-Step to
                    task_type="cover": it keeps that track's tempo / structure /
                    harmony and sings the lyrics over it instead of inventing music.
      ref_audio  -> a voice/style reference clip, so the singer sounds like it.
    """

    warn_prefix = ""

    def _p(value, status):
        if progress_cb:
            progress_cb(value, warn_prefix + status)

    # Fold instruments + singer into ACE-Step's single "prompt" field —
    # it doesn't have separate instrument/singer inputs, just one style
    # description string plus the lyrics block.
    tags_parts = [style.strip()] if style.strip() else []
    # Vocals go FIRST (right after the genre) so a long instrument list
    # can't drown them out. "auto" still asks for a singer.
    if singer != "instrumental":
        if singer and singer != "auto":
            tags_parts.append(f"{singer} vocals, lead vocals, singing")
        else:
            tags_parts.append("vocals, lead vocals, singing")
    if instruments:
        tags_parts.append(", ".join(instruments))
    tags = ", ".join(p for p in tags_parts if p) or "pop"

    # ACE-Step's own convention for "no vocals" is a literal [instrumental]
    # lyrics block rather than a separate flag.
    if singer == "instrumental":
        lyrics_for_model = "[instrumental]"
    else:
        lyrics_for_model = _clean_lyrics_for_ace(lyrics)
    vocal_language = _guess_vocal_language(lyrics_for_model, language)
    # Name the sung language in the style prompt as well as in vocal_language;
    # without it non-English vocals are often mumbled or sung with an English accent.
    _lang_names = {"ar": "Arabic", "zh": "Chinese", "ja": "Japanese",
                   "ko": "Korean", "ru": "Russian", "es": "Spanish",
                   "fr": "French", "de": "German", "it": "Italian",
                   "pt": "Portuguese", "hi": "Hindi", "tr": "Turkish",
                   "fa": "Persian", "ur": "Urdu"}
    if singer != "instrumental" and vocal_language in _lang_names:
        tags = f"{_lang_names[vocal_language]} vocals, sung in {_lang_names[vocal_language]}, " + tags
    # ACE-Step has no dialect switch (only vocal_language="ar"), so steer the
    # accent through the style prompt. Set ACE_STEP_ARABIC_DIALECT to "" to
    # disable, or e.g. "Gulf" / "Levantine" for another dialect.
    if singer != "instrumental" and vocal_language == "ar":
        _dia = os.environ.get("ACE_STEP_ARABIC_DIALECT", "Egyptian").strip()
        if _dia:
            tags = (f"{_dia} Arabic dialect, {_dia} accent, colloquial {_dia} pronunciation, "
                    f"not Modern Standard Arabic, not Gulf Arabic, " + tags)
    # Arrangement hints: keep intro/outro short and fill the gaps between sung
    # phrases with brief improvisation instead of long instrumental stretches.
    # (A style prompt is a soft hint; the length estimate above is what really
    # keeps the song tight.)
    if singer != "instrumental":
        _low = lyrics_for_model.lower()
        if "solo" in _low or "[inst" in _low:
            tags += ", short intro, short outro, brief instrumental fills between vocal phrases, solo kept short"
        else:
            tags += ", short intro, short outro, brief instrumental fills between vocal phrases, no long instrumental breaks"
    duration = duration or ACE_STEP_DURATION_SECONDS or _auto_duration(lyrics_for_model, vocal_language)
    needed = _needed_seconds(lyrics_for_model, vocal_language)
    if singer != "instrumental" and needed > duration + 5:
        warn_prefix = (f"WARNING: these lyrics need about {needed / 60:.1f} min but this song is "
                       f"limited to {duration / 60:.1f} min, so the end may be rushed or cut off. "
                       "Shorten the lyrics or remove repeated sections. | ")
        print("[ACE-Step] " + warn_prefix, flush=True)
    print(f"[ACE-Step] sending: prompt={tags!r} vocal_language={vocal_language} "
          f"duration={duration}s\n--- lyrics sent ---\n{lyrics_for_model}\n-------------------",
          flush=True)

    _require_ace_running(_p)
    _p(18, f"Connecting to ACE-Step server at {ACE_STEP_API_URL}...")
    _ace_http("GET", "/health", timeout=15)

    _p(25, f"Submitting ~{int(duration)}s song to ACE-Step "
           "(first run downloads models on the server — can take a while)...")
    payload = {
        "prompt": tags,
        "lyrics": lyrics_for_model,
        "audio_duration": duration,
        "inference_steps": int(infer_steps or ACE_STEP_INFER_STEPS),
        "batch_size": 1,
        "vocal_language": vocal_language,
        "audio_format": "wav",
        # LM features off: faster and lighter, esp. on CPU / small GPUs.
        "thinking": False,
        "use_cot_caption": False,
        "use_cot_language": False,
    }
    # Same seed / tempo / key for every chunk keeps the sound consistent.
    if seed is not None:
        payload["seed"] = int(seed)
        payload["use_random_seed"] = False
    if bpm:
        payload["bpm"] = int(float(bpm))
    if key_scale:
        payload["key_scale"] = key_scale
    if src_audio or ref_audio:
        files = {}
        if src_audio:
            payload["task_type"] = "cover"
            payload["audio_cover_strength"] = (ACE_COVER_STRENGTH if cover_strength is None
                                               else float(cover_strength))
            files["src_audio"] = src_audio
        if ref_audio:
            files["reference_audio"] = ref_audio
        print(f"[ACE-Step] uploading audio: {sorted(files)} "
              f"task_type={payload.get('task_type', 'text2music')}", flush=True)
        raw_reply = _ace_post_multipart("/release_task", payload, files)
    else:
        raw_reply = _ace_http("POST", "/release_task", payload)
    submit = json.loads(raw_reply.decode("utf-8"))
    task_id = ((submit or {}).get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"ACE-Step did not accept the task: {submit!r}")

    # Poll until the task is done or failed. The reply shape is checked
    # loosely on purpose (status as int/str, data as list/dict, result as
    # JSON string or list) and the raw reply is logged to the console so a
    # mismatch is visible right away instead of after a long timeout.
    def _parse_result(raw):
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return []
        if isinstance(raw, dict):
            raw = [raw]
        return raw if isinstance(raw, list) else []

    started = time.time()
    last_log = -999.0
    file_path = None
    while True:
        elapsed = time.time() - started
        if elapsed > ACE_STEP_TIMEOUT_SECONDS:
            raise RuntimeError(
                f"ACE-Step took longer than {ACE_STEP_TIMEOUT_SECONDS // 60} minutes "
                "— giving up. Try a shorter duration or a faster machine."
            )
        reply = json.loads(_ace_http("POST", "/query_result",
                                     {"task_id_list": [task_id]}).decode("utf-8"))
        if elapsed - last_log >= 30:
            last_log = elapsed
            print(f"[ACE-Step] query_result after {int(elapsed)}s: {str(reply)[:700]}", flush=True)

        rows = (reply or {}).get("data")
        if isinstance(rows, dict):
            rows = [rows]
        entry = (rows or [None])[0] or {}
        status = entry.get("status")
        items = _parse_result(entry.get("result"))

        for item in items:
            if isinstance(item, dict) and item.get("file"):
                file_path = item["file"]
                break
        if file_path:
            break
        if str(status).lower() in ("1", "succeeded", "success", "completed", "done"):
            raise RuntimeError(f"ACE-Step reports success but returned no audio file: {entry!r}")
        if str(status).lower() in ("2", "failed", "error"):
            raise RuntimeError(f"ACE-Step failed to generate the song: {entry.get('result')!r}")

        # Creep progress from 30 -> 85 while we wait (no real % from the API).
        _p(min(85, 30 + elapsed / 20),
           f"Generating song... {int(elapsed)}s elapsed (CPU can be slow, sit tight)")
        time.sleep(ACE_STEP_POLL_SECONDS)

    _p(90, "Downloading generated audio...")
    audio = _ace_http("GET", file_path if file_path.startswith("/") else "/" + file_path,
                      timeout=300)
    if not audio:
        raise RuntimeError("ACE-Step returned an empty audio file.")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(audio)


def _split_ace_chunks(lyrics_for_model, limit_seconds, language=""):
    """Group cleaned lyric sections into chunks of <= limit_seconds of audio.
    Never splits inside a section; one oversized section becomes its own chunk."""
    blocks = [b.strip() for b in _re.split(r"\n\s*\n", lyrics_for_model) if b.strip()]
    chunks, cur, cur_sec = [], [], 0.0
    for b in blocks:
        sec = max(_needed_seconds(b, language) - 12, 5.0)     # drop the per-call base
        if cur and cur_sec + sec + 12 > limit_seconds:
            chunks.append("\n\n".join(cur))
            cur, cur_sec = [], 0.0
        cur.append(b)
        cur_sec += sec
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def _join_audio_chunks(paths, out_path, crossfade):
    """Join WAV chunks into out_path with ffmpeg (already required by this app).
    Crossfades the seams; falls back to a plain join if the crossfade fails."""
    ff = shutil.which("ffmpeg")
    if ff is None:
        raise RuntimeError(
            "ffmpeg is required to join the song chunks but wasn't found on PATH. "
            f"The individual parts are saved in: {Path(paths[0]).parent}")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    n = len(paths)

    def _run(filter_graph, last_label):
        cmd = [ff, "-y"]
        for p in paths:
            cmd += ["-i", str(p)]
        cmd += ["-filter_complex", filter_graph, "-map", last_label,
                "-c:a", "pcm_s16le", str(out_path)]
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", creationflags=flags)

    attempts = []
    if crossfade > 0:
        parts, prev = [], "[0:a]"
        for k in range(1, n):
            lab = f"[x{k}]"
            parts.append(f"{prev}[{k}:a]acrossfade=d={crossfade}:c1=qsin:c2=qsin{lab}")
            prev = lab
        attempts.append((";".join(parts), prev))
    attempts.append(("".join(f"[{k}:a]" for k in range(n)) + f"concat=n={n}:v=0:a=1[out]", "[out]"))

    last_err = ""
    for graph, label in attempts:
        proc = _run(graph, label)
        if proc.returncode == 0 and Path(out_path).exists() and Path(out_path).stat().st_size > 1000:
            return
        last_err = (proc.stderr or "")[-600:]
        print(f"[ACE-Step] join attempt failed: {last_err}", flush=True)
    raise RuntimeError(f"ffmpeg could not join the song chunks: {last_err}")


# ============================================================
# SEAMLESS LONG SONGS  (one continuous music bed, voice added after)
# ------------------------------------------------------------
# The old way generated every chunk as its OWN finished song (own intro,
# own ending, own tempo/key drift, own loudness) and crossfaded them for
# 1 second, so you heard separate pieces. The seamless way:
#
#   1) MUSIC FIRST, IN ONE PIECE: the whole song's music is a single
#      [instrumental] request, so there is nothing to join (no seams, no
#      gaps). Only if the server cannot do that (too long / timeout) does it
#      fall back to one bed per chunk: same style / seed, tempo + key of the
#      first bed locked, each bed trimmed to real music and loudness-matched,
#      then joined with a long equal-power crossfade.
#   3) VOICE LAST: for each chunk the lyrics are sung over THAT bed
#      (ACE-Step cover mode, same as "own backing track"), the singing is
#      lifted out with Demucs, placed at the right time on the joined
#      timeline, and mixed onto the single continuous music track.
#
# The music is therefore one piece; only the voice is made in parts, and
# those parts start/end at lyric-section boundaries (natural pauses).
# Costs roughly twice the time of the old way (music + voice passes).
#
# Tuning (environment variables):
#   ACE_STEP_SEAMLESS      1 (default) = this method, 0 = old chunk-and-join
#   ACE_STEP_SEAM_SECONDS  crossfade length; 0 (default) = 8 beats, 3-8 s
#   ACE_STEP_BED_MODE      single (default) = all the music in one request,
#                          chunks = always one bed per chunk
#   ACE_STEP_SINGLE_BED_MAX_SECONDS  longest one-piece music request (420)
# ============================================================
ACE_SEAMLESS = os.environ.get("ACE_STEP_SEAMLESS", "1").strip() != "0"
# SINGLE PASS (default ON): generate the whole song - music AND vocals - in ONE
# ACE-Step request, never split into parts. Slower on CPU, but one continuous
# song with no seams. Set ACE_STEP_SINGLE_PASS=0 to go back to the chunked /
# seamless methods for long songs.
ACE_SINGLE_PASS = os.environ.get("ACE_STEP_SINGLE_PASS", "1").strip() != "0"
ACE_SEAM_SECONDS = float(os.environ.get("ACE_STEP_SEAM_SECONDS", "0") or 0)
ACE_BED_LUFS = -18.0
# Vocal clarity (see _polish_vocals / _mix_vocals_over_track).
#   ACE_STEP_VOCAL_OVER_MUSIC_DB : how much LOUDER the voice is than the music,
#       measured in LUFS (default 3). The music is NOT turned down - the voice is
#       brought up. Raise to 4-5 if the words are still buried, lower to 1-2 if
#       the voice sounds too much on top.
#   ACE_STEP_VOCAL_CHUNK_LUFS    : level every sung part is evened out to first.
ACE_VOCAL_OVER_MUSIC_DB = float(os.environ.get("ACE_STEP_VOCAL_OVER_MUSIC_DB", "3") or 3)
ACE_VOCAL_CHUNK_LUFS = float(os.environ.get("ACE_STEP_VOCAL_CHUNK_LUFS", "-16") or -16)
# "single" (default): the WHOLE song's music is ONE request, so there is nothing
#   to join and therefore no seams. If the server can't do it (too long / timeout)
#   it falls back to "chunks" (several beds, trimmed + crossfaded).
# "chunks": always use several beds.
ACE_BED_MODE = os.environ.get("ACE_STEP_BED_MODE", "single").strip().lower()
ACE_SINGLE_BED_MAX_SECONDS = float(os.environ.get("ACE_STEP_SINGLE_BED_MAX_SECONDS", "420") or 420)


def _run_ffmpeg(args):
    ff = _ffmpeg_or_raise()
    proc = subprocess.run([ff, "-y"] + [str(a) for a in args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + (proc.stderr or "")[-400:])


def _pcm_wav_seconds(path):
    import wave
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _demucs_available():
    if getattr(sys, "frozen", False):
        return True
    import importlib.util
    return importlib.util.find_spec("demucs") is not None and \
        importlib.util.find_spec("numpy") is not None


def _estimate_bpm_key(path):
    """(bpm, 'A minor') measured from a WAV, or (None, None) if librosa is missing."""
    try:
        import numpy as np
        import librosa
        y, sr = librosa.load(str(path), sr=22050, mono=True, duration=90)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(np.atleast_1d(tempo)[0])
        if bpm <= 0:
            return None, None
        while bpm < 70:
            bpm *= 2
        while bpm > 180:
            bpm /= 2
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
        major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
        minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
        names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        best_c, best_k = -2.0, None
        for i in range(12):
            for prof, mode in ((major, "major"), (minor, "minor")):
                c = float(np.corrcoef(chroma, np.roll(prof, i))[0, 1])
                if c > best_c:
                    best_c, best_k = c, f"{names[i]} {mode}"
        return int(round(bpm)), (best_k if best_c >= 0.55 else None)
    except Exception as e:
        print(f"[ACE-Step] tempo/key detection skipped: {e}", flush=True)
        return None, None


def _bed_levels_db(a, sr=44100, win=0.05):
    """Per-50ms loudness (dB) of a (frames, ch) float array."""
    import numpy as np
    hop = int(sr * win)
    n = len(a) // hop
    mono = a[:n * hop].mean(axis=1) if a.ndim > 1 else a[:n * hop]
    return 20 * np.log10(np.sqrt((mono.reshape(n, hop) ** 2).mean(axis=1)) + 1e-9), hop


def _prepare_bed_segment(src, dst):
    """Make a music bed ready to be joined: cut the quiet lead-in and the
    fade-out/dead air at its ends (judged against the bed's OWN music level,
    not a fixed dB - ACE-Step's noise floor sits around -48 dB, which a fixed
    threshold misses), then match loudness. Joining trimmed beds means the
    crossfade overlaps real music with real music, so there is no dip or
    silent gap at the seam. 44.1 kHz stereo PCM."""
    import wave
    import numpy as np
    dst = Path(dst)
    tmp = dst.with_name(dst.stem + "_tmp.wav")
    trimmed = dst.with_name(dst.stem + "_trim.wav")
    _run_ffmpeg(["-i", src, "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", tmp])
    with wave.open(str(tmp), "rb") as w:
        ch, raw = w.getnchannels(), w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, ch) / 32768.0
    db, hop = _bed_levels_db(a)
    n = len(db)
    if n < 20:
        raise RuntimeError("A generated music part is too short.")
    ref = float(np.percentile(db, 75))               # the bed's normal music level
    live = db >= ref - 15                            # "real music" windows
    ok = live[:-1] & live[1:]                        # two in a row (ignore clicks)
    idx = np.where(ok)[0]
    if len(idx) == 0:
        raise RuntimeError("A generated music part came out silent.")
    i0 = min(int(idx[0]), int(n * 0.30))             # never cut more than 30% per end
    i1 = max(int(idx[-1]) + 2, int(n * 0.70))
    cut = a[i0 * hop:min(len(a), i1 * hop)].copy()
    f = min(int(0.03 * 44100), len(cut) // 4)        # 30 ms edge fades, no clicks
    if f > 0:
        ramp = np.linspace(0, 1, f, dtype=np.float32)[:, None]
        cut[:f] *= ramp
        cut[-f:] *= ramp[::-1]
    with wave.open(str(trimmed), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes((np.clip(cut, -1, 1) * 32767).astype("<i2").tobytes())
    inner = db[i0:i1]
    dead = float((inner < ref - 30).sum()) * 0.05
    print(f"[ACE-Step] bed trimmed: {len(a) / 44100:.1f}s -> {len(cut) / 44100:.1f}s "
          f"(cut {i0 * 0.05:.1f}s head, {(n - i1) * 0.05:.1f}s tail); "
          f"{dead:.1f}s of dead air left inside", flush=True)
    _run_ffmpeg(["-i", trimmed, "-af", f"loudnorm=I={ACE_BED_LUFS}:TP=-1.5:LRA=11",
                 "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", dst])
    tmp.unlink(missing_ok=True)
    trimmed.unlink(missing_ok=True)
    if not dst.exists() or dst.stat().st_size < 1000:
        raise RuntimeError("A generated music part came out silent.")


def _join_beds(paths, out_path, xf):
    """Join beds into ONE track with long equal-power crossfades."""
    n = len(paths)
    if n == 1:
        shutil.copyfile(paths[0], out_path)
        return
    parts, prev, args = [], "[0:a]", []
    for k in range(1, n):
        lab = f"[x{k}]"
        parts.append(f"{prev}[{k}:a]acrossfade=d={xf:.3f}:c1=qsin:c2=qsin{lab}")
        prev = lab
    for p in paths:
        args += ["-i", p]
    args += ["-filter_complex", ";".join(parts), "-map", prev,
             "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", out_path]
    _run_ffmpeg(args)


def _place_vocals(vocal_paths, starts, total_seconds, out_path):
    """Lay each vocal part on one silent timeline at its start time (short
    fades on the edges) and write it as a single vocal track."""
    import wave
    import numpy as np
    sr = 44100
    total = int(total_seconds * sr) + sr
    mix = np.zeros((total, 2), dtype=np.float32)
    fade = int(0.25 * sr)
    for p, st in zip(vocal_paths, starts):
        with wave.open(str(p), "rb") as w:
            ch, rate, raw = w.getnchannels(), w.getframerate(), w.readframes(w.getnframes())
        if rate != sr:
            raise RuntimeError(f"Unexpected vocal sample rate {rate}")
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, ch) / 32768.0
        if ch == 1:
            a = np.repeat(a, 2, axis=1)
        fi, fo = min(fade, len(a) // 2), min(int(0.8 * sr), len(a) // 2)
        if fi > 0:
            a[:fi] *= (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, fi))).astype(np.float32)[:, None]
        if fo > 0:
            a[-fo:] *= (0.5 + 0.5 * np.cos(np.linspace(0, np.pi, fo))).astype(np.float32)[:, None]
        s = int(st * sr)
        e = min(total, s + len(a))
        if e > s:
            mix[s:e] += a[:e - s]
    pcm = (np.clip(mix, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _dead_air_report(path, min_len=1.0):
    """List of (start_s, length_s) where the music drops far below its normal level."""
    try:
        import wave
        import numpy as np
        _run_ffmpeg(["-i", path, "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le",
                     str(path) + "_chk.wav"])
        with wave.open(str(path) + "_chk.wav", "rb") as w:
            ch, raw = w.getnchannels(), w.readframes(w.getnframes())
        Path(str(path) + "_chk.wav").unlink(missing_ok=True)
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, ch) / 32768.0
        db, _ = _bed_levels_db(a)
        low = db < float(np.percentile(db, 75)) - 25
        out, i = [], 0
        while i < len(low):
            if low[i]:
                j = i
                while j < len(low) and low[j]:
                    j += 1
                if (j - i) * 0.05 >= min_len:
                    out.append((round(i * 0.05, 1), round((j - i) * 0.05, 1)))
                i = j
            else:
                i += 1
        return out
    except Exception as e:
        print(f"[ACE-Step] dead-air check skipped: {e}", flush=True)
        return []


def _generate_long_song_seamless(chunks, style, instruments, singer, out_path, progress_cb,
                                 language="", ref_audio=None, bed_out=None):
    """Music first (one continuous bed), voice second. See the block comment above."""
    def _p(v, s):
        if progress_cb:
            progress_cb(v, s)

    out_path = Path(out_path)
    work = out_path.parent / "chunks"
    work.mkdir(parents=True, exist_ok=True)
    n = len(chunks)
    stem = out_path.stem
    bpm = ACE_STEP_BPM or None
    key = ACE_STEP_KEY or None
    needs = [max(20.0, _needed_seconds(t, language)) for t in chunks]

    def _with_retry(label, fn):
        attempt = 0
        while True:
            t0 = time.time()
            try:
                return fn()
            except RuntimeError as e:
                took = time.time() - t0
                if attempt >= ACE_STEP_CHUNK_RETRIES or took > 540:
                    raise RuntimeError(f"{label} failed after {int(took)}s: {e}. "
                                       f"Finished parts are in {work}. If this was a timeout, "
                                       "restart ACE-Step and use a smaller ACE_STEP_CHUNK_SECONDS.")
                attempt += 1
                print(f"[ACE-Step] {label} failed ({e}); retrying...", flush=True)
                time.sleep(5)

    def _cut(src, start, length, dst):
        _run_ffmpeg(["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src,
                     "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", dst])

    # ---------------- 1) the music: ONE continuous bed ----------------------------
    bed_path = work / f"{stem}_bed_joined.wav"
    segs, lens, starts = [], [], []

    # 1a) preferred: the whole song's music in ONE request -> no joins at all.
    # Each chunk's share of the timeline follows its lyric length; one intro and
    # one outro for the whole song (each per-chunk estimate includes 12 s of both).
    weights = [max(12.0, x - 12.0) for x in needs]
    lead, tail = 8.0, 6.0
    total_est = lead + sum(weights) + tail
    single_ok = False
    if ACE_BED_MODE != "chunks" and total_est <= ACE_SINGLE_BED_MAX_SECONDS:
        raw = work / f"{stem}_bed_single_raw.wav"
        try:
            if not (bed_path.exists() and bed_path.stat().st_size > 1000):
                print(f"[ACE-Step] seamless: generating ALL the music in one piece "
                      f"(~{total_est:.0f}s)", flush=True)
                _ace_generate_one(
                    "[instrumental]", style, instruments, "instrumental", str(raw),
                    progress_cb=lambda v, s: _p(10 + 35 * min(max(float(v), 0), 100) / 100.0,
                                                f"Music (one piece): {s}"),
                    seed=ACE_STEP_SEED, bpm=bpm, key_scale=key, language=language,
                    duration=total_est)
                _prepare_bed_segment(str(raw), str(bed_path))
            L = _pcm_wav_seconds(bed_path)
            scale = L / total_est              # trimming may have shortened the bed
            pad = 3.0                          # windows overlap so no line gets cut off
            acc = lead
            for i in range(n):
                s0 = 0.0 if i == 0 else acc * scale
                acc += weights[i]
                s1 = L if i == n - 1 else min(L, acc * scale + pad)
                if s1 - s0 < 10.0:
                    s0 = max(0.0, s1 - 10.0)
                seg = work / f"{stem}_win{i + 1:02d}.wav"
                _cut(str(bed_path), s0, s1 - s0, str(seg))
                segs.append(seg)
                starts.append(s0)
                lens.append(s1 - s0)
            single_ok = True
        except RuntimeError as e:
            print(f"[ACE-Step] one-piece music failed ({e}); falling back to joined parts",
                  flush=True)
            segs, lens, starts = [], [], []

    # 1b) fallback: one bed per chunk, trimmed to real music and crossfaded.
    if not single_ok:
        print(f"[ACE-Step] seamless: {n} music parts, joined", flush=True)
        for i, text in enumerate(chunks):
            raw = work / f"{stem}_bed{i + 1:02d}_raw.wav"
            seg = work / f"{stem}_bed{i + 1:02d}.wav"
            if not (seg.exists() and seg.stat().st_size > 1000):
                def _cb(v, s, i=i):
                    _p(10 + 30 * (i + min(max(float(v), 0), 100) / 100.0) / n,
                       f"Music {i + 1}/{n}: {s}")

                # +8 s: the quiet lead-in / fade-out of every bed is cut off afterwards
                _with_retry(f"Music part {i + 1}/{n}", lambda: _ace_generate_one(
                    "[instrumental]", style, instruments, "instrumental", str(raw),
                    progress_cb=_cb, seed=ACE_STEP_SEED, bpm=bpm, key_scale=key,
                    language=language, duration=needs[i] + 8))
                _prepare_bed_segment(str(raw), str(seg))
            if i == 0 and (not bpm or not key):
                d_bpm, d_key = _estimate_bpm_key(seg)
                bpm = bpm or d_bpm
                key = key or d_key
                print(f"[ACE-Step] locking tempo={bpm} key={key} for the other parts", flush=True)
            segs.append(seg)
        _p(42, "Joining the music into one continuous track...")
        lens = [_pcm_wav_seconds(s) for s in segs]
        xf = ACE_SEAM_SECONDS or (min(8.0, max(3.0, 8 * 60.0 / bpm)) if bpm else 4.0)
        xf = max(0.5, min(xf, min(lens) / 3.0))
        t = 0.0
        for L_ in lens:
            starts.append(t)
            t += L_ - xf
        _join_beds([str(s) for s in segs], str(bed_path), xf)

    total = _pcm_wav_seconds(bed_path)
    dead = _dead_air_report(str(bed_path))
    if dead:
        print(f"[ACE-Step] WARNING: the music itself has quiet stretches at "
              f"{dead[:6]} (start s, length s) - generated that way by the model", flush=True)

    # ---------------- 2) the voice, over each part of the ONE bed -------------------
    vocals = []
    for i, text in enumerate(chunks):
        cov = work / f"{stem}_cover{i + 1:02d}.wav"
        voc = work / f"{stem}_vocals{i + 1:02d}.wav"
        voc16 = work / f"{stem}_vocals{i + 1:02d}_pcm.wav"
        if not (voc16.exists() and voc16.stat().st_size > 1000):
            def _cb(v, s, i=i):
                _p(45 + 45 * (i + min(max(float(v), 0), 100) / 100.0) / n,
                   f"Voice {i + 1}/{n}: {s}")

            _with_retry(f"Voice part {i + 1}/{n}", lambda: _ace_generate_one(
                text, style, [], singer, str(cov), progress_cb=_cb, seed=ACE_STEP_SEED,
                language=language, duration=lens[i], src_audio=str(segs[i]),
                ref_audio=ref_audio))
            _make_backtrack_from_song(
                cov, voc, lambda m, i=i: _p(45 + 45 * (i + 0.8) / n, f"Voice {i + 1}/{n}: {m}"),
                stem_name="vocals")
            # clean up + even out this part's voice (EQ, compression, level) so every
            # chunk of the song is equally clear and equally loud
            _polish_vocals(voc, voc16, ACE_VOCAL_CHUNK_LUFS)
        vocals.append(voc16)

    # ---------------- 3) one vocal track on the one music track ---------------------
    _p(92, "Putting the singing on the continuous music...")
    vocal_full = work / f"{stem}_vocals_full.wav"
    _place_vocals(vocals, starts, total, vocal_full)
    _mix_vocals_over_track(bed_path, vocal_full, out_path, polish=False)  # already polished per chunk
    if bed_out:
        shutil.copyfile(bed_path, bed_out)
    _p(98, "Song joined.")


def generate_song_audio_ace(lyrics, style, instruments, singer, out_path, progress_cb=None,
                            language="", duration=None, ref_audio=None, bed_out=None):
    """Generate a song with ACE-Step. Long songs are split into section-based
    chunks (each short enough for CPU), generated one by one with the same
    seed/style, then joined with a crossfade. Short songs use one request."""

    def _p(value, status):
        if progress_cb:
            progress_cb(value, status)

    one_kw = dict(seed=ACE_STEP_SEED, bpm=ACE_STEP_BPM or None, key_scale=ACE_STEP_KEY or None,
                  language=language, duration=duration, ref_audio=ref_audio)

    # Instrumental, or the user forced a fixed length: single request.
    if singer == "instrumental" or ACE_STEP_DURATION_SECONDS:
        return _ace_generate_one(lyrics, style, instruments, singer, out_path,
                                 progress_cb=progress_cb, **one_kw)

    # Single pass: the whole song in ONE request (length is chosen automatically
    # from the lyrics, up to the 360 s the server allows).
    if ACE_SINGLE_PASS:
        return _ace_generate_one(lyrics, style, instruments, singer, out_path,
                                 progress_cb=progress_cb, **one_kw)

    chunks = _split_ace_chunks(_clean_lyrics_for_ace(lyrics), ACE_STEP_CHUNK_SECONDS, language)
    if len(chunks) <= 1:
        return _ace_generate_one(lyrics, style, instruments, singer, out_path,
                                 progress_cb=progress_cb, **one_kw)

    # Long song: build ONE continuous music bed first, add the voice after
    # (see SEAMLESS LONG SONGS above). Needs Demucs + numpy; otherwise the
    # old chunk-and-join method below is used.
    if ACE_SEAMLESS and _demucs_available():
        return _generate_long_song_seamless(
            chunks, style, instruments, singer, out_path, progress_cb,
            language=language, ref_audio=ref_audio, bed_out=bed_out)
    print("[ACE-Step] seamless mode unavailable (needs demucs + numpy) - "
          "using the old chunk-and-join method", flush=True)

    out_path = Path(out_path)
    chunk_dir = out_path.parent / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    n = len(chunks)
    print(f"[ACE-Step] splitting song into {n} chunks "
          f"(~{ACE_STEP_CHUNK_SECONDS:.0f}s each)", flush=True)

    paths = []
    for i, text in enumerate(chunks):
        cp = chunk_dir / f"{out_path.stem}_part{i + 1:02d}.wav"
        if cp.exists() and cp.stat().st_size > 1000:        # already done
            paths.append(cp)
            continue

        def _cp(v, s, i=i):
            frac = (i + min(max(float(v), 0.0), 100.0) / 100.0) / n
            _p(15 + frac * 75, f"Chunk {i + 1}/{n}: {s}")

        attempt = 0
        while True:
            t0 = time.time()
            try:
                _ace_generate_one(text, style, instruments, singer, str(cp),
                                  progress_cb=_cp, **one_kw)
                break
            except RuntimeError as e:
                took = time.time() - t0
                # A failure after ~10 min is almost certainly the server's
                # generation timeout; its worker may still be running, so a
                # retry would only compete with it. Restart ACE-Step instead.
                if attempt >= ACE_STEP_CHUNK_RETRIES or took > 540:
                    raise RuntimeError(
                        f"Chunk {i + 1}/{n} failed after {int(took)}s: {e}. "
                        f"Chunks finished so far are in {chunk_dir}. "
                        "If this was a timeout, restart ACE-Step and use a smaller "
                        "ACE_STEP_CHUNK_SECONDS (e.g. 30).")
                attempt += 1
                print(f"[ACE-Step] chunk {i + 1}/{n} failed ({e}); retrying "
                      f"({attempt}/{ACE_STEP_CHUNK_RETRIES})...", flush=True)
                time.sleep(5)
        paths.append(cp)

    _p(92, f"Joining {n} chunks...")
    # Cut each chunk's quiet lead-in / fade-out (the source of silent gaps),
    # match their loudness, and crossfade longer than before.
    try:
        trimmed = []
        for pth in paths:
            tp = Path(pth).with_name(Path(pth).stem + "_trimmed.wav")
            _prepare_bed_segment(str(pth), str(tp))
            trimmed.append(tp)
        paths = trimmed
    except Exception as e:
        print(f"[ACE-Step] trim before join skipped: {e}", flush=True)
    _join_audio_chunks(paths, out_path, max(ACE_STEP_CROSSFADE_SECONDS, 3.0))
    _p(98, "Song joined.")


# ============================================================
# SONG + SEPARATE BACKTRACK  (full_song.wav + backtrack.wav)
# ------------------------------------------------------------
# Two COMPLETE, INDEPENDENT productions (nothing is ever separated or
# stripped out of another file):
#   RUN 1) FULL SONG  - lyrics + vocals + music.
#   RUN 2) BACKTRACK  - once the full song is finished, ACE-Step is run
#          AGAIN as a brand-new music-only production ([instrumental]),
#          so it is arranged, rendered and mixed as a real backing track
#          and is NOT a vocal-removed copy of the song (which sounds thin
#          and has voice "ghosts"). To keep it a close relative of the song
#          it re-uses the song's style / instruments / seed and is locked
#          to the song's measured tempo + key and exact length.
# Both files then get the same loudness mastering (ACE_STEP_NORMALIZE=0
# turns that off). If run 2 fails, the song is still kept and the error is
# written to backtrack_error.txt - the app NEVER falls back to Demucs here.
#
# Tuning (environment variables):
#   ACE_STEP_BACKTRACK_STEPS        inference steps for run 2 (default: same as
#                                   the song; raise e.g. to 32-50 ONLY if your
#                                   ACE-Step server runs a base/SFT model - the
#                                   turbo model should stay at 8)
#   ACE_STEP_BACKTRACK_MATCH_TEMPO  1 = lock run 2 to the song's measured BPM/key
#                                   (needs librosa; default 1)
#   ACE_STEP_BACKTRACK_HINT         extra style words for the music-only run
# ============================================================
ACE_BACKTRACK_HINT = os.environ.get(
    "ACE_STEP_BACKTRACK_HINT",
    "full band arrangement, all instruments clearly audible, balanced mix, high quality")
ACE_NORMALIZE = os.environ.get("ACE_STEP_NORMALIZE", "1").strip() != "0"
ACE_TARGET_LUFS = float(os.environ.get("ACE_STEP_TARGET_LUFS", "-14") or -14)
ACE_BACKTRACK_STEPS = int(os.environ.get("ACE_STEP_BACKTRACK_STEPS", "0") or 0) or None
ACE_BACKTRACK_MATCH_TEMPO = os.environ.get("ACE_STEP_BACKTRACK_MATCH_TEMPO", "1").strip() != "0"
# Words that keep the music-only run from inventing a sung part.
ACE_BACKTRACK_NO_VOCAL_TAGS = "instrumental, no vocals, no singing, no humming, no choir"


def _wav_seconds(path):
    try:
        import soundfile as sf
        info = sf.info(str(path))
        return info.frames / float(info.samplerate)
    except Exception:
        return 0.0


def _normalize_loudness(path, target_lufs=-14.0):
    """Even out the volume of a WAV in place (EBU R128 loudnorm, true peak
    kept under -1.5 dB). If ffmpeg fails the original file is left untouched."""
    ff = shutil.which("ffmpeg")
    if ff is None:
        return False
    path = Path(path)
    tmp = path.with_name(path.stem + "_norm.wav")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [ff, "-y", "-i", str(path), "-af",
         f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11", "-ar", "48000",
         "-c:a", "pcm_s16le", str(tmp)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=flags)
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1000:
        tmp.replace(path)
        return True
    tmp.unlink(missing_ok=True)
    print(f"[ACE-Step] loudness step skipped: {(proc.stderr or '')[-300:]}", flush=True)
    return False


def _make_backtrack_from_song(song_path, back_path, status=None, stem_name="no_vocals"):
    """Remove the vocals from the finished song with Demucs and save the
    result as back_path (a WAV). Same audio as the song, so the music is
    identical. Raises RuntimeError with the real Demucs message on failure.
    stem_name="vocals" keeps the singing instead (used by the backing-track
    mode to lift the voice out of the generated version)."""
    import glob

    def _s(msg):
        if status:
            status(msg)

    # ffmpeg: on PATH, or bundled next to the exe / inside _internal.
    if shutil.which("ffmpeg") is None:
        for d in (BASE_DIR, BASE_DIR / "_internal",
                  Path(getattr(sys, "_MEIPASS", BASE_DIR))):
            if (d / "ffmpeg.exe").exists() or (d / "ffmpeg").exists():
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                break
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg was not found (Demucs needs it). Put ffmpeg.exe next "
                           "to the app or on PATH.")

    out_dir = tempfile.mkdtemp(prefix="demucs_backtrack_")
    try:
        _s("Separating vocals from music (1-3 min)...")
        args = ["-n", "htdemucs", "--two-stems=vocals", "-o", out_dir, str(song_path)]

        if getattr(sys, "frozen", False):
            # Packaged .exe: sys.executable is the app itself and cannot run
            # "python -m demucs", so run the bundled Demucs inside this process.
            try:
                from demucs.separate import main as demucs_main
            except Exception as e:
                raise RuntimeError(f"Demucs is not bundled in this build ({e}).")
            try:
                demucs_main(args)
            except SystemExit as e:
                if e.code not in (0, None):
                    raise RuntimeError(f"Demucs failed (exit {e.code}). "
                                       "The console window shows the details.")
            except Exception as e:
                raise RuntimeError(f"Demucs failed: {e}")
        else:
            proc = subprocess.run([sys.executable, "-m", "demucs"] + args,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace",
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "")[-900:]
                hint = ""
                low = err.lower()
                if "no module named demucs" in low:
                    hint = "  FIX: pip install demucs"
                elif "torchcodec" in low:
                    hint = "  FIX: pip install torchcodec   (or: pip install \"torchaudio<2.9\")"
                raise RuntimeError(f"Demucs failed (exit {proc.returncode}): {err}{hint}")

        found = glob.glob(os.path.join(out_dir, "**", f"{stem_name}.*"), recursive=True)
        if not found:
            raise RuntimeError(f"Demucs finished but produced no '{stem_name}' file.")
        src = found[0]
        if src.lower().endswith(".wav"):
            shutil.copyfile(src, str(back_path))
        else:                                   # e.g. .mp3/.flac: convert to WAV
            conv = subprocess.run(
                [shutil.which("ffmpeg"), "-y", "-i", src, "-c:a", "pcm_s16le", str(back_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if conv.returncode != 0:
                raise RuntimeError("Could not convert the backtrack to WAV: " + (conv.stderr or "")[-300:])
        if not Path(back_path).exists() or Path(back_path).stat().st_size < 1000:
            raise RuntimeError("The backtrack file was not written.")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _generate_backtrack_rerun(song_path, back_path, style, instruments, language, _p):
    """RUN 2: a fresh, complete music-only production made by ACE-Step.
    Nothing is taken from the song file except its length, tempo and key
    (so the two files line up). Raises RuntimeError if generation fails."""
    song_path, back_path = Path(song_path), Path(back_path)

    # Same length as the finished song (the default length of an
    # "[instrumental]" request is only ~30 s, so it must be passed explicitly).
    secs = _wav_seconds(song_path)
    if not secs:
        try:
            secs = _pcm_wav_seconds(song_path)
        except Exception:
            secs = 0.0
    duration = None
    if secs > 0:
        duration = min(float(secs), float(ACE_MAX_SONG_SECONDS))
        if secs > ACE_MAX_SONG_SECONDS + 1:
            print(f"[ACE-Step] backtrack limited to {ACE_MAX_SONG_SECONDS:.0f}s "
                  f"(song is {secs:.0f}s)", flush=True)

    # Lock tempo + key to the song so the backtrack can be played/sung with it.
    bpm, key = ACE_STEP_BPM or None, ACE_STEP_KEY or None
    if ACE_BACKTRACK_MATCH_TEMPO and not (bpm and key):
        d_bpm, d_key = _estimate_bpm_key(song_path)
        bpm, key = bpm or d_bpm, key or d_key
    print(f"[ACE-Step] backtrack run: duration={duration} bpm={bpm} key={key}", flush=True)

    # Full-band, music-only style prompt.
    music_style = ", ".join(p for p in (
        (style or "").strip(), ACE_BACKTRACK_NO_VOCAL_TAGS, ACE_BACKTRACK_HINT) if p)

    tmp = back_path.with_name(back_path.stem + "_raw.wav")
    tmp.unlink(missing_ok=True)
    attempt = 0
    while True:
        try:
            _ace_generate_one(
                "[instrumental]", music_style, instruments, "instrumental", str(tmp),
                progress_cb=_p, seed=ACE_STEP_SEED, bpm=bpm, key_scale=key,
                language=language, duration=duration, infer_steps=ACE_BACKTRACK_STEPS)
            break
        except RuntimeError as e:
            if attempt >= ACE_STEP_CHUNK_RETRIES:
                raise
            attempt += 1
            print(f"[ACE-Step] backtrack run failed ({e}); retrying...", flush=True)
            time.sleep(5)
    tmp.replace(back_path)


def _generate_song_and_backtrack(folder, stem, lyrics, style, instruments, singer,
                                 language, _p, ref_audio=None):
    """Returns (main_output_path, tracks_dict, final_status_text).

    RUN 1 makes <stem>_full_song.wav (music + vocals). When it is finished,
    RUN 2 makes <stem>_backtrack.wav as a separate music-only production.
    The backtrack is never created by removing the voice from the song."""
    folder = Path(folder)
    song_path = folder / f"{stem}_full_song.wav"
    back_path = folder / f"{stem}_backtrack.wav"
    (folder / "backtrack_error.txt").unlink(missing_ok=True)

    # ---- RUN 1: full song (voice + music) ---------------------------------------
    _p(6, "File 1/2: producing the full song (voice + music)...")
    back_path.unlink(missing_ok=True)
    generate_song_audio_ace(
        lyrics, style, instruments, singer, str(song_path),
        progress_cb=lambda v, s: _p(6 + min(float(v), 100) * 0.46, "Full song: " + s),
        language=language, ref_audio=ref_audio)      # no bed_out: backtrack is a re-run
    # The song is complete: make its loudness final before anything else.
    if ACE_NORMALIZE:
        _p(53, "Mastering the full song...")
        _normalize_loudness(song_path, ACE_TARGET_LUFS)

    # ---- RUN 2: re-run ACE-Step for the music-only backtrack --------------------
    _p(55, "File 2/2: full song done - re-running to produce the backtrack "
           "as a separate music production...")
    try:
        _generate_backtrack_rerun(
            song_path, back_path, style, instruments, language,
            lambda v, s: _p(55 + min(float(v), 100) * 0.38, "Backtrack: " + s))
    except Exception as e:
        import traceback
        traceback.print_exc()
        # Keep the exact reason next to the song so it can be read later.
        try:
            (folder / "backtrack_error.txt").write_text(str(e), encoding="utf-8")
        except Exception:
            pass
        return (song_path, {"full_song": str(song_path)},
                f"Full song saved, but the BACKTRACK RUN FAILED: {e}")

    if ACE_NORMALIZE:
        _p(94, "Mastering the backtrack (same loudness as the song)...")
        _normalize_loudness(back_path, ACE_TARGET_LUFS)

    return (song_path,
            {"full_song": str(song_path), "backtrack": str(back_path)},
            "Complete!")


# ============================================================
# SING ON MY OWN BACKING TRACK  (+ optional voice reference)
# ------------------------------------------------------------
# Instead of inventing new music every time, the user loads a backing track
# in the Create Song window and the lyrics are sung OVER IT:
#
#   1) The track is converted to a clean WAV (and cut at ACE_STEP_BACKING_MAX_SECONDS).
#   2) ACE-Step runs in "cover" mode with that WAV as the source, so the sung
#      version follows the track's tempo, structure and harmony.
#   3) If "put the singing on top of my original track" is on (default), the
#      vocals are lifted out of ACE-Step's version with Demucs and mixed onto
#      the ORIGINAL file, so the music in the result is your own, untouched.
#
# Output files in the song folder:
#   <stem>_full_song.wav   singing + your track (or ACE-Step's version if mixing is off)
#   <stem>_backtrack.wav   your original track (converted to WAV, not altered)
#   <stem>_vocals.wav      the isolated singing
#   <stem>_ace_version.wav ACE-Step's own rendering (only when mixing is on)
#
# Tuning (environment variables):
#   ACE_STEP_COVER_STRENGTH      1.0 = follow the track as closely as possible
#                                (lower, e.g. 0.7, lets the model change more)
#   ACE_STEP_BACKING_MAX_SECONDS longest track used (default 360 = 6 min)
# ============================================================
ACE_COVER_STRENGTH = float(os.environ.get("ACE_STEP_COVER_STRENGTH", "1.0") or 1.0)
ACE_BACKING_MAX_SECONDS = float(os.environ.get("ACE_STEP_BACKING_MAX_SECONDS", "360") or 360)
SONG_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
SONG_AUDIO_MAX_BYTES = 150 * 1024 * 1024


def _save_song_audio_upload(file_storage, folder, label):
    """Save an uploaded audio file as <folder>/<label>.<ext>. Raises ValueError
    with a message that is safe to show the user."""
    ext = Path(file_storage.filename or "").suffix.lower()
    if ext not in SONG_AUDIO_EXTS:
        label = label.replace("_", " ")
        raise ValueError(f"The {label} file must be an audio file "
                         f"({', '.join(sorted(SONG_AUDIO_EXTS))}).")
    dest = Path(folder) / f"{label}{ext}"
    file_storage.save(dest)
    if dest.stat().st_size > SONG_AUDIO_MAX_BYTES:
        dest.unlink(missing_ok=True)
        label = label.replace("_", " ")
        raise ValueError(f"The {label} file is too large "
                         f"(max {SONG_AUDIO_MAX_BYTES // (1024 * 1024)} MB).")
    return dest


def _ffmpeg_or_raise():
    ff = shutil.which("ffmpeg")
    if ff is None:
        for d in (BASE_DIR, BASE_DIR / "_internal", Path(getattr(sys, "_MEIPASS", BASE_DIR))):
            if (d / "ffmpeg.exe").exists() or (d / "ffmpeg").exists():
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                ff = shutil.which("ffmpeg")
                break
    if ff is None:
        raise RuntimeError("ffmpeg was not found. Put ffmpeg.exe next to the app or on PATH.")
    return ff


def _prepare_backing_track(src, dst, max_seconds):
    """Convert any audio file to a 44.1 kHz stereo WAV, cut at max_seconds.
    Returns the length in seconds."""
    ff = _ffmpeg_or_raise()
    proc = subprocess.run(
        [ff, "-y", "-i", str(src), "-vn", "-t", str(int(max_seconds)),
         "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0 or not Path(dst).exists() or Path(dst).stat().st_size < 1000:
        raise RuntimeError("Could not read the backing track as audio: "
                           + (proc.stderr or "")[-300:])
    # Plain PCM WAV written just above, so the standard library can read its
    # length (no dependency on soundfile being installed).
    import wave
    with wave.open(str(dst), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _measure_lufs(path):
    """Integrated loudness (LUFS) of an audio file, or None if it can't be measured
    (silent file, no ffmpeg)."""
    import json as _json
    try:
        ff = _ffmpeg_or_raise()
        proc = subprocess.run(
            [ff, "-hide_banner", "-nostats", "-i", str(path), "-af",
             "loudnorm=I=-24:print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        m = _re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr or "", _re.S)
        if not m:
            return None
        v = float(_json.loads(m.group(0))["input_i"])
        return v if v > -70 else None
    except Exception:
        return None


# Voice chain: remove rumble + low-mid mud, add presence where consonants live (the
# part that makes WORDS understandable), gentle compression so quiet words come up
# to the level of the loud ones, then a limiter against peaks.
_VOCAL_CHAIN = ("highpass=f=90,"
                "equalizer=f=250:t=q:w=1:g=-2,"
                "equalizer=f=3500:t=q:w=1:g=3,"
                "treble=g=1.5:f=8000,"
                "acompressor=threshold=0.125:ratio=3:attack=8:release=120:makeup=2,")


def _polish_vocals(src, dst, lufs=-16.0):
    """Make an isolated voice clear and professional-sounding: EQ for
    intelligibility, compression for an even level, loudness set to `lufs`,
    limiter on the peaks. Writes 44.1 kHz stereo 16-bit WAV to dst."""
    chain = (_VOCAL_CHAIN + f"loudnorm=I={lufs}:TP=-1.5:LRA=7,"
             "alimiter=limit=0.89")
    try:
        _run_ffmpeg(["-i", src, "-af", chain, "-ar", "44100", "-ac", "2",
                     "-c:a", "pcm_s16le", dst])
    except RuntimeError as e:
        print(f"[ACE-Step] vocal polish skipped ({e}); using the plain voice", flush=True)
        _run_ffmpeg(["-i", src, "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", dst])


def _mix_vocals_over_track(track_path, vocals_path, out_path, music_gain=1.0, vocal_gain=1.0,
                           polish=True, vocal_over_music_db=None):
    """Lay the isolated vocals on top of the track so the words are clearly heard.

    The MUSIC KEEPS ITS LEVEL (music_gain=1.0). The voice is made clear and then
    set to sit `vocal_over_music_db` LUFS above the music (ACE_STEP_VOCAL_OVER_MUSIC_DB),
    the way a mixed record has its lead vocal on top. The finished mix is then
    limited and normalized as a whole.
      polish=True  : run the voice through _polish_vocals first (use for a raw voice).
      polish=False : the voice was already polished (seamless mode does it per chunk).
    """
    import math
    if vocal_over_music_db is None:
        vocal_over_music_db = ACE_VOCAL_OVER_MUSIC_DB
    ff = _ffmpeg_or_raise()
    tmp_files = []
    try:
        voc = str(vocals_path)
        if polish:
            pv = str(out_path) + "_vocpol.wav"
            tmp_files.append(pv)
            _polish_vocals(voc, pv, ACE_VOCAL_CHUNK_LUFS)
            voc = pv

        # How loud is the music, how loud is the voice -> bring the voice to
        # (music loudness + vocal_over_music_db). Music is never touched.
        music_l, voc_l = _measure_lufs(track_path), _measure_lufs(voc)
        if music_l is not None and voc_l is not None:
            gain_db = max(-12.0, min(15.0, (music_l + vocal_over_music_db) - voc_l))
        else:
            gain_db = 0.0
        gain_db += 20.0 * math.log10(max(vocal_gain, 1e-6))
        print(f"[ACE-Step] mix: music {music_l if music_l is not None else '?'} LUFS, "
              f"voice {voc_l if voc_l is not None else '?'} LUFS -> voice {gain_db:+.1f} dB "
              f"(target {vocal_over_music_db:+.1f} dB over the music)", flush=True)

        fmt = "aformat=sample_rates=44100:channel_layouts=stereo"
        tail = (f"alimiter=limit=0.89,"
                f"loudnorm=I={ACE_TARGET_LUFS}:TP=-1.5:LRA=11,aresample=44100[out]")
        # normalize=0 keeps both inputs at full level (default amix halves them);
        # older ffmpeg builds don't know it, so fall back without it.
        last_err = ""
        for amix in ("amix=inputs=2:duration=first:dropout_transition=0:normalize=0",
                     "amix=inputs=2:duration=first:dropout_transition=0"):
            graph = (f"[0:a]{fmt},volume={music_gain}[m];"
                     f"[1:a]{fmt},volume={gain_db:.2f}dB[v];"
                     f"[m][v]{amix},{tail}")
            proc = subprocess.run(
                [ff, "-y", "-i", str(track_path), "-i", voc,
                 "-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", str(out_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode == 0 and Path(out_path).exists() and Path(out_path).stat().st_size > 1000:
                return
            last_err = proc.stderr or ""
        raise RuntimeError("ffmpeg could not mix the vocals onto the track: " + last_err[-300:])
    finally:
        for f in tmp_files:
            try:
                Path(f).unlink(missing_ok=True)
            except Exception:
                pass


def _generate_song_on_backing_track(folder, stem, lyrics, style, singer, language,
                                    backing_path, voice_ref, mix_original, _p):
    """Returns (main_output_path, tracks_dict, final_status_text)."""
    folder = Path(folder)
    back_path = folder / f"{stem}_backtrack.wav"
    vocals_path = folder / f"{stem}_vocals.wav"
    full_path = folder / f"{stem}_full_song.wav"
    # With mixing on, ACE-Step's own rendering is kept under its own name.
    ace_path = folder / (f"{stem}_ace_version.wav" if mix_original else f"{stem}_full_song.wav")

    _p(6, "Reading your backing track...")
    seconds = _prepare_backing_track(backing_path, back_path, ACE_BACKING_MAX_SECONDS)
    if seconds < 10:
        raise RuntimeError(f"The backing track is only {seconds:.0f}s long - "
                           "ACE-Step needs at least about 10 seconds.")
    notes = []
    if seconds >= ACE_BACKING_MAX_SECONDS - 1:
        notes.append(f"track was cut to the first {int(ACE_BACKING_MAX_SECONDS // 60)} minutes")
    lyr = _clean_lyrics_for_ace(lyrics)
    need = _needed_seconds(lyr, _guess_vocal_language(lyr, language))
    if need > seconds + 5:
        notes.append(f"the lyrics need about {need / 60:.1f} min but your track is "
                     f"{seconds / 60:.1f} min, so the singing may be rushed")

    _p(10, f"Singing the lyrics over your {seconds:.0f}s track (ACE-Step cover mode)...")
    # Instruments are NOT sent: the music already exists. bpm/key are NOT sent
    # either, so the model takes them from the track itself.
    _ace_generate_one(
        lyrics, style, [], singer, str(ace_path),
        progress_cb=lambda v, s: _p(10 + min(float(v), 100) * 0.50, "On your track: " + s),
        seed=ACE_STEP_SEED, language=language, duration=seconds,
        src_audio=str(back_path), ref_audio=voice_ref)

    if not mix_original:
        if ACE_NORMALIZE:
            _normalize_loudness(ace_path, ACE_TARGET_LUFS)
        status = "Complete!" + (" Note: " + "; ".join(notes) if notes else "")
        return (ace_path, {"full_song": str(ace_path), "backtrack": str(back_path)}, status)

    _p(62, "Lifting the singing out of the generated version (1-3 min)...")
    try:
        _make_backtrack_from_song(ace_path, vocals_path,
                                  lambda msg: _p(72, "Vocals: " + str(msg)),
                                  stem_name="vocals")
        _p(90, "Putting the singing on top of your original track...")
        _mix_vocals_over_track(back_path, vocals_path, full_path)
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            (folder / "mix_error.txt").write_text(str(e), encoding="utf-8")
        except Exception:
            pass
        # Fall back to ACE-Step's own version so the run isn't wasted.
        shutil.copyfile(ace_path, full_path)
        if ACE_NORMALIZE:
            _normalize_loudness(full_path, ACE_TARGET_LUFS)
        return (full_path,
                {"full_song": str(full_path), "backtrack": str(back_path)},
                "Song saved, but your ORIGINAL music could not be mixed in "
                f"(full_song is ACE-Step's version of your track): {e}")

    if ACE_NORMALIZE:
        _p(96, "Matching the volume...")
        _normalize_loudness(full_path, ACE_TARGET_LUFS)
    status = "Complete!" + (" Note: " + "; ".join(notes) if notes else "")
    return (full_path,
            {"full_song": str(full_path), "backtrack": str(back_path),
             "vocals": str(vocals_path), "ace_version": str(ace_path)},
            status)


# Populated on first use by generate_song_audio_musicgen() — same reasoning
# as the ACE-Step singleton above: load once, reuse for the life of the
# process. MusicGen has NO lyrics/vocals input at all — it only takes a
# text style description — so `lyrics` and `singer` are intentionally not
# passed to it; the dashboard shows a note about this when MusicGen is
# selected (see the Backend dropdown handling in app.js).
_MUSICGEN_MODEL = None
_MUSICGEN_PROCESSOR = None
_MUSICGEN_LOCK = threading.Lock()

MUSICGEN_MODEL_ID = "facebook/musicgen-small"   # smallest checkpoint = fastest on CPU
MUSICGEN_MAX_NEW_TOKENS = 512                    # roughly ~10s of audio at 50 tokens/sec


def generate_song_audio_musicgen(style, instruments, out_path, progress_cb=None):
    """Generate INSTRUMENTAL audio (no vocals, lyrics ignored) from a style
    description, using Meta's MusicGen via Hugging Face transformers
    (CPU-capable, no GPU required)."""
    global _MUSICGEN_MODEL, _MUSICGEN_PROCESSOR

    def _p(value, status):
        if progress_cb:
            progress_cb(value, status)

    try:
        from transformers import AutoProcessor, MusicgenForConditionalGeneration
    except ImportError:
        raise RuntimeError(
            "MusicGen isn't installed. On the machine running app.py: "
            "pip install transformers torch (the CPU build of torch is fine)."
        )

    with _MUSICGEN_LOCK:
        if _MUSICGEN_MODEL is None:
            _p(18, f"Loading {MUSICGEN_MODEL_ID} (first run downloads it "
                    "from Hugging Face)...")
            _MUSICGEN_PROCESSOR = AutoProcessor.from_pretrained(MUSICGEN_MODEL_ID)
            _MUSICGEN_MODEL = MusicgenForConditionalGeneration.from_pretrained(MUSICGEN_MODEL_ID)

    prompt_parts = [style.strip()] if style.strip() else []
    if instruments:
        prompt_parts.append(", ".join(instruments))
    prompt = ", ".join(p for p in prompt_parts if p) or "instrumental music"

    _p(30, "Generating instrumental audio with MusicGen (no vocals — "
            "lyrics aren't used by this backend)...")

    inputs = _MUSICGEN_PROCESSOR(text=[prompt], padding=True, return_tensors="pt")
    audio_values = _MUSICGEN_MODEL.generate(**inputs, max_new_tokens=MUSICGEN_MAX_NEW_TOKENS)
    sampling_rate = _MUSICGEN_MODEL.config.audio_encoder.sampling_rate

    _p(90, "Saving generated audio...")
    import scipy.io.wavfile
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    scipy.io.wavfile.write(str(out_path), rate=sampling_rate,
                            data=audio_values[0, 0].cpu().numpy())


# ============================================================
# AI LYRICS WRITER  (the "Generate lyrics" button in Create Song)
# ------------------------------------------------------------
# POST /api/generate_lyrics_ai  {style, subject, language, title?}
#   -> {title, lyrics}  (lyrics already use [verse]/[chorus] tags, short
#      sung lines, ready to paste into the Lyrics box)
#
# Which AI writes the lyrics (set environment variables, then restart):
#   Anthropic (default when a key is set):
#       ANTHROPIC_API_KEY=sk-ant-...
#       LYRICS_LLM_MODEL=claude-sonnet-5-5        (optional)
#   Local, free, offline (Ollama):
#       LYRICS_LLM_PROVIDER=ollama
#       LYRICS_LLM_MODEL=qwen2.5:7b               (any model you pulled)
#       OLLAMA_URL=http://127.0.0.1:11434         (optional)
# ============================================================
LYRICS_LLM_PROVIDER = os.environ.get("LYRICS_LLM_PROVIDER", "").strip().lower()
LYRICS_LLM_MODEL = os.environ.get("LYRICS_LLM_MODEL", "").strip()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

_LYRIC_LANG_NAMES = {"ar": "Arabic", "en": "English", "zh": "Chinese", "ja": "Japanese",
                     "ko": "Korean", "ru": "Russian", "es": "Spanish", "fr": "French",
                     "de": "German", "it": "Italian", "pt": "Portuguese", "hi": "Hindi",
                     "tr": "Turkish", "fa": "Persian", "ur": "Urdu"}


def _lyrics_ai_settings():
    """Settings for the lyrics AI. Environment variables win; otherwise they are
    read from a plain-text file called lyrics_ai.env next to the app (so a
    packaged .exe started from Explorer can use them too). The file is read on
    every call, so editing it needs NO restart. One KEY=VALUE per line:
        ANTHROPIC_API_KEY=sk-ant-...
        LYRICS_LLM_PROVIDER=ollama      (optional)
        LYRICS_LLM_MODEL=...            (optional)
        OLLAMA_URL=http://127.0.0.1:11434   (optional)
    """
    vals = {}
    try:
        f = BASE_DIR / "lyrics_ai.env"
        if f.exists():
            for line in f.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    for k in ("ANTHROPIC_API_KEY", "LYRICS_LLM_PROVIDER", "LYRICS_LLM_MODEL", "OLLAMA_URL"):
        if os.environ.get(k, "").strip():
            vals[k] = os.environ[k].strip()
    return vals


def _pick_ollama_model(base_url, wanted=""):
    """Choose a model that is really installed in Ollama. Uses the wanted one if
    it exists. Otherwise picks a general-purpose model, preferring ones small
    enough (<= ~9 GB) to answer in a reasonable time on a CPU."""
    import urllib.request
    try:
        with urllib.request.urlopen(base_url + "/api/tags", timeout=8) as r:
            models = json.loads(r.read().decode("utf-8")).get("models") or []
    except Exception:
        raise RuntimeError("Ollama is not running. Start the Ollama app (or run 'ollama serve'), "
                           "or save an Anthropic key in 'AI key settings'.")
    sizes = {m.get("name", ""): int(m.get("size") or 0) for m in models if m.get("name")}
    names = list(sizes)
    if not names:
        raise RuntimeError("Ollama has no models installed. Run:  ollama pull gemma3:4b")
    if wanted and wanted in names:
        return wanted
    local = [n for n in names if "cloud" not in n.lower()]
    good = [n for n in local if not any(b in n.lower() for b in
            ("r1", "coder", "embed", "vision", "reason", "think", "gpt-oss", "deepseek"))]
    pool = good or local or names
    small = [n for n in pool if 0 < sizes.get(n, 0) <= 9_000_000_000]
    pool = small or pool
    for pref in ("gemma", "mistral", "llama", "qwen", "phi"):
        for n in pool:
            if pref in n.lower():
                return n
    return pool[0]


def _llm_complete(system, user, max_tokens=1200, timeout=None):
    """One text completion from the configured AI. Returns the reply text."""
    import urllib.request
    import urllib.error

    import socket
    timeout = timeout or int(os.environ.get("LYRICS_LLM_TIMEOUT", "900") or 900)
    cfg = _lyrics_ai_settings()
    api_key = cfg.get("ANTHROPIC_API_KEY", "")
    # No key saved = use the free local Ollama model (default).
    provider = (cfg.get("LYRICS_LLM_PROVIDER", "").lower()
                or ("anthropic" if api_key else "ollama"))
    model_name = cfg.get("LYRICS_LLM_MODEL", "")
    ollama_url = cfg.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    if provider == "ollama":
        url = ollama_url + "/api/chat"
        model_name = _pick_ollama_model(ollama_url, model_name)
        print(f"[lyrics-ai] asking Ollama model '{model_name}' (waits up to {timeout}s)...", flush=True)
        body = {"model": model_name, "stream": False, "think": False, "keep_alive": "30m",
                "options": {"num_predict": 900, "temperature": 0.8},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        headers = {"Content-Type": "application/json"}
    elif provider == "anthropic":
        key = api_key
        if not key:
            raise RuntimeError("No Anthropic key found. Put ANTHROPIC_API_KEY=sk-ant-... in "
                               "lyrics_ai.env next to the app, or use a local model with "
                               "LYRICS_LLM_PROVIDER=ollama.")
        url = "https://api.anthropic.com/v1/messages"
        body = {"model": model_name or "claude-sonnet-5-5", "max_tokens": max_tokens,
                "system": system, "messages": [{"role": "user", "content": user}]}
        headers = {"Content-Type": "application/json", "x-api-key": key,
                   "anthropic-version": "2023-06-01"}
    else:
        raise RuntimeError("No lyrics AI is configured. Create a file named lyrics_ai.env next "
                           f"to the app ({BASE_DIR}) containing the line "
                           "ANTHROPIC_API_KEY=sk-ant-... (or LYRICS_LLM_PROVIDER=ollama).")

    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Lyrics AI returned HTTP {e.code}: "
                           f"{e.read().decode('utf-8', 'replace')[:300]}")
    except (socket.timeout, TimeoutError):
        raise RuntimeError(f"The lyrics AI did not finish within {timeout // 60} minutes - the "
                           "model is too big for this PC. Install a smaller one "
                           "(ollama pull gemma3:4b) or save an Anthropic key in 'AI key settings'.")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Can't reach the lyrics AI ({e.reason}).")

    if provider == "ollama":
        msg = data.get("message") or {}
        text = (msg.get("content") or "")
        if not text.strip() and msg.get("thinking"):
            raise RuntimeError(f"The model '{model_name}' only produced hidden 'thinking' text and no "
                               "lyrics. Use a normal model such as gemma3 or llama3.")
    else:
        text = "".join(b.get("text", "") for b in (data.get("content") or [])
                       if b.get("type") == "text")
    return text.strip()


def _write_lyrics_with_ai(style, subject, language, title=""):
    lang_code = _normalize_language(language)
    if lang_code:
        lang_name = _LYRIC_LANG_NAMES.get(lang_code, lang_code)
        lang_rule = f"Write ALL the lyrics in {lang_name}."
    else:
        lang_name = ""
        lang_rule = "Write the lyrics in the same language as the subject description."

    dialect_rule = ""
    if lang_code == "ar":
        dia = os.environ.get("ACE_STEP_ARABIC_DIALECT", "Egyptian").strip()
        if dia:
            dialect_rule = (f"Write in everyday spoken {dia} Arabic (colloquial), not Modern "
                            f"Standard Arabic: use natural {dia} words and spelling as people "
                            "actually speak, e.g. for Egyptian: عايز، إزاي، ده، دي، مش، كده، دلوقتي. ")

    system = (
        "You are a professional songwriter. You write singable song lyrics and reply with "
        "the lyrics ONLY: no explanations, no notes, no markdown, no code fences.\n"
        "FORMAT (strict, an AI singer reads this directly):\n"
        "- First line: TITLE: <short song title>\n"
        "- Then a blank line, then the lyrics.\n"
        "- Put each section tag alone on its own line in square brackets, lowercase: "
        "[verse], [chorus], [verse], [chorus], [bridge], [chorus]. Use [intro] or [outro] "
        "only if they contain sung words.\n"
        "- Under each tag write short sung lines of about 4 to 8 words, one line per row.\n"
        "- 2 verses of 4 lines, a chorus of 4 lines that repeats, a bridge of 2 to 4 lines. "
        "Write about 32 to 44 lines in total so the song runs roughly 3.5 to 5 minutes; \n"
        "never more than 6 minutes.\n"
        "- Match the requested musical style in mood, imagery and rhythm. Use natural "
        "rhyme and a memorable chorus. Stay on the given subject.\n"
        "- Do not write stage directions or text in parentheses."
    )
    user = (f"Musical style: {style or 'pop'}\n"
            f"Song subject: {subject}\n"
            + (f"Working title: {title}\n" if title else "")
            + f"{lang_rule} {dialect_rule}")

    raw = _llm_complete(system, user)
    raw = _re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw.strip())     # drop code fences
    out_title, body = "", raw
    m = _re.match(r"\s*(?:TITLE|Title|العنوان)\s*[:：]\s*(.+?)\s*\n", raw)
    if m:
        out_title = m.group(1).strip().strip('"*')
        body = raw[m.end():].strip()
    body = _re.sub(r"\n{3,}", "\n\n", body).strip()
    if not body:
        raise RuntimeError("The AI returned no lyrics. Try again.")
    return out_title, body


@app.route("/api/lyrics_ai_key", methods=["GET", "POST"])
@login_required
def lyrics_ai_key():
    """GET: is a key saved?  POST {key}: save it to lyrics_ai.env (admin only)."""
    if request.method == "GET":
        k = _lyrics_ai_settings().get("ANTHROPIC_API_KEY", "")
        return jsonify(has_key=bool(k), hint=(k[:7] + "..." + k[-4:]) if len(k) > 14 else "")
    if session.get("role") != "admin":
        return jsonify(error="Only an admin can change the AI key."), 403
    key = ((request.get_json(force=True) or {}).get("key") or "").strip()
    if not key.startswith("sk-ant-") or len(key) < 30 or any(c.isspace() for c in key):
        return jsonify(error="That is not an Anthropic key. It is one long string "
                             "starting with sk-ant- (create it at console.anthropic.com)."), 400
    f = BASE_DIR / "lyrics_ai.env"
    lines = []
    try:
        if f.exists():
            lines = [l for l in f.read_text(encoding="utf-8-sig").splitlines()
                     if not l.strip().startswith("ANTHROPIC_API_KEY")]
    except Exception:
        lines = []
    lines.append("ANTHROPIC_API_KEY=" + key)
    try:
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        return jsonify(error=f"Could not save the key file: {e}"), 500
    return jsonify(ok=True)


@app.route("/api/generate_lyrics_ai", methods=["POST"])
@login_required
def generate_lyrics_ai():
    data = request.get_json(force=True) or {}
    subject = (data.get("subject") or "").strip()[:800]
    style = (data.get("style") or "").strip()[:300]
    title = (data.get("title") or "").strip()[:120]
    if not subject:
        return jsonify(error="Describe what the song is about first."), 400
    try:
        out_title, lyrics = _write_lyrics_with_ai(style, subject, data.get("language"), title)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[lyrics-ai] FAILED: {e}", flush=True)
        return jsonify(error=str(e)), 502
    print(f"[lyrics-ai] OK - {len(lyrics.splitlines())} lines", flush=True)
    return jsonify(title=out_title, lyrics=lyrics)


@app.route("/api/create_song", methods=["POST"])
@login_required
def create_song():
    backing_file = voice_file = None
    if request.mimetype == "multipart/form-data":
        # With audio attached, the page sends the usual JSON fields as one
        # "payload" form field, plus optional files: backing_track, voice_ref.
        try:
            data = json.loads(request.form.get("payload") or "{}")
        except Exception:
            return jsonify(error="Bad request data"), 400
        backing_file = request.files.get("backing_track")
        voice_file = request.files.get("voice_ref")
    else:
        data = request.get_json(force=True) or {}

    lyrics = (data.get("lyrics") or "").strip()
    if not lyrics:
        return jsonify(error="Lyrics can't be empty"), 400

    title = (data.get("title") or "").strip() or "Untitled Song"
    style = (data.get("style") or "").strip()
    singer = data.get("singer") or "auto"
    instruments = data.get("instruments") or []
    if not isinstance(instruments, list):
        instruments = [str(instruments)]
    instruments = [str(i) for i in instruments]

    # The two panel options. Both are ACTIVE by default: if the page doesn't
    # send them (older page / old saved setup) they count as ON.
    #   include_title_lyrics -> save title + lyrics with the song
    #   stems (backtrack)    -> also create the separate music-only backtrack
    stems = _as_bool(data.get("stems", data.get("backtrack")), True)
    include_title_lyrics = _as_bool(data.get("include_title_lyrics"), True)
    backend = data.get("backend") or "ace"
    if backend not in ("ace", "musicgen"):
        return jsonify(error=f"Unknown backend: {backend}"), 400

    # Own backing track / voice reference (ACE-Step only).
    use_backing = bool(backing_file and backing_file.filename)
    use_voice = bool(voice_file and voice_file.filename)
    if (use_backing or use_voice) and backend != "ace":
        return jsonify(error="A backing track or voice reference only works with the "
                             "ACE-Step backend."), 400
    if use_backing and singer == "instrumental":
        return jsonify(error="A backing track is for sung lyrics - pick a singer other "
                             "than Instrumental."), 400
    mix_original = _as_bool(data.get("mix_original"), True)

    backing_path = voice_path = None
    in_dir = None
    if use_backing or use_voice:
        updir, _ = user_dirs(session["user"])
        in_dir = updir / "song_inputs" / uuid.uuid4().hex[:12]
        in_dir.mkdir(parents=True, exist_ok=True)
        try:
            if use_backing:
                backing_path = _save_song_audio_upload(backing_file, in_dir, "backing_track")
            if use_voice:
                voice_path = _save_song_audio_upload(voice_file, in_dir, "voice_reference")
        except ValueError as e:
            shutil.rmtree(in_dir, ignore_errors=True)
            return jsonify(error=str(e)), 400

    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "progress": 0,
            "status":   "Ready",
            "srt":      "",
            "output":   "",
            "input":    "",
            "user":     session["user"],
            "_ts":      time.time(),
        }
        _save_job(job_id)

    language = _normalize_language(data.get("language"))

    threading.Thread(
        target=_run_create_song_job,
        args=(job_id, title, lyrics, style, instruments, singer, backend, language, stems,
              include_title_lyrics,
              str(backing_path) if backing_path else None,
              str(voice_path) if voice_path else None,
              mix_original),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id)


def _as_bool(value, default=True):
    """Tolerant bool for JSON/form values. None/"" -> default (so the two
    panel options are ON unless the page explicitly sends false)."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)


def _run_create_song_job(job_id, title, lyrics, style, instruments, singer, backend="ace",
                         language="", stems=True, include_title_lyrics=True,
                         backing_path=None, voice_path=None, mix_original=True):
    try:
        with LOCK:
            user = JOBS[job_id].get("user", "shared")

        def _p(value, status):
            with LOCK:
                JOBS[job_id]["progress"] = float(value)
                JOBS[job_id]["status"]   = status
                JOBS[job_id]["_ts"]      = time.time()
                if int(value) % 10 == 0:
                    _save_job(job_id)

        _p(5, "Preparing song request...")

        stem = secure_filename(title) or "song"
        songs_folder = OUTPUT_DIR / user / "Songs" / f"{stem}_{int(time.time())}"
        songs_folder.mkdir(parents=True, exist_ok=True)

        # Write the request metadata first, so it's browsable even if
        # generation below fails partway through (see generate_song_audio).
        if include_title_lyrics:
            (songs_folder / "lyrics.txt").write_text(f"{title}\n\n{lyrics}\n", encoding="utf-8")
        (songs_folder / "song_request.json").write_text(json.dumps({
            "title": title,
            "style": style,
            "singer": singer,
            "instruments": instruments,
            "backend": backend,
            "language": language or "auto",
            "with_backtrack": bool(stems),
            "include_title_lyrics": bool(include_title_lyrics),
            "own_backing_track": bool(backing_path),
            "voice_reference": bool(voice_path),
            "mix_original": bool(mix_original),
        }, indent=2), encoding="utf-8")

        # Move the uploaded audio into the song folder (kept as a record of
        # what this song was made from) and clean the temporary upload folder.
        backing_in = voice_in = None
        for src, label in ((backing_path, "input_backing_track"), (voice_path, "input_voice_reference")):
            if not src:
                continue
            dest = songs_folder / (label + Path(src).suffix.lower())
            shutil.move(src, str(dest))
            if label == "input_backing_track":
                backing_in = dest
            else:
                voice_in = dest
        for src in (backing_path, voice_path):
            if src:
                shutil.rmtree(Path(src).parent, ignore_errors=True)
                break

        tracks = None
        final_status = "Complete!"
        if backing_in:
            # The lyrics are sung over the user's own track (see the
            # "SING ON MY OWN BACKING TRACK" section above).
            out_path, tracks, final_status = _generate_song_on_backing_track(
                songs_folder, stem, lyrics, style, singer, language,
                str(backing_in), str(voice_in) if voice_in else None, mix_original, _p)
        elif stems and backend == "ace" and singer != "instrumental":
            # full_song.wav, then a SEPARATE re-run for backtrack.wav (no vocal removal)
            out_path, tracks, final_status = _generate_song_and_backtrack(
                songs_folder, stem, lyrics, style, instruments, singer, language, _p,
                ref_audio=str(voice_in) if voice_in else None)
        else:
            _p(15, f"Generating song with {backend}...")
            # .wav, not .mp3 — both backends output WAV audio.
            out_path = songs_folder / f"{stem}.wav"
            generate_song_audio(lyrics, style, instruments, singer,
                                 str(out_path), backend=backend, progress_cb=_p,
                                 language=language,
                                 ref_audio=str(voice_in) if voice_in else None)

        with LOCK:
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["status"]   = final_status
            JOBS[job_id]["output"]   = str(out_path)
            JOBS[job_id]["song"]     = str(out_path)
            JOBS[job_id]["tracks"]   = tracks
            JOBS[job_id]["folder"]   = str(songs_folder)
            JOBS[job_id]["files"]    = [p.name for p in songs_folder.iterdir() if p.is_file()]
            JOBS[job_id]["_ts"]      = time.time()
            _save_job(job_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"]   = f"Error: {e}"
            JOBS[job_id]["progress"] = 0
            JOBS[job_id]["_ts"]      = time.time()
            _save_job(job_id)


# ============================================================
# SAVED AI SETUPS ("presets") — save / list / load / delete
# Stored per user in outputs/<user>/Presets/<name>.json
# ============================================================

PRESET_KEYS = ("title", "style", "singer", "instruments", "backend", "lyrics", "language",
               "include_title_lyrics", "backtrack")
# The two radio/check options: saved as real booleans, ON by default.
PRESET_BOOL_KEYS = ("include_title_lyrics", "backtrack")
PRESET_MAX_BYTES = 200_000


def _preset_dir():
    _, outdir = user_dirs(session["user"])
    d = outdir / "Presets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _preset_path(name):
    stem = secure_filename((name or "").strip())
    if not stem:
        return None, None
    return stem, _preset_dir() / f"{stem}.json"


def _last_preset_file():
    return _preset_dir() / "_last.txt"      # .txt, so it never shows up in the *.json listing


def _set_last_preset(stem):
    try:
        _last_preset_file().write_text(stem, encoding="utf-8")
    except Exception:
        pass


def _get_last_preset():
    """Name of the setup the user last chose (None if unset or since deleted)."""
    try:
        f = _last_preset_file()
        if f.exists():
            name = f.read_text(encoding="utf-8").strip()
            if name and (_preset_dir() / f"{name}.json").exists():
                return name
    except Exception:
        pass
    return None


@app.route("/api/song_presets", methods=["GET"])
@login_required
def list_song_presets():
    items = []
    for f in sorted(_preset_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        items.append({"name": f.stem, "saved": int(f.stat().st_mtime)})
    return jsonify(presets=items, last=_get_last_preset())


@app.route("/api/song_presets", methods=["POST"])
@login_required
def save_song_preset():
    data = request.get_json(force=True, silent=True) or {}
    stem, path = _preset_path(data.get("name"))
    if not stem:
        return jsonify(error="Give the setup a name"), 400
    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        return jsonify(error="Invalid settings"), 400

    clean = {}
    for k in PRESET_KEYS:
        if k in settings:
            v = settings[k]
            if k == "instruments":
                v = v if isinstance(v, list) else [v]
                v = [str(i) for i in v]
            elif k in PRESET_BOOL_KEYS:
                v = _as_bool(v, True)
            else:
                v = str(v)
            clean[k] = v
    # Always store both options so a saved setup remembers them explicitly.
    for k in PRESET_BOOL_KEYS:
        clean.setdefault(k, True)
    if not clean:
        return jsonify(error="Nothing to save"), 400
    blob = json.dumps({"name": stem, "settings": clean}, indent=2, ensure_ascii=False)
    if len(blob.encode("utf-8")) > PRESET_MAX_BYTES:
        return jsonify(error="Setup is too large to save"), 400
    path.write_text(blob, encoding="utf-8")
    _set_last_preset(stem)
    return jsonify(ok=True, name=stem)


@app.route("/api/song_presets/_last", methods=["GET"])
@login_required
def load_last_song_preset():
    """Startup helper: the full setup (incl. lyrics) the user last chose,
    or the most recently saved one if none was chosen yet.
    Returns {} (200) when the user has no saved setups at all."""
    name = _get_last_preset()
    if not name:
        files = sorted(_preset_dir().glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        name = files[0].stem if files else None
    if not name:
        return jsonify({})
    return load_song_preset(name)


@app.route("/api/song_presets/<name>", methods=["GET"])
@login_required
def load_song_preset(name):
    stem, path = _preset_path(name)
    if not stem or not path.exists():
        return jsonify(error="Saved setup not found"), 404
    try:
        preset = json.loads(path.read_text(encoding="utf-8"))
        # Setups saved before these options existed load with both ON.
        st = preset.setdefault("settings", {})
        for k in PRESET_BOOL_KEYS:
            st[k] = _as_bool(st.get(k), True)
        _set_last_preset(stem)
        return jsonify(preset)
    except Exception:
        return jsonify(error="That saved setup is damaged"), 500


@app.route("/api/song_presets/<name>", methods=["DELETE"])
@login_required
def delete_song_preset(name):
    stem, path = _preset_path(name)
    if not stem or not path.exists():
        return jsonify(error="Saved setup not found"), 404
    path.unlink()
    return jsonify(ok=True)


# ============================================================
# NEW ROUTES (added for the Tkinter-style dashboard)
# ============================================================

@app.route("/api/save_srt", methods=["POST"])
@login_required
def save_srt():
    """Save edited SRT content back to the user's outputs folder.

    Accepts either a bare filename ("Song.srt") or a path relative to
    the user's outputs folder ("SRT/Song/Song.srt"), since generated
    SRTs live under outputs/<user>/SRT/<song>/, not flat in
    outputs/<user>/.
    """
    data = request.get_json() or {}
    name = data.get("srt")
    content = data.get("content")
    if not name or content is None:
        return jsonify(error="Missing srt name or content"), 400

    _, outdir = user_dirs(session["user"])
    outdir_resolved = outdir.resolve()

    # Normalize and prevent path traversal, but allow nested subfolders
    target = (outdir / name).resolve()
    if outdir_resolved not in target.parents and target != outdir_resolved:
        return jsonify(error="Invalid path"), 400

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return jsonify(ok=True, file=str(target.relative_to(outdir_resolved)))


@app.route("/api/output_preview")
@login_required
def output_preview():
    """Return the raw text of a user's output file — used by app.js to
    restore the SRT preview after a browser reload."""
    rel = request.args.get("path", "")
    if not rel:
        return "", 400

    _, outdir = user_dirs(session["user"])
    outdir_resolved = outdir.resolve()
    target = (outdir / rel).resolve()
    if outdir_resolved not in target.parents and target != outdir_resolved:
        return "", 400
    if not target.exists() or not target.is_file():
        return "", 404

    return (target.read_text(encoding="utf-8"),
            200,
            {"Content-Type": "text/plain; charset=utf-8"})


@app.route("/api/output_abs")
@login_required
def output_abs():
    """Return the complete on-disk path for a file under the user's
    outputs folder (given the relative path app.js keeps in state.srt).
    Used by the GUI's "Copy path" button after a page reload."""
    rel = request.args.get("path", "")
    if not rel:
        return jsonify(error="No path"), 400
    _, outdir = user_dirs(session["user"])
    outdir_resolved = outdir.resolve()
    target = (outdir / rel).resolve()
    if outdir_resolved not in target.parents and target != outdir_resolved:
        return jsonify(error="Bad path"), 400
    return jsonify(path=str(target), exists=target.exists())


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
@login_required
def cancel_job(job_id):
    """Best-effort cancel — marks the job as cancelled."""
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Job not found"), 404
    if not _owns_job(job):
        return jsonify(error="Forbidden"), 403
    with LOCK:
        JOBS[job_id]["status"]   = "Cancelled"
        JOBS[job_id]["progress"] = 0
        JOBS[job_id]["_ts"]      = time.time()
        _save_job(job_id)
    return jsonify(ok=True)


@app.route("/api/browse/<kind>")
@login_required
def browse_folder(kind):
    """
    Show the user's output folder(s) for `kind`.

    Most kinds have ONE top-level folder — outputs/<user>/<Kind>/ —
    with a subfolder per song inside it (see _run_pipeline,
    _run_video_job, _run_lyrics_and_tab_job, _run_create_song_job).
    The "lyrics_tab" kind is the exception: the dashboard's "Export
    Lyrics" and "Export Guitar Solo Tab" buttons were merged into one
    ("Export Lyrics & Tab"), but their outputs still live in the two
    separate folders they always did (Lyrics/ and Tabs/), so its one
    Browse button here just lists/opens both.

    If the request is coming from the same machine that's running this
    server, we open the folder(s) in the real, native OS file explorer
    (Explorer/Finder/whatever). The person clicking the button is
    sitting at this machine, so there's no ambiguity about whose
    window should pop up.

    If the request comes from a different device on the network, there
    is no way for a web page to open a native file-explorer window on
    the REQUESTING device — no browser grants any website that
    capability, for security reasons, regardless of framework. So for
    remote requests we fall back to an in-page listing (merged across
    all of this kind's folders) with per-file download links, letting
    that device pull the files down to itself.
    """
    _, outdir = user_dirs(session["user"])

    folder_names_for_kind = {
        "srt":           ["SRT"],
        "karaoke":       ["Karaoke"],
        "lyrics_tab":    ["Lyrics", "Tabs"],
        "transcription": ["Transcription"],
        "songs":         ["Songs"],
    }
    folder_names = folder_names_for_kind.get(kind)
    if folder_names is None:
        return jsonify(error=f"Unknown browse kind: {kind}"), 400

    targets = []
    for name in folder_names:
        t = outdir / name
        t.mkdir(parents=True, exist_ok=True)
        targets.append(t)

    display_path = " & ".join(folder_names)

    if _is_local_request():
        opened_any = False
        for t in targets:
            if _open_native_folder(t):
                opened_any = True
        if opened_any:
            return jsonify(ok=True, opened=True, path=display_path)

    found = []
    for t in targets:
        found.extend(p for p in t.rglob("*") if p.is_file())
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    files = [{
        "name":    p.name,
        "rel":     str(p.relative_to(outdir)),   # for /api/download_output/<rel>
        "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
    } for p in found]

    return jsonify(ok=True, opened=False, path=display_path, files=files)


# ============================================================
# RUN
# ============================================================
def _lan_ip():
    """Best-effort guess at this machine's LAN IP (e.g. 192.168.x.x)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _open_browser(port):
    """Wait until the server accepts connections, then open the default browser."""
    import webbrowser

    def _wait_and_open():
        url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.3)
        webbrowser.open(url)

    threading.Thread(target=_wait_and_open, daemon=True).start()


if __name__ == "__main__":
    ip = _lan_ip()

    # ACE-Step is started automatically the first time an AI button needs it.
    if _ace_is_running():
        print(f"[ACE] ACE-Step is reachable at {ACE_STEP_API_URL}", flush=True)
    else:
        print(f"[ACE] ACE-Step is not running at {ACE_STEP_API_URL}. "
              "It will be started automatically when you use an AI button.",
              flush=True)

    try:
        from waitress import serve
        port = 8000
        print("Starting production server (waitress)")
        print(f" * Running on http://127.0.0.1:{port}")
        print(f" * Running on http://{ip}:{port}")
        _open_browser(port)
        serve(app, host="0.0.0.0", port=port, channel_timeout=3600, threads=8)
    except ImportError:
        port = 5000
        print("waitress not installed — falling back to Flask's dev server.")
        print("Run 'pip install waitress' to use the production server instead.")
        print(f" * Running on http://127.0.0.1:{port}")
        print(f" * Running on http://{ip}:{port}")
        _open_browser(port)
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)