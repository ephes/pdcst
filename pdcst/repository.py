"""
Repositories to store podcasts/episodes.

Atm only uses json. But this could also be a sqlite or a
remote api, maybe fastAPI... hmm.
"""
import warnings
from datetime import datetime, timezone
from operator import itemgetter
from urllib.parse import urlsplit

import feedparser
from pydantic import BaseModel, parse_file_as, validator

from .models import Episode, Podcast


def as_utc(value):
    """Legacy naive storage values represent UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parsed_date(value):
    """Feedparser supplies UTC date tuples."""
    return datetime(*value[:6], tzinfo=timezone.utc)


class FeedError(ValueError):
    """A feed cannot supply usable podcast metadata."""


class FeedInDb(BaseModel):
    url: str
    updated: datetime

    _utc_updated = validator("updated", allow_reuse=True)(as_utc)


class EpisodeInDb(BaseModel):
    index: int
    guid: str
    audio_url: str
    published: datetime
    title: str
    audio_file: str | None

    _utc_published = validator("published", allow_reuse=True)(as_utc)

    @property
    def pk(self):
        return self.guid


class EpisodeListInDb(BaseModel):
    """
    Used to make a list of episodes serializable to json.
    """

    __root__: list[EpisodeInDb]


class PodcastInDb(BaseModel):
    feed: FeedInDb
    title: str
    episodes_count: int
    file_name_pattern: str
    directory: str | None

    @property
    def pk(self):
        return self.feed.url


class PodcastListInDb(BaseModel):
    """
    Used to make a list of podcasts serializable to json.
    """

    __root__: list[PodcastInDb]


class JsonRepository:
    """
    Store a dict of serialized pydantic models in a json store.

    Use model.pk as primary key to be able to quickly locate models
    and maybe get/update/remove them.
    """

    _all = None

    def __init__(self, base_dir):
        self.base_dir = base_dir

    @property
    def path(self):
        return self.base_dir / self.name

    def _fetch_all(self):
        """
        Fetch dict of pydantic models from json in self.path
        """
        try:
            return {
                model.pk: model
                for model in parse_file_as(list[self.storage_model_type], self.path)
            }
        except FileNotFoundError:
            return {}

    @property
    def all(self):
        """
        Cache fetching all models from file.
        """
        if self._all is None:
            self._all = self._fetch_all()
        return self._all

    def list(self, additional_attributes=None):
        """
        Just return a list of all stored models. Convert models from storage
        model type to domain model type.
        """
        if additional_attributes is None:
            return [
                self.domain_model_type.from_dict(model.dict())
                for model in self.all.values()
            ]
        else:
            return [
                self.domain_model_type.from_dict(model.dict() | additional_attributes)
                for model in self.all.values()
            ]

    def _write_models(self, locked):
        try:
            self.path.parent.mkdir(exist_ok=True)
        except AttributeError:
            # LocalPath in tests
            pass
        models = self.storage_model_list_type(__root__=list(locked.values()))
        with self.path.open("w") as f:
            f.write(models.json())

    def add(self, model):
        """Add a single model to the repository."""
        model_in_db = self.storage_model_type(**model.dict())
        # To avoid race conditions implement proper locking of json file FIXME
        locked = self._fetch_all()
        locked[model_in_db.pk] = model_in_db
        self._write_models(locked)

    def add_list(self, models):
        """Add a list of models to the repository."""
        models_in_db = [self.storage_model_type(**model.dict()) for model in models]
        # To avoid race conditions implement proper locking of json file FIXME
        locked = self._fetch_all()
        for model_in_db in models_in_db:
            locked[model_in_db.pk] = model_in_db
        self._write_models(locked)


class EpisodeRepository(JsonRepository):
    """
    Store episodes metadata and audio files for a podcast.
    """

    name = "episodes.json"
    storage_model_type = EpisodeInDb
    storage_model_list_type = EpisodeListInDb
    domain_model_type = Episode


class PodcastRepository(JsonRepository):
    """
    Store podcasts metadata.
    """

    name = "podcasts.json"
    storage_model_type = PodcastInDb
    storage_model_list_type = PodcastListInDb
    domain_model_type = Podcast


class FeedParserRepository:
    """
    Fetch/parse feed data from http.
    """

    AUDIO_TYPES = {
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/aac",
        "audio/x-m4a",
        "audio/ogg",
        "audio/opus",
        "audio/wav",
        "audio/x-wav",
        "audio/flac",
        "audio/x-flac",
    }
    AUDIO_SUFFIXES = {"mp3", "m4a", "aac", "ogg", "oga", "opus", "wav", "flac"}

    @classmethod
    def audio_url(cls, entry):
        fallback = None
        for enclosure in entry.get("enclosures", []):
            href = enclosure.get("href")
            if not isinstance(href, str) or any(c.isspace() for c in href):
                continue
            try:
                url = urlsplit(href)
                if url.scheme not in {"http", "https"} or not url.hostname:
                    continue
                # Access validates an explicitly supplied port.
                url.port
            except ValueError:
                continue
            media_type = enclosure.get("type", "").split(";", 1)[0].strip().lower()
            if media_type in cls.AUDIO_TYPES:
                return href
            if (
                not media_type
                and url.path.lower().rsplit(".", 1)[-1] in cls.AUDIO_SUFFIXES
            ):
                fallback = fallback or href
        if fallback:
            return fallback
        raise ValueError("No supported audio enclosure")

    @classmethod
    def parse_feed_entries(cls, entries):
        """Keep usable episodes and report a bounded count of skipped entries."""
        episodes = []
        skipped = 0
        for entry in entries:
            try:
                episode = {
                    "guid": entry["id"],
                    "audio_url": cls.audio_url(entry),
                    "published": parsed_date(entry["published_parsed"]),
                    "title": entry["title"],
                }
                if not all(
                    isinstance(episode[key], str) and episode[key].strip()
                    for key in ("guid", "title")
                ):
                    raise ValueError("Missing episode identity or title")
            except (KeyError, AttributeError, TypeError, ValueError, IndexError):
                skipped += 1
                continue
            episodes.append(episode)
        if not episodes:
            raise FeedError("Feed has no usable audio episodes")
        if skipped:
            warnings.warn(
                f"Skipped {skipped} unusable feed entry/entries", stacklevel=2
            )
        episodes.sort(key=itemgetter("published"))
        for index, episode in enumerate(episodes, 1):
            episode["index"] = index
        return episodes

    def get(self, url, directory, episode_name_pattern):
        """Fetch a feed; channel update defaults to the newest usable episode."""
        document = feedparser.parse(url)
        title = document.get("feed", {}).get("title")
        if not isinstance(title, str) or not title.strip():
            raise FeedError("Feed is missing a title")

        episodes_from_feed = self.parse_feed_entries(document.get("entries", []))
        validated_episodes = [EpisodeInDb(**episode) for episode in episodes_from_feed]
        updated = validated_episodes[-1].published
        channel = document["feed"]
        # Read raw keys rather than feedparser's deprecated updated/pubDate alias.
        for date in (
            dict.get(channel, "updated_parsed"),
            dict.get(channel, "published_parsed"),
            dict.get(document, "updated_parsed"),  # HTTP Last-Modified
        ):
            if date:
                try:
                    updated = parsed_date(date)
                except (TypeError, ValueError, IndexError):
                    continue
                break
        feed_in_db = FeedInDb(url=url, updated=updated)

        # validate and build podcast
        podcast_in_db = PodcastInDb(
            feed=feed_in_db,
            title=title,
            file_name_pattern=episode_name_pattern,
            episodes_count=len(validated_episodes),
            directory=directory,
        )
        podcast = Podcast.from_dict(podcast_in_db.dict())

        # validate and build episodes
        episodes = [
            Episode.from_dict(episode.dict() | {"podcast": podcast})
            for episode in validated_episodes
        ]

        return podcast, episodes
