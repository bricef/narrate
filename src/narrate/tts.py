"""Speech synthesis backends, voice resolution and the synthesis cache.

Voice names in scripts can come from three catalogues: Kokoro ids
(`bf_emma`), ElevenLabs default voices (`George`) and Narakeet voices
(`Brian`). Each known name has a profile (language/accent and gender). A
backend uses a name directly when it is one of its own voices, and otherwise
picks its voice for the name's profile, so a script written for one engine (or
for Narakeet) still renders sensibly on another.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .phonetics import PHONETIC_STYLES, phonemize, to_ipa
from .text import Speech, speech_text

JOIN_SECONDS = 0.06  # between segments synthesised as separate requests

SAMPLE_RATE = 24000


@dataclass(frozen=True)
class Profile:
    lang: str  # lowercase, e.g. "en-us", "en-gb", "de"
    gender: str  # "f" | "m"


@dataclass(frozen=True)
class Voice:
    id: str
    lang: str | None  # in the backend's own convention; None lets the engine decide


# Narakeet voice names (as used in its docs and examples) by language and gender
NARAKEET_PROFILES: dict[str, tuple[str, str]] = {
    **dict.fromkeys(
        "joanna jennifer kimberly salli kendra ivy ruth danielle jackie julia linda betty mimi "
        "missy holly debbie cindy mandy kerry harmony sandy maria clara diana catherine hannah "
        "veronica jenna donna paige martha naomi whitney maya alicia anita jodie lonnie kimmy "
        "cherry candace ellen katie melinda penny".split(), ("en-us", "f")),
    **dict.fromkeys(
        "matt joey justin kevin matthew stephen gregory mike rodney ronald john peter nick michael "
        "max raymond gary jonah brandon spike duncan wesley zach samuel sebastian benjamin andrew "
        "blake grant derek stanley darren warren darius clayton franklin jared brady patrick dalton "
        "jimmy burton wally randy fred barney".split(), ("en-us", "m")),
    **dict.fromkeys("amy emma victoria daisy beatrice amelia".split(), ("en-gb", "f")),
    **dict.fromkeys("brian benedict edward anton william alfred arthur".split(), ("en-gb", "m")),
    **dict.fromkeys("nicole olivia emily".split(), ("en-au", "f")),
    **dict.fromkeys("russell russel graham alan".split(), ("en-au", "m")),
    **dict.fromkeys("raveena kajal neerja sheela devika kavita".split(), ("en-in", "f")),
    **dict.fromkeys("prabhat manish".split(), ("en-in", "m")),
    **dict.fromkeys("aileen".split(), ("en-scotland", "f")),
    **dict.fromkeys("niamh".split(), ("en-ie", "f")),
    **dict.fromkeys("aletta ayanda".split(), ("en-za", "f")),
    **dict.fromkeys("monika vicki marlene gertrud".split(), ("de", "f")),
    **dict.fromkeys("helmut hans florian dietrich".split(), ("de", "m")),
    **dict.fromkeys("carmen lucia conchita lupe mia blanca elvira paquita ramona".split(), ("es", "f")),
    **dict.fromkeys("enrique sergio andres ricardo javier".split(), ("es", "m")),
    **dict.fromkeys("celine lea lucienne audrey".split(), ("fr", "f")),
    **dict.fromkeys("alain mathieu jacques victor antoine".split(), ("fr", "m")),
    **dict.fromkeys("alessandra carla gina".split(), ("it", "f")),
    **dict.fromkeys("giorgio giovanni pietro giulio".split(), ("it", "m")),
    **dict.fromkeys("camila vitoria leandra".split(), ("pt-br", "f")),
    **dict.fromkeys("murilo".split(), ("pt-br", "m")),
    **dict.fromkeys("mizuki tomoka".split(), ("ja", "f")),
    **dict.fromkeys("takumi toshiro".split(), ("ja", "m")),
    **dict.fromkeys("aditi kajal".split(), ("hi", "f")),
    **dict.fromkeys("zhiyu mei".split(), ("zh", "f")),
}

# -- Kokoro ---------------------------------------------------------------------

KOKORO_VOICES = (
    "af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova af_river af_sarah "
    "af_sky am_adam am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck am_santa "
    "bf_alice bf_emma bf_isabella bf_lily bm_daniel bm_fable bm_george bm_lewis ef_dora em_alex "
    "em_santa ff_siwis hf_alpha hf_beta hm_omega hm_psi if_sara im_nicola jf_alpha jf_gongitsune "
    "jf_nezumi jf_tebukuro jm_kumo pf_dora pm_alex pm_santa zf_xiaobei zf_xiaoni zf_xiaoxiao "
    "zf_xiaoyi zm_yunjian zm_yunxi zm_yunxia zm_yunyang"
).split()
# Kokoro id prefix letter -> profile language, and the espeak code that phonemizes it
KOKORO_PREFIX_LANG = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr", "h": "hi",
                      "i": "it", "j": "ja", "p": "pt-br", "z": "zh"}
KOKORO_LANG_PREFIX = {lang: prefix for prefix, lang in KOKORO_PREFIX_LANG.items()}
ESPEAK = {"en": "en-us", "en-us": "en-us", "en-gb": "en-gb", "fr": "fr-fr", "pt": "pt-br",
          "pt-br": "pt-br", "zh": "cmn", "zh-cn": "cmn", "cmn": "cmn"}
KOKORO_DEFAULT = "af_heart"
KOKORO_BY_PROFILE = {
    ("en-us", "f"): "af_heart", ("en-us", "m"): "am_michael", ("en-gb", "f"): "bf_emma",
    ("en-gb", "m"): "bm_george", ("es", "f"): "ef_dora", ("es", "m"): "em_alex",
    ("fr", "f"): "ff_siwis", ("fr", "m"): "ff_siwis", ("it", "f"): "if_sara",
    ("it", "m"): "im_nicola", ("pt-br", "f"): "pf_dora", ("pt-br", "m"): "pm_alex",
    ("hi", "f"): "hf_alpha", ("hi", "m"): "hm_omega", ("ja", "f"): "jf_alpha",
    ("ja", "m"): "jm_kumo", ("zh", "f"): "zf_xiaoxiao", ("zh", "m"): "zm_yunxi",
}

# -- ElevenLabs -----------------------------------------------------------------

# Default voices usable on every plan (library voices need a paid plan)
ELEVENLABS_VOICES: dict[str, tuple[str, tuple[str, str]]] = {
    "sarah": ("EXAVITQu4vr4xnSDxMaL", ("en-us", "f")),
    "laura": ("FGY2WhTYpPnrIDTdsKH5", ("en-us", "f")),
    "jessica": ("cgSgspJ2msm6clMCkdW9", ("en-us", "f")),
    "matilda": ("XrExE9yKIg1WjnnlVkGX", ("en-us", "f")),
    "river": ("SAz9YHcvj6GT2YYXdXww", ("en-us", "f")),
    "alice": ("Xb7hH8MSUJpSbSDYk0k2", ("en-gb", "f")),
    "lily": ("pFZP5JQG7iQjIQuC4Bku", ("en-gb", "f")),
    "brian": ("nPczCjzI2devNBz1zQrb", ("en-us", "m")),
    "eric": ("cjVigY5qzO86Huf0OWal", ("en-us", "m")),
    "chris": ("iP95p4xoKVk53GoZ742B", ("en-us", "m")),
    "will": ("bIHbv24MWmeRgasZH58o", ("en-us", "m")),
    "liam": ("TX3LPaxmHKxFdv7VOQHJ", ("en-us", "m")),
    "roger": ("CwhRBWXzGAHq8TQ4Fs17", ("en-us", "m")),
    "adam": ("pNInz6obpgDQGcFmaJgB", ("en-us", "m")),
    "george": ("JBFqnCBsd6RMkjVDRZzb", ("en-gb", "m")),
    "daniel": ("onwK4e9ZLuTAKqWW03F9", ("en-gb", "m")),
    "callum": ("N2lVS1w4EtoT3dr4eOWO", ("en-gb", "m")),
    "charlie": ("IKne3meq5aSn9XLyUdCD", ("en-au", "m")),
}
ELEVENLABS_DEFAULT = "george"
# ElevenLabs voices are multilingual: an accent match matters for English only
ELEVENLABS_BY_PROFILE = {
    ("en-us", "f"): "sarah", ("en-us", "m"): "eric", ("en-gb", "f"): "alice",
    ("en-gb", "m"): "george", ("en-au", "m"): "charlie",
}
ELEVENLABS_BY_GENDER = {"f": "sarah", "m": "george"}
ELEVENLABS_MODEL = "eleven_multilingual_v2"
ELEVENLABS_PHONEME_MODEL = "eleven_flash_v2"  # reads <phoneme alphabet="ipa"> tags
ELEVENLABS_SPEED = (0.7, 1.2)
# Models with no speed setting: narrate synthesises at 1.0 and stretches the rest
# (#47; the factor-q intro sped eleven_v4 up with atempo for the same reason)
ELEVENLABS_FIXED_SPEED = ("eleven_v3", "eleven_v4")
_ELEVENLABS_ID_RE = re.compile(r"^[A-Za-z0-9]{20}$")
NO_KEY = "ELEVENLABS_API_KEY is not set (put it in .env or the environment)"


# When an engine has no voice for an accent, try these next (same gender)
ACCENT_FALLBACK = {
    "en-au": ["en-gb", "en-us"], "en-nz": ["en-gb", "en-us"], "en-ie": ["en-gb", "en-us"],
    "en-za": ["en-gb", "en-us"], "en-scotland": ["en-gb", "en-us"], "en-in": ["en-gb", "en-us"],
    "en-gb": ["en-us"], "en-us": ["en-gb"],
}


def by_profile(table: dict[tuple[str, str], str], profile: Profile) -> str | None:
    """The table's voice for a profile, falling back to a related accent."""
    for lang in [profile.lang, *ACCENT_FALLBACK.get(profile.lang, [])]:
        if (lang, profile.gender) in table:
            return table[(lang, profile.gender)]
    return None


