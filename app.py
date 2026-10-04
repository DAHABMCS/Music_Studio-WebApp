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


def _open_native_folder(path):
    """Open `path` in the OS's native file explorer on THIS machine.
    Only ever call this after confirming the request is local — see
    _is_local_request(). Returns True on success."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # noqa: F821 (Windows-only builtin)
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
        args=(job_id, str(input_path), str(srt_path), chord_method),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id)


def _run_lyrics_job(job_id, input_path, srt_path, chord_method):
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
            input_path, cues, stem,
            str(pdf_path), str(mp3_path),
            chord_method=chord_method,
            status_callback=lambda msg: _p(50, msg),
        )

        with LOCK:
            JOBS[job_id]["progress"]   = 100
            JOBS[job_id]["status"]     = "Complete!"
            JOBS[job_id]["output"]     = str(pdf_path)
            JOBS[job_id]["pdf"]        = str(pdf_path)
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
              start_sec, end_sec, use_demucs),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id)


def _run_lyrics_and_tab_job(job_id, input_path, srt_path, chord_method,
                             start_sec, end_sec, use_demucs):
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
            input_path, cues, stem,
            str(lyrics_pdf), str(lyrics_mp3),
            chord_method=chord_method,
            status_callback=lambda msg: _p(25, msg),
        )
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
                title=f"{stem} — Piano Arrangement",
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
                title=f"{stem} — Fingerstyle Guitar TAB (Travis Picking)",
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

    # translate to shape app.js expects
    return jsonify(
        id=job_id,
        status=("done" if job["progress"] >= 100 and "Error" not in job["status"] else
                ("error" if "Error" in job["status"] else "running")),
        progress=job["progress"],
        message=job["status"],
        result={
            "srt": srt_rel,
            "preview": job.get("srt", "")[:8000],
            "video": job["output"] if "video" in job and not job.get("keep_vocals") else None,
            "lyric_video": job["output"] if "video" in job and job.get("keep_vocals") else None,
            "pdf": job.get("pdf"),
            "mp3": job.get("mp3"),
            "tab_pdf": job.get("tab_pdf"),
            "folder": job.get("folder"),
            "files":  job.get("files"),
            "song":   job.get("song"),
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
# Songs that already fit in one chunk are generated in a single request.
ACE_STEP_CHUNK_SECONDS = float(os.environ.get("ACE_STEP_CHUNK_SECONDS", "60") or 60)
ACE_STEP_SEED = int(os.environ.get("ACE_STEP_SEED", "12345") or 12345)
ACE_STEP_BPM = os.environ.get("ACE_STEP_BPM", "").strip()        # e.g. "96" (optional)
ACE_STEP_KEY = os.environ.get("ACE_STEP_KEY", "").strip()        # e.g. "A minor" (optional)
ACE_STEP_CROSSFADE_SECONDS = float(os.environ.get("ACE_STEP_CROSSFADE_SECONDS", "1.0") or 1.0)
ACE_STEP_CHUNK_RETRIES = 1


# ============================================================
# ACE-STEP SERVER CHECK (this app does NOT start ACE-Step)
# ------------------------------------------------------------
# ACE-Step must be started separately on the PC that runs it, e.g. with
# start_ace_step.bat. This app only checks that it is reachable.
#   - /api/ace_status : used by the dashboard banner
#   - _require_ace_running() : called before every song request
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


@app.route("/api/ace_status")
@login_required
def ace_status():
    """Used by the dashboard to show a warning banner if ACE-Step isn't reachable."""
    try:
        _ace_http("GET", "/health", timeout=5)
        return jsonify(running=True, url=ACE_STEP_API_URL)
    except Exception as e:
        return jsonify(running=False, url=ACE_STEP_API_URL, error=str(e),
                       hint=ACE_START_HINT)


@app.route("/api/ace_start", methods=["POST"])
@login_required
def ace_start():
    """Kept so an old dashboard 'Start server' button doesn't 404. This app
    no longer starts ACE-Step itself."""
    return jsonify(error="This app does not start ACE-Step. " + ACE_START_HINT), 400


