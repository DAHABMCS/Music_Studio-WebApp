"""
Music Studio - launcher + first-run setup  (this is what PyInstaller turns into MusicStudio.exe)

WHY A "THIN" LAUNCHER
  PyTorch / Whisper / Demucs are several GB and differ for GPU vs CPU PCs, so they are NOT
  frozen into the .exe. The .exe is small and does this:
    1. First run (or after an update): tells the user what will be installed, then sets up
         - Python 3.11 (private copy, only if no suitable Python is found)
         - a private virtual environment
         - PyTorch: CUDA build if an NVIDIA GPU + driver is found, otherwise CPU-only
         - the libraries in requirements.txt (+ best-effort requirements-optional.txt)
         - ffmpeg
         - the app files (app.py, templates, static ...) copied to a writable folder
    2. Every run: starts the Flask/waitress server with the private Python and shows a
       small control window (Open / Stop).
  Everything lives in  %LOCALAPPDATA%\\MusicStudio  (no admin rights needed).

  Command line:   MusicStudio.exe --setup     force the setup / repair again
"""
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

# ----------------------------------------------------------------------------- settings
APP_NAME = "Music Studio"
APP_VERSION = "1.0.3"                 # BUMP THIS on every release -> users get re-synced
PORT = 8000                           # app.py serves on 8000 (waitress)

PYTHON_VERSION = "3.11.9"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-amd64.exe"
ACCEPTED_PYTHONS = ((3, 10), (3, 11))          # what we accept if already installed

TORCH_INDEX = "https://download.pytorch.org/whl/{tag}"
# wheel tag -> (torch, torchaudio).  cu128 is needed for RTX 50-series (Blackwell) cards.
TORCH_PINS = {"cu128": ("2.7.1", "2.7.1"), "cu124": ("2.5.1", "2.5.1"),
              "cu121": ("2.5.1", "2.5.1"), "cu118": ("2.5.1", "2.5.1"),
              "cpu": ("2.5.1", "2.5.1")}
# (minimum Windows driver version, wheel tag) - best first
CUDA_BUILDS = ((570.65, "cu128"), (551.61, "cu124"), (531.14, "cu121"), (522.06, "cu118"))

VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

FFMPEG_URLS = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
)
MIN_FREE_GB = 12

# Imported inside the private venv at the end of setup. If one of these fails, setup is NOT marked
# complete (so the app never starts half-installed). Keep this to modules the app needs at startup.
REQUIRED_IMPORTS = ("torch", "numpy", "flask", "waitress", "librosa", "soundfile", "demucs",
                    "whisper", "stable_whisper", "mido", "cryptography")
_VERIFY_CODE = r"""
import importlib, importlib.metadata as md, json, sys
dists, mods = json.loads(sys.argv[1]), json.loads(sys.argv[2])
bad = []
for d in dists:
    try:
        md.version(d)
    except md.PackageNotFoundError:
        bad.append(["dist", d])
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        bad.append(["import", m + " (" + type(e).__name__ + ": " + str(e)[:150] + ")"])
print("VERIFY_JSON " + json.dumps(bad))
"""

HOME = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "MusicStudio"
APP_DIR = HOME / "app"          # app.py + templates + static ... (also uploads/outputs/users.json)
VENV_DIR = HOME / "venv"
PY_DIR = HOME / "python"        # private Python, only if needed
BIN_DIR = HOME / "bin"          # ffmpeg.exe / ffprobe.exe
LOG_DIR = HOME / "logs"
STATE_FILE = HOME / "setup_state.json"
VENV_PY = VENV_DIR / "Scripts" / "python.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Cancelled(Exception):
    pass


class SetupError(Exception):
    pass


