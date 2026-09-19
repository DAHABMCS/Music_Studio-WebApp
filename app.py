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

BASE_DIR   = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
ASSETS_DIR = BASE_DIR / "assets"
USERS_FILE = BASE_DIR / "users.json"
JOBS_DIR   = BASE_DIR / "jobs"

for d in (UPLOAD_DIR, OUTPUT_DIR, ASSETS_DIR, JOBS_DIR):
    d.mkdir(exist_ok=True)

# Create a default user file if none exists.
# NOTE: change this password immediately after first login.
DEFAULT_ADMIN_PASSWORD = "change-me-now"
if not USERS_FILE.exists():
    USERS_FILE.write_text(json.dumps({
        "admin": {
            "password": generate_password_hash(DEFAULT_ADMIN_PASSWORD),
            "role": "admin",
        }
    }, indent=2))

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
# ============================================================
def load_users():
    try:
        return json.loads(USERS_FILE.read_text())
    except Exception:
        return {}


def save_users(users: dict):
    USERS_FILE.write_text(json.dumps(users, indent=2))


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
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "").strip()

        user = load_users().get(u)
        if user is not None and verify_password(p, user):
            session.permanent = True          # survive browser restart
            session["user"] = u
            session["role"] = get_role(user)
            return redirect(url_for("index"))

        error = "Invalid username or password"

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
# MAIN PAGE
# ============================================================
@app.route("/")
@login_required
def index():
    return render_template("dashboard.html", user=session["user"])


# ============================================================
# STATIC ASSETS
# ============================================================
@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(ASSETS_DIR, filename)


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

        _p(92, "Generating chord/rhythm PDF...")
        chord_pdf = out_dir / "02_RHYTHM_CHORDS.pdf"
        music_module.draw_chord_pdf(chords, tempo_bpm, chord_pdf)

        # --- Stage 8: text report --------------------------------
        _p(97, "Writing report...")
        report_file = out_dir / "TRANSCRIPTION_REPORT.txt"
        music_module.create_report(
            report_file, input_path_obj, tempo_bpm, tab_notes, chords,
            total_duration_seconds=total_duration_seconds,
        )

        # --- Done ------------------------------------------------
        with LOCK:
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["status"]   = "Complete!"
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
            "folder": job.get("folder"),
            "files":  job.get("files"),
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
    Show the user's output folder for `kind`.

    Each kind now has ONE top-level folder — outputs/<user>/<Kind>/ —
    with a subfolder per song inside it (see _run_pipeline,
    _run_video_job, _run_lyrics_job, _run_tab_job). That means there's
    a single, well-defined folder to point at: "the folder to view and
    its subfolders" is exactly outputs/<user>/<Kind>/.

    If the request is coming from the same machine that's running this
    server, we open that folder in the real, native OS file explorer
    (Explorer/Finder/whatever). The person clicking the button is
    sitting at this machine, so there's no ambiguity about whose
    window should pop up.

    If the request comes from a different device on the network, there
    is no way for a web page to open a native file-explorer window on
    the REQUESTING device — no browser grants any website that
    capability, for security reasons, regardless of framework. So for
    remote requests we fall back to an in-page listing with per-file
    download links, letting that device pull the files down to itself.
    """
    _, outdir = user_dirs(session["user"])

    folder_name_for_kind = {
        "srt":           "SRT",
        "karaoke":       "Karaoke",
        "lyrics":        "Lyrics",
        "tabs":          "Tabs",
        "transcription": "Transcription",
    }
    folder_name = folder_name_for_kind.get(kind)
    if folder_name is None:
        return jsonify(error=f"Unknown browse kind: {kind}"), 400

    target = outdir / folder_name
    target.mkdir(parents=True, exist_ok=True)

    if _is_local_request() and _open_native_folder(target):
        return jsonify(ok=True, opened=True, path=folder_name)

    found = [p for p in target.rglob("*") if p.is_file()]
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    files = [{
        "name":    p.name,
        "rel":     str(p.relative_to(outdir)),   # for /api/download_output/<rel>
        "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
    } for p in found]

    return jsonify(ok=True, opened=False, path=folder_name, files=files)


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    try:
        from waitress import serve
        print("Starting production server (waitress) on http://0.0.0.0:8000")
        print("Access production server on http://<Server IP>:8000")
        print("Access production server on http://192.168.1.67:8000")
        serve(app, host="0.0.0.0", port=8000)
    except ImportError:
        print("waitress not installed — falling back to Flask's dev server.")
        print("Run 'pip install waitress' to use the production server instead.")
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)