"""Only the documented filename fields can be formatted."""

import pytest

from pdcst.models import FilenamePatternError


@pytest.mark.parametrize(
    "pattern",
    [
        "{missing}",
        "{title.__class__}",
        "{title[0]}",
        "{index:{title}}",
        "{title!r}",
        "{",
        "{title:03d}",
        "{index:bad}",
        "../{title}.mp3",
        "x\x00.mp3",
    ],
)
def test_pattern_errors_are_bounded(podcast, episode, pattern):
    podcast.file_name_pattern = pattern
    with pytest.raises(ValueError, match="Invalid filename pattern") as error:
        podcast.get_audio_file_name(episode)
    assert len(str(error.value)) < 150


def test_padded_index_and_escaped_braces(podcast, episode):
    podcast.file_name_pattern = "{{show}}_{index:03}_{title}.{file_format}"
    assert podcast.get_audio_file_name(episode) == "{show}_001_refactoring.mp3"


@pytest.mark.parametrize(
    "pattern",
    [
        "{title:1000000000}",
        "{title:.1000000000}",
        "{index:01000000000}",
        "{title:256}",
        "{title:.256}",
        "{index:0256d}",
    ],
)
def test_expansion_rejected_before_formatting(podcast, episode, pattern, monkeypatch):
    def forbidden(*args):
        pytest.fail("unbounded spec reached field formatting")

    monkeypatch.setattr("pdcst.models.format", forbidden, raising=False)

    class GuardedPattern(str):
        def format(self, **kwargs):
            pytest.fail("unbounded pattern reached str.format")

    podcast.file_name_pattern = GuardedPattern(pattern)
    with pytest.raises(FilenamePatternError):
        podcast.get_audio_file_name(episode)


@pytest.mark.parametrize(
    "pattern,size", [("{index:0255d}", 255), ("{title:255}", 255), ("{title:.255}", 11)]
)
def test_format_limit_boundary(podcast, episode, pattern, size):
    podcast.file_name_pattern = pattern
    assert len(podcast.get_audio_file_name(episode).encode("utf-8")) == size


@pytest.mark.parametrize(
    "pattern,title",
    [
        ("x" * 256, "short"),
        ("{title}{title}", "a" * 128),
        ("{title}", "é" * 128),
        ("{title:.128}", "é" * 256),
    ],
)
def test_basename_byte_budget(podcast, episode, pattern, title):
    podcast.file_name_pattern = pattern
    episode.title = title
    with pytest.raises(FilenamePatternError):
        podcast.get_audio_file_name(episode)
