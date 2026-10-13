"""Inputs prepared for the avatar services, and published where a service only takes a URL.

Every avatar service gets the same copy of a portrait: 1920x1080 JPEG (cropped to fill, quality 92), which
keeps the comparison fair and stays inside upload limits such as OmniHuman's 5 MB.

Some services (OmniHuman) only fetch inputs from a public URL. The default publisher is Litterbox
(litterbox.catbox.moe): a free host whose links are unlisted and expire after an hour. The files are the
synthetic portrait and the narration audio; set `uploads: none` in plan.yaml to refuse publishing, and
those services then can't run.
"""

from __future__ import annotations

from pathlib import Path

import requests
from PIL import Image, ImageOps

from .providers.base import ServiceError

SIZE = (1920, 1080)
LITTERBOX = "https://litterbox.catbox.moe/resources/internals/api.php"


def portrait_jpeg(portrait: Path) -> Path:
    """The 1920x1080 JPEG every avatar service is given, beside the run's portraits."""
    out = portrait.parent.parent / "upload" / f"{portrait.stem}.jpg"
    if not out.exists() or out.stat().st_mtime < portrait.stat().st_mtime:
        out.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(portrait) as image:
            ImageOps.fit(image.convert("RGB"), SIZE, Image.LANCZOS).save(out, "JPEG", quality=92)
    return out


_published: dict[str, str] = {}


def publish(path: Path, how: str = "litterbox") -> str:
    """A public URL for `path`, valid for at least an hour."""
    if how == "none":
        raise ServiceError(f"{path.name}: this service needs a public URL, and plan.yaml says uploads: none")
    key = f"{path}:{path.stat().st_mtime_ns}"
    if key not in _published:
        with path.open("rb") as f:
            response = requests.post(LITTERBOX, data={"reqtype": "fileupload", "time": "1h"},
                                     files={"fileToUpload": (path.name, f)}, timeout=120)
        if response.status_code >= 400 or not response.text.startswith("https://"):
            raise ServiceError(f"publishing {path.name}: HTTP {response.status_code}: {response.text[:200]}")
        _published[key] = response.text.strip()
    return _published[key]
