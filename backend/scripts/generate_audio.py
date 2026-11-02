"""Generate original royalty-free audio loops for the demo catalog.

Each track's loop is synthesised from its authored BPM, Camelot key and energy, so
the audio actually matches the acoustic metadata the AI DJ reasons over. Output is
8-bit / 16 kHz mono WAV to keep the repository small; these are demo assets, not
production masters.

Usage: python scripts/generate_audio.py [--force]
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from track_data import TRACKS  # noqa: E402

SAMPLE_RATE = 16000
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "static" / "audio"

# Camelot code -> root MIDI note (A minor = 57? using A3 = 57 for minor keys).
_CAMELOT_ROOT = {
    "1A": 56, "2A": 51, "3A": 58, "4A": 53, "5A": 48, "6A": 55,
    "7A": 50, "8A": 57, "9A": 52, "10A": 47, "11A": 54, "12A": 49,
    "1B": 59, "2B": 54, "3B": 61, "4B": 56, "5B": 51, "6B": 58,
    "7B": 53, "8B": 48, "9B": 55, "10B": 50, "11B": 57, "12B": 52,
}

_MINOR_STEPS = (0, 3, 7)
_MAJOR_STEPS = (0, 4, 7)
# i - VI - III - VII style movement, expressed in semitones from the root.
_PROGRESSION = (0, 8, 3, 10)


def midi_to_freq(note: float) -> float:
    return 440.0 * (2 ** ((note - 69) / 12.0))


def _render_track(track: dict) -> bytes:
    bpm = float(track["bpm"])
    seconds = int(track["duration"])
    key = track["key"]
    energy = float(track["energy"])
    root = _CAMELOT_ROOT[key]
    minor = key.endswith("A")
    steps = _MINOR_STEPS if minor else _MAJOR_STEPS
    beat = 60.0 / bpm
    total_samples = seconds * SAMPLE_RATE
    samples = array("B", bytes(total_samples))

    chord_len = beat * 4
    bass_freq = midi_to_freq(root - 12)
    # Chord voices (three notes) per progression step, as frequencies.
    chords = [
        [midi_to_freq(root + step + interval) for interval in steps]
        for step in _PROGRESSION
    ]
    pad_gain = 0.16 + 0.1 * energy
    bass_gain = 0.20 + 0.08 * energy
    lead_gain = 0.04 + 0.12 * energy

    for index in range(total_samples):
        t = index / SAMPLE_RATE
        chord_index = int(t // chord_len) % len(chords)
        chord = chords[chord_index]
        position_in_beat = (t % beat) / beat

        value = 0.0
        for voice_index, frequency in enumerate(chord):
            detune = 1.0 + 0.0015 * (voice_index - 1)
            value += pad_gain * math.sin(2 * math.pi * frequency * detune * t)
        value += bass_gain * math.sin(2 * math.pi * bass_freq * t)

        # Kick: decaying sine thump on each beat.
        if position_in_beat < 0.16:
            envelope = math.exp(-position_in_beat * 38)
            value += 0.5 * envelope * math.sin(2 * math.pi * (58 - 28 * position_in_beat) * t)
        # Off-beat hat: short high-frequency burst.
        hat_pos = (t + beat / 2) % beat / beat
        if hat_pos < 0.05:
            value += 0.10 * math.exp(-hat_pos * 90) * math.sin(2 * math.pi * 6200 * t)
        # Lead arpeggio on 16ths for higher-energy tracks.
        if energy > 0.45:
            sixteenth = beat / 4
            step_index = int(t // sixteenth)
            lead_freq = chord[step_index % len(chord)] * 2
            arp_pos = (t % sixteenth) / sixteenth
            value += lead_gain * math.exp(-arp_pos * 12) * math.sin(2 * math.pi * lead_freq * t)

        # Gentle fade in/out to avoid clicks and make looping smoother.
        fade = min(1.0, t / 0.8, (seconds - t) / 1.2)
        value *= max(0.0, fade)

        magnitude = max(-1.0, min(1.0, value))
        samples[index] = int(128 + magnitude * 120)

    header = b"RIFF" + struct.pack("<I", 36 + total_samples) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE, 1, 8)
    header += b"data" + struct.pack("<I", total_samples)
    return header + samples.tobytes()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Regenerate existing files")
    args = parser.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for track in TRACKS:
        target = OUTPUT_DIR / f"{track['slug']}.wav"
        if target.exists() and not args.force:
            print(f"skip {target.name}")
            continue
        target.write_bytes(_render_track(track))
        print(f"wrote {target.name} ({target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
