"""Google Gemini API: Nano Banana images (generateContent).

16:9 at 2K is 2752x1536. Each call returns one final image (interim "thought" images are skipped and not
charged); it comes back as JPEG, so it's re-saved as PNG like every portrait. The cost is tokens: image
output at $30 per million (1680 tokens for a 2K image) and thinking at $7.50, from usageMetadata.
There's no person setting for images, and every image carries a SynthID watermark.
"""

from __future__ import annotations

import base64
import io

import requests
from PIL import Image

from .base import ImageService, Made, ServiceError, check

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
