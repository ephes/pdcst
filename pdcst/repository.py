"""
Repositories to store podcasts/episodes.

Atm only uses json. But this could also be a sqlite or a
remote api, maybe fastAPI... hmm.
"""
import fcntl
import json
import os
import stat
import time
import unicodedata
import warnings
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from operator import itemgetter
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import feedparser
import httpx
from pydantic import BaseModel, ValidationError, parse_obj_as, validator

from .models import Episode, Podcast
from .storage import replacement_file

STORE_MAX_BYTES = 16 * 1024**2


class StoreReadError(ValueError):
    """Metadata could not be read; diagnostics contain no stored values."""

    def __init__(self, path, category):
        self.path = path
        self.category = category
        super().__init__(f"Cannot read metadata {path}: {category}")


class StoreWriteError(ValueError):
    """Metadata cannot be published within the readable size limit."""


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


class DuplicateIdentity(ValueError):
    """A stored or incoming batch contains repeated identities."""


def index_unique(models, key):
    """Refuse repeated identities, including identical rows, before indexing."""
    indexed = {}
    positions = {}
    for position, model in enumerate(models, 1):
        identity = key(model)
        if identity in indexed:
            raise DuplicateIdentity(
                f"Duplicate identity at positions {positions[identity]} and {position}"
            )
        indexed[identity] = model
        positions[identity] = position
    return indexed


class DownloadConflict(ValueError):
    """Episode identity or destination ownership changed."""


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

    def __init__(self, base_dir):
        self.base_dir = Path(str(base_dir))
        try:
            if self.base_dir.exists() and not self.base_dir.is_dir():
                raise StoreReadError(
                    self.base_dir, "storage directory must be a directory"
                )
        except OSError:
            raise StoreReadError(self.base_dir, "filesystem error") from None

    @property
    def path(self):
        return self.base_dir / self.name

    def _read_bytes(self):
        """Read one bounded regular-file generation, or None for a missing store."""
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise StoreReadError(self.path, "filesystem error") from None
        try:
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise StoreReadError(self.path, "not a regular file")
                if info.st_size > STORE_MAX_BYTES:
                    raise StoreReadError(self.path, "size limit exceeded (16 MiB)")
                data = source.read(STORE_MAX_BYTES + 1)
                if len(data) > STORE_MAX_BYTES:
                    raise StoreReadError(self.path, "size limit exceeded (16 MiB)")
        except OSError:
            raise StoreReadError(self.path, "filesystem error") from None
        return data

    def _fetch_all(self):
        data = self._read_bytes()
        if data is None:
            return {}
        try:
            decoded = data.decode("utf-8")
        except UnicodeError:
            raise StoreReadError(self.path, "invalid UTF-8") from None
        try:
            values = json.loads(decoded)
        except (ValueError, RecursionError):
            raise StoreReadError(self.path, "invalid JSON") from None
        try:
            models = parse_obj_as(list[self.storage_model_type], values)
        except (ValidationError, RecursionError, OverflowError):
            raise StoreReadError(self.path, "invalid schema") from None
        return index_unique(models, lambda model: model.pk)

    @property
    def all(self):
        """Read current models without retaining an unversioned cache."""
        return self._fetch_all()

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

    def _serialize_models(self, locked):
        models = self.storage_model_list_type(__root__=list(locked.values()))
        serialized = models.json().encode("utf-8")
        if len(serialized) > STORE_MAX_BYTES:
            raise StoreWriteError(
                f"Cannot write metadata {self.path}: size limit exceeded (16 MiB)"
            )
        return serialized

    def _write_models(self, locked):
        serialized = self._serialize_models(locked)
        with replacement_file(self.path, binary=True) as output:
            output.write(serialized)

    def add(self, model):
        """Add a single model to the repository."""
        self.add_list([model])

    def add_list(self, models):
        """Reload and atomically commit models under a stable advisory lock."""
        models_in_db = index_unique(
            [self.storage_model_type(**model.dict()) for model in models],
            lambda model: model.pk,
        )
        with self._lock():
            locked = self._fetch_all()
            for model_in_db in models_in_db.values():
                locked[model_in_db.pk] = model_in_db
            self._write_models(locked)


