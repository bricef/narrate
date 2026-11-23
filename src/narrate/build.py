"""Build a video from a parsed script: plan each scene, render segments, mux.

Timing follows spec §4: a scene's narration track (speech, pauses, recorded
audio) is laid out first, then the scene's duration is derived from it and from
any video's sync mode. The global timeline is kept in whole frames, so every
scene's audio is cut to exactly its frames' length and audio and video never
drift apart.
"""

from __future__ import annotations

import dataclasses
import os
from contextlib import closing, contextmanager
import shutil
import sys
from concurrent.futures import Future, ThreadPoolExecutor, wait
from itertools import accumulate
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection

import numpy as np

from . import media, motion, music, network, pages, render, subtitles, voicemap
from .config import (
    BUILTIN_MUSIC, FPS, XFADE, ConfigError, Settings, SubtitleConfig, parse_pitch, parse_transition, parse_color, parse_number, parse_size, parse_speed,
    OverlaySpec, parse_overlay, parse_time, parse_volume, settings_from_header, subtitles_with_mode,
)
from .doctor import find_font
from .frames import FrameRenderer, FrameSpec, SlideSpec
from .script import ANIMATED_SIZES, POSITIONS, SIZES, Directive, Media, Narration, Quote, Render, Scene, Script, Slide
from .stt import captions as stt_captions
from .subtitles import Cue
from .ssml import ssml_runs
from .text import Run, speech_runs, ssml_text, subtitle_text
from .tts import SAMPLE_RATE, EstimatingSynth, Prefetching, Synth, Voice

SR = SAMPLE_RATE
PARAGRAPH_PAUSE = {"normal": 0.4, "fragment": 0.1, "merge": 0.4}
# Directives that shape the narration (applied in order when scenes merge)
NARRATION_KEYS = {"voice", "voice-speed", "voice-volume", "voice-pitch", "voice-emphasis",
                  "narration-language", "narration-mode", "pause", "pause-until"}
DEFAULT_STILL_SECONDS = 1.0
MAX_PAUSE_UNTIL = 600.0
AUDIO_CODECS = {
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100"],
    ".m4a": ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"],
    ".aac": ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"],
    ".ogg": ["-c:a", "libopus", "-b:a", "128k", "-ar", "48000"],
    ".opus": ["-c:a", "libopus", "-b:a", "128k", "-ar", "48000"],
    ".flac": ["-c:a", "flac"],
    ".wav": ["-c:a", "pcm_s16le"],
}
TARGETS = {  # x264 preset, crf, faststart
    "offline": ("medium", 16, False),
    "online": ("medium", 18, True),
    "self-hosted": ("slow", 23, True),
}
UNSUPPORTED: dict[str, str] = {}
# Emphasis as a prosody bundle that works on every engine: speed (rendered by
# the engine), pitch in semitones and gain in dB
EMPHASIS = {
    "none": (1.0, 0.0, 0.0),
    "moderate": (0.96, 0.75, 1.5),
    "strong": (0.92, 1.5, 3.0),
    "reduced": (1.06, -1.0, -4.0),
}


class BuildError(Exception):
    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.line = line


@dataclass
class Visual:
    kind: str  # "still" | "video" | "page"
    path: Path | None = None
    fit: str = "cover"
    position: str = "center"
    sync: str = "freeze"
    start: float = 0.0
    length: float = 0.0
    volume: float = 1.0
    has_audio: bool = False
    frames: int = 0
    motion: str | None = None  # pan/zoom animation of a still image
    viewport: tuple[int, int] | None = None  # an HTML scene's design size
    zoom: float | None = None  # how far a pan/zoom moves, if not motion.ZOOM / PAN_ZOOM
    render: Render | None = None  # a render block not drawn yet (planning without rendering)
    line: int = 0  # the script line it comes from
    focus: tuple[float, float] | None = None  # a focal point, fractions of the source (#63)
    windows: tuple | None = None  # pan/zoom windows given outright, (from, to) (#63)
    align: tuple[float, float] | None = None  # where a still crop sits, object-position fractions (from focus)

    @property
    def css_position(self) -> str:
        """Its position for a frame's CSS: a named one, or the alignment its focal point gives."""
        return f"{self.align[0] * 100:.4g}% {self.align[1] * 100:.4g}%" if self.align else self.position


@dataclass
class Layout:
    """How a scene's visuals share its time: seconds on screen in one pass, playback speed of each."""

    pieces: list[float]
    tempo: list[float]
    repeat: bool = False  # loop the one-pass sequence to fill the scene


@dataclass
class ParagraphTiming:
    """When a narration block (a paragraph or a recording) plays in its scene."""

    line: int
    start: float
    end: float
    estimated: bool = False  # its length is a guess from the text (a preview's EstimatingSynth)


@dataclass
class ScenePlan:
    index: int
    visuals: list[Visual]
    spec: FrameSpec  # slide, callouts, theme: shared by every visual of the scene
    audio: np.ndarray
    duration: float
    cues: list[Cue] = field(default_factory=list)
    layout: Layout = field(default_factory=lambda: Layout([], []))
    slides: list[SlideSpec] = field(default_factory=list)  # share the scene's time equally
    transition: tuple[str, float] | None = None  # incoming: Narakeet type, seconds
    marks: dict[str, float] = field(default_factory=dict)  # cue name -> seconds in, for HTML scenes
    paragraphs: list[ParagraphTiming] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)  # each paragraph as plain text, recordings transcribed (#58)
    overlays: list[OverlaySpec] = field(default_factory=list)  # web pages over this scene (#59)
    hide_overlays: bool = False  # (overlay: none): the video's overlays stay off this scene


@dataclass
class Layer:
    """An overlay page captured and ready to lay over the picture (#59)."""

    path: Path
    still: bool  # one PNG (the page never changed), else an FFV1 stream with alpha
    x: int = 0
    y: int = 0
    offset: float = 0.0  # seconds into the picture below when it starts
    enable: str | None = None  # an ffmpeg expression for when it shows


@dataclass
class BuildResult:
    video: Path  # the audio file, for an audio build
    poster: Path | None
    subtitle_files: list[Path]
    duration: float
    notes: list[str]
    transcript: Path | None = None  # <name>.transcript.txt, when asked for (#58)


class AudioTrack:
    """A scene's narration sequence: segments played back to back."""

    def __init__(self) -> None:
        self.chunks: list[np.ndarray] = []
        self.length = 0  # samples

    @property
    def seconds(self) -> float:
        return self.length / SR

    def add(self, samples: np.ndarray) -> None:
        self.chunks.append(samples.astype(np.float32, copy=False))
        self.length += len(samples)

    def silence(self, seconds: float) -> None:
        if seconds > 0:
            self.add(np.zeros(int(round(seconds * SR)), dtype=np.float32))

    def until(self, seconds: float) -> None:
        self.silence(seconds - self.seconds)

    def samples(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.float32)


