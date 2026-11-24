"""
pdcst is a little podcast command line utility to show
new episodes and download audio files.
"""

import re
import unicodedata
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

import typer

from .config import settings
from .download import DownloadError, download_with_progress
from .models import Episode, FilenamePatternError, Podcast
from .repository import (
    AUDIO_INTENT,
    REFRESH_INTENT,
    DownloadConflict,
    DuplicateIdentity,
    EpisodeRepository,
    FeedParserRepository,
    PodcastRepository,
    SharedDirectoryError,
    StoreReadError,
    StoreWriteError,
)
from .storage import InvalidReplacementTarget, PublicationDurabilityError

__all__ = ["subscribe", "list_all_podcasts", "list_all_episodes", "terminal_text"]

__version__ = "0.13"
cli = typer.Typer()

# Feed text may carry CR/LF, C1 controls (RI, DCS, OSC, ...) and bidirectional
# overrides that rewrite, hide or reorder terminal output.
_TERMINAL_UNSAFE_FORMATS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_PODCAST_LINE_MAX_CHARS = 160
_SHARED_FEED_PREVIEW_LIMIT = 5
_FEED_ORIGIN_MAX_HOST_CHARS = 254


def _terminal_character(char: str) -> str:
    if (
        unicodedata.category(char) in {"Cc", "Zl", "Zp"}
        or char in _TERMINAL_UNSAFE_FORMATS
    ):
        return f"\\u{ord(char):04x}"
    return char


def terminal_text(value) -> str:
    """Escape control, line-separator and bidi-override characters for display."""
    return "".join(_terminal_character(char) for char in str(value))


def _terminal_title_preview(title: str, budget: int) -> str:
    remaining = budget
    parts = []
    for index, char in enumerate(title[:budget], start=1):
        escaped = _terminal_character(char)
        # Reserve space for the omission marker while more input remains.
        available = remaining - (1 if index < len(title) else 0)
        if len(escaped) > available:
            parts.append("…")
            break
        parts.append(escaped)
        remaining -= len(escaped)
    return "".join(parts)


def _feed_origin(feed_url: str) -> str:
    """Return only the scheme, hostname and optional port of a stored feed."""
    unavailable = "feed origin unavailable"
    try:
        url = urlsplit(feed_url)
        host, port = url.hostname, url.port
    except ValueError:
        return unavailable
    if (
        url.scheme not in {"http", "https"}
        or not host
        or len(host) > _FEED_ORIGIN_MAX_HOST_CHARS
        or "\\" in url.netloc
        or not re.fullmatch(r"[\w.:-]+", host)
    ):
        return unavailable
    if url.netloc.rsplit("@", 1)[-1].startswith("["):
        host = f"[{host}]"
    return f"{url.scheme}://{host}" + (f":{port}" if port is not None else "")


def contained_path(root, *parts):
    """Resolve a relative destination before any filesystem mutation."""
    root = Path(root).resolve()
    for part in parts:
        path = Path(part)
        if (
            path.is_absolute()
            or PureWindowsPath(part).drive
            or "\\" in str(part)
            or ".." in path.parts
            or not path.parts
        ):
            raise ValueError("Destination must be relative to PDCST_HOME")
    try:
        destination = root.joinpath(*parts).resolve()
    except RuntimeError:
        # Path.resolve raises RuntimeError for symlink loops on Python 3.10.
        raise ValueError("Podcast directory is unavailable or unsafe") from None
    if destination == root or root not in destination.parents:
        raise ValueError("Destination escapes PDCST_HOME")
    return destination


def _subscription_directory_key(root, directory):
    """Identify directories for cross-feed ownership on every host."""
    return unicodedata.normalize("NFC", str(contained_path(root, directory))).casefold()


def _subscription_owner_key(root, directory):
    """Return a catalog directory key only if it can own a contained path."""
    try:
        return _subscription_directory_key(root, directory)
    except ValueError:
        return None


def subscription_directory(directory, root=None):
    """Contain paths and reserve every component aliasing control storage."""
    root = Path(settings.root if root is None else root).resolve()
    destination = contained_path(root, directory)
    reserved = {
        "podcasts.json",
        "podcasts.json.lock",
        "episodes.json",
        "episodes.json.lock",
        "episodes.json.download.lock",
        ".feed-locks",
        REFRESH_INTENT,
        AUDIO_INTENT,
    }
    for part in (*Path(directory).parts, *destination.relative_to(root).parts):
        key = unicodedata.normalize("NFC", part).casefold()
        if key in reserved or key.startswith((".pdcst-refresh-", ".pdcst-download-")):
            raise ValueError(
                "Podcast directory component is reserved for repository storage"
            )
    return destination