def _require_ace_running(progress=None, max_wait=900):
    """Called before each song request. Returns when ACE-Step answers.
    If nothing is listening, fail at once with instructions. If the port is
    open but /health isn't answering yet (models still loading), wait."""
    if _ace_is_running():
        return
    if not _ace_port_open():
        raise RuntimeError(
            f"ACE-Step is not running at {ACE_STEP_API_URL}. " + ACE_START_HINT)
    t0, last = time.time(), 0
    while time.time() - t0 < max_wait:
        if _ace_is_running():
            return
        if progress and time.time() - last >= 10:
            last = time.time()
            progress(18, f"Waiting for ACE-Step to finish loading... "
                         f"{int(time.time() - t0)}s")
        time.sleep(2)
    raise RuntimeError(
        f"ACE-Step is listening at {ACE_STEP_API_URL} but not answering after "
        f"{max_wait // 60} minutes. Check its window for errors.")


def generate_song_audio(lyrics, style, instruments, singer, out_path,
                         backend="ace", progress_cb=None, language=""):
    """Generate a song and write it to out_path. `backend` selects which
    engine does the work — routed here from the dashboard's Backend
    dropdown in the Create Song modal:

      "ace"      -> generate_song_audio_ace(...)       lyrics + vocals
      "musicgen" -> generate_song_audio_musicgen(...)  instrumental only
    """
    if backend == "musicgen":
        return generate_song_audio_musicgen(style, instruments, out_path,
                                             progress_cb=progress_cb)
    return generate_song_audio_ace(lyrics, style, instruments, singer, out_path,
                                    progress_cb=progress_cb, language=language)


