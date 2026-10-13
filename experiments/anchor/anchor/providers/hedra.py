"""Hedra API v3, the Hedra Avatar model: one image plus our audio, lip-synced.

Files are uploaded first (POST /files; their URLs last an hour), then POST /models/hedra-avatar with
start_image, audio, prompt, 16:9 and 1080p; GET /jobs/{id}/status until COMPLETED, then the output URL.
6.25 cents a second at 1080p; the free /estimate call records the price before submitting. The
idempotency key is derived from the call, so a resubmission isn't charged twice.
Its acceptable-use policy bars media of "individuals other than yourself" and "fake personas": ask
Hedra whether a generated anchor is allowed before relying on it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import requests

from ..uploads import portrait_jpeg
from .base import Made, Status, VideoService, check

BASE = "https://api.hedra.com/v3"


class HedraAvatar(VideoService):
    needs = ("HEDRA_API_KEY",)  # "<key_id>:<secret>"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Key {self.key('HEDRA_API_KEY')}"}

    def _upload(self, path: Path) -> str:
        with path.open("rb") as f:
            data = check(requests.post(f"{BASE}/files", headers=self._headers(), files={"file": f}, timeout=120),
                         "Hedra upload")
        return data["url"]

    def submit(self, inputs: dict[str, Path | str], seconds: float | None) -> dict[str, Any]:
        portrait, audio = portrait_jpeg(Path(inputs["portrait"])), Path(inputs["audio"])
        body = {"prompt": str(inputs.get("prompt", "")), "aspect_ratio": self.settings.get("aspect", "16:9"),
                "resolution": self.settings.get("resolution", "1080p"),
                "start_image": {"source": "url", "url": self._upload(portrait)},
                "audio": {"source": "url", "url": self._upload(audio)}}
        estimate = requests.post(f"{BASE}/models/hedra-avatar/estimate", headers=self._headers(),
                                 json={"input": body}, timeout=30)
        idem = hashlib.sha256(f"{portrait}|{audio}|{body['prompt']}".encode()).hexdigest()[:32]
        data = check(requests.post(f"{BASE}/models/hedra-avatar", headers=self._headers(), timeout=60,
                                   json={"input": body, "idempotency_key": idem}), "Hedra submit")
        return {"job_id": data["job_id"], "estimate": estimate.json() if estimate.ok else None, "seconds": seconds}

    def poll(self, job: dict[str, Any]) -> Status:
        state = check(requests.get(f"{BASE}/jobs/{job['job_id']}/status", headers=self._headers(), timeout=30),
                      "Hedra status").get("status")
        if state not in ("COMPLETED", "FAILED"):
            return Status("running")
        data = check(requests.get(f"{BASE}/jobs/{job['job_id']}", headers=self._headers(), timeout=30), "Hedra job")
        outputs = data.get("outputs") or []
        if data.get("status") == "FAILED" or not outputs or outputs[0].get("status") != "COMPLETED":
            return Status("failed", message=f"Hedra: {data.get('error') or outputs}")
        estimate = job.get("estimate") or {}
        cost = estimate.get("cost")
        return Status("done", Made(url=outputs[0]["url"], reported={"estimate": estimate},
                                   cost_usd=float(cost) if isinstance(cost, (int, float)) else None))
