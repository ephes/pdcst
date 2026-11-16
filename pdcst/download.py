"""Download audio without publishing incomplete or unsuccessful responses."""

from pathlib import Path
from time import monotonic

import httpx
import rich.progress

from .config import settings
from .storage import PublicationDurabilityError, replacement_file


class DownloadError(ValueError):
    """An expected audio acquisition failure with a private, bounded diagnostic."""


def download_with_progress(url, target_path, *, directory_fd=None):
    """Transfer audio, reporting transport failures without private URL values."""
    try:
        return _download_with_progress(url, target_path, directory_fd=directory_fd)
    except PublicationDurabilityError:
        # The target is an internal stage; publication has not happened yet.
        raise DownloadError(
            "Unable to stage audio download: directory sync failed"
        ) from None
    except httpx.HTTPStatusError as error:
        raise DownloadError(
            f"Audio download failed (HTTP {error.response.status_code})"
        ) from None
    except (httpx.HTTPError, httpx.InvalidURL):
        raise DownloadError("Unable to fetch audio") from None


def _download_with_progress(url, target_path, *, directory_fd=None):
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
            if response.status_code != 200 or "Content-Range" in response.headers:
                raise DownloadError("Incomplete media response")
            encoding = response.headers.get("Content-Encoding", "identity")
            if encoding.strip().lower() not in {"", "identity"}:
                raise DownloadError("Unsupported media content encoding")
            length = response.headers.get("Content-Length", "")
            total = None
            if length.isascii() and length.isdigit():
                digits = length.lstrip("0") or "0"
                limit = str(settings.audio_max_bytes)
                if len(digits) > len(limit) or (
                    len(digits) == len(limit) and digits > limit
                ):
                    raise DownloadError("Audio download byte limit exceeded")
                total = int(digits)
            if directory_fd is None:
                target_path.parent.mkdir(parents=True, exist_ok=True)
            with replacement_file(
                target_path, binary=True, directory_fd=directory_fd
            ) as output:
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
