"""
All the domain models for pdcst live here.
"""

import re
from functools import partial
from string import Formatter
from urllib.parse import urlsplit

FILENAME_MAX_BYTES = 255
FILENAME_PATTERN_MAX_CHARS = 4096
WINDOWS_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}
WINDOWS_FORBIDDEN_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
FILENAME_PATTERN_ERROR = (
    "Invalid filename pattern: use index, title and file_format; "
    "decimal width/precision and filename bytes must be at most 255"
)
# Standard format-spec syntax with bounded numbers (each at most 255, checked
# separately): optional fill+alignment, then for the integer index sign,
# "#", zero padding, width, grouping and an integer type (no "c", which could
# emit arbitrary characters); "n" is recognized for a migration diagnostic.
# Text fields allow width, precision and "s". Text
# widths cannot start with 0 (that would zero-fill strings on the right).
_ALIGN = r"(?:[^{}]?[<>=^])?"
INDEX_SPEC = _ALIGN + r"[+\- ]?#?0?[0-9]{0,3}[,_]?[bdoxXn]?"
TEXT_SPEC = _ALIGN + r"(?:[1-9][0-9]{0,2})?(?:\.[0-9]{1,3})?s?"


AUDIO_MIME_EXTENSIONS = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}
AUDIO_SUFFIXES = frozenset(AUDIO_MIME_EXTENSIONS.values()) | {"oga"}


def effective_audio_format(audio_url: str, media_type: str | None) -> str | None:
    """Return format evidence, excluding legacy guesses used only for filenames."""
    try:
        path = urlsplit(audio_url).path
    except ValueError:
        # Legacy stores admit nonempty URL strings even if they cannot be parsed.
        path = ""
    _, separator, suffix = path.rsplit("/", 1)[-1].rpartition(".")
    if separator and suffix.lower() in AUDIO_SUFFIXES:
        return suffix.lower()
    return AUDIO_MIME_EXTENSIONS.get(media_type)


def audio_format_changed(previous: str | None, current: str | None) -> bool:
    """Require two known formats to confirm a change."""
    return previous is not None and current is not None and previous != current


class FilenamePatternError(ValueError):
    """A pattern cannot produce a supported single filename."""


class Feed:
    """
    To be able to fetch a feed with feedparser, we need it's url.
    """

    def __init__(self, *args, url, updated):
        self.url = url
        self.updated = updated

    @classmethod
    def from_dict(cls, feed):
        return cls(**feed)

    def dict(self):
        return {
            "url": self.url,
            "updated": self.updated,
        }

    def __eq__(self, other):
        return self.url == other.url


