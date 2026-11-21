import os
import subprocess
import sys

import pytest
import typer
from typer.testing import CliRunner

import pdcst
from pdcst.repository import EpisodeRepository, PodcastRepository


def test_download_selections(tmp_path, monkeypatch, podcast, episode):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    monkeypatch.setattr(pdcst, "list_all_podcasts", lambda: [podcast])
    monkeypatch.setattr(pdcst, "list_all_episodes", lambda _: [episode])
    calls = []

    def transfer(url, stage, **kwargs):
        calls.append((url, stage))
        stage.write_bytes(b"synthetic audio")

    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    runner = CliRunner()
    for args, message in [
        (["unknown", "rest"], "No matching podcast"),
        (["python", "unknown"], "No matching episode"),
    ]:
        result = runner.invoke(pdcst.cli, ["download", *args], color=False)
        assert result.exit_code != 0
        assert message in result.output
        assert "Traceback" not in result.output
        assert not calls and not list(tmp_path.iterdir())
    EpisodeRepository(tmp_path / podcast.directory).add(episode)
    result = runner.invoke(pdcst.cli, ["download", "PyThOn", "ReFaCtOr"], color=False)
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    saved = EpisodeRepository(tmp_path / podcast.directory).list()[0]
    assert saved.audio_file
    assert (
        tmp_path / podcast.directory / saved.audio_file
    ).read_bytes() == b"synthetic audio"


def test_ambiguous_downloads_never_mutate(tmp_path, monkeypatch, podcast, episode):
    import copy

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    other_show = copy.deepcopy(podcast)
    other_show.title += " Extra"
    other_episode = copy.deepcopy(episode)
    other_episode.title += " Extra"
    other_episode.guid = "other"
    monkeypatch.setattr(pdcst, "list_all_podcasts", lambda: [podcast, other_show])
    monkeypatch.setattr(pdcst, "list_all_episodes", lambda _: [episode, other_episode])

    def forbidden(*args):
        raise AssertionError("ambiguous selection reached mutation")

    monkeypatch.setattr(EpisodeRepository, "download_episode", forbidden)
    for show, title, message in [
        ("python", "refactor", "Ambiguous podcast"),
        ("", "refactor", "Empty podcast"),
        (podcast.title, "refactor", "Ambiguous episode"),
        (podcast.title, "", "Empty episode"),
    ]:
        result = CliRunner().invoke(pdcst.cli, ["download", show, title])
        assert result.exit_code != 0
        assert message in result.output
        assert not list(tmp_path.iterdir())
    # The library helper defaults to the first match.
    assert pdcst.find_podcast_by_identifier("python") == podcast


def test_exact_titles_disambiguate_download(tmp_path, monkeypatch, podcast, episode):
    import copy

    other_show = copy.deepcopy(podcast)
    other_show.title += " Extra"
    other_episode = copy.deepcopy(episode)
    other_episode.title += " Extra"
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    monkeypatch.setattr(pdcst, "list_all_podcasts", lambda: [other_show, podcast])
    monkeypatch.setattr(pdcst, "list_all_episodes", lambda _: [other_episode, episode])
    calls = []
    monkeypatch.setattr(
        EpisodeRepository, "download_episode", lambda self, *args: calls.append(args)
    )
    result = CliRunner().invoke(
        pdcst.cli, ["download", podcast.title.swapcase(), episode.title.swapcase()]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][0] is episode


def test_duplicate_exact_titles_are_ambiguous(monkeypatch, podcast):
    monkeypatch.setattr(pdcst, "list_all_podcasts", lambda: [podcast, podcast])
    result = CliRunner().invoke(pdcst.cli, ["download", podcast.title, "anything"])
    assert result.exit_code != 0
    assert "Ambiguous podcast" in result.output


