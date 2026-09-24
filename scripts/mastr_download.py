"""Run the open-mastr bulk download with a MB/s rate shown next to the ETA.

open-mastr has two bulk-download code paths, each with its own progress bar:

1. Subset pulls (e.g. ``db-mastr-core`` -> wind/solar/storage/market) go
   through ``partial_download_with_unzip_http``, which extracts files one by one
   from the remote zip via HTTP range requests and drives a tqdm bar with
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

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from tqdm import tqdm as _tqdm

import open_mastr.xml_download.utils_download_bulk as _bulk


def _tqdm_mb(*args, **kwargs):
    """tqdm wrapper that labels the full-download chunk bar's rate as MB/s."""
    if kwargs.get("unit") == "":  # the 1 MiB-chunk bar, not the file-count bar
        kwargs["unit"] = "MB"
    return _tqdm(*args, **kwargs)


def _partial_download_with_progress(
    save_path: str, url: str, names_to_download: set[str]
) -> None:
    """Byte-driven replacement for open-mastr's ``partial_download_with_unzip_http``.

    Identical extraction behaviour; the per-file bar is swapped for a bytes bar
    so the rate shows MB/s and the ETA tracks remaining bytes, not file count.
    Since open-mastr 1.0 the caller works out which tables are still missing
    and always includes Katalogwerte, so this only has to extract.
    """
    remote_zip_file = _bulk.unzip_http.RemoteZipFile(url)
    remote_zip_names = [
        name.lower().split("_")[0].split(".")[0]
        for name in remote_zip_file.namelist()
    ]

    download_files_list = [
        remote_zip_file.namelist()[remote_index]
        for name in names_to_download
        for remote_index, remote_zip_name in enumerate(remote_zip_names)
        if remote_zip_name == name
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
#   read_xml_file -> process_table_before_insertion
#     -> check_for_column_mismatch_and_try_to_solve_it -> add_table_to_sqlite_database
# after write_mastr_xml_to_database has cleared each target table once with
# delete_all_existing_rows. Replacing the clear, the column check (which would
# ALTER the SQL table) and the insert lands the parsed, cleansed DataFrame in
# parquet and never materialises the 11 GB SQLite.
#
# Patching the writer rather than reimplementing the loop keeps open-mastr's
# XML parsing, katalogwerte decoding and bulk cleansing exactly as upstream.
import sqlalchemy as sa  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mastr_parquet  # noqa: E402

_part_counter: dict[str, int] = {}
# The XSD-generated Table of every table this run writes, for finalise_table.
_tables: dict[str, sa.Table] = {}


class _ErrorRecorder(logging.Handler):
    """Collect open-mastr's ERROR records.

    process_xml_file catches every exception, logs it as ERROR and carries on,
    so a file that fails to parse, cleanse or write would otherwise end in
    "Bulk download was successful" with its rows silently missing.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


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


def _coerce_to_table_dtypes(df: pd.DataFrame, table: sa.Table) -> pd.DataFrame:
    """Apply the table's declared types so every part shares one schema.

    Without this, pyarrow infers per part and a column that is all-NULL in one
    XML file but populated in the next produces incompatible parts.

    Also materialises declared columns the XML does not supply, as all-NULL.
    SQLite got them for free from CREATE TABLE, and dropping them here would
    make a SELECT of a valid column fail against a directly-written store while
    succeeding against a converted one. open-mastr's surrogate id is the
    exception: it is not MaStR data, and only SQLite can fill it.
    """
    surrogate = {c.name for c in table.columns if mastr_parquet.is_surrogate_key(c)}
    dtypes = {
        col: types for col, types in mastr_parquet.column_types(table).items()
        if col not in surrogate
    }
    for col, (_, pdt) in dtypes.items():
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
    # Declared order first, then any columns the XML carries beyond the XSD
    # (which open-mastr would have added to the SQL table with ALTER TABLE).
    extras = [c for c in df.columns if c not in dtypes]
    return df[list(dtypes) + extras]


def _reset_table_parts(db_table: sa.Table, engine: sa.Engine) -> None:
    """Replaces delete_all_existing_rows: drop any stale parts of the table so
    a re-run does not append to the last one."""
    shutil.rmtree(mastr_parquet.parts_dir(db_table.name), ignore_errors=True)
    _part_counter[db_table.name] = 0
    _tables[db_table.name] = db_table


def _keep_all_columns(
    df: pd.DataFrame, db_table: sa.Table, engine: sa.Engine, alter_database_tables: bool,
) -> pd.DataFrame:
    """Replaces check_for_column_mismatch_and_try_to_solve_it, which ALTERs the
    SQL table for XML columns the XSD lacks. Parquet parts keep those columns
    and finalise_table merges them with union_by_name."""
    return df


def _add_table_to_parquet(df: pd.DataFrame, db_table: sa.Table, engine: sa.Engine) -> None:
    """Replaces add_table_to_sqlite_database: write a part instead of INSERTing."""
    df = _coerce_to_table_dtypes(df, db_table)
    seq = _part_counter.get(db_table.name, 0)
    _part_counter[db_table.name] = seq + 1
    mastr_parquet.write_part(df, db_table.name, seq)


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
    _uw.delete_all_existing_rows = _reset_table_parts
    _uw.check_for_column_mismatch_and_try_to_solve_it = _keep_all_columns
    _uw.add_table_to_sqlite_database = _add_table_to_parquet


def _finalise(tables: list[sa.Table]) -> None:
    """Merge parts per table and report what landed."""
    for table in sorted(tables, key=lambda t: t.name):
        n = mastr_parquet.finalise_table(table)
        size = mastr_parquet.table_path(table.name).stat().st_size / 1e6
        print(f"  {table.name:<44} {n:>10} rows  {size:>8.1f} MB")


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
    errors = _ErrorRecorder()
    logging.getLogger("open-MaStR").addHandler(errors)
    # open-mastr needs an engine, but the patched writer never connects to it.
    # Its default path is a symlink into this repo, where a connection would
    # create an empty open-mastr.db, so point it at a throwaway file instead.
    # (An in-memory URL does not work: create_efficient_engine passes pool
    # options that SQLite's in-memory pool rejects.) Passing the data model
    # skips open-mastr's DROP/CREATE TABLE.
    with tempfile.TemporaryDirectory() as tmp:
        db = Mastr(engine=sa.create_engine(f"sqlite:///{tmp}/unused.db"))
        model = db.generate_data_model(data=data, date=date)
        db.download(
            **opts, mastr_table_to_db_table=model, add_views_for_old_table_names=False,
        )
    if errors.messages:
        raise SystemExit(
            f"{len(errors.messages)} error(s) logged by open-mastr, store left "
            "unchanged:\n  " + "\n  ".join(errors.messages)
        )
    written = [_tables[name] for name, n in _part_counter.items() if n > 0]
    if not written:
        raise SystemExit(
            "No parquet parts were written. The download produced no tables. "
            "Check the log above for parse errors."
        )
    print(f"\nMerging {len(written)} tables into {mastr_parquet.PARQUET_DIR}:")
    _finalise(written)


if __name__ == "__main__":
    main()
