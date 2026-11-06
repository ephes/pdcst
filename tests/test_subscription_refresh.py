import copy
from datetime import timedelta

import pytest

import pdcst
from pdcst.repository import EpisodeRepository, PodcastRepository


def test_refresh_preserves_valid_download(tmp_path, monkeypatch, podcast, episode):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    directory = tmp_path / podcast.directory
    directory.mkdir()
    (directory / "saved.mp3").write_bytes(b"synthetic audio")
    episode.audio_file = "saved.mp3"
    repo = EpisodeRepository(directory)
    repo.add(episode)
    # Prime a stale cache: importing must reload under the lock.
    repo.list()
    fresh = copy.deepcopy(episode)
    fresh.audio_file = None
    fresh.title = "Changed title"
    fresh.published += timedelta(days=1)
    new = copy.deepcopy(fresh)
    new.guid = "new"

    class Parser:
        def get(self, *_):
            return podcast, [fresh, new]

    pdcst.subscribe(
        podcast.feed.url, "{title}", None, Parser(), PodcastRepository(tmp_path)
    )
    saved, added = EpisodeRepository(directory).list()
    assert saved.audio_file == "saved.mp3"
    assert saved.title == fresh.title and saved.published == fresh.published
    assert added.audio_file is None
    saved.audio_file = None
    repo.add(saved)
    assert repo.list()[0].audio_file is None


@pytest.mark.parametrize(
    "name", ["missing.mp3", "../outside.mp3", "/outside.mp3", "link.mp3"]
)
def test_import_drops_invalid_association(tmp_path, episode, name):
    (tmp_path / "link.mp3").symlink_to(tmp_path.parent / "outside.mp3")
    repo = EpisodeRepository(tmp_path)
    episode.audio_file = name
    repo.add(episode)
    episode.audio_file = None
    repo.import_list([episode])
    assert repo.list()[0].audio_file is None


@pytest.mark.parametrize(
    "titles", [("Same Show", "Same Show"), ("Same/Show", "Same:Show")]
)
def test_distinct_feeds_have_distinct_directories(tmp_path, monkeypatch, feed, titles):
    from pdcst.models import Podcast

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)

    class Parser:
        def get(self, url, directory, pattern):
            current = copy.deepcopy(feed)
            current.url = url
            return (
                Podcast(
                    feed=current,
                    title=titles[int(url[-1])],
                    episodes_count=0,
                    directory=directory,
                ),
                [],
            )

    repo = PodcastRepository(tmp_path)
    a = pdcst.subscribe("https://example.test/0", "{title}", None, Parser(), repo)
    b = pdcst.subscribe("https://example.test/1", "{title}", None, Parser(), repo)
    assert a.directory != b.directory
    refreshed = pdcst.subscribe(b.feed.url, "{title}", None, Parser(), repo)
    assert refreshed.directory == b.directory
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    with pytest.raises(ValueError, match="already used"):
        pdcst.subscribe(b.feed.url, "{title}", a.directory, Parser(), repo)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}


@pytest.mark.parametrize("unsafe", ["../outside", "/absolute", "escaping-link"])
def test_unsafe_legacy_subscription_does_not_block_others(
    tmp_path, monkeypatch, podcast, unsafe
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    (tmp_path / "escaping-link").symlink_to(tmp_path.parent, target_is_directory=True)
    repo = PodcastRepository(tmp_path)
    bad = copy.deepcopy(podcast)
    bad.feed.url = "https://example.test/unsafe"
    bad.directory = unsafe
    repo.add(bad)

    class Parser:
        def get(self, *_):
            return podcast, []

    result = pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), repo)
    assert result.directory == podcast.directory
    assert len(repo.list()) == 2


def test_shared_legacy_directory_requires_explicit_separation(
    tmp_path, monkeypatch, podcast
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    repo = PodcastRepository(tmp_path)
    other = copy.deepcopy(podcast)
    other.feed.url = "https://example.test/other"
    repo.add_list([podcast, other])
    before = repo.path.read_bytes()

    class Parser:
        def get(self, _url, directory, _pattern):
            fresh = copy.deepcopy(podcast)
            if directory is not None:
                fresh.directory = directory
            return fresh, []

    with pytest.raises(ValueError, match="choose a distinct --podcast-dir"):
        pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), repo)
    assert repo.path.read_bytes() == before
    assert not (tmp_path / podcast.directory).exists()
    result = pdcst.subscribe(podcast.feed.url, "{title}", "separated", Parser(), repo)
    assert result.directory == "separated"
    assert (
        next(p for p in repo.list() if p.feed.url == other.feed.url).directory
        == other.directory
    )
