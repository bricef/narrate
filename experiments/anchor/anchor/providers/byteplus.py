"""BytePlus: Seedream 5.0 images (ModelArk) and OmniHuman 1.5 talking avatars (Vision AI).

Seedream: OpenAI-style images/generations with an API key; 2048x1152 costs $0.045 (up to 2.61 MP); the
"AI-generated" watermark is on unless turned off; no seed. OmniHuman 1.5: CVSubmitTask / CVGetResult with
req_key realman_avatar_picture_omni15_cv; the image and audio go by public URL (uploads.publish); audio
under 60 s; one job at a time; about 27x real time at 1080p; the video URL lasts an hour; $0.12/s.
Authentication: an API key where the account has one, else access-key request signing (HMAC-SHA256).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import requests

from ..uploads import portrait_jpeg, publish
from .base import ImageService, Made, ServiceError, Status, VideoService, check

ARK = "https://ark.ap-southeast.bytepluses.com/api/v3"
CV_KEY_BASE = "https://openapi.cv.bytepluses.com"
CV_SIGNED_HOST, REGION, SERVICE, VERSION = "cv.byteplusapi.com", "ap-singapore-1", "cv", "2024-06-06"
PATHS = {"CVSubmitTask": "/api/common/v3/submit_task", "CVGetResult": "/api/common/v3/get_result"}
OMNI15 = "realman_avatar_picture_omni15_cv"


class SeedreamImage(ImageService):
    needs = ("ARK_API_KEY",)

    def generate(self, prompt: str, aspect: str) -> Made:
        body = {"model": self.settings["model"], "prompt": prompt, "size": self.settings["size"],
                "output_format": "png", "response_format": "b64_json", "watermark": False}
        response = requests.post(f"{ARK}/images/generations", json=body, timeout=180,
                                 headers={"Authorization": f"Bearer {self.key('ARK_API_KEY')}"})
        data = check(response, "Seedream")
        if "error" in data:
            raise ServiceError(f"Seedream: {data['error']}")
        return Made(data=base64.b64decode(data["data"][0]["b64_json"]), reported={"usage": data.get("usage")},
                    cost_usd=float(self.settings["est_usd"]))


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def signed_headers(ak: str, sk: str, query: str, payload: str, now: datetime.datetime) -> dict[str, str]:
    """BytePlus Vision's HMAC-SHA256 request signature (the raw secret key, no prefix)."""
    xdate, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    phash = hashlib.sha256(payload.encode()).hexdigest()
    signed = "content-type;host;x-content-sha256;x-date"
    canonical = "\n".join(["POST", "/", query, f"content-type:application/json\nhost:{CV_SIGNED_HOST}\n"
                           f"x-content-sha256:{phash}\nx-date:{xdate}\n", signed, phash])
    scope = f"{day}/{REGION}/{SERVICE}/request"
    to_sign = "\n".join(["HMAC-SHA256", xdate, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(sk.encode(), day)
    for part in (REGION, SERVICE, "request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Date": xdate, "X-Content-Sha256": phash,
            "Authorization": f"HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed}, Signature={signature}"}


class OmniHuman(VideoService):
    in_flight = 1  # the account's concurrency limit

    def missing(self) -> list[str]:
        if self.env.get("BYTEPLUS_CV_API_KEY") or (self.env.get("BYTEPLUS_AK") and self.env.get("BYTEPLUS_SK")):
            return []
        return ["BYTEPLUS_CV_API_KEY (or BYTEPLUS_AK and BYTEPLUS_SK)"]

    def _call(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.env.get("BYTEPLUS_CV_API_KEY"):
            response = requests.post(CV_KEY_BASE + PATHS[action], json=body, timeout=60,
                                     headers={"Authorization": f"Bearer {self.env['BYTEPLUS_CV_API_KEY']}"})
        else:
            payload, query = json.dumps(body), f"Action={action}&Version={VERSION}"
            headers = signed_headers(self.key("BYTEPLUS_AK"), self.key("BYTEPLUS_SK"), query, payload,
                                     datetime.datetime.now(datetime.timezone.utc))
            response = requests.post(f"https://{CV_SIGNED_HOST}/?{query}", headers=headers, data=payload, timeout=60)
        data = check(response, f"OmniHuman {action}")
        if data.get("code") != 10000:
            raise ServiceError(f"OmniHuman {action}: {data.get('code')} {data.get('message') or data}")
        return data

    def submit(self, inputs: dict[str, Path | str], seconds: float | None) -> dict[str, Any]:
        how = self.settings.get("uploads", "litterbox")
        body = {"req_key": OMNI15, "image_url": publish(portrait_jpeg(Path(inputs["portrait"])), how),
                "audio_url": publish(Path(inputs["audio"]), how),
                "output_resolution": int(str(self.settings.get("resolution", "1080p")).rstrip("p"))}
        if inputs.get("prompt"):
            body["prompt"] = str(inputs["prompt"])
        return {"task_id": self._call("CVSubmitTask", body)["data"]["task_id"], "seconds": seconds}

    def poll(self, job: dict[str, Any]) -> Status:
        try:
            data = self._call("CVGetResult", {"req_key": OMNI15, "task_id": job["task_id"]})["data"]
        except ServiceError as e:
            return Status("failed", message=str(e))
        state = data.get("status")
        if state in ("in_queue", "generating"):
            return Status("running")
        if state == "done":
            url = json.loads(data["resp_data"])["video_url"]
            seconds = job.get("seconds") or 0
            return Status("done", Made(url=url, reported={"seconds": seconds},
                                       cost_usd=round(seconds * float(self.settings["est_usd_per_s"]), 4)))
        return Status("failed", message=f"OmniHuman: {state}")
