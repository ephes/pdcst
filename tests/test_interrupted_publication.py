"""SIGKILL journeys run the real CLI with synthetic stores and fake transfers."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import pdcst
from pdcst.repository import PodcastRepository


def cli_process(home, script, *args):
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        cwd=home,
        env=dict(
            os.environ,
            PDCST_HOME=str(home),
            PYTHONPATH=str(Path(pdcst.__file__).resolve().parent.parent),
            NO_COLOR="1",
            COLUMNS="240",
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_barrier(process, path):
    deadline = time.monotonic() + 10
    while not path.exists():
        if process.poll() is not None:
            out, err = process.communicate(timeout=5)
            pytest.fail(f"Child exited: {out}\n{err}")
        if time.monotonic() >= deadline:
            pytest.fail(f"Child did not reach {path.name}")
        time.sleep(0.01)


@contextmanager
def paused_cli(home, script, *args):
    process = cli_process(home, script, *args)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def run_cli(home, *args):
    process = cli_process(
        home,
        "import pdcst; pdcst.FeedParserRepository.fetch_document = "
        "lambda *_: (_ for _ in ()).throw(AssertionError('no network')); pdcst.cli()",
        *args,
    )
    try:
        out, err = process.communicate(timeout=10)
        return process.returncode, out + err
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


REFRESH = """
import os, sys
from pathlib import Path
import pdcst
from pdcst.repository import EpisodeRepository, PodcastRepository
home = pdcst.settings.root
show = PodcastRepository(home).list()[0]
repo = EpisodeRepository(home / show.directory)
episodes = repo.list()
episodes[0].title = 'Refreshed'
show.episodes_count = 1
pdcst.FeedParserRepository.get = lambda *_: (show, episodes)
replace = os.replace
def pause():
    (home / 'ready').touch()
    sys.stdin.readline()
def intercepted(source, target):
    replace(source, target)
    if Path(target) == repo.path and sys.argv[1] == 'interrupted':
        pause()
os.replace = intercepted
pdcst.cli(['add', show.feed.url], standalone_mode=False)
pause()
"""


@pytest.mark.parametrize("phase", ["interrupted", "settled"])
def test_killed_refresh_requires_explicit_recovery(publication_store, phase):
    home, episodes = publication_store
    old_episodes = episodes.path.read_bytes()
    old_podcasts = (home / "podcasts.json").read_bytes()
    with paused_cli(home, REFRESH, phase) as process:
        wait_barrier(process, home / "ready")
        process.kill()
        process.wait(timeout=5)
    assert json.loads(episodes.path.read_text())[0]["title"] == "Refreshed"
    if phase == "settled":
        assert not list(home.rglob("*intent.json"))
        code, output = run_cli(home, "episodes", "python")
        assert code == 0, output
        return
    markers = list(home.rglob(".pdcst-refresh-intent.json"))
    assert len(markers) == 2
    intent = json.loads(markers[0].read_text())
    assert Path(intent["previous_episodes"]).read_bytes() == old_episodes
    assert Path(intent["previous_podcasts"]).read_bytes() == old_podcasts
    assert (home / "podcasts.json").read_bytes() == old_podcasts
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    for command in [
        ("podcasts",),
        ("episodes", "python"),
        ("add", "https://example.test/feed"),
        ("download", "python", "refreshed"),
    ]:
        code, output = run_cli(home, *command)
        assert code != 0
        assert "unresolved publication" in output
        assert ".pdcst-refresh-intent.json" in output
        assert "Traceback" not in output
    assert before == {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}


AUDIO = """
import os, sys
from pathlib import Path
import pdcst
from pdcst.repository import EpisodeRepository
home = pdcst.settings.root
replace = os.replace
def pause():
    (home / 'ready').touch()
    sys.stdin.readline()
def intercepted(source, target):
    replace(source, target)
    if Path(target).name.endswith('.previous') and sys.argv[1] == 'interrupted':
        pause()
os.replace = intercepted
def transfer(url, stage):
    stage.write_bytes(b'completed audio')
pdcst.download_with_progress = transfer
pdcst.cli(['download', 'python', 'refactor'], standalone_mode=False)
pause()
"""


@pytest.mark.parametrize("phase", ["interrupted", "settled"])
def test_killed_audio_publication_retains_intent(publication_store, phase):
    home, episodes = publication_store
    before = episodes.path.read_bytes()
    with paused_cli(home, AUDIO, phase) as process:
        wait_barrier(process, home / "ready")
        process.kill()
        process.wait(timeout=5)
    if phase == "settled":
        assert not list(home.rglob("*intent.json"))
        code, output = run_cli(home, "episodes", "python")
        assert code == 0, output
        assert (episodes.base_dir / "saved.mp3").read_bytes() == b"completed audio"
        return
    marker = episodes.base_dir / ".pdcst-download-intent.json"
    assert marker.is_file()
    intent = json.loads(marker.read_text())
    assert Path(intent["backup"]).read_bytes() == b"previous audio"
    assert Path(intent["stage"]).read_bytes() == b"completed audio"
    assert not Path(intent["target"]).exists()
    assert episodes.path.read_bytes() == before
    files = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    for command in [("episodes", "python"), ("download", "python", "refactor")]:
        code, output = run_cli(home, *command)
        assert code != 0
        assert "unresolved publication" in output
        assert str(marker) in output
        assert "Traceback" not in output
    assert files == {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}


def test_refresh_cleanup_failure_preserves_primary(publication_store, monkeypatch):
    home, episodes = publication_store
    monkeypatch.setattr(pdcst.settings, "root", home)
    shows = PodcastRepository(home)
    show = shows.list()[0]
    incoming = episodes.list()
    primary = TypeError("synthetic publication failure")

    class Parser:
        def get(self, *_):
            return show, incoming

    def fail_write(*_):
        raise primary

    unlink = Path.unlink

    def fail_cleanup(path, *args, **kwargs):
        if path.name == ".pdcst-refresh-intent.json":
            raise OSError("synthetic cleanup failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(shows, "_write_models", fail_write)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    with pytest.warns(RuntimeWarning, match="recovery cleanup incomplete"):
        with pytest.raises(TypeError) as raised:
            pdcst.subscribe(show.feed.url, "saved.mp3", None, Parser(), shows)
    assert raised.value is primary
    assert list(home.rglob(".pdcst-refresh-intent.json"))
    assert list(home.rglob("*.previous"))