def fit_length(samples: np.ndarray, n: int, *, tile: bool = False) -> np.ndarray:
    if len(samples) >= n:
        return samples[:n]
    if tile and len(samples):
        return np.tile(samples, -(-n // len(samples)))[:n]
    return np.concatenate([samples, np.zeros(n - len(samples), dtype=np.float32)])


LOUDNESS_TARGET = -23.0  # LUFS, the EBU R 128 programme level


def apply_gain(samples: np.ndarray, volume: float | str) -> np.ndarray:
    if volume == "normalized":
        # EBU R 128: bring each voice sample to the same integrated loudness;
        # clips too short to gate fall back to matching RMS
        measured = media.loudness(samples, SR)
        if measured is not None:
            return samples * (10 ** ((LOUDNESS_TARGET - measured) / 20))
        rms = float(np.sqrt(np.mean(samples**2))) if len(samples) else 0.0
        return samples * (10 ** (LOUDNESS_TARGET / 20) / rms) if rms > 1e-6 else samples
    return samples * (10 ** (float(volume) / 20)) if volume else samples


class Builder:
    def __init__(self, script: Script, synth: Synth, *, log: Callable[[str], None] | None = None,
                 jobs: int | None = None, regenerate: bool = False, measure_pages: bool = True,
                 scenes: Collection[int] | None = None, subtitles: str | None = None, transcript: bool = False,
                 local_network: bool = False):
        self.script = script
        self.measure_pages = measure_pages  # load HTML scenes to time them (the studio's previews don't)
        self.regenerate = regenerate  # make stored render outputs (render:web) again
        # Let captured pages reach this machine and private networks, not just public addresses (#93)
        self.local_network = local_network or network.allowed_by_environment()
        self.rendered: dict[str, int] = {}  # render blocks drawn afresh by this build, by renderer (#81)
        self.synth = synth
        # Encodes run in a thread pool alongside narration; x264 threads each one too
        self.jobs = max(1, jobs or int(os.environ.get("NARRATE_JOBS", 0)) or min(4, os.cpu_count() or 1))
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.notes: set[str] = set()
        try:
            self.settings: Settings = settings_from_header(script.header, script.warnings)
            if subtitles is not None:  # the build's choice, over the header's (#58), before any planning
                self.settings.subtitles = subtitles_with_mode(script.header.get("subtitles"), subtitles)
        except ConfigError as e:
            raise BuildError(str(e)) from None
        self.settings.transcript = self.settings.transcript or transcript
        try:
            self.voice_map = voicemap.load(script.base_dir)
        except voicemap.VoiceMapError as e:
            raise BuildError(str(e)) from None
        self.mode = "normal"  # narration-mode persists across scenes
        try:
            self.transition = parse_transition(self.settings.transition) if self.settings.transition else None
        except ConfigError as e:
            raise BuildError(f"header transition: {e}") from None
        self._voices: dict[tuple[str | None, str | None], Voice] = {}
        self.selected = self._selection(scenes)  # render only these scenes (None: all of them)
        self._frames: FrameRenderer | None = None  # the build's Chromium, while frames render
        self._probe: FrameRenderer | None = None  # one started just to measure pages, when there isn't

    def _selection(self, scenes: Collection[int] | None) -> set[int] | None:
        """The scenes to render, widened to whole merge groups, which share one narration."""
        if scenes is None:
            return None
        wanted = set(scenes)
        known = {scene.index for scene in self.script.scenes}
        if missing := sorted(wanted - known):
            raise BuildError(f"no scene {', '.join(map(str, missing))}: the script has {len(known)}")
        selected: set[int] = set()
        for group in merge_groups(self.script.scenes):
            indexes = {scene.index for scene in group}
            if indexes & wanted:
                selected |= indexes
        if selected != wanted:
            self.notes.add("scenes merged into one narration are rendered together")
        return selected

    def close(self) -> None:
        """Stop the Chromium started to measure HTML scenes, if any."""
        if self._probe is not None:
            self._probe.__exit__(None, None, None)
            self._probe = None
            shutil.rmtree(self._probe_dir, ignore_errors=True)

    def _pages(self) -> FrameRenderer:
        """A Chromium to load HTML scenes in: the build's own, or one started for planning."""
        if self._frames is not None:
            return self._frames
        if self._probe is None:
            self._probe_dir = Path(tempfile.mkdtemp(prefix="narrate-pages-"))
            self._probe = FrameRenderer(self.settings.width, self.settings.height, self._probe_dir,
                                        self.local_network).__enter__()
        return self._probe

    # -- helpers -------------------------------------------------------------

    def _path(self, name: str, line: int) -> Path:
        path = (self.script.base_dir / name).resolve()
        if not path.is_file():
            raise BuildError(f"file not found: {name}", line)
        return path

    def _voice(self, name: str | None, lang: str | None) -> Voice:
        key = (name, lang)
        if key not in self._voices:
            mapped = voicemap.apply(self.voice_map, name, self.synth.name)
            self._voices[key] = self.synth.resolve(mapped, lang.lower() if lang else None, self.notes)
        return self._voices[key]

    def _speak(self, text: str, voice: Voice, speed: float, pitch: float = 1.0) -> np.ndarray:
        """Synthesise at the nearest speed the engine supports, stretch the remainder, shift pitch."""
        lo, hi = self.synth.speed_range
        native = min(hi, max(lo, speed))
        audio = self.synth.synth(text, voice, native, self.notes)
        if not audio.any():  # silence (a preview's estimate): only its length matters
            return np.zeros(int(len(audio) * native / speed), dtype=np.float32)
        if abs(speed / native - 1.0) > 1e-3 and len(audio):
            audio = media.time_stretch(audio, SR, speed / native)
        if abs(pitch - 1.0) > 1e-3 and len(audio):
            audio = media.pitch_shift(audio, SR, pitch)
        return audio

    def _theme(self, value: str, line: int) -> str:
        if value.lower().endswith(".css"):
            return str(self._path(value, line))
        if value.lower() not in ("default", "light"):
            raise BuildError(f"unknown theme {value!r}", line)
        return value.lower()

    # -- planning ------------------------------------------------------------

    def plans(self, scenes: list[Scene], workdir: Path):
        """Scene plans in order, merging scenes that share one narration (narration-mode: merge)."""
        for group in merge_groups(scenes):
            if len(group) == 1:
                yield self.plan_scene(group[0], workdir)
            else:
                yield from self._plan_merged(group, workdir)

    def _plan_merged(self, group: list[Scene], workdir: Path):
        """One continuous narration across the group; the scenes share its time equally (spec §4.4)."""
        voiced = self.plan_scene(Scene(group[0].index, group[0].line,
                                       [b for scene in group for b in scene.blocks if _is_narration(b)]), workdir)
        # Whole-frame shares (800 samples each), so the narration slices back together exactly
        shares = allocate_frames(max(len(group), round(voiced.duration * FPS)), [1.0] * len(group))
        at = 0
        for scene, frames in zip(group, shares):
            plan = self.plan_scene(Scene(scene.index, scene.line, [b for b in scene.blocks if not _is_narration(b)]),
                                   workdir, force_duration=frames / FPS)
            lo, hi = at * SR // FPS, (at + frames) * SR // FPS
            plan.audio = fit_length(plan.audio, hi - lo) + fit_length(voiced.audio[lo:hi], hi - lo)
            if scene is group[0]:  # one narration across the group: its words go with its first scene
                plan.spoken = voiced.spoken + plan.spoken
            if not plan.cues:
                start, end = at / FPS, (at + frames) / FPS
                plan.cues = [Cue(max(c.start, start) - start, min(c.end, end) - start, c.text)
                             for c in voiced.cues if min(c.end, end) > max(c.start, start)]
            at += frames
            yield plan

    def plan_scene(self, scene, workdir: Path, force_duration: float | None = None) -> ScenePlan:
        s = self.settings
        voice_name, lang = s.voice, s.narration_language
        speed, volume, pitch = s.voice_speed, s.voice_volume, s.voice_pitch
        emphasis = "none"
        image_size: str | None = None
        image_position: str | None = None
        duration: float | None = None
        transition = self.transition if scene.index > 1 else None
        spec = FrameSpec(canvas=s.canvas, theme=self._theme(s.theme, 0) if s.theme else "default")
        track = AudioTrack()
        sources: list[Media] = []
        slides: list[Slide] = []
        quotes: list[str] = []
        narration_spans: list[tuple[float, float, str]] = []
        recordings: list[tuple[float, Path, str | None]] = []  # start in the track, file, language hint
        spoken: list[tuple[float, str]] = []  # (start, plain text) of each narration block, for the transcript
        marks: dict[str, float] = {}  # "1", "2", … for each narration block, and SSML mark names
        paragraphs: list[ParagraphTiming] = []
        overlays: list[OverlaySpec] = []
        hide_overlays = False

        for block in scene.blocks:
            try:
                if isinstance(block, Directive):
                    key, value = block.key, block.value
                    if key == "voice":
                        voice_name = str(value)
                    elif key == "voice-speed":
                        speed = parse_speed(value)
                    elif key == "voice-volume":
                        volume = parse_volume(value)
                    elif key == "voice-pitch":
                        pitch = parse_pitch(value)
                    elif key == "voice-emphasis":
                        emphasis = _choice(value, set(EMPHASIS), "voice-emphasis")
                    elif key == "narration-language":
                        lang = None if str(value).lower() == "default" else str(value)
                    elif key == "narration-mode":
                        mode = str(value).lower()
                        if mode not in PARAGRAPH_PAUSE:
                            raise ConfigError(f"invalid narration-mode {value!r}")
                        self.mode = mode
                    elif key == "pause":
                        track.silence(parse_number(value, "pause"))
                    elif key == "pause-until":
                        track.until(min(parse_time(value), MAX_PAUSE_UNTIL))
                    elif key == "duration":
                        duration = parse_number(value, "duration")
                    elif key == "canvas":
                        spec.canvas = parse_color(value, "canvas")
                    elif key == "theme":
                        spec.theme = self._theme(str(value), block.line)
                    elif key == "font-size":
                        spec.font_size = int(parse_number(value, "font-size"))
                    elif key == "image-size":
                        image_size = _choice(value, SIZES | ANIMATED_SIZES, "image-size")
                    elif key == "image-position":
                        image_position = _choice(value, POSITIONS, "image-position")
                    elif key == "overlay":
                        overlay = parse_overlay(value)
                        if overlay is None:
                            hide_overlays = True
                        else:  # its file is checked when it's captured: planning alone doesn't need it
                            overlay.line = block.line
                            overlays.append(overlay)
                    elif key == "callout":
                        if not isinstance(value, dict):
                            raise ConfigError("callout needs properties such as cx/cy")
                        spec.callouts.append(dict(value))
                    elif key == "transition":
                        if scene.index == 1:
                            self.script.warnings.append(f"line {block.line}: no transition into the first scene; ignored")
                        else:
                            transition = parse_transition(value, self.transition[0] if self.transition else "crossfade")
                    elif key in UNSUPPORTED:
                        self.notes.add(UNSUPPORTED[key])
                elif isinstance(block, Media):
                    if block.kind == "audio":
                        path = self._path(block.path, block.line)
                        marks[str(sum(k.isdigit() for k in marks) + 1)] = track.seconds
                        recordings.append((track.seconds, path, lang))
                        start = track.seconds
                        track.add(media.decode_audio(path, SR))
                        paragraphs.append(ParagraphTiming(block.line, start, track.seconds))
                    else:
                        if block.size is None and image_size and block.kind == "image":
                            block.size = image_size
                        if block.position is None and image_position:
                            block.position = image_position
                        sources.append(block)
                elif isinstance(block, Slide):
                    slides.append(block)
                elif isinstance(block, Quote):
                    quotes.append(block.text)
                elif isinstance(block, Narration):
                    voice = self._voice(voice_name, lang)
                    if block.ssml:
                        runs = ssml_runs(block.text, voice.lang or "en", self.notes)
                        if runs is None:
                            self.script.warnings.append(f"line {block.line}: SSML is not well-formed XML; read as plain text")
                            runs = [Run(ssml_text(block.text))]
                    else:
                        runs = speech_runs(block.text, voice.lang or "en", self.notes)
                    start = track.seconds
                    estimates = getattr(self.synth, "estimates", 0)
                    block_cue = str(sum(k.isdigit() for k in marks) + 1)
                    for run in runs:
                        track.silence(run.pause)
                        marks.setdefault(block_cue, track.seconds)  # where the voice starts
                        for mark in run.marks:
                            marks[mark] = track.seconds
                        if not run.text:
                            continue
                        rate, semitones, gain = EMPHASIS[run.emphasis if run.emphasis != "none" else emphasis]
                        audio = self._speak(run.text, voice, speed * rate * run.rate,
                                            pitch * 2 ** (semitones / 12) * run.pitch)
                        track.add(apply_gain(apply_gain(audio, volume), gain + run.gain))
                    marks.setdefault(block_cue, start)
                    paragraphs.append(ParagraphTiming(block.line, marks[block_cue], track.seconds,
                                                      getattr(self.synth, "estimates", 0) > estimates))
                    if runs:
                        caption = subtitle_text(ssml_text(block.text) if block.ssml else block.text)
                        narration_spans.append((start, track.seconds, caption))
                        spoken.append((start, caption))
                        track.silence(PARAGRAPH_PAUSE[self.mode])
            except ConfigError as e:
                raise BuildError(str(e), block.line) from None


        # An HTML scene with nothing else to time the scene lasts as long as its animations
        natural = track.seconds <= 0 and duration is None and force_duration is None
        visuals = [self._visual(m, workdir, natural) for m in sources]
        for v, m in zip(visuals, sources):
            v.line = m.line
            self._frame_freely(v, m)
        if force_duration is not None:
            duration = force_duration
        narration = track.samples()
        d, layout = layout_visuals(visuals, track.seconds, duration)

        n = int(round(d * SR))
        sync = next((v.sync for v in visuals if v.kind == "video"), None)
        audio = fit_length(narration, n, tile=sync == "loop-audio")
        if any(v.kind == "video" and v.has_audio and v.volume for v in visuals):
            audio = audio + self._clip_audio(visuals, layout, n)

        captioned = s.subtitles.mode != "none" and not quotes
        # Recordings are transcribed once, for their captions and for the transcript alike
        heard = self._recording_phrases(recordings, captioned) if recordings and (captioned or s.transcript) else None
        if quotes:
            texts = [q for q in quotes if q]
            cues = subtitles.spread(0.0, d, texts) if texts else []
        elif captioned:
            spans = narration_spans + [(start + a, start + b, text) for (start, _, _), phrases in zip(recordings, heard or [])
                                       for a, b, text in stt_captions(phrases, s.subtitles.break_chars)]
            cues = sorted((c for start, end, text in spans
                           for c in subtitles.narration_cues(start, end, text, s.subtitles.break_chars)),
                          key=lambda c: c.start)
        else:
            cues = []
        if heard:  # each recording is one paragraph of the transcript, where it plays
            spoken += [(start, " ".join(w for words in phrases for _, _, w in words))
                       for (start, _, _), phrases in zip(recordings, heard) if phrases]
        plan = ScenePlan(scene.index, visuals, spec, audio, d, cues, layout,
                         [SlideSpec(lang=sl.lang, text=sl.text) for sl in slides], transition, marks, paragraphs,
                         [text for _, text in sorted(spoken, key=lambda p: p[0]) if text])
        plan.overlays, plan.hide_overlays = overlays, hide_overlays
        return plan

    def _recording_phrases(self, recordings: list[tuple[float, Path, str | None]],
                           captioned: bool) -> list[list] | None:
        """The phrases spoken in each of a scene's recordings (times within the recording), or None when
        speech-to-text isn't available. Captions need it, so a failure there stops the build; a transcript
        alone notes what it couldn't hear."""
        from . import stt

        heard = []
        for _, path, lang in recordings:
            hint = lang.split("-")[0].lower() if lang else None
            try:
                heard.append(stt.transcribe(path, hint))
            except ImportError:  # faster-whisper is loaded on first use
                self.notes.add("subtitles for recorded audio (needs faster-whisper)" if captioned
                               else "transcript of recorded audio (needs faster-whisper)")
                return None
            except Exception as e:
                if captioned:
                    raise
                self.notes.add(f"transcript: couldn't transcribe {path.name} ({e})")
                heard.append([])
        return heard

    def _render_blocks(self, frames: FrameRenderer) -> None:
        """Render every render block, with its scene's theme and canvas, and point it at the output."""
        s = self.settings
        for scene in self.script.scenes:
            if self.selected is not None and scene.index not in self.selected:
                continue  # not rendered, so nothing to draw
            theme, canvas = self._theme(s.theme, 0) if s.theme else "default", s.canvas
            for block in scene.blocks:
                try:
                    if isinstance(block, Directive) and block.key == "theme":
                        theme = self._theme(str(block.value), block.line)
                    elif isinstance(block, Directive) and block.key == "canvas":
                        canvas = parse_color(block.value, "canvas")
                    elif isinstance(block, Media) and block.render and not block.path:
                        block.kind = render.renderer(block.render.name).kind
                        ctx = render.Context(s.width, s.height, theme, canvas, frames,
                                             self.script.base_dir, self.regenerate)
                        kept = render.existing(block.render, ctx) is not None
                        block.path = str(render.output(block, ctx))
                        if not kept or (self.regenerate and render.renderer(block.render.name).stored):
                            self.rendered[block.render.name] = self.rendered.get(block.render.name, 0) + 1
                except (ConfigError, render.RenderError) as e:
                    raise BuildError(str(e), getattr(e, "line", None) or block.line) from None

    def _visual(self, m: Media, workdir: Path, natural: bool = False) -> Visual:
        if m.render and not m.path:
            # Audio builds and the speech prefetch plan timing only; they don't render
            return Visual("still", None, m.size or self.settings.asset_resize, m.position or "center", render=m.render)
        path = self._path(m.path, m.line)
        size = m.size or self.settings.asset_resize
        animation = None
        if size in ANIMATED_SIZES:
            animation, size = size, "cover"
        position = m.position or "center"
        if m.kind == "html":
            if animation:
                self.notes.add(f"{animation} on an HTML scene (ignored; pan/zoom applies to images)")
            try:
                viewport = parse_size(m.viewport) if m.viewport else (self.settings.width, self.settings.height)
            except ConfigError as e:
                raise BuildError(f"viewport: {e}", m.line) from None
            length = pages.natural_length(self._pages(), path, viewport) if natural and self.measure_pages else 0.0
            return Visual("page", path, size, position, length=length or DEFAULT_STILL_SECONDS, viewport=viewport)
        if m.kind == "image" and not media.is_animated_gif(path):
            return Visual("still", path, size, position, motion=animation,
                          zoom=m.zoom or self.settings.image_zoom if animation else None)
        if animation:
            self.notes.add(f"{animation} on a video clip (ignored; pan/zoom applies to images)")
        info = media.probe(path)
        if m.segment and m.segment[1] is None:
            at = {"start": 0.0, "end": max(0.0, info.duration - 0.1)}.get(m.segment[0])
            frame = workdir / f"still-{m.line}.png"
            media.extract_frame(path, parse_time(m.segment[0]) if at is None else at, frame)
            return Visual("still", frame, size, position)
        start, end = 0.0, info.duration
        if m.segment:
            a, b = m.segment
            start = 0.0 if a == "start" else parse_time(a)
            end = info.duration if b == "end" else min(info.duration, parse_time(b))
        if end <= start:
            raise BuildError(f"empty video segment in {m.path}", m.line)
        default_sync = "loop" if path.suffix.lower() == ".gif" else "freeze"
        return Visual(
            "video", path, size, position, sync=m.sync or default_sync, start=start,
            length=end - start, volume=1.0 if m.volume is None else m.volume,
            has_audio=info.has_audio,
        )

    def _frame_freely(self, v: Visual, m: Media) -> None:
        """A focal point and pan/zoom windows (#63): carried to the visual, with the crop alignment its
        source's size gives."""
        if m.focus is None and m.windows is None:
            return
        w, h = self.settings.width, self.settings.height
        v.focus, v.windows = m.focus, m.windows
        size = None
        if v.kind == "still" and v.path is not None:
            from PIL import Image

            with Image.open(v.path) as im:
                size = im.size
        elif v.kind == "video" and v.path is not None:
            info = media.probe(v.path)
            size = (info.width, info.height) if info.width and info.height else None
        elif v.kind == "page":
            size = v.viewport
        if m.windows is not None:
            if v.kind != "still" or v.render is not None:
                self.notes.add("from/to windows on a clip, page or render block (ignored; they apply to pictures)")
                v.windows = None
            else:
                v.motion = v.motion or "custom"
                if size and any(motion.window_rect(win, *size, w, h)[1] for win in m.windows if win):
                    self.notes.add(f"line {m.line}: a window reached past the picture's edge, so it was moved in")
        if m.focus is not None and size:
            v.align = motion.alignment(m.focus, size[0], size[1], w, h, v.fit)

    def _clip_audio(self, visuals: list[Visual], layout: Layout, n: int) -> np.ndarray:
        """The soundtracks of the scene's clips, each placed where its clip plays."""
        span = int(round(sum(layout.pieces) * SR)) if layout.repeat else n
        mix = np.zeros(span, dtype=np.float32)
        offset = 0.0
        for v, piece, tempo in zip(visuals, layout.pieces, layout.tempo):
            if v.kind == "video" and v.has_audio and v.volume:
                clip = media.decode_audio(v.path, SR, start=v.start, duration=v.length, tempo=tempo)
                at = int(round(offset * SR))
                length = min(int(round(piece * SR)), span - at)
                if length > 0:
                    mix[at:at + length] += fit_length(clip, length) * v.volume
            offset += piece
        return fit_length(mix, n, tile=layout.repeat)

    # -- rendering -----------------------------------------------------------

    def build(self, out: Path, *, workdir: Path | None = None) -> BuildResult:
        own_workdir = workdir is None
        work = Path(tempfile.mkdtemp(prefix="narrate-")) if own_workdir else workdir.resolve()
        work.mkdir(parents=True, exist_ok=True)
        try:
            return self._build(out, work)
        finally:
            self.close()
            if own_workdir:
                shutil.rmtree(work, ignore_errors=True)

    def _build(self, out: Path, work: Path) -> BuildResult:
        if not self.script.scenes:
            raise BuildError("the script has no scenes")
        track = self._music_track()  # before any speech is synthesised or any scene captured (#90)
        with self._speech(work):
            return self._render_all(out, work, track)

    @contextmanager
    def _speech(self, work: Path):
        """Synthesise speech ahead in the background when the engine can work concurrently."""
        backend = self.synth
        if getattr(backend, "concurrency", 1) > 1:
            self.synth = self._prefetch_speech(work)
        try:
            yield
        finally:
            if isinstance(self.synth, Prefetching):
                self.synth.close()
            self.synth = backend

    def build_audio(self, out: Path, *, workdir: Path | None = None) -> BuildResult:
        """Narration (with recorded audio, clip soundtracks and music) as an audio file, no video."""
        codec = AUDIO_CODECS.get(out.suffix.lower())
        if codec is None:
            raise BuildError(f"unsupported audio format {out.suffix!r} (use {', '.join(sorted(AUDIO_CODECS))})")
        if not self.script.scenes:
            raise BuildError("the script has no scenes")
        track = self._music_track()  # before any speech is synthesised (#90)
        own_workdir = workdir is None
        work = Path(tempfile.mkdtemp(prefix="narrate-")) if own_workdir else workdir
        work.mkdir(parents=True, exist_ok=True)
        try:
            with self._speech(work):
                parts: list[np.ndarray] = []
                cues: list[Cue] = []
                scene_samples: list[int] = []
                spoken: list[list[str]] = []
                for plan in self.plans(self.script.scenes, work):
                    spoken.append(plan.spoken)
                    if plan.visuals or plan.slides or plan.spec.callouts:
                        self.notes.add("images, videos, slides and callouts (ignored in audio output)")
                    n = int(round(plan.duration * SR))
                    offset = sum(scene_samples) / SR
                    cues += [Cue(c.start + offset, c.end + offset, c.text) for c in plan.cues]
                    parts.append(fit_length(plan.audio, n))
                    scene_samples.append(n)
            audio = np.concatenate(parts)
            if track is not None:
                audio = audio + self._music(track, len(audio), scene_samples)
            if self.settings.loudness is not None:
                audio = media.normalise_loudness(audio, SR, self.settings.loudness)
            peak = float(np.abs(audio).max()) if len(audio) else 0.0
            if peak > 0.99:
                audio = audio * (0.99 / peak)
            wav = work / "audio.wav"
            _write_wav(wav, self._fade(audio))
            out.parent.mkdir(parents=True, exist_ok=True)
            media.run(["-i", str(wav), *codec, str(out)])
            files: list[Path] = []
            if self.settings.subtitles.mode != "none" and cues:
                srt, vtt = out.with_suffix(".srt"), out.with_suffix(".vtt")
                srt.write_text(subtitles.to_srt(cues), encoding="utf-8")
                vtt.write_text(subtitles.to_vtt(cues), encoding="utf-8")
                files = [srt, vtt]
            transcript = self._write_transcript(out, spoken)
            return BuildResult(out, None, files, len(audio) / SR, sorted(self.notes), transcript)
        finally:
            self.close()
            if own_workdir:
                shutil.rmtree(work, ignore_errors=True)

    def _render_all(self, out: Path, work: Path, track: np.ndarray | None) -> BuildResult:
        s = self.settings
        scenes = self.script.scenes
        if self.selected is not None:
            # Part of the video: the header's fades only where the video itself starts or ends
            if scenes[0].index not in self.selected:
                s.fade_in = 0.0
            if scenes[-1].index not in self.selected:
                s.fade_out = 0.0
            if s.loudness is not None:
                self.notes.add("loudness is normalised over the scenes rendered, so it can differ slightly "
                               "from the whole video's")
        count = len(scenes) if self.selected is None else len(self.selected)
        plans: list[ScenePlan] = []
        pending: list[list[Future]] = []
        scene_frames: list[int] = []
        audio_parts: list[np.ndarray] = []
        cues: list[Cue] = []
        scene_samples: list[int] = []
        whole_samples: list[int] = []  # every scene's, rendered or not, to place the music
        kept: list[tuple[int, int]] = []  # where the rendered scenes' samples sit in the whole video
        frame = 0  # in the whole video
        out_frame = 0  # in what's rendered
        placed: list[tuple[ScenePlan, int, int, int]] = []  # each scene: where in the output, how long, where in the whole
        # Scene k's segments encode in the pool while scene k+1 is narrated
        with ThreadPoolExecutor(self.jobs) as pool, FrameRenderer(s.width, s.height, work,
                                                                  self.local_network) as renderer:
            self._frames = renderer
            self._render_blocks(renderer)
            for plan, rendered in self._planned(work):
                start_f = frame
                end_f = max(start_f + 1, int(round(start_f + plan.duration * FPS)))
                frame = end_f
                lo, hi = int(round(start_f * SR / FPS)), int(round(end_f * SR / FPS))
                whole_samples.append(hi - lo)
                if not rendered:
                    continue
                if not plans and plan.transition and self.selected is not None:
                    self.notes.add(f"no transition into scene {plan.index}: nothing comes before it in this render")
                    plan.transition = None
                self.log(f"scene {len(plans) + 1}/{count}")
                plans.append(plan)
                frames = end_f - start_f
                audio_parts.append(fit_length(plan.audio, hi - lo))
                scene_samples.append(hi - lo)
                kept.append((lo, hi))
                offset = out_frame / FPS
                cues += [Cue(c.start + offset, min(c.end, frames / FPS) + offset, c.text) for c in plan.cues]
                pending.append(self._render_scene(plan, frames, renderer, work, pool.submit))
                scene_frames.append(frames)
                placed.append((plan, out_frame, frames, start_f))
                out_frame += frames
            video_layers = self._video_overlays(placed, renderer, work)
            scene_segments = [[f.result() for f in futures] for futures in pending]
            self._frames = None
        self.notes.update(network.note(page, f.target) for page, failed in renderer.failures.items()
                          for f in failed if f.local)
        # A proxy that refused, or can't be used, is said once for each reason, whatever it kept out
        self.notes.update(network.proxy_note(f.why) for failed in renderer.failures.values() for f in failed if f.why)

        total = out_frame / FPS
        audio = np.concatenate(audio_parts)
        if track is not None:
            if self.selected is None:
                audio = audio + self._music(track, len(audio), scene_samples)
            else:  # the music as it plays under these scenes in the whole video
                whole = self._music(track, sum(whole_samples), whole_samples)
                audio = audio + np.concatenate([whole[lo:hi] for lo, hi in kept])
        if s.loudness is not None:
            audio = media.normalise_loudness(audio, SR, s.loudness)
        peak = float(np.abs(audio).max()) if len(audio) else 0.0
        if peak > 0.99:
            audio = audio * (0.99 / peak)
        wav = work / "audio.wav"
        _write_wav(wav, self._fade(audio))

        self.log("muxing")
        out.parent.mkdir(parents=True, exist_ok=True)
        transitions = [plan.transition for plan in plans]
        subtitle_files = self._mux(out, scene_segments, scene_frames, transitions, wav, cues, work, video_layers)
        poster = out.with_suffix(".jpg")
        media.extract_frame(out, _poster_time(s.poster_time, total), poster)
        transcript = self._write_transcript(out, [plan.spoken for plan in plans])
        return BuildResult(out, poster, subtitle_files, total, sorted(self.notes), transcript)

    def _video_overlays(self, placed: list[tuple[ScenePlan, int, int, int]], renderer: FrameRenderer,
                        work: Path) -> list[Layer]:
        """The header's overlays (#59), captured on the video's own clock over the scenes they cover, so a
        ticker scrolls on through cuts and transitions. A scene with (overlay: none) hides them."""
        layers: list[Layer] = []
        if not self.settings.overlays or not placed:
            return layers
        end = placed[-1][3] + placed[-1][2]  # where what's rendered ends, in the whole video
        scenes = [{"index": plan.index, "start": round(g / FPS, 4), "duration": round(n / FPS, 4)}
                  for plan, _, n, g in placed]
        for k, spec in enumerate(self.settings.overlays):
            chosen = [p for p in placed if spec.scenes is None or spec.scenes[0] <= p[0].index <= spec.scenes[1]]
            if not chosen:
                continue
            lo, hi = chosen[0][1], chosen[-1][1] + chosen[-1][2]  # in the output
            # The page's clock is the whole video's: a build of some scenes picks it up where they fall
            layer = self._capture_overlay(spec, hi - lo, chosen[0][3] / FPS, renderer, work / f"video-overlay{k}",
                                          pages.cue_script(end / FPS, {}, spec.params, scenes))
            layer.offset = lo / FPS
            hidden = [f"gte(t,{o / FPS:.4f})*lt(t,{(o + n) / FPS:.4f})" for plan, o, n, _ in chosen if plan.hide_overlays]
            conditions = ([f"gte(t,{lo / FPS:.4f})*lt(t,{hi / FPS:.4f})"] if layer.still else []) + \
                         ([f"not({'+'.join(hidden)})"] if hidden else [])
            layer.enable = "*".join(conditions) or None
            layers.append(layer)
        return layers

    def _write_transcript(self, out: Path, scenes: list[list[str]]) -> Path | None:
        """What's spoken, as plain text beside the output (#58): a line per paragraph, a blank line
        between scenes. Not <name>.txt, which a .txt script built in its own folder would be."""
        if not self.settings.transcript:
            return None
        path = out.with_name(f"{out.stem}.transcript.txt")
        blocks = ["\n".join(paragraphs) for paragraphs in scenes if paragraphs]
        path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
        return path

    def _planned(self, work: Path):
        """Every scene's plan, and whether it's rendered.

        Scenes outside the selection are only timed, so that those rendered fall
        where they do in the whole video (the music under them, say). They're
        timed from speech already cached, estimated where it isn't, so nothing is
        synthesised for them."""
        if self.selected is None:
            for plan in self.plans(self.script.scenes, work):
                yield plan, True
            return
        real = self.synth
        estimator = EstimatingSynth(real)
        untimed: list[int] = []
        for group in merge_groups(self.script.scenes):
            rendered = group[0].index in self.selected
            self.synth = real if rendered else estimator
            try:
                plans = list(self.plans(group, work))
            except BuildError:
                if rendered:
                    raise
                untimed += [scene.index for scene in group]  # its problem is for a build of that scene
                continue
            finally:
                self.synth = real
            for plan in plans:
                yield plan, rendered
        if self.settings.background and (estimator.estimates or untimed):
            self.notes.add("the music is placed by estimated timing of scenes not rendered "
                           f"({'their speech is not synthesised yet' if estimator.estimates else 'some have problems'})")

    def _render_scene(self, plan: ScenePlan, frames: int, renderer: FrameRenderer | None, work: Path,
                      submit: Callable[..., Future] | None = None) -> list[Future]:
        """Encode a scene as segments totalling exactly `frames` frames, its overlays (#59) laid on top.

        A scene's overlay pages are captured here (Chromium stays on this thread), then laid over the
        scene's joined picture in one more pass, so they run unbroken across its pictures and slides."""
        submit = submit or _run_now
        segments = self._render_picture(plan, frames, renderer, work, submit)
        if not plan.overlays:
            return segments
        base = work / f"scene-{plan.index:03d}"
        layers = [self._capture_overlay(spec, frames, 0.0, renderer, base.with_name(f"{base.name}-overlay{k}"),
                                        pages.cue_script(frames / FPS, plan.marks, spec.params))
                  for k, spec in enumerate(plan.overlays)]

        def lay_over() -> Path:
            # Queued after its pieces, so waiting here can't starve the pool
            wait(segments)
            under = _join([f.result() for f in segments], base.with_name(f"{base.name}-under.mp4"))
            inputs, graph, label = _layer_graph("0:v", layers, 1)
            over = base.with_name(f"{base.name}-over.mp4")
            media.run(["-i", str(under), *inputs, "-filter_complex", graph.lstrip(";"), "-map", f"[{label}]",
                       "-frames:v", str(frames), "-an", *_SEGMENT_CODEC, str(over)])
            return over

        return [submit(lay_over)]

    def _capture_overlay(self, spec: OverlaySpec, count: int, start: float, renderer: FrameRenderer, out: Path,
                         init_script: str) -> Layer:
        """Capture an overlay page, keeping its transparency: one PNG if it never changes, else a stream.

        The page is laid out at the video's size, or at its `viewport`, drawn sharply at the video's size
        and centred. `start` is the page's time at the first frame."""
        w, h = self.settings.width, self.settings.height
        path = self._path(spec.file, spec.line)
        vw, vh = spec.viewport or (w, h)
        scale = min(w / vw, h / vh)
        x, y = (w - round(vw * scale)) // 2, (h - round(vh * scale)) // 2
        first: list[bytes] = []
        captured: list[int] = []

        def images():
            with closing(pages.capture(renderer, path, (vw, vh), scale, count, FPS, captured.append, init_script,
                                       transparent=True, start=start)) as frames:
                for k, png in enumerate(frames):
                    if not k:
                        first.append(png)
                    yield png

        stream = out.with_suffix(".mkv")
        media.encode_images(images(), FPS, ["-frames:v", str(count), "-c:v", "ffv1", "-pix_fmt", "bgra"], stream)
        if captured and captured[0] <= 1:  # it never changed: one picture does
            still = out.with_suffix(".png")
            still.write_bytes(first[0])
            stream.unlink(missing_ok=True)
            return Layer(still, True, x, y)
        self.log(f"  {path.name}: overlay captured over {count} frames")
        return Layer(stream, False, x, y)

    def _render_picture(self, plan: ScenePlan, frames: int, renderer: FrameRenderer | None, work: Path,
                        submit: Callable[..., Future] | None = None) -> list[Future]:
        """Encode a scene's pictures, clips, pages and slides as segments totalling exactly `frames` frames.

        Slides share the scene's time equally, independently of the visuals.
        Over images they are composed into the frame (so theme backdrops
        apply), splitting an image where the slide changes; over clips, and
        over a looping sequence, they are time-windowed overlays.

        Frames are rendered here (Chromium stays on this thread); encodes go to
        `submit` and come back as futures of the scene's segments.
        """
        submit = submit or _run_now
        base = work / f"scene-{plan.index:03d}"
        slides: list[SlideSpec | None] = list(plan.slides) or [None]
        bounds = list(accumulate(allocate_frames(frames, [1.0] * len(slides)), initial=0))

        def spec(**changes) -> FrameSpec:
            return FrameSpec(**{**plan.spec.__dict__, **changes})

        def name(suffix: str) -> Path:
            return base.with_name(f"{base.name}-{suffix}")

        if not plan.visuals:
            return [submit(_encode_still, renderer.render(spec(slide=slide), name(f"s{j}.png")),
                           bounds[j + 1] - bounds[j], name(f"s{j}.mp4"))
                    for j, slide in enumerate(slides) if bounds[j + 1] > bounds[j]]

        layout = plan.layout
        cache: dict[bool, list[tuple[Path, float, float]]] = {}

        def overlays(backdrop: bool = False) -> list[tuple[Path, float, float]]:
            """Slides and callouts as time-windowed overlays over the whole scene."""
            if backdrop not in cache:
                cache[backdrop] = [
                    (renderer.render(spec(slide=slide, over_video=True, backdrop=backdrop),
                                     name(f"overlay{j}{'b' if backdrop else ''}.png")),
                     bounds[j] / FPS, bounds[j + 1] / FPS)
                    for j, slide in enumerate(slides)
                ] if plan.slides or plan.spec.callouts else []
            return cache[backdrop]

        if layout.repeat:
            # One pass at natural length, looped from disk afterwards (the loop
            # filter would hold every frame in memory); slides and callouts go
            # on after looping so they follow the scene's time, not each pass
            counts = [max(1, round(p * FPS)) for p in layout.pieces]
        else:
            counts = allocate_frames(frames, layout.pieces)
        segments: list[Future] = []
        at = 0
        for i, (v, count, tempo) in enumerate(zip(plan.visuals, counts, layout.tempo)):
            if count <= 0:
                continue
            lo, hi = at, at + count
            at = hi
            if v.kind == "video":
                windows = [] if layout.repeat else _windows(overlays(), lo, hi)
                segments.append(submit(self._encode_video, v, count, tempo, windows, plan.spec.canvas,
                                       name(f"{i}.mp4")))
                continue
            if v.kind == "page":
                # Captured here, since Chromium stays on this thread, and streamed to ffmpeg as it goes
                windows = [] if layout.repeat else _windows(overlays(), lo, hi)
                # Cues count from the page's own start, which may be negative: a negative CSS
                # delay starts an animation part-way through
                cues = {k: t - lo / FPS for k, t in plan.marks.items()}
                segments.append(_run_now(self._encode_page, v, count, windows, plan.spec.canvas, renderer,
                                         name(f"{i}.mp4"), pages.cue_script(count / FPS, cues)))
                continue
            if v.motion:
                # One continuous animation, with slides overlaid rather than splitting it
                windows = [] if layout.repeat else _windows(overlays(backdrop=True), lo, hi)
                segments.append(submit(self._encode_motion, v, count, windows, name(f"{i}.mp4")))
                continue
            image = {"image": v.path, "fit": v.fit, "position": v.css_position}
            if layout.repeat:
                png = renderer.render(spec(**image, slide=None, callouts=[]), name(f"{i}.png"))
                segments.append(submit(_encode_still, png, count, name(f"{i}.mp4")))
                continue
            for j, slide in enumerate(slides):
                a, b = max(lo, bounds[j]), min(hi, bounds[j + 1])
                if b > a:
                    png = renderer.render(spec(**image, slide=slide), name(f"{i}-{j}.png"))
                    segments.append(submit(_encode_still, png, b - a, name(f"{i}-{j}.mp4")))
        if not layout.repeat:
            return segments

        final_overlays = overlays()  # rendered now: Chromium must not run on a pool thread

        def loop_pass() -> Path:
            # Queued after its pieces, so waiting here can't starve the pool
            wait(segments)
            parts = [f.result() for f in segments]
            once = parts[0]
            if len(parts) > 1:
                once = name("once.mp4")
                listing = name("once.txt")
                listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
                media.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(once)])
            looped = base.with_suffix(".mp4")
            inputs, graph = _overlay_graph("[0:v]null", final_overlays)
            media.run(["-stream_loop", "-1", "-i", str(once), *inputs, "-filter_complex", graph,
                       "-map", "[v]", "-frames:v", str(frames), "-an", *_SEGMENT_CODEC, str(looped)])
            return looped

        return [submit(loop_pass)]

    def _prefetch_speech(self, work: Path) -> Synth:
        """List every speech request the script makes and start synthesising them in the background."""
        requests: list[tuple[str, Voice, float]] = []
        real = self.synth

        class Recorder:
            name = real.name
            speed_range = real.speed_range  # so requests are recorded at the speed actually synthesised

            def resolve(self, name, lang, notes):
                return real.resolve(name, lang, notes)

            def synth(self, text, voice, speed, notes):
                requests.append((text, voice, speed))
                return np.zeros(1, dtype=np.float32)

        shadow = Builder(dataclasses.replace(self.script, warnings=[]), Recorder(), log=lambda _: None, jobs=1,
                         local_network=self.local_network)
        scenes = [scene for scene in self.script.scenes if self.selected is None or scene.index in self.selected]
        try:
            for _ in shadow.plans(scenes, work):
                pass
        finally:
            shadow.close()
        self.log(f"speech: {len(set(requests))} paragraphs, {real.concurrency} at a time")
        return Prefetching(real, requests, self.notes)

    def _encode_motion(self, v: Visual, frames: int, overlays: list[tuple[Path, float, float]], out: Path) -> Path:
        w, h = self.settings.width, self.settings.height
        inputs, graph = _overlay_graph("[0:v]setsar=1", overlays)
        return media.encode_frames(
            motion.frames(v.path, v.motion, v.position, w, h, frames, v.zoom, v.focus, v.windows), w, h, FPS,
            [*inputs, "-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames), "-an", *_SEGMENT_CODEC],
            out,
        )

    def _encode_video(self, v: Visual, frames: int, tempo: float, overlays: list[tuple[Path, float, float]],
                      canvas: str | None, out: Path) -> Path:
        """One clip as exactly `frames` frames: sped up or slowed by `tempo`, last frame held if short."""
        w, h = self.settings.width, self.settings.height
        chain = ["setpts=PTS-STARTPTS"]
        if abs(tempo - 1.0) > 1e-6:
            chain.append(f"setpts=PTS/{tempo:.6f}")
        chain += [f"fps={FPS}", *_resize_filters(v.fit, v.position, w, h, canvas or "#000000", v.align), "setsar=1",
                  f"tpad=stop_mode=clone:stop_duration={frames / FPS + 1:.3f}"]
        inputs, graph = _overlay_graph(f"[0:v]{','.join(chain)}", overlays)
        media.run(["-ss", f"{v.start:.3f}", "-t", f"{v.length:.3f}", "-i", str(v.path), *inputs,
                   "-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames),
                   "-an", *_SEGMENT_CODEC, str(out)])
        return out

    def _encode_page(self, v: Visual, frames: int, overlays: list[tuple[Path, float, float]], canvas: str | None,
                     renderer: FrameRenderer, out: Path, init_script: str | None = None) -> Path:
        """An HTML scene captured frame by frame (pages.py), as exactly `frames` frames."""
        w, h = self.settings.width, self.settings.height
        vw, vh = v.viewport
        # A page designed smaller than the frame renders at a higher pixel density, not enlarged afterwards
        scale = (min if v.fit == "contain" else max)(w / vw, h / vh)
        chain = [*_resize_filters(v.fit, v.position, w, h, canvas or "#000000", v.align), "setsar=1"]
        inputs, graph = _overlay_graph(f"[0:v]{','.join(chain)}", overlays)
        captured: list[int] = []
        with closing(pages.capture(renderer, v.path, v.viewport, scale, frames, FPS, captured.append,
                                   init_script)) as images:
            media.encode_images(images, FPS, [*inputs, "-filter_complex", graph, "-map", "[v]",
                                              "-frames:v", str(frames), "-an", *_SEGMENT_CODEC], out)
        if captured and captured[0] < frames:
            self.log(f"  {v.path.name}: {captured[0]} of {frames} frames captured, the rest settled")
        return out

    def _fade(self, audio: np.ndarray) -> np.ndarray:
        """The sound fades with the picture (header fade-in / fade-out)."""
        s = self.settings
        for seconds, at_start in ((s.fade_in, True), (s.fade_out, False)):
            n = min(len(audio), int(round(seconds * SR)))
            if n:
                ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
                audio = audio.copy()
                if at_start:
                    audio[:n] *= ramp
                else:
                    audio[-n:] *= ramp[::-1]
        return audio

    def _music_track(self) -> np.ndarray | None:
        """The header's background track, decoded: None without one.

        A build reads it first (#90), so a file that's missing or won't decode, or a built-in track
        that can't be made in the cache, stops the build in its first seconds, not once every scene
        is captured. Laying it under the scenes (`_music`) waits for their lengths."""
        bg = self.settings.background
        if bg is None:
            return None
        candidate = self.script.base_dir / bg.source
        if not candidate.is_file():
            if bg.source not in BUILTIN_MUSIC:
                raise BuildError(f"background file not found: {bg.source}")
            try:
                candidate = music.track(bg.source)  # narrate's own rendering of Narakeet's track name
            except OSError as e:  # writing it to the cache
                raise BuildError(f"background track {bg.source} couldn't be made: {e}") from None
        media.ffmpeg()  # found (or fetched) first, so that a problem with ffmpeg isn't blamed on the file
        try:
            return media.decode_audio(candidate, SR)
        except media.MediaError as e:
            raise BuildError(f"background {bg.source} couldn't be decoded: {e}") from None

    def _music(self, track: np.ndarray, total: int, scene_samples: list[int]) -> np.ndarray:
        """The background track under `total` samples: looped, at its volume, faded over the first and
        last scenes."""
        bg = self.settings.background
        loop = fit_length(track, total, tile=True) * bg.volume
        if bg.fade_in and scene_samples:
            n = min(scene_samples[0], total)
            loop[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)
        if bg.fade_out and scene_samples:
            n = min(scene_samples[-1], total)
            loop[total - n:] *= np.linspace(1.0, 0.0, n, dtype=np.float32)
        return loop

    def _mux(self, out: Path, scene_segments: list[list[Path]], scene_frames: list[int],
             transitions: list[tuple[str, float] | None], wav: Path, cues: list[Cue], work: Path,
             layers: list[Layer] | None = None) -> list[Path]:
        s = self.settings
        if any(transitions):
            files = [_join(segs, work / f"scene-{i:03d}-all.mp4") for i, segs in enumerate(scene_segments, 1)]
            args = [a for f in files for a in ("-i", str(f))]
            graph = transition_graph(scene_frames, transitions)
        else:
            listing = work / "segments.txt"
            listing.write_text("".join(f"file '{p.as_posix()}'\n" for segs in scene_segments for p in segs))
            files = [listing]
            args = ["-f", "concat", "-safe", "0", "-i", str(listing)]
            graph = "[0:v]null[vx]"
        if layers:  # the video's overlays (#59): above the transitions, below the subtitles and fades
            inputs, over, label = _layer_graph("under", layers, len(files))
            args += inputs
            graph = graph.replace("[vx]", "[under]") + over.replace(f"[{label}]", "[vx]")
        # Block-quote captions are explicit content: burn them in unless the
        # mode writes them to files instead
        burn = cues and (s.subtitles.burned or s.subtitles.mode == "none")
        graph += f";[vx]{self._subtitle_filter(cues, work)}[vs]" if burn else ";[vx]null[vs]"
        # Fades from and to black go last, so burned-in subtitles fade too
        total = sum(scene_frames) / FPS
        fades = ([f"fade=t=in:st=0:d={min(s.fade_in, total):.4f}"] if s.fade_in else []) + \
                ([f"fade=t=out:st={max(0.0, total - s.fade_out):.4f}:d={min(s.fade_out, total):.4f}"]
                 if s.fade_out else [])
        graph += f";[vs]{','.join(fades) or 'null'}[v]"
        audio_index = len(files) + len(layers or [])
        args += ["-i", str(wav)]
        maps = ["-map", "[v]", "-map", f"{audio_index}:a"]
        preset, crf, faststart = TARGETS[s.target]
        codecs = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        subtitle_files: list[Path] = []
        if s.subtitles.files and cues:
            srt, vtt = out.with_suffix(".srt"), out.with_suffix(".vtt")
            srt.write_text(subtitles.to_srt(cues), encoding="utf-8")
            vtt.write_text(subtitles.to_vtt(cues), encoding="utf-8")
            subtitle_files = [srt, vtt]
            if s.subtitles.mode == "embed":
                args += ["-i", str(srt)]
                maps += ["-map", f"{audio_index + 1}:s"]
                codecs += ["-c:s", "mov_text", "-metadata:s:s:0", "language=eng"]
        if faststart:
            codecs += ["-movflags", "+faststart"]
        media.run([*args, "-filter_complex", graph, *maps, *codecs, str(out)])
        return subtitle_files

    def _subtitle_filter(self, cues: list[Cue], work: Path) -> str:
        s = self.settings
        cfg = s.subtitles if s.subtitles.burned else SubtitleConfig(mode="overlay")
        fonts = work / "fonts"
        fonts.mkdir(exist_ok=True)
        font_name = "DejaVu Sans"
        for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
            if (found := find_font(name)) is not None:
                shutil.copy(found, fonts / name)
        if cfg.font:
            font_path = self._path(cfg.font, 0)
            shutil.copy(font_path, fonts / font_path.name)
            font_name = _font_family(font_path)
        ass = work / "captions.ass"
        ass.write_text(subtitles.to_ass(cues, cfg, s.width, s.height, font_name), encoding="utf-8")
        return f"subtitles=filename='{ass.as_posix()}':fontsdir='{fonts.as_posix()}'"


_SEGMENT_CODEC = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-pix_fmt", "yuv420p",
                  "-r", str(FPS), "-video_track_timescale", "15360"]


