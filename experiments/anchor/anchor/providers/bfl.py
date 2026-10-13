"""Black Forest Labs FLUX (api.bfl.ai): submit, poll the returned polling_url, download result.sample.

FLUX 3 Image takes an aspect ratio and a resolution tier, not exact pixels, and nothing else: unknown
fields are refused. FLUX.2 [pro] takes exact width and height, a seed, and disable_pup (its prompt
rewriting is on unless turned off). The submit response's `cost` is in credits, 1 credit = $0.01.
Signed result URLs expire within minutes, so the image is fetched at once.
"""

from __future__ import annotations

import time

import requests

from .base import ImageService, Made, ServiceError, check

HOST = "https://api.bfl.ai"
RUNNING = {"Pending", "Reasoning", "Generating"}


class BFLImage(ImageService):
    needs = ("BFL_API_KEY",)

    def _body(self, prompt: str, aspect: str) -> dict:
        s = self.settings
        if s["model"] == "flux-3-image":
            return {"prompt": prompt, "aspect_ratio": aspect, "resolution": s.get("resolution", "2k"),
                    "safety_tolerance": 2, "grounding": False}
        body = {"prompt": prompt, "width": int(s["width"]), "height": int(s["height"]), "output_format": "png",
                "safety_tolerance": 2, "disable_pup": True}
        if "seed" in s:
            body["seed"] = int(s["seed"])
        return body

    def generate(self, prompt: str, aspect: str) -> Made:
        headers = {"x-key": self.key("BFL_API_KEY"), "accept": "application/json"}
        submitted = check(requests.post(f"{HOST}/v1/{self.settings['model']}", json=self._body(prompt, aspect),
                                        headers=headers, timeout=60), "BFL submit")
        deadline = time.monotonic() + 600
        while True:
            body = requests.get(submitted["polling_url"], headers=headers, timeout=60).json()
            status = body.get("status")
            if status == "Ready":
                image = requests.get(body["result"]["sample"], timeout=120)  # signed: no x-key
                if image.status_code >= 400:
                    raise ServiceError(f"BFL download: HTTP {image.status_code}")
                credits = submitted.get("cost")
                return Made(data=image.content, reported={"credits": credits, "output_mp": submitted.get("output_mp"),
                                                          "id": submitted.get("id")},
                            cost_usd=round(credits / 100, 4) if isinstance(credits, (int, float)) else None)
            if status not in RUNNING:
                raise ServiceError(f"BFL: {status}: {body.get('details') or body}")
            if time.monotonic() > deadline:
                raise ServiceError("BFL: still running after 10 minutes")
            time.sleep(1.5)
