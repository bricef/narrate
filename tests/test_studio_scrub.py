"""HTML scenes scrubbed in the studio's preview (#73): the page on a clock the studio moves, showing
each moment as a build captures it."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
from PIL import Image
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright
from starlette.testclient import TestClient

from narrate import media
from narrate.build import Builder
from narrate.script import parse
from narrate.studio.analysis import run
from narrate.studio.preview import Files, frame, page_html
from narrate.studio.server import create_app
from narrate.tts import SilentSynth

from test_studio_e2e import running  # noqa: F401 - the browser fixture

# Something on every clock a page can use: a CSS animation (green, 100 px a second), a script moving a
# box on requestAnimationFrame's time (magenta, 100 px a second), a timer revealing a box at 1.5 s
# (cyan) and a box revealed at the second paragraph's cue (yellow)
ANIMATED = """<!doctype html>
<html><head><style>
html, body { margin: 0; width: 320px; height: 180px; background: #000; }
div { position: absolute; width: 20px; height: 20px; }
#css { top: 20px; left: 0; background: #00ff00; animation: go 3s linear forwards; }
@keyframes go { to { left: 300px; } }
#raf { top: 100px; left: 0; background: #ff00ff; }
#late { top: 60px; left: 150px; background: #00ffff; display: none; }
#cue { top: 150px; left: 0; background: #ffff00; opacity: 0; animation: show .01s linear forwards;
       animation-delay: var(--cue-2, 9s); }
