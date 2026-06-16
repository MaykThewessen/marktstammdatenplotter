"""Run the open-mastr bulk download with a MB/s rate shown next to the ETA.

open-mastr has two bulk-download code paths, each with its own progress bar:

1. Subset pulls (e.g. ``db-mastr-core`` -> wind/solar/storage/...) go through
   ``partial_download_with_unzip_http``, which extracts files one by one from
   the remote zip via HTTP range requests and drives a tqdm bar with
   ``unit=" file"``. File sizes vary by orders of magnitude, so a per-file bar
   gives both a meaningless rate and a wildly swinging ETA.

2. Full pulls (``db-mastr-all``) and the partial-path fallback go through
   ``full_download_without_unzip_http``, which streams 1 MiB chunks into a tqdm
   bar with ``unit=""`` (so the rate prints as a bare ``/s``).

This wrapper patches both before invoking ``Mastr().download`` so the transfer
rate is reported in MB/s right next to tqdm's ETA (``[elapsed<remaining, rate]``):

- Path 1 is re-driven by *bytes* (summing each member's compressed size from the
  remote zip's central directory), giving a real MB/s rate and a byte-accurate
  ETA. The override mirrors upstream's extraction logic exactly; only the
  progress bar changes.
- Path 2 keeps upstream's loop; we only relabel its bar's ``unit`` to ``MB``
  (each ``bar.update()`` already consumes one 1 MiB chunk).

Both bars use a 1024-based scale so "MB" means MiB consistently across paths.

Usage:
    python scripts/mastr_download.py wind solar storage   # selected tables
    python scripts/mastr_download.py                       # full export
"""

from __future__ import annotations

import sys
from pathlib import Path

from tqdm import tqdm as _tqdm

import open_mastr.xml_download.utils_download_bulk as _bulk


def _tqdm_mb(*args, **kwargs):
    """tqdm wrapper that labels the full-download chunk bar's rate as MB/s."""
    if kwargs.get("unit") == "":  # the 1 MiB-chunk bar, not the file-count bar
        kwargs["unit"] = "MB"
    return _tqdm(*args, **kwargs)


def _partial_download_with_progress(save_path: str, url: str, bulk_data_list: list):
    """Byte-driven replacement for open-mastr's ``partial_download_with_unzip_http``.

    Identical extraction behaviour; the per-file bar is swapped for a bytes bar
    so the rate shows MB/s and the ETA tracks remaining bytes, not file count.
    """
    is_katalogwerte_existing = False
    if Path(save_path).exists():
        bulk_data_list, is_katalogwerte_existing = _bulk.check_download_completeness(
            save_path, bulk_data_list
        )
        if bulk_data_list:
            _bulk.log.info(
                f"MaStR file already present but missing the following data: {bulk_data_list}"
            )
        else:
            _bulk.log.info(f"MaStR file already present: {save_path}")
            return None

    remote_zip_file = _bulk.unzip_http.RemoteZipFile(url)
    remote_zip_names = [
        name.lower().split("_")[0].split(".")[0]
        for name in remote_zip_file.namelist()
    ]

    download_files_list = [
        remote_zip_file.namelist()[remote_index]
        for bulk_data_name in bulk_data_list
        for bulk_file_name in _bulk.BULK_INCLUDE_TABLES_MAP[bulk_data_name]
        for remote_index, remote_zip_name in enumerate(remote_zip_names)
        if remote_zip_name == bulk_file_name
    ]

    # Compressed sizes = bytes actually pulled over HTTP, read from the zip's
    # central directory (no extra requests).
    sizes = [remote_zip_file.files[name].compress_size for name in download_files_list]
    with _tqdm(
        total=sum(sizes),
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc="MaStR download",
    ) as bar:
        for name, size in zip(download_files_list, sizes):
            remote_zip_file.extractzip(name, path=Path(save_path))
            bar.update(size)

    if not is_katalogwerte_existing:
        remote_zip_file.extractzip("Katalogwerte.xml", path=Path(save_path))


_bulk.tqdm = _tqdm_mb
_bulk.partial_download_with_unzip_http = _partial_download_with_progress

from open_mastr import Mastr  # noqa: E402  (import after the patches are applied)


def main() -> None:
    data = sys.argv[1:] or None  # None -> open-mastr downloads the full export
    Mastr().download(data=data)


if __name__ == "__main__":
    main()
