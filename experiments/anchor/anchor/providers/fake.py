"""Stand-in services for the tests: no network, nothing spent, files that are just labels."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import ImageService, Made, Status, VideoService


class FakeImage(ImageService):
    def generate(self, prompt: str, aspect: str) -> Made:
        return Made(data=f"fake image: {aspect}: {prompt}".encode(), reported={"images": 1})


class FakeVideo(VideoService):
    polls_before_done = 1
    submitted: list[dict] = []

    def submit(self, inputs: dict[str, Path | str], seconds: float | None) -> dict[str, Any]:
        job = {"id": f"fake-{len(FakeVideo.submitted) + 1}", "seconds": seconds,
               "inputs": {k: str(v) for k, v in inputs.items()}, "polls": 0}
        FakeVideo.submitted.append(job)
        return job

    def poll(self, job: dict[str, Any]) -> Status:
        job["polls"] += 1
        if job["polls"] <= self.polls_before_done:
            return Status("running")
        return Status("done", Made(data=b"fake video for " + job["inputs"]["portrait"].encode(),
                                   reported={"seconds": job["seconds"]}))
