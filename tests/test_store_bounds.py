"""Bounded metadata reads with synthetic filesystem faults on every host."""

import stat
from types import SimpleNamespace

import pytest

from pdcst import repository
from pdcst.repository import EpisodeRepository


def test_missing_and_valid_store(tmp_path, episode):
    repo = EpisodeRepository(tmp_path)
    assert repo.list() == []
    repo.add(episode)
    assert repo.list()[0].guid == episode.guid


@pytest.mark.parametrize("growing", [False, True])
def test_oversized_store_is_refused(tmp_path, monkeypatch, growing):
    repo = EpisodeRepository(tmp_path)
    repo.path.write_bytes(b" " * 64 + b"[]")
    monkeypatch.setattr(repository, "STORE_MAX_BYTES", 32, raising=False)
    if growing:
        # The bytes exceed the limit even though the initial stat did not.
        monkeypatch.setattr(
            repository.os,
            "fstat",
            lambda _: SimpleNamespace(st_mode=stat.S_IFREG, st_size=0),
        )
    with pytest.raises(ValueError, match="metadata.*size limit"):
        repo.list()


def test_nonregular_descriptor_is_refused_without_read(tmp_path, monkeypatch):
    repo = EpisodeRepository(tmp_path)
    repo.path.write_bytes(b"[]")
    opened = []
    original_open = repository.os.open

    def nonblocking_open(path, flags):
        opened.append(flags)
        return original_open(path, flags)

    monkeypatch.setattr(repository.os, "open", nonblocking_open)
    monkeypatch.setattr(
        repository.os,
        "fstat",
        lambda _: SimpleNamespace(st_mode=stat.S_IFIFO, st_size=0),
    )
    with pytest.raises(ValueError, match="metadata.*regular file"):
        repo.list()
    assert opened and opened[0] & repository.os.O_NONBLOCK


@pytest.mark.parametrize(
    "data,category",
    [
        (b'{"private": "SIGNED_SENTINEL"', "invalid JSON"),
        (b"\xffSIGNED_SENTINEL", "invalid UTF-8"),
        (b'[{"audio_url": "SIGNED_SENTINEL"}]', "invalid schema"),
    ],
)
def test_private_malformed_store_is_bounded(tmp_path, data, category):
    repo = EpisodeRepository(tmp_path)
    repo.path.write_bytes(data)
    with pytest.raises(ValueError, match=category) as error:
        repo.list()
    assert "SIGNED_SENTINEL" not in str(error.value)
    assert len(str(error.value)) < 500
    assert repo.path.read_bytes() == data


@pytest.mark.parametrize("field", ["published", "feed"])
def test_unrepresentable_utc_date_is_invalid_schema(tmp_path, field):
    import json

    from pdcst.repository import PodcastRepository

    row = {
        "index": 1,
        "guid": "synthetic",
        "audio_url": "SIGNED_SENTINEL",
        "published": "9999-12-31T23:59:59-05:00",
        "title": "Synthetic",
        "audio_file": None,
    }
    repo = EpisodeRepository(tmp_path)
    if field == "feed":
        repo = PodcastRepository(tmp_path)
        row = {
            "feed": {"url": "SIGNED_SENTINEL", "updated": row["published"]},
            "title": "Synthetic",
            "episodes_count": 1,
            "file_name_pattern": "{title}.mp3",
            "directory": "synthetic",
        }
    repo.path.write_text(json.dumps([row]))
    with pytest.raises(ValueError, match="invalid schema") as error:
        repo.list()
    assert "SIGNED_SENTINEL" not in str(error.value)
