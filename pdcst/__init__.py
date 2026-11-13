"""
pdcst is a little podcast command line utility to show
new episodes and download audio files.
"""

import unicodedata
from pathlib import Path, PureWindowsPath

import typer

from .config import settings
from .download import DownloadError, download_with_progress
from .models import Episode, FilenamePatternError, Podcast
from .repository import (
    DownloadConflict,
    DuplicateIdentity,
    EpisodeRepository,
    FeedParserRepository,
    PodcastRepository,
    StoreReadError,
    StoreWriteError,
)
from .storage import PublicationDurabilityError

__all__ = ["subscribe", "list_all_podcasts", "list_all_episodes"]

__version__ = "0.12"
cli = typer.Typer()


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
    destination = root.joinpath(*parts).resolve()
    if destination == root or root not in destination.parents:
        raise ValueError("Destination escapes PDCST_HOME")
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
    if fp_repo is None:
        fp_repo = FeedParserRepository()
    if podcasts_repo is None:
        podcasts_repo = PodcastRepository(settings.root)
    with podcasts_repo.feed_lock(feed_url):
        return _subscribe(feed_url, name_pattern, podcast_dir, fp_repo, podcasts_repo)


def _subscribe(feed_url, name_pattern, podcast_dir, fp_repo, podcasts_repo):
    """Acquire and publish while the caller holds the per-feed lock."""
    podcast, episodes = fp_repo.get(feed_url, podcast_dir, name_pattern)
    with podcasts_repo._lock():
        subscriptions = podcasts_repo.list()
        if podcast_dir is None:
            existing = next(
                (item for item in subscriptions if item.feed.url == feed_url), None
            )
            if existing is not None:
                podcast.directory = existing.directory
            else:
                legacy = podcast.title.lower().replace(" ", "_")
                try:
                    legacy_path = contained_path(settings.root, legacy)
                except ValueError:
                    pass
                else:
                    if legacy_path.is_dir():
                        podcast.directory = legacy

        def owned(directory):
            candidate = unicodedata.normalize(
                "NFC", str(contained_path(settings.root, directory))
            ).casefold()
            for item in subscriptions:
                if item.feed.url == feed_url:
                    continue
                try:
                    stored_path = contained_path(settings.root, item.directory)
                except ValueError:
                    # Unsafe legacy paths cannot own a contained destination.
                    continue
                if (
                    unicodedata.normalize("NFC", str(stored_path)).casefold()
                    == candidate
                ):
                    return True
            return False

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
        episode_dir = contained_path(settings.root, podcast.directory)
        for episode in episodes:
            contained_path(
                settings.root, podcast.directory, podcast.get_audio_file_name(episode)
            )
        locked = podcasts_repo._fetch_all()
        stored = podcasts_repo.storage_model_type(**podcast.dict())
        locked[stored.pk] = stored
        podcasts_repo._serialize_models(locked)
        # Import under the episode lock before publishing its subscription pointer.
        with EpisodeRepository(episode_dir).subscription_import(episodes):
            podcasts_repo._write_models(locked)
    return podcast


def list_all_podcasts(podcasts=None) -> list[Podcast]:
    """
    List all podcasts.
    """
    if podcasts is None:
        podcasts = PodcastRepository(settings.root)
    return podcasts.list()


def list_all_episodes(podcast) -> list[Episode]:
    """
    List all episodes for a podcast.
    """
    episodes_repo = EpisodeRepository(contained_path(settings.root, podcast.directory))
    return episodes_repo.list(additional_attributes={"podcast": podcast})


class SelectionError(ValueError):
    """A download title does not select one item."""


def _find_title(items, identifier, kind, unique):
    key = identifier.casefold()
    if unique and not key.strip():
        raise SelectionError(f"Empty {kind} identifier; use a title")
    matches = [item for item in items if key in item.title.casefold()]
    if unique:
        exact = [item for item in matches if key == item.title.casefold()]
        matches = exact or matches
        if len(matches) > 1:
            raise SelectionError(
                f"Ambiguous {kind} ({len(matches)} matches); "
                "use an exact title or a more specific title substring"
            )
    return next(iter(matches), None)


def find_podcast_by_identifier(identifier: str, *, unique=False) -> Podcast | None:
    return _find_title(list_all_podcasts(), identifier, "podcast", unique)


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
    except PublicationDurabilityError as error:
        raise typer.BadParameter(str(error)) from None
    except OSError:
        raise typer.BadParameter(
            "Unable to update local storage: filesystem error; "
            "check PDCST_HOME and write permissions"
        ) from None
    print(f"Podcast {podcast.title} added")


@cli.command(name="podcasts")
def list_podcasts():
    """
    List your podcast subscriptions.
    """
    try:
        all_podcasts = list_all_podcasts()
    except (DuplicateIdentity, StoreReadError) as error:
        raise typer.BadParameter(str(error)) from None
    for podcast in all_podcasts:
        print(podcast)


@cli.command(name="episodes")
def list_episodes(identifier: str):
    """
    List the available episodes for a podcast.

    pdcst episodes python
    """
    try:
        podcast = find_podcast_by_identifier(identifier)
        if podcast is None:
            return
        episodes = list_all_episodes(podcast)
    except (DuplicateIdentity, StoreReadError) as error:
        raise typer.BadParameter(str(error)) from None
    for episode in episodes:
        print(episode)


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
        episode = find_episode_by_identifier(podcast, episode, unique=True)
        if episode is None:
            raise typer.BadParameter("No matching episode")
        EpisodeRepository(
            contained_path(settings.root, podcast.directory)
        ).download_episode(
            episode, podcast.get_audio_file_name(episode), download_with_progress
        )
    except (
        DownloadConflict,
        DuplicateIdentity,
        DownloadError,
        PublicationDurabilityError,
        FilenamePatternError,
        StoreReadError,
        StoreWriteError,
        SelectionError,
    ) as error:
        raise typer.BadParameter(str(error)) from None


if __name__ == "__main__":
    cli()
