"""Collects the helper downloads that get bundled INTO the installer, so users need less internet
at first run.  Run from the project root (build_installer.bat does it).
   runtime\\ffmpeg.exe, runtime\\ffprobe.exe        (from gyan.dev, ~90 MB download)
   prereqs\\vc_redist.x64.exe                       (Microsoft Visual C++ 2015-2022 runtime)
Anything already present is kept. A failed download is only a warning: the launcher downloads the
missing piece on the user's PC at first run instead."""
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FFMPEG_URLS = ("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
               "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip")
VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


def download(url: str, dest: Path):
    print(f"      downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "MusicStudioBuild/1.0"})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    tmp.replace(dest)


def collect_ffmpeg(root: Path = ROOT, fetch=download) -> bool:
    out = root / "runtime"
    if (out / "ffmpeg.exe").exists():
        print("[ffmpeg] already in runtime\\ - keeping it")
        return True
    out.mkdir(exist_ok=True)
    zpath = root / "_ffmpeg.zip"
    for url in FFMPEG_URLS:
        try:
            fetch(url, zpath)
            with zipfile.ZipFile(zpath) as zf:
                for member in zf.namelist():
                    base = member.rsplit("/", 1)[-1].lower()
                    if base in ("ffmpeg.exe", "ffprobe.exe"):
                        with zf.open(member) as src, open(out / base, "wb") as dst:
                            shutil.copyfileobj(src, dst)
            zpath.unlink(missing_ok=True)
            if (out / "ffmpeg.exe").exists():
                print("[ffmpeg] OK -> runtime\\ffmpeg.exe")
                return True
        except Exception as e:                       # noqa
            print(f"[ffmpeg] failed from {url}: {e}")
    zpath.unlink(missing_ok=True)
    print("[ffmpeg] WARNING: not collected - the launcher will download it at first run.")
    return False


def collect_vcredist(root: Path = ROOT, fetch=download) -> bool:
    out = root / "prereqs"
    if (out / "vc_redist.x64.exe").exists():
        print("[vc_redist] already in prereqs\\ - keeping it")
        return True
    out.mkdir(exist_ok=True)
    try:
        fetch(VCREDIST_URL, out / "vc_redist.x64.exe")
        print("[vc_redist] OK -> prereqs\\vc_redist.x64.exe")
        return True
    except Exception as e:                           # noqa
        print(f"[vc_redist] WARNING: {e} - the launcher will download it at first run if needed.")
        return False


if __name__ == "__main__":
    collect_ffmpeg()
    collect_vcredist()
    sys.exit(0)                                       # never fail the build because of a download
