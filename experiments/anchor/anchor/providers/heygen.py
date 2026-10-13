"""HeyGen API v3, Avatar IV from a photo: one image plus our audio, lip-synced.

Upload the image and the audio (POST /v3/assets), then POST /v3/videos with type "image" (always Avatar
IV), the audio asset, 1080p and 16:9; GET /v3/videos/{id} until completed. The v1/v2 endpoints retire on
2026-10-31, so only v3 is used. Pay as you go; the price is unconfirmed ($2.31/min in the help centre,
$0.05/s on HeyGen's blog), so the plan estimates the higher. No consent step for synthetic faces, but
its moderation policy bans political content.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import requests

from ..uploads import portrait_jpeg
from .base import Made, Status, VideoService, check

BASE = "https://api.heygen.com"


class HeyGenAvatar(VideoService):
    needs = ("HEYGEN_API_KEY",)

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.key("HEYGEN_API_KEY")}

    def _upload(self, path: Path) -> str:
        with path.open("rb") as f:
            data = check(requests.post(f"{BASE}/v3/assets", headers=self._headers(), files={"file": f}, timeout=120),
                         "HeyGen upload")
        return data["data"]["asset_id"]

    def submit(self, inputs: dict[str, Path | str], seconds: float | None) -> dict[str, Any]:
        portrait, audio = portrait_jpeg(Path(inputs["portrait"])), Path(inputs["audio"])
        idem = hashlib.sha256(f"{portrait}|{audio}".encode()).hexdigest()[:32]
        body = {"type": "image", "image": {"type": "asset_id", "asset_id": self._upload(portrait)},
                "audio_asset_id": self._upload(audio), "resolution": "1080p", "aspect_ratio": "16:9",
                "title": f"anchor-{idem[:12]}"}
        data = check(requests.post(f"{BASE}/v3/videos", headers={**self._headers(), "Idempotency-Key": idem},
                                   json=body, timeout=60), "HeyGen submit")
        return {"video_id": data["data"]["video_id"], "seconds": seconds}

    def poll(self, job: dict[str, Any]) -> Status:
        data = check(requests.get(f"{BASE}/v3/videos/{job['video_id']}", headers=self._headers(), timeout=30),
                     "HeyGen status")["data"]
        if data.get("status") == "completed":
            return Status("done", Made(url=data["video_url"], reported={k: data.get(k) for k in ("duration", "status")}))
        if data.get("status") == "failed":
            return Status("failed", message=f"HeyGen: {data.get('failure_code')}: {data.get('failure_message')}")
        return Status("running")
