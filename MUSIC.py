"""
MUSIC.py
Pure-logic pipeline for the "Full Transcription (TAB+MIDI)" feature.

Loaded at runtime by app.py's _run_full_transcription_job() via
importlib.util.spec_from_file_location(), so this file must live in
the SAME directory as app.py.

The web worker calls the 13 pipeline functions below directly — one
per stage — so it can update JOBS[job_id]["progress"] between them.
This file deliberately contains NO desktop entry point (no main(),
no Tkinter, no all-in-one process_song()) so it is safe to load on a
headless server.

Pipeline functions (called in this order by the web worker):
    convert_input_to_wav          -> 01_INPUT_AUDIO.wav
    separate_sources              -> (optional) Demucs "other" stem
    detect_tempo                  -> float BPM
    get_audio_duration_seconds    -> float seconds
    extract_melody                -> frame-level notes
    smooth_melody                 -> musical notes
    make_tab_notes                -> string/fret positions
    detect_chords                 -> per-beat chord segments
    create_guitar_midi            -> 03_GUITAR_SOLO.mid
    create_chord_midi             -> 04_RHYTHM_CHORDS.mid
    draw_guitar_tab_pdf           -> 01_GUITAR_SOLO_TAB.pdf
    draw_chord_pdf                -> 02_RHYTHM_CHORDS.pdf
    create_report                 -> TRANSCRIPTION_REPORT.txt
"""

import sys
import subprocess
import shutil
import math
from pathlib import Path

import numpy as np
import librosa
import soundfile as sf

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors

from music21 import stream, note, chord, meter, tempo


# ============================================================
# CONFIGURATION
# ============================================================

SAMPLE_RATE = 44100

# Guitar standard tuning, low E to high E
GUITAR_TUNING = {
    6: 40,  # E2
    5: 45,  # A2
    4: 50,  # D3
    3: 55,  # G3
    2: 59,  # B3
    1: 64,  # E4
}

STRING_NAMES = {
    6: "E",
    5: "A",
    4: "D",
    3: "G",
    2: "B",
    1: "e",
}

NOTE_NAMES = [
    "C", "C#", "D", "D#", "E", "F",
    "F#", "G", "G#", "A", "A#", "B"
]

# Common guitar chord templates
CHORD_TEMPLATES = {
    "":      [0, 4, 7],
    "m":     [0, 3, 7],
    "7":     [0, 4, 7, 10],
    "maj7":  [0, 4, 7, 11],
    "m7":    [0, 3, 7, 10],
    "sus2":  [0, 2, 7],
    "sus4":  [0, 5, 7],
    "dim":   [0, 3, 6],
    "aug":   [0, 4, 8],
}


# ============================================================
# GENERAL UTILITIES
# ============================================================

def run_command(command):
    print("\n>", " ".join(str(x) for x in command))

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    if result.returncode != 0:
        print(result.stdout)
        # Previously this only included the command itself in the raised
        # error, not the program's actual output — so a real failure
        # (bad codec, out of memory, model download failure, a path
        # ffmpeg/demucs choked on, etc.) surfaced in the UI as just
        # "Command failed: <command>", with zero indication of what
        # actually went wrong. That made every failure look identical
        # and unfixable ("doesn't work solid") even when the underlying
        # cause was different and diagnosable every time. Including the
        # tail of the actual output fixes that.
        tail = (result.stdout or "").strip()[-1500:]
        raise RuntimeError(
            "Command failed:\n" + " ".join(str(x) for x in command)
            + ("\n\n--- output ---\n" + tail if tail else "")
        )

    return result.stdout


def check_program(program):
    return shutil.which(program) is not None


def midi_to_note_name(midi_number):
    midi_number = int(round(midi_number))
    name = NOTE_NAMES[midi_number % 12]
    octave = midi_number // 12 - 1
    return f"{name}{octave}"


def midi_to_frequency(midi_number):
    return 440.0 * (2 ** ((midi_number - 69) / 12))


def frequency_to_midi(freq):
    if freq <= 0:
        return None
    return 69 + 12 * math.log2(freq / 440.0)


def frequency_to_midi_rounded(freq):
    value = frequency_to_midi(freq)
    if value is None:
        return None
    return int(round(value))


# ============================================================
# INPUT CONVERSION
# ============================================================

