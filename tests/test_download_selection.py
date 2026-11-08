from typer.testing import CliRunner

import pdcst
from pdcst.repository import EpisodeRepository


def test_download_selections(tmp_path, monkeypatch, podcast, episode):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    monkeypatch.setattr(pdcst, "list_all_podcasts", lambda: [podcast])
    monkeypatch.setattr(pdcst, "list_all_episodes", lambda _: [episode])
    calls = []
    monkeypatch.setattr(
        pdcst, "download_with_progress", lambda *args: calls.append(args)
    )
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
    assert EpisodeRepository(tmp_path / podcast.directory).list()[0].audio_file


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
    # Browse retains its first-match contract.
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
