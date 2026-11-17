import errno
import os
import wave

import numpy as np
import pytest

from narrate import media, music
from narrate.build import Builder, BuildError
from narrate.cli import main
from narrate.script import parse_file
from narrate.tts import SilentSynth


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    import os

    tools = tmp_path_factory.mktemp("tools")
    old = os.environ.get("NARRATE_TOOLS")
    os.environ["NARRATE_TOOLS"] = str(tools)
    try:
        # ffmpeg itself stays where it is: resolve it before switching tools dirs
        yield {name: music.track(name) for name in music.TRACKS}
    finally:
        if old is None:
            os.environ.pop("NARRATE_TOOLS")
        else:
            os.environ["NARRATE_TOOLS"] = old


@pytest.mark.parametrize("name", music.TRACKS)
def test_tracks_render_as_quiet_loops(rendered, name):
    audio = media.decode_audio(rendered[name], music.SR)
    assert len(audio) / music.SR > 15
    assert media.loudness(audio, music.SR) == pytest.approx(music.LEVEL_LUFS, abs=1.0)


def test_rendering_is_deterministic():
    assert np.array_equal(music.ukulele_1(), music.ukulele_1())


def test_user_recordings_take_precedence(tmp_path, monkeypatch):
    own = tmp_path / "corporate-1.mp3"
    own.write_bytes(b"not really an mp3")
    monkeypatch.setenv("NARRATE_MUSIC_DIR", str(tmp_path))
    assert music.track("corporate-1") == own


def test_builtin_track_plays_under_a_script(tmp_path):
    script = tmp_path / "s.md"
    script.write_text("---\nsize: 320x180\nbackground: ukulele-1 fade-in\n---\n\n```\nHi\n```\n\n(duration: 3)\n")
    result = Builder(parse_file(script), SilentSynth(), log=lambda _: None).build(tmp_path / "out.mp4")
    assert not any("ukulele" in n for n in result.notes)
    audio = media.decode_audio(result.video, music.SR)
    assert float(np.sqrt(np.mean(audio[-music.SR:] ** 2))) > 0.003  # music is audible


# A build's background music is ready before it spends anything (#90): a problem with it used to surface
# only once every scene and overlay was captured, and all of that was lost.


class Counting(SilentSynth):
    """Silent speech that records what it synthesises, several requests at a time as ElevenLabs does, so a
    build starts synthesising the whole script in the background before it plans the first scene."""

    concurrency = 4

    def __init__(self):
        self.spoken = []

    def synth(self, text, voice, speed, notes, only_cached=False):
        if only_cached:
            return None  # nothing is in the cache
        self.spoken.append(text)
        return super().synth(text, voice, speed, notes)


def unmakeable(project, monkeypatch):
    # The reported error. From the command line, a music folder that can't be written is now stopped first
    # by the start-up check (#89): this is a write that fails anyway, in a Builder used without that check,
    # or in a folder that changes after it.
    def refuse(name):
        raise PermissionError(13, "Permission denied", f"/cache/tools/music/v2/{name}.part")

    monkeypatch.setattr(music, "track", refuse)
    return "uplifting-2", r"background track uplifting-2 couldn't be made: \[Errno 13\] Permission denied"


def missing(project, monkeypatch):
    return "music.mp3", "background file not found: music.mp3"


def undecodable(project, monkeypatch):
    (project / "music.mp3").write_bytes(b"not really an mp3")
    return "music.mp3", "background music.mp3 couldn't be decoded: ffmpeg failed"


MUSIC_PROBLEMS = [unmakeable, missing, undecodable]


def with_music_problem(project, monkeypatch, problem):
    """A script whose background music has `problem`, and the error that should name it. An HTML scene and
    an overlay over the whole video are what a build captures frame by frame, the costly part."""
    source, message = problem(project, monkeypatch)
    (project / "page.html").write_text("<!doctype html><style>body{background:navy}</style>")
    path = project / "s.md"
    path.write_text(f"---\nsize: 320x180\nbackground: {source}\noverlay: page.html\n---\n\n"
                    "![](page.html)\n\nThe first scene.\n\n---\n\nThe second scene.\n")
    return parse_file(path), message


@pytest.mark.parametrize("scenes", [None, [2]], ids=["whole", "some-scenes"])
@pytest.mark.parametrize("problem", MUSIC_PROBLEMS)
def test_a_music_problem_stops_a_build_before_it_synthesises_or_captures(tmp_path, monkeypatch, problem, scenes):
    script, message = with_music_problem(tmp_path, monkeypatch, problem)
    synth, work = Counting(), tmp_path / "work"
    with pytest.raises(BuildError, match=message):
        Builder(script, synth, log=lambda _: None, scenes=scenes).build(tmp_path / "out.mp4", workdir=work)
    assert synth.spoken == []
    assert list(work.iterdir()) == []  # where every scene and overlay is captured


@pytest.mark.parametrize("problem", MUSIC_PROBLEMS)
def test_a_music_problem_stops_an_audio_build_before_it_synthesises(tmp_path, monkeypatch, problem):
    script, message = with_music_problem(tmp_path, monkeypatch, problem)
    synth = Counting()
    with pytest.raises(BuildError, match=message):
        Builder(script, synth, log=lambda _: None).build_audio(tmp_path / "out.mp3")
    assert synth.spoken == []


# wave.open on a file it can't open used to leave a half-made writer, which raised again as it was collected:
# pytest reports that as this warning, where the command line printed it as a traceback after the error
@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_a_track_that_cant_be_written_is_one_line_of_error(tmp_path, monkeypatch, capsys):
    media.ffmpeg()  # resolved before switching tools folders, so it isn't fetched again
    tools = tmp_path / "tools"
    part = tools / "music" / f"v{music.VERSION}" / "uplifting-2.part"
    part.mkdir(parents=True)  # where the track is written first: as a folder, it can't be opened, even by root
    monkeypatch.setenv("NARRATE_TOOLS", str(tools))
    monkeypatch.delenv("NARRATE_MUSIC_DIR", raising=False)
    with_music_problem(tmp_path, monkeypatch, lambda project, monkeypatch: ("uplifting-2", None))
    monkeypatch.chdir(tmp_path)
    assert main(["build", "s.md", "--tts", "silent", "-q"]) == 2
    error = f"[Errno {errno.EISDIR}] {os.strerror(errno.EISDIR)}: {str(part)!r}"
    assert capsys.readouterr().err.splitlines() == [
        f"error: s.md: background track uplifting-2 couldn't be made: {error}"]


def no_ffmpeg(project, monkeypatch):
    """A music file that's fine, and no ffmpeg to decode it with."""
    with wave.open(str(project / "music.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(music.SR)
        w.writeframes(bytes(2 * music.SR))
    monkeypatch.setattr(media, "find_ffmpeg", lambda: ("none", None))
    monkeypatch.setattr(media, "ffmpeg", media.ffmpeg.__wrapped__)  # not the one found already: it looks again
    return "music.wav", "^no ffmpeg on PATH"


def test_no_ffmpeg_is_reported_as_itself_not_as_the_music(tmp_path, monkeypatch):
    script, message = with_music_problem(tmp_path, monkeypatch, no_ffmpeg)
    synth = Counting()
    with pytest.raises(media.MediaError, match=message):  # not a BuildError blaming music.wav
        Builder(script, synth, log=lambda _: None).build(tmp_path / "out.mp4")
    assert synth.spoken == []
