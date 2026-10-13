"""What every service adapter does.

An image service makes one picture from a prompt (waiting for it if the service is asynchronous). A
video service submits a job, which the runner records at once, and is then polled until the clip is
ready to download: a run that stops picks its jobs up again instead of paying twice.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

TIMEOUT = 120


class ServiceError(RuntimeError):
    """The service refused or failed; the message says why, in its own words where it gave them."""


@dataclass
class Made:
    """What a call produced: the file's bytes or where to fetch them, and what the service said it cost."""

    data: bytes | None = None
    url: str | None = None
    reported: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None  # when the service reported a cost in dollars, or one we can convert


@dataclass
class Status:
    state: str  # "running", "done" or "failed"
    made: Made | None = None
    message: str = ""


class Service:
    needs: tuple[str, ...] = ()  # the environment variables it needs (its keys)

    def __init__(self, settings: dict[str, Any], env: dict[str, str] | None = None):
        self.settings = settings
        self.env = os.environ if env is None else env

    def missing(self) -> list[str]:
        return [name for name in self.needs if not self.env.get(name)]

    def key(self, name: str) -> str:
        value = self.env.get(name)
        if not value:
            raise ServiceError(f"set {name}")
        return value


class ImageService(Service):
    def generate(self, prompt: str, aspect: str) -> Made:
        raise NotImplementedError


class VideoService(Service):
    in_flight = 2  # jobs submitted at once, unless the plan says otherwise

    def submit(self, inputs: dict[str, Path | str], seconds: float | None) -> dict[str, Any]:
        """Start a job; what to record to find it again. `inputs` holds local files (portrait, audio) and the prompt."""
        raise NotImplementedError

    def poll(self, job: dict[str, Any]) -> Status:
        raise NotImplementedError


def check(response: requests.Response, what: str) -> dict[str, Any]:
    """A JSON response, or a ServiceError with the service's own message."""
    try:
        body = response.json()
    except ValueError:
        body = {"text": response.text[:500]}
    if response.status_code >= 400:
        raise ServiceError(f"{what}: HTTP {response.status_code}: {body}")
    return body


def download(url: str, out: Path, headers: dict[str, str] | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(out.suffix + ".part")
    with requests.get(url, headers=headers or {}, stream=True, timeout=TIMEOUT) as r:
        if r.status_code >= 400:
            raise ServiceError(f"download {url}: HTTP {r.status_code}")
        with part.open("wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    part.replace(out)


def wait(poll, job: dict[str, Any], every: float = 5.0, limit: float = 900.0) -> Status:
    """Poll until the job finishes or `limit` seconds pass."""
    deadline = time.monotonic() + limit
    while True:
        status = poll(job)
        if status.state != "running" or time.monotonic() > deadline:
            return status
        time.sleep(every)