class Podcast:
    """
    Podcasts have a feed that can be fetched. Attributes like
    title or episodes_count are retrieved from the feed.

    The file_name_pattern is settable and determines the naming
    schema for the audio files of podcast episodes. Podcast episodes
    live in a directory which is also settable. By default, it's
    derived from the title.
    """

    # Set by the repository when the stored directory still needs to be pinned.
    _legacy_directory = False

    def __init__(
        self,
        *,
        feed,
        title,
        episodes_count,
        file_name_pattern="{index:03}_{title}.{file_format}",
        directory=None,
    ):
        self.feed = feed if isinstance(feed, Feed) else Feed.from_dict(feed)
        self.title = title
        self.file_name_pattern = file_name_pattern
        self.episodes_count = episodes_count
        self.directory = (
            self.title_for_filename(self.title) or "podcast"
            if directory is None
            else directory
        )

    @classmethod
    def from_dict(cls, podcast):
        """Construct a podcast from a dict."""
        return cls(**podcast)

    @staticmethod
    def title_for_filename(title: str) -> str:
        """Remove characters that could cause problems in file names."""
        return re.sub(
            r"[^\w.-]",
            "_",
            title.lower().replace(" ", "_").replace("?", "").replace(",", ""),
        ).strip(".")

    def dict(self):
        """Serialize a podcast to dict."""
        return {
            "title": self.title,
            "feed": self.feed.dict(),
            "episodes_count": self.episodes_count,
            "file_name_pattern": self.file_name_pattern,
            "directory": self.directory,
        }

    def __repr__(self):
        return f"{self.title} - {self.episodes_count} episodes"

    def __str__(self):
        return self.__repr__()

    def __eq__(self, other):
        equal = self.feed == other.feed and self.title == other.title
        return equal

    def _filename_pattern_parts(self):
        if len(self.file_name_pattern) > FILENAME_PATTERN_MAX_CHARS:
            raise FilenamePatternError(
                "Invalid filename pattern: patterns are limited to 4096 characters"
            )
        try:
            parts = list(Formatter().parse(self.file_name_pattern))
            for _, field, spec, conversion in parts:
                if field is None:
                    continue
                grammar = INDEX_SPEC if field == "index" else TEXT_SPEC
                if (
                    field not in {"index", "title", "file_format"}
                    or conversion is not None
                    or re.fullmatch(grammar, spec) is None
                    or any(
                        int(n) > FILENAME_MAX_BYTES for n in re.findall(r"[0-9]+", spec)
                    )
                ):
                    raise ValueError
        except ValueError:
            raise FilenamePatternError(FILENAME_PATTERN_ERROR) from None
        if any(field == "index" and spec.endswith("n") for _, field, spec, _ in parts):
            raise FilenamePatternError(
                "Invalid filename pattern: replace locale-dependent index type 'n' with 'd' "
                "(use ',d' or '_d' for grouping)"
            )
        return parts

    def validate_audio_filename_pattern(self):
        """Admit filename format syntax without loading an episode."""
        parts = self._filename_pattern_parts()
        try:
            for _, field, spec, _ in parts:
                if field is not None:
                    format(1 if field == "index" else "x", spec)
        except ValueError:
            raise FilenamePatternError(FILENAME_PATTERN_ERROR) from None

    def _audio_file_format(self, episode):
        file_format = effective_audio_format(
            episode.audio_url, episode.audio_media_type
        )
        if file_format is None:
            # Preserve legacy records' filename behavior when no type was stored.
            path = urlsplit(episode.audio_url).path
            file_format = self.title_for_filename(path.rsplit(".", 1)[-1])
        return file_format

    def get_audio_file_name(self, episode):
        """Use the filename pattern to build a name for an episode's audio."""
        return self.audio_filename_renderer()(episode)

    def audio_filename_renderer(self):
        """Compile the current filename pattern for a batch of episodes."""
        parts = self._filename_pattern_parts()
        fields = {field for _, field, _, _ in parts}
        return partial(self._render_audio_file_name, parts, fields)

    def _render_audio_file_name(self, parts, fields, episode):
        details = {}
        if "index" in fields:
            details["index"] = episode.index
        if "title" in fields:
            details["title"] = self.title_for_filename(episode.title)
        if "file_format" in fields:
            details["file_format"] = self._audio_file_format(episode)
        try:
            chunks = []
            size = 0
            for literal, field, spec, _ in parts:
                chunk = literal + (format(details[field], spec) if field else "")
                size += len(chunk.encode("utf-8"))
                if size > FILENAME_MAX_BYTES:
                    raise ValueError
                chunks.append(chunk)
            name = "".join(chunks)
        except (
            KeyError,
            ValueError,
            TypeError,
            IndexError,
            AttributeError,
            OverflowError,
        ):
            raise FilenamePatternError(FILENAME_PATTERN_ERROR) from None
        if (
            not name
            or name.endswith((".", " "))
            or WINDOWS_FORBIDDEN_CHARS.search(name)
            or name.partition(".")[0].rstrip(" ").upper() in WINDOWS_DEVICE_NAMES
        ):
            raise FilenamePatternError(
                "Invalid filename pattern: episode name must be a Windows-compatible basename"
            )
        return name


class Episode:
    """
    Episodes belong to a podcast. They have an index, which we
    calculate by ourselves. The guid, audio_url, published date
    and title are attributes which we get from the feed.

    We store a reference to downloaded audio files in the audio_file
    attribute.
    """

    def __init__(
        self,
        *,
        index,
        guid,
        audio_url,
        published,
        title,
        podcast=None,
        audio_file=None,
        audio_media_type=None,
    ):
        self.podcast = podcast
        self.index = index
        self.guid = guid
        self.audio_url = audio_url
        self.published = published
        self.title = title
        self.audio_file = audio_file
        self.audio_media_type = audio_media_type

    @classmethod
    def from_dict(cls, episode):
        """Construct an episode from a dict."""
        return cls(**episode)

    def __repr__(self):
        return f"{self.title}"

    def __str__(self):
        return self.__repr__()

    def __eq__(self, other):
        return self.podcast == other.podcast and self.audio_url == other.audio_url

    def dict(self):
        """Serialize an episode to dict."""
        return {
            "index": self.index,
            "guid": self.guid,
            "audio_url": self.audio_url,
            "published": self.published,
            "title": self.title,
            "audio_file": self.audio_file,
            "audio_media_type": self.audio_media_type,
        }

    @property
    def audio_file_name(self):
        return self.podcast.get_audio_file_name(self)