def profile_of(name: str) -> Profile | None:
    key = name.strip().lower()
    if key in KOKORO_VOICES:
        return Profile(KOKORO_PREFIX_LANG[key[0]], key[1])
    if key in ELEVENLABS_VOICES:
        return Profile(*ELEVENLABS_VOICES[key][1])
    if key in NARAKEET_PROFILES:
        return Profile(*NARAKEET_PROFILES[key])
    return None


def split_voice(name: str | None) -> tuple[str | None, str | None]:
    """`xenobot/de-DE` -> ("xenobot", "de-de")."""
    if not name:
        return None, None
    base, _, lang = name.strip().partition("/")
    return base.strip() or None, lang.strip().lower() or None


def cache_dir() -> Path:
    root = os.environ.get("NARRATE_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "narrate"
    )
    return Path(root)


class SynthCache:
    def __init__(self, backend: str):
        self.dir = cache_dir() / "tts" / backend
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, *key: object) -> Path:
        return self.dir / (hashlib.sha256(json.dumps(key).encode()).hexdigest() + ".npy")

    @staticmethod
    def load(path: Path) -> np.ndarray | None:
        """The cached audio, refreshing its timestamp so pruning sees it as recently used."""
        try:
            audio = np.load(path)
        except (FileNotFoundError, ValueError, OSError):
            return None
        try:
            os.utime(path)
        except OSError:
            pass
        return audio


