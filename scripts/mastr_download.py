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

Writes the parquet store at data/mastr/parquet/ by default, never materialising
the 11 GB SQLite (see the parquet-writer section below). Pass --sqlite for
open-mastr's own SQL path.

Usage:
    python scripts/mastr_download.py wind solar storage   # selected tables
    python scripts/mastr_download.py                       # full export
    python scripts/mastr_download.py wind --sqlite         # legacy SQLite path
    python scripts/mastr_download.py wind --date=20260616 --keep-old
                                       # reprocess a cached export in place
"""

from __future__ import annotations

import os
import shutil
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


# The combustion-table parse fix that used to live here is gone: open-mastr
# 0.17.2 fixed it upstream. It aborted the whole combustion table (gas/coal/oil,
# EinheitenVerbrennung) when a catalog column held comma-separated codes —
# WeitereBrennstoffe values like "2442, 2442" crashed
# replace_mastr_katalogeintraege's astype and left combustion_extended at 0 rows,
# logged as ERROR only, with the download still exiting 0. Upstream now splits on
# commas and uses pd.api.types.is_string_dtype, so a local override would only
# shadow their implementation.
import pandas as pd  # noqa: E402
import open_mastr.xml_download.utils_write_to_database as _uw  # noqa: E402

# --- parquet writer -------------------------------------------------------
# open-mastr only knows how to write SQL. Its per-XML-file worker does
#   read_xml_file -> process_table_before_insertion -> add_table_to_sqlite_database
# so replacing the last step (and neutering CREATE TABLE) lands the parsed,
# cleansed DataFrame in parquet and never materialises the 11 GB SQLite.
#
# Patching the writer rather than reimplementing the loop keeps open-mastr's
# XML parsing, katalogwerte decoding and bulk cleansing exactly as upstream.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mastr_parquet  # noqa: E402

_part_counter: dict[str, int] = {}


def _to_boolean(s: pd.Series) -> pd.Series:
    """Coerce a cleansed MaStR column to pandas nullable boolean.

    MaStR XML yields booleans as the strings "true"/"false"; SQLite silently
    took whatever came through and stored INTEGER 0/1. Parquet has a real bool
    type, so normalise here.
    """
    if s.dtype == object or isinstance(s.dtype, pd.StringDtype):
        mapped = s.map(
            {"true": True, "false": False, "True": True, "False": False,
             "1": True, "0": False, 1: True, 0: False, True: True, False: False}
        )
        # Anything unmapped stays NA rather than becoming a spurious True.
        return mapped.astype("boolean")
    return pd.to_numeric(s, errors="coerce").astype("boolean")


def _coerce_to_orm_dtypes(df: pd.DataFrame, sql_table_name: str) -> pd.DataFrame:
    """Apply the ORM's declared types so every part shares one schema.

    Without this, pyarrow infers per part and a column that is all-NULL in one
    XML file but populated in the next produces incompatible parts.

    Also materialises ORM columns the XML does not supply, as all-NULL. SQLite
    got them for free from CREATE TABLE (wind_extended: 24 of 101 columns are
    100 % NULL for the 2026-06 export), and dropping them here would make
    `SELECT Meldedatum FROM wind_extended` fail against a directly-written store
    while succeeding against a converted one.
    """
    orm = mastr_parquet.orm_dtypes(sql_table_name)
    for col, (_, pdt) in orm.items():
        if col not in df.columns:
            df[col] = pd.Series(index=df.index, dtype=pdt)
            continue
        s = df[col]
        try:
            if pdt.startswith("datetime64"):
                # astype pins the unit: pandas picks it per column from the
                # parsed values, and parts whose units disagree cannot be merged.
                df[col] = pd.to_datetime(s, errors="coerce").astype(pdt)
            elif pdt == "boolean":
                df[col] = _to_boolean(s)
            elif pdt in ("Int64", "float64"):
                df[col] = pd.to_numeric(s, errors="coerce").astype(pdt)
            else:
                df[col] = s.astype("string")
        except (TypeError, ValueError):
            # Never lose rows over a type: keep the values as text and let the
            # finalise pass carry them through.
            df[col] = s.astype("string")
    # ORM order first, then any columns the XML carries beyond the ORM (which is
    # what add_missing_columns_to_table appended to the SQLite table).
    extras = [c for c in df.columns if c not in orm]
    return df[list(orm) + extras]


def _create_database_table_noop(engine, xml_table_name: str) -> None:
    """Replaces create_database_table. Parquet needs no DDL; the matching reset
    is dropping any stale parts so a re-run does not append to the last one."""
    sql_table_name = _uw.tablename_mapping[xml_table_name]["__class__"].__table__.name
    shutil.rmtree(mastr_parquet.parts_dir(sql_table_name), ignore_errors=True)
    _part_counter[sql_table_name] = 0


def _add_table_to_parquet(df, xml_table_name, sql_table_name, engine) -> None:
    """Replaces add_table_to_sqlite_database: write a part instead of INSERTing."""
    df = _coerce_to_orm_dtypes(df, sql_table_name)
    seq = _part_counter.get(sql_table_name, 0)
    _part_counter[sql_table_name] = seq + 1
    mastr_parquet.write_part(df, sql_table_name, seq)


def _install_parquet_writer() -> None:
    """Point open-mastr's writer at the parquet store.

    Hard-fails on the parallel code path: ProcessPoolExecutor children re-import
    open_mastr and would resolve the *original* process_xml_file, silently
    writing SQLite while we wait for parquet parts that never arrive.
    """
    for var in ("NUMBER_OF_PROCESSES", "USE_RECOMMENDED_NUMBER_OF_PROCESSES"):
        if var in os.environ:
            raise SystemExit(
                f"{var} is set, which runs open-mastr's writer in subprocesses "
                "that would not see the parquet patch (and would write SQLite "
                "instead). Unset it, or pass --sqlite to use the legacy path."
            )
    mastr_parquet.set_store_dir(mastr_parquet.REPO_DIR)
    _uw.create_database_table = _create_database_table_noop
    _uw.add_table_to_sqlite_database = _add_table_to_parquet


def _finalise(tables: list[str]) -> None:
    """Merge parts per table and report what landed."""
    for table in sorted(tables):
        n = mastr_parquet.finalise_table(table)
        size = mastr_parquet.table_path(table).stat().st_size / 1e6
        print(f"  {table:<28} {n:>10} rows  {size:>8.1f} MB")


from open_mastr import Mastr  # noqa: E402  (import after the patches are applied)


def main() -> None:
    args = sys.argv[1:]
    use_parquet = "--sqlite" not in args
    # Pin the export date to reprocess a zip already on disk instead of pulling
    # today's. Without it open-mastr targets today and re-downloads ~3 GB.
    date = next((a.split("=", 1)[1] for a in args if a.startswith("--date=")), None)
    # open-mastr deletes XML files that are not from the requested date, so a
    # cached export is destroyed unless this is set.
    keep_old = "--keep-old" in args
    data = [a for a in args if not a.startswith("--")] or None

    opts = {"data": data, "keep_old_downloads": keep_old}
    if date:
        opts["date"] = date

    if not use_parquet:
        Mastr().download(**opts)
        return

    _install_parquet_writer()
    Mastr().download(**opts)
    written = sorted(_part_counter)
    if not written:
        raise SystemExit(
            "No parquet parts were written — the download produced no tables. "
            "Check the log above for parse errors."
        )
    print(f"\nMerging {len(written)} tables into {mastr_parquet.PARQUET_DIR}:")
    _finalise(written)


if __name__ == "__main__":
    main()
