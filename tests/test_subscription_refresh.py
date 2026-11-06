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