def convert_input_to_wav(input_file, output_wav):
    """
    Converts MP3/MP4/WAV/etc. into a clean mono WAV file.
    """
    if not check_program("ffmpeg"):
        raise RuntimeError(
            "FFmpeg was not found.\n"
            "Install FFmpeg and add it to PATH."
        )

    command = [
        "ffmpeg",
        "-y",
        "-i", str(input_file),
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-sample_fmt", "s16",
        str(output_wav)
    ]

    run_command(command)
    return output_wav


# ============================================================
# SOURCE SEPARATION
# ============================================================

def separate_sources(input_wav, output_dir):
    """
    Uses Demucs to separate the song into 4 stems.

    Returns the path to the "other" stem (guitar/piano/etc.), or
    None if Demucs is unavailable or fails — in which case the
    caller should fall back to using the full mix.

    Expected Demucs output:
        vocals.wav
        drums.wav
        bass.wav
        other.wav

    'other' is normally the most useful stem for guitar,
    piano and other harmonic instruments.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not check_program("demucs"):
        print("\nWARNING: Demucs is not installed.")
        print("The program will use the original audio instead.")
        return None

    demucs_dir = output_dir / "demucs"

    # Full 4-stem separation (vocals/drums/bass/other) instead of
    # a 2-stem vocals split. With only 2 stems, drums and bass stay
    # mixed in with the guitar, which makes the monophonic pitch
    # tracker (pyin) unreliable for most of the song. Isolating
    # "other" (guitar/piano/etc.) gives it a much cleaner signal
    # across the whole track, not just a few clean passages.
    command = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        "htdemucs",
        "-o",
        str(demucs_dir),
        str(input_wav)
    ]

    try:
        run_command(command)
    except Exception as exc:
        print("Demucs failed:", exc)
        return None

    stem_root = demucs_dir / "htdemucs"

    # Prefer "other" first now that we have a true 4-stem split.
    candidates = list(stem_root.rglob("other.wav"))
    if candidates:
        print("Using Demucs 'other' stem (guitar/piano/etc.):")
        print(candidates[0])
        return candidates[0]

    # Fall back to no_vocals.wav in case an older/2-stem model
    # config was used for some reason.
    candidates = list(stem_root.rglob("no_vocals.wav"))
    if candidates:
        print("Using Demucs no-vocals stem:")
        print(candidates[0])
        return candidates[0]

    return None


# ============================================================
# GUITAR SOLO / MELODY EXTRACTION
# ============================================================

def extract_melody(audio_file):
    """
    Extracts a monophonic melody using librosa.pyin.

    This is especially useful when the guitar solo is reasonably
    isolated after source separation.
    """
    print("\nExtracting melody...")

    y, sr = librosa.load(
        str(audio_file),
        sr=SAMPLE_RATE,
        mono=True
    )

    # Normalize
    peak = np.max(np.abs(y))
    if peak > 0:
        y = y / peak

    # Remove very low level noise
    y_trimmed, index = librosa.effects.trim(y, top_db=35)
    if len(y_trimmed) == 0:
        raise RuntimeError("No usable audio detected.")
    y = y_trimmed

    # PYIN
    # hop_length=256 (~5.8ms/frame at 44.1kHz) is very fine-grained — for
    # a full song (unlike the Guitar Tab feature, which only analyzes a
    # short user-picked range) this means tens of thousands of frames of
    # librosa.pyin's fairly expensive per-frame search, easily adding
    # several extra minutes with the UI frozen on one progress percentage
    # the whole time (there's no way to report progress mid-call — pyin
    # is one opaque call). 512 halves the frame count/runtime while still
    # giving ~11.6ms time resolution, which is more than fine for
    # detecting individual guitar notes/onsets.
    f0, voiced_flag, voiced_prob = librosa.pyin(
        y,
        fmin=librosa.note_to_hz("E2"),
        fmax=librosa.note_to_hz("E6"),
        frame_length=4096,
        hop_length=512,
        sr=sr
    )

    times = librosa.times_like(f0, sr=sr, hop_length=512)

    # A fixed 0.60 confidence cutoff can wipe out entire sections
    # of a song (anything not a very clean, isolated single note).
    # Instead, use an adaptive threshold based on the distribution
    # of confidence values actually present in this track, with a
    # floor so we don't accept pure noise. This keeps far more of
    # the song's notes while still filtering out the least
    # reliable frames.
    voiced_confidences = [
        voiced_prob[i]
        for i in range(len(f0))
        if f0[i] is not None and voiced_flag[i]
    ]

    if voiced_confidences:
        adaptive_threshold = min(
            0.60,
            max(0.35, float(np.percentile(voiced_confidences, 25)))
        )
    else:
        adaptive_threshold = 0.35

    print(f"Using adaptive confidence threshold: {adaptive_threshold:.2f}")

    notes = []
    for i, freq in enumerate(f0):
        if freq is None:
            continue
        if not voiced_flag[i]:
            continue
        if voiced_prob[i] < adaptive_threshold:
            continue

        midi = frequency_to_midi_rounded(freq)
        if midi is None:
            continue

        # Guitar range filter
        if midi < 40 or midi > 88:
            continue

        notes.append({
            "time": float(times[i]),
            "midi": midi,
            "frequency": float(freq),
            "confidence": float(voiced_prob[i])
        })

    print("Raw detected frames:", len(notes))
    return notes


def smooth_melody(notes):
    """
    Converts many frame-level detections into actual musical notes.
    """
    if not notes:
        return []

    result = []

    current_pitch = notes[0]["midi"]
    start_time = notes[0]["time"]
    last_time = notes[0]["time"]
    pitches = [current_pitch]

    for item in notes[1:]:
        pitch = item["midi"]
        t = item["time"]

        # Gap
        if t - last_time > 0.15:
            avg_pitch = int(round(np.median(pitches)))
            result.append({
                "start": start_time,
                "end": last_time,
                "midi": avg_pitch
            })
            current_pitch = pitch
            start_time = t
            pitches = [pitch]

        # Same note / small variation
        elif abs(pitch - current_pitch) <= 1:
            pitches.append(pitch)

        else:
            # Finish previous note
            avg_pitch = int(round(np.median(pitches)))
            if last_time - start_time >= 0.08:
                result.append({
                    "start": start_time,
                    "end": last_time,
                    "midi": avg_pitch
                })
            current_pitch = pitch
            start_time = t
            pitches = [pitch]

        last_time = t

    if pitches:
        avg_pitch = int(round(np.median(pitches)))
        if last_time - start_time >= 0.08:
            result.append({
                "start": start_time,
                "end": last_time,
                "midi": avg_pitch
            })

    # Remove extremely short notes
    result = [n for n in result if n["end"] - n["start"] >= 0.07]

    print("Musical notes detected:", len(result))
    return result


# ============================================================
# GUITAR FINGERING / TAB
# ============================================================

def find_best_guitar_position(midi_number, previous_position=None):
    """
    Finds a playable guitar string/fret.

    Attempts to keep consecutive notes in a natural position.
    """
    possibilities = []
    for string_number, open_midi in GUITAR_TUNING.items():
        fret = midi_number - open_midi
        if 0 <= fret <= 22:
            possibilities.append((string_number, fret))

    if not possibilities:
        return None

    if previous_position is None:
        # Prefer middle strings / moderate frets
        return min(possibilities, key=lambda x: abs(x[1] - 7))

    previous_string, previous_fret = previous_position

    def cost(position):
        string_number, fret = position
        return (
            abs(fret - previous_fret)
            + abs(string_number - previous_string) * 1.5
        )

    return min(possibilities, key=cost)


def make_tab_notes(notes):
    result = []
    previous_position = None

    for n in notes:
        position = find_best_guitar_position(n["midi"], previous_position)
        if position is None:
            continue

        string_number, fret = position
        item = dict(n)
        item["string"] = string_number
        item["fret"] = fret
        item["note_name"] = midi_to_note_name(n["midi"])

        result.append(item)
        previous_position = position

    return result


# ============================================================
# AUDIO DURATION
# ============================================================

def get_audio_duration_seconds(audio_file):
    """
    Reads the exact duration of an audio file (in seconds) using
    soundfile, which is fast and doesn't require decoding the
    whole file into memory twice. Used so the generated MIDI/PDF
    output can be padded out to the true length of the song,
    instead of silently stopping wherever pitch/chord detection
    happened to give up.
    """
    with sf.SoundFile(str(audio_file)) as f:
        return len(f) / f.samplerate


# ============================================================
# TEMPO
# ============================================================

def detect_tempo(audio_file):
    y, sr = librosa.load(str(audio_file), sr=SAMPLE_RATE, mono=True)

    tempo_value, beats = librosa.beat.beat_track(y=y, sr=sr)

    try:
        tempo_value = float(np.asarray(tempo_value).flatten()[0])
    except Exception:
        tempo_value = 120.0

    if tempo_value < 40:
        tempo_value *= 2
    if tempo_value > 220:
        tempo_value /= 2

    return tempo_value


# ============================================================
# CHORD DETECTION
# ============================================================

def chord_name_from_chroma(chroma_vector):
    chroma_vector = np.asarray(chroma_vector)

    if np.max(chroma_vector) <= 0:
        return "N"

    chroma_vector = chroma_vector / np.max(chroma_vector)

    best_name = "N"
    best_score = -999

    for root in range(12):
        for suffix, intervals in CHORD_TEMPLATES.items():
            template = np.zeros(12)
            for interval in intervals:
                template[(root + interval) % 12] = 1

            # weighted similarity
            score = np.dot(chroma_vector, template)
            # Reward root
            score += chroma_vector[root] * 0.5

            if score > best_score:
                best_score = score
                best_name = NOTE_NAMES[root] + suffix

    # Reject very weak matches
    if best_score < 1.0:
        return "N"

    return best_name


def detect_chords(audio_file, tempo_bpm):
    print("\nDetecting chords...")

    y, sr = librosa.load(str(audio_file), sr=SAMPLE_RATE, mono=True)

    hop_length = 2048
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop_length)

    frame_times = librosa.frames_to_time(
        np.arange(chroma.shape[1]),
        sr=sr,
        hop_length=hop_length
    )

    # One chord per beat
    beat_length = 60.0 / tempo_bpm
    duration = len(y) / sr

    chords = []
    t = 0.0
    while t < duration:
        end = min(t + beat_length, duration)
        mask = (frame_times >= t) & (frame_times < end)

        if np.any(mask):
            vector = np.mean(chroma[:, mask], axis=1)
            chord_name = chord_name_from_chroma(vector)
        else:
            chord_name = "N"

        chords.append({
            "start": t,
            "end": end,
            "chord": chord_name
        })
        t = end

    # Merge consecutive identical chords
    merged = []
    for item in chords:
        if merged and merged[-1]["chord"] == item["chord"]:
            merged[-1]["end"] = item["end"]
        else:
            merged.append(dict(item))

    print("Chord segments:", len(merged))
    return merged


# ============================================================
# MIDI CREATION
# ============================================================

def create_guitar_midi(
    tab_notes,
    tempo_bpm,
    output_file,
    total_duration_seconds=None
):
    print("\nCreating guitar MIDI...")

    score = stream.Score()
    part = stream.Part()
    part.append(tempo.MetronomeMark(number=tempo_bpm))
    part.append(meter.TimeSignature("4/4"))

    # Tracks how far along the timeline (in seconds) we've already
    # written, so we can insert a rest to fill any gap between the
    # end of the previous note and the start of the next one.
    # Without this, undetected passages are simply skipped and the
    # whole output collapses to be much shorter than the song.
    cursor_seconds = 0.0

    for n in tab_notes:
        gap_seconds = n["start"] - cursor_seconds
        if gap_seconds > 0.05:
            rest_quarter_length = gap_seconds * tempo_bpm / 60.0
            rest = note.Rest()
            rest.duration.quarterLength = max(0.05, rest_quarter_length)
            part.append(rest)

        duration_seconds = max(0.10, n["end"] - n["start"])
        quarter_length = duration_seconds * tempo_bpm / 60.0

        midi_note = note.Note(n["midi"])
        midi_note.duration.quarterLength = max(0.125, quarter_length)
        part.append(midi_note)

        cursor_seconds = n["start"] + duration_seconds

    # Pad a final rest out to the true end of the song. Without
    # this, the MIDI simply stops after the last detected note,
    # so a fade-out, sustained note, or any undetected tail
    # section gets cut off entirely and the output is shorter
    # than the actual input audio.
    if total_duration_seconds is not None:
        trailing_gap_seconds = total_duration_seconds - cursor_seconds
        if trailing_gap_seconds > 0.05:
            trailing_rest = note.Rest()
            trailing_rest.duration.quarterLength = max(
                0.05,
                trailing_gap_seconds * tempo_bpm / 60.0
            )
            part.append(trailing_rest)

    # IMPORTANT: music21's MIDI writer only looks at actual note
    # on/off events to decide where the track ends. Any trailing
    # Rest with nothing audible after it - whether it's the padding
    # rest above, or simply the natural last element of tab_notes -
    # produces no MIDI events at all and gets silently dropped, so
    # the file still ends at the last real note regardless of how
    # much rest padding was appended. To force the file to actually
    # run the full song length, always insert a near-silent one-tick
    # "marker" note right at the true end of the part's timeline.
    if part.highestTime > 0:
        end_offset = part.highestTime
        marker = note.Note(60)
        marker.volume.velocity = 1
        marker.duration.quarterLength = 0.05
        part.insert(max(0.0, end_offset - marker.duration.quarterLength), marker)

    score.append(part)
    score.write("midi", fp=str(output_file))

    return output_file


def create_chord_midi(
    chords,
    tempo_bpm,
    output_file,
    total_duration_seconds=None
):
    score = stream.Score()
    part = stream.Part()
    part.append(tempo.MetronomeMark(number=tempo_bpm))
    part.append(meter.TimeSignature("4/4"))

    cursor_seconds = 0.0

    for item in chords:
        name = item["chord"]
        duration = item["end"] - item["start"]
        quarter_length = max(0.25, duration * tempo_bpm / 60.0)

        if name == "N":
            # Keep a rest here instead of dropping the segment
            # entirely, so later chords stay lined up with the
            # actual song timeline instead of sliding earlier.
            rest = note.Rest()
            rest.duration.quarterLength = quarter_length
            part.append(rest)
            cursor_seconds = item["end"]
            continue

        root_name = name[0]
        if len(name) > 1 and name[1] == "#":
            root_name += "#"
        elif len(name) > 1 and name[1] == "b":
            root_name += "-"

        try:
            c = chord.Chord(root_name)
            c.duration.quarterLength = quarter_length
            part.append(c)
            cursor_seconds = item["end"]
        except Exception:
            # Even if the chord object failed to build, keep a
            # rest of the right length so timing doesn't drift.
            rest = note.Rest()
            rest.duration.quarterLength = quarter_length
            part.append(rest)
            cursor_seconds = item["end"]

    # Pad a final rest out to the true end of the song, in case
    # rounding or an empty chords list left a gap at the very end.
    if total_duration_seconds is not None:
        trailing_gap_seconds = total_duration_seconds - cursor_seconds
        if trailing_gap_seconds > 0.05:
            trailing_rest = note.Rest()
            trailing_rest.duration.quarterLength = max(
                0.05,
                trailing_gap_seconds * tempo_bpm / 60.0
            )
            part.append(trailing_rest)

    # Same fix as in create_guitar_midi: a trailing Rest - whether
    # it's this padding rest, or simply the last chord segment being
    # an "N" (no chord) - produces no MIDI events and gets silently
    # dropped by music21's writer. Always anchor a near-silent marker
    # note at the true end of the timeline so the file's actual
    # length matches the full song, not just the last audible chord.
    if part.highestTime > 0:
        end_offset = part.highestTime
        marker = note.Note(60)
        marker.volume.velocity = 1
        marker.duration.quarterLength = 0.05
        part.insert(max(0.0, end_offset - marker.duration.quarterLength), marker)

    score.append(part)
    score.write("midi", fp=str(output_file))

    return output_file


# ============================================================
# GUITAR TAB PDF
# ============================================================

def draw_guitar_tab_pdf(
    tab_notes,
    tempo_bpm,
    output_file,
    title="Guitar Solo — Standard Notation + TAB"
):
    print("\nCreating guitar TAB PDF...")

    page_width, page_height = A4

    c = canvas.Canvas(str(output_file), pagesize=A4)
    margin = 40
    c.setTitle(title)

    # Header
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, page_height - 45, title)

    c.setFont("Helvetica", 9)
    c.drawString(
        margin,
        page_height - 60,
        f"Detected tempo: {tempo_bpm:.1f} BPM    "
        f"Standard tuning: E A D G B E"
    )

    y = page_height - 100
    line_spacing = 12
    notes_per_row = 12

    rows = [
        tab_notes[i:i + notes_per_row]
        for i in range(0, len(tab_notes), notes_per_row)
    ]

    if not rows:
        c.setFont("Helvetica", 12)
        c.drawString(margin, y, "No reliable guitar melody was detected.")

    for row_index, row in enumerate(rows):
        if y < 130:
            c.showPage()
            y = page_height - 60
            c.setFont("Helvetica-Bold", 16)
            c.drawString(margin, y, title + " — continued")
            y -= 45

        # ----------------------------------------------------
        # Standard notation area
        # ----------------------------------------------------
        notation_y = y

        c.setFont("Helvetica-Bold", 9)
        c.drawString(margin, notation_y + 30, "STANDARD NOTATION")

        staff_top = notation_y + 15
        for line in range(5):
            yy = staff_top - line * 6
            c.line(margin, yy, page_width - margin, yy)

        # Treble clef
        c.setFont("Times-Bold", 22)
        c.drawString(margin + 5, staff_top - 25, "𝄞")

        # Draw approximate noteheads
        x_start = margin + 45
        x_end = page_width - margin - 10
        usable_width = x_end - x_start
        step = usable_width / max(1, len(row))

        for i, n in enumerate(row):
            x = x_start + i * step
            midi = n["midi"]
            staff_position = (midi - 64) * 0.85
            note_y = staff_top - 24 + staff_position

            c.ellipse(
                x - 3, note_y - 2, x + 3, note_y + 2,
                stroke=1, fill=1
            )
            c.line(x + 3, note_y, x + 3, note_y + 22)

            c.setFont("Helvetica", 6)
            c.drawCentredString(x, staff_top - 43, n["note_name"])

        # ----------------------------------------------------
        # TAB
        # ----------------------------------------------------
        tab_top = y - 55

        c.setFont("Helvetica-Bold", 9)
        c.drawString(margin, tab_top + 12, "GUITAR TAB")

        for string_number in range(1, 7):
            yy = tab_top - (string_number - 1) * line_spacing
            c.line(margin, yy, page_width - margin, yy)

            c.setFont("Helvetica-Bold", 7)
            c.drawString(margin - 20, yy - 3, STRING_NAMES[string_number])

        # Frets
        x_start = margin + 20
        step = (page_width - margin - x_start - 5) / max(1, len(row))

        for i, n in enumerate(row):
            x = x_start + i * step
            string_number = n["string"]
            fret = n["fret"]
            yy = tab_top - (string_number - 1) * line_spacing

            # White background for fret number
            c.setFillColor(colors.white)
            c.rect(x - 5, yy - 4, 10, 8, stroke=0, fill=1)

            c.setFillColor(colors.black)
            c.setFont("Helvetica-Bold", 8)
            c.drawCentredString(x, yy - 3, str(fret))

        # Row number
        c.setFont("Helvetica", 7)
        c.drawString(page_width - 70, tab_top - 70, f"Line {row_index + 1}")

        y = tab_top - 100

    c.save()
    return output_file


# ============================================================
# RHYTHM / CHORD PDF
# ============================================================

def draw_chord_pdf(
    chords,
    tempo_bpm,
    output_file,
    title="Rhythm Guitar — Chords + Rhythm Chart"
):
    print("\nCreating rhythm/chords PDF...")

    page_width, page_height = A4

    c = canvas.Canvas(str(output_file), pagesize=A4)
    margin = 40
    c.setTitle(title)

    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, page_height - 45, title)

    c.setFont("Helvetica", 9)
    c.drawString(
        margin,
        page_height - 60,
        f"Detected tempo: {tempo_bpm:.1f} BPM    "
        f"Time signature: 4/4"
    )

    y = page_height - 100

    beats_per_measure = 4

    # Convert chord segments into beat-level chart
    beat_chords = []
    for item in chords:
        duration = item["end"] - item["start"]
        number_of_beats = max(
            1,
            int(round(duration * tempo_bpm / 60.0))
        )
        for _ in range(number_of_beats):
            beat_chords.append(item["chord"])

    # Group into measures
    measures = [
        beat_chords[i:i + beats_per_measure]
        for i in range(0, len(beat_chords), beats_per_measure)
    ]

    measures_per_row = 4
    measure_rows = [
        measures[i:i + measures_per_row]
        for i in range(0, len(measures), measures_per_row)
    ]

    for row_number, row in enumerate(measure_rows):
        if y < 170:
            c.showPage()
            y = page_height - 60
            c.setFont("Helvetica-Bold", 16)
            c.drawString(margin, y, title + " — continued")
            y -= 50

        c.setFont("Helvetica-Bold", 9)
        c.drawString(
            margin,
            y,
            f"Measures {row_number * measures_per_row + 1}"
        )
        y -= 20

        box_width = (page_width - 2 * margin) / measures_per_row
        box_height = 70

        for m_index, measure in enumerate(row):
            x = margin + m_index * box_width

            # Measure box
            c.rect(x, y - box_height, box_width, box_height)

            # Measure number
            actual_measure = row_number * measures_per_row + m_index + 1
            c.setFont("Helvetica", 7)
            c.drawString(x + 4, y - 12, str(actual_measure))

            # Beat divisions
            beat_width = box_width / 4
            for beat in range(1, 4):
                xx = x + beat * beat_width
                c.line(xx, y - box_height, xx, y)

            # Chords
            for beat, chord_name in enumerate(measure):
                xx = x + beat * beat_width + beat_width / 2

                c.setFont("Helvetica-Bold", 10)
                c.drawCentredString(xx, y - 34, chord_name)

                # Downbeat marker
                c.setFont("Helvetica", 6)
                c.drawCentredString(xx, y - 48, str(beat + 1))

        y -= box_height + 45

    # Chord dictionary
    if y < 180:
        c.showPage()
        y = page_height - 60

    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Chord Reference")
    y -= 25

    unique_chords = []
    for item in chords:
        name = item["chord"]
        if name not in unique_chords:
            unique_chords.append(name)

    c.setFont("Helvetica", 9)
    x = margin
    for name in unique_chords:
        if x > page_width - 100:
            x = margin
            y -= 20
        c.drawString(x, y, name)
        x += 65

    y -= 40

    # Rhythm explanation
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, y, "Basic Rhythm Guide")
    y -= 18

    c.setFont("Helvetica", 8)
    rhythm_lines = [
        "1   2   3   4",
        "↓   ↓   ↓   ↓",
        "Downstroke on each beat",
        "",
        "For a more complete performance:",
        "↓   ↓↑   ↑↓↑",
    ]
    for line in rhythm_lines:
        c.drawString(margin, y, line)
        y -= 12

    c.save()
    return output_file


# ============================================================
# TEXT REPORT
# ============================================================

def create_report(
    output_file,
    input_file,
    tempo_bpm,
    tab_notes,
    chords,
    total_duration_seconds=None
):
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("MUSIC TRANSCRIPTION REPORT\n")
        f.write("===========================\n\n")
        f.write(f"Input: {input_file}\n")
        f.write(f"Tempo: {tempo_bpm:.2f} BPM\n")

        if total_duration_seconds is not None:
            minutes = int(total_duration_seconds // 60)
            seconds = total_duration_seconds - minutes * 60

            f.write(
                f"Total song duration: {minutes}:{seconds:05.2f} "
                f"({total_duration_seconds:.2f} sec)\n"
            )

            if tab_notes:
                last_note_end = max(n["end"] for n in tab_notes)
                coverage_pct = (
                    100.0 * last_note_end / total_duration_seconds
                    if total_duration_seconds > 0 else 0.0
                )
                f.write(
                    "Guitar melody detected up to: "
                    f"{last_note_end:.2f} sec "
                    f"({coverage_pct:.0f}% of song; the MIDI/PDF "
                    "still runs the full song length, with rests "
                    "filling any undetected passages)\n"
                )

        f.write("\n")
        f.write("GUITAR NOTES\n")
        f.write("------------\n")
        for n in tab_notes:
            f.write(
                f"{n['start']:8.2f}  "
                f"{n['note_name']:4s}  "
                f"String {n['string']}  "
                f"Fret {n['fret']}\n"
            )

        f.write("\nCHORDS\n")
        f.write("------\n")
        for item in chords:
            f.write(
                f"{item['start']:8.2f} - "
                f"{item['end']:8.2f} : "
                f"{item['chord']}\n"
            )