class Synth(Protocol):
    name: str
    speed_range: tuple[float, float]  # speeds the engine renders natively; narrate stretches the rest

    def resolve(self, name: str | None, lang: str | None, notes: set[str]) -> Voice:
        """Pick this backend's voice for a script voice name and optional language override."""

    def synth(self, text: "Speech", voice: Voice, speed: float, notes: set[str]) -> np.ndarray:
        """Mono float32 samples at SAMPLE_RATE.

        `text` is a string, or (text, language) segments when part of a sentence
        is in another language (`[See]{de}`); the voice stays the same.
        """


# -- Kokoro backend -------------------------------------------------------------

MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")


def tools_dir() -> Path:
    """Downloaded tools (Kokoro model, ffmpeg): kept apart from the speech cache,
    so pointing NARRATE_CACHE somewhere fresh doesn't trigger new downloads."""
    root = os.environ.get("NARRATE_TOOLS") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "narrate"
    )
    return Path(root)


def model_dir() -> Path:
    return tools_dir() / "models" / "kokoro-v1.0"


def ensure_models(log=lambda msg: print(msg, file=sys.stderr)) -> Path:
    target = model_dir()
    target.mkdir(parents=True, exist_ok=True)
    for name in MODEL_FILES:
        path = target / name
        if path.exists():
            continue
        log(f"downloading Kokoro model file {name} (first use only) …")
        part = path.with_suffix(path.suffix + ".part")
        with urllib.request.urlopen(MODEL_URL + name) as resp, open(part, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        part.rename(path)
    return target


def kokoro_resolve(name: str | None, lang: str | None, notes: set[str]) -> Voice:
    base, suffix_lang = split_voice(name)
    lang = lang or suffix_lang
    voice_id = KOKORO_DEFAULT
    if base:
        key = base.lower()
        profile = profile_of(key)
        same_name = f"{KOKORO_LANG_PREFIX.get(profile.lang, '?')}{profile.gender}_{key}" if profile else ""
        if key in KOKORO_VOICES:
            voice_id = key
        elif same_name in KOKORO_VOICES:
            voice_id = same_name  # e.g. ElevenLabs "Sarah" -> af_sarah
            notes.add(f"voice {base!r} matched to Kokoro voice {voice_id}")
        elif profile and (match := by_profile(KOKORO_BY_PROFILE, profile)):
            voice_id = match
            notes.add(f"voice {base!r} approximated by Kokoro voice {voice_id}")
        else:
            notes.add(f"voice {base!r} has no Kokoro equivalent; using {KOKORO_DEFAULT}")
            # Keep the voice's language so the text is still phonemized correctly
            lang = lang or (profile.lang if profile else None)
    return Voice(voice_id, espeak_code(lang or KOKORO_PREFIX_LANG[voice_id[0]]))


def segment_phonemes(part: str, tag: str | None, voice_lang: str) -> str:
    if tag in PHONETIC_STYLES:
        return to_ipa(part, tag)
    return phonemize(part, espeak_code(tag) if tag else voice_lang)


def espeak_code(lang: str) -> str:
    """An espeak language for Kokoro's phonemizer: keep regions espeak knows, else drop them."""
    lang = lang.lower()
    return ESPEAK.get(lang) or ESPEAK.get(lang.split("-")[0]) or lang.split("-")[0]


# ElevenLabs credits per character, by model, where it reports none itself: the flash and turbo models cost half
CREDITS_PER_CHARACTER = {"eleven_flash_v2": 0.5, "eleven_flash_v2_5": 0.5, "eleven_turbo_v2": 0.5,
                         "eleven_turbo_v2_5": 0.5}


class Usage:
    """What a run asked of its speech engine (#81), for cost reports. Per model:
    - `characters`, `requests`: what was actually sent, in requests that succeeded (retries of refused ones
      aren't billed, so aren't counted);
    - `credits`, for a paid engine: as the engine billed them (its `character-cost` header), else estimated
      from the model's rate per character (`estimated_requests` says how many were);
    - `cached_characters`: what distinct requests the cache answered instead. A request asked again later in
      the same run (the speech is planned twice) counts once.

    Engines synthesise from several threads, hence the lock."""

    def __init__(self, engine: str, billed: bool = False):
        self.engine = engine
        self.billed = billed
        self._lock = threading.Lock()
        self._models: dict[str, dict] = {}
        self._made: set = set()
        self._cached: dict = {}

    def _empty(self) -> dict:
        return {"characters": 0, "requests": 0, "cached_characters": 0,
                **({"credits": 0.0, "estimated_requests": 0} if self.billed else {})}

    def _model(self, model: str) -> dict:
        return self._models.setdefault(model, self._empty())

    def sent(self, model: str, characters: int, credits: float | None = None) -> None:
        with self._lock:
            entry = self._model(model)
            entry["characters"] += characters
            entry["requests"] += 1
            if self.billed:
                if credits is None:
                    credits = characters * CREDITS_PER_CHARACTER.get(model, 1.0)
                    entry["estimated_requests"] += 1
                entry["credits"] += credits

    def looked_up(self, model: str, key, characters: int, hit: bool) -> None:
        """A request the cache was asked about: answered (`hit`), or about to be synthesised."""
        with self._lock:
            if not hit:
                self._made.add(key)
                self._cached.pop(key, None)
            elif key not in self._made:
                self._cached[key] = (model, characters)

    def report(self) -> dict:
        with self._lock:
            models = {name: dict(entry) for name, entry in self._models.items()}
            for model, characters in self._cached.values():
                models.setdefault(model, self._empty())["cached_characters"] += characters
        return {"engine": self.engine, "models": models}


class SilentSynth:
    """Timing-only backend: silence as long as the speech would roughly be."""

    name = "silent"
    seconds_per_char = 0.065
    speed_range = (0.01, 100.0)

    def resolve(self, name: str | None, lang: str | None, notes: set[str]) -> Voice:
        return kokoro_resolve(name, lang, set())

    def synth(self, text, voice: Voice, speed: float, notes: set[str], only_cached: bool = False) -> np.ndarray:
        seconds = max(0.3, len(speech_text(text)) * self.seconds_per_char / speed)
        return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


class EstimatingSynth:
    """Speech from `real`'s cache where it's there, and an estimate from the text's length where it isn't.

    For previews (the studio's timing): nothing is synthesised, so nothing is
    spent. `estimates` counts the requests that weren't cached, and
    `missing_chars` what synthesising them would send to the engine."""

    def __init__(self, real: "Synth"):
        self.real = real
        self.name = real.name
        self.speed_range = real.speed_range
        self.estimates = 0
        self.missing_chars = 0

    def resolve(self, name: str | None, lang: str | None, notes: set[str]) -> Voice:
        return self.real.resolve(name, lang, notes)

    def synth(self, text, voice: Voice, speed: float, notes: set[str]) -> np.ndarray:
        hit = self.real.synth(text, voice, speed, notes, only_cached=True)
        if hit is not None:
            return hit
        self.estimates += 1
        self.missing_chars += len(speech_text(text))
        return SilentSynth().synth(text, voice, speed, notes)


class KokoroSynth:
    """Kokoro-82M on the CPU, one ONNX session per worker thread.

    Several smaller sessions synthesise more speech per second than one
    session using every core (about 1.5x with four on eight cores), so
    paragraphs are synthesised `concurrency` at a time. espeak, which
    phonemizes the text, is not thread-safe and runs under a lock.
    """

    name = "kokoro"
    speed_range = (0.5, 2.0)

    def __init__(self) -> None:
        self._models: Path | None = None  # fetched on first synthesis, not just to look in the cache
        cores = os.cpu_count() or 1
        self.concurrency = max(1, int(os.environ.get("NARRATE_KOKORO_WORKERS", 0)) or min(4, cores // 2))
        self._threads = max(1, cores // self.concurrency)
        self._local = threading.local()
        self._phonemize = threading.Lock()
        self._cache = SynthCache(self.name)
        self.usage = Usage(self.name)

    def _engine(self):
        if not hasattr(self._local, "kokoro"):
            import onnxruntime as rt
            from kokoro_onnx import Kokoro

            with self._phonemize:  # one download, whichever thread gets here first
                if self._models is None:
                    self._models = ensure_models()

            options = rt.SessionOptions()
            options.intra_op_num_threads = self._threads
            # Idle ONNX threads busy-wait by default, which starves the video
            # encodes running alongside narration; sleeping costs little here
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            session = rt.InferenceSession(str(self._models / MODEL_FILES[0]), options,
                                          providers=["CPUExecutionProvider"])
            self._local.kokoro = Kokoro.from_session(session, str(self._models / MODEL_FILES[1]))
        return self._local.kokoro

    def resolve(self, name: str | None, lang: str | None, notes: set[str]) -> Voice:
        return kokoro_resolve(name, lang, notes)

    def synth(self, text: str, voice: Voice, speed: float, notes: set[str],
              only_cached: bool = False) -> np.ndarray | None:
        clamped = min(2.0, max(0.5, speed))
        if clamped != speed:
            notes.add(f"voice-speed {speed} (Kokoro accepts 0.5–2; clamped)")
        cached = self._cache.path(MODEL_FILES[0], text, voice.id, voice.lang, round(clamped, 3))
        if (hit := self._cache.load(cached)) is not None or only_cached:
            if not only_cached:
                self.usage.looked_up(MODEL_FILES[0], cached, len(speech_text(text)), True)
            return hit
        self.usage.looked_up(MODEL_FILES[0], cached, len(speech_text(text)), False)
        kokoro = self._engine()
        segments = [(text, None)] if isinstance(text, str) else text
        # Each segment in its own language, or as given phonetically, spliced
        # into one utterance so the voice and its prosody run straight through
        phonemes = " ".join(segment_phonemes(part, tag, voice.lang) for part, tag in segments)
        audio, sr = kokoro.create(phonemes, voice=voice.id, speed=clamped, lang=voice.lang, is_phonemes=True)
        assert sr == SAMPLE_RATE
        audio = np.asarray(audio, dtype=np.float32)
        np.save(cached, audio)
        self.usage.sent(MODEL_FILES[0], len(speech_text(text)))
        return audio


# -- ElevenLabs backend ---------------------------------------------------------


class LanguageRejected(Exception):
    """The model refused an explicit language_code."""


class ElevenLabsSynth:
    name = "elevenlabs"
    url = "https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_24000"
    speed_range = ELEVENLABS_SPEED
    # Simultaneous requests: the free plan allows 2; raise it for larger plans
    concurrency = int(os.environ.get("NARRATE_ELEVENLABS_CONCURRENCY", 2))

    def __init__(self, model: str | None = None, planning: bool = False) -> None:
        """`planning`: for timing and pricing only (narrate check --timing), which needs no key: it reads
        the speech cache and estimates the rest, and never calls the API (#71)."""
        self.key = os.environ.get("ELEVENLABS_API_KEY")
        if not self.key and not planning:
            raise RuntimeError(NO_KEY)
        self.model = model or os.environ.get("NARRATE_ELEVENLABS_MODEL") or ELEVENLABS_MODEL
        if self.model.startswith(ELEVENLABS_FIXED_SPEED):
            self.speed_range = (1.0, 1.0)
        self._cache = SynthCache(self.name)
        self.usage = Usage(self.name, billed=True)
        self._ignores_language: set[str] = set()
        self._library_names: dict[str, str] | None = None

    def _get(self, url: str) -> bytes:
        if not self.key:
            raise RuntimeError(NO_KEY)
        request = urllib.request.Request(url, headers={"xi-api-key": self.key})
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.read()

    def _library(self) -> dict[str, str]:
        """Voices in the account's library by lowercase name; empty if the key can't list voices."""
        if self._library_names is None and not self.key:
            self._library_names = {}  # no key, no account to look in: the default voices only
        if self._library_names is None:
            try:
                voices = json.loads(self._get("https://api.elevenlabs.io/v1/voices")).get("voices", [])
                self._library_names = {v["name"].split(" - ")[0].strip().lower(): v["voice_id"] for v in voices}
            except (urllib.error.URLError, ValueError, KeyError, TypeError, OSError):
                self._library_names = {}  # e.g. a key without voices_read
        return self._library_names

    def resolve(self, name: str | None, lang: str | None, notes: set[str]) -> Voice:
        base, suffix_lang = split_voice(name)
        lang = lang or suffix_lang
        voice_name = ELEVENLABS_DEFAULT
        voice_id = None
        if base:
            key = base.lower()
            profile = profile_of(key)
            if _ELEVENLABS_ID_RE.match(base):
                voice_id = base  # a raw voice id from the user's own library
            elif key in ELEVENLABS_VOICES:
                voice_name = key
            elif key in self._library():
                voice_id = self._library()[key]  # a voice in the account's library, by name
            elif profile:
                voice_name = by_profile(ELEVENLABS_BY_PROFILE, profile) or ELEVENLABS_BY_GENDER[profile.gender]
                notes.add(f"voice {base!r} approximated by ElevenLabs voice {voice_name.title()}")
                lang = lang or (profile.lang if not profile.lang.startswith("en") else None)
            else:
                notes.add(f"voice {base!r} is unknown; using ElevenLabs voice {voice_name.title()}")
        code = lang.split("-")[0] if lang else None
        return Voice(voice_id or ELEVENLABS_VOICES[voice_name][0], code)

    def synth(self, text: str, voice: Voice, speed: float, notes: set[str],
              only_cached: bool = False) -> np.ndarray | None:
        if not isinstance(text, str):
            # One request per segment, each with its own language hint; phonetic
            # segments go to a model that reads IPA
            parts = [self._synth_phonetic(to_ipa(part, tag), voice, speed, notes, only_cached)
                     if tag in PHONETIC_STYLES
                     else self.synth(part, Voice(voice.id, tag.split("-")[0] if tag else voice.lang), speed, notes,
                                     only_cached)
                     for part, tag in text]
            if any(p is None for p in parts):
                return None
            gap = np.zeros(int(JOIN_SECONDS * SAMPLE_RATE), dtype=np.float32)
            return np.concatenate([p for part in parts for p in (part, gap)][:-1])
        lo, hi = self.speed_range
        clamped = min(hi, max(lo, speed))
        if clamped != speed:
            notes.add(f"voice-speed {speed} ({self.model} accepts {lo}–{hi}; clamped)")
        cached = self._cache.path(self.model, text, voice.id, voice.lang, round(clamped, 3))
        if (hit := self._cache.load(cached)) is not None or only_cached:
            if not only_cached:
                self.usage.looked_up(self.model, cached, len(text), True)
            return hit
        self.usage.looked_up(self.model, cached, len(text), False)
        body: dict = {"text": text, "model_id": self.model}
        if clamped != 1.0:
            body["voice_settings"] = {"speed": clamped}
        if voice.lang and self.model not in self._ignores_language:
            body["language_code"] = voice.lang
        try:
            pcm = self._post(voice.id, body)
        except LanguageRejected:
            # Older models refuse language_code: remember, and let them detect the language
            self._ignores_language.add(self.model)
            notes.add(f"language hints with {self.model} (the model detects the language itself)")
            body.pop("language_code", None)
            pcm = self._post(voice.id, body)
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        np.save(cached, audio)
        return audio

    def _synth_phonetic(self, ipa: str, voice: Voice, speed: float, notes: set[str],
                        only_cached: bool = False) -> np.ndarray | None:
        """A word given in IPA, via a <phoneme> tag (only the flash/turbo v2 models read them)."""
        lo, hi = ELEVENLABS_SPEED
        clamped = min(hi, max(lo, speed))
        cached = self._cache.path(ELEVENLABS_PHONEME_MODEL, "ipa", ipa, voice.id, round(clamped, 3))
        if (hit := self._cache.load(cached)) is not None or only_cached:
            if not only_cached:
                self.usage.looked_up(ELEVENLABS_PHONEME_MODEL, cached, len(ipa), True)
            return hit
        self.usage.looked_up(ELEVENLABS_PHONEME_MODEL, cached, len(ipa), False)
        notes.add(f"phonetic spans on ElevenLabs are voiced by {ELEVENLABS_PHONEME_MODEL}, "
                  "the model family that reads IPA")
        safe = html.escape(ipa, quote=True)
        body: dict = {"text": f'<phoneme alphabet="ipa" ph="{safe}">{safe}</phoneme>',
                      "model_id": ELEVENLABS_PHONEME_MODEL}
        if clamped != 1.0:
            body["voice_settings"] = {"speed": clamped}
        audio = np.frombuffer(self._post(voice.id, body), dtype="<i2").astype(np.float32) / 32768.0
        np.save(cached, audio)
        return audio

    def _post(self, voice_id: str, body: dict) -> bytes:
        if not self.key:
            raise RuntimeError(NO_KEY)
        request = urllib.request.Request(
            self.url.format(voice=voice_id), data=json.dumps(body).encode(), method="POST",
            headers={"xi-api-key": self.key, "Content-Type": "application/json"},
        )
        for attempt in range(5):
            try:
                with urllib.request.urlopen(request, timeout=120) as resp:
                    data = resp.read()
                    cost = resp.headers.get("character-cost")
                try:
                    credits = float(cost) if cost else None
                except ValueError:
                    credits = None
                self.usage.sent(body["model_id"], len(body["text"]), credits)
                return data
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                try:
                    detail = json.loads(detail)["detail"]["message"]
                except (ValueError, KeyError, TypeError):
                    pass
                if e.code in (400, 422) and "language" in str(detail).lower() and "language_code" in body:
                    raise LanguageRejected(str(detail)) from None
                raise RuntimeError(f"ElevenLabs request failed ({e.code}): {detail}") from None
        raise RuntimeError("ElevenLabs request failed after retries")


class Prefetching:
    """Wraps a backend: starts synthesising known requests in the background, in order.

    `synth` returns a request's result once it is ready (so the caller can
    get on with earlier scenes meanwhile) and falls back to the backend for
    anything not prefetched.
    """

    def __init__(self, backend: Synth, requests: list[tuple[str, Voice, float]], notes: set[str]):
        from concurrent.futures import ThreadPoolExecutor

        self.backend = backend
        self.name = backend.name
        self.speed_range = backend.speed_range
        self._pool = ThreadPoolExecutor(getattr(backend, "concurrency", 1))
        self._futures = {r: self._pool.submit(backend.synth, *r, notes) for r in dict.fromkeys(requests)}

    def resolve(self, name: str | None, lang: str | None, notes: set[str]) -> Voice:
        return self.backend.resolve(name, lang, notes)

    def synth(self, text: str, voice: Voice, speed: float, notes: set[str], only_cached: bool = False) -> np.ndarray:
        if only_cached:
            return self.backend.synth(text, voice, speed, notes, only_cached=True)
        future = self._futures.get((text, voice, speed))
        return future.result() if future else self.backend.synth(text, voice, speed, notes)

    def close(self) -> None:
        self._pool.shutdown(cancel_futures=True)


def make_synth(name: str, model: str | None = None, planning: bool = False) -> Synth:
    """The engine called `name`. With `planning`, one used only to time and price a script, from the
    cache and estimates: ElevenLabs then works without a key, and refuses to synthesise (#71)."""
    if name == "auto":
        name = "elevenlabs" if os.environ.get("ELEVENLABS_API_KEY") else "kokoro"
    if name == "kokoro":
        return KokoroSynth()
    if name == "elevenlabs":
        return ElevenLabsSynth(model, planning=planning)
    if name == "silent":
        return SilentSynth()
    raise ValueError(f"unknown TTS backend {name!r}")