def subscribe(
    feed_url: str,
    name_pattern: str,
    podcast_dir: str | None,
    fp_repo=None,
    podcasts_repo=None,
) -> Podcast:
    """
    Subscribe to a podcast feed url.
    """
    if podcasts_repo is None:
        podcasts_repo = PodcastRepository(Path(settings.root).resolve())
    root = podcasts_repo.base_dir.absolute()
    if podcast_dir is not None:
        subscription_directory(podcast_dir, root=root)
    if fp_repo is None:
        fp_repo = FeedParserRepository()
    with podcasts_repo.admitted() as admitted:
        admitted._check_intents()
        with admitted.feed_lock(feed_url) as locked:
            return _subscribe(
                feed_url, name_pattern, podcast_dir, fp_repo, locked, root
            )


def _subscribe(feed_url, name_pattern, podcast_dir, fp_repo, podcasts_repo, root):
    """Acquire and publish while the caller holds the per-feed lock."""
    podcast, episodes = fp_repo.get(feed_url, podcast_dir, name_pattern)
    with podcasts_repo._lock():
        locked = podcasts_repo._fetch_all()
        subscriptions = [podcasts_repo._listed_podcast(row) for row in locked.values()]
        if podcast_dir is None:
            existing = next(
                (item for item in subscriptions if item.feed.url == feed_url), None
            )
            if existing is not None:
                podcast.directory = existing.directory
            else:
                legacy = podcast.title.lower().replace(" ", "_")
                try:
                    legacy_path = contained_path(root, legacy)
                except ValueError:
                    pass
                else:
                    if legacy_path.is_dir():
                        podcast.directory = legacy

        owners = set()
        for item in subscriptions:
            if item.feed.url == feed_url:
                continue
            key = _subscription_owner_key(root, item.directory)
            if key is not None:
                owners.add(key)

        def owned(directory):
            return _subscription_directory_key(root, directory) in owners

        if owned(podcast.directory):
            if podcast_dir is not None or existing is not None:
                raise ValueError(
                    "Podcast directory is already used by another feed; "
                    "choose a distinct --podcast-dir"
                )
            base = podcast.directory
            suffix = 2
            while owned(podcast.directory):
                podcast.directory = f"{base}_{suffix}"
                suffix += 1
        episode_dir = subscription_directory(podcast.directory, root=root)

        def prepare(episodes_by_guid):
            if episodes_by_guid:
                names = set()
                render_name = podcast.audio_filename_renderer()
                for episode in episodes_by_guid.values():
                    name = render_name(episode)
                    contained_path(root, podcast.directory, name)
                    key = unicodedata.normalize("NFC", name).casefold()
                    if key in names:
                        raise FilenamePatternError(
                            "Episode filename collision; choose a distinct filename "
                            "pattern including {index}"
                        )
                    names.add(key)
            podcast.episodes_count = len(episodes_by_guid)
            stored = podcasts_repo.storage_model_type(**podcast.dict())
            locked[stored.pk] = stored
            podcasts_repo._serialize_models(locked)

        # Preflight the retained count under both locks before either publication.
        with EpisodeRepository(episode_dir).subscription_import(
            episodes, podcasts_repo, prepare=prepare
        ):
            podcasts_repo._write_models(locked)
    return podcast


def list_all_podcasts(podcasts=None) -> list[Podcast]:
    """
    List all podcasts.
    """
    if podcasts is None:
        podcasts = PodcastRepository(Path(settings.root).resolve())
    return podcasts.list()


def list_all_episodes(podcast) -> list[Episode]:
    """
    List all episodes for a podcast.
    """
    try:
        directory = contained_path(settings.root, podcast.directory)
    except (OSError, RuntimeError, ValueError):
        raise StoreReadError(
            Path(EpisodeRepository.name),
            "podcast episode directory is unavailable or unsafe",
        ) from None
    episodes_repo = EpisodeRepository(directory)
    return episodes_repo.list(additional_attributes={"podcast": podcast})


class SelectionError(ValueError):
    """A title identifier is blank or ambiguous."""


def _title_key(title):
    return unicodedata.normalize("NFC", title).casefold()


def _find_title(items, identifier, kind, unique):
    key = _title_key(identifier)
    if unique and not key.strip():
        raise SelectionError(f"Empty {kind} identifier; use a title")
    first_match = first_exact = None
    match_count = exact_count = 0
    for item in items:
        title = _title_key(item.title)
        if key not in title:
            continue
        if not unique:
            return item
        if match_count == 0:
            first_match = item
        match_count += 1
        if title == key:
            if exact_count == 0:
                first_exact = item
            exact_count += 1

    count = exact_count or match_count
    if count > 1:
        raise SelectionError(
            f"Ambiguous {kind} ({count} matches); "
            "use an exact title or a more specific title substring"
        )
    return first_exact if exact_count else first_match


