"""OpenAI GPT Image (POST /v1/images/generations): one picture, returned as base64.

Sizes are custom, with both edges multiples of 16 (2048x1152 is a documented 16:9 size). There is no
seed. The cost is tokens: the response's `usage` is recorded when present, and the dollar figure
follows OpenAI's own calculator (docs/research and the spec in #87).
"""

from __future__ import annotations

import base64
import math

import requests

from .base import ImageService, Made, ServiceError, check

URL = "https://api.openai.com/v1/images/generations"
# Output tokens per quality, as OpenAI's calculator has them
BASE = {"gpt-image-2": {"low": 16, "medium": 48, "high": 96},
        "gpt-image-2.5": {"low": 16, "medium": 24, "high": 48, "xhigh": 64, "max": 96}}


def output_tokens(model: str, quality: str, width: int, height: int) -> int:
    family = "gpt-image-2.5" if model.startswith("gpt-image-2.5") else "gpt-image-2"
    base = BASE[family][quality]
    u = round(base / (max(width, height) / min(width, height)))  # Python rounds half to even, as the calculator does
    return math.ceil(base * u * (2_000_000 + width * height) / 4_000_000)


class OpenAIImage(ImageService):
    needs = ("OPENAI_API_KEY",)

    def generate(self, prompt: str, aspect: str) -> Made:
        s = self.settings
        width, height = (int(v) for v in s["size"].split("x"))
        body = {"model": s["model"], "prompt": prompt, "size": s["size"], "quality": s["quality"], "n": 1,
                "output_format": "png", "background": "opaque", "moderation": s.get("moderation", "auto")}
        response = requests.post(URL, json=body, timeout=300,
                                 headers={"Authorization": f"Bearer {self.key('OPENAI_API_KEY')}"})
        data = check(response, "OpenAI image")
        if not data.get("data"):
            raise ServiceError(f"OpenAI image: no image in the response: {data}")
        tokens = (data.get("usage") or {}).get("output_tokens") or output_tokens(s["model"], s["quality"], width, height)
        return Made(data=base64.b64decode(data["data"][0]["b64_json"]),
                    reported={"usage": data.get("usage"), "output_tokens": tokens},
                    cost_usd=round(tokens * 30 / 1_000_000, 4))