@pytest.mark.parametrize("legacy_exists", [False, True])
def test_first_successful_download_freezes_legacy_directory(
    tmp_path, monkeypatch, podcast, episode, legacy_exists
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.title = "Synthetic Show!"
    podcast.directory = None
    podcast.file_name_pattern = "saved.mp3"
    shows = PodcastRepository(tmp_path)
    shows.add(podcast)
    original = shows.path.read_bytes()
    legacy = tmp_path / "synthetic_show!"
    fallback = tmp_path / "synthetic_show_"
    chosen = legacy if legacy_exists else fallback
    episode.audio_url = "https://example.test/episode.mp3"
    episodes = EpisodeRepository(chosen)
    episodes.add(episode)

    selected = pdcst.find_podcast_by_identifier("synthetic")
    assert selected.directory == chosen.name
    assert pdcst.list_all_episodes(selected)[0].audio_file is None
    assert shows.path.read_bytes() == original

    def fail(*args, **kwargs):
        raise pdcst.DownloadError("Synthetic transfer failure")

    monkeypatch.setattr(pdcst, "download_with_progress", fail)
    with pytest.raises(typer.BadParameter, match="Synthetic transfer failure"):
        pdcst.download("synthetic", "refactor")
    assert shows.path.read_bytes() == original

    def transfer(url, stage, **kwargs):
        assert stage.parent == chosen
        stage.write_bytes(b"synthetic audio")

    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    pdcst.download("synthetic", "refactor")
    competitor = fallback if legacy_exists else legacy
    competitor.mkdir()
    selected = pdcst.find_podcast_by_identifier("synthetic")
    assert selected.directory == chosen.name
    assert pdcst.list_all_episodes(selected)[0].audio_file == "saved.mp3"
    assert shows.all[podcast.feed.url].directory == chosen.name
    pdcst.download("synthetic", "refactor")
    assert (chosen / "saved.mp3").read_bytes() == b"synthetic audio"
    assert not list(competitor.iterdir())


@pytest.mark.parametrize("conflicting_claim", [False, True])
def test_legacy_transfer_releases_catalog_writer_and_rechecks_ownership(
    tmp_path, monkeypatch, podcast, episode, conflicting_claim
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.title = "Synthetic Show"
    podcast.directory = None
    podcast.file_name_pattern = "saved.mp3"
    shows = PodcastRepository(tmp_path)
    shows.add(podcast)
    chosen = tmp_path / "synthetic_show"
    episode.audio_url = "https://example.test/episode.mp3"
    EpisodeRepository(chosen).add(episode)
    other_directory = chosen.name if conflicting_claim else "other_show"
    other_url = "https://example.test/other.xml"

    def transfer(url, stage, **kwargs):
        # A separate process must finish its update before this transfer returns.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import sys
from pathlib import Path
from pdcst.models import Podcast
from pdcst.repository import PodcastRepository

shows = PodcastRepository(Path(sys.argv[1]))
data = next(iter(shows.all.values())).dict()
data["feed"]["url"] = sys.argv[2]
data.update(title="Other Show", directory=sys.argv[3])
shows.add(Podcast.from_dict(data))
""",
                str(tmp_path),
                other_url,
                other_directory,
            ],
            env={**os.environ, "PDCST_LOCK_WAIT_SECONDS": "0.2"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        stage.write_bytes(b"synthetic audio")

    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    if conflicting_claim:
        with pytest.raises(typer.BadParameter, match="already used by another feed"):
            pdcst.download("synthetic", "refactor")
    else:
        pdcst.download("synthetic", "refactor")

    assert shows.all[other_url].directory == other_directory
    assert shows.all[podcast.feed.url].directory == (
        None if conflicting_claim else chosen.name
    )
    assert (chosen / "saved.mp3").read_bytes() == b"synthetic audio"


def _forbid_transfer(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("refused legacy directory reached the transfer")

    monkeypatch.setattr(pdcst, "download_with_progress", forbidden)


def test_legacy_pin_refuses_reserved_directory(tmp_path, monkeypatch, podcast):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.title = ".feed-locks"
    podcast.directory = None
    shows = PodcastRepository(tmp_path)
    shows.add(podcast)
    original = shows.path.read_bytes()
    (tmp_path / ".feed-locks").mkdir()
    _forbid_transfer(monkeypatch)
    with pytest.raises(typer.BadParameter, match="reserved for repository storage"):
        pdcst.download("feed-locks", "refactor")
    assert shows.path.read_bytes() == original
    assert not list((tmp_path / ".feed-locks").iterdir())


def test_legacy_pin_refuses_directory_of_another_feed(
    tmp_path, monkeypatch, podcast, episode
):
    from pdcst.models import Feed, Podcast

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.title = "Synthetic Show!"
    podcast.directory = None
    owner = Podcast(
        feed=Feed(url="https://example.test/other.xml", updated=podcast.feed.updated),
        title="Other Show",
        directory="synthetic_show_",
        episodes_count=1,
    )
    shows = PodcastRepository(tmp_path)
    shows.add_list([podcast, owner])
    original = shows.path.read_bytes()
    EpisodeRepository(tmp_path / "synthetic_show_").add(episode)
    _forbid_transfer(monkeypatch)
    with pytest.raises(typer.BadParameter, match="already used by another feed"):
        pdcst.download("synthetic", "refactor")
    assert shows.path.read_bytes() == original


def test_legacy_pin_identity_change_reports_saved_audio(
    tmp_path, monkeypatch, podcast, episode
):
    from pdcst.storage import DirectoryIdentityChanged

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.title = "Synthetic Show!"
    podcast.directory = None
    podcast.file_name_pattern = "saved.mp3"
    shows = PodcastRepository(tmp_path)
    shows.add(podcast)
    original = shows.path.read_bytes()
    episode.audio_url = "https://example.test/episode.mp3"
    EpisodeRepository(tmp_path / "synthetic_show_").add(episode)

    def transfer(url, stage, **kwargs):
        stage.write_bytes(b"synthetic audio")

    def changed(self, locked):
        raise DirectoryIdentityChanged("Storage root directory identity changed")

    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    monkeypatch.setattr(PodcastRepository, "_write_models", changed)
    with pytest.raises(typer.BadParameter) as raised:
        pdcst.download("synthetic", "refactor")
    message = str(raised.value)
    assert f"Audio saved in {(tmp_path / 'synthetic_show_').resolve()}" in message
    assert "set the directory in podcasts.json" in message
    assert (
        tmp_path / "synthetic_show_" / "saved.mp3"
    ).read_bytes() == b"synthetic audio"
    assert shows.path.read_bytes() == original


def test_concurrently_pinned_directory_releases_subscription_lock(
    tmp_path, monkeypatch, podcast, episode
):
    from pdcst.models import Feed, Podcast

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    monkeypatch.setattr(pdcst.settings, "lock_wait_seconds", 0.1)
    podcast.directory = None
    podcast.file_name_pattern = "saved.mp3"
    shows = PodcastRepository(tmp_path)
    shows.add(podcast)
    pinned = tmp_path / "concurrently-pinned"
    EpisodeRepository(pinned).add(episode)
    find_podcast = pdcst.find_podcast_by_identifier

    def select_then_pin(*args, **kwargs):
        selected = find_podcast(*args, **kwargs)
        assert selected._legacy_directory
        podcast.directory = pinned.name
        shows.add(podcast)
        return selected

    other = Podcast(
        feed=Feed(url="https://example.test/other.xml", updated=podcast.feed.updated),
        title="Other Show",
        directory="other_show",
        episodes_count=1,
    )

    def transfer(url, stage, **kwargs):
        assert stage.parent == pinned.resolve()
        # Complete another writer's lock acquisition while the transfer is open.
        shows.add(other)
        stage.write_bytes(b"synthetic audio")

    monkeypatch.setattr(pdcst, "find_podcast_by_identifier", select_then_pin)
    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    pdcst.download("python", "refactor")
    assert shows.all[other.feed.url].directory == other.directory
    assert shows.all[podcast.feed.url].directory == pinned.name
    assert (pinned / "saved.mp3").read_bytes() == b"synthetic audio"


@pytest.mark.parametrize(
    ("unique", "identifier", "expected_index", "match_count"),
    [
        (True, "STRASSE", 20, None),
        (True, "str", None, 41),
        (False, "STRASSE", 0, None),
    ],
)
def test_episode_selection_folds_each_visited_title_once(
    monkeypatch, podcast, unique, identifier, expected_index, match_count
):
    from types import SimpleNamespace

    visited = []

    class Title(str):
        def casefold(self):
            visited.append(self)
            return super().casefold()

    titles = [Title(f"Straße {index}") for index in range(41)]
    titles[20] = Title("Straße")
    episodes = [SimpleNamespace(title=title) for title in titles]
    monkeypatch.setattr(pdcst, "list_all_episodes", lambda _: episodes)

    if match_count is None:
        selected = pdcst.find_episode_by_identifier(podcast, identifier, unique=unique)
        assert selected is episodes[expected_index]
    else:
        with pytest.raises(pdcst.SelectionError, match=rf"\({match_count} matches\)"):
            pdcst.find_episode_by_identifier(podcast, identifier, unique=unique)
    assert visited == (titles if unique else titles[:1])


@pytest.mark.parametrize("pattern", ["{title[0]}", "{title:=5}"])
def test_invalid_stored_filename_pattern_precedes_episode_read(
    tmp_path, monkeypatch, podcast, pattern
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.file_name_pattern = pattern
    PodcastRepository(tmp_path).add(podcast)

    def unexpected_read(*args, **kwargs):
        pytest.fail("invalid stored pattern reached the episode repository")

    monkeypatch.setattr(EpisodeRepository, "list", unexpected_read)
    result = CliRunner().invoke(pdcst.cli, ["download", podcast.title, "episode"])
    assert result.exit_code != 0
    assert "Invalid filename pattern" in result.output
