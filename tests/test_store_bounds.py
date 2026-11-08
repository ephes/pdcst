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


@pytest.mark.parametrize("headroom", [1, 0, -1])
@pytest.mark.parametrize("operation", ["add_list", "import_list"])
def test_write_obeys_read_byte_ceiling(
    tmp_path, monkeypatch, episode, headroom, operation
):
    import copy

    repo = EpisodeRepository(tmp_path)
    episode.audio_file = "saved.mp3"
    audio = tmp_path / episode.audio_file
    audio.write_bytes(b"synthetic audio")
    repo.add(episode)
    before = repo.path.read_bytes()
    incoming = copy.deepcopy(episode)
    incoming.title = "Unicode \u00e9\U0001f40d" * 20
    stored = repo.storage_model_type(**incoming.dict())
    # Exercise real UTF-8 bytes rather than ASCII-only JSON escaping.
    original_json = repo.storage_model_list_type.json
    monkeypatch.setattr(
        repo.storage_model_list_type,
        "json",
        lambda self: original_json(self, ensure_ascii=False),
    )
    encoded = repo.storage_model_list_type(__root__=[stored]).json().encode("utf-8")
    monkeypatch.setattr(repository, "STORE_MAX_BYTES", len(encoded) + headroom)
    if headroom < 0:
        with pytest.raises(ValueError, match="metadata.*size limit"):
            getattr(repo, operation)([incoming])
        assert repo.path.read_bytes() == before
    else:
        getattr(repo, operation)([incoming])
        assert len(repo.path.read_bytes()) == len(encoded)
    assert repo.list()[0].audio_file == "saved.mp3"
    assert audio.read_bytes() == b"synthetic audio"
    assert not list(tmp_path.glob("*.tmp"))


def test_oversized_subscription_precedes_episode_import(
    tmp_path, monkeypatch, podcast, episode
):
    import copy

    import pdcst
    from pdcst.repository import PodcastRepository

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcasts = PodcastRepository(tmp_path)
    podcasts.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episode.audio_file = "saved.mp3"
    episodes.add(episode)
    audio = episodes.base_dir / episode.audio_file
    audio.write_bytes(b"synthetic audio")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    updated = copy.deepcopy(podcast)
    updated.title = "x" * 5000
    incoming = copy.deepcopy(episode)
    incoming.audio_url = "https://example.test/changed.mp3"
    monkeypatch.setattr(repository, "STORE_MAX_BYTES", 2048)

    class Parser:
        def get(self, *_):
            return updated, [incoming]

    with pytest.raises(ValueError, match="metadata.*size limit"):
        pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), podcasts)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    assert episodes.list()[0].audio_file == "saved.mp3"
    assert audio.read_bytes() == b"synthetic audio"
