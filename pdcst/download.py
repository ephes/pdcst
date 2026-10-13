"""Download audio without publishing incomplete or unsuccessful responses."""

from pathlib import Path

import httpx
import rich.progress

from .storage import replacement_file


def download_with_progress(url, target_path):
    target_path = Path(target_path)
    with httpx.Client(follow_redirects=True, max_redirects=5) as client:
        with client.stream(
            "GET", url, headers={"Accept-Encoding": "identity"}
        ) as response:
            response.raise_for_status()
            encoding = response.headers.get("Content-Encoding", "identity")
            if encoding.strip().lower() not in {"", "identity"}:
                raise ValueError("Unsupported media content encoding")
            length = response.headers.get("Content-Length", "")
            total = int(length) if length.isascii() and length.isdigit() else None
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
                        output.write(chunk)
                        completed += len(chunk)
                        progress.update(task, completed=completed)
                if total is not None and completed != total:
                    raise ValueError("Incomplete media response")
    return target_path
