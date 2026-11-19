"""Run the open-mastr bulk download into the parquet store, with a byte-driven bar.

open-mastr has two bulk-download code paths, each with its own progress bar:

1. Subset pulls (e.g. ``db-mastr-core`` -> wind/solar/storage/market) go
   through ``partial_download_with_unzip_http``, which extracts files one by one
   from the remote zip via HTTP range requests and drives a tqdm bar with
   ``unit=" file"``. File sizes vary by orders of magnitude, so a per-file bar
   gives both a meaningless rate and a wildly swinging ETA.

2. Full pulls (``db-mastr-all``) and the partial-path fallback go through
   ``full_download_without_unzip_http``. Since open-mastr 1.1.0 (PR #824) its
   bar is sized from the server's content-length, but it still counts one tick
   per ``iter_content`` chunk with ``unit=""``. Chunks vary in size up to 1 MiB,
   so the rate is not MB/s either. Upstream fixed that in PR #852 (bytes bar,
   ``update(len(chunk))``), due in the first release after 1.1.0. We leave this
   path to upstream.

This wrapper re-drives path 1 by *bytes*: it sums each member's compressed size
from the remote zip's central directory, so the bar shows a real MB/s rate and
a byte-accurate ETA next to tqdm's elapsed time. The override mirrors upstream
1.1.0's function line for line; only the progress bar changes. See
``_partial_download_with_progress`` before upgrading open-mastr.

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
from zipfile import ZipFile

from tqdm import tqdm

import open_mastr.xml_download.utils_download_bulk as _bulk


def _partial_download_with_progress(
    save_path: str, url: str, names_to_download: set[str]
) -> None:
    """Byte-driven replacement for open-mastr's ``partial_download_with_unzip_http``.

    A copy of upstream 1.1.0's function (PR #843, issue #839) with the per-file
    bar swapped for a bytes bar, so the rate shows MB/s and the ETA tracks
    remaining bytes, not file count. Because it replaces the function wholesale,
    any upstream fix to it is shadowed: on every open-mastr upgrade, diff it
    against the installed ``inspect.getsource`` and re-port.

    Since open-mastr 1.0 the caller works out which tables are still missing
    and always includes Katalogwerte, so this only has to extract. Members are
    appended to ``<save_path>.part``, which replaces ``save_path`` only once
    every new member has its full uncompressed size. A dropped connection can
    end a member early without raising and still leave a valid zip, so without
    the size check a truncated XML would be parsed as if complete.
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

    if not download_files_list:
        return
    expected_sizes = {
        name: remote_zip_file.files[name].file_size for name in download_files_list
    }
    # Compressed sizes = bytes actually pulled over HTTP, read from the zip's
    # central directory (no extra requests).
    sizes = [remote_zip_file.files[name].compress_size for name in download_files_list]
    part_path = save_path + ".part"
    Path(part_path).unlink(missing_ok=True)  # left behind by a killed run
    try:
        # An earlier partial download left a zip to append to; otherwise start empty.
        if os.path.exists(save_path):
            shutil.copyfile(save_path, part_path)
        with tqdm(
            total=sum(sizes),
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc="MaStR download",
        ) as bar:
            for name, size in zip(download_files_list, sizes):
                remote_zip_file.extractzip(name, path=Path(part_path))
                bar.update(size)
        with ZipFile(part_path) as part_zip:
            incomplete = [
                name
                for name in download_files_list
                if part_zip.getinfo(name).file_size != expected_sizes[name]
            ]
        if incomplete:
            raise OSError(f"Incomplete download of {incomplete}")
        os.replace(part_path, save_path)
    finally:
        Path(part_path).unlink(missing_ok=True)


_bulk.partial_download_with_unzip_http = _partial_download_with_progress


# No local fix for multi-value catalog columns. open-mastr 0.17.2 stopped
# comma-separated codes (WeitereBrennstoffe "2442, 2442") from aborting the
# whole EinheitenVerbrennung table. Since 1.1.0 (PRs #819/#821) each code is
# resolved and the deduplicated names are joined with "|". The 1.0 join used
# ",", which is ambiguous because catalog names contain commas themselves: a
# store built with 1.0 holds "Heizöl, leicht,Erdgas, Erdölgas". Split these
# columns on "|" only, and only after a rebuild with >= 1.1.0.
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
