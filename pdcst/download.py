"""Download audio without publishing incomplete or unsuccessful responses."""

import logging
from pathlib import Path
from time import monotonic

import httpx
import rich.progress

from .config import settings
from .models import AUDIO_MIME_EXTENSIONS
from .storage import replacement_file

MEDIA_TYPES = frozenset(AUDIO_MIME_EXTENSIONS) | {
    "application/ogg",
    "application/mp4",
    "application/octet-stream",
    "binary/octet-stream",
}
PROGRESS_UPDATE_INTERVAL = 0.1
logger = logging.getLogger(__name__)


class DownloadError(ValueError):
    """An expected audio acquisition failure with a private, bounded diagnostic."""


def download_with_progress(url, target_path, *, directory_fd=None):
    """Transfer audio, reporting transport failures without private URL values."""
    try:
        return _download_with_progress(url, target_path, directory_fd=directory_fd)
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
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise DownloadError("Audio download duration limit exceeded")
        return min(10, remaining)

    client = httpx.Client(
        follow_redirects=False,
        max_redirects=5,
        timeout=min(10, settings.audio_max_seconds),
    )
    published = transfer_failed = False
    try:
        request = client.build_request(
            "GET", url, headers={"Accept-Encoding": "identity"}
        )
        for redirects in range(client.max_redirects + 1):
            request.extensions["timeout"] = httpx.Timeout(check_deadline()).as_dict()
            # Automatic redirects read discarded bodies even with stream=True.
            response = client.send(request, stream=True)
            response_failed = close_failed = False
            try:
                check_deadline()
                if response.has_redirect_location:
                    if redirects == client.max_redirects:
                        raise httpx.TooManyRedirects(
                            "Exceeded audio redirect limit", request=request
                        )
                    # HTTPX resolves locations and applies cookie/auth header rules.
                    request = response.next_request
                    continue
                response.raise_for_status()
                if response.status_code != 200 or "Content-Range" in response.headers:
                    raise DownloadError("Incomplete media response")
                declared_type = response.headers.get("Content-Type", "").strip()
                media_type = declared_type.partition(";")[0].strip().lower()
                if declared_type and media_type not in MEDIA_TYPES:
                    raise DownloadError("Unsupported media content type")
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
                    check_deadline()
                    completed = 0
                    with rich.progress.Progress(
                        rich.progress.BarColumn(bar_width=None),
                        rich.progress.DownloadColumn(),
                        rich.progress.TransferSpeedColumn(),
                    ) as progress:
                        task = progress.add_task("Download", total=total)
                        next_update = monotonic() + PROGRESS_UPDATE_INTERVAL
                        check_deadline()
                        chunks = response.iter_raw()
                        while True:
                            was_closed = response.is_closed
                            try:
                                chunk = next(chunks, None)
                            except Exception:
                                # HTTPX marks the response closed before retiring
                                # the stream at EOF, after yielding all bytes.
                                if was_closed or not response.is_closed:
                                    raise
                                close_failed = True
                                break
                            if chunk is None:
                                break
                            check_deadline()
                            if completed + len(chunk) > settings.audio_max_bytes:
                                raise DownloadError(
                                    "Audio download byte limit exceeded"
                                )
                            output.write(chunk)
                            completed += len(chunk)
                            now = monotonic()
                            if now >= next_update:
                                progress.update(task, completed=completed)
                                next_update = now + PROGRESS_UPDATE_INTERVAL
                        progress.update(task, completed=completed)
                    check_deadline()
                    if completed == 0:
                        raise DownloadError("Empty audio response")
                    if total is not None and completed != total:
                        raise DownloadError("Incomplete media response")
                published = True
                break
            except BaseException:
                response_failed = True
                raise
            finally:
                try:
                    response.close()
                except Exception:
                    if not (response_failed or published):
                        raise
                    close_failed = True
                if published and close_failed:
                    logger.warning("Audio transferred; response close failed")
    except BaseException:
        transfer_failed = True
        raise
    finally:
        try:
            client.close()
        except Exception:
            if published:
                logger.warning("Audio transferred; HTTP client close failed")
            elif not transfer_failed:
                raise
    return target_path
