"""Download audio without publishing incomplete or unsuccessful responses."""

import os
import tempfile
from pathlib import Path

import httpx
import rich.progress


def download_with_progress(url, target_path):
    target_path = Path(target_path)
    temporary = None
    try:
        with httpx.Client(follow_redirects=True, max_redirects=5) as client:
            with client.stream(
                "GET", url, headers={"Accept-Encoding": "identity"}
            ) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length", "")
                total = int(length) if length.isascii() and length.isdigit() else None
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=target_path.parent, delete=False
                ) as output:
                    temporary = Path(output.name)
                    completed = 0
                    with rich.progress.Progress(
                        rich.progress.BarColumn(bar_width=None),
                        rich.progress.DownloadColumn(),
                        rich.progress.TransferSpeedColumn(),
                    ) as progress:
                        task = progress.add_task("Download", total=total)
                        for chunk in response.iter_raw():
                            output.write(chunk)
                            completed += len(chunk)
                            progress.update(task, completed=completed)
                    if total is not None and completed != total:
                        raise ValueError("Incomplete media response")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target_path)
        return target_path
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
