"""End-to-end builds with the silent speech backend (timing-only, no models or network)."""

from pathlib import Path

import pytest
from PIL import Image

from narrate import media
from narrate.build import Builder, Visual, allocate_frames, layout_visuals
from narrate.config import FPS
from narrate.doctor import find_font
from narrate.script import parse, parse_file
from narrate.tts import SilentSynth
from narrate.cli import main

EXAMPLES = Path(__file__).parent.parent / "reference" / "examples"
CHAR = SilentSynth.seconds_per_char


@pytest.fixture
def project(tmp_path):
    Image.new("RGB", (640, 360), "steelblue").save(tmp_path / "blue.png")
    media.run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-f", "lavfi",
               "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-shortest", str(tmp_path / "clip.mp4")])
    return tmp_path


def build(project: Path, text: str, name: str = "out"):
    script_path = project / f"{name}.md"
    script_path.write_text(text)
    builder = Builder(parse_file(script_path), SilentSynth(), log=lambda _: None)
    return builder.build(project / f"{name}.mp4", workdir=project / f"work-{name}")


def frames(seconds: float) -> float:
    return round(seconds * FPS) / FPS


@pytest.mark.parametrize("sync, audio, expected", [
    ("freeze", 1.0, 2.0), ("freeze", 3.0, 3.0), ("match", 3.0, 3.0), ("trim", 1.0, 1.0),
    ("trim", 3.0, 2.0), ("loop", 3.0, 3.0), ("trim-or-loop", 1.0, 1.0), ("loop-audio", 1.0, 2.0),
    ("freeze", 0.0, 2.0),
])
def test_video_sync_durations(sync, audio, expected):
    assert layout_visuals([Visual("video", sync=sync, length=2.0)], audio, None)[0] == expected


def clip(length: float, sync: str = "freeze") -> Visual:
    return Visual("video", sync=sync, length=length)


STILL = Visual("still")


def test_sequence_spare_time_goes_to_images():
    d, layout = layout_visuals([clip(2), STILL, clip(3)], 10.0, None)
    assert d == 10.0 and layout.pieces == [2.0, 5.0, 3.0] and not layout.repeat


def test_sequence_without_images_holds_the_last_clip():
    d, layout = layout_visuals([clip(2), clip(3)], 8.0, None)
    assert layout.pieces == [2.0, 6.0]


def test_sequence_plays_in_full_under_short_narration():
    d, layout = layout_visuals([clip(2), STILL], 1.0, None)
    assert d == 3.0 and layout.pieces == [2.0, 1.0]


def test_sequence_match_scales_everything():
    d, layout = layout_visuals([clip(2, "match"), STILL, clip(1)], 8.0, None)
    assert d == 8.0 and layout.pieces == [4.0, 2.0, 2.0] and layout.tempo == [0.5, 1.0, 0.5]


def test_sequence_trim_cuts_in_order():
    d, layout = layout_visuals([clip(2, "trim"), clip(3)], 3.5, None)
    assert d == 3.5 and layout.pieces == [2.0, 1.5]


def test_sequence_loop_repeats_the_whole_pass():
    d, layout = layout_visuals([clip(2, "loop"), STILL], 7.0, None)
    assert d == 7.0 and layout.repeat and layout.pieces == [2.0, 1.0]


def test_images_alone_share_the_narration():
    d, layout = layout_visuals([STILL, STILL, STILL], 3.0, None)
    assert layout.pieces == [1.0, 1.0, 1.0]


def test_allocate_frames_sums_exactly():
    assert allocate_frames(10, [1, 1, 1]) == [4, 3, 3]
    assert sum(allocate_frames(301, [2.0, 5.0, 3.0])) == 301