class EpisodeRepository(JsonRepository):
    """
    Store episodes metadata and audio files for a podcast.
    """

    def _download_record(self, locked, guid, audio_url, name):
        """Validate identity and exclusive ownership while holding the JSON lock."""
        from . import contained_path

        reserved = {self.name, self.name + ".lock", self.name + ".download.lock"}
        if name.casefold() in reserved or name.casefold().startswith(
            ".pdcst-download-"
        ):
            raise DownloadConflict(
                "Episode filename is reserved for repository storage"
            )
        if Path(name).name != name:
            raise DownloadConflict("Episode name must be a single relative file name")
        if (self.base_dir / name).is_symlink():
            raise DownloadConflict("Audio destination must not be a symlink")
        target = contained_path(self.base_dir, name)
        current = locked.get(guid)
        if current is None or current.audio_url != audio_url:
            raise DownloadConflict(
                "Episode missing or enclosure changed; retry download"
            )

        def name_key(value):
            return unicodedata.normalize("NFC", value).casefold()

        def path_key(filename):
            return name_key(str(contained_path(self.base_dir, filename)))

        key = path_key(name)
        for row in locked.values():
            if row.guid == guid or not row.audio_file:
                continue
            try:
                owner_key = path_key(row.audio_file)
            except (ValueError, RuntimeError):
                # Unsafe paths and symlink loops cannot own a contained file.
                continue
            if owner_key == key:
                raise DownloadConflict(
                    "Audio destination is already owned by another episode"
                )
        owned = current.audio_file == name
        # Conservative case folding gives the same refusal policy on every host.
        if not owned and (
            target.exists()
            or any(name_key(p.name) == name_key(name) for p in self.base_dir.iterdir())
        ):
            raise DownloadConflict(
                "Audio destination contains an unowned existing file"
            )
        return current, target

    def download_episode(self, episode, name, transfer):
        """Claim audio, transfer outside the JSON lock, then commit only its link."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        claim = self.path.with_suffix(self.path.suffix + ".download.lock")
        with claim.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Closing the descriptor releases the claim even when transfer fails.
            with self._lock():
                self._download_record(
                    self._fetch_all(), episode.guid, episode.audio_url, name
                )
            stage = self.base_dir / f".pdcst-download-{uuid4().hex}"
            descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            os.close(descriptor)
            completed = False
            rollback_failed = False
            try:
                transfer(episode.audio_url, stage)
                completed = True
                with self._lock():
                    locked = self._fetch_all()
                    current, target = self._download_record(
                        locked, episode.guid, episode.audio_url, name
                    )
                    if target.exists():
                        stage.chmod(stat.S_IMODE(target.stat().st_mode))
                    backup = None
                    if target.exists():
                        backup = (
                            self.base_dir / f".pdcst-download-{uuid4().hex}.previous"
                        )
                    published = False
                    previous_moved = False
                    try:
                        if backup is not None:
                            os.replace(target, backup)
                            previous_moved = True
                        os.replace(stage, target)
                        published = True
                        current.audio_file = name
                        self._write_models(locked)
                    except BaseException:
                        # Never overwrite completed bytes if moving them aside fails.
                        if published:
                            try:
                                os.replace(target, stage)
                            except BaseException:
                                rollback_failed = True
                            else:
                                published = False
                        if previous_moved and not published:
                            try:
                                os.replace(backup, target)
                            except BaseException:
                                rollback_failed = True
                        raise
                    else:
                        if backup is not None:
                            try:
                                backup.unlink()
                            except OSError:
                                warnings.warn(
                                    "Download completed; cleanup could not remove "
                                    f"previous audio retained at {backup}",
                                    RuntimeWarning,
                                    stacklevel=2,
                                )
                        return target
            except BaseException as error:
                if rollback_failed:
                    recovery = target if published else stage
                    previous = (
                        f"; previous audio retained at {backup}"
                        if previous_moved
                        else ""
                    )
                    warnings.warn(
                        "Download rollback incomplete; "
                        f"completed audio retained at {recovery}{previous}; "
                        "move retained audio aside or restore it before retrying",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                elif completed and stage.exists():
                    recovery = f"completed audio retained at {stage}"
                    if isinstance(error, DownloadConflict):
                        raise DownloadConflict(f"{error}; {recovery}") from error
                    warnings.warn(recovery, RuntimeWarning, stacklevel=2)
                raise
            finally:
                if not completed:
                    stage.unlink(missing_ok=True)

    def _usable_audio_files(self, locked):
        """Retain only regular, non-symlink files with one unambiguous owner."""
        from . import contained_path

        candidates = {}
        names = Counter()
        files = Counter()
        reserved = {self.name, self.name + ".lock", self.name + ".download.lock"}
        for row in locked.values():
            name = row.audio_file
            if not name:
                continue
            key = unicodedata.normalize("NFC", name).casefold()
            if (
                Path(name).name != name
                or "\\" in name
                or key in reserved
                or key.startswith(".pdcst-download-")
            ):
                continue
            try:
                if (self.base_dir / name).is_symlink():
                    continue
                path = contained_path(self.base_dir, name)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    continue
            except (ValueError, RuntimeError, OSError):
                continue
            identity = (info.st_dev, info.st_ino)
            candidates[row.pk] = (name, key, identity)
            names[key] += 1
            files[identity] += 1
        return {
            guid: name
            for guid, (name, key, identity) in candidates.items()
            if names[key] == 1 and files[identity] == 1
        }

    def _import_locked(self, models, locked):
        incoming = index_unique(
            [self.storage_model_type(**model.dict()) for model in models],
            lambda model: model.pk,
        )
        usable = self._usable_audio_files(locked)
        for model in incoming.values():
            previous = locked.get(model.pk)
            model.audio_file = None
            if previous and previous.audio_url == model.audio_url:
                model.audio_file = usable.get(model.pk)
            locked[model.pk] = model
        self._write_models(locked)

    def import_list(self, models):
        """Refresh feed metadata while retaining usable local download state."""
        with self._lock():
            self._import_locked(models, self._fetch_all())

    @contextmanager
    def subscription_import(self, models):
        """Keep the episode generation owned until subscription publication settles."""
        with self._lock():
            locked = self._fetch_all()
            previous = self._read_bytes()
            backup = self.base_dir / f".pdcst-refresh-{uuid4().hex}.previous"
            recovery = self.base_dir / f".pdcst-refresh-{uuid4().hex}.refreshed"
            if previous is not None:
                with replacement_file(backup, binary=True) as output:
                    output.write(previous)
            retain = False
            try:
                self._import_locked(models, locked)
                try:
                    yield
                except BaseException:
                    try:
                        # Retain the new generation before attempting compensation.
                        os.link(self.path, recovery)
                        if previous is None:
                            self.path.unlink()
                        else:
                            os.replace(backup, self.path)
                    except BaseException:
                        retain = True
                        warnings.warn(
                            "Refresh rollback incomplete; "
                            f"previous metadata retained at {backup}; "
                            f"refreshed metadata at {self.path} or {recovery}",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                    raise
            finally:
                if not retain:
                    backup.unlink(missing_ok=True)
                    recovery.unlink(missing_ok=True)

    name = "episodes.json"
    storage_model_type = EpisodeInDb
    storage_model_list_type = EpisodeListInDb
    domain_model_type = Episode


class PodcastRepository(JsonRepository):
    """
    Store podcasts metadata.
    """

    @contextmanager
    def feed_lock(self, url):
        """Serialize one feed's acquisition through publication across processes."""
        directory = self.base_dir / ".feed-locks"
        directory.mkdir(parents=True, exist_ok=True)
        name = sha256(url.encode("utf-8")).hexdigest() + ".lock"
        with (directory / name).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def list(self, additional_attributes=None):
        from . import contained_path

        stored_models = list(self.all.values())
        podcasts = [
            self.domain_model_type.from_dict(
                model.dict() | (additional_attributes or {})
            )
            for model in stored_models
        ]
        for podcast, stored in zip(podcasts, stored_models):
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
        try:
            index_unique(episodes, lambda episode: episode["guid"])
        except DuplicateIdentity as error:
            raise FeedError(f"{error} (usable feed entries)") from None
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
