"""Selected enclosure types survive storage and determine extensionless names."""

import json

import feedparser
import pytest

import pdcst
from pdcst.repository import EpisodeRepository, FeedParserRepository
from tests.test_feed_import import item


@pytest.mark.parametrize(
    "url,media_type,suffix",
    [
        ("https://example.test/download", "audio/mpeg", "mp3"),
        ("https://example.test/download?format=m4a", "audio/mp4", "m4a"),
        ("https://example.test/file.php", "audio/x-m4a", "m4a"),
        ("https://example.test/mp3", "audio/mp4", "m4a"),
        ("https://example.test/v1.mp3/download", "audio/mp4", "m4a"),
        ("https://example.test/a.OGG?token=x", "audio/mpeg", "ogg"),
    ],
)
def test_enclosure_type_roundtrip_and_download(
    tmp_path, monkeypatch, url, media_type, suffix
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    document = feedparser.parse(
        "<rss><channel><title>Synthetic</title>"
        + item(f'<enclosure url="{url}" type="{media_type}"/>')
        + "</channel></rss>"
    )
    monkeypatch.setattr(FeedParserRepository, "fetch_document", lambda *_: document)
    show = pdcst.subscribe("https://example.test/feed", "{index}.{file_format}", None)
    repo = EpisodeRepository(tmp_path / show.directory)
    assert json.loads(repo.path.read_text())[0]["audio_media_type"] == media_type
    reloaded = pdcst.list_all_episodes(pdcst.list_all_podcasts()[0])[0]
    assert reloaded.audio_file_name == f"1.{suffix}"
    calls = []

    def transfer(enclosure, stage, **kwargs):
        calls.append(enclosure)
        stage.write_bytes(b"synthetic audio")

    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    pdcst.download("synthetic", "episode")
    assert calls == [url]
    assert (repo.base_dir / f"1.{suffix}").read_bytes() == b"synthetic audio"
    assert repo.list()[0].audio_file == f"1.{suffix}"


def test_legacy_record_without_media_type(tmp_path, episode):
    repo = EpisodeRepository(tmp_path)
    repo.add(episode)
    rows = json.loads(repo.path.read_text())
    rows[0].pop("audio_media_type", None)
    repo.path.write_text(json.dumps(rows))
    reloaded = repo.list({"podcast": episode.podcast})[0]
    assert reloaded.audio_file_name.endswith(".mp3")
    repo.add(reloaded)
    assert repo.list()[0].guid == episode.guid


@pytest.mark.parametrize(
    "new_path,retains_audio",
    [
        ("file.OGG?token=x", True),
        ("file.ogg?token=y", False),
        ("other.ogg?token=x", False),
    ],
)
def test_refresh_ogg_suffix_change_preserves_download(
    tmp_path, monkeypatch, new_path, retains_audio
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    feed_url = "https://example.test/feed"
    url = "https://example.test/file.oga?token=x"

    def fetch(*_):
        return feedparser.parse(
            "<rss><channel><title>Synthetic</title>"
            + item(f'<enclosure url="{url}" type="audio/ogg"/>')
            + "</channel></rss>"
        )

    def transfer(enclosure, stage, **kwargs):
        assert enclosure == url
        stage.write_bytes(b"original audio")

    monkeypatch.setattr(FeedParserRepository, "fetch_document", fetch)
    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    show = pdcst.subscribe(feed_url, "{index}.{file_format}", None)
    repo = EpisodeRepository(tmp_path / show.directory)
    pdcst.download("synthetic", "episode")
    assert repo.list()[0].audio_file == "1.oga"

    url = f"https://example.test/{new_path}"
    pdcst.subscribe(feed_url, "{index}.{file_format}", None)

    current = repo.list({"podcast": show})[0]
    assert current.audio_url == url
    assert current.audio_file_name == "1.ogg"
    assert current.audio_file == ("1.oga" if retains_audio else None)
    assert (repo.base_dir / "1.oga").read_bytes() == b"original audio"


@pytest.mark.parametrize(
    "path,previous_type,new_type,old_suffix,new_suffix",
    [
        ("download", "audio/mpeg", "audio/mp4", "mp3", "m4a"),
        ("download", "audio/mpeg", "audio/mp3", "mp3", "mp3"),
        ("file.MP3?token=x", "audio/mpeg", "audio/mp4", "mp3", "mp3"),
        ("download", None, "audio/mp4", "_download", "m4a"),
    ],
)
def test_refresh_retains_audio_unless_effective_format_changes(
    tmp_path, monkeypatch, path, previous_type, new_type, old_suffix, new_suffix
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    url = f"https://example.test/{path}"
    old_type = previous_type or "audio/mpeg"
    feed_url = "https://example.test/feed"

    def fetch(*_):
        return feedparser.parse(
            "<rss><channel><title>Synthetic</title>"
            + item(f'<enclosure url="{url}" type="{old_type}"/>')
            + "</channel></rss>"
        )

    def transfer(enclosure, stage, **kwargs):
        assert enclosure == url
        stage.write_bytes(payload)

    monkeypatch.setattr(FeedParserRepository, "fetch_document", fetch)
    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    show = pdcst.subscribe(feed_url, "{index}.{file_format}", None)
    repo = EpisodeRepository(tmp_path / show.directory)
    if previous_type is None:
        # A historical store had no MIME type for its extensionless URL.
        legacy = repo.list()[0]
        legacy.audio_media_type = None
        repo.add(legacy)
    payload = b"original audio"
    pdcst.download("synthetic", "episode")
    old_name = f"1.{old_suffix}"
    old_audio = repo.base_dir / old_name
    assert repo.list()[0].audio_file == old_name

    old_type = new_type
    pdcst.subscribe(feed_url, "{index}.{file_format}", None)

    current = repo.list({"podcast": show})[0]
    assert current.audio_media_type == new_type
    assert old_audio.read_bytes() == b"original audio"
    assert current.audio_file_name == f"1.{new_suffix}"
    if previous_type is not None and old_suffix != new_suffix:
        assert current.audio_file is None
        payload = b"new audio"
        pdcst.download("synthetic", "episode")
        assert repo.list()[0].audio_file == current.audio_file_name
        assert (repo.base_dir / current.audio_file_name).read_bytes() == b"new audio"
        assert old_audio.read_bytes() == b"original audio"
    else:
        assert current.audio_file == old_name
