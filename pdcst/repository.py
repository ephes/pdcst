"""
Repositories to store podcasts/episodes.

Atm only uses json. But this could also be a sqlite or a
remote api, maybe fastAPI... hmm.
"""

import errno
import fcntl
import json
import os
import re
import stat
import time
import unicodedata
import warnings
from collections import Counter
from contextlib import ExitStack, contextmanager
from copy import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from operator import itemgetter
from pathlib import Path, PureWindowsPath
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
from xml.parsers import expat
from xml.sax import SAXParseException

import feedparser
import httpx
from feedparser.exceptions import CharacterEncodingOverride, NonXMLContentType
from pydantic import BaseModel, ValidationError, conint, constr, parse_obj_as, validator

from .config import settings
from .models import AUDIO_MIME_EXTENSIONS, AUDIO_SUFFIXES, Episode, Podcast
from .storage import (
    DirectoryAdmissionError,
    DirectoryIdentityChanged,
    PublicationDurabilityError,
    admitted_directory,
    replacement_file,
    sync_directory,
)

STORE_MAX_BYTES = 16 * 1024**2
AUDIO_DESTINATION_SCAN_MAX_ENTRIES = 10_000
AUDIO_DESTINATION_SCAN_MAX_SECONDS = 1.0


@dataclass
class _DestinationScanBudget:
    entries: int
    seconds: float
    # Admission may use at most half, so the mandatory publication recheck of
    # the same directory (plus its stage) still fits the remaining budget.
    reserve_recheck: bool = False


# Recoverable well-formedness errors that feedparser's lenient fallback repairs
# (e.g. a bare ``&`` in a title or an HTML entity such as ``&nbsp;`` without a
# DOCTYPE). expat reports only its first error, so such a document is accepted
# only if a strict re-parse succeeds once these constructs are neutralised;
# truncation, mismatched tags or trailing junk later in the body still refuse.
_RECOVERABLE_XML_ERRORS = frozenset(
    {"undefined entity", "not well-formed (invalid token)"}
)
_UNESCAPED_AMPERSAND = re.compile(
    r"&(?!(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9A-Fa-f]+);)"
)
_XML_DECLARATION = re.compile(r"\A\ufeff?(?:<\?xml\b[^>]*\?>)?")


def _strictly_well_formed(body: bytes | bytearray, encoding: object) -> bool:
    # Re-parse the text feedparser decoded (its detected/repaired encoding),
    # re-encoded as UTF-8 without the original declaration.
    if not isinstance(encoding, str) or not encoding:
        encoding = "utf-8"
    try:
        text = bytes(body).decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return False
    text = _UNESCAPED_AMPERSAND.sub("&amp;", _XML_DECLARATION.sub("", text, count=1))
    parser = expat.ParserCreate(encoding="UTF-8", namespace_separator=" ")
    try:
        parser.Parse(text.encode("utf-8", "surrogatepass"), True)
    except (expat.ExpatError, ValueError, UnicodeError):
        return False
    return True


def _feed_parse_refused(document, body: bytes | bytearray) -> bool:
    """Return True when feedparser reports a fatal or incomplete document."""
    if not document.get("bozo"):
        return False
    error = document.get("bozo_exception")
    if isinstance(error, (CharacterEncodingOverride, NonXMLContentType)):
        return False
    if (
        isinstance(error, SAXParseException)
        and error.getMessage() in _RECOVERABLE_XML_ERRORS
    ):
        return not _strictly_well_formed(body, document.get("encoding"))
    return True


class StoreReadError(ValueError):
    """Metadata could not be read; diagnostics contain no stored values."""

    def __init__(self, path, category):
        self.path = path
        self.category = category
        super().__init__(f"Cannot read metadata {path}: {category}")


class _StoreGenerationError(StoreReadError):
    """The read did not obtain one complete metadata generation."""


class StoreWriteError(ValueError):
    """Metadata cannot be safely written or its advisory lock admitted."""


REFRESH_INTENT = ".pdcst-refresh-intent.json"
AUDIO_INTENT = ".pdcst-download-intent.json"


def write_intent(path, details, *, directory_fd=None):
    """Durably record recovery paths before publishing any affected generation."""
    with replacement_file(path, binary=True, directory_fd=directory_fd) as output:
        output.write(json.dumps(details).encode("utf-8"))


def clear_intent(path, *, directory_fd=None):
    try:
        if directory_fd is None:
            path.unlink()
        else:
            os.unlink(path.name, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    if directory_fd is None:
        sync_directory(path.parent)
    else:
        sync_directory(path.parent, directory_fd=directory_fd)


@contextmanager
def advisory_lock(path, *, directory_fd=None):
    """Admit a stable regular inode and wait for it under a monotonic budget."""
    descriptor = None
    deadline = time.monotonic() + settings.lock_wait_seconds
    try:
        try:
            descriptor = os.open(
                path.name if directory_fd is not None else path,
                os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW,
                0o666,
                **({"dir_fd": directory_fd} if directory_fd is not None else {}),
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not a regular lock file")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StoreWriteError(
                        f"Cannot acquire lock {path}: busy (wait limit exceeded); retry later"
                    )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(0.05, remaining))
            named = os.stat(
                path.name if directory_fd is not None else path,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(named.st_mode) or (info.st_dev, info.st_ino) != (
                named.st_dev,
                named.st_ino,
            ):
                raise OSError("lock identity changed")
        except OSError:
            raise StoreWriteError(
                f"Cannot acquire lock {path}: regular stable file required"
            ) from None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)


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


