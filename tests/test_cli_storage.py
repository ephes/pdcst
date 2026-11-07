"""Registered CLI failure boundaries using only synthetic stores and transfers."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import pdcst
from pdcst.repository import EpisodeRepository, FeedParserRepository, PodcastRepository

FRESH_PROBE = """
import json
import sys
from pathlib import Path
from unittest.mock import patch
from typer.testing import CliRunner

# Patch the default home lookup before importing settings, with no real home writes.
with patch.object(Path, 'home', return_value=Path(sys.argv[1])):
    import pdcst
from pdcst.models import Podcast, Episode
from pdcst.repository import FeedParserRepository
from datetime import datetime, timezone

def feed(self, url, directory, pattern):
    podcast = Podcast(feed={'url': url, 'updated': datetime.now(timezone.utc)},
                      title='Synthetic Show', episodes_count=1,
                      directory=directory, file_name_pattern=pattern)
    episode = Episode(podcast=podcast, index=1, guid='synthetic',
                      audio_url='https://example.invalid/audio.mp3',
                      title='Synthetic Episode', published=datetime.now(timezone.utc))
    return podcast, [episode]
with patch.object(FeedParserRepository, 'get', feed):
    result = CliRunner().invoke(pdcst.cli, sys.argv[2:], color=False)
print(json.dumps({'exit': result.exit_code, 'output': result.output,
                  'exception': type(result.exception).__name__}))
