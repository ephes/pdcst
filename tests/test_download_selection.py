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
