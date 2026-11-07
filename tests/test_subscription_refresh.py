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
    # Read once: importing must still reload under the lock.
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


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["corrupt", "read", "replace"])
def test_episode_import_failure_keeps_subscription(
    tmp_path, monkeypatch, podcast, episode, existing, failure
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    repo = PodcastRepository(tmp_path)
    if existing:
        repo.add(podcast)
    before = repo.path.read_bytes() if existing else None
    original_subscription = repo.list()  # Read before the attempted import.
    fresh = copy.deepcopy(podcast)
    fresh.directory = "updated"
    fresh.episodes_count = 9
    directory = tmp_path / fresh.directory
    directory.mkdir()
    episodes = EpisodeRepository(directory)
    episodes.path.write_text("broken" if failure == "corrupt" else "[]")
    episode_bytes = episodes.path.read_bytes()
    injected = []

    class Parser:
        def get(self, *_):
            return fresh, [episode]

    def fail_read(self):
        injected.append("read")
        raise OSError("injected read")

    if failure == "read":
        monkeypatch.setattr(EpisodeRepository, "_fetch_all", fail_read)
    elif failure == "replace":
        from pdcst.storage import os

        replace = os.replace

        def fail_replace(source, target):
            if target == episodes.path:
                injected.append("replace")
                raise OSError("injected replace")
            return replace(source, target)

        monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises((ValueError, OSError)):
        pdcst.subscribe(podcast.feed.url, "{title}", fresh.directory, Parser(), repo)
    if failure != "corrupt":
        assert injected == [failure]
    assert (repo.path.read_bytes() if repo.path.exists() else None) == before
    assert repo.list() == original_subscription
    assert episodes.path.read_bytes() == episode_bytes
    assert not list(tmp_path.rglob("*.tmp"))


def test_subscription_publish_failure_is_retryable(
    tmp_path, monkeypatch, podcast, episode
):
    from pdcst.storage import os

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    repo = PodcastRepository(tmp_path)
    repo.add(podcast)
    before = repo.path.read_bytes()
    directory = tmp_path / podcast.directory
    directory.mkdir()
    (directory / "saved.mp3").write_bytes(b"synthetic")
    episode.audio_file = "saved.mp3"
    episodes = EpisodeRepository(directory)
    episodes.add(episode)
    fresh = copy.deepcopy(podcast)
    fresh.episodes_count = 2
    incoming = copy.deepcopy(episode)
    incoming.audio_file = None
    incoming.title = "Updated"

    class Parser:
        def get(self, *_):
            return fresh, [incoming]

    replace = os.replace
    injected = []

    def fail_publish(source, target):
        if target == repo.path:
            injected.append(True)
            raise OSError("injected publication")
        return replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_publish)
        with pytest.raises(OSError, match="injected publication"):
            pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), repo)
    assert injected == [True]
    assert repo.path.read_bytes() == before
    imported = EpisodeRepository(directory).list()[0]
    assert imported.title == "Updated"
    assert imported.audio_file == "saved.mp3"
    pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), repo)
    assert repo.list()[0].episodes_count == 2
    assert EpisodeRepository(directory).list()[0].audio_file == "saved.mp3"