def unique_json_object(pairs):
    """Reject ambiguous object members at every nesting level, without values."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


class DownloadConflict(ValueError):
    """Episode identity or destination ownership changed."""


StoredIdentity = constr(strict=True, min_length=1)


class FeedInDb(BaseModel):
    url: StoredIdentity
    updated: datetime

    _utc_updated = validator("updated", allow_reuse=True)(as_utc)


class EpisodeInDb(BaseModel):
    index: conint(strict=True, gt=0)
    guid: StoredIdentity
    audio_url: StoredIdentity
    published: datetime
    title: str
    audio_file: str | None
    audio_media_type: str | None = None

    @validator("audio_media_type")
    def supported_media_type(cls, value):
        if value is not None and value not in AUDIO_MIME_EXTENSIONS:
            raise ValueError("unsupported audio media type")
        return value

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
    episodes_count: conint(strict=True, ge=0)
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

    _directory_fd = None
    _directory_label = "Storage directory"
    # Absolute path captured when the directory descriptor was admitted.
    _admitted_path = None

    def _check_directory(self):
        """Descriptor-bound operations replace this with their identity checker."""

    @contextmanager
    def admitted(self, *, create=True, parent=None):
        """Use an isolated repository view bound to one held directory.

        A directory below an admitted parent repository is opened relative to
        the parent's held descriptor, and its checker also checks the parent.
        A directory outside an admitted parent is refused, never walked by name.
        """
        if self._directory_fd is not None:
            self._check_directory()
            yield self
            return
        relative = None
        own = Path(os.path.abspath(self.base_dir))
        try:
            if parent is not None and parent._directory_fd is not None:
                parent._check_directory()
                base = parent._admitted_path or Path(os.path.abspath(parent.base_dir))
                try:
                    relative = own.relative_to(base)
                except ValueError:
                    relative = None
                if relative is None or not relative.parts:
                    raise DirectoryIdentityChanged(
                        f"{self._directory_label} is outside the admitted "
                        f"{parent._directory_label.lower()}; restore the "
                        "directory layout before retrying"
                    )
            if relative is None:
                admission = admitted_directory(
                    self.base_dir, create=create, label=self._directory_label
                )
            else:
                admission = admitted_directory(
                    relative,
                    create=create,
                    parent_fd=parent._directory_fd,
                    label=self._directory_label,
                )
            with admission as (descriptor, held):
                if relative is None:
                    check = held
                else:

                    def check():
                        parent._check_directory()
                        held()

                    check()
                admitted = copy(self)
                admitted._directory_fd = descriptor
                admitted._check_directory = check
                admitted._admitted_path = own
                yield admitted
        except DirectoryAdmissionError as error:
            raise StoreWriteError(f"Unable to update local storage: {error}") from None

    def _local(self, path):
        return path.name if self._directory_fd is not None else path

    def _stat(self, path):
        return os.stat(
            self._local(path), dir_fd=self._directory_fd, follow_symlinks=False
        )

    def _exists(self, path):
        try:
            self._stat(path)
        except OSError as error:
            # Like pathlib: an unusable parent is absence here; the store read
            # then reports a bounded StoreReadError instead of a raw OSError.
            if error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                return False
            raise
        return True

    def _unlink(self, path):
        try:
            os.unlink(
                self._local(path),
                **(
                    {"dir_fd": self._directory_fd}
                    if self._directory_fd is not None
                    else {}
                ),
            )
        except FileNotFoundError:
            pass

    def _replace(self, source, target):
        os.replace(
            self._local(source),
            self._local(target),
            src_dir_fd=self._directory_fd,
            dst_dir_fd=self._directory_fd,
        )

    def _link(self, source, target):
        os.link(
            self._local(source),
            self._local(target),
            src_dir_fd=self._directory_fd,
            dst_dir_fd=self._directory_fd,
            follow_symlinks=False,
        )

    def _sync(self):
        sync_directory(self.base_dir, directory_fd=self._directory_fd)

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
            descriptor = os.open(
                self._local(self.path),
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                **(
                    {"dir_fd": self._directory_fd}
                    if self._directory_fd is not None
                    else {}
                ),
            )
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
                after = os.fstat(source.fileno())

                def generation(value):
                    return (
                        value.st_dev,
                        value.st_ino,
                        value.st_size,
                        value.st_mtime_ns,
                    )

                # Atomic replacement unlinks the opened inode, changing ctime
                # without changing its bytes. Still read that coherent descriptor.
                unlinked = after.st_nlink < info.st_nlink
                if generation(info) != generation(after) or (
                    info.st_ctime_ns != after.st_ctime_ns and not unlinked
                ):
                    raise _StoreGenerationError(
                        self.path, "file generation changed; retry"
                    )
                if len(data) != info.st_size:
                    raise _StoreGenerationError(self.path, "incomplete read; retry")
        except OSError:
            raise StoreReadError(self.path, "filesystem error") from None
        return data

    def _lock_path(self):
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _check_intents(self):
        """Refuse a marker left by an interrupted publication.

        Writers publish and retire intents while holding this store's lock. An
        unlocked reader that sees a marker therefore waits (bounded by the lock
        wait limit) for that lock and re-checks: a live publication has retired
        its marker by then, and a marker still present is unresolved.
        """
        markers = self._intent_markers()
        if not markers:
            return
        if not self.__dict__.get("_lock_depth"):
            try:
                with self._lock():
                    pass
            except StoreWriteError as error:
                # Busy past the wait limit, or an unusable lock (e.g. read-only
                # store): name the marker. If the writer retired its marker
                # meanwhile, its publication finished and the read proceeds.
                markers = self._intent_markers()
                if not markers:
                    return
                raise StoreReadError(
                    self.path,
                    f"publication marker {markers[0]} present and its lock is "
                    f"unavailable ({error}); retry later, or follow README "
                    "interrupted-publication recovery",
                ) from error
            return
        marker = markers[0]
        raise StoreReadError(
            self.path,
            f"unresolved publication; inspect {marker} and follow "
            "README interrupted-publication recovery before retrying",
        )

    def _intent_markers(self):
        markers = []
        for name in (REFRESH_INTENT, AUDIO_INTENT):
            marker = self.base_dir / name
            if self._exists(marker):
                markers.append(marker)
        return markers

    def _read_confirmed_bytes(self):
        self._check_intents()
        try:
            data = self._read_bytes()
        except _StoreGenerationError:
            markers = self._intent_markers()
            if not markers:
                raise
        else:
            markers = self._intent_markers()
            if not markers:
                return data

        # The first read may overlap a publication. Wait/recheck using the
        # owner lock and reread a settled generation while holding it.
        if self.__dict__.get("_lock_depth"):
            self._check_intents()
            return self._read_bytes()
        try:
            with self._lock():
                return self._read_bytes()
        except StoreWriteError as error:
            raise StoreReadError(
                self.path,
                f"publication marker {markers[0]} observed during read and its "
                f"lock is unavailable ({error}); retry later, or follow README "
                "interrupted-publication recovery",
            ) from error

    def _fetch_all(self):
        data = self._read_confirmed_bytes()
        if data is None:
            return {}
        try:
            decoded = data.decode("utf-8")
        except UnicodeError:
            raise StoreReadError(self.path, "invalid UTF-8") from None
        try:
            values = json.loads(decoded, object_pairs_hook=unique_json_object)
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
        if self._directory_fd is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with advisory_lock(
            self._lock_path(),
            **(
                {"directory_fd": self._directory_fd}
                if self._directory_fd is not None
                else {}
            ),
        ):
            self.__dict__["_lock_depth"] = self.__dict__.get("_lock_depth", 0) + 1
            try:
                self._check_directory()
                self._check_intents()
                yield
            finally:
                self.__dict__["_lock_depth"] -= 1

    def _serialize_models(self, locked):
        models = self.storage_model_list_type(__root__=list(locked.values()))
        serialized = models.json().encode("utf-8")
        if len(serialized) > STORE_MAX_BYTES:
            raise StoreWriteError(
                f"Cannot write metadata {self.path}: size limit exceeded (16 MiB)"
            )
        return serialized

    def _write_refresh_backup(self, backup, previous):
        """Write the private rollback copy with the original episode file mode.

        The caller syncs the backup directory before publishing its intent.
        """
        try:
            mode = stat.S_IMODE(self._stat(self.path).st_mode)
        except FileNotFoundError:
            mode = None
        descriptor = os.open(
            self._local(backup),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self._directory_fd,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(previous)
                output.flush()
                if mode is not None:
                    os.fchmod(output.fileno(), mode)
                os.fsync(output.fileno())
        finally:
            os.close(descriptor)

    def _write_models(self, locked):
        serialized = self._serialize_models(locked)
        self._check_directory()
        with replacement_file(
            self.path, binary=True, directory_fd=self._directory_fd
        ) as output:
            output.write(serialized)
            self._check_directory()

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

    _directory_label = "Episode directory"

    @staticmethod
    def _audio_generation(info):
        if info is None:
            return None
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def _audio_stage_info(self, stage, descriptor):
        """Admit bounded completed bytes through a nonblocking, no-follow fd."""
        try:
            info = os.fstat(descriptor)
            named = self._stat(stage)
        except OSError:
            raise DownloadConflict(
                "Audio stage unavailable or generation changed"
            ) from None
        if (
            not stat.S_ISREG(info.st_mode)
            or not 0 < info.st_size <= settings.audio_max_bytes
        ):
            raise DownloadConflict(
                "Audio stage must be a nonempty regular file within the audio byte limit"
            )
        if self._audio_generation(info) != self._audio_generation(named):
            raise DownloadConflict("Audio stage generation changed")
        return info

    def _existing_audio_info(self, path):
        """Read one no-follow stat, with absence represented by None."""
        try:
            return self._stat(path)
        except FileNotFoundError:
            return None

    def _audio_destination_info(self, target):
        """An existing owned destination must be a regular, non-link file."""
        info = self._existing_audio_info(target)
        if info is not None:
            if stat.S_ISLNK(info.st_mode):
                raise DownloadConflict("Audio destination must not be a symlink")
            if not stat.S_ISREG(info.st_mode):
                raise DownloadConflict("Audio destination must be a regular file")
        return info

    def _download_record(self, locked, guid, audio_url, name, scan_budget=None):
        """Validate identity and exclusive ownership while holding the JSON lock."""
        reserved = {self.name, self.name + ".lock", self.name + ".download.lock"}
        if name.casefold() in reserved or name.casefold().startswith(
            (".pdcst-download-", ".pdcst-refresh-")
        ):
            raise DownloadConflict(
                "Episode filename is reserved for repository storage"
            )
        if (
            not name
            or name in {".", ".."}
            or "\\" in name
            or PureWindowsPath(name).drive
            or Path(name).name != name
        ):
            raise DownloadConflict("Episode name must be a single relative file name")
        target = self.base_dir / name
        target_info = self._audio_destination_info(target)
        current = locked.get(guid)
        if current is None or current.audio_url != audio_url:
            raise DownloadConflict(
                "Episode missing or enclosure changed; retry download"
            )

        def name_key(value):
            return unicodedata.normalize("NFC", value).casefold()

        def path_key(filename):
            if Path(filename).name != filename or "\\" in filename:
                raise ValueError("Unsafe audio association")
            info = (
                target_info
                if filename == name
                else self._existing_audio_info(self.base_dir / filename)
            )
            if info is not None and not stat.S_ISREG(info.st_mode):
                raise ValueError("Unsafe audio association")
            return name_key(filename)

        key = name_key(name)
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
        occupied = target_info is not None
        if not owned and not occupied:
            occupied = self._scan_audio_destination(key, scan_budget)
        if not owned and occupied:
            raise DownloadConflict(
                "Audio destination contains an unowned existing file"
            )
        return current, target, target_info

    def _scan_audio_destination(self, key, budget):
        if budget is None:
            budget = _DestinationScanBudget(
                AUDIO_DESTINATION_SCAN_MAX_ENTRIES, AUDIO_DESTINATION_SCAN_MAX_SECONDS
            )
        started = time.monotonic()
        entries_left = budget.entries // 2 if budget.reserve_recheck else budget.entries
        deadline = started + (
            budget.seconds / 2 if budget.reserve_recheck else budget.seconds
        )
        consumed = 0

        def refuse():
            raise DownloadConflict(
                "Audio destination ownership scan incomplete (entry/time limit); "
                "narrow episode directory and retry"
            )

        def check_time():
            if time.monotonic() >= deadline:
                refuse()

        try:
            check_time()
            with self._directory_entries() as entries:
                while True:
                    check_time()
                    if consumed >= entries_left:
                        refuse()
                    try:
                        entry = next(entries)
                    except StopIteration:
                        check_time()
                        return False
                    consumed += 1
                    entry_key = unicodedata.normalize("NFC", entry.name).casefold()
                    check_time()
                    if entry_key == key:
                        return True
        finally:
            # Share active scan work across admission/publication, excluding the
            # potentially long transfer and time waiting for either lock.
            budget.entries -= consumed
            budget.seconds = max(0.0, budget.seconds - (time.monotonic() - started))

    def _directory_entries(self):
        # scandir(fd) duplicates the fd; the iterator owns only that duplicate.
        return os.scandir(
            self._directory_fd if self._directory_fd is not None else self.base_dir
        )

    def download_episode(self, episode, name, transfer):
        """Transfer callbacks must use the supplied directory_fd for stage IO."""
        try:
            with admitted_directory(self.base_dir) as (descriptor, check):
                admitted = copy(self)
                admitted._directory_fd = descriptor

                return admitted._download_in_directory(episode, name, transfer, check)
        except DirectoryAdmissionError as error:
            raise DownloadConflict(str(error)) from None

    def _download_in_directory(self, episode, name, transfer, check_directory):
        """Claim audio, transfer outside the JSON lock, then commit only its link."""
        check = check_directory

        def check_directory():
            try:
                check()
            except DirectoryAdmissionError as error:
                raise DownloadConflict(str(error)) from None

        scan_budget = _DestinationScanBudget(
            AUDIO_DESTINATION_SCAN_MAX_ENTRIES,
            AUDIO_DESTINATION_SCAN_MAX_SECONDS,
            reserve_recheck=True,
        )
        claim = self.path.with_suffix(self.path.suffix + ".download.lock")
        with advisory_lock(claim, directory_fd=self._directory_fd):
            check_directory()
            # Closing the descriptor releases the claim even when transfer fails.
            with self._lock():
                self._download_record(
                    self._fetch_all(),
                    episode.guid,
                    episode.audio_url,
                    name,
                    scan_budget,
                )
            scan_budget.reserve_recheck = False
            check_directory()
            stage = self.base_dir / f".pdcst-download-{uuid4().hex}"
            descriptor = os.open(
                stage.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o666,
                dir_fd=self._directory_fd,
            )
            os.close(descriptor)
            completed = False
            rollback_failed = False
            stage_descriptor = None
            try:
                transfer(episode.audio_url, stage, directory_fd=self._directory_fd)
                try:
                    stage_descriptor = os.open(
                        stage.name,
                        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                        dir_fd=self._directory_fd,
                    )
                except OSError:
                    raise DownloadConflict(
                        "Audio stage must be a nonempty regular file"
                    ) from None
                stage_info = self._audio_stage_info(stage, stage_descriptor)
                completed = True
                check_directory()
                with self._lock():
                    locked = self._fetch_all()
                    current, target, target_info = self._download_record(
                        locked, episode.guid, episode.audio_url, name, scan_budget
                    )
                    check_directory()
                    if self._audio_generation(
                        self._audio_stage_info(stage, stage_descriptor)
                    ) != self._audio_generation(stage_info):
                        raise DownloadConflict("Audio stage generation changed")
                    if target_info is not None:
                        os.fchmod(stage_descriptor, stat.S_IMODE(target_info.st_mode))
                        changed = self._audio_stage_info(stage, stage_descriptor)
                        # Our chmod changes mode/ctime, never identity or payload.
                        if (
                            changed.st_dev,
                            changed.st_ino,
                            changed.st_size,
                            changed.st_mtime_ns,
                        ) != (
                            stage_info.st_dev,
                            stage_info.st_ino,
                            stage_info.st_size,
                            stage_info.st_mtime_ns,
                        ):
                            raise DownloadConflict("Audio stage generation changed")
                        stage_info = changed
                    if self._audio_generation(
                        self._audio_destination_info(target)
                    ) != self._audio_generation(target_info):
                        raise DownloadConflict("Audio destination generation changed")
                    backup = None
                    if target_info is not None:
                        backup = (
                            self.base_dir / f".pdcst-download-{uuid4().hex}.previous"
                        )
                    published = False
                    previous_moved = False
                    intent = self.base_dir / AUDIO_INTENT
                    check_directory()
                    write_intent(
                        intent,
                        {
                            "kind": "audio",
                            "metadata": str(self.path),
                            "target": str(target),
                            "stage": str(stage),
                            "backup": str(backup) if backup is not None else None,
                        },
                        directory_fd=self._directory_fd,
                    )
                    try:
                        check_directory()
                        if self._audio_generation(
                            self._audio_destination_info(target)
                        ) != self._audio_generation(target_info):
                            raise DownloadConflict(
                                "Audio destination generation changed"
                            )
                        if self._audio_generation(
                            self._audio_stage_info(stage, stage_descriptor)
                        ) != self._audio_generation(stage_info):
                            raise DownloadConflict("Audio stage generation changed")
                        if backup is not None:
                            self._replace(target, backup)
                            previous_moved = True
                        if self._audio_generation(
                            self._audio_stage_info(stage, stage_descriptor)
                        ) != self._audio_generation(stage_info):
                            raise DownloadConflict("Audio stage generation changed")
                        self._replace(stage, target)
                        published = True
                        self._sync()
                        check_directory()
                        current.audio_file = name
                        self._write_models(locked)
                    except PublicationDurabilityError:
                        # JSON is already visible: keep its matching audio and backup.
                        if backup is not None:
                            warnings.warn(
                                f"Publication durability uncertain; previous audio retained at {backup}",
                                RuntimeWarning,
                                stacklevel=2,
                            )
                        raise
                    except BaseException:
                        # Never overwrite completed bytes if moving them aside fails.
                        if published:
                            try:
                                self._replace(target, stage)
                            except BaseException:
                                rollback_failed = True
                            else:
                                published = False
                        if previous_moved and not published:
                            try:
                                self._replace(backup, target)
                            except BaseException:
                                rollback_failed = True
                        try:
                            self._sync()
                            if not rollback_failed:
                                clear_intent(intent, directory_fd=self._directory_fd)
                        except BaseException:
                            rollback_failed = True
                        raise
                    else:
                        # The matching audio and JSON are both durable. A crash
                        # after this point needs at most orphan-backup cleanup.
                        try:
                            clear_intent(intent, directory_fd=self._directory_fd)
                        except OSError:
                            retained = (
                                f"previous audio retained at {backup}; "
                                if backup is not None
                                else ""
                            )
                            warnings.warn(
                                "Download completed; recovery cleanup pending at "
                                f"{intent}; {retained}"
                                "stop all pdcst commands and follow README "
                                "interrupted-publication recovery; do not repeat transfer",
                                RuntimeWarning,
                                stacklevel=2,
                            )
                            return target
                        if backup is not None:
                            try:
                                self._unlink(backup)
                                self._sync()
                            except OSError:
                                warnings.warn(
                                    "Download completed; cleanup could not remove or "
                                    "durably retire previous audio backup at "
                                    f"{backup}; inspect before removing manually",
                                    RuntimeWarning,
                                    stacklevel=2,
                                )
                        return target
            except BaseException as error:
                if rollback_failed:
                    recovery = target if published else stage
                    previous = (
                        f"; previous audio at {backup} or {target}"
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
                elif completed and self._exists(stage):
                    recovery = f"completed audio retained at {stage}"
                    if isinstance(error, DownloadConflict):
                        try:
                            unchanged = self._audio_generation(
                                self._audio_stage_info(stage, stage_descriptor)
                            ) == self._audio_generation(stage_info)
                        except DownloadConflict:
                            unchanged = False
                        if not unchanged:
                            recovery = f"inspect changed audio stage at {stage}"
                        raise DownloadConflict(f"{error}; {recovery}") from error
                    warnings.warn(recovery, RuntimeWarning, stacklevel=2)
                raise
            finally:
                if stage_descriptor is not None:
                    os.close(stage_descriptor)
                if not completed:
                    # A callback may have replaced the stage with a directory.
                    # Never traverse/remove its contents or mask the refusal.
                    try:
                        info = self._stat(stage)
                    except FileNotFoundError:
                        pass
                    else:
                        if not stat.S_ISDIR(info.st_mode):
                            self._unlink(stage)

    def _usable_audio_files(self, locked):
        """Retain only regular, non-symlink files with one unambiguous owner."""
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
                name in {".", ".."}
                or Path(name).name != name
                or "\\" in name
                or PureWindowsPath(name).drive
                or key in reserved
                or key.startswith((".pdcst-download-", ".pdcst-refresh-"))
            ):
                continue
            try:
                info = self._stat(self.base_dir / name)
            except (FileNotFoundError, ValueError):
                # Absent entries and invalid path strings cannot retain audio.
                continue
            if not stat.S_ISREG(info.st_mode):
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

    def _merge_import(self, models, locked):
        incoming = index_unique(
            [self.storage_model_type(**model.dict()) for model in models],
            lambda model: model.pk,
        )
        usable = self._usable_audio_files(locked)
        next_index = max((row.index for row in locked.values()), default=0) + 1
        # Stores written before indices were stable may hold duplicates from
        # renumbered feed windows. Keep each index on one row (preferring a
        # downloaded row, then the oldest) and move later duplicates after the
        # retained maximum so index-based filenames stay distinct.
        seen = set()
        for row in sorted(
            locked.values(),
            key=lambda row: (row.index, row.pk not in usable, row.published, row.pk),
        ):
            row.audio_file = usable.get(row.pk)
            if row.index in seen:
                row.index = next_index
                next_index += 1
            seen.add(row.index)
        for model in sorted(
            incoming.values(), key=lambda row: (row.published, row.guid)
        ):
            previous = locked.get(model.pk)
            if previous is not None:
                model.index = previous.index
            else:
                model.index = next_index
                next_index += 1
            model.audio_file = None
            if previous and previous.audio_url == model.audio_url:
                model.audio_file = previous.audio_file
            locked[model.pk] = model
        return dict(sorted(locked.items(), key=lambda item: (item[1].index, item[0])))

    def _import_locked(self, models, locked):
        self._write_models(self._merge_import(models, locked))

    def import_list(self, models):
        """Refresh feed metadata while retaining usable local download state."""
        with self._lock():
            self._import_locked(models, self._fetch_all())

    @contextmanager
    def subscription_import(self, models, podcasts_repo=None, prepare=None):
        """Fence an interrupted pair until an operator settles its generations."""
        if podcasts_repo is not None and self._directory_fd is None:
            with self.admitted(parent=podcasts_repo) as admitted:
                with admitted.subscription_import(models, podcasts_repo, prepare):
                    yield
            return
        with self._lock():
            locked = self._merge_import(models, self._fetch_all())
            if prepare is not None:
                prepare(locked)
            previous = self._read_bytes()
            token = uuid4().hex
            backup = self.base_dir / f".pdcst-refresh-{token}.previous"
            recovery = self.base_dir / f".pdcst-refresh-{token}.refreshed"
            marker = self.base_dir / REFRESH_INTENT
            markers = [(self, marker)]
            podcast_backup = None
            podcast_previous = None
            if podcasts_repo is not None:
                podcast_previous = podcasts_repo._read_bytes()
                podcast_backup = (
                    podcasts_repo.base_dir / f".pdcst-refresh-{token}.previous"
                )
                markers.append((podcasts_repo, podcasts_repo.base_dir / REFRESH_INTENT))
            retain = False
            imported = False
            completed = False
            try:
                if previous is not None:
                    self._write_refresh_backup(backup, previous)
                if podcast_previous is not None:
                    podcasts_repo._write_refresh_backup(
                        podcast_backup, podcast_previous
                    )
                details = {
                    "kind": "refresh",
                    "episodes": str(self.path),
                    "previous_episodes": str(backup) if previous is not None else None,
                    "refreshed_episodes": str(recovery),
                    "podcasts": str(podcasts_repo.path) if podcasts_repo else None,
                    "previous_podcasts": (
                        str(podcast_backup) if podcast_previous is not None else None
                    ),
                    "markers": [str(path) for _, path in markers],
                }
                # Backup directory entries must survive before either marker
                # can authorize the first metadata replacement.
                self._sync()
                if podcasts_repo is not None:
                    podcasts_repo._sync()
                # Check every held directory before the first intent exists.
                for repo, _ in markers:
                    repo._check_directory()
                for repo, path in markers:
                    repo._check_directory()
                    write_intent(
                        path,
                        details,
                        **(
                            {"directory_fd": repo._directory_fd}
                            if repo._directory_fd is not None
                            else {}
                        ),
                    )
                self._write_models(locked)
                imported = True
                yield
                completed = True
            except BaseException as error:
                episodes_unsynced = isinstance(error, PublicationDurabilityError)
                if episodes_unsynced:
                    if error.path != self.path:
                        # Subscription or intent visible with uncertain sync:
                        # keep both generations and all recovery evidence.
                        retain = True
                        raise
                    imported = True
                if imported:
                    try:
                        self._link(self.path, recovery)
                        self._sync()
                        if previous is None:
                            self._unlink(self.path)
                        else:
                            self._replace(backup, self.path)
                        self._sync()
                    except BaseException:
                        retain = True
                        warnings.warn(
                            "Refresh rollback incomplete; "
                            f"previous metadata at {backup} or {self.path}; "
                            f"refreshed metadata at {self.path} or {recovery}; "
                            f"recovery intent at {marker}",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                    else:
                        if episodes_unsynced:
                            raise StoreWriteError(
                                f"Refresh of {self.path} could not be synced and "
                                "was rolled back; previous episodes retained"
                            ) from error
                raise
            finally:
                if not retain:
                    # Retire markers only after successful publication or a
                    # synced rollback. Keep backups if marker retirement fails.
                    pending_error = not completed
                    try:
                        for repo, path in markers:
                            clear_intent(
                                path,
                                **(
                                    {"directory_fd": repo._directory_fd}
                                    if repo._directory_fd is not None
                                    else {}
                                ),
                            )
                    except BaseException as error:
                        if not pending_error:
                            if not isinstance(error, OSError):
                                raise
                            warnings.warn(
                                "Refresh completed; recovery cleanup pending at "
                                + ", ".join(str(path) for _, path in markers)
                                + "; metadata backups retained; stop all pdcst "
                                "commands and follow README interrupted-publication "
                                "recovery; do not repeat refresh",
                                RuntimeWarning,
                                stacklevel=2,
                            )
                        else:
                            warnings.warn(
                                f"Refresh recovery cleanup incomplete; inspect {marker}",
                                RuntimeWarning,
                                stacklevel=2,
                            )
                    else:
                        try:
                            self._unlink(backup)
                            self._unlink(recovery)
                            if podcast_backup is not None:
                                podcasts_repo._unlink(podcast_backup)
                        except OSError:
                            if pending_error:
                                message = "Refresh recovery cleanup incomplete"
                            else:
                                message = "Refresh completed; backup cleanup incomplete"
                            warnings.warn(
                                f"{message}; inspect {backup}, {recovery}, {podcast_backup}",
                                RuntimeWarning,
                                stacklevel=2,
                            )

    name = "episodes.json"
    storage_model_type = EpisodeInDb
    storage_model_list_type = EpisodeListInDb
    domain_model_type = Episode


class PodcastRepository(JsonRepository):
    """
    Store podcasts metadata.
    """

    _directory_label = "Storage root directory"

    def _fetch_all(self):
        if self._directory_fd is not None:
            return super()._fetch_all()
        try:
            try:
                self.base_dir.lstat()
            except FileNotFoundError:
                return {}
            with self.admitted(create=False) as admitted:
                return super(PodcastRepository, admitted)._fetch_all()
        except (StoreWriteError, OSError):
            raise StoreReadError(
                self.path, "storage directory unavailable or changed"
            ) from None

    def add_list(self, models):
        with self.admitted() as admitted:
            return super(PodcastRepository, admitted).add_list(models)

    @contextmanager
    def feed_lock(self, url):
        """Serialize one feed's acquisition through publication across processes."""
        name = sha256(url.encode("utf-8")).hexdigest() + ".lock"
        with self.admitted() as admitted, ExitStack() as stack:
            # Only admission and acquisition are lock failures; identity errors
            # raised by the caller while the lock is held keep their own label.
            try:
                descriptor, check_locks = stack.enter_context(
                    admitted_directory(
                        ".feed-locks",
                        parent_fd=admitted._directory_fd,
                        label="Feed lock directory",
                    )
                )

                def check():
                    admitted._check_directory()
                    check_locks()

                check()
                directory = self.base_dir / ".feed-locks"
                stack.enter_context(
                    advisory_lock(directory / name, directory_fd=descriptor)
                )
                check()
            except DirectoryAdmissionError as error:
                raise StoreWriteError(f"Cannot acquire feed lock: {error}") from None
            locked = copy(admitted)
            locked._check_directory = check
            yield locked

    def _listed_podcast(self, stored, additional_attributes=None):
        from . import contained_path

        podcast = self.domain_model_type.from_dict(
            stored.dict() | (additional_attributes or {})
        )
        if stored.directory is None:
            podcast._legacy_directory = True
            legacy = stored.title.lower().replace(" ", "_")
            try:
                path = contained_path(self.base_dir, legacy)
            except ValueError:
                return podcast
            if path.is_dir():
                podcast.directory = legacy
        return podcast

    def list(self, additional_attributes=None):
        from . import _subscription_directory_key

        podcasts = [
            self._listed_podcast(stored, additional_attributes)
            for stored in self.all.values()
        ]
        if not any(podcast._legacy_directory for podcast in podcasts):
            return podcasts

        owners = {}
        for podcast in podcasts:
            try:
                key = _subscription_directory_key(self.base_dir, podcast.directory)
            except ValueError:
                # Unsafe legacy paths cannot own a contained destination.
                continue
            owner = owners.get(key)
            if owner is not None and (
                owner._legacy_directory or podcast._legacy_directory
            ):
                raise StoreReadError(
                    self.path,
                    "Podcast directory is already used by another feed; "
                    "choose a distinct --podcast-dir for one subscription",
                )
            owners[key] = podcast
        return podcasts

    @contextmanager
    def download_subscription(self, podcast):
        """Pin the directory of a legacy subscription after a completed download.

        The directory is validated like a subscription directory, and refused
        when another feed already uses it, before any download starts.
        """
        from . import (
            _subscription_directory_key,
            contained_path,
            subscription_directory,
        )

        if not podcast._legacy_directory:
            yield podcast, None
            return

        with self.admitted(create=False) as admitted, ExitStack() as migration:
            migration.enter_context(admitted._lock())
            rows = admitted._fetch_all()
            stored = rows.get(podcast.feed.url)
            if stored is None:
                raise DownloadConflict("Podcast subscription changed; retry download")
            current = admitted._listed_podcast(stored)
            root = admitted.base_dir.resolve()
            resolved = contained_path(root, current.directory)
            admitted._check_directory()
            if stored.directory is not None:
                migration.close()
                yield current, resolved
                return

            try:
                subscription_directory(current.directory, root=root)
            except ValueError as error:
                raise DownloadConflict(
                    f"{error}; set a distinct directory in podcasts.json"
                ) from None

            target = _subscription_directory_key(root, current.directory)
            for url, other in rows.items():
                if url == podcast.feed.url:
                    continue
                try:
                    other_key = _subscription_directory_key(
                        root, admitted._listed_podcast(other).directory
                    )
                except ValueError:
                    # Unsafe legacy paths cannot own a contained destination.
                    continue
                if other_key == target:
                    raise DownloadConflict(
                        "Podcast directory is already used by another feed; "
                        "set a distinct directory in podcasts.json"
                    )

            current.directory = str(resolved.relative_to(root))
            stored.directory = current.directory
            admitted._serialize_models(rows)
            yield current, resolved
            try:
                admitted._write_models(rows)
            except PublicationDurabilityError:
                raise
            except (OSError, StoreWriteError, DirectoryAdmissionError):
                raise StoreWriteError(
                    f"Audio saved in {resolved}, but the subscription directory "
                    "could not be saved. Check local storage and set the directory "
                    "in podcasts.json before retrying."
                ) from None

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
    MAX_XML_DEPTH = 64
    MAX_XML_NODES = 100_000
    MAX_XML_ENTRIES = 10_000
    MAX_XML_LINKS = 50_000
    MAX_XML_ATTRIBUTES = 200_000
    MAX_XML_BYTES = 16 * 1024**2

    @classmethod
    def preflight_xml(cls, body, headers, remaining):
        """Count XML work without building a tree or expanding DTD entities."""
        remaining()
        # Use the same charset detection/repair as the later feed parse, without
        # constructing entries. Neutralise only the tolerated entity spellings.
        converted = feedparser.encodings.convert_to_utf8(headers, bytes(body), {})
        if len(converted) > cls.MAX_XML_BYTES:
            raise FeedError("Feed XML decoded byte limit exceeded")
        text = _UNESCAPED_AMPERSAND.sub("&amp;", converted.decode("utf-8"))
        normalized = text.encode("utf-8")
        if len(normalized) > cls.MAX_XML_BYTES:
            raise FeedError("Feed XML repaired byte limit exceeded")
        parser = expat.ParserCreate(namespace_separator=" ")
        depth = nodes = entries = links = attributes = 0

        def start(name, attrs):
            nonlocal depth, nodes, entries, links, attributes
            depth += 1
            nodes += 1
            attributes += len(attrs)
            local = name.rsplit(" ", 1)[-1].lower()
            entries += local in {"item", "entry"}
            links += local in {"link", "enclosure"}
            if (
                depth > cls.MAX_XML_DEPTH
                or nodes > cls.MAX_XML_NODES
                or entries > cls.MAX_XML_ENTRIES
                or links > cls.MAX_XML_LINKS
                or attributes > cls.MAX_XML_ATTRIBUTES
            ):
                raise FeedError("Feed XML work limit exceeded")
            if nodes % 256 == 0:
                remaining()

        def end(_name):
            nonlocal depth
            depth -= 1

        def namespace(_prefix, _uri):
            nonlocal attributes
            attributes += 1
            if attributes > cls.MAX_XML_ATTRIBUTES:
                raise FeedError("Feed XML work limit exceeded")

        def doctype(*_args):
            raise FeedError("Feed XML DOCTYPE is unsupported")

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.StartNamespaceDeclHandler = namespace
        parser.StartDoctypeDeclHandler = doctype
        try:
            for offset in range(0, len(normalized), 64 * 1024):
                remaining()
                stop = offset + 64 * 1024
                parser.Parse(normalized[offset:stop], False)
            parser.Parse(b"", True)
        except (expat.ExpatError, ValueError, UnicodeError) as error:
            if isinstance(error, FeedError):
                raise
            raise FeedError("Feed contains malformed or incomplete XML") from None
        remaining()

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
                        if length.isascii() and length.isdigit():
                            digits = length.lstrip("0") or "0"
                            limit = str(cls.MAX_BYTES)
                            if len(digits) > len(limit) or (
                                len(digits) == len(limit) and digits > limit
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
                        cls.preflight_xml(body, headers, remaining)
                        document = feedparser.parse(
                            bytes(body), response_headers=headers
                        )
                        remaining()
                        if _feed_parse_refused(document, body):
                            raise FeedError("Feed contains malformed or incomplete XML")
                        for entry in document.entries:
                            remaining()
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
        except FeedError:
            raise
        except httpx.HTTPStatusError as error:
            failure = f"Unable to fetch feed (HTTP {error.response.status_code})"
        except httpx.TimeoutException:
            failure = "Unable to fetch feed (timeout)"
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            failure = "Unable to fetch feed"
        # Raise outside the handler so library callers receive no raw transport
        # cause or context, including signed URLs in formatted tracebacks.
        raise FeedError(failure)

    AUDIO_TYPES = frozenset(AUDIO_MIME_EXTENSIONS)
    AUDIO_SUFFIXES = AUDIO_SUFFIXES

    @classmethod
    def audio_url(cls, entry):
        """Keep the URL-only API while retaining types in parsed episodes."""
        return cls.audio_enclosure(entry)[0]

    @classmethod
    def audio_enclosure(cls, entry):
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
                return href, media_type
            if (
                not media_type
                and url.path.lower().rsplit(".", 1)[-1] in cls.AUDIO_SUFFIXES
            ):
                fallback = fallback or (href, None)
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
                audio_url, media_type = cls.audio_enclosure(entry)
                episode = {
                    "guid": entry["id"],
                    "audio_url": audio_url,
                    "audio_media_type": media_type,
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
