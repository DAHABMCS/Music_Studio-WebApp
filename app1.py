import os
import sys
import uuid
import json
import hmac
import hashlib
import shutil
import threading
from pathlib import Path
from functools import wraps

from flask import (Flask, render_template, request, jsonify,
                   send_from_directory, session, redirect, url_for)
from werkzeug.utils import secure_filename
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

# Create a default user file if none exists
if not USERS_FILE.exists():
    USERS_FILE.write_text(json.dumps({"admin": "admin"}, indent=2))

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
            return redirect(url_for("index"))

        error = "Invalid username or password"

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
# FILE BROWSER
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
    # translate to shape app.js expects
    return jsonify(
        id=job_id,
        status=("done" if job["progress"] >= 100 and "Error" not in job["status"] else
                ("error" if "Error" in job["status"] else "running")),
        progress=job["progress"],
        message=job["status"],
        result={"srt": Path(job["output"]).name if job["output"] else None,
                "preview": job.get("srt", "")[:8000]},
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
    return send_from_directory(outdir, rel, as_attachment=True)


# ============================================================
# NEW ROUTES (added for the Tkinter-style dashboard)
# ============================================================

@app.route("/api/save_srt", methods=["POST"])
@login_required
def save_srt():
    """Save edited SRT content back to the user's outputs folder."""
    data = request.get_json() or {}
    name = data.get("srt")
    content = data.get("content")
    if not name or content is None:
        return jsonify(error="Missing srt name or content"), 400

    _, outdir = user_dirs(session["user"])
    # Prevent path traversal
    safe_name = Path(name).name
    target = (outdir / safe_name).resolve()
    if target.parent != outdir.resolve():
        return jsonify(error="Invalid path"), 400

    target.write_text(content, encoding="utf-8")
    return jsonify(ok=True, file=safe_name)


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
    Open the user's output folder on the machine running the server.
    Works for local installs. On remote servers this will open the
    folder on the server, not the client — that's expected.
    """
    import platform as _platform
    import subprocess as _subprocess

    _, outdir = user_dirs(session["user"])

    subfolders = {
        "srt":           None,
        "karaoke":       None,
        "lyrics":        None,
        "tabs":          None,
        "transcription": None,
    }
    sub = subfolders.get(kind)
    target = str(outdir if sub is None else (outdir / sub))

    try:
        system = _platform.system()
        if system == "Windows":
            os.startfile(target)
        elif system == "Darwin":
            _subprocess.Popen(["open", target])
        else:
            _subprocess.Popen(["xdg-open", target])
    except Exception as e:
        return jsonify(error=f"Could not open folder: {e}"), 500

    return jsonify(ok=True, path=target)


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)