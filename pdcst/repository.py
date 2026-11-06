"""
Repositories to store podcasts/episodes.

Atm only uses json. But this could also be a sqlite or a
remote api, maybe fastAPI... hmm.
"""
import fcntl
import time
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from operator import itemgetter
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx
from pydantic import BaseModel, parse_file_as, validator

from .models import Episode, Podcast
from .storage import replacement_file


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
        self.base_dir = Path(str(base_dir))

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

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _write_models(self, locked):
        models = self.storage_model_list_type(__root__=list(locked.values()))
        serialized = models.json()
        with replacement_file(self.path) as output:
            output.write(serialized)

    def add(self, model):
        """Add a single model to the repository."""
        self.add_list([model])

    def add_list(self, models):
        """Reload and atomically commit models under a stable advisory lock."""
        models_in_db = [self.storage_model_type(**model.dict()) for model in models]
        with self._lock():
            locked = self._fetch_all()
            for model_in_db in models_in_db:
                locked[model_in_db.pk] = model_in_db
            self._write_models(locked)
            self._all = None


class EpisodeRepository(JsonRepository):
    """
    Store episodes metadata and audio files for a podcast.
    """

    def import_list(self, models):
        """Refresh feed metadata while retaining usable local download state."""
        from . import contained_path

        incoming = [self.storage_model_type(**model.dict()) for model in models]
        with self._lock():
            locked = self._fetch_all()
            for model in incoming:
                previous = locked.get(model.pk)
                model.audio_file = None
                if previous and previous.audio_file:
                    name = previous.audio_file
                    try:
                        path = contained_path(self.base_dir, name)
                        if (
                            Path(name).name == name
                            and "\\" not in name
                            and path.is_file()
                        ):
                            model.audio_file = name
                    except ValueError:
                        pass
                locked[model.pk] = model
            self._write_models(locked)
            self._all = None

    name = "episodes.json"
    storage_model_type = EpisodeInDb
    storage_model_list_type = EpisodeListInDb
    domain_model_type = Episode


class PodcastRepository(JsonRepository):
    """
    Store podcasts metadata.
    """

    def list(self, additional_attributes=None):
        from . import contained_path

        podcasts = super().list(additional_attributes)
        for podcast, stored in zip(podcasts, self.all.values()):
            if stored.directory is None:
                legacy = stored.title.lower().replace(" ", "_")
                try:
                    path = contained_path(self.base_dir, legacy)
                except ValueError:
                    continue
                if path.is_dir():
                    podcast.directory = legacy
        return podcasts

    name = "podcasts.json"
    storage_model_type = PodcastInDb
    storage_model_list_type = PodcastListInDb
    domain_model_type = Podcast


class FeedParserRepository:
    """
    Fetch/parse feed data from http.
    """

    MAX_BYTES = 8 * 1024 * 1024
    MAX_SECONDS = 30
    MAX_REDIRECTS = 5

    @classmethod
    def fetch_document(cls, url):
        """Acquire bounded, uncompressed feed bytes before parsing."""
        deadline = time.monotonic() + cls.MAX_SECONDS

        def remaining():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise FeedError("Feed fetch exceeded time limit")
            return min(10, budget)

        try:
            with httpx.Client(follow_redirects=False) as client:
                for redirects in range(cls.MAX_REDIRECTS + 1):
                    with client.stream(
                        "GET",
                        url,
                        timeout=remaining(),
                        headers={"Accept-Encoding": "identity"},
                    ) as response:
                        remaining()
                        if response.has_redirect_location:
                            if redirects == cls.MAX_REDIRECTS:
                                raise FeedError("Feed has too many redirects")
                            url = urljoin(
                                str(response.url), response.headers["location"]
                            )
                            continue
                        response.raise_for_status()
                        encoding = response.headers.get("content-encoding", "identity")
                        if encoding.strip().lower() not in {"", "identity"}:
                            raise FeedError("Unsupported feed content encoding")
                        length = response.headers.get("content-length", "")
                        if (
                            length.isascii()
                            and length.isdigit()
                            and int(length) > cls.MAX_BYTES
                        ):
                            raise FeedError("Feed exceeds byte limit")
                        body = bytearray()
                        for chunk in response.iter_raw():
                            remaining()
                            if len(body) + len(chunk) > cls.MAX_BYTES:
                                raise FeedError("Feed exceeds byte limit")
                            body.extend(chunk)
                        remaining()
                        headers = dict(response.headers)
                        headers["content-location"] = urljoin(
                            str(response.url), headers.get("content-location", "")
                        )
                        document = feedparser.parse(
                            bytes(body), response_headers=headers
                        )
                        for entry in document.entries:
                            for enclosure in entry.get("links", []):
                                if enclosure.get("rel") != "enclosure":
                                    continue
                                href = enclosure.get("href")
                                if isinstance(href, str) and not any(
                                    c.isspace() for c in href
                                ):
                                    enclosure["href"] = urljoin(
                                        headers["content-location"], href
                                    )
                        modified = headers.get("last-modified")
                        if modified:
                            try:
                                date = as_utc(parsedate_to_datetime(modified))
                                document["updated_parsed"] = date.utctimetuple()
                            except (TypeError, ValueError, OverflowError):
                                pass
                        return document
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as error:
            if isinstance(error, FeedError):
                raise
            raise FeedError("Unable to fetch feed") from error

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
        document = self.fetch_document(url)
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