def find_podcast_by_identifier(identifier: str, *, unique=False) -> Podcast | None:
    try:
        podcasts = list_all_podcasts()
    except SharedDirectoryError as error:
        # Refuse only a selection that maps to a shared directory.
        podcast = _find_title(error.podcasts, identifier, "podcast", unique)
        if any(podcast is shared for shared in error.shared):
            raise
        return podcast
    return _find_title(podcasts, identifier, "podcast", unique)


def find_episode_by_identifier(
    podcast: Podcast, identifier: str, *, unique=False
) -> Episode | None:
    return _find_title(list_all_episodes(podcast), identifier, "episode", unique)


@cli.command()
def add(
    feed_url: str,
    name_pattern: str = typer.Option(
        "{index:03}_{title}.{file_format}",
        help="Pattern to use for downloaded file names",
    ),
    podcast_dir: str = typer.Option(
        None, help="base directory for storing podcast episodes"
    ),
):
    """
    Subscribe to podcast with given FEED_URL.

    You can specify a file name pattern for downloaded audio files with --name-pattern
    """
    try:
        podcast = subscribe(feed_url, name_pattern, podcast_dir)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from None
    except (InvalidReplacementTarget, PublicationDurabilityError) as error:
        raise typer.BadParameter(str(error)) from None
    except OSError:
        raise typer.BadParameter(
            "Unable to update local storage: filesystem error; "
            "check PDCST_HOME and write permissions"
        ) from None
    prefix, suffix = "Podcast ", " added"
    budget = _PODCAST_LINE_MAX_CHARS - len(prefix) - len(suffix)
    preview = _terminal_title_preview(podcast.title, budget)
    print(f"{prefix}{preview}{suffix}")


@cli.command(name="podcasts")
def list_podcasts():
    """
    List your podcast subscriptions.
    """
    try:
        all_podcasts = list_all_podcasts()
    except SharedDirectoryError as error:
        feeds = "; ".join(
            f"{_terminal_title_preview(podcast.title, _PODCAST_LINE_MAX_CHARS)} "
            f"<{_feed_origin(podcast.feed.url)}>"
            for podcast in error.shared[:_SHARED_FEED_PREVIEW_LIMIT]
        )
        omitted = len(error.shared) - _SHARED_FEED_PREVIEW_LIMIT
        if omitted > 0:
            noun = "feed" if omitted == 1 else "feeds"
            feeds += f"; {omitted} more conflicting {noun}"
        raise typer.BadParameter(
            f"{error}; re-add one of these feeds with a distinct --podcast-dir: "
            f"{feeds}"
        ) from None
    except (DuplicateIdentity, StoreReadError) as error:
        raise typer.BadParameter(str(error)) from None
    for podcast in all_podcasts:
        suffix = f" - {podcast.episodes_count} episodes"
        budget = max(1, _PODCAST_LINE_MAX_CHARS - len(suffix))
        print(_terminal_title_preview(podcast.title, budget) + suffix)


@cli.command(name="episodes")
def list_episodes(identifier: str):
    """
    List the available episodes for a podcast.

    pdcst episodes python
    """
    try:
        podcast = find_podcast_by_identifier(identifier, unique=True)
        if podcast is None:
            raise typer.BadParameter("No matching podcast")
        episodes = list_all_episodes(podcast)
    except (DuplicateIdentity, SelectionError, StoreReadError) as error:
        raise typer.BadParameter(str(error)) from None
    for episode in episodes:
        print(terminal_text(episode))


@cli.command()
def download(podcast: str, episode: str):
    """
    Download a podcast episode.

    pdcst download python htmx
    """
    try:
        podcast = find_podcast_by_identifier(podcast, unique=True)
        if podcast is None:
            raise typer.BadParameter("No matching podcast")
        subscriptions = PodcastRepository(Path(settings.root).resolve())
        with subscriptions.download_subscription(podcast) as (podcast, repository):
            podcast.validate_audio_filename_pattern()
            if repository is None:
                episode = find_episode_by_identifier(podcast, episode, unique=True)
            else:
                episodes = repository.list(additional_attributes={"podcast": podcast})
                episode = _find_title(episodes, episode, "episode", unique=True)
            if episode is None:
                raise typer.BadParameter("No matching episode")
            if repository is None:
                directory = contained_path(subscriptions.base_dir, podcast.directory)
                repository = EpisodeRepository(directory)
            repository.download_episode(
                episode, podcast.get_audio_file_name(episode), download_with_progress
            )
    except (
        DownloadConflict,
        DuplicateIdentity,
        DownloadError,
        InvalidReplacementTarget,
        PublicationDurabilityError,
        FilenamePatternError,
        StoreReadError,
        StoreWriteError,
        SelectionError,
    ) as error:
        raise typer.BadParameter(str(error)) from None


if __name__ == "__main__":
    cli()
