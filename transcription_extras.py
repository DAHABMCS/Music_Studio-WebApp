"""
transcription_extras.py
Per-instrument MIDI + PDF export and vocal sheet music with lyrics.

Used by app.py::_run_full_transcription_job.

Requires:
    demucs, librosa, pretty_midi, numpy, reportlab, music21
Strictly requires (for real staff-notation PDFs):
    MuseScore 4 on PATH, in the Windows registry, or in a default install folder.
"""
from __future__ import annotations

import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------
STEM_NAMES_6 = ("drums", "bass", "other", "vocals", "guitar", "piano")
STEM_LABELS = {
    "drums":  "Drums",
    "bass":   "Bass",
    "other":  "Other",
    "vocals": "Vocals",
    "guitar": "Guitar",
    "piano":  "Piano",
}

# Drum MIDI note numbers (General MIDI drum map)
GM_KICK = 36
GM_SNARE = 38
GM_HIHAT = 42

# General MIDI programs — MuseScore uses these to decide notation (TAB vs staff)
GM_PROGRAM_BASS = 33       # Electric Bass (finger)
GM_PROGRAM_GUITAR = 25     # Acoustic Guitar (steel)
GM_PROGRAM_VOCAL = 54      # Synth Voice
GM_PROGRAM_PIANO = 0       # Acoustic Grand Piano
GM_PROGRAM_DRUMS = 0       # Drum kit (is_drum=True overrides)

DEMUCS_MODEL = os.environ.get("ACE_STEM_MODEL", "htdemucs_6s").strip() or "htdemucs_6s"


