"""Download audio without publishing incomplete or unsuccessful responses."""

import logging
import os
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from time import monotonic

import httpx
import rich.progress

from .config import settings
from .models import AUDIO_MIME_EXTENSIONS
from .storage import DirectoryAdmissionError, admitted_directory, replacement_file

GENERIC_BINARY_MEDIA_TYPES = frozenset(
    {"application/octet-stream", "binary/octet-stream"}
)
MEDIA_TYPES = (
    frozenset(AUDIO_MIME_EXTENSIONS)
    | {"application/ogg", "application/mp4"}
    | GENERIC_BINARY_MEDIA_TYPES
)
PROGRESS_UPDATE_INTERVAL = 0.1
logger = logging.getLogger(__name__)


class DownloadError(ValueError):
    """An expected audio acquisition failure with a private, bounded diagnostic."""


def _warn_cleanup_failure(message: str) -> None:
    # A broken logging handler must not change the transfer outcome.
    with suppress(Exception):
        logger.warning(message)


@contextmanager
def _audio_progress(total):
    """Yield a byte counter that becomes silent after a display failure."""
    progress = None

    def stop():
        nonlocal progress
        if progress is not None:
            renderer, progress = progress, None
            with suppress(Exception):
                renderer.stop()

    def update(completed):
        if progress is not None:
            try:
                progress.update(task, completed=completed)
            except Exception:
                stop()

    try:
        try:
            progress = rich.progress.Progress(
                rich.progress.BarColumn(bar_width=None),
                rich.progress.DownloadColumn(),
                rich.progress.TransferSpeedColumn(),
            )
            progress.start()
            task = progress.add_task("Download", total=total)
        except Exception:
            stop()
        yield update
    finally:
        stop()


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
    except DirectoryAdmissionError as error:
        raise DownloadError(str(error)) from None


def _resolve_destination(parent):
    try:
        return parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DirectoryAdmissionError(
            "Audio destination directory cannot be resolved"
        ) from None


def _create_destination_parent(parent):
    """Create missing directories only below a parent whose link can be synced.

    Admission syncs every readable parent of the destination chain, so a retry
    settles links left by an interrupted attempt. Existing search-only
    ancestors stay usable, but a new link below one could never be synced.
    """
    base = parent
    while True:
        try:
            base.resolve(strict=True)
            break
        except FileNotFoundError:
            if base.parent == base:
                break
            base = base.parent
        except (OSError, RuntimeError):
            raise DirectoryAdmissionError(
                "Audio destination directory cannot be opened or created"
            ) from None
    if base == parent:
        return
    try:
        descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        raise DirectoryAdmissionError(
            "Audio destination directory cannot be created below an "
            "unreadable parent"
        ) from None
    os.close(descriptor)
    parent.mkdir(parents=True, exist_ok=True)


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
            next_request = None
            try:
                check_deadline()
                if response.has_redirect_location:
                    if redirects == client.max_redirects:
                        raise httpx.TooManyRedirects(
                            "Exceeded audio redirect limit", request=request
                        )
                    # HTTPX resolves locations and applies cookie/auth header rules.
                    next_request = response.next_request
                    if next_request is None:
                        raise DownloadError("Audio redirect did not prepare a request")
                    request = next_request
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
                with ExitStack() as files:
                    before_replace = check_deadline
                    if directory_fd is None:
                        parent = target_path.parent
                        # Keep pathname mkdir semantics for relative paths and
                        # existing directory aliases, then sync the real chain.
                        _create_destination_parent(parent)
                        resolved = _resolve_destination(parent)
                        directory_fd, check = files.enter_context(
                            admitted_directory(
                                resolved, label="Audio destination directory"
                            )
                        )

                        def check_destination_and_deadline():
                            check()
                            if _resolve_destination(parent) != resolved:
                                raise DirectoryAdmissionError(
                                    "Audio destination directory identity changed"
                                )
                            check_deadline()

                        before_replace = check_destination_and_deadline

                    output = files.enter_context(
                        replacement_file(
                            target_path,
                            binary=True,
                            directory_fd=directory_fd,
                            before_replace=before_replace,
                        )
                    )
                    check_deadline()
                    completed = 0
                    with _audio_progress(total) as update_progress:
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
                            if output.write(chunk) != len(chunk):
                                raise DownloadError("Incomplete audio stage write")
                            completed += len(chunk)
                            now = monotonic()
                            if now >= next_update:
                                update_progress(completed)
                                next_update = now + PROGRESS_UPDATE_INTERVAL
                        update_progress(completed)
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
                    if not (response_failed or published or next_request is not None):
                        raise
                    close_failed = True
                if published and close_failed:
                    _warn_cleanup_failure("Audio transferred; response close failed")
                elif next_request is not None and close_failed and not response_failed:
                    _warn_cleanup_failure(
                        "Audio redirect accepted; response close failed"
                    )
    except BaseException:
        transfer_failed = True
        raise
    finally:
        try:
            client.close()
        except Exception:
            if published:
                _warn_cleanup_failure("Audio transferred; HTTP client close failed")
            elif not transfer_failed:
                raise
    return target_path