# ----------------------------------------------------------------------------- small helpers
def bundle_dir() -> Path:
    """Folder that holds the 'payload' (app files) - PyInstaller's _internal, or this folder."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def payload_dir() -> Path:
    return bundle_dir() / "payload"


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def requirements_hash() -> str:
    h = hashlib.sha256()
    for name in ("requirements.txt", "requirements-optional.txt"):
        f = payload_dir() / name
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def clean_env(extra_path=()) -> dict:
    """Environment for child Python processes (drops PyInstaller's variables)."""
    env = os.environ.copy()
    for k in list(env):
        if k in ("PYTHONHOME", "PYTHONPATH", "_MEIPASS2") or k.startswith("_PYI"):
            env.pop(k, None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["KMP_DUPLICATE_LIB_OK"] = "TRUE"            # avoids the Windows torch/numpy OpenMP clash
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    if extra_path:
        env["PATH"] = os.pathsep.join(str(p) for p in extra_path) + os.pathsep + env.get("PATH", "")
    return env


def app_env(device="cpu") -> dict:
    """Environment for app.py: ffmpeg on PATH, GPU/CPU choice, model caches outside the install."""
    env = clean_env(extra_path=(BIN_DIR, VENV_DIR / "Scripts"))
    cache = Path.home() / ".music_studio_cache"
    for sub, var in (("hf", "HF_HOME"), ("torch", "TORCH_HOME"), ("xdg", "XDG_CACHE_HOME"),
                     ("mpl", "MPLCONFIGDIR")):
        env.setdefault(var, str(cache / sub))
    env["MUSIC_STUDIO_MANAGED"] = "1"                 # tells app.py its old bootstrap is not needed
    if getattr(sys, "frozen", False):                 # where Setup put MusicStudio.exe, start_ace_step.bat ...
        env["MUSIC_STUDIO_INSTALL_DIR"] = str(Path(sys.executable).resolve().parent)
    env["MUSIC_STUDIO_DEVICE"] = "cuda" if device == "cuda" else "cpu"
    if device != "cuda":
        env["CUDA_VISIBLE_DEVICES"] = ""
    return env


def free_gb(path: Path) -> float:
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free / 1024 ** 3


def port_open(port=PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


# ----------------------------------------------------------------------------- GPU detection
def _nvidia_smi_candidates():
    c = [shutil.which("nvidia-smi")]
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    c.append(os.path.join(windir, "System32", "nvidia-smi.exe"))
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    c.append(os.path.join(pf, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"))
    return [x for x in c if x and os.path.exists(x)]


def parse_nvidia_smi(text: str):
    """'NVIDIA GeForce RTX 3060, 551.86' -> ('NVIDIA GeForce RTX 3060', 551.86) or None."""
    for line in (text or "").splitlines():
        if "," not in line:
            continue
        name, _, drv = line.rpartition(",")
        try:
            return name.strip(), float(drv.strip())
        except ValueError:
            continue
    return None


def detect_nvidia():
    """Returns (gpu_name, driver_version) or None when there is no usable NVIDIA GPU."""
    for exe in _nvidia_smi_candidates():
        try:
            out = subprocess.run([exe, "--query-gpu=name,driver_version", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=20,
                                 creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
            if out.returncode == 0:
                got = parse_nvidia_smi(out.stdout)
                if got:
                    return got
        except Exception:
            pass
    return None


def pick_torch_tag(driver):
    """Newest CUDA wheel the installed driver can run, else 'cpu'."""
    if driver:
        for min_drv, tag in CUDA_BUILDS:
            if driver >= min_drv:
                return tag
    return "cpu"


# ----------------------------------------------------------------------------- setup engine
class Setup:
    """Runs all setup steps. `ui` needs: log(str) status(str) progress(done,total) cancelled()."""

    def __init__(self, ui):
        self.ui = ui
        self.proc = None
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._logf = open(LOG_DIR / "setup.log", "a", encoding="utf-8", errors="replace")
        self.log(f"===== setup {time.strftime('%Y-%m-%d %H:%M:%S')}  v{APP_VERSION} =====")

    # -- logging / process plumbing
    def log(self, msg):
        self._logf.write(msg + "\n")
        self._logf.flush()
        self.ui.log(msg)

    def check_cancel(self):
        if self.ui.cancelled():
            raise Cancelled()

    def run(self, cmd, env=None, cwd=None, status=None):
        cmd = [str(c) for c in cmd]
        self.log("$ " + " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                     errors="replace", creationflags=NO_WINDOW,
                                     env=env or clean_env(), cwd=cwd)
        try:
            for line in self.proc.stdout:
                line = line.rstrip()
                if line:
                    self.log(line)
                    if status and line.split(" ", 1)[0] in ("Collecting", "Downloading", "Installing",
                                                            "Successfully", "Building"):
                        self.ui.status(f"{status}  -  {line[:70]}")
                self.check_cancel()
            self.proc.wait()
            return self.proc.returncode
        except Cancelled:
            self.kill()
            raise
        finally:
            self.proc = None

    def kill(self):
        p = self.proc
        if p and p.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], creationflags=NO_WINDOW,
                           capture_output=True)

    def pip(self, args, status):
        base = [VENV_PY, "-m", "pip", "install", "--no-input", "--progress-bar", "off",
                "--retries", "10", "--timeout", "60"]
        return self.run(base + list(args), env=clean_env(), status=status)

    def download(self, url, dest: Path, label):
        self.log(f"download {url}")
        self.ui.status(label)
        req = urllib.request.Request(url, headers={"User-Agent": "MusicStudioSetup/1.0"})
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                self.check_cancel()
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                self.ui.progress(done, total)
        tmp.replace(dest)
        self.ui.progress(0, 0)

    # -- steps
    def find_python(self):
        """A usable Python 3.10 / 3.11 (our private copy first), or None."""
        cands = [[str(PY_DIR / "python.exe")], ["py", "-3.11"], ["py", "-3.10"], ["python"]]
        code = "import sys,venv;print(sys.version_info[0],sys.version_info[1])"
        for c in cands:
            if c[0].endswith("python.exe") and not Path(c[0]).exists():
                continue
            try:
                out = subprocess.run(c + ["-c", code], capture_output=True, text=True, timeout=30,
                                     creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
                                     env=clean_env())
                if out.returncode == 0:
                    maj, mnr = (int(x) for x in out.stdout.split()[:2])
                    if (maj, mnr) in ACCEPTED_PYTHONS:
                        return c
            except Exception:
                continue
        return None

    def ensure_vcredist(self):
        """PyTorch needs the Visual C++ 2015-2022 runtime. Windows asks for admin permission."""
        self.ui.status("Step 1/8: checking the Visual C++ runtime ...")
        sysdir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        if (sysdir / "vcruntime140_1.dll").exists() and (sysdir / "msvcp140.dll").exists():
            self.log("VC++ runtime present")
            return
        exe = payload_dir() / "prereqs" / "vc_redist.x64.exe"        # bundled by build_installer.bat
        if not exe.exists():
            HOME.mkdir(parents=True, exist_ok=True)
            exe = HOME / "vc_redist.x64.exe"
            self.download(VCREDIST_URL, exe, "Step 1/8: downloading the Visual C++ runtime ...")
        self.ui.status("Step 1/8: installing the Visual C++ runtime (allow the Windows prompt) ...")
        rc = self.run([exe, "/install", "/quiet", "/norestart"])
        if rc in (0, 1638, 3010):                                   # ok / newer present / reboot advised
            self.log(f"VC++ runtime ready (code {rc})")
            return
        raise SetupError(f"The Visual C++ runtime could not be installed (code {rc}). If you cancelled "
                         "the Windows permission prompt, start Music Studio again and click Yes.")

    def ensure_python(self):
        self.ui.status("Step 2/8: looking for Python ...")
        py = self.find_python()
        if py:
            self.log(f"Using Python: {' '.join(py)}")
            return py
        self.ui.status(f"Step 2/8: downloading Python {PYTHON_VERSION} ...")
        HOME.mkdir(parents=True, exist_ok=True)
        inst = HOME / "python-installer.exe"
        self.download(PYTHON_URL, inst, f"Step 2/8: downloading Python {PYTHON_VERSION} ...")
        self.ui.status("Step 2/8: installing Python (private copy, no admin needed) ...")
        rc = self.run([inst, "/quiet", "InstallAllUsers=0", f"TargetDir={PY_DIR}", "Include_launcher=0",
                       "Include_test=0", "Include_doc=0", "Shortcuts=0", "PrependPath=0",
                       "AssociateFiles=0", "Include_pip=1"])
        inst.unlink(missing_ok=True)
        if rc != 0 or not (PY_DIR / "python.exe").exists():
            raise SetupError(f"Python installer failed (exit code {rc}).")
        return [str(PY_DIR / "python.exe")]

    def ensure_venv(self, py):
        self.ui.status("Step 3/8: creating the private environment ...")
        if not VENV_PY.exists():
            rc = self.run(py + ["-m", "venv", VENV_DIR])
            if rc != 0 or not VENV_PY.exists():
                raise SetupError("Could not create the virtual environment.")
        rc = self.run([VENV_PY, "-m", "pip", "install", "--no-input", "--upgrade", "pip",
                       "setuptools", "wheel"], status="Step 3/8: updating pip")
        if rc != 0:
            raise SetupError("Could not update pip (no internet?).")

    def torch_state(self):
        """('cuda' | 'cpu' | None, version) of the torch inside the venv."""
        try:
            out = subprocess.run([str(VENV_PY), "-c",
                                  "import torch;print(torch.__version__, torch.cuda.is_available())"],
                                 capture_output=True, text=True, timeout=180, creationflags=NO_WINDOW,
                                 stdin=subprocess.DEVNULL, env=clean_env())
            if out.returncode == 0:
                ver, cuda = out.stdout.split()[:2]
                return ("cuda" if cuda == "True" else "cpu"), ver
        except Exception:
            pass
        return None, ""

    def install_torch(self, tag):
        self.ui.status(f"Step 4/8: installing PyTorch ({'GPU ' + tag if tag != 'cpu' else 'CPU only'}) "
                       "- this is the big download ...")
        tv, av = TORCH_PINS[tag]
        args = [f"torch=={tv}", f"torchaudio=={av}", "--index-url", TORCH_INDEX.format(tag=tag)]
        if self.torch_state()[0] is not None:
            # torch is already there (e.g. the CPU build, now switching to CUDA or back).
            # "==2.5.1" also matches "2.5.1+cu124", so pip would skip it without these flags.
            args = ["--force-reinstall", "--no-deps"] + args
        return self.pip(args, f"Step 4/8: PyTorch ({tag})") == 0

    def ensure_torch(self, gpu):
        want = pick_torch_tag(gpu[1] if gpu else None)
        have, ver = self.torch_state()
        prev = load_state().get("torch_build")
        if have and ((want == "cpu") == (have == "cpu")) and prev in (None, want):
            self.log(f"torch {ver} ({have}) already installed")
            return "cuda" if have == "cuda" else "cpu", want
        used = want
        if want != "cpu":
            ok = self.install_torch(want)
            if not ok:
                self.log("GPU build failed - falling back to CPU-only PyTorch")
                used = "cpu"
            else:
                have, _ = self.torch_state()
                if have != "cuda":
                    self.log("PyTorch installed but CUDA is not usable - switching to CPU-only")
                    used = "cpu"
        if used == "cpu":
            if not self.install_torch("cpu"):
                raise SetupError("Could not install PyTorch (no internet?).")
        have, _ = self.torch_state()
        return (have or "cpu"), used

    def install_requirements(self):
        self.ui.status("Step 5/8: installing libraries ...")
        req = payload_dir() / "requirements.txt"
        if req.exists():
            if self.pip(["-r", req], "Step 5/8: libraries") != 0:
                raise SetupError("Installing the required libraries failed - see the log.")
        opt = payload_dir() / "requirements-optional.txt"
        if opt.exists():
            for line in opt.read_text(encoding="utf-8").splitlines():
                pkg = line.split("#")[0].strip()
                if not pkg:
                    continue
                self.ui.status(f"Step 6/8: optional component {pkg} ...")
                if self.pip([pkg], f"Step 6/8: {pkg}") != 0:
                    self.log(f"WARNING: optional package '{pkg}' could not be installed - skipped.")

    def ensure_ffmpeg(self):
        self.ui.status("Step 7/8: checking ffmpeg ...")
        if (BIN_DIR / "ffmpeg.exe").exists() or shutil.which("ffmpeg"):
            return
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        shipped = payload_dir() / "runtime"                 # bundled by build_installer.bat
        if (shipped / "ffmpeg.exe").exists():
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                if (shipped / name).exists():
                    shutil.copy2(shipped / name, BIN_DIR / name)
            self.log("ffmpeg installed from the bundled copy")
            return
        last = None
        for url in FFMPEG_URLS:
            try:
                z = HOME / "ffmpeg.zip"
                self.download(url, z, "Step 7/8: downloading ffmpeg ...")
                with zipfile.ZipFile(z) as zf:
                    for member in zf.namelist():
                        base = member.rsplit("/", 1)[-1].lower()
                        if base in ("ffmpeg.exe", "ffprobe.exe"):
                            with zf.open(member) as src, open(BIN_DIR / base, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                z.unlink(missing_ok=True)
                if (BIN_DIR / "ffmpeg.exe").exists():
                    return
            except Cancelled:
                raise
            except Exception as e:
                last = e
                self.log(f"ffmpeg download failed from {url}: {e}")
        raise SetupError(f"Could not download ffmpeg ({last}).")

    def _required_dists(self):
        """Package names listed in the shipped requirements.txt (version specifiers stripped)."""
        req = payload_dir() / "requirements.txt"
        names = []
        if req.exists():
            for line in req.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._\-]*)", line.split("#")[0])
                if m:
                    names.append(m.group(1))
        return names

    def _verify_once(self):
        """Ask the private venv which required packages are missing / fail to import."""
        cmd = [str(VENV_PY), "-c", _VERIFY_CODE,
               json.dumps(self._required_dists()), json.dumps(list(REQUIRED_IMPORTS))]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               creationflags=NO_WINDOW, env=clean_env(), cwd=str(APP_DIR), timeout=900)
        except Exception as e:                          # noqa
            return [["import", f"verification could not run: {e}"]]
        for line in (r.stdout or "").splitlines():
            if line.startswith("VERIFY_JSON "):
                return json.loads(line[len("VERIFY_JSON "):])
        tail = ((r.stderr or "") + (r.stdout or ""))[-400:].strip()
        return [["import", "verification script failed: " + tail]]

    def verify_install(self):
        """Do not trust pip's exit code alone: prove the libraries are really there, repair once
        if something is missing, and refuse to finish setup if it still is."""
        self.ui.status("Step 8/8: verifying the installation ...")
        bad = self._verify_once()
        if bad:
            self.log(f"VERIFY: problems found: {bad}")
            dists = [name for kind, name in bad if kind == "dist"]
            for d in dists:
                self.check_cancel()
                self.log(f"VERIFY: trying to repair {d}")
                self.pip([d], f"Repairing {d}")
            if dists:
                bad = self._verify_once()
        if bad:
            self.log(f"VERIFY FAILED: {bad}")
            raise SetupError("These required components are missing or broken after installation:\n  "
                             + "\n  ".join(name for _, name in bad))
        self.log("VERIFY OK: all required libraries are installed and import correctly.")

    def sync_payload(self):
        self.ui.status("Step 8/8: installing the application files ...")
        src = payload_dir()
        if not src.is_dir():
            raise SetupError("The installer is damaged: app files are missing.")
        APP_DIR.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.name in ("runtime", "prereqs"):         # installer helpers (ffmpeg, VC++), not app files
                continue
            dst = APP_DIR / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)
        # user data (users.json, uploads, outputs, jobs, lyrics_ai.env) is never in the payload,
        # so it is never overwritten here.

    # -- the whole thing
    def run_all(self, gpu):
        if free_gb(HOME) < MIN_FREE_GB:
            raise SetupError(f"Not enough free disk space: need about {MIN_FREE_GB} GB, "
                             f"have {free_gb(HOME):.1f} GB.")
        self.ensure_vcredist()
        self.check_cancel()
        py = self.ensure_python()
        self.check_cancel()
        self.ensure_venv(py)
        self.check_cancel()
        device, tag = self.ensure_torch(gpu)
        self.check_cancel()
        self.install_requirements()
        self.check_cancel()
        self.ensure_ffmpeg()
        self.check_cancel()
        self.sync_payload()
        self.check_cancel()
        self.verify_install()
        save_state({"complete": True, "version": APP_VERSION, "requirements": requirements_hash(),
                    "device": device, "torch_build": tag,
                    "gpu": (gpu[0] if gpu else None), "installed": time.strftime("%Y-%m-%d %H:%M:%S")})
        self.log(f"SETUP COMPLETE  device={device} build={tag}")
        return device


# ----------------------------------------------------------------------------- GUI
def describe_hardware(gpu):
    tag = pick_torch_tag(gpu[1] if gpu else None)
    if gpu and tag != "cpu":
        return (f"NVIDIA GPU detected: {gpu[0]} (driver {gpu[1]}).\n"
                f"-> The GPU (CUDA) version of PyTorch will be installed (much faster).", True)
    if gpu:
        return (f"NVIDIA GPU found ({gpu[0]}) but its driver ({gpu[1]}) is too old for GPU mode.\n"
                "-> The CPU-only version will be installed. Update the NVIDIA driver and run "
                "'Repair setup' later to use the GPU.", False)
    return ("No NVIDIA GPU detected.\n-> The CPU-only version of PyTorch will be installed "
            "(slower, but everything works).", False)


def run_gui():
    import tkinter as tk
    from tkinter import messagebox, ttk

    HOME.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("640x420")
    root.resizable(False, False)

    force = "--setup" in sys.argv or "--repair" in sys.argv
    state = load_state()
    ready = (VENV_PY.exists() and (APP_DIR / "app.py").exists() and state.get("complete")
             and state.get("version") == APP_VERSION and state.get("requirements") == requirements_hash())

    if ready and not force:
        if start_server_ui(root):
            root.mainloop()
        return

    # ---- tell the user what is going to happen
    first_time = not state.get("complete")
    gpu = detect_nvidia()
    hw_text, hw_gpu = describe_hardware(gpu)
    root.withdraw()
    intro = ("Welcome to Music Studio!\n\n"
             "Before the first start, Music Studio has to set up its components. "
             "This happens only once.\n\n"
             if first_time else
             "Music Studio needs to update or repair its components.\n\n")
    msg = (intro + hw_text + "\n\n"
           "What will be installed:\n"
           "  - Microsoft Visual C++ runtime (only if missing - Windows may ask for permission)\n"
           "  - Python 3.11 (only if none is found)\n"
           "  - PyTorch, Whisper, Demucs and the other libraries\n"
           "  - ffmpeg\n\n"
           f"An internet connection is required. Download: about {'5-8' if hw_gpu else '2-3'} GB, "
           f"disk space: about {MIN_FREE_GB} GB, time: 10-30 minutes.\n\n"
           "Continue?")
    if not messagebox.askokcancel(APP_NAME + " - first-time setup" if first_time else APP_NAME, msg):
        root.destroy()
        return
    root.deiconify()

    # ---- progress window
    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    title = ttk.Label(frm, text="Setting up Music Studio ...", font=("Segoe UI", 13, "bold"))
    title.pack(anchor="w")
    status = ttk.Label(frm, text="Starting ...", wraplength=610)
    status.pack(anchor="w", pady=(6, 4))
    bar = ttk.Progressbar(frm, mode="indeterminate", length=610)
    bar.pack(fill="x")
    bar.start(12)
    box = tk.Text(frm, height=14, wrap="none", font=("Consolas", 8), state="disabled")
    box.pack(fill="both", expand=True, pady=(8, 6))
    cancel_btn = ttk.Button(frm, text="Cancel")
    cancel_btn.pack(anchor="e")

    q = queue.Queue()
    cancel_flag = threading.Event()

    class UI:                                   # called from the worker thread -> only queue
        def log(self, m): q.put(("log", m))
        def status(self, m): q.put(("status", m))
        def progress(self, d, t): q.put(("progress", (d, t)))
        def cancelled(self): return cancel_flag.is_set()

    result = {}
    setup = Setup(UI())

    def work():
        try:
            result["device"] = setup.run_all(gpu)
        except Cancelled:
            result["error"] = "cancelled"
        except Exception as e:                  # noqa
            setup.log(traceback.format_exc())
            result["error"] = str(e) or e.__class__.__name__
        q.put(("done", None))

    def on_cancel():
        cancel_flag.set()
        setup.kill()
        status.config(text="Cancelling ...")
    cancel_btn.config(command=on_cancel)
    root.protocol("WM_DELETE_WINDOW", on_cancel)

    def pump():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    box.config(state="normal")
                    box.insert("end", val + "\n")
                    box.see("end")
                    if int(box.index("end-1c").split(".")[0]) > 600:
                        box.delete("1.0", "200.0")
                    box.config(state="disabled")
                elif kind == "status":
                    status.config(text=val)
                elif kind == "progress":
                    d, t = val
                    if t:
                        bar.stop()
                        bar.config(mode="determinate", maximum=t, value=d)
                        status.config(text=status.cget("text").split("  (")[0]
                                      + f"  ({d / 1048576:.0f} / {t / 1048576:.0f} MB)")
                    else:
                        bar.config(mode="indeterminate")
                        bar.start(12)
                elif kind == "done":
                    return finished()
        except queue.Empty:
            pass
        root.after(100, pump)

    def finished():
        bar.stop()
        if "error" in result:
            if result["error"] != "cancelled":
                messagebox.showerror(APP_NAME, "Setup could not be completed:\n\n" + result["error"] +
                                     f"\n\nDetails: {LOG_DIR / 'setup.log'}\n\n"
                                     "Start Music Studio again to retry.")
            root.destroy()
            return
        dev = "your NVIDIA GPU" if result.get("device") == "cuda" else "the CPU (no usable GPU found)"
        messagebox.showinfo(APP_NAME, f"Setup finished.\nMusic Studio will use {dev}.")
        for w in root.winfo_children():
            w.destroy()
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        start_server_ui(root)

    threading.Thread(target=work, daemon=True).start()
    root.after(100, pump)
    root.mainloop()


def start_server_ui(root):
    """Start app.py with the private Python and show a small control window."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    url = f"http://127.0.0.1:{PORT}"
    if port_open():                              # already running -> just open the browser
        webbrowser.open(url)
        root.destroy()
        return False

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logf = open(LOG_DIR / "server.log", "a", encoding="utf-8", errors="replace")
    logf.write(f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    logf.flush()
    proc = subprocess.Popen([str(VENV_PY), "-u", str(APP_DIR / "app.py")], cwd=str(APP_DIR),
                            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=NO_WINDOW,
                            env=app_env(load_state().get("device", "cpu")))

    def stop():
        if proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], creationflags=NO_WINDOW,
                           capture_output=True)

    root.deiconify()
    root.title(APP_NAME)
    root.geometry("380x190")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=14)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(anchor="w")
    status = ttk.Label(frm, text="Starting ...", wraplength=350)
    status.pack(anchor="w", pady=(4, 10))
    row = ttk.Frame(frm)
    row.pack(fill="x")
    ttk.Button(row, text="Open in browser", command=lambda: webbrowser.open(url)).pack(side="left")
    ttk.Button(row, text="Show log", command=lambda: os.startfile(str(LOG_DIR))).pack(side="left", padx=6)
    ttk.Button(row, text="Stop && exit", command=lambda: (stop(), root.destroy())).pack(side="right")
    ttk.Label(frm, text="Closing this window stops Music Studio.", foreground="#666").pack(
        anchor="w", pady=(12, 0))
    root.protocol("WM_DELETE_WINDOW", lambda: (stop(), root.destroy()))

    t0 = time.time()
    shown = {"ok": False}

    def watch():
        if proc.poll() is not None:
            messagebox.showerror(APP_NAME, "Music Studio stopped unexpectedly.\n\nSee: " +
                                 str(LOG_DIR / "server.log") + "\n(Run 'Repair setup' if this repeats.)")
            root.destroy()
            return
        if not shown["ok"] and port_open():
            shown["ok"] = True
            status.config(text=f"Running at {url}  (the browser opens automatically)")
        elif not shown["ok"] and time.time() - t0 > 120:
            status.config(text="Still starting ... (the first start can take a minute)")
        root.after(1000, watch)

    root.after(1000, watch)
    import atexit
    atexit.register(stop)
    return True


if __name__ == "__main__":
    try:
        run_gui()
    except Exception:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "launcher_crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        try:
            import tkinter
            from tkinter import messagebox
            messagebox.showerror(APP_NAME, "Unexpected error:\n\n" + traceback.format_exc()[-800:])
        except Exception:
            pass