def _ace_http(method, path, payload=None, timeout=60):
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
    req = urllib.request.Request(ACE_STEP_API_URL + path, data=data,
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


import re as _re


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
        m = tag_re.match(line)
        if m:
            sections.append([m.group(1).strip(), []])
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
        base = _re.sub(r"[\s\d]+$", "", head)          # "verse 2" -> "verse"
        if text:
            if base in known_sung:
                final = tag
            else:                                       # None / instrumental / unknown
                final = cycle[n_sung % 2]
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
    Tune with ACE_STEP_SECONDS_PER_WORD (e.g. 1.2 if lyrics still get cut off,
    0.7 if the song has too much empty music)."""
    forced = os.environ.get("ACE_STEP_SECONDS_PER_WORD", "").strip()
    if forced:
        wps = float(forced)
    else:
        wps = 1.1 if _guess_vocal_language(lyrics_for_model, language) == "ar" else 0.85
    total = 12.0                                   # intro + outro
    for line in lyrics_for_model.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            low = line.lower()
            total += 10 if ("inst" in low or "intro" in low) else 4   # gap between sections
            continue
        total += max(2.5, len(line.split()) * wps + 0.8)
    return float(round(total))


def _auto_duration(lyrics_for_model, language=""):
    """Needed time, kept inside the 30s..360s range the server allows."""
    return float(max(30, min(360, _needed_seconds(lyrics_for_model, language))))


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
                      seed=None, bpm=None, key_scale=None, language=""):
    """Generate ONE piece of audio (a whole short song, or one chunk of a long
    song) and write it to out_path, by calling a running ACE-Step 1.5 REST
    API server (local or remote)."""

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
    duration = ACE_STEP_DURATION_SECONDS or _auto_duration(lyrics_for_model, vocal_language)
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
        "inference_steps": ACE_STEP_INFER_STEPS,
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
    submit = json.loads(_ace_http("POST", "/release_task", payload).decode("utf-8"))
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
            parts.append(f"{prev}[{k}:a]acrossfade=d={crossfade}:c1=tri:c2=tri{lab}")
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


def generate_song_audio_ace(lyrics, style, instruments, singer, out_path, progress_cb=None,
                            language=""):
    """Generate a song with ACE-Step. Long songs are split into section-based
    chunks (each short enough for CPU), generated one by one with the same
    seed/style, then joined with a crossfade. Short songs use one request."""

    def _p(value, status):
        if progress_cb:
            progress_cb(value, status)

    one_kw = dict(seed=ACE_STEP_SEED, bpm=ACE_STEP_BPM or None, key_scale=ACE_STEP_KEY or None,
                  language=language)

    # Instrumental, or the user forced a fixed length: single request.
    if singer == "instrumental" or ACE_STEP_DURATION_SECONDS:
        return _ace_generate_one(lyrics, style, instruments, singer, out_path,
                                 progress_cb=progress_cb, **one_kw)

    chunks = _split_ace_chunks(_clean_lyrics_for_ace(lyrics), ACE_STEP_CHUNK_SECONDS, language)
    if len(chunks) <= 1:
        return _ace_generate_one(lyrics, style, instruments, singer, out_path,
                                 progress_cb=progress_cb, **one_kw)

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
    _join_audio_chunks(paths, out_path, ACE_STEP_CROSSFADE_SECONDS)
    _p(98, "Song joined.")


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


@app.route("/api/create_song", methods=["POST"])
@login_required
def create_song():
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

    backend = data.get("backend") or "ace"
    if backend not in ("ace", "musicgen"):
        return jsonify(error=f"Unknown backend: {backend}"), 400

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
        args=(job_id, title, lyrics, style, instruments, singer, backend, language),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id)


def _run_create_song_job(job_id, title, lyrics, style, instruments, singer, backend="ace",
                         language=""):
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
        (songs_folder / "lyrics.txt").write_text(lyrics, encoding="utf-8")
        (songs_folder / "song_request.json").write_text(json.dumps({
            "title": title,
            "style": style,
            "singer": singer,
            "instruments": instruments,
            "backend": backend,
            "language": language or "auto",
        }, indent=2), encoding="utf-8")

        _p(15, f"Generating song with {backend}...")
        # .wav, not .mp3 — both backends output WAV audio.
        out_path = songs_folder / f"{stem}.wav"
        generate_song_audio(lyrics, style, instruments, singer,
                             str(out_path), backend=backend, progress_cb=_p,
                             language=language)

        with LOCK:
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["status"]   = "Complete!"
            JOBS[job_id]["output"]   = str(out_path)
            JOBS[job_id]["song"]     = str(out_path)
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

PRESET_KEYS = ("title", "style", "singer", "instruments", "backend", "lyrics", "language")
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


@app.route("/api/song_presets", methods=["GET"])
@login_required
def list_song_presets():
    items = []
    for f in sorted(_preset_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        items.append({"name": f.stem, "saved": int(f.stat().st_mtime)})
    return jsonify(presets=items)


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
            else:
                v = str(v)
            clean[k] = v
    if not clean:
        return jsonify(error="Nothing to save"), 400
    blob = json.dumps({"name": stem, "settings": clean}, indent=2, ensure_ascii=False)
    if len(blob.encode("utf-8")) > PRESET_MAX_BYTES:
        return jsonify(error="Setup is too large to save"), 400
    path.write_text(blob, encoding="utf-8")
    return jsonify(ok=True, name=stem)


@app.route("/api/song_presets/<name>", methods=["GET"])
@login_required
def load_song_preset(name):
    stem, path = _preset_path(name)
    if not stem or not path.exists():
        return jsonify(error="Saved setup not found"), 404
    try:
        return jsonify(json.loads(path.read_text(encoding="utf-8")))
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

    # ACE-Step is NOT started by this app. Start it separately on this PC.
    if _ace_is_running():
        print(f"[ACE] ACE-Step is reachable at {ACE_STEP_API_URL}", flush=True)
    else:
        print(f"[ACE] ACE-Step is NOT running at {ACE_STEP_API_URL}. "
              "Start it separately (start_ace_step.bat) before creating songs.",
              flush=True)

    try:
        from waitress import serve
        port = 8000
        print("Starting production server (waitress)")
        print(f" * Running on http://127.0.0.1:{port}")
        print(f" * Running on http://{ip}:{port}")
        _open_browser(port)
        serve(app, host="0.0.0.0", port=port)
    except ImportError:
        port = 5000
        print("waitress not installed — falling back to Flask's dev server.")
        print("Run 'pip install waitress' to use the production server instead.")
        print(f" * Running on http://127.0.0.1:{port}")
        print(f" * Running on http://{ip}:{port}")
        _open_browser(port)
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)