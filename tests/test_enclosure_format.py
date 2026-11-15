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

    def transfer(enclosure, stage):
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
