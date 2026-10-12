from datetime import datetime

import pytest

from pdcst.models import Episode, Feed, Podcast


@pytest.fixture
def feed():
    return Feed(url="https://example.com/rss.xml", updated=datetime.utcnow())


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
        "published": datetime.utcnow(),
        "title": "Refactoring",
    }
    return Episode.from_dict(attributes)
