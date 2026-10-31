"""
audio_metering.py
=================
Standalone DSP helpers for the Studio Audio Editor "5-Star" upgrade.

    LUFS (ITU-R BS.1770-4), True Peak (dBTP, 4x oversampled),
    RMS / sample-peak dB meters, phase correlation, mono-sum,
    ffmpeg loudnorm two-pass, declipping (ffmpeg adeclip),
    presence EQ (3 kHz peaking), harmonic exciter.

Dependencies: numpy, scipy, soundfile (all already required by the editor).
Optional:     ffmpeg on PATH (for loudnorm + declip).

Reference:
  ITU-R BS.1770-4  https://www.itu.int/rec/R-REC-BS.1770/en
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np


# =====================================================================
#  Loading
# =====================================================================
def _load_audio(path):
    """Load any audio file as (N, 2) float32 + sample rate. Uses ffmpeg
    fallback for formats soundfile can't read."""
    import soundfile as sf
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception:
        ff = shutil.which("ffmpeg")
        if not ff:
            raise RuntimeError(
                f"soundfile cannot read {path} and ffmpeg is not on PATH."
            )
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        try:
            import os
            os.close(fd)
            subprocess.run(
                [ff, "-y", "-i", str(path), "-vn", "-acodec", "pcm_f32le",
                 "-ar", "44100", "-ac", "2", tmp],
                check=True, capture_output=True,
            )
            data, sr = sf.read(tmp, dtype="float32", always_2d=True)
        finally:
            try:
                import os
                os.remove(tmp)
            except OSError:
                pass
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    return np.ascontiguousarray(data), int(sr)


# =====================================================================
#  Basic level metering
# =====================================================================
def _db(amp):
    return 20.0 * np.log10(np.maximum(np.abs(amp), 1e-9))


def rms_db(data, sr, win_seconds=0.05):
    """Per-channel RMS dB array with a sliding window (for live meters)."""
    hop = max(1, int(sr * win_seconds))
    n = len(data) // hop
    if n < 1:
        return np.array([-120.0]), np.array([-120.0])
    blocks = data[:n * hop].reshape(n, hop, -1)
    rms = np.sqrt((blocks ** 2).mean(axis=1) + 1e-12)
    L = _db(rms[:, 0])
    R = _db(rms[:, 1]) if rms.shape[1] > 1 else L
    return L, R


def peak_db(data):
    """Sample peak per channel in dBFS."""
    if data.size == 0:
        return -120.0, -120.0
    L = float(_db(data[:, 0]).max())
    R = float(_db(data[:, 1]).max()) if data.shape[1] > 1 else L
    return L, R


def true_peak_db(data, sr, oversample=4):
    """True Peak in dBTP via 4x linear-phase oversampling."""
    if data.size == 0:
        return -120.0, -120.0
    try:
        from scipy import signal
        L = signal.resample_poly(data[:, 0], oversample, 1)
        R = signal.resample_poly(data[:, 1], oversample, 1) if data.shape[1] > 1 else L
    except Exception:
        L, R = data[:, 0], (data[:, 1] if data.shape[1] > 1 else data[:, 0])
    return float(_db(L).max()), float(_db(R).max())


# =====================================================================
#  ITU-R BS.1770-4 integrated loudness (LUFS)
# =====================================================================
def _k_weight(data, sr):
    """K-weighting filter (shelving + highpass) per BS.1770-4."""
    try:
        from scipy import signal
    except ImportError:
        return data
    # Stage 1: high-shelf (approx 1681 Hz, +4 dB, Q 0.707)
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = np.tan(np.pi * f0 / sr)
    Vh = 10 ** (G / 20.0)
    Vb = Vh ** 0.4996667741545416
    a0 = 1.0 + K / Q + K * K
    b1 = [(Vh + Vb * K / Q + K * K) / a0,
          2.0 * (K * K - Vh) / a0,
          (Vh - Vb * K / Q + K * K) / a0]
    a1 = [1.0,
          2.0 * (K * K - 1.0) / a0,
          (1.0 - K / Q + K * K) / a0]
    # Stage 2: highpass (38 Hz, Q 0.5)
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = np.tan(np.pi * f0 / sr)
    b2 = [1.0, -2.0, 1.0]
    a2 = [1.0,
          2.0 * (K * K - 1.0) / (1.0 + K / Q + K * K),
          (1.0 - K / Q + K * K) / (1.0 + K / Q + K * K)]
    y = signal.lfilter(b1, a1, data, axis=0)
    y = signal.lfilter(b2, a2, y, axis=0)
    return y


def integrated_lufs(data, sr):
    """Integrated loudness (LUFS) with absolute + relative gating."""
    if data.size == 0:
        return -70.0
    y = _k_weight(data, sr)
    block, hop = int(0.4 * sr), int(0.1 * sr)
    if block < 1 or len(y) < block:
        return -70.0
    n = 1 + (len(y) - block) // hop
    z = np.empty(n)
    for i in range(n):
        seg = y[i * hop:i * hop + block]
        z[i] = np.mean(seg[:, 0] ** 2) + (
            np.mean(seg[:, 1] ** 2) if seg.shape[1] > 1 else 0.0)
    L = -0.691 + 10 * np.log10(z + 1e-12)
    keep = L > -70.0
    if keep.sum() == 0:
        return -70.0
    rel = -0.691 + 10 * np.log10(
        np.mean(10 ** ((L[keep] + 0.691) / 10)) + 1e-12) - 10.0
    keep &= L > max(-70.0, rel)
    if keep.sum() == 0:
        return -70.0
    return float(-0.691 + 10 * np.log10(
        np.mean(10 ** ((L[keep] + 0.691) / 10)) + 1e-12))


