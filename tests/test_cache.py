import os
import time

import numpy as np
import pytest

from narrate import cache
from narrate.cli import main
from narrate.tts import SynthCache


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATE_CACHE", str(tmp_path))
    return tmp_path


def entry(root, engine: str, name: str, size: int, age_days: float):
    path = root / "tts" / engine / f"{name}.npy"
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.zeros(size // 4, dtype=np.float32))
    used = time.time() - age_days * 86400
    os.utime(path, (used, used))
    return path


def test_parse_size():
    assert cache.parse_size("500M") == 500 * 1024**2
    assert cache.parse_size("2g") == 2 * 1024**3
    assert cache.parse_size("1048576") == 1048576
    with pytest.raises(ValueError):
        cache.parse_size("lots")


def test_prune_to_size_removes_least_recently_used_first(root):
    old = entry(root, "kokoro", "old", 4000, 30)
    mid = entry(root, "elevenlabs", "mid", 4000, 10)
    new = entry(root, "kokoro", "new", 4000, 1)
    removed = cache.prune(cache.entries(), max_size=9000)
    assert [e.path for e in removed] == [old] and mid.exists() and new.exists()


def test_prune_by_age_and_recent_entries_are_protected(root):
    stale = entry(root, "kokoro", "stale", 4000, 40)
    fresh = entry(root, "kokoro", "fresh", 4000, 0.01)  # used minutes ago, e.g. by a running build
    removed = cache.prune(cache.entries(), older_than=30 * 86400, max_size=0)
    assert [e.path for e in removed] == [stale] and fresh.exists()


def test_a_cache_hit_refreshes_the_entry(root):
    path = entry(root, "kokoro", "hit", 400, 50)
    assert SynthCache.load(path) is not None
    assert time.time() - path.stat().st_mtime < 60
    assert SynthCache.load(root / "missing.npy") is None


def test_cache_command(root, capsys):
    entry(root, "kokoro", "a", 4000, 20)
    entry(root, "elevenlabs", "b", 4000, 20)
    assert main(["cache"]) == 0
    out = capsys.readouterr().out
    assert "kokoro" in out and "elevenlabs" in out and "2 entries" in out
    assert main(["cache", "--prune", "--older-than", "10"]) == 0
    assert "removed 2 entries" in capsys.readouterr().out
    assert main(["cache", "--prune"]) == 2


def test_files_from_before_engine_folders_are_listed(root):
    legacy = root / "tts" / "old.npy"
    legacy.parent.mkdir(parents=True)
    np.save(legacy, np.zeros(10, dtype=np.float32))
    assert [e.engine for e in cache.entries()] == ["legacy"]


def test_cache_timestamp_refresh_ignores_oserror(tmp_path, monkeypatch):
    from narrate.tts import SpeechCache
    import numpy as np

    cache_file = tmp_path / "sample.npy"
    np.save(cache_file, np.zeros((10,)))

    def mock_utime(p):
        raise PermissionError("Permission denied")

    monkeypatch.setattr("os.utime", mock_utime)
    loaded = SpeechCache.load(cache_file)
    assert loaded is not None