def test_scene_timing_rules(project, tmp_path):
    script = parse(
        "---\nsize: 320x180\n---\n"
        "```\nslide only\n```\n\n---\n"           # 1: text slide with no audio -> 1 s
        "![](blue.png)\n\n(duration: 2.5)\n\n---\n"  # 2: duration
        "Hello there.\n\n(pause-until: 3)\n\n---\n"  # 3: pause-until from scene start
        "![](clip.mp4)\n\nHi.\n\n---\n"           # 4: 2 s clip, short narration, freeze
        "![trim](clip.mp4)\n\n" + "Long words here. " * 6 + "\n",  # 5: trim to the clip
        project / "s.md",
    )
    builder = Builder(script, SilentSynth(), log=lambda _: None)
    durations = [builder.plan_scene(s, tmp_path).duration for s in script.scenes]
    assert durations == pytest.approx([1.0, 2.5, 3.0, 2.0, 2.0], abs=1e-3)


def test_build_end_to_end(project):
    result = build(project, (
        "---\nsize: 320x180\nsubtitles: embed\n---\n\n"
        "![contain](blue.png)\n\nHello there.\n\n---\n\n"
        "```md\n# Title\n* point\n```\n\n(duration: 1)\n\n---\n\n"
        "![](clip.mp4)\n\n(callout:\n  cx: 160\n  cy: 90)\n\n---\n\n"
        "(pause: 0.5)\n\n> caption only\n"
    ))
    hello = len("Hello there.") * CHAR + 0.4
    expected = frames(hello) + 1.0 + 2.0 + 0.5
    assert result.duration == pytest.approx(expected, abs=2 / FPS)
    info = media.probe(result.video)
    assert (info.width, info.height, info.has_audio, info.has_subtitles) == (320, 180, True, True)
    assert info.duration == pytest.approx(result.duration, abs=0.05)
    srt = (project / "out.srt").read_text()
    assert "Hello there." in srt and "caption only" in srt
    assert (project / "out.vtt").exists() and result.poster.exists()


def test_build_accepts_relative_keep_work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "script.md").write_text("A short scene.\n")

    assert main(["build", "script.md", "--tts", "silent", "-q", "--keep-work", "work"]) == 0

    assert (tmp_path / "script.mp4").is_file()
    assert (tmp_path / "work").is_dir()


def test_burned_captions_and_custom_font_render(project):
    font = find_font("DejaVuSans-Bold.ttf")
    if font is None:
        pytest.skip("needs a TrueType font to copy into the project")
    (project / "bold.ttf").write_bytes(font.read_bytes())
    result = build(project, (
        "---\nsize: 320x180\nsubtitles:\n  mode: overlay\n  font: bold.ttf\n  position: top\n---\n\n"
        "![](blue.png)\n\nCaptioned narration.\n"
    ), name="burn")
    assert media.probe(result.video).has_subtitles is False  # burned in, not a track
    assert "Captioned narration." in (project / "burn.srt").read_text()  # and the files come too (#58)


LOOP_PROBE = """
import resource, sys
import numpy as np
from pathlib import Path
from narrate import media
from narrate.build import Builder, Layout, ScenePlan, Visual
from narrate.frames import FrameSpec
from narrate.script import parse
from narrate.tts import SilentSynth

d = Path(sys.argv[1])
b = Builder(parse("---\\nsize: 640x360\\n---\\nx\\n", d / "s.md"), SilentSynth(), log=lambda _: None)
v = Visual("video", d / "clip.mp4", sync="loop", start=0.0, length=20.0)
plan = ScenePlan(1, [v], FrameSpec(), np.zeros(1, dtype=np.float32), 30.0, layout=Layout([20.0], [1.0], repeat=True))
(seg,) = [f.result() for f in b._render_scene(plan, 900, None, d)]
seg.rename(d / "out.mp4")
# ru_maxrss is in kilobytes on Linux, in bytes on macOS
print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // (1024 * 1024 if sys.platform == "darwin" else 1024))
"""


