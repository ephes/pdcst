"""Storage admission uses strict integers and one opened file generation."""

import json
import os

import pytest

from pdcst import repository
from pdcst.repository import EpisodeRepository, PodcastRepository, StoreReadError


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("operation", ["list", "add"])
def test_read_generation(tmp_path, monkeypatch, episode, replacement, operation):
    repo = EpisodeRepository(tmp_path)
    repo.add(episode)
    original = repo.path.read_bytes()
    changed = original.replace(b"Refactoring", b"Other title")
    assert len(changed) == len(original)
    real_fdopen = os.fdopen

    class InterleavedRead:
        def __init__(self, descriptor, *args, **kwargs):
            self.source = real_fdopen(descriptor, *args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def fileno(self):
            return self.source.fileno()

        def read(self, limit):
            first = self.source.read(10)
            if replacement:
                sibling = tmp_path / "replacement"
                sibling.write_bytes(changed)
                sibling.replace(repo.path)
            else:
                with repo.path.open("r+b") as writer:
                    writer.write(changed)
                    writer.flush()
                    os.fsync(writer.fileno())
            return first + self.source.read(limit - len(first))

    with monkeypatch.context() as patch:
        patch.setattr(repository.os, "fdopen", InterleavedRead)
        if replacement:
            assert repo.list()[0].title == episode.title
        else:
            with pytest.raises(StoreReadError, match="generation changed"):
                repo.list() if operation == "list" else repo.add(episode)
    assert repo.path.read_bytes() == changed


@pytest.mark.parametrize("value", [True, False, 1.5, -1, 0, "1"])
@pytest.mark.parametrize("operation", ["list", "add", "import_list"])
def test_strict_positive_index(tmp_path, episode, value, operation):
    repo = EpisodeRepository(tmp_path)
    repo.add(episode)
    rows = json.loads(repo.path.read_text())
    rows[0]["index"] = value
    repo.path.write_text(json.dumps(rows))
    before = repo.path.read_bytes()
    with pytest.raises(StoreReadError, match="invalid schema"):
        (
            repo.list()
            if operation == "list"
            else getattr(repo, operation)(episode if operation == "add" else [episode])
        )
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize("value", [True, False, 1.5, -1, "1"])
@pytest.mark.parametrize("operation", ["list", "add"])
def test_strict_nonnegative_count(tmp_path, podcast, value, operation):
    repo = PodcastRepository(tmp_path)
    repo.add(podcast)
    rows = json.loads(repo.path.read_text())
    rows[0]["episodes_count"] = value
    repo.path.write_text(json.dumps(rows))
    before = repo.path.read_bytes()
    with pytest.raises(StoreReadError, match="invalid schema"):
        repo.list() if operation == "list" else repo.add(podcast)
    assert repo.path.read_bytes() == before


def test_legacy_integer_roundtrip(tmp_path, podcast, episode):
    podcast.episodes_count = 0
    shows = PodcastRepository(tmp_path)
    shows.add(podcast)
    episodes = EpisodeRepository(tmp_path)
    episodes.add(episode)
    assert shows.list()[0].episodes_count == 0
    assert episodes.list()[0].index == 1