@keyframes show { to { opacity: 1; } }
</style></head><body><div id="css"></div><div id="raf"></div><div id="late"></div><div id="cue"></div>
<script>
const box = document.getElementById("raf"), t0 = performance.now();
function tick(now) { box.style.left = Math.min(300, (now - t0) / 10) + "px"; requestAnimationFrame(tick); }
requestAnimationFrame(tick);
setTimeout(() => { document.getElementById("late").style.display = "block"; }, 1500);
</script></body></html>
"""

SCRIPT = ("---\nsize: 320x180\n---\n\n![](anim.html)\n\n"
          "A first paragraph, long enough to take a couple of seconds or so.\n\nThe second one.\n")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATE_CACHE", str(tmp_path / "cache"))
    (tmp_path / "anim.html").write_text(ANIMATED)
    (tmp_path / "scene.html").write_text("<!doctype html><link rel=stylesheet href=look.css><p>page</p>")
    (tmp_path / "look.css").write_text("p { color: red }")
    Image.new("RGB", (640, 360), "red").save(tmp_path / "red.png")
    return tmp_path


def shown(project, text: str, scene: int, t: float) -> dict:
    analysis = run(text, project / "s.md", SilentSynth())
    (project / "scratch").mkdir(exist_ok=True)
    return frame(analysis.plans[scene], t, analysis.settings, Files(project, project / "scratch", "k")), analysis


def narration(url: str) -> dict:
    return json.loads(parse_qs(urlsplit(url).query)["narrate"][0])


# -- the page's address and placement ---------------------------------------------------------------

def test_a_page_gets_the_cues_a_build_gives_it(project):
    text = "---\nsize: 320x180\n---\n\n![](red.png)\n\n![](scene.html)\n\nFirst words.\n\nSecond words, a few more.\n"
    page, analysis = shown(project, text, 1, 2.5)  # noqa: F811
    plan = analysis.plans[1]
    assert page["kind"] == "page" and page["url"].startswith("/pages/k/scene.html?v=")
    # The page starts half way through the scene, on a whole frame, and its cues count from there
    assert page["start"] == pytest.approx(plan.duration / 2, abs=1 / 30)
    assert round(page["start"] * 30) == pytest.approx(page["start"] * 30, abs=1e-3)
    cues = narration(page["url"])["cues"]
    assert cues == {k: pytest.approx(t - page["start"], abs=1e-3) for k, t in plan.marks.items()}
    assert cues["1"] < 0  # the first paragraph began before the page appeared: a negative delay
    assert page["time"] == pytest.approx(2.5 - page["start"], abs=1e-3)
    # Another moment of the same page has the same address: scrubbing doesn't reload it
    assert shown(project, text, 1, 2.9)[0]["url"] == page["url"]


@pytest.mark.parametrize("fit, box", [
    ("contain", {"left": 70.0, "top": 0.0, "scale_x": 0.5, "scale_y": 0.5}),
    ("cover", {"left": 0.0, "top": -70.0, "scale_x": 320 / 360, "scale_y": 320 / 360}),
    ("fit", {"left": 0.0, "top": 0.0, "scale_x": 320 / 360, "scale_y": 0.5}),
])
def test_a_page_designed_at_another_size_is_fitted_as_a_build_fits_it(project, fit, box):
    text = f"---\nsize: 320x180\n---\n\n(html: {{file: scene.html, viewport: 360x360, size: {fit}}})\n\n(duration: 1)\n"
    page, _ = shown(project, text, 1, 0.5)  # noqa: F811
    assert page["size"] == [360, 360]
    assert page["box"] == {k: pytest.approx(v) for k, v in box.items()}


def test_a_named_position_places_a_cropped_page(project):
    text = "---\nsize: 320x180\n---\n\n(html: {file: scene.html, viewport: 360x360, position: top})\n\n(duration: 1)\n"
    assert shown(project, text, 1, 0.5)[0]["box"]["top"] == 0.0


def test_the_page_runs_on_a_fake_clock_installed_before_its_own_scripts():
    source = '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8"><script>mine()</script></head><body></body></html>'
    served = page_html(source, {"duration": 3, "cues": {"1": 0, "fleet": 1.25}, "params": {"name": "</script>"}})
    assert served.startswith("<!DOCTYPE html>")  # the page keeps its rendering mode
    order = [served.index(s) for s in ('<head>', "vendor/fake-timers.js", "window.__narrateSeek",
                                       '"--cue-fleet": "1.2500s"', "page-clock.js", "mine()")]
    assert order == sorted(order)
    assert "<\\/script>" in served and served.count("</script>") == 4  # a parameter can't end the script early
    # Without a head, or anything at all
    assert page_html("<!doctype html><p>hi</p>", {}).index("fake-timers") > len("<!doctype html>")
    assert page_html("<p>hi</p>", {}).endswith("<p>hi</p>")


def test_the_studio_serves_the_page_and_its_files(project):
    script = project / "s.md"
    script.write_text("![](scene.html)\n\nHello.\n")
    client = TestClient(create_app(script, "silent", hosts=["testserver"]))
    pages = f"/pages/{client.app.state.session.key}/"
    narrate = json.dumps({"duration": 2, "cues": {"1": 0}})
    served = client.get(pages + "scene.html", params={"narrate": narrate})
    assert served.status_code == 200 and "page-clock.js" in served.text and '"--cue-1": "0.0000s"' in served.text
    assert client.get(pages + "look.css").text == "p { color: red }"  # its own files, as they are
    assert client.get(pages + "scene.html", params={"narrate": "[1, 2"}).status_code == 400
    assert client.get(pages + "..%2Fsecret.html").status_code == 404
    assert client.get("/static/vendor/fake-timers.js").status_code == 200


# -- in the browser, against a build ---------------------------------------------------------------

def where(pixels, row: int, colour: tuple[int, int, int]) -> int | None:
    """Where a box of `colour` starts across `row`, or None if it isn't there."""
    match = np.where(np.all(np.abs(pixels[row] - colour) < 80, axis=1))[0]
    return int(match[0]) if len(match) else None


