"""A run's records: the ledger of what was spent, the jobs submitted, and the portraits picked.

The ledger is append-only JSON lines, one entry per finished paid call: what it was estimated to cost,
what the service reported, and the cost counted. A job is recorded the moment it's submitted, so a run
that stops can poll it again instead of paying for it twice.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml


class OverBudget(RuntimeError):
    pass


class Store:
    def __init__(self, run: Path, budget: float):
        self.run = run
        self.budget = budget
        run.mkdir(parents=True, exist_ok=True)
        self.ledger_path = run / "ledger.jsonl"
        self.jobs_path = run / "jobs.json"
        self.picks_path = run / "picks.yaml"

    # -- money ---------------------------------------------------------------------------------------

    def entries(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        return [json.loads(line) for line in self.ledger_path.read_text().splitlines() if line.strip()]

    def spent(self) -> float:
        return round(sum(e["cost_usd"] for e in self.entries()), 4)

    def committed(self) -> float:
        """Spent, plus what jobs submitted but not finished will cost."""
        return round(self.spent() + sum(j["est_usd"] for j in self.jobs().values()), 4)

    def allow(self, est_usd: float, what: str) -> None:
        if self.committed() + est_usd > self.budget + 1e-9:
            raise OverBudget(f"{what} would take spending to ${self.committed() + est_usd:.2f}, "
                             f"over the ${self.budget:.2f} budget in plan.yaml")

    def record(self, key: str, service: str, est_usd: float, reported: dict | None = None,
               cost_usd: float | None = None, status: str = "done", note: str = "") -> None:
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "key": key, "service": service, "status": status,
                 "est_usd": est_usd, "reported": reported or {}, "cost_usd": est_usd if cost_usd is None else cost_usd,
                 "note": note}
        with self.ledger_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    # -- jobs submitted, not yet collected --------------------------------------------------------------

    def jobs(self) -> dict[str, dict]:
        return json.loads(self.jobs_path.read_text()) if self.jobs_path.exists() else {}

    def submitted(self, key: str, service: str, job: dict, est_usd: float) -> None:
        jobs = self.jobs()
        jobs[key] = {"service": service, "job": job, "est_usd": est_usd, "submitted": time.time()}
        self._write_jobs(jobs)

    def collected(self, key: str) -> None:
        jobs = self.jobs()
        jobs.pop(key, None)
        self._write_jobs(jobs)

    def _write_jobs(self, jobs: dict) -> None:
        part = self.jobs_path.with_suffix(".part")
        part.write_text(json.dumps(jobs, indent=2) + "\n")
        part.replace(self.jobs_path)

    # -- portraits picked ---------------------------------------------------------------------------------

    def picks(self) -> dict[str, int]:
        if not self.picks_path.exists():
            return {}
        return yaml.safe_load(self.picks_path.read_text()) or {}

    def pick(self, model: str, gender: str, candidate: int) -> None:
        picks = self.picks()
        picks[f"{model}-{gender}"] = candidate
        self.picks_path.write_text(yaml.safe_dump(picks, sort_keys=True))
