import pytest

from pdcst.models import Podcast


@pytest.mark.parametrize(
    "test_input,expected",
    [
        ("", ""),
        ("ba,z", "baz"),
        ("Foobar", "foobar"),
        ("foo bar", "foo_bar"),
        ("asdf?", "asdf"),
    ],
)
def test_title_for_filename(test_input, expected):
    assert expected == Podcast.title_for_filename(test_input)


@pytest.mark.parametrize(
    "file_name_pattern,expected", [("gpp_{index:04}_{title}", "gpp_0001_refactoring")]
)
def test_audio_file_name(file_name_pattern, expected, podcast, episode):
    podcast.file_name_pattern = file_name_pattern
    assert podcast.get_audio_file_name(episode) == expected
