"""Download audio without publishing incomplete or unsuccessful responses."""

from pathlib import Path
from time import monotonic

import httpx
import rich.progress

from .config import settings
from .storage import replacement_file


class DownloadError(ValueError):
    """An expected audio acquisition failure with a private, bounded diagnostic."""


def download_with_progress(url, target_path):
    """Transfer audio, reporting transport failures without private URL values."""
    try:
        return _download_with_progress(url, target_path)
    except httpx.HTTPStatusError as error:
        raise DownloadError(
            f"Audio download failed (HTTP {error.response.status_code})"
        ) from None
    except (httpx.HTTPError, httpx.InvalidURL):
        raise DownloadError("Unable to fetch audio") from None


def _download_with_progress(url, target_path):
    target_path = Path(target_path)
    deadline = monotonic() + settings.audio_max_seconds

    def check_deadline():
        if monotonic() >= deadline:
            raise DownloadError("Audio download duration limit exceeded")

    with httpx.Client(
        follow_redirects=True,
        max_redirects=5,
        timeout=min(10, settings.audio_max_seconds),
    ) as client:
        with client.stream(
            "GET", url, headers={"Accept-Encoding": "identity"}
        ) as response:
            check_deadline()
            response.raise_for_status()
            encoding = response.headers.get("Content-Encoding", "identity")
            if encoding.strip().lower() not in {"", "identity"}:
                raise DownloadError("Unsupported media content encoding")
            length = response.headers.get("Content-Length", "")
            total = int(length) if length.isascii() and length.isdigit() else None
            if total is not None and total > settings.audio_max_bytes:
                raise DownloadError("Audio download byte limit exceeded")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with replacement_file(target_path, binary=True) as output:
                completed = 0
                with rich.progress.Progress(
                    rich.progress.BarColumn(bar_width=None),
                    rich.progress.DownloadColumn(),
                    rich.progress.TransferSpeedColumn(),
                ) as progress:
                    task = progress.add_task("Download", total=total)
                    for chunk in response.iter_raw():
                        check_deadline()
                        if completed + len(chunk) > settings.audio_max_bytes:
                            raise DownloadError("Audio download byte limit exceeded")
                        output.write(chunk)
                        completed += len(chunk)
                        progress.update(task, completed=completed)
                check_deadline()
                if completed == 0:
                    raise DownloadError("Empty audio response")
                if total is not None and completed != total:
                    raise DownloadError("Incomplete media response")
    return target_path
