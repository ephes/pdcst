"""Same-process API and cooperating-process browse freshness."""

import os
import subprocess
import sys
from pathlib import Path

import pdcst
from pdcst.repository import PodcastRepository


def test_same_reader_sees_cooperating_writer_without_writing(
    tmp_path, monkeypatch, podcast
):
    reader = PodcastRepository(tmp_path)
    assert pdcst.list_all_podcasts(reader) == []
    writer = PodcastRepository(tmp_path)
    writer.add(podcast)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def refuse_write(*_):
        raise AssertionError("Browse must not write or take a writer lock")

    with monkeypatch.context() as patch:
        patch.setattr(PodcastRepository, "_write_models", refuse_write)
        patch.setattr(PodcastRepository, "_lock", refuse_write)
        assert [p.feed.url for p in pdcst.list_all_podcasts(reader)] == [
            podcast.feed.url
        ]
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    # Read again after a genuinely separate process updates this subscription.
    code = """
from pdcst.repository import PodcastRepository
from pdcst.config import settings
repo = PodcastRepository(settings.root)
podcast = repo.list()[0]
podcast.title = 'Updated by another process'
repo.add(podcast)
"""
    env = dict(
        os.environ,
        PDCST_HOME=str(tmp_path),
        PYTHONPATH=str(Path(pdcst.__file__).resolve().parent.parent),
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    before = writer.path.read_bytes()
    assert reader.list()[0].title == "Updated by another process"
    assert writer.path.read_bytes() == before


def test_default_api_browse_subscribe_browse(tmp_path):
    code = """
from datetime import datetime, timezone
from unittest.mock import patch
import pdcst
from pdcst.models import Feed, Podcast
from pdcst.repository import FeedParserRepository, PodcastRepository
from typer.testing import CliRunner

assert pdcst.list_all_podcasts() == []
show = Podcast(feed=Feed(url='https://example.test/feed',
    updated=datetime(2024, 1, 1, tzinfo=timezone.utc)),
    title='Fresh Show', episodes_count=0)
with patch.object(FeedParserRepository, 'get', return_value=(show, [])):
    pdcst.subscribe(show.feed.url, '{index}.mp3', None)
path = PodcastRepository(pdcst.settings.root).path
before = path.read_bytes()
with patch.object(PodcastRepository, '_write_models', side_effect=AssertionError):
    assert [p.title for p in pdcst.list_all_podcasts()] == ['Fresh Show']
    result = CliRunner().invoke(pdcst.cli, ['podcasts'], color=False)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == 'Fresh Show - 0 episodes'
assert path.read_bytes() == before
"""
    env = dict(
        os.environ,
        PDCST_HOME=str(tmp_path),
        PYTHONPATH=str(Path(pdcst.__file__).resolve().parent.parent),
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_default_repositories_follow_current_settings(tmp_path, monkeypatch, podcast):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    assert pdcst.list_all_podcasts() == []
    PodcastRepository(tmp_path).add(podcast)
    assert [p.title for p in pdcst.list_all_podcasts()] == [podcast.title]
    other_root = tmp_path / "other"
    other_root.mkdir()
    monkeypatch.setattr(pdcst.settings, "root", other_root)
    assert pdcst.list_all_podcasts() == []