def built(video, frame_index: int, project) -> dict:
    out = project / f"built-{frame_index}.png"
    media.extract_frame(video, (frame_index - 0.25) / 30, out)  # the first frame from then on
    pixels = np.asarray(Image.open(out).convert("RGB"), dtype=int)
    return {"css": where(pixels, 30, (0, 255, 0)), "raf": where(pixels, 110, (255, 0, 255)),
            "late": where(pixels, 70, (0, 255, 255)) is not None, "cue": where(pixels, 160, (255, 255, 0)) is not None}


@pytest.fixture(params=["chromium", "webkit"])
def scrub_page(request):
    with sync_playwright() as playwright:
        browser_type = getattr(playwright, request.param)
        try:
            browser = browser_type.launch()
        except PlaywrightError as error:
            if request.param == "webkit":
                pytest.skip(f"Playwright WebKit is not installed: {error}")
            raise
        try:
            yield browser.new_page()
        finally:
            browser.close()


PREVIEWED = """() => {
  const left = (id) => document.getElementById(id).getBoundingClientRect().left;
  const style = (id) => getComputedStyle(document.getElementById(id));
  return {css: left("css"), raf: left("raf"), late: style("late").display !== "none", cue: Number(style("cue").opacity) > 0.5};
}"""


def test_scrubbing_shows_each_moment_as_a_build_captures_it(project, scrub_page):
    page = scrub_page
    script = project / "s.md"
    script.write_text(SCRIPT)
    with ThreadPoolExecutor(1) as pool:  # a build of its own, beside the browser driving the studio
        video = pool.submit(lambda: Builder(parse(SCRIPT, script), SilentSynth()).build(project / "out.mp4")).result().video
    cue = run(SCRIPT, script, SilentSynth()).plans[1].marks["2"]
    with running(script) as url:
        page.goto(url)
        expect(page.locator(".tl-scene")).to_have_count(1)
        page.locator(".tl-scene").first.click()  # the timeline has the keys: Home, ± a frame, ± a second
        page.keyboard.press("Home")
        expect(page.get_by_test_id("page-frame")).to_be_visible()
        inner = next(f for f in page.frames if "/anim.html" in f.url)

        def at(frame_index: int):
            expect(page.locator(".preview iframe")).to_have_count(1)
            expect(page.frame_locator(".preview iframe").locator("html")).to_have_attribute(
                "data-narrate-frame", str(frame_index), timeout=10000)
            seen, wanted = inner.evaluate(PREVIEWED), built(video, frame_index, project)
            assert seen["css"] == pytest.approx(wanted["css"], abs=2), frame_index
            assert seen["raf"] == pytest.approx(wanted["raf"], abs=2), frame_index
            assert (seen["late"], seen["cue"]) == (wanted["late"], wanted["cue"]), frame_index

        at(0)
        page.keyboard.press("Shift+ArrowRight")
        page.keyboard.press("Shift+ArrowRight")
        at(60)  # forwards: the clock runs on a frame at a time
        page.keyboard.press("Shift+ArrowLeft")
        at(30)  # backwards: the page reloads and runs forward again
        for _ in range(17):
            page.keyboard.press("ArrowRight")
        at(47)  # the timer has fired
        # Past the second paragraph's cue, the yellow box has appeared, in the preview as in the video
        for _ in range(round(cue * 30) - 47 + 3):
            page.keyboard.press("ArrowRight")
        at(round(cue * 30) + 3)

        # Playing the scene, the page keeps up with the narration, not only at each paragraph's start
        page.keyboard.press("Home")
        at(0)
        page.get_by_role("button", name="▶ Play scene").click()
        expect(page.get_by_test_id("playback")).to_contain_text("Playing scene 1")
        shown = page.frame_locator(".preview iframe").locator("html")
        # (frames 15 to 125 are inside the first paragraph: following paragraphs alone would jump past them)
        expect(shown).to_have_attribute("data-narrate-frame", re.compile(r"^(1[5-9]|[2-9]\d|1[01]\d|12[0-5])$"),
                                        timeout=10000)
        page.get_by_role("button", name="■ Stop").click()
