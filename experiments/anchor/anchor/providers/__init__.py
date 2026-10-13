"""The services the experiment calls, one adapter each, behind two small interfaces (base.py).

ADAPTERS maps a plan.yaml `provider` to its adapter; the real ones load only when used, so the plan
can be checked without their dependencies.
"""

from __future__ import annotations

from importlib import import_module

from .base import Service

ADAPTERS = {
    "fake-image": "fake:FakeImage",
    "fake-video": "fake:FakeVideo",
    "openai": "openai:OpenAIImage",
    "bfl": "bfl:BFLImage",
    "google": "google:GeminiImage",
    "byteplus-image": "byteplus:SeedreamImage",
    "omnihuman": "byteplus:OmniHuman",
    "hedra": "hedra:HedraAvatar",
    "heygen": "heygen:HeyGenAvatar",
    "omni": "google:OmniVideo",
    "kling-avatar": "kling:KlingAvatar",
    "kling-video": "kling:KlingVideo",
}


def adapter(provider: str) -> type[Service]:
    if provider not in ADAPTERS:
        raise KeyError(f"no adapter for provider {provider!r} (known: {', '.join(sorted(ADAPTERS))})")
    module, name = ADAPTERS[provider].split(":")
    return getattr(import_module(f".{module}", __name__), name)
