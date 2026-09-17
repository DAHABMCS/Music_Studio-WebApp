import threading
import uuid
import time
from pathlib import Path
from datetime import datetime

JOBS = {}   # job_id -> dict
LOCK = threading.Lock()

def create_job(user: str, kind: str) -> str:
    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "user": user,
            "kind": kind,
            "status": "queued",
            "progress": 0,
            "message": "Queued",
            "result": None,
            "error": None,
            "created": datetime.utcnow().isoformat(),
            "started": None,
            "finished": None,
        }
    return job_id

def update_job(job_id, *, progress=None, message=None, status=None):
    with LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        if progress is not None: j["progress"] = progress
        if message is not None:  j["message"] = message
        if status is not None:   j["status"] = status

def finish_job(job_id, result=None, error=None):
    with LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        j["status"] = "error" if error else "done"
        j["result"] = result
        j["error"] = str(error) if error else None
        j["finished"] = datetime.utcnow().isoformat()
        if not error:
            j["progress"] = 100

def get_job(job_id):
    return JOBS.get(job_id)

def run_in_thread(target, *args, **kwargs):
    t = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t