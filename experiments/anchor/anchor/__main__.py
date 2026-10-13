"""python -m anchor: the experiment's commands. See README.md."""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

from .plan import GENDERS, Plan, PlanError
from .providers import adapter
from .providers.base import ServiceError
from .runner import Runner
from .store import OverBudget, Store

HERE = Path(__file__).resolve().parent.parent


def services_for(plan: Plan):
    cache = {}

    def service(call):
        if call.service not in cache:
            cache[call.service] = adapter(call.settings["provider"])(call.settings)
        return cache[call.service]

    return service


def show_plan(plan: Plan, store: Store, phases: set[int] | None, avatars: list[str] | None) -> None:
    calls = plan.calls(store.run, store.picks(), phases, avatars)
    totals: dict[int, float] = defaultdict(float)
    done_total = 0.0
    for call in calls:
        done = (store.run / call.key).exists()
        state = "done" if done else ("waiting" if call.waiting else "to do")
        if not done:
            totals[call.phase] += call.est_usd
        else:
            done_total += call.est_usd
        length = f" {call.seconds:5.1f}s" if call.seconds else "       "
        print(f"  {call.phase}  {state:7}  ${call.est_usd:6.3f}{length}  {call.key}")
        if call.waiting and not done:
            print(f"                              ↳ {call.waiting}")
    print()
    for phase in sorted(totals):
        print(f"  phase {phase}: ${totals[phase]:.2f} still to spend")
    print(f"  in all:  ${sum(totals.values()):.2f} still to spend; ${store.spent():.2f} spent so far; "
          f"budget ${plan.budget:.2f}")


def show_inputs(plan: Plan) -> None:
    """What each kind of call sends, once, so the inputs can be reviewed without the matrix's repetition."""
    calls = plan.calls(plan.run_dir(), {})
    print("Lines (phase 0), each in both voices:")
    for line, text in plan.data["lines"].items():
        print(f"  {line} (~{plan.estimate_seconds(line):.0f} s): {' '.join(text.split())}")
    print(f"\nVoices: {plan.data['voices']}")
    for gender in GENDERS:
        prompt = next(c.inputs["prompt"] for c in calls if c.kind == "image" and gender in c.key)
        print(f"\nPortrait prompt ({gender}):\n  {prompt}")
    print(f"\nAvatar prompt (where a service takes one):\n  {' '.join(plan.data['avatar_prompt'].split())}")
    print("\nImage models:")
    for name, settings in plan.data["image_models"].items():
        print(f"  {name}: {settings}")
    print("\nAvatar services:")
    for name, settings in plan.data["avatars"].items():
        print(f"  {name}: {settings}")


def check_keys(plan: Plan) -> int:
    missing = 0
    settings = list(plan.data["image_models"].items()) + list(plan.data["avatars"].items())
    for name, s in settings:
        try:
            service = adapter(s["provider"])(s)
        except KeyError as e:
            print(f"  {name:12} no adapter yet ({e})")
            missing += 1
            continue
        need = service.missing()
        missing += bool(need)
        print(f"  {name:12} {'ready' if not need else 'needs ' + ', '.join(need)}")
    return 1 if missing else 0


def load_env(path: Path) -> None:
    """KEY=VALUE lines from the experiment's .env (git-ignored); variables already set win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main(argv: list[str] | None = None) -> int:
    load_env(HERE / ".env")
    parser = argparse.ArgumentParser(prog="python -m anchor", description=__doc__)
    parser.add_argument("--plan", default=str(HERE / "plan.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="every call the plan implies, and its estimated cost (no network)")
    p.add_argument("--phase", type=int, action="append", help="only these phases")
    p.add_argument("--avatars", help="phase 3's avatar services, comma-separated, over plan.yaml's")
    sub.add_parser("inputs", help="what each kind of call sends, for review")
    sub.add_parser("keys", help="which services have their keys set")
    for name, helptext in (("audio", "phase 0: the lines, in both voices"),
                           ("portraits", "phase 1: portrait candidates")):
        q = sub.add_parser(name, help=helptext)
        q.add_argument("--spend", action="store_true", help="make the paid calls (otherwise, say what they'd be)")
        q.add_argument("--only", help="only this image model")
    q = sub.add_parser("pick", help="choose a portrait candidate: pick MODEL GENDER N")
    q.add_argument("model")
    q.add_argument("gender", choices=GENDERS)
    q.add_argument("candidate", type=int)
    q = sub.add_parser("clips", help="phases 2–4: the clips")
    q.add_argument("--phase", type=int, required=True, choices=(2, 3, 4))
    q.add_argument("--spend", action="store_true", help="make the paid calls (otherwise, say what they'd be)")
    q.add_argument("--only", help="only this avatar or video service")
    q.add_argument("--avatars", help="phase 3's avatar services, comma-separated, over plan.yaml's")
    sub.add_parser("ledger", help="what's been spent, call by call")
    q = sub.add_parser("score", help="face similarity, to the portrait and across clips (needs the score extra)")
    q.add_argument("--frames", type=int, default=8, help="frames sampled per clip")
    q = sub.add_parser("review", help="write the blind review page")
    q.add_argument("--reveal", action="store_true", help="show the services' and models' names")
    args = parser.parse_args(argv)

    try:
        plan = Plan.load(Path(args.plan))
    except (OSError, PlanError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    store = Store(plan.run_dir(), plan.budget)
    avatars = args.avatars.split(",") if getattr(args, "avatars", None) else None

    try:
        if args.command == "plan":
            show_plan(plan, store, set(args.phase) if args.phase else None, avatars)
        elif args.command == "inputs":
            show_inputs(plan)
        elif args.command == "keys":
            return check_keys(plan)
        elif args.command == "pick":
            if not (store.run / f"portraits/{args.model}-{args.gender}-{args.candidate}.png").exists():
                print(f"error: there's no portraits/{args.model}-{args.gender}-{args.candidate}.png", file=sys.stderr)
                return 2
            store.pick(args.model, args.gender, args.candidate)
            print(f"picked {args.model}-{args.gender}-{args.candidate}")
        elif args.command == "ledger":
            for e in store.entries():
                print(f"  {e['time']}  {e['status']:6}  ${e['cost_usd']:7.3f}  {e['key']}  {e['reported'] or ''}")
            print(f"  spent ${store.spent():.2f} of ${plan.budget:.2f}; "
                  f"${store.committed() - store.spent():.2f} more in jobs still running")
        elif args.command in ("audio", "portraits", "clips"):
            phase = {"audio": 0, "portraits": 1}.get(args.command, getattr(args, "phase", None))
            calls = plan.calls(store.run, store.picks(), {phase}, avatars)
            if args.only:
                calls = [c for c in calls if c.service == args.only]
            Runner(plan, store, services_for(plan), spend=args.spend).run(calls)
            print(f"spent ${store.spent():.2f} of ${plan.budget:.2f}")
        elif args.command == "score":
            from .score import score

            score(plan, store, args.frames)
        elif args.command == "review":
            from .review import review

            print(review(plan, store, reveal=args.reveal))
    except (OverBudget, ServiceError, PlanError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