def _choice(value: Any, allowed: set[str], what: str) -> str:
    text = str(value).strip().lower()
    if text not in allowed:
        raise ConfigError(f"invalid {what} {value!r}")
    return text


def _is_narration(block) -> bool:
    return (isinstance(block, Narration) or (isinstance(block, Media) and block.kind == "audio")
            or (isinstance(block, Directive) and block.key in NARRATION_KEYS))


def merge_groups(scenes: list[Scene]) -> list[list[Scene]]:
    """Runs of scenes narrated as one: from a scene that sets narration-mode: merge
    to the scene that switches it back (spec §4.4); every other scene stands alone."""
    groups: list[list[Scene]] = []
    current: list[Scene] = []
    merging = False
    for scene in scenes:
        modes = [str(b.value).lower() for b in scene.blocks if isinstance(b, Directive) and b.key == "narration-mode"]
        if merging or "merge" in modes:
            current.append(scene)
            for mode in modes:
                merging = mode == "merge"
            if not merging:
                groups.append(current)
                current = []
        else:
            groups.append([scene])
    if current:
        groups.append(current)
    return groups


def layout_visuals(visuals: list[Visual], a: float, duration: float | None) -> tuple[float, Layout]:
    """Scene length and how the visuals share it (spec §4.2, §16.6).

    Images alone share the narration equally. Once a scene has a clip, its
    visuals play in order as one sequence: clips at their own length, images
    at DEFAULT_STILL_SECONDS each, and the first clip's sync mode applies to
    the sequence as a whole. Spare time goes to the images, or holds the last
    clip's final frame when there are none.
    """
    n = len(visuals)
    if not any(v.kind == "video" for v in visuals):
        if a <= 0 and duration is None and any(v.kind == "page" for v in visuals):
            # HTML scenes play their animations out; any pictures beside them get a still's time
            natural = [v.length if v.kind == "page" else DEFAULT_STILL_SECONDS for v in visuals]
            return max(sum(natural), 1 / FPS), Layout(natural, [1.0] * n)
        if a > 0:
            d = max(a, duration or 0.0)
        else:
            d = duration if duration is not None else DEFAULT_STILL_SECONDS
        d = max(d, 1 / FPS)
        return d, Layout([d / n] * n if n else [], [1.0] * n)

    natural = [v.length if v.kind == "video" else DEFAULT_STILL_SECONDS for v in visuals]
    total = sum(natural)
    sync = next(v.sync for v in visuals if v.kind == "video")
    if a <= 0:
        d = duration if duration is not None else total
    else:
        if sync in ("match", "trim-or-loop"):
            d = a
        elif sync == "trim":
            d = min(total, a)
        else:  # freeze, loop, loop-audio
            d = max(total, a)
        d = max(d, duration or 0.0)
    d = max(d, 1 / FPS)

    if sync == "match" and a > 0:
        scale = d / total
        return d, Layout([x * scale for x in natural],
                         [1 / scale if v.kind == "video" else 1.0 for v in visuals])
    if sync in ("loop", "trim-or-loop") and d > total:
        return d, Layout(natural, [1.0] * n, repeat=True)
    pieces = list(natural)
    if d > total:
        stills = [i for i, v in enumerate(visuals) if v.kind in ("still", "page")] or [n - 1]
        for i in stills:
            pieces[i] += (d - total) / len(stills)
    elif d < total:
        remaining = d
        for i, x in enumerate(pieces):
            pieces[i] = min(x, max(0.0, remaining))
            remaining -= pieces[i]
    return d, Layout(pieces, [1.0] * n)


