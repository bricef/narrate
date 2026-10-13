"""Running the plan's calls: in order, resumably, within the budget, and only with --spend.

Audio is narrate's own (`narrate audio` with ElevenLabs). Pictures are made one at a time. Clips are
submitted up to each service's limit, recorded as they're submitted, then polled together; a run that
stops collects its jobs next time rather than paying for them again.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .plan import Call, Plan
from .providers.base import ImageService, Made, ServiceError, Service, VideoService, download
from .store import Store

Services = Callable[[Call], Service]


class Runner:
    def __init__(self, plan: Plan, store: Store, services: Services, spend: bool = False,
                 log: Callable[[str], None] = print, every: float = 10.0, limit: float = 1800.0):
        self.plan, self.store, self.services, self.spend, self.log = plan, store, services, spend, log
        self.every, self.limit = every, limit

    def run(self, calls: list[Call]) -> None:
        for call in calls:
            if call.kind == "audio":
                self._audio(call)
            elif call.kind == "image":
                self._image(call)
        self._videos([c for c in calls if c.kind == "video"])

    def _ready(self, call: Call) -> bool:
        if (self.store.run / call.key).exists():
            return False
        if call.waiting:
            self.log(f"  waiting  {call.key}: {call.waiting}")
            return False
        return True

    def _gate(self, call: Call) -> bool:
        if not self.spend:
            self.log(f"  would    {call.key}  (${call.est_usd:.3f}; add --spend to make it)")
            return False
        self.store.allow(call.est_usd, call.key)
        return True

    def _write(self, call: Call, made: Made, headers: dict[str, str] | None = None) -> Path:
        out = self.store.run / call.key
        out.parent.mkdir(parents=True, exist_ok=True)
        if made.data is not None:
            out.write_bytes(made.data)
        elif made.url:
            download(made.url, out, headers)
        else:
            raise ServiceError(f"{call.key}: the service returned no file")
        return out

    # -- audio: narrate's own speech --------------------------------------------------------------------

    def _audio(self, call: Call) -> None:
        if not self._ready(call) or not self._gate(call):
            return
        repo = self.plan.path.parent.parent.parent
        out = self.store.run / call.key
        out.parent.mkdir(parents=True, exist_ok=True)
        v = call.settings
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "line.md"
            script.write_text(f"---\nvoice: {call.inputs['voice']}\n---\n\n{call.inputs['text']}\n")
            usage = Path(tmp) / "usage.json"
            result = subprocess.run(
                ["uv", "run", "--project", str(repo), "narrate", "audio", str(script), "--tts", v["engine"],
                 "--tts-model", v["model"], "-o", str(out), "--usage", str(usage), "-q"],
                cwd=repo, capture_output=True, text=True)
            if result.returncode != 0:
                raise ServiceError(f"{call.key}: narrate audio failed: {result.stderr.strip()[-500:]}")
            reported = json.loads(usage.read_text()).get("speech") if usage.exists() else {}
        self.store.record(call.key, "narrate", 0.0, reported, cost_usd=0.0, note="ElevenLabs credits, not dollars")
        self.log(f"  made     {call.key}")

    # -- pictures ---------------------------------------------------------------------------------------------

    def _image(self, call: Call) -> None:
        if not self._ready(call) or not self._gate(call):
            return
        service = self.services(call)
        assert isinstance(service, ImageService)
        try:
            made = service.generate(call.inputs["prompt"], call.inputs["aspect"])
        except ServiceError as e:
            self.store.record(call.key, call.service, call.est_usd, {"error": str(e)}, cost_usd=0.0, status="failed")
            self.log(f"  failed   {call.key}: {e}")
            return
        self._write(call, made)
        self.store.record(call.key, call.service, call.est_usd, made.reported, made.cost_usd)
        self.log(f"  made     {call.key}  (${made.cost_usd if made.cost_usd is not None else call.est_usd:.3f})")

    # -- clips --------------------------------------------------------------------------------------------------

    def _videos(self, calls: list[Call]) -> None:
        jobs = self.store.jobs()
        queue = [c for c in calls if c.key not in jobs and self._ready(c)]
        active = {c.key: (c, jobs[c.key]["job"]) for c in calls if c.key in jobs
                  and not (self.store.run / c.key).exists()}
        for key in active:
            self.log(f"  resuming {key}")
        deadline = time.monotonic() + self.limit
        while queue or active:
            for call in list(queue):
                service = self.services(call)
                limit = int(call.settings.get("in_flight", getattr(service, "in_flight", 2)))
                if sum(1 for c, _ in active.values() if c.service == call.service) >= limit:
                    continue
                queue.remove(call)
                if not self._gate(call):
                    continue
                inputs = {name: (self.store.run / value if name in ("portrait", "audio") else value)
                          for name, value in call.inputs.items() if value}
                try:
                    job = service.submit(inputs, call.seconds)
                except ServiceError as e:
                    self.store.record(call.key, call.service, call.est_usd, {"error": str(e)}, cost_usd=0.0,
                                      status="failed")
                    self.log(f"  refused  {call.key}: {e}")
                    continue
                self.store.submitted(call.key, call.service, job, call.est_usd)
                active[call.key] = (call, job)
                self.log(f"  started  {call.key}  (${call.est_usd:.3f})")
            for key, (call, job) in list(active.items()):
                service = self.services(call)
                assert isinstance(service, VideoService)
                status = service.poll(job)
                if status.state == "running":
                    continue
                del active[key]
                self.store.collected(key)
                if status.state == "done" and status.made:
                    self._write(call, status.made, getattr(service, "download_headers", None))
                    self.store.record(key, call.service, call.est_usd, status.made.reported, status.made.cost_usd)
                    self.log(f"  made     {key}")
                else:
                    self.store.record(key, call.service, call.est_usd, {"error": status.message}, cost_usd=0.0,
                                      status="failed", note="failed jobs are usually not billed: check the service")
                    self.log(f"  failed   {key}: {status.message}")
            if active:
                if time.monotonic() > deadline:
                    self.log(f"  stopping: {len(active)} jobs still running; run again to collect them")
                    return
                time.sleep(self.every)

