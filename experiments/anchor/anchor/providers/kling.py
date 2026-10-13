"""Kling (api-singapore.klingai.com): Avatar (one image plus our audio) and Kling 3.0 image-to-video.

Authentication: the API key (Bearer), which every endpoint takes; the older access-key/secret JWT
(HS256, iss = access key) works on the /v1 endpoints only. Inputs go as raw base64 (no data: prefix):
the image up to 10 MB, the audio file up to 5 MB and 2–300 s. Avatar's 1080p is its pro mode
($0.112/s; std is 720p at $0.056/s, inferred from pricing). The task reports the units it deducted
(final_unit_deduction), at $0.14 a unit. API access is sold as prepaid resource packs.
"""

from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path
from typing import Any

import jwt
import requests

from ..uploads import portrait_jpeg
from .base import Made, ServiceError, Status, VideoService, check

BASE = "https://api-singapore.klingai.com"
USD_PER_UNIT = 0.14


class Kling(VideoService):
    def missing(self) -> list[str]:
        if self.env.get("KLING_API_KEY") or (self.env.get("KLING_ACCESS_KEY") and self.env.get("KLING_SECRET_KEY")):
            return []
        return ["KLING_API_KEY (or KLING_ACCESS_KEY and KLING_SECRET_KEY)"]

    def headers(self) -> dict[str, str]:
        if self.env.get("KLING_API_KEY"):
            return {"Authorization": f"Bearer {self.env['KLING_API_KEY']}"}
        now = int(time.time())
        token = jwt.encode({"iss": self.key("KLING_ACCESS_KEY"), "exp": now + 1800, "nbf": now - 5},
                           self.key("KLING_SECRET_KEY"), algorithm="HS256", headers={"alg": "HS256", "typ": "JWT"})
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def b64(path: Path) -> str:
        return base64.b64encode(path.read_bytes()).decode()

    @staticmethod
    def ok(body: dict[str, Any], what: str) -> dict[str, Any]:
        if body.get("code") != 0:
            raise ServiceError(f"Kling {what}: {body.get('code')} {body.get('message')}")
        return body["data"]


class KlingAvatar(Kling):
    PATH = "/v1/videos/avatar/image2video"

    def submit(self, inputs: dict[str, Path | str], seconds: float | None) -> dict[str, Any]:
        portrait, audio = portrait_jpeg(Path(inputs["portrait"])), Path(inputs["audio"])
        body = {"image": self.b64(portrait), "sound_file": self.b64(audio), "prompt": str(inputs.get("prompt", "")),
                "mode": self.settings.get("mode", "pro"),
                "external_task_id": hashlib.sha256(f"{portrait}|{audio}".encode()).hexdigest()[:32]}
        data = self.ok(check(requests.post(BASE + self.PATH, json=body, headers=self.headers(), timeout=120),
                             "Kling avatar"), "avatar")
        return {"task_id": data["task_id"], "seconds": seconds}

    def poll(self, job: dict[str, Any]) -> Status:
        data = self.ok(check(requests.get(f"{BASE}{self.PATH}/{job['task_id']}", headers=self.headers(), timeout=30),
                             "Kling status"), "status")
        state = data.get("task_status")
        if state == "succeed":
            units = data.get("final_unit_deduction")
            return Status("done", Made(url=data["task_result"]["videos"][0]["url"], reported={"units": units},
                                       cost_usd=round(float(units) * USD_PER_UNIT, 4) if units else None))
        if state == "failed":
            return Status("failed", message=f"Kling: {data.get('task_status_msg')}")
        return Status("running")


class KlingVideo(Kling):
    """Kling 3.0 image-to-video from the portrait, silent, one shot (phase 4)."""

    def submit(self, inputs: dict[str, Path | str], seconds: float | None) -> dict[str, Any]:
        body = {"contents": [{"type": "prompt", "text": str(inputs.get("prompt", ""))},
                             {"type": "first_frame", "url": self.b64(portrait_jpeg(Path(inputs["portrait"])))}],
                "settings": {"resolution": "1080p", "duration": int(seconds or 5), "audio": "off", "multi_shot": False}}
        data = self.ok(check(requests.post(f"{BASE}/image-to-video/kling-3.0", json=body, headers=self.headers(),
                                           timeout=120), "Kling 3.0"), "3.0")
        return {"id": data["id"], "seconds": seconds}

    def poll(self, job: dict[str, Any]) -> Status:
        tasks = self.ok(check(requests.get(f"{BASE}/tasks", params={"task_ids": job["id"]}, headers=self.headers(),
                                           timeout=30), "Kling task"), "task")
        task = tasks[0] if tasks else {}
        if task.get("status") == "succeeded":
            url = next(o["url"] for o in task.get("outputs", []) if o.get("type") == "video")
            seconds = job.get("seconds") or 0
            return Status("done", Made(url=url, reported={"task": {k: task.get(k) for k in ("status", "units")}},
                                       cost_usd=round(seconds * float(self.settings["est_usd_per_s"]), 4)))
        if task.get("status") == "failed":
            return Status("failed", message=f"Kling 3.0: {task}")
        return Status("running")