def _run_now(fn: Callable, *args) -> Future:
    """A completed future: the synchronous stand-in for an executor's submit."""
    future: Future = Future()
    try:
        future.set_result(fn(*args))
    except BaseException as e:  # noqa: BLE001 - surfaced by .result()
        future.set_exception(e)
    return future


def _windows(overlays: list[tuple[Path, float, float]], lo: int, hi: int) -> list[tuple[Path, float, float]]:
    """The overlays showing during scene frames [lo, hi), in that piece's own time."""
    start, end = lo / FPS, hi / FPS
    return [(png, max(a, start) - start, min(b, end) - start) for png, a, b in overlays if min(b, end) > max(a, start)]


def _layer_graph(base: str, layers: list[Layer], first_input: int) -> tuple[list[str], str, str]:
    """ffmpeg inputs, and a filter graph fragment (starting with ";") laying `layers` over the stream
    labelled `base`, in order; returns them with the label of the result (#59)."""
    inputs: list[str] = []
    graph, label = "", base
    for k, layer in enumerate(layers):
        if layer.still:
            inputs += ["-loop", "1", "-framerate", str(FPS)]
        if layer.offset:
            inputs += ["-itsoffset", f"{layer.offset:.6f}"]
        inputs += ["-i", str(layer.path)]
        enable = f":enable='{layer.enable}'" if layer.enable else ""
        # A stream that ends early leaves the picture as it is from then on
        graph += f";[{label}][{first_input + k}:v]overlay={layer.x}:{layer.y}:format=auto:eof_action=pass{enable}[layer{k}]"
        label = f"layer{k}"
    return inputs, graph, label


