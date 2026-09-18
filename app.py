import os
import sys
import uuid
import json
import hmac
import hashlib
import shutil
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

BASE_DIR   = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
ASSETS_DIR = BASE_DIR / "assets"
USERS_FILE = BASE_DIR / "users.json"

for d in (UPLOAD_DIR, OUTPUT_DIR, ASSETS_DIR):
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

JOBS = {}
LOCK = threading.Lock()


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
        }

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
        }
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
        song_folder = OUTPUT_DIR / user / stem
        srt_folder = song_folder / "SRT"
        srt_folder.mkdir(parents=True, exist_ok=True)

        out_path = srt_folder / f"{stem}.srt"

        def _p(value, status):
            with LOCK:
                JOBS[job_id]["progress"] = float(value)
                JOBS[job_id]["status"]   = status

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

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"]   = f"Error: {e}"
            JOBS[job_id]["progress"] = 0


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
        }

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
        }

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

        _p(5, "Reading subtitles...")
        srt_text = Path(srt_path).read_text(encoding="utf-8")
        cues = SubtitleEngine._parse_srt_cues(srt_text)
        if not cues:
            raise RuntimeError("No cues found in SRT — nothing to export.")

        stem = Path(input_path).stem
        song_folder = OUTPUT_DIR / user / stem
        lyrics_folder = song_folder / "Lyrics"
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

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"]   = f"Error: {e}"
            JOBS[job_id]["progress"] = 0


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
        }

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

        resolved_end = end_sec
        if resolved_end is None:
            _p(2, "Checking track length...")
            resolved_end = engine.get_audio_duration(input_path)

        stem = Path(input_path).stem
        song_folder = OUTPUT_DIR / user / stem
        tabs_folder = song_folder / "Tabs"
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

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"]   = f"Error: {e}"
            JOBS[job_id]["progress"] = 0


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
        song_folder = OUTPUT_DIR / user / stem
        sub_folder_name = "Lyric_Video" if keep_vocals else "Karaoke"
        video_folder = song_folder / sub_folder_name
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

    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["status"]   = f"Error: {e}"
            JOBS[job_id]["progress"] = 0
    finally:
        if temp_audio_path and os.path.exists(temp_audio_path):
            try:
                os.unlink(temp_audio_path)
            except OSError:
                pass
        if demucs_out_dir:
            shutil.rmtree(demucs_out_dir, ignore_errors=True)
            JOBS[job_id]["progress"] = 0


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
    return jsonify(**job)


# app.js polls /api/job/<id> — alias
@app.route("/api/job/<job_id>")
@login_required
def job_status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Unknown job"), 404

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
    the user's outputs folder ("Song/SRT/Song.srt"), since generated
    SRTs live in per-song subfolders, not flat in outputs/<user>/.
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


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
@login_required
def cancel_job(job_id):
    """Best-effort cancel — marks the job as cancelled."""
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Job not found"), 404
    with LOCK:
        JOBS[job_id]["status"]   = "Cancelled"
        JOBS[job_id]["progress"] = 0
    return jsonify(ok=True)


@app.route("/api/browse/<kind>")
@login_required
def browse_folder(kind):
    """
    Return the contents of the user's output subfolder as JSON, so the
    frontend can render it in the browser (inline modal / new page)
    instead of opening a native OS file explorer on the SERVER.

    IMPORTANT: this route used to call os.startfile()/xdg-open/open,
    which opens a folder window on the machine running the Flask/
    Waitress process — not on the client's browser. On a remote
    production server that just opens (and hangs) a folder on the
    server's own desktop while the client's request sits there
    waiting, which is what caused the browser tab to appear to
    "close". This version never touches the server's GUI.

    NOTE: every "kind" used to map to None, which made `target` always
    resolve to the bare outputs root (outdir) no matter what button was
    clicked — so "Browse SRT Folder" and friends were really all
    browsing the same (usually empty-looking) top level. On top of
    that, SRT/Karaoke/Lyrics/Tabs files don't live in one flat folder —
    they're nested per song as outputs/<user>/<song>/<FolderName>/...
    (see _run_pipeline, _run_video_job, _run_lyrics_job, _run_tab_job).
    So this now searches every song folder for the matching subfolder
    name instead of assuming one fixed path.
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

    if not outdir.exists():
        return jsonify(ok=True, path=folder_name, files=[])

    found = []
    for song_dir in outdir.iterdir():
        if not song_dir.is_dir():
            continue
        target = song_dir / folder_name
        if not target.exists():
            continue
        for p in target.rglob("*"):
            if p.is_file():
                found.append(p)

    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    files = [{
        "name":    p.name,
        "rel":     str(p.relative_to(outdir)),   # for /api/download_output/<rel>
        "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
    } for p in found]

    return jsonify(ok=True, path=folder_name, files=files)


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