"""


def fresh_command(tmp_path, args, root=None):
    env = dict(os.environ, PYTHONPATH=str(Path(pdcst.__file__).resolve().parent.parent))
    env.pop("PDCST_HOME", None)
    if root is not None:
        env["PDCST_HOME"] = str(root)
    process = subprocess.run(
        [sys.executable, "-c", FRESH_PROBE, str(tmp_path), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


@pytest.mark.parametrize("args", [["--help"], ["podcasts"], ["episodes", "missing"]])
@pytest.mark.parametrize("configured", [False, True])
def test_fresh_home_readonly_commands(tmp_path, args, configured):
    root = tmp_path / "custom" if configured else tmp_path / "pdcst"
    result = fresh_command(tmp_path, args, root if configured else None)
    assert result["exit"] == 0, result
    assert not root.exists()


def test_fresh_home_add_creates_root(tmp_path):
    result = fresh_command(tmp_path, ["add", "https://example.invalid/feed.xml"])
    assert result["exit"] == 0, result
    assert (tmp_path / "pdcst/podcasts.json").is_file()
    assert (tmp_path / "pdcst/synthetic_show/episodes.json").is_file()


@pytest.mark.parametrize(
    "args",
    [
        ["podcasts"],
        ["episodes", "show"],
        ["add", "https://example.invalid/feed.xml"],
        ["download", "show", "episode"],
    ],
)
def test_file_root_is_a_concise_cli_error(tmp_path, args):
    root = tmp_path / "file-root"
    root.write_bytes(b"untouched")
    result = fresh_command(tmp_path, args, root)
    assert result["exit"] != 0
    diagnostic = " ".join(result["output"].replace("│", " ").split())
    assert "must be a directory" in diagnostic, result
    assert "Traceback" not in result["output"]
    assert root.read_bytes() == b"untouched"


@pytest.fixture
def cli_store(tmp_path, monkeypatch, podcast, episode):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.title = "Synthetic Show"
    episode.title = "Synthetic Episode"
    episode.audio_url = "https://example.invalid/audio.mp3"
    podcasts = PodcastRepository(tmp_path)
    podcasts.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episodes.add(episode)

    def feed(self, url, directory, pattern):
        selected = copy.deepcopy(podcast)
        selected.file_name_pattern = pattern
        return selected, [copy.deepcopy(episode)]

    monkeypatch.setattr(FeedParserRepository, "get", feed)

    def forbidden(*args, **kwargs):
        pytest.fail("Refusal must precede metadata write or audio transfer")

    monkeypatch.setattr(pdcst, "download_with_progress", forbidden)
    return podcasts, episodes, podcast, episode, forbidden


@pytest.mark.parametrize(
    "command,location",
    [
        ("podcasts", "podcasts"),
        ("episodes", "podcasts"),
        ("add", "podcasts"),
        ("download", "podcasts"),
        ("episodes", "episodes"),
        ("add", "episodes"),
        ("download", "episodes"),
    ],
)
@pytest.mark.parametrize("kind", ["json", "utf8", "schema", "permission"])
def test_cli_store_errors_preserve_private_metadata(
    cli_store, monkeypatch, command, kind, location
):
    from pdcst import repository

    podcasts, episodes, podcast, _, forbidden = cli_store
    repo = podcasts if location == "podcasts" else episodes
    data = {
        "json": b'{"SIGNED_SENTINEL":',
        "utf8": b"\xffSIGNED_SENTINEL",
        "schema": b'[{"audio_url":"https://example.invalid/?SIGNED_SENTINEL"}]',
    }
    if kind == "permission":
        original_open = repository.os.open

        def denied(path, flags, *args, **kwargs):
            if Path(path) == repo.path:
                raise PermissionError("SIGNED_SENTINEL")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(repository.os, "open", denied)
    else:
        repo.path.write_bytes(data[kind])
    before = {p: p.read_bytes() for p in (podcasts.path, episodes.path)}
    monkeypatch.setattr(repository.JsonRepository, "_write_models", forbidden)
    args = {
        "podcasts": ["podcasts"],
        "episodes": ["episodes", "show"],
        "add": ["add", podcast.feed.url],
        "download": ["download", "show", "episode"],
    }[command]
    result = CliRunner().invoke(pdcst.cli, args, color=False)
    assert result.exit_code != 0
    assert "Cannot read metadata" in result.output, result.output
    assert "SIGNED_SENTINEL" not in result.output
    assert "Traceback" not in result.output
    assert len(result.output) < 1400
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("command", ["add", "download"])
@pytest.mark.parametrize(
    "pattern", ["{unknown}", "{", "{title:03d}", "{title.__class__}", "../{title}.mp3"]
)
def test_cli_pattern_errors_precede_mutation(cli_store, monkeypatch, command, pattern):
    podcasts, episodes, podcast, _, forbidden = cli_store
    podcast.file_name_pattern = pattern
    podcasts.add(podcast)
    before = {p: p.read_bytes() for p in (podcasts.path, episodes.path)}
    monkeypatch.setattr(EpisodeRepository, "_write_models", forbidden)
    args = (
        ["add", podcast.feed.url, "--name-pattern", pattern]
        if command == "add"
        else ["download", "show", "episode"]
    )
    result = CliRunner().invoke(pdcst.cli, args, color=False)
    assert result.exit_code != 0
    assert "Invalid filename pattern" in result.output
    assert "Traceback" not in result.output
    assert {p: p.read_bytes() for p in before} == before
    assert not list(episodes.base_dir.glob(".pdcst-download-*"))


def test_root_beneath_file_fails_concisely(tmp_path):
    parent = tmp_path / "parent-file"
    parent.write_bytes(b"untouched")
    result = fresh_command(
        tmp_path, ["add", "https://example.invalid/feed.xml"], parent / "data"
    )
    assert result["exit"] != 0
    assert "Unable to update local storage" in result["output"]
    assert "Traceback" not in result["output"]
    assert parent.read_bytes() == b"untouched"


def test_root_creation_denial_is_private(cli_store, monkeypatch):
    podcasts, episodes, podcast, _, forbidden = cli_store
    before = {p: p.read_bytes() for p in (podcasts.path, episodes.path)}
    attempted = []
    original_mkdir = Path.mkdir

    def denied(path, *args, **kwargs):
        if path == podcasts.base_dir / ".feed-locks":
            attempted.append(path)
            raise PermissionError("SIGNED_SENTINEL")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied)
    monkeypatch.setattr(PodcastRepository, "_write_models", forbidden)
    result = CliRunner().invoke(pdcst.cli, ["add", podcast.feed.url], color=False)
    assert result.exit_code != 0
    assert attempted == [podcasts.base_dir / ".feed-locks"]
    assert "Unable to update local storage" in result.output
    assert "SIGNED_SENTINEL" not in result.output
    assert "Traceback" not in result.output
    assert len(result.output) < 1400
    assert {p: p.read_bytes() for p in before} == before
