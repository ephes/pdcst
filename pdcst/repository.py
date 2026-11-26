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
from math import isfinite
from pathlib import Path, PureWindowsPath
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
from xml.parsers import expat
from xml.sax import SAXParseException

import feedparser
import httpx
from feedparser.exceptions import CharacterEncodingOverride, NonXMLContentType
from pydantic import (
    BaseModel,
    PydanticValueError,
    ValidationError,
    conint,
    constr,
    parse_obj_as,
    validator,
)

from .config import settings
from .download import GENERIC_BINARY_MEDIA_TYPES, DownloadError
from .models import (
    AUDIO_MIME_EXTENSIONS,
    AUDIO_SUFFIXES,
    Episode,
    Podcast,
    audio_format_changed,
    effective_audio_format,
)
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
_OGG_ENCLOSURE_URL = re.compile(
    r"(https?://[^/?#\s]+/[^?#\s]*)\.(oga|ogg)([?#]\S*)?", re.IGNORECASE
)


def _audio_size_admitted(info):
    return 0 < info.st_size <= settings.audio_max_bytes


def _same_enclosure_for_retention(previous_url, current_url):
    """Treat an Ogg suffix swap as the same enclosure for existing downloads."""
    if previous_url == current_url:
        return True
    previous = _OGG_ENCLOSURE_URL.fullmatch(previous_url)
    current = _OGG_ENCLOSURE_URL.fullmatch(current_url)
    if previous is None or current is None:
        return False
    previous_stem, previous_suffix, previous_tail = previous.groups()
    current_stem, current_suffix, current_tail = current.groups()
    return (
        previous_stem == current_stem
        and previous_suffix.lower() != current_suffix.lower()
        and previous_tail == current_tail
    )


@dataclass
class _DestinationScanBudget:
    entries: int
    seconds: float
    # Reserve more than half of the entries (and half of the time) for the
    # mandatory publication recheck, which also sees the transfer stage.
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


class SharedDirectoryError(StoreReadError):
    """Subscriptions map to a directory another feed uses.

    ``podcasts`` holds the full listing and ``shared`` the subscriptions
    involved; the message itself contains no stored values.
    """

    def __init__(self, path, podcasts, shared):
        self.podcasts = podcasts
        self.shared = shared
        super().__init__(
            path,
            "Podcast directory is already used by another feed; "
            "choose a distinct --podcast-dir for one subscription",
        )


# A stored name that cannot refer to any directory entry retains no audio.
_ABSENT_NAME_ERRNOS = frozenset({errno.ENOENT, errno.ENAMETOOLONG, errno.EILSEQ})


class StoreWriteError(ValueError):
    """Metadata cannot be safely written or its advisory lock admitted."""


REFRESH_INTENT = ".pdcst-refresh-intent.json"
AUDIO_INTENT = ".pdcst-download-intent.json"


def write_intent(path, details, *, directory_fd=None):
    """Durably record recovery paths before publishing any affected generation."""
    with replacement_file(path, binary=True, directory_fd=directory_fd) as output:
        output.write(json.dumps(details).encode("utf-8"))


def clear_intent(path, *, directory_fd=None):
    """Durably retire an owned intent, including after an earlier unlink."""
    opened_here = directory_fd is None
    if opened_here:
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            return
    try:
        try:
            os.unlink(path.name, dir_fd=directory_fd)
        except FileNotFoundError:
            # An earlier attempt may have unlinked the marker without syncing.
            pass
        sync_directory(path.parent, directory_fd=directory_fd)
    finally:
        if opened_here:
            os.close(directory_fd)


def _warn_completed_operation(message, *, category=RuntimeWarning):
    try:
        warnings.warn(message, category, stacklevel=3)
    except Exception:
        # Warning filters and custom handlers can raise during diagnostics.
        pass


def _retain_recovery(failure, message):
    """Attach recovery locations to a propagating failure, then warn best effort.

    The failure stays the reported outcome: a raising warning filter or handler
    can neither replace it nor discard the retained paths it names.
    """
    try:
        if hasattr(failure, "add_note"):
            failure.add_note(message)
        else:
            # Python 3.10: the attribute later versions render with tracebacks.
            failure.__notes__ = [*getattr(failure, "__notes__", ()), message]
    except Exception:
        pass
    _warn_completed_operation(message)