def _overlay_graph(base: str, overlays: list[tuple[Path, float, float]]) -> tuple[list[str], str]:
    """ffmpeg inputs and a filter graph compositing time-windowed overlays onto `base`, output [v]."""
    inputs: list[str] = []
    graph = f"{base}[v0]"
    for k, (png, a, b) in enumerate(overlays, 1):
        inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(png)]
        enable = "" if len(overlays) == 1 else f":enable='gte(t,{a:.4f})*lt(t,{b:.4f})'"
        graph += f";[v{k - 1}][{k}:v]overlay=0:0:format=auto{enable}[v{k}]"
    return inputs, graph + f";[v{len(overlays)}]null[v]"


# fade-black / fade-white: the outgoing frame fades to the colour over the first
# half, the incoming scene fades in from it over the second (xfade's own
# fadeblack goes dark almost at once). Luma and chroma of each colour in 8-bit
# video range; xfade's progress P runs from 1 to 0.
_DIP_LEVELS = {"fade-black": (16, 128), "fade-white": (235, 128)}


def _dip(name: str) -> str:
    luma, chroma = _DIP_LEVELS[name]
    m = f"if(PLANE,{chroma},{luma})"
    return f"custom:expr='if(gt(P,0.5),{m}+(A-{m})*(2*P-1),{m}+(B-{m})*(1-2*P))'"


