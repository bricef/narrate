"""The harness, with fake services: what it plans, what it spends, and that it never spends twice."""

import wave
from pathlib import Path

import pytest
import yaml

from anchor.plan import Plan, PlanError
from anchor.providers import ADAPTERS
from anchor.providers.fake import FakeVideo
from anchor.runner import Runner
from anchor.store import OverBudget, Store
from anchor.__main__ import services_for

PLAN = {
    "budget_usd": 10, "run": "test",
    "voices": {"engine": "elevenlabs", "model": "eleven_v4", "male": "George", "female": "Lily"},
    "lines": {"L1": "one two three four five six seven eight nine ten", "L2": "a b c d e", "D30": "x " * 75},
    "portrait": {"prompt": "A photo of {person} at a desk.", "people": {"male": "a man", "female": "a woman"},
                 "aspect": "16:9", "candidates": 2},
    "image_models": {"a": {"provider": "fake-image", "est_usd": 0.05}, "b": {"provider": "fake-image", "est_usd": 0.2}},
    "avatar_prompt": "An anchor speaks.",
    "avatars": {"x": {"provider": "fake-video", "est_usd_per_s": 0.1},
                "y": {"provider": "fake-video", "est_usd_per_s": 0.05}},
    "phases": {"2": {"lines": ["L1"]}, "3": {"avatars": [], "portraits": {"male": None, "female": None},
                                            "lines": ["L2", "D30"]}},
}


def make_plan(tmp_path: Path, **changes) -> Plan:
    data = {**PLAN, **changes}
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(data))
    return Plan.load(path)


def write_wav(path: Path, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * int(8000 * seconds))


@pytest.fixture(autouse=True)
def fresh_fakes():
    FakeVideo.submitted = []


def test_the_plan_expands_into_every_call_with_its_cost(tmp_path):
    plan = make_plan(tmp_path)
    run = plan.run_dir()
    calls = plan.calls(run)
    by_phase = {p: [c for c in calls if c.phase == p] for p in range(4)}
    assert len(by_phase[0]) == 2 * 3  # each line in each voice
    assert len(by_phase[1]) == 2 * 2 * 2  # models × voices × candidates
    assert {c.inputs["prompt"] for c in by_phase[1]} == {"A photo of a man at a desk.", "A photo of a woman at a desk."}
    assert len(by_phase[2]) == 2 * 2 * 2  # models × voices × services, one line
    assert all(c.waiting for c in by_phase[2])  # nothing picked, no audio yet
    # Phase 3 is costed before it's chosen, with the dearest services, so the plan doesn't under-estimate
    assert {c.service for c in by_phase[3]} == {"x", "y"} and all("choose phase 3" in c.waiting for c in by_phase[3])
    # Lengths come from the words until the audio exists, then from the audio itself
    assert by_phase[2][0].seconds == 4.0 and by_phase[2][0].est_usd == pytest.approx(0.4)
    write_wav(run / "audio/male-L1.wav", 6.0)
    clip = next(c for c in plan.calls(run, phases={2}) if c.key == "clips/p2/x--a-male--L1.mp4")
    assert clip.seconds == 6.0 and clip.est_usd == pytest.approx(0.6)


def test_a_bad_plan_is_refused(tmp_path):
    with pytest.raises(PlanError, match="line 'L9'"):
        make_plan(tmp_path, phases={"2": {"lines": ["L9"]}, "3": {"lines": []}})
    with pytest.raises(PlanError, match="est_usd"):
        make_plan(tmp_path, image_models={"a": {"provider": "fake-image"}})


def test_nothing_is_spent_without_spend(tmp_path):
    plan = make_plan(tmp_path)
    store = Store(plan.run_dir(), plan.budget)
    said = []
    Runner(plan, store, services_for(plan), spend=False, log=said.append).run(plan.calls(store.run, phases={1}))
    assert not list(store.run.glob("portraits/*")) and store.spent() == 0
    assert sum("would" in line for line in said) == 8