@contextmanager
def advisory_lock(path, *, directory_fd=None):
    """Admit a stable single-link inode under a monotonic wait budget."""
    descriptor = None
    deadline = time.monotonic() + settings.lock_wait_seconds

    def remaining_wait():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StoreWriteError(
                f"Cannot acquire lock {path}: busy (wait limit exceeded); retry later"
            )
        return remaining

    try:
        try:
            descriptor = os.open(
                path.name if directory_fd is not None else path,
                os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW,
                0o666,
                **({"dir_fd": directory_fd} if directory_fd is not None else {}),
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("not a single-link regular lock file")
            while True:
                remaining = remaining_wait()
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
            if (
                not stat.S_ISREG(named.st_mode)
                or named.st_nlink != 1
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise OSError("lock identity changed")
            remaining_wait()
        except OSError:
            raise StoreWriteError(
                f"Cannot acquire lock {path}: regular stable file required with exactly one hard link"
            ) from None
        yield
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass  # Preserve the admission or operation failure.
        raise
    else:
        try:
            os.close(descriptor)
        except OSError:
            _warn_completed_operation(
                f"Operation completed; lock release uncertain for {path}"
            )


class _UTCDateRangeError(PydanticValueError):
    code = "utc_date_range"
    msg_template = "date is outside the UTC date range"


def as_utc(value):
    """Legacy naive storage values represent UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    try:
        return value.astimezone(timezone.utc)
    except OverflowError:
        raise _UTCDateRangeError() from None


def parsed_date(value):
    """Feedparser supplies UTC date tuples."""
    return datetime(*value[:6], tzinfo=timezone.utc)


def _episode_date(entry):
    for field in ("published_parsed", "updated_parsed"):
        # Check actual keys to avoid feedparser's deprecated updated alias.
        if field not in entry.keys():
            continue
        try:
            return parsed_date(entry[field])
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
    raise ValueError("Missing usable episode timestamp")


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


def finite_json_float(token):
    value = float(token)
    if not isfinite(value):
        raise ValueError("nonfinite JSON number")
    return value


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
            values = json.loads(
                decoded,
                object_pairs_hook=unique_json_object,
                parse_constant=finite_json_float,
                parse_float=finite_json_float,
            )
        except (ValueError, RecursionError):
            raise StoreReadError(self.path, "invalid JSON") from None
        try:
            models = parse_obj_as(list[self.storage_model_type], values)
        except ValidationError as error:
            for issue in error.errors():
                if issue["type"] == "value_error.utc_date_range":
                    field = {"published": "published", "updated": "feed.updated"}.get(
                        issue["loc"][-1]
                    )
                    if field:
                        raise StoreReadError(
                            self.path,
                            f"invalid schema: {field} is outside the UTC date range",
                        ) from None
            raise StoreReadError(self.path, "invalid schema") from None
        except (RecursionError, OverflowError):
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
        try:
            serialized = models.json(allow_nan=False).encode("utf-8")
        except ValueError:
            raise StoreWriteError(
                f"Cannot write metadata {self.path}: invalid JSON"
            ) from None
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
            self.path,
            binary=True,
            directory_fd=self._directory_fd,
            before_replace=self._check_directory,
        ) as output:
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
        if not stat.S_ISREG(info.st_mode) or not _audio_size_admitted(info):
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

    def _download_record(
        self, locked, guid, audio_url, selected_format, name, scan_budget=None
    ):
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
        if (
            current is None
            or current.audio_url != audio_url
            or audio_format_changed(
                selected_format,
                effective_audio_format(current.audio_url, current.audio_media_type),
            )
        ):
            raise DownloadConflict(
                "Episode missing or enclosure changed; retry download"
            )

        def name_key(value):
            return unicodedata.normalize("NFC", value).casefold()

        key = name_key(name)
        for row in locked.values():
            filename = row.audio_file
            if row.guid == guid or not filename or name_key(filename) != key:
                continue
            if Path(filename).name != filename or "\\" in filename:
                continue
            try:
                info = (
                    target_info
                    if filename == name
                    else self._existing_audio_info(self.base_dir / filename)
                )
            except (ValueError, RuntimeError):
                # Unsafe paths and symlink loops cannot own a contained file.
                continue
            if info is None or stat.S_ISREG(info.st_mode):
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
        # Admission reserves one more entry than it may use: the recheck lists
        # the same directory plus the transfer stage.
        entries_left = (
            (budget.entries - 1) // 2 if budget.reserve_recheck else budget.entries
        )
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
                    try:
                        entry = next(entries)
                    except StopIteration:
                        check_time()
                        return False
                    check_time()
                    # At the cap, this entry serves only as an overflow sentinel.
                    if consumed >= entries_left:
                        refuse()
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
            if self._directory_fd is not None:
                return self._download_in_directory(
                    episode, name, transfer, self._check_directory
                )
            with admitted_directory(self.base_dir) as (descriptor, check):
                admitted = copy(self)
                admitted._directory_fd = descriptor

                return admitted._download_in_directory(episode, name, transfer, check)
        except DirectoryAdmissionError as error:
            raise DownloadConflict(str(error)) from None

    def _download_in_directory(self, episode, name, transfer, check_directory):
        """Claim audio, transfer outside the JSON lock, then commit only its link."""
        check = check_directory
        guid, audio_url = episode.guid, episode.audio_url
        selected_format = effective_audio_format(
            audio_url, getattr(episode, "audio_media_type", None)
        )

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
        check_directory()
        with advisory_lock(claim, directory_fd=self._directory_fd):
            check_directory()
            # Closing the descriptor releases the claim even when transfer fails.
            with self._lock():
                self._download_record(
                    self._fetch_all(),
                    guid,
                    audio_url,
                    selected_format,
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
                try:
                    transfer(audio_url, stage, directory_fd=self._directory_fd)
                except PublicationDurabilityError:
                    raise DownloadError(
                        "Unable to stage audio download: directory sync failed"
                    ) from None
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
                        locked, guid, audio_url, selected_format, name, scan_budget
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
                    except PublicationDurabilityError as error:
                        # JSON is already visible: keep its matching audio and backup.
                        if backup is not None:
                            _retain_recovery(
                                error,
                                "Publication durability uncertain; "
                                f"previous audio retained at {backup}",
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
                            _warn_completed_operation(
                                "Download completed; recovery cleanup pending at "
                                f"{intent}; {retained}"
                                "stop all pdcst commands and follow README "
                                "interrupted-publication recovery; do not repeat transfer"
                            )
                            return target
                        if backup is not None:
                            try:
                                self._unlink(backup)
                                self._sync()
                            except OSError:
                                _warn_completed_operation(
                                    "Download completed; cleanup could not remove or "
                                    "durably retire previous audio backup at "
                                    f"{backup}; inspect before removing manually"
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
                    _retain_recovery(
                        error,
                        "Download rollback incomplete; "
                        f"completed audio retained at {recovery}{previous}; "
                        "move retained audio aside or restore it before retrying",
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
                    _retain_recovery(error, recovery)
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
        """Retain only size-admitted regular files with one unambiguous owner."""
        candidates = {}
        refused_size = 0
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
            except ValueError:
                # Invalid path strings cannot retain audio.
                continue
            except OSError as error:
                # Absent entries and impossible names cannot retain audio;
                # permission and I/O failures abort the refresh.
                if error.errno in _ABSENT_NAME_ERRNOS:
                    continue
                raise
            if not stat.S_ISREG(info.st_mode):
                continue
            identity = (info.st_dev, info.st_ino)
            names[key] += 1
            files[identity] += 1
            if _audio_size_admitted(info):
                candidates[row.pk] = (name, key, identity)
            else:
                refused_size += 1
        if refused_size:
            _warn_completed_operation(
                f"Ignoring {refused_size} retained audio references to empty files "
                "or files over the audio byte limit",
            )
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
            if (
                previous
                and previous.audio_file
                and _same_enclosure_for_retention(previous.audio_url, model.audio_url)
                and not audio_format_changed(
                    effective_audio_format(
                        previous.audio_url, previous.audio_media_type
                    ),
                    effective_audio_format(model.audio_url, model.audio_media_type),
                )
            ):
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
            # Only markers whose creation was attempted need durable retirement.
            attempted = []
            retain = False
            imported = False
            completed = False
            failure = None
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
                    attempted.append((repo, path))
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
                failure = error
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
                        _retain_recovery(
                            error,
                            "Refresh rollback incomplete; "
                            f"previous metadata at {backup} or {self.path}; "
                            f"refreshed metadata at {self.path} or {recovery}; "
                            f"recovery intent at {marker}",
                        )
                    else:
                        if episodes_unsynced:
                            failure = StoreWriteError(
                                f"Refresh of {self.path} could not be synced and "
                                "was rolled back; previous episodes retained"
                            )
                            raise failure from error
                raise
            finally:
                if not retain:
                    # Retire markers only after successful publication or a
                    # synced rollback. Keep backups if marker retirement fails.
                    pending_error = not completed
                    try:
                        for repo, path in attempted:
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
                            # Published and synced: keep the committed outcome.
                            _warn_completed_operation(
                                "Refresh completed; recovery cleanup pending at "
                                + ", ".join(str(path) for _, path in markers)
                                + "; metadata backups retained; stop all pdcst "
                                "commands and follow README interrupted-publication "
                                "recovery; do not repeat refresh"
                            )
                        else:
                            _retain_recovery(
                                failure,
                                f"Refresh recovery cleanup incomplete; inspect {marker}",
                            )
                    else:
                        backups = [(self, backup), (self, recovery)]
                        if podcast_backup is not None:
                            backups.append((podcasts_repo, podcast_backup))
                        remaining = []
                        for repo, path in backups:
                            try:
                                repo._unlink(path)
                            except OSError:
                                remaining.append(str(path))
                        if remaining:
                            if pending_error:
                                _retain_recovery(
                                    failure,
                                    "Refresh recovery cleanup incomplete; inspect "
                                    + ", ".join(remaining),
                                )
                            else:
                                _warn_completed_operation(
                                    "Refresh completed; backup cleanup incomplete; "
                                    "inspect " + ", ".join(remaining)
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
            except (ValueError, RuntimeError):
                return podcast
            if path.is_dir():
                podcast.directory = legacy
        return podcast

    def list(self, additional_attributes=None):
        from . import _subscription_owner_key

        podcasts = [
            self._listed_podcast(stored, additional_attributes)
            for stored in self.all.values()
        ]
        groups = {}
        for podcast in podcasts:
            key = _subscription_owner_key(self.base_dir, podcast.directory)
            if key is not None:
                groups.setdefault(key, []).append(podcast)
        shared = [
            podcast for group in groups.values() if len(group) > 1 for podcast in group
        ]
        if shared:
            raise SharedDirectoryError(self.path, podcasts, shared)
        return podcasts

    def _subscription_generation(self, rows, url):
        stored = rows[url]
        return stored, self._listed_podcast(stored)

    def _download_subscription_selection(self, rows, url):
        from . import contained_path, subscription_directory

        try:
            stored, current = self._subscription_generation(rows, url)
        except KeyError:
            raise DownloadConflict(
                "Podcast subscription changed; retry download"
            ) from None
        try:
            if stored.directory is not None:
                return stored, current, contained_path(self.base_dir, current.directory)
            resolved = subscription_directory(current.directory, root=self.base_dir)
        except ValueError as error:
            raise DownloadConflict(
                f"{error}; set a distinct directory in podcasts.json"
            ) from None
        current.directory = str(resolved.relative_to(self.base_dir.resolve()))
        return stored, current, resolved

    def _check_directory_owner(self, rows, url, target):
        from . import _subscription_owner_key

        for other_url, other in rows.items():
            if other_url == url:
                continue
            other_key = _subscription_owner_key(
                self.base_dir, self._listed_podcast(other).directory
            )
            if other_key == target:
                raise DownloadConflict(
                    "Podcast directory is already used by another feed; "
                    "set a distinct directory in podcasts.json"
                )

    def _browse_directory(self, podcast, *, expected=None):
        from . import contained_path

        rows = self._fetch_all()
        url = podcast.feed.url
        try:
            _, current = self._subscription_generation(rows, url)
        except KeyError:
            current = None
        changed = "Podcast subscription directory changed; retry browsing"
        if current is None or current.directory != podcast.directory:
            raise StoreReadError(Path(EpisodeRepository.name), changed)
        try:
            directory = contained_path(self.base_dir, current.directory)
        except (OSError, RuntimeError, ValueError):
            raise StoreReadError(
                Path(EpisodeRepository.name),
                "podcast episode directory is unavailable or unsafe; retry browsing",
            ) from None
        if expected is not None and directory != expected:
            raise StoreReadError(Path(EpisodeRepository.name), changed)
        target = unicodedata.normalize("NFC", str(directory)).casefold()
        try:
            self._check_directory_owner(rows, url, target)
        except DownloadConflict as error:
            raise StoreReadError(Path(EpisodeRepository.name), str(error)) from None
        return directory

    def list_episodes(self, podcast):
        """Read one subscription's episodes and confirm its directory before returning."""
        with self._browse_episode_selection(podcast) as (_, episodes):
            return episodes

    @contextmanager
    def _browse_episode_selection(self, podcast):
        """Hold the checked directory after reading its episode inventory."""
        with ExitStack() as stack:
            try:
                admitted = stack.enter_context(self.admitted(create=False))
                directory = admitted._browse_directory(podcast)
                try:
                    directory.lstat()
                except FileNotFoundError:
                    selected = None
                    episodes = []
                else:
                    selected = stack.enter_context(
                        EpisodeRepository(directory).admitted(
                            create=False, parent=admitted
                        )
                    )
                    episodes = selected.list(additional_attributes={"podcast": podcast})

                # A catalog writer may have committed while the read was open.
                admitted._browse_directory(podcast, expected=directory)
                if selected is None:
                    admitted._check_directory()
                    try:
                        directory.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        raise StoreReadError(
                            Path(EpisodeRepository.name),
                            "Podcast subscription directory changed; retry browsing",
                        )
                else:
                    selected._check_directory()
            except (DirectoryAdmissionError, OSError, StoreWriteError):
                raise StoreReadError(
                    Path(EpisodeRepository.name),
                    "podcast episode directory is unavailable or unsafe; retry browsing",
                ) from None
            yield selected, episodes

    @contextmanager
    def download_selection(self, podcast):
        """Keep the repository used for selection open through the download."""
        with self.download_subscription(podcast) as (current, repository):
            current.validate_audio_filename_pattern()
            if current._legacy_directory:
                episodes = (
                    repository.list(additional_attributes={"podcast": current})
                    if repository is not None
                    else []
                )
                yield current, repository, episodes
            else:
                with self._browse_episode_selection(current) as (selected, episodes):
                    yield current, selected, episodes

    @contextmanager
    def download_subscription(self, podcast):
        """Yield the selected podcast and any reselected episode repository."""
        from . import _subscription_directory_key

        if not podcast._legacy_directory:
            yield podcast, None
            return

        url = podcast.feed.url
        with self.admitted(create=False) as admitted:
            # Refuse reserved paths before creating anything in the feed lock directory.
            stored, current, resolved = admitted._download_subscription_selection(
                admitted._fetch_all(), url
            )
            if stored.directory is not None:
                yield current, EpisodeRepository(resolved)
                return

            with admitted.feed_lock(url) as locked:
                with locked._lock():
                    rows = locked._fetch_all()
                    stored, current, resolved = locked._download_subscription_selection(
                        rows, url
                    )
                    directory = current.directory
                    if stored.directory is None:
                        target = _subscription_directory_key(locked.base_dir, directory)
                        locked._check_directory_owner(rows, url, target)
                        rows[url] = stored.copy(update={"directory": directory})
                        locked._serialize_models(rows)

                episodes = EpisodeRepository(resolved)
                if stored.directory is not None:
                    yield current, episodes
                    return
                if not os.path.lexists(resolved):
                    # A missing directory holds no episodes to select.
                    yield current, None
                    return

                # Keep the directory used by the audio transfer admitted through pinning.
                with episodes.admitted(create=False, parent=locked) as selected:
                    yield current, selected
                    incomplete = (
                        f"Audio saved in {resolved}, but the subscription directory "
                        "could not be saved."
                    )
                    moved = []

                    def check_pin():
                        # Storage root first, so a failure after it names the
                        # episode directory that holds the saved audio.
                        locked._check_directory()
                        try:
                            selected._check_directory()
                        except DirectoryIdentityChanged:
                            moved.append(True)
                            raise

                    try:
                        with locked._lock():
                            rows = locked._fetch_all()
                            if rows.get(url) != stored:
                                raise DownloadConflict("Podcast subscription changed")
                            locked._check_directory_owner(rows, url, target)
                            rows[url] = stored.copy(update={"directory": directory})
                            pin = copy(locked)
                            pin._check_directory = check_pin
                            pin._write_models(rows)
                    except PublicationDurabilityError:
                        raise
                    except DownloadConflict as error:
                        raise StoreWriteError(f"{incomplete} {error}") from None
                    except DirectoryIdentityChanged:
                        if not moved:
                            raise StoreWriteError(
                                f"{incomplete} Check local storage and set the "
                                "directory in podcasts.json before retrying."
                            ) from None
                        raise StoreWriteError(
                            f"Audio was saved in the episode directory then at "
                            f"{resolved}, but that directory moved or was replaced "
                            "before the subscription directory could be saved. "
                            "Restore the original directory layout and verify the "
                            "saved files before retrying."
                        ) from None
                    except (
                        OSError,
                        DuplicateIdentity,
                        StoreReadError,
                        StoreWriteError,
                        DirectoryAdmissionError,
                    ):
                        raise StoreWriteError(
                            f"{incomplete} Check local storage and set the directory "
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
        spare_bytes = cls.MAX_XML_BYTES - len(converted)
        if spare_bytes < 0:
            raise FeedError("Feed XML decoded byte limit exceeded")
        text = converted.decode("utf-8")
        # Replacing an ASCII ampersand with "&amp;" adds four UTF-8 bytes.
        max_repairs = spare_bytes // 4
        for count, _ in enumerate(_UNESCAPED_AMPERSAND.finditer(text), 1):
            if count > max_repairs:
                raise FeedError("Feed XML repaired byte limit exceeded")
            if count % 256 == 0:
                remaining()
        remaining()
        normalized = _UNESCAPED_AMPERSAND.sub("&amp;", text).encode("utf-8")
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
            if media_type == "application/ogg":
                suffixes = {"ogg", "oga"}
            elif media_type == "application/mp4":
                suffixes = {"m4a"}
            elif not media_type or media_type in GENERIC_BINARY_MEDIA_TYPES:
                suffixes = cls.AUDIO_SUFFIXES
            else:
                continue
            if url.path.lower().rsplit(".", 1)[-1] in suffixes:
                fallback = fallback or (href, None)
        if fallback:
            return fallback
        raise ValueError("No supported audio enclosure")

    @classmethod
    def parse_feed_entries(cls, entries):
        """Return usable episode dictionaries ordered by episode time and GUID."""
        episodes, skipped = cls._parse_feed_entry_models(entries)
        if skipped:
            _warn_completed_operation(
                f"Skipped {skipped} unusable feed entry/entries", category=UserWarning
            )
        return [episode.dict(exclude={"audio_file"}) for episode in episodes]

    @classmethod
    def _parse_feed_entry_models(cls, entries):
        episodes = []
        skipped = 0
        for entry in entries:
            try:
                audio_url, media_type = cls.audio_enclosure(entry)
                guid, title = entry["id"], entry["title"]
                if not all(
                    isinstance(value, str) and value.strip() for value in (guid, title)
                ):
                    raise ValueError("Missing episode identity or title")
                episode = EpisodeInDb(
                    index=len(episodes) + 1,
                    guid=guid,
                    audio_url=audio_url,
                    audio_media_type=media_type,
                    published=_episode_date(entry),
                    title=title,
                )
            except (KeyError, AttributeError, TypeError, ValueError, IndexError):
                skipped += 1
                continue
            episodes.append(episode)
        try:
            index_unique(episodes, lambda episode: episode.guid)
        except DuplicateIdentity as error:
            raise FeedError(f"{error} (usable feed entries)") from None
        if not episodes:
            raise FeedError("Feed has no usable audio episodes")
        episodes.sort(key=lambda episode: (episode.published, episode.guid))
        for index, episode in enumerate(episodes, 1):
            episode.index = index
        return episodes, skipped

    def get(self, url, directory, episode_name_pattern):
        """Fetch a feed and bound its update date by the newest usable episode."""
        document = self.fetch_document(url)
        title = document.get("feed", {}).get("title")
        if not isinstance(title, str) or not title.strip():
            raise FeedError("Feed is missing a title")

        validated_episodes, skipped = self._parse_feed_entry_models(
            document.get("entries", [])
        )
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
                    updated = max(updated, parsed_date(date))
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

        # Build domain episodes from the validated rows.
        episodes = [
            Episode.from_dict(episode.dict() | {"podcast": podcast})
            for episode in validated_episodes
        ]

        if skipped:
            _warn_completed_operation(
                f"Skipped {skipped} unusable feed entry/entries", category=UserWarning
            )
        return podcast, episodes
