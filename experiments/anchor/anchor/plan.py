"""The experiment's plan: plan.yaml, checked, and expanded into the calls it implies, each with its cost.

A call is one request to one service: an audio line, a portrait candidate, or a clip. Each has a key,
which is also where its output goes in the run's folder, so a run can stop and carry on: an output
already there is never asked for again.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

GENDERS = ("male", "female")
WORDS_PER_SECOND = 2.5  # an anchor's pace, for estimating a line's length before its audio exists


class PlanError(ValueError):
    pass


@dataclass
class Call:
    phase: int
    kind: str  # "audio", "image" or "video"
    key: str  # its output's path in the run's folder
    service: str  # the image model or video service's name in plan.yaml
    settings: dict[str, Any]  # that service's settings from plan.yaml
    inputs: dict[str, Any] = field(default_factory=dict)  # what this call sends: prompt, portrait, audio, …
    est_usd: float = 0.0
    seconds: float | None = None  # a clip's length, from its audio or its settings
    waiting: str | None = None  # why it can't run yet (a portrait not picked, audio not made)


@dataclass
class Plan:
    path: Path
    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> Plan:
        data = yaml.safe_load(path.read_text())
        plan = cls(path.resolve(), data)
        plan.check()
        return plan

    @property
    def budget(self) -> float:
        return float(self.data["budget_usd"])

    def run_dir(self, root: Path | None = None) -> Path:
        return (root or self.path.parent / "runs") / str(self.data["run"])

    def check(self) -> None:
        d = self.data
        for key in ("budget_usd", "run", "voices", "lines", "portrait", "image_models", "avatars", "phases"):
            if key not in d:
                raise PlanError(f"plan.yaml needs {key!r}")
        for gender in GENDERS:
            if gender not in d["voices"] or gender not in d["portrait"]["people"]:
                raise PlanError(f"plan.yaml needs a {gender} voice and person")
        for name, model in d["image_models"].items():
            if "provider" not in model or "est_usd" not in model:
                raise PlanError(f"image model {name!r} needs a provider and est_usd")
        for name, avatar in d["avatars"].items():
            if "provider" not in avatar or "est_usd_per_s" not in avatar:
                raise PlanError(f"avatar service {name!r} needs a provider and est_usd_per_s")
        for phase in ("2", "3"):
            for line in d["phases"][phase]["lines"]:
                if line not in d["lines"]:
                    raise PlanError(f"phase {phase} uses line {line!r}, which plan.yaml doesn't have")

    # -- expansion ---------------------------------------------------------------------------------

    def calls(self, run: Path, picks: dict[str, int] | None = None, phases: set[int] | None = None,
              chosen: list[str] | None = None) -> list[Call]:
        """Every call the plan implies, in order. `picks` are the chosen portrait candidates, by
        "model-gender"; `chosen` the avatar services phase 3 uses (the plan's, unless given)."""
        picks = picks or {}
        out: list[Call] = []
        want = phases or {0, 1, 2, 3, 4}
        d = self.data
        if 0 in want:
            out += self._audio(run)
        if 1 in want:
            out += self._portraits()
        if 2 in want:
            out += self._clips(2, run, picks, list(d["image_models"]), list(d["avatars"]), d["phases"]["2"]["lines"])
        if 3 in want:
            p3 = d["phases"]["3"]
            # Until they're chosen, phase 3 is costed with the dearest two services, so the plan doesn't under-estimate
            dearest = sorted(d["avatars"], key=lambda a: -float(d["avatars"][a]["est_usd_per_s"]))[:2]
            services = chosen or p3.get("avatars") or dearest
            out += self._clips(3, run, picks, None, services, p3["lines"], best=p3.get("portraits") or {})
        if 4 in want and d["phases"].get("4", {}).get("enabled"):
            out += self._movement(run, picks)
        return out

    def _audio(self, run: Path) -> list[Call]:
        v = self.data["voices"]
        return [Call(0, "audio", f"audio/{gender}-{line}.wav", "narrate", v,
                     {"text": text, "voice": v[gender]}, est_usd=0.0)
                for gender in GENDERS for line, text in self.data["lines"].items()]

    def _portraits(self) -> list[Call]:
        p = self.data["portrait"]
        calls = []
        for name, model in self.data["image_models"].items():
            for gender in GENDERS:
                prompt = " ".join(p["prompt"].split()).replace("{person}", p["people"][gender])
                for i in range(1, int(p["candidates"]) + 1):
                    calls.append(Call(1, "image", f"portraits/{name}-{gender}-{i}.png", name, model,
                                      {"prompt": prompt, "aspect": p["aspect"], "candidate": i},
                                      est_usd=float(model["est_usd"])))
        return calls

    def portrait(self, model: str, gender: str, picks: dict[str, int]) -> tuple[str | None, str | None]:
        """The chosen portrait's key, or why there isn't one yet."""
        pick = picks.get(f"{model}-{gender}")
        if pick is None:
            return None, f"pick a {gender} portrait from {model} (python -m anchor pick {model} {gender} N)"
        return f"portraits/{model}-{gender}-{pick}.png", None

    def _clips(self, phase: int, run: Path, picks: dict[str, int], models: list[str] | None, services: list[str],
               lines: list[str], best: dict[str, str] | None = None) -> list[Call]:
        calls = []
        avatars = self.data["avatars"]
        for service in services:
            if service not in avatars:
                raise PlanError(f"phase {phase} uses avatar service {service!r}, which plan.yaml doesn't have")
            settings = avatars[service]
            for gender in GENDERS:
                for model in (models if models is not None else [best.get(gender) or "unchosen"]):
                    if model == "unchosen":  # still costed, so the plan shows what phase 3 will take
                        portrait, waiting = None, f"choose phase 3's {gender} portrait in plan.yaml"
                    else:
                        portrait, waiting = self.portrait(model, gender, picks)
                    for line in lines:
                        audio = f"audio/{gender}-{line}.wav"
                        seconds = audio_seconds(run / audio) or self.estimate_seconds(line)
                        calls.append(Call(
                            phase, "video", f"clips/p{phase}/{service}--{model}-{gender}--{line}.mp4", service,
                            settings, {"portrait": portrait, "audio": audio,
                                       "prompt": " ".join(self.data.get("avatar_prompt", "").split())},
                            est_usd=round(float(settings["est_usd_per_s"]) * seconds, 4), seconds=seconds,
                            waiting=waiting or (None if (run / audio).exists() else "make the audio first")))
        return calls

    def _movement(self, run: Path, picks: dict[str, int]) -> list[Call]:
        p4 = self.data["phases"]["4"]
        calls = []
        for gender in GENDERS:
            model = (self.data["phases"]["3"].get("portraits") or {}).get(gender)
            portrait, waiting = self.portrait(model, gender, picks) if model else (None, "choose phase 3's portraits")
            for name, video in p4["video"].items():
                seconds = float(video["seconds"])
                calls.append(Call(4, "video", f"clips/p4/{name}--{model}-{gender}.mp4", name, video,
                                  {"portrait": portrait, "prompt": " ".join(p4["prompt"].split())},
                                  est_usd=round(float(video["est_usd_per_s"]) * seconds, 4), seconds=seconds,
                                  waiting=waiting))
        return calls

    def estimate_seconds(self, line: str) -> float:
        return round(len(self.data["lines"][line].split()) / WORDS_PER_SECOND, 1)


def audio_seconds(path: Path) -> float | None:
    if not path.exists():
        return None
    with wave.open(str(path)) as w:
        return round(w.getnframes() / w.getframerate(), 2)
