"""
pdcst is a little podcast command line utility to show
new episodes and download audio files.
"""

from pathlib import Path, PureWindowsPath

import typer

from .config import settings
from .download import download_with_progress
from .models import Episode, Podcast
from .repository import EpisodeRepository, FeedParserRepository, PodcastRepository

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
    fp_repo=FeedParserRepository(),
    podcasts_repo=PodcastRepository(settings.root),
) -> Podcast:
    """
    Subscribe to a podcast feed url.
    """
    # add fetched podcast metadata to repository
    podcast, episodes = fp_repo.get(feed_url, podcast_dir, name_pattern)
    episode_dir = contained_path(settings.root, podcast.directory)
    for episode in episodes:
        contained_path(
            settings.root, podcast.directory, podcast.get_audio_file_name(episode)
        )
    podcasts_repo.add(podcast)

    # add fetched episodes metadata to repository
    episodes_repo = EpisodeRepository(episode_dir)
    episodes_repo.add_list(episodes)
    return podcast


def list_all_podcasts(podcasts=PodcastRepository(settings.root)) -> list[Podcast]:
    """
    List all podcasts.
    """
    return podcasts.list()


def list_all_episodes(podcast) -> list[Episode]:
    """
    List all episodes for a podcast.
    """
    episodes_repo = EpisodeRepository(contained_path(settings.root, podcast.directory))
    return episodes_repo.list(additional_attributes={"podcast": podcast})


def find_podcast_by_identifier(identifier: str) -> Podcast | None:
    all_podcasts = list_all_podcasts()
    found = None
    for podcast in all_podcasts:
        if identifier in podcast.title.lower():
            found = podcast
            break
    return found


def find_episode_by_identifier(podcast: Podcast, identifier: str) -> Episode | None:
    episodes = list_all_episodes(podcast)
    found = None
    for episode in episodes:
        if identifier in episode.title.lower():
            found = episode
            break
    return found


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
    podcast = subscribe(feed_url, name_pattern, podcast_dir)
    print(f"Podcast {podcast.title} added")


@cli.command(name="podcasts")
def list_podcasts():
    """
    List your podcast subscriptions.
    """
    all_podcasts = list_all_podcasts()
    for podcast in all_podcasts:
        print(podcast)


@cli.command(name="episodes")
def list_episodes(identifier: str):
    """
    List the available episodes for a podcast.

    pdcst episodes python
    """
    podcast = find_podcast_by_identifier(identifier)
    if podcast is None:
        return
    episodes = list_all_episodes(podcast)
    for episode in episodes:
        print(episode)


@cli.command()
def download(podcast: str, episode: str):
    """
    Download a podcast episode.

    pdcst download python htmx
    """
    podcast = find_podcast_by_identifier(podcast)
    if podcast is None:
        return
    episode = find_episode_by_identifier(podcast, episode)
    target_path = contained_path(
        settings.root, podcast.directory, podcast.get_audio_file_name(episode)
    )
    download_with_progress(episode.audio_url, target_path)
    episode.audio_file = target_path.name
    EpisodeRepository(contained_path(settings.root, podcast.directory)).add(episode)


if __name__ == "__main__":
    cli()
