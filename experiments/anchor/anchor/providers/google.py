"""Google Gemini API: Nano Banana images (generateContent) and Gemini Omni Flash 1.1 clips (interactions).

16:9 at 2K is 2752x1536. Each call returns one final image (interim "thought" images are skipped and not
charged); it comes back as JPEG, so it's re-saved as PNG like every portrait. The cost is tokens: image
output at $30 per million (1680 tokens for a 2K image) and thinking at $7.50, from usageMetadata.
There's no person setting for images, and every image carries a SynthID watermark.
"""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from .base import ImageService, Made, ServiceError, Status, VideoService, check

BASE = "https://generativelanguage.googleapis.com/v1beta"
PRICE = {"gemini-nano-banana-2.1": (30.0, 7.5), "gemini-3-pro-image": (120.0, 12.0)}  # $/M: image, text+thinking


def png(data: bytes) -> bytes:
    out = io.BytesIO()
    Image.open(io.BytesIO(data)).save(out, "PNG")
    return out.getvalue()


def cost(model: str, usage: dict) -> float | None:
    rates = PRICE.get(model)
    if not rates or not usage:
        return None
    image = sum(d.get("tokenCount", 0) for d in usage.get("candidatesTokensDetails", []) if d.get("modality") == "IMAGE")
    text = usage.get("thoughtsTokenCount", 0) + sum(
        d.get("tokenCount", 0) for d in usage.get("candidatesTokensDetails", []) if d.get("modality") == "TEXT")
    return round(image * rates[0] / 1e6 + text * rates[1] / 1e6, 4)


class GeminiImage(ImageService):
    needs = ("GEMINI_API_KEY",)

    def generate(self, prompt: str, aspect: str) -> Made:
        model = self.settings["model"]
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["IMAGE"],
                                     "imageConfig": {"aspectRatio": aspect, "imageSize": self.settings.get("size", "2K")}}}
        data = check(requests.post(f"{BASE}/models/{model}:generateContent", json=body, timeout=600,
                                   headers={"x-goog-api-key": self.key("GEMINI_API_KEY")}), "Gemini image")
        if not data.get("candidates"):
            raise ServiceError(f"Gemini image: blocked: {data.get('promptFeedback')}")
        candidate = data["candidates"][0]
        images = [p.get("inlineData") or p.get("inline_data") for p in candidate.get("content", {}).get("parts", [])
                  if (p.get("inlineData") or p.get("inline_data")) and not p.get("thought")]
        if not images:
            raise ServiceError(f"Gemini image: no image (finishReason {candidate.get('finishReason')})")
        usage = data.get("usageMetadata", {})
        return Made(data=png(base64.b64decode(images[-1]["data"])), reported={"usage": usage},
                    cost_usd=cost(model, usage))


class OmniVideo(VideoService):
    """Gemini Omni Flash 1.1 image-to-video, for shots with movement (phase 4).

    The interactions call is synchronous: it returns when the clip is made, with a file URI for anything
    over 4 MB (1080p), which is downloaded with the key once the file is ACTIVE. Audio is always generated
    (the clip is used silent). Google warns plain requests can drop after about 60 seconds; untested here.
    Recognisable real people are blocked; a generated face isn't one. 1080p is upscaled.
    """

    needs = ("GEMINI_API_KEY",)
    in_flight = 1

    @property
    def download_headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.key("GEMINI_API_KEY")}

    def submit(self, inputs: dict[str, Path | str], seconds: float | None) -> dict[str, Any]:
        first = Path(inputs["portrait"]).read_bytes()
        body = {"model": self.settings["model"], "store": False,
                "input": [{"type": "image", "data": base64.b64encode(first).decode(), "mime_type": "image/png"},
                          {"type": "text", "text": f"<FIRST_FRAME> {inputs.get('prompt', '')}"}],
                "response_format": {"type": "video", "aspect_ratio": "16:9", "resolution": "1080p",
                                    "duration": f"{int(seconds or 8)}s", "delivery": "uri"},
                "generation_config": {"video_config": {"task": "image_to_video"}}}
        data = check(requests.post(f"{BASE}/interactions", json=body, timeout=1200, headers=self.download_headers),
                     "Omni")
        if data.get("status") != "completed":
            raise ServiceError(f"Omni: {data.get('status')}: {data}")
        videos = [c for step in data.get("steps", []) if step.get("type") == "model_output"
                  for c in step.get("content", []) if c.get("type") == "video"]
        if not videos or not videos[-1].get("uri"):
            raise ServiceError(f"Omni: no video URI in the response: {data}")
        return {"file": re.search(r"files/([^:/?]+)", videos[-1]["uri"]).group(1), "usage": data.get("usage"),
                "seconds": seconds}

    def poll(self, job: dict[str, Any]) -> Status:
        state = check(requests.get(f"{BASE}/files/{job['file']}", headers=self.download_headers, timeout=60),
                      "Omni file").get("state")
        if state == "ACTIVE":
            seconds = job.get("seconds") or 0
            return Status("done", Made(url=f"{BASE}/files/{job['file']}:download?alt=media",
                                       reported={"usage": job.get("usage")},
                                       cost_usd=round(seconds * float(self.settings["est_usd_per_s"]), 4)))
        if state == "FAILED":
            return Status("failed", message="Omni: the file failed")
        return Status("running")
