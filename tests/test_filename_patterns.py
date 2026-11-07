"""Only the documented filename fields can be formatted."""

import pytest


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