def transition_graph(frames: list[int], transitions: list[tuple[str, float] | None]) -> str:
    """Filter graph joining scene inputs 0..n-1 with xfade transitions, output [vx].

    As in Narakeet's renders, a transition starts at the cut and plays over the
    start of the incoming scene while the outgoing scene holds its last frame,
    so the joined video is exactly as long as the scenes and the audio
    timeline is untouched. A transition is clamped to the incoming scene.
    """
    n = len(frames)
    span = [0] * n  # frames of the transition into scene k
    for k in range(1, n):
        if transitions[k]:
            span[k] = min(round(transitions[k][1] * FPS), frames[k])

    parts = []
    for k in range(n):
        hold = span[k + 1] if k + 1 < n else 0  # last frame held while the next scene comes in
        pads = [f"tpad=stop_mode=clone:stop_duration={hold / FPS:.6f}"] if hold else []
        # One timebase for every input (set after fps, which changes it), so xfade
        # and concat outputs always match the next scene's input (#38)
        parts.append(f"[{k}:v]" + ",".join([f"fps={FPS}", *pads, "settb=AVTB"]) + f"[p{k}]")
    current, length = "p0", frames[0] + (span[1] if n > 1 else 0)
    for k in range(1, n):
        padded = frames[k] + (span[k + 1] if k + 1 < n else 0)
        if span[k]:
            name = transitions[k][0]
            kind = _dip(name) if name in _DIP_LEVELS else XFADE[name]
            parts.append(f"[{current}][p{k}]xfade=transition={kind}:duration={span[k] / FPS:.6f}"
                         f":offset={(length - span[k]) / FPS:.6f}[x{k}]")
            length += padded - span[k]
        else:
            parts.append(f"[{current}][p{k}]concat=n=2:v=1:a=0[x{k}]")
            length += padded
        current = f"x{k}"
    return ";".join([*parts, f"[{current}]null[vx]"])