def test_looping_a_long_clip_keeps_memory_flat(tmp_path):
    """The loop filter buffered the whole segment in memory (~300 MB here); looping from disk doesn't."""
    import subprocess
    import sys

    media.run(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "20", "-c:v", "libx264",
               "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(tmp_path / "clip.mp4")])
    peak_mb = int(subprocess.run([sys.executable, "-c", LOOP_PROBE, str(tmp_path)], check=True,
                                 capture_output=True, text=True).stdout.split()[-1])
    assert peak_mb < 200
    out = tmp_path / "out.mp4"
    assert round(media.probe(out).duration * FPS) == 900
    # The second pass repeats the first: frames 20 s apart match
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    media.extract_frame(out, 2.0, a)
    media.extract_frame(out, 22.0, b)
    import numpy as np

    pa, pb = (np.asarray(Image.open(p).convert("L"), dtype=float) for p in (a, b))
    assert np.abs(pa - pb).mean() < 3


def test_mixed_sequence_end_to_end(project):
    result = build(project, "---\nsize: 320x180\n---\n\n![](clip.mp4)\n\n![](blue.png)\n\n![](clip.mp4)\n\nShort.\n",
                   name="mixed")
    assert result.duration == pytest.approx(2 + 1 + 2, abs=1 / FPS)
    middle = project / "middle.png"
    media.extract_frame(result.video, 2.5, middle)
    r, g, b = Image.open(middle).convert("RGB").getpixel((160, 90))
    assert b > r + 40  # the steel-blue image, between the two test-pattern clips


def frame_at(video: Path, t: float, tmp: Path):
    import numpy as np

    png = tmp / f"frame-{t}.png"
    media.extract_frame(video, t, png)
    return np.asarray(Image.open(png).convert("L"), dtype=float)


def test_slides_take_turns_in_a_scene(project):
    result = build(project, "---\nsize: 320x180\n---\n\n```\nONE\n```\n\n```\nTWO\n```\n\n(duration: 2)\n",
                   name="turns")
    assert result.duration == pytest.approx(2.0, abs=1 / FPS)
    first, second = frame_at(result.video, 0.5, project), frame_at(result.video, 1.5, project)
    assert abs(first - second).mean() > 1  # different text


def test_two_slides_split_an_image_and_window_over_a_clip(project, tmp_path):
    from narrate.frames import FrameRenderer

    media.run(["-loop", "1", "-i", str(project / "blue.png"), "-t", "2", "-c:v", "libx264",
               "-pix_fmt", "yuv420p", "-vf", "scale=320:180", str(project / "still.mp4")])
    script = parse("---\nsize: 320x180\n---\n\n![](blue.png)\n\n```\nONE\n```\n\n```\nTWO\n```\n\n(duration: 2)\n\n"
                   "---\n\n![](still.mp4)\n\n```\nONE\n```\n\n```\nTWO\n```\n", project / "s.md")
    builder = Builder(script, SilentSynth(), log=lambda _: None)
    with FrameRenderer(320, 180, tmp_path) as renderer:
        image_plan = builder.plan_scene(script.scenes[0], tmp_path)
        assert len(builder._render_scene(image_plan, 60, renderer, tmp_path)) == 2
        clip_plan = builder.plan_scene(script.scenes[1], tmp_path)
        (future,) = builder._render_scene(clip_plan, 60, renderer, tmp_path)
        segment = future.result()
    first, second = frame_at(segment, 0.5, tmp_path), frame_at(segment, 1.5, tmp_path)
    assert abs(first - second).mean() > 1


def test_overlay_windows_are_in_piece_time():
    from narrate.build import _windows

    overlays = [(Path("a.png"), 0.0, 1.0), (Path("b.png"), 1.0, 2.0)]
    assert _windows(overlays, 15, 45) == [(Path("a.png"), 0.0, 0.5), (Path("b.png"), 0.5, 1.0)]


def test_segment_range_is_honoured_at_both_ends(project, tmp_path):
    """Narakeet's own video-segments render ignores the end of a T-T range; narrate follows the spec."""
    import numpy as np

    result = build(project, "---\nsize: 320x180\n---\n\n![00:00.5-00:01.5](clip.mp4)\n", name="segment")
    assert result.duration == pytest.approx(1.0, abs=1 / FPS)
    first, source = frame_at(result.video, 0.0, tmp_path), frame_at(project / "clip.mp4", 0.5, tmp_path)
    assert np.abs(first - source).mean() < 6  # starts on the source frame at 0.5 s