def test_a_run_makes_portraits_then_clips_and_records_what_it_spent(tmp_path):
    plan = make_plan(tmp_path)
    store = Store(plan.run_dir(), plan.budget)
    runner = Runner(plan, store, services_for(plan), spend=True, log=lambda _: None, every=0)
    runner.run(plan.calls(store.run, phases={1}))
    assert len(list(store.run.glob("portraits/*.png"))) == 8 and store.spent() == pytest.approx(4 * 0.05 + 4 * 0.2)
    for model in ("a", "b"):
        for gender in ("male", "female"):
            store.pick(model, gender, 2)
            write_wav(store.run / f"audio/{gender}-L1.wav", 3.0)
    runner.run(plan.calls(store.run, store.picks(), {2}))
    clips = sorted(p.name for p in store.run.glob("clips/p2/*.mp4"))
    assert len(clips) == 8 and "x--a-male--L1.mp4" in clips
    assert (store.run / "clips/p2/x--b-female--L1.mp4").read_bytes().endswith(b"portraits/b-female-2.png")
    assert store.jobs() == {}  # every job collected
    assert store.spent() == pytest.approx(1.0 + 4 * 0.3 + 4 * 0.15)
    # Again: nothing is made or spent twice
    runner.run(plan.calls(store.run, store.picks(), {1, 2}))
    assert len(FakeVideo.submitted) == 8 and store.spent() == pytest.approx(2.8)


def test_a_stopped_run_collects_its_jobs_instead_of_paying_again(tmp_path):
    plan = make_plan(tmp_path)
    store = Store(plan.run_dir(), plan.budget)
    for gender in ("male", "female"):
        (store.run / "portraits").mkdir(parents=True, exist_ok=True)
        (store.run / f"portraits/a-{gender}-1.png").write_bytes(b"png")
        store.pick("a", gender, 1)
        write_wav(store.run / f"audio/{gender}-L1.wav", 2.0)
    calls = [c for c in plan.calls(store.run, store.picks(), {2}) if c.key.startswith("clips/p2/x--a-")]
    stopping = Runner(plan, store, services_for(plan), spend=True, log=lambda _: None, every=0, limit=0)
    FakeVideo.polls_before_done = 99
    stopping.run(calls)  # submits, then gives up waiting
    assert len(store.jobs()) == 2 and store.committed() == pytest.approx(0.4)
    FakeVideo.polls_before_done = 1
    said = []
    Runner(plan, store, services_for(plan), spend=True, log=said.append, every=0).run(calls)
    assert len(FakeVideo.submitted) == 2  # collected, not submitted again
    assert sum("resuming" in line for line in said) == 2 and store.jobs() == {}


def test_the_budget_stops_a_call_that_would_pass_it(tmp_path):
    plan = make_plan(tmp_path, budget_usd=0.3)
    store = Store(plan.run_dir(), plan.budget)
    runner = Runner(plan, store, services_for(plan), spend=True, log=lambda _: None)
    with pytest.raises(OverBudget, match="over the \\$0.30 budget"):
        runner.run(plan.calls(store.run, phases={1}))
    assert store.spent() <= 0.3


def test_every_provider_in_the_real_plan_has_an_adapter():
    plan = Plan.load(Path(__file__).parent.parent / "plan.yaml")
    providers = {s["provider"] for s in [*plan.data["image_models"].values(), *plan.data["avatars"].values(),
                                         *plan.data["phases"]["4"]["video"].values()]}
    assert providers <= set(ADAPTERS), providers - set(ADAPTERS)


def test_the_review_page_hides_names_until_revealed(tmp_path):
    from anchor.review import review

    plan = make_plan(tmp_path)
    store = Store(plan.run_dir(), plan.budget)
    (store.run / "portraits").mkdir(parents=True)
    (store.run / "portraits/a-male-1.png").write_bytes(b"png")
    store.pick("a", "male", 1)
    (store.run / "clips/p2").mkdir(parents=True)
    (store.run / "clips/p2/x--a-male--L1.mp4").write_bytes(b"mp4")
    blind = review(plan, store).read_text()
    assert "Service " in blind and "Portraits " in blind
    assert "(x)" not in blind and "(a)" not in blind and "x--a-male" not in blind  # not even in file names
    clip = next((store.run / "review/media").glob("p2-*.mp4"))
    assert clip.read_bytes() == b"mp4" and "x" not in clip.name.split("-")
    revealed = review(plan, store, reveal=True).read_text()
    assert "(x)" in revealed and "(a)" in revealed
    # The letters don't change between runs of the page
    assert review(plan, store).read_text() == blind


def test_openai_token_formula_matches_its_published_table():
    from anchor.providers.openai import output_tokens

    assert output_tokens("gpt-image-2", "high", 1024, 1024) == 7024  # $0.211, as OpenAI's table says
    assert output_tokens("gpt-image-2", "high", 1536, 1024) == 5488  # $0.165
    assert output_tokens("gpt-image-2.5-flare", "max", 2048, 1152) == 5650  # $0.17
