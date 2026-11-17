"""Built-in background music: Narakeet's track names, synthesised locally.

Narakeet's own tracks can't be redistributed, so narrate renders stand-ins
under the same names, deterministically and licence-free: plucked strings
(Karplus-Strong) for the ukulele tracks, pads, plucked arpeggios, an
electric-piano voice, bass and soft percussion for the others. Each track is a
seamless loop (its tail wraps round to the start), rendered once into the
tools cache. Real recordings with the same names in $NARRATE_MUSIC_DIR take
precedence.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

import numpy as np

SR = 24000
TRACKS = ("ukulele-1", "ukulele-2", "uplifting-1", "uplifting-2", "corporate-1")
VERSION = 2  # bump to re-render cached tracks after changing the synthesis
# Mastered to sit about 10 dB under narration at volume 1; `background: x 2` raises it
LEVEL_LUFS = -30.0


def _hz(note: int) -> float:
    """MIDI note number -> frequency."""
    return 440.0 * 2 ** ((note - 69) / 12)


def _chord(root: int, minor: bool = False) -> list[int]:
    return [root, root + (3 if minor else 4), root + 7]


def _pluck(freq: float, seconds: float, rng: np.random.Generator, brightness: float = 0.5,
           decay: float = 0.996) -> np.ndarray:
    """Karplus-Strong plucked string, computed a period at a time."""
    period = max(2, int(round(SR / freq)))
    n = int(seconds * SR)
    out = np.zeros(n + period + 1, dtype=np.float64)
    burst = rng.uniform(-1, 1, period)
    # Smoothing the excitation softens the attack (lower brightness = warmer)
    for _ in range(int((1 - brightness) * 4)):
        burst = 0.5 * (burst + np.roll(burst, 1))
    out[:period] = burst
    for start in range(period, n + 1, period):
        block = out[start - period:start + 1]
        end = min(start + period, len(out))
        out[start:end] = decay * 0.5 * (block[:end - start] + block[1:end - start + 1])
    return out[:n] * np.exp(-np.arange(n) / SR * 1.2)


def _tone(freq: float, seconds: float, harmonics: list[float], attack: float = 0.01,
          release: float = 0.3, decay: float = 0.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    wave_ = sum(a * np.sin(2 * np.pi * freq * (k + 1) * t) for k, a in enumerate(harmonics))
    env = np.minimum(1.0, t / max(attack, 1e-3)) * np.exp(-t * decay)
    tail = int(release * SR)
    if 0 < tail < len(env):
        env[-tail:] *= np.linspace(1, 0, tail)
    return wave_ * env


def _pad(notes: list[int], seconds: float) -> np.ndarray:
    detune = [0.997, 1.0, 1.003]
    return sum(_tone(_hz(n) * d, seconds, [1, 0.35, 0.15, 0.07], attack=0.6, release=0.8)
               for n in notes for d in detune) / (len(notes) * len(detune))


def _keys(note: int, seconds: float) -> np.ndarray:
    """Electric-piano-ish: bell-like harmonics with a quick decay."""
    return _tone(_hz(note), seconds, [1, 0.4, 0.25, 0.1, 0.06], attack=0.005, release=0.2, decay=3.0)


def _kick(seconds: float = 0.35) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    freq = 50 + 70 * np.exp(-t * 30)
    return np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 9)


def _hat(rng: np.random.Generator, seconds: float = 0.06) -> np.ndarray:
    noise = rng.uniform(-1, 1, int(seconds * SR))
    noise = noise - np.convolve(noise, np.ones(4) / 4, mode="same")  # crude high-pass
    return noise * np.exp(-np.arange(len(noise)) / SR * 60)


def _reverb(signal: np.ndarray, rng: np.random.Generator, seconds: float = 1.2, wet: float = 0.25) -> np.ndarray:
    impulse = rng.standard_normal(int(seconds * SR)) * np.exp(-np.arange(int(seconds * SR)) / SR * 4.5)
    impulse /= np.sqrt(np.sum(impulse ** 2))
    size = 1 << int(np.ceil(np.log2(len(signal) + len(impulse))))
    tail = np.fft.irfft(np.fft.rfft(signal, size) * np.fft.rfft(impulse, size), size)[:len(signal) + len(impulse)]
    out = np.zeros_like(tail)
    out[:len(signal)] = signal
    return out * (1 - wet) + tail * wet


class _Score:
    def __init__(self, bpm: float, bars: int, seed: int):
        self.beat = 60.0 / bpm
        self.length = int(bars * 4 * self.beat * SR)
        self.mix = np.zeros(self.length + 4 * SR)  # room for tails, wrapped round later
        self.rng = np.random.default_rng(seed)

    def add(self, at_beats: float, sound: np.ndarray, gain: float) -> None:
        start = int(at_beats * self.beat * SR)
        end = min(len(self.mix), start + len(sound))
        self.mix[start:end] += sound[:end - start] * gain

    def render(self, reverb: float = 0.25) -> np.ndarray:
        mixed = _reverb(self.mix, self.rng, wet=reverb)
        loop = mixed[:self.length].copy()
        tail = mixed[self.length:]
        for i in range(0, len(tail), self.length):  # wrap tails round: seamless loop
            chunk = tail[i:i + self.length]
            loop[:len(chunk)] += chunk
        return (loop / (np.max(np.abs(loop)) + 1e-9) * 0.7).astype(np.float32)


UKULELE_STRINGS = [67, 60, 64, 69]  # G4 C4 E4 A4, re-entrant tuning


def _voicing(chord: list[int]) -> list[int]:
    """Each ukulele string fretted to the nearest chord tone at or above it."""
    tones = {n % 12 for n in chord}
    return [next(s + f for f in range(5) if (s + f) % 12 in tones) for s in UKULELE_STRINGS]


def ukulele_1() -> np.ndarray:
    score = _Score(bpm=100, bars=8, seed=11)
    chords = [_chord(60), _chord(67), _chord(69, True), _chord(65)] * 2  # C G Am F
    strum = [(0, 1.0, "d"), (1, 0.8, "d"), (1.5, 0.6, "u"), (2.5, 0.6, "u"), (3, 0.9, "d"), (3.5, 0.6, "u")]
    for bar, chord in enumerate(chords):
        strings = _voicing(chord)
        for beat, velocity, direction in strum:
            order = strings if direction == "d" else strings[::-1]
            for k, note in enumerate(order):
                at = bar * 4 + beat + k * 0.012 / score.beat + score.rng.normal(0, 0.004)
                score.add(at, _pluck(_hz(note), 1.4, score.rng, brightness=0.55), velocity * 0.25)
        score.add(bar * 4, _tone(_hz(chord[0] - 24), 3.6, [1, 0.2], attack=0.02, decay=0.8), 0.25)
    return score.render(reverb=0.2)


def ukulele_2() -> np.ndarray:
    score = _Score(bpm=88, bars=8, seed=22)
    chords = [_chord(65), _chord(60), _chord(62, True), _chord(70)] * 2  # F C Dm Bb
    pattern = [0, 2, 1, 3, 0, 3, 1, 2]
    for bar, chord in enumerate(chords):
        strings = _voicing(chord)
        for step, string in enumerate(pattern):
            score.add(bar * 4 + step * 0.5, _pluck(_hz(strings[string]), 1.6, score.rng, brightness=0.45),
                      0.28 if step % 2 == 0 else 0.2)
        for beat in range(4):
            score.add(bar * 4 + beat + 0.5, _hat(score.rng, 0.05), 0.04)
    return score.render(reverb=0.3)


def uplifting_1() -> np.ndarray:
    score = _Score(bpm=110, bars=8, seed=33)
    chords = [_chord(62), _chord(69), _chord(71, True), _chord(67)] * 2  # D A Bm G
    for bar, chord in enumerate(chords):
        score.add(bar * 4, _pad([n - 12 for n in chord], 4 * score.beat + 0.8), 0.5)
        arp = chord + [chord[0] + 12]
        for step in range(8):
            note = arp[[0, 1, 2, 3, 2, 1, 2, 3][step]] + 12
            score.add(bar * 4 + step * 0.5, _pluck(_hz(note), 0.8, score.rng, brightness=0.7, decay=0.99), 0.18)
        for beat in range(4):
            score.add(bar * 4 + beat, _kick(), 0.22)
        score.add(bar * 4, _tone(_hz(chord[0] - 24), 4 * score.beat, [1, 0.3], attack=0.02, decay=0.4), 0.3)
    return score.render(reverb=0.3)


def uplifting_2() -> np.ndarray:
    score = _Score(bpm=96, bars=8, seed=44)
    chords = [_chord(64, True), _chord(60), _chord(67), _chord(62)] * 2  # Em C G D
    for bar, chord in enumerate(chords):
        swell = 0.3 + 0.25 * (bar % 4) / 3
        score.add(bar * 4, _pad(chord + [chord[0] + 12], 4 * score.beat + 1.0), swell)
        for beat in (0, 1.5, 2, 3):
            for note in chord:
                score.add(bar * 4 + beat, _keys(note + 12, 1.2), 0.08)
        score.add(bar * 4, _tone(_hz(chord[0] - 24), 4 * score.beat, [1, 0.25], attack=0.05, decay=0.3), 0.28)
    return score.render(reverb=0.35)


def corporate_1() -> np.ndarray:
    score = _Score(bpm=112, bars=8, seed=55)
    chords = [_chord(63), _chord(60, True), _chord(68), _chord(70)] * 2  # Eb Cm Ab Bb
    for bar, chord in enumerate(chords):
        score.add(bar * 4, _pad(chord, 4 * score.beat + 0.6), 0.32)
        for step in range(8):
            score.add(bar * 4 + step * 0.5, _keys(chord[step % 3] + 12, 0.5), 0.12)
        for beat in range(4):
            score.add(bar * 4 + beat, _kick(), 0.25)
            score.add(bar * 4 + beat + 0.5, _hat(score.rng), 0.06)
        for beat in (0, 1.5, 2.5):
            score.add(bar * 4 + beat, _tone(_hz(chord[0] - 24), 0.6, [1, 0.4], attack=0.01, decay=2.0), 0.3)
    return score.render(reverb=0.22)


SYNTHS = {"ukulele-1": ukulele_1, "ukulele-2": ukulele_2, "uplifting-1": uplifting_1,
          "uplifting-2": uplifting_2, "corporate-1": corporate_1}


def track(name: str) -> Path:
    """A playable file for a built-in track name: a user recording, or narrate's own rendering."""
    if user_dir := os.environ.get("NARRATE_MUSIC_DIR"):
        for ext in (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"):
            if (candidate := Path(user_dir) / f"{name}{ext}").is_file():
                return candidate
    from .tts import tools_dir

    path = tools_dir() / "music" / f"v{VERSION}" / f"{name}.wav"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        from . import media

        audio = SYNTHS[name]()
        if (measured := media.loudness(audio, SR)) is not None:
            audio = audio * 10 ** ((LEVEL_LUFS - measured) / 20)
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
        part = path.with_suffix(".part")
        # Opened first: when wave.open opens a file that can't be written, its half-made writer
        # complains again as it's collected, after the error itself (#90)
        with open(part, "wb") as raw, wave.open(raw, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        part.rename(path)
    return path