# ==================================================================
# 1. Stem separation
# ==================================================================
def separate_all_stems(wav_path, out_dir, log=print):
    """Run Demucs stem separation. Returns dict: stem_name -> absolute wav path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="demucs_stems_"))
    try:
        cmd = [sys.executable, "-m", "demucs", "-n", DEMUCS_MODEL,
               "-o", str(tmp), str(wav_path)]
        log(f"[stems] running: {' '.join(cmd)}")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        except FileNotFoundError as e:
            log(f"[stems] demucs not installed: {e}")
            return {}
        if proc.returncode != 0:
            log(f"[stems] demucs failed (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '')[-800:]}")
            return {}

        wanted = STEM_NAMES_6 if "6s" in DEMUCS_MODEL else ("drums", "bass", "vocals", "other")
        result = {}
        for stem in wanted:
            matches = list(tmp.rglob(f"{stem}.wav"))
            if not matches:
                continue
            dest = out_dir / f"stem_{stem}.wav"
            shutil.copyfile(matches[0], dest)
            result[stem] = str(dest)
            log(f"[stems] saved {stem} -> {dest.name}")
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==================================================================
# 2. Per-instrument transcription to MIDI
# ==================================================================
def transcribe_stem_to_midi(wav_path, out_midi, instrument, tempo_bpm, log=print):
    """Transcribe a single-instrument stem to a MIDI file.

    MuseScore picks notation (TAB vs staff) from the instrument name and
    General MIDI program we set here.
    """
    try:
        import librosa
        import pretty_midi
    except ImportError as e:
        raise RuntimeError(
            f"Per-instrument transcription needs librosa and pretty_midi "
            f"(pip install librosa pretty_midi): {e}"
        )

    y, sr = librosa.load(str(wav_path), sr=22050, mono=True)
    duration = float(len(y) / sr) if sr else 0.0
    midi = pretty_midi.PrettyMIDI(initial_tempo=float(tempo_bpm or 120.0))

    if instrument == "drums":
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
        onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr,
                                            hop_length=512, backtrack=True)
        times = librosa.frames_to_time(onsets, sr=sr, hop_length=512)
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
        stft = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))

        # Set the name explicitly to "Drums" — MuseScore needs this for percussion staff
        drum = pretty_midi.Instrument(program=GM_PROGRAM_DRUMS, is_drum=True, name="Drums")
        for t, frame in zip(times, onsets):
            spec = stft[:, max(0, frame - 2):frame + 3].mean(axis=1)
            if spec.size == 0:
                continue
            lo = spec[(freqs >= 20) & (freqs < 150)].sum()
            mid = spec[(freqs >= 150) & (freqs < 3000)].sum()
            hi = spec[(freqs >= 3000) & (freqs < 12000)].sum()
            note = GM_KICK if lo >= max(mid, hi) else (GM_SNARE if mid >= hi else GM_HIHAT)
            drum.notes.append(pretty_midi.Note(
                velocity=100, pitch=note,
                start=float(t), end=float(t) + 0.05))
        midi.instruments.append(drum)
        midi.write(str(out_midi))
        return duration

    if instrument in ("bass", "guitar", "vocals"):
        f0, voiced, _ = librosa.pyin(
            y, fmin=librosa.note_to_hz("C1"),
            fmax=librosa.note_to_hz("C7"), sr=sr)
        times = librosa.times_like(f0, sr=sr)

        # MuseScore decides on TAB vs staff from the name and program
        if instrument == "bass":
            inst = pretty_midi.Instrument(program=GM_PROGRAM_BASS, name="Electric Bass")
        elif instrument == "guitar":
            inst = pretty_midi.Instrument(program=GM_PROGRAM_GUITAR, name="Guitar")
        else:
            inst = pretty_midi.Instrument(program=GM_PROGRAM_VOCAL, name="Voice")

        notes = []
        cur = None
        for t, f, v in zip(times, f0, voiced):
            if v and f is not None and not np.isnan(f):
                pitch = int(round(librosa.hz_to_midi(float(f))))
                if cur is not None and cur.pitch == pitch and float(t) - cur.end < 0.15:
                    cur.end = float(t) + 0.05
                else:
                    cur = pretty_midi.Note(velocity=90, pitch=pitch,
                                           start=float(t), end=float(t) + 0.05)
                    notes.append(cur)
        inst.notes = notes
        midi.instruments.append(inst)
        midi.write(str(out_midi))
        return duration

    # piano / other : polyphonic CQT peak picking
    cqt = np.abs(librosa.cqt(y, sr=sr, fmin=librosa.note_to_hz("C1"),
                             n_bins=84, bins_per_octave=12, hop_length=512))
    times = librosa.times_like(cqt[0], sr=sr, hop_length=512)
    threshold = cqt.max() * 0.15 if cqt.size else 0.0
    inst = pretty_midi.Instrument(
        program=GM_PROGRAM_PIANO if instrument == "piano" else 0,
        name=STEM_LABELS.get(instrument, instrument))
    active = {}
    for i, t in enumerate(times):
        col = cqt[:, i]
        peaks = set(np.where(col > threshold)[0].tolist())
        for p in peaks:
            if p in active:
                active[p].end = float(t) + 0.05
            else:
                n = pretty_midi.Note(
                    velocity=int(min(127, max(1, col[p] * 127))),
                    pitch=int(24 + p),
                    start=float(t), end=float(t) + 0.05)
                active[p] = n
                inst.notes.append(n)
        for p in list(active.keys()):
            if p not in peaks and float(t) - active[p].end > 0.1:
                del active[p]
    midi.instruments.append(inst)
    midi.write(str(out_midi))
    return duration


# ==================================================================
# 3. MuseScore discovery + render (no text fallback — real notation only)
# ==================================================================
def _find_musescore():
    """Find MuseScore 4 (or 3) on the user's machine."""
    # 1. On PATH
    for name in ("musescore", "mscore", "MuseScore4", "MuseScore3"):
        p = shutil.which(name)
        if p:
            return p

    # 2. Windows registry (most reliable for MuseScore 4)
    if sys.platform.startswith("win"):
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for sub in (
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MuseScore4",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\MuseScore4",
                ):
                    try:
                        with winreg.OpenKey(hive, sub) as k:
                            install, _ = winreg.QueryValueEx(k, "InstallLocation")
                            exe = Path(install) / "bin" / "MuseScore4.exe"
                            if exe.is_file():
                                return str(exe)
                    except OSError:
                        pass
        except ImportError:
            pass

        # 3. Default install folders
        for root in (os.environ.get("ProgramFiles"),
                     os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA")):
            if not root:
                continue
            for s in ("MuseScore 4/bin/MuseScore4.exe",
                      "MuseScore 3/bin/MuseScore3.exe"):
                cand = Path(root) / s
                if cand.is_file():
                    return str(cand)

    # 4. macOS
    if sys.platform == "darwin":
        for app in ("/Applications/MuseScore 4.app/Contents/MacOS/mscore",
                    "/Applications/MuseScore 3.app/Contents/MacOS/mscore"):
            if Path(app).is_file():
                return app
    return None


def render_pdf_from_midi(midi_or_xml_path, out_pdf, title="", log=print):
    """Render MIDI (or MusicXML) to PDF using MuseScore.
    Raises RuntimeError if MuseScore is missing or fails."""
    ms = _find_musescore()
    if not ms:
        raise RuntimeError(
            "MuseScore is not installed or could not be found. "
            "Please install MuseScore 4 from https://musescore.org/download "
            "and restart Music Studio. MuseScore is required to draw real "
            "sheet music (notes, TAB, staff) from the transcription."
        )

    cmd = [ms, "-o", str(out_pdf), str(midi_or_xml_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode == 0 and Path(out_pdf).is_file() and Path(out_pdf).stat().st_size > 1000:
            return True
        log(f"[pdf] MuseScore failed for {Path(midi_or_xml_path).name}: "
            f"{(proc.stderr or '')[-400:]}")
        raise RuntimeError(
            f"MuseScore could not render {Path(out_pdf).name}. "
            f"Error: {(proc.stderr or '')[-300:]}"
        )
    except subprocess.SubprocessError as e:
        raise RuntimeError(f"Could not run MuseScore: {e}")


# ==================================================================
# 4. Vocal score: MusicXML with lyrics (staff + words underneath)
# ==================================================================
def _parse_srt_blocks(text):
    import re
    blocks = []
    for block in re.split(r"\r?\n\s*\r?\n", (text or "").strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        for i, l in enumerate(lines):
            if "-->" in l:
                try:
                    a, b = l.split("-->")
                    def _t(s):
                        h, m, rest = s.strip().replace(",", ".").split(":")
                        return int(h) * 3600 + int(m) * 60 + float(rest)
                    txt = " ".join(x.strip() for x in lines[i + 1:])
                    if txt:
                        blocks.append((_t(a), _t(b), txt))
                except Exception:
                    pass
                break
    return blocks


def write_vocal_musicxml(srt_path, vocal_midi_path, out_xml, title="", log=print):
    """Build a MusicXML vocal score with each sung word attached to the note
    that falls inside its SRT cue. MuseScore opens this and prints lyrics
    directly under the staff, exactly like the reference images."""
    try:
        import pretty_midi
        from music21 import stream, note, metadata, layout
    except ImportError as e:
        log(f"[vocal_xml] music21 / pretty_midi missing: {e}")
        return False

    cues = _parse_srt_blocks(Path(srt_path).read_text(encoding="utf-8"))
    if not cues:
        log("[vocal_xml] no cues in SRT")
        return False

    pm = pretty_midi.PrettyMIDI(str(vocal_midi_path))
    vocal_inst = None
    for inst in pm.instruments:
        if inst.name == "Voice":
            vocal_inst = inst
            break
    if vocal_inst is None and pm.instruments:
        vocal_inst = pm.instruments[0]
    if vocal_inst is None:
        log("[vocal_xml] vocal MIDI has no instruments")
        return False

    # Work on a mutable copy of the cues so we can peel words off one at a time
    cue_text = [list(c) for c in cues]   # [start, end, "remaining words"]
    cue_idx = 0

    s = stream.Score()
    s.insert(0, metadata.Metadata())
    s.metadata.title = title or "Vocal Score"
    s.metadata.composer = "Music Studio"

    part = stream.Part()
    part.append(layout.Staff())
    part.append(layout.MeasureNumbering())

    for n in vocal_inst.notes:
        m21_note = note.Note(n.pitch)
        m21_note.quarterLength = max(0.25, n.end - n.start)

        # Advance to the SRT cue whose interval contains this note
        while cue_idx < len(cue_text) and cue_text[cue_idx][1] < n.start:
            cue_idx += 1
        if cue_idx < len(cue_text):
            cs, ce, remaining = cue_text[cue_idx]
            if cs <= n.start <= ce and remaining:
                words = remaining.split()
                if words:
                    m21_note.lyric = words[0]
                    cue_text[cue_idx][2] = " ".join(words[1:])

        part.append(m21_note)

    s.append(part)
    s.write("musicxml", fp=str(out_xml))
    log(f"[vocal_xml] wrote {out_xml}")
    return True