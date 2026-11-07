"""Synthetic duplicate admission and ordered concurrent acquisition regressions."""

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import feedparser
import pytest

import pdcst
from pdcst.repository import EpisodeRepository, FeedParserRepository, PodcastRepository
from tests.test_feed_import import item


@pytest.mark.parametrize("changed", [False, True])
def test_duplicate_feed_refuses_publication(
    tmp_path, monkeypatch, podcast, episode, changed
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    subscriptions = PodcastRepository(tmp_path)
    subscriptions.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episode.audio_file = "saved.mp3"
    episodes.add(episode)
    audio = episodes.base_dir / episode.audio_file
    audio.write_bytes(b"previous audio")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    second = item()
    if changed:
        second = second.replace("Episode", "Changed").replace("a.mp3", "b.mp3")
    document = feedparser.parse(
        f"<rss><channel><title>Podcast</title>{item()}{second}</channel></rss>"
    )
    monkeypatch.setattr(
        FeedParserRepository, "fetch_document", staticmethod(lambda _: document)
    )
    with pytest.raises(ValueError, match="Duplicate identity at positions 1 and 2"):
        pdcst.subscribe(
            podcast.feed.url, "{index}.mp3", None, podcasts_repo=subscriptions
        )
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    assert audio.read_bytes() == b"previous audio"


@pytest.mark.parametrize("operation", ["add_list", "import_list"])
@pytest.mark.parametrize("changed", [False, True])
def test_duplicate_incoming_episodes_leave_store_intact(
    tmp_path, episode, operation, changed
):
    repo = EpisodeRepository(tmp_path)
    repo.add(episode)
    before = repo.path.read_bytes()
    other = copy.deepcopy(episode)
    if changed:
        other.title = "Changed"
        other.audio_url = "https://example.test/other.mp3"
    with pytest.raises(ValueError, match="Duplicate identity at positions 1 and 2"):
        getattr(repo, operation)([episode, other])
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["episode", "podcast"])
@pytest.mark.parametrize("changed", [False, True])
def test_duplicate_legacy_rows_are_never_collapsed(
    tmp_path, podcast, episode, kind, changed
):
    repo, model = (
        (EpisodeRepository(tmp_path), episode)
        if kind == "episode"
        else (PodcastRepository(tmp_path), podcast)
    )
    row = repo.storage_model_type(**model.dict())
    other = row.copy(deep=True)
    if changed:
        other.title = "Changed"
    repo.path.write_text(repo.storage_model_list_type(__root__=[row, other]).json())
    before = repo.path.read_bytes()
    audio = tmp_path / "saved.mp3"
    audio.write_bytes(b"previous audio")
    # Reading is refused too, so callers cannot unknowingly use a collapsed view.
    with pytest.raises(ValueError, match="Duplicate identity"):
        repo.list()
    with pytest.raises(ValueError, match="Duplicate identity"):
        repo.add(model)
    if kind == "episode":
        with pytest.raises(ValueError, match="Duplicate identity"):
            repo.import_list([model])
    assert repo.path.read_bytes() == before
    assert audio.read_bytes() == b"previous audio"


def test_same_feed_acquisition_is_ordered_but_other_feeds_are_independent(
    tmp_path, monkeypatch, podcast, episode
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    repo = PodcastRepository(tmp_path)
    repo.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episode.audio_file = "saved.mp3"
    episodes.add(episode)
    (episodes.base_dir / "saved.mp3").write_bytes(b"old audio")
    first_fetched = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_fetched = threading.Event()
    old, new = copy.deepcopy(episode), copy.deepcopy(episode)
    old.title = "Old acquisition"
    new.title = "New acquisition"
    new.audio_url = "https://example.test/new.mp3"

    class Parser:
        def __init__(self, version):
            self.version = version

        def get(self, url, *_):
            show = copy.deepcopy(podcast)
            show.feed.url = url
            show.episodes_count = 1
            if self.version == "old":
                first_fetched.set()
                assert release_first.wait(5)
                return show, [old]
            if self.version == "new":
                second_fetched.set()
                return show, [new]
            show.directory = "independent"
            return show, []

    def refresh(version, url):
        if version == "new":
            second_started.set()
        return pdcst.subscribe(
            url, "{index}.mp3", None, Parser(version), PodcastRepository(tmp_path)
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(refresh, "old", podcast.feed.url)
        try:
            assert first_fetched.wait(5)
            second = pool.submit(refresh, "new", podcast.feed.url)
            assert second_started.wait(5)
            # A separate feed must finish while the first acquisition is paused.
            pool.submit(refresh, "other", "https://example.test/other").result(5)
            assert not second_fetched.wait(0.2)
        finally:
            release_first.set()
        first.result(5)
        second.result(5)
    assert second_fetched.is_set()
    saved = EpisodeRepository(episodes.base_dir).all[episode.guid]
    assert saved.title == new.title
    assert saved.audio_url == new.audio_url
    assert saved.audio_file is None
    assert (episodes.base_dir / "saved.mp3").read_bytes() == b"old audio"


@pytest.mark.parametrize(
    "kind,command",
    [
        ("podcast", ["podcasts"]),
        ("podcast", ["episodes", "python"]),
        ("podcast", ["download", "python", "refactor"]),
        ("episode", ["episodes", "python"]),
        ("episode", ["download", "python", "refactor"]),
    ],
)
def test_duplicate_legacy_cli_error_omits_identity(
    tmp_path, podcast, episode, kind, command
):
    import os
    import subprocess
    import sys
    from pathlib import Path

    podcast.feed.url = "https://example.test/feed?key=PRIVATE_FEED_IDENTITY"
    episode.guid = "PRIVATE_EPISODE_IDENTITY"
    subscriptions = PodcastRepository(tmp_path)
    subscriptions.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episodes.add(episode)
    repo = subscriptions if kind == "podcast" else episodes
    row = next(iter(repo._fetch_all().values()))
    repo.path.write_text(repo.storage_model_list_type(__root__=[row, row]).json())
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    result = subprocess.run(
        [sys.executable, "-c", "import pdcst; pdcst.cli()", *command],
        cwd=tmp_path,
        env=dict(
            os.environ,
            PDCST_HOME=str(tmp_path),
            PYTHONPATH=str(Path(pdcst.__file__).resolve().parent.parent),
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Duplicate identity at positions 1 and 2" in output
    assert "Traceback" not in output
    assert len(output) < 1500
    assert "PRIVATE_FEED_IDENTITY" not in output
    assert "PRIVATE_EPISODE_IDENTITY" not in output
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