def short_term_lufs(data, sr, seconds=3.0):
    """3-second short-term loudness for live UI updates."""
    n = int(seconds * sr)
    return integrated_lufs(data[-n:] if len(data) > n else data, sr)


# =====================================================================
#  Loudness normalization (ffmpeg two-pass, numpy fallback)
# =====================================================================
def normalize_peak(data, target_dbfs=-1.0):
    lp, rp = peak_db(data)
    cur = max(lp, rp)
    gain = 10 ** ((target_dbfs - cur) / 20.0)
    return (data * gain).astype(np.float32), target_dbfs - cur


def normalize_lufs(path_in, path_out, target_lufs=-14.0):
    """Two-pass ffmpeg loudnorm (accurate). Numpy fallback if ffmpeg missing.
    Returns the achieved LUFS."""
    ff = shutil.which("ffmpeg")
    if not ff:
        data, sr = _load_audio(path_in)
        cur = integrated_lufs(data, sr)
        out = np.clip(data * 10 ** ((target_lufs - cur) / 20.0), -1.0, 1.0)
        import soundfile as sf
        sf.write(str(path_out), out, sr)
        return integrated_lufs(out, sr)

    # Pass 1 — measure
    p1 = subprocess.run(
        [ff, "-hide_banner", "-nostats", "-i", str(path_in),
         "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p1.stderr or "", re.S)
    if not m:
        raise RuntimeError("ffmpeg loudnorm measurement failed")
    st = json.loads(m.group(0))

    # Pass 2 — apply
    p2 = subprocess.run(
        [ff, "-y", "-i", str(path_in), "-af",
         (f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
          f"measured_I={st['input_i']}:measured_TP={st['input_tp']}:"
          f"measured_LRA={st['input_lra']}:measured_thresh={st['input_thresh']}:"
          f"offset={st['target_offset']}:linear=true"),
         "-ar", "44100", "-c:a", "pcm_s16le", str(path_out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if p2.returncode != 0:
        raise RuntimeError("ffmpeg loudnorm apply failed: " +
                           (p2.stderr or "")[-400:])
    data, sr = _load_audio(path_out)
    return integrated_lufs(data, sr)


# =====================================================================
#  Declipping / clipping detection
# =====================================================================
def detect_clipping(data, threshold_dbfs=-0.1):
    """Return list of (start_sec, end_sec) regions where |sample| >= threshold."""
    if data.size == 0:
        return []
    thresh = 10 ** (threshold_dbfs / 20.0)
    peak = np.maximum(np.abs(data[:, 0]),
                      np.abs(data[:, 1]) if data.shape[1] > 1 else 0.0)
    mask = peak >= thresh
    if not mask.any():
        return []
    d = np.diff(np.concatenate([[0], mask.astype(int), [0]]))
    idx = np.where(d != 0)[0].reshape(-1, 2)
    return [(int(a), int(b)) for a, b in idx]


def declip_ffmpeg(path_in, path_out):
    """Repair clipped samples using ffmpeg's adeclip filter."""
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg is required for declipping")
    proc = subprocess.run(
        [ff, "-y", "-i", str(path_in), "-af", "adeclip",
         "-ar", "44100", "-c:a", "pcm_s16le", str(path_out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg adeclip failed: " +
                           (proc.stderr or "")[-400:])


# =====================================================================
#  Mono compatibility / phase
# =====================================================================
def phase_correlation(data):
    """-1 = fully out of phase, 0 = uncorrelated, +1 = mono."""
    if data.size == 0 or data.shape[1] < 2:
        return 1.0
    L, R = data[:, 0], data[:, 1]
    denom = np.sqrt(np.sum(L * L) * np.sum(R * R)) + 1e-12
    return float(np.clip(np.sum(L * R) / denom, -1.0, 1.0))


def mono_sum(data):
    """Sum L+R to mono and duplicate back to stereo (safe audition)."""
    m = data.mean(axis=1, keepdims=True)
    return np.repeat(m, 2, axis=1)


# =====================================================================
#  3 kHz presence EQ + harmonic exciter
# =====================================================================
def presence_eq(data, sr, gain_db=3.0, center_hz=3000.0, q=1.0):
    """Peaking EQ centred on the 3 kHz "presence" region."""
    try:
        from scipy import signal
    except ImportError:
        return data
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * center_hz / sr
    alpha = np.sin(w0) / (2 * q)
    b = [1 + alpha * A, -2 * np.cos(w0), 1 - alpha * A]
    a = [1 + alpha / A, -2 * np.cos(w0), 1 - alpha / A]
    return signal.lfilter(b, a, data, axis=0).astype(np.float32)


def harmonic_exciter(data, sr, amount=0.15, cutoff_hz=6000.0):
    """Add gentle upper harmonics via tanh-saturated highpass."""
    try:
        from scipy import signal
    except ImportError:
        return data
    sos = signal.butter(4, cutoff_hz / (sr / 2.0), btype="high", output="sos")
    high = signal.sosfilt(sos, data, axis=0)
    return (data + np.tanh(high * 3.0) * amount).astype(np.float32)