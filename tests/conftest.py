from datetime import datetime, timezone

import pytest

from pdcst.models import Episode, Feed, Podcast
from pdcst.repository import EpisodeRepository, PodcastRepository


@pytest.fixture
def feed():
    return Feed(url="https://example.com/rss.xml", updated=datetime.now(timezone.utc))


@pytest.fixture
def podcast(feed):
    return Podcast(feed=feed, title="Python Podcast", episodes_count=3)


@pytest.fixture
def episode(podcast):
    attributes = {
        "podcast": podcast,
        "index": 1,
        "audio_url": (
            "https://d2mmy4gxasde9x.cloudfront.net/cast_audio/" "pp38_refactoring.mp3"
        ),
        "guid": "dfd60a58-8bd3-4fe2-8dab-87f55093a0c7",
        "published": datetime.now(timezone.utc),
        "title": "Refactoring",
    }
    return Episode.from_dict(attributes)


@pytest.fixture
def publication_store(tmp_path, podcast, episode):
    podcast.file_name_pattern = "saved.mp3"
    episode.audio_file = "saved.mp3"
    PodcastRepository(tmp_path).add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episodes.add(episode)
    (episodes.base_dir / "saved.mp3").write_bytes(b"previous audio")
    return tmp_path, episodes