def _join(segments: list[Path], out: Path) -> Path:
    """One file from a scene's segments (stream copy)."""
    if len(segments) == 1:
        return segments[0]
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in segments))
    media.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)])
    return out


def allocate_frames(frames: int, pieces: list[float]) -> list[int]:
    """Split a whole number of frames in proportion to `pieces` (largest remainder)."""
    total = sum(pieces)
    if total <= 0:
        return [frames] + [0] * (len(pieces) - 1)
    exact = [p / total * frames for p in pieces]
    counts = [int(x) for x in exact]
    for i in sorted(range(len(pieces)), key=lambda i: exact[i] - counts[i], reverse=True)[:frames - sum(counts)]:
        counts[i] += 1
    return counts


def _resize_filters(fit: str, position: str, w: int, h: int, canvas: str,
                    align: tuple[float, float] | None = None) -> list[str]:
    """ffmpeg filters fitting a clip or page to w×h; `align` (from a focal point) places the crop or the
    picture as CSS object-position fractions would."""
    if fit == "fit":
        return [f"scale={w}:{h}"]
    if fit == "contain":
        px, py = ((f"(ow-iw)*{align[0]:.6f}", f"(oh-ih)*{align[1]:.6f}") if align else ("(ow-iw)/2", "(oh-ih)/2"))
        return [f"scale={w}:{h}:force_original_aspect_ratio=decrease", f"pad={w}:{h}:{px}:{py}:color={canvas}"]
    if align:
        return [f"scale={w}:{h}:force_original_aspect_ratio=increase",
                f"crop={w}:{h}:(iw-ow)*{align[0]:.6f}:(ih-oh)*{align[1]:.6f}"]
    x = {"left": "0", "top-left": "0", "right": "iw-ow"}.get(position, "(iw-ow)/2")
    y = {"top": "0", "top-left": "0", "bottom": "ih-oh"}.get(position, "(ih-oh)/2")
    return [f"scale={w}:{h}:force_original_aspect_ratio=increase", f"crop={w}:{h}:{x}:{y}"]


def _encode_still(png: Path, frames: int, out: Path) -> Path:
    media.run(["-loop", "1", "-framerate", str(FPS), "-i", str(png), "-frames:v", str(frames),
               "-vf", "format=yuv420p,setsar=1", "-tune", "stillimage", *_SEGMENT_CODEC, str(out)])
    return out


def _write_wav(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def _poster_time(value: str, total: float) -> float:
    text = value.strip().lower()
    last = max(0.0, total - 1 / FPS)
    if text == "start":
        return 0.0
    if text == "end":
        return last
    if text.endswith("%"):
        return min(last, total * float(text[:-1]) / 100)
    return min(last, parse_time(text))


def _font_family(path: Path) -> str:
    from PIL import ImageFont

    return ImageFont.truetype(str(path), 10).getname()[0]
