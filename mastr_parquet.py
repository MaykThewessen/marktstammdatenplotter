"""Parquet store for the Marktstammdatenregister bulk snapshot.

Same data as the open-mastr SQLite DB, one zstd parquet file per table, read
through DuckDB. Measured on the 2026-06-16 export (34 tables, 38.0 M rows):

    SQLite           11.32 GB    PV pipeline load 107 s
    parquet + zstd    1.30 GB    PV pipeline load 3.6 s

The 8.7x size drop is zstd over columnar chunks; the ~30x load speedup is the
row-store/column-store split — SQLite stores all 96 columns of a solar row
contiguously, so selecting 16 still pages through the other 80.

Two ways to populate `data/mastr/parquet/`:

1. `pixi run db-mastr-core` — downloads the bulk XML and writes parquet
   directly, never materialising the 11 GB SQLite (see scripts/mastr_download.py).
2. `pixi run db-mastr-parquet` — one-shot conversion of an existing SQLite DB
   via `convert_from_sqlite` below.

The read API mirrors `mastr_db` (load / load_geo / load_for_pipeline) and shares
that module's `pipeline_sql` + `finalise_pipeline_frame`, so both backends are
provably equivalent rather than parallel implementations that can drift.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd

import mastr_db

REPO_DIR = Path(__file__).resolve().parent / "data" / "mastr" / "parquet"
HOME_DIR = Path.home() / ".open-MaStR" / "data" / "parquet"
# Prefer in-repo store when present (gitignored); fall back to open-mastr's dir.
PARQUET_DIR = REPO_DIR if REPO_DIR.exists() else HOME_DIR


def set_store_dir(path: Path) -> Path:
    """Repoint the module-level store, creating it. Returns the new path.

    The writers call this so a first-ever run cannot land parquet in HOME_DIR
    while readers (which resolved PARQUET_DIR at import, before the directory
    existed) look in REPO_DIR.
    """
    global PARQUET_DIR
    PARQUET_DIR = Path(path)
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    return PARQUET_DIR

# zstd level 9: measured 1.30 GB total. Level 19 buys under 3 % for ~6x the
# write time, and these files are rewritten on every refresh.
COMPRESSION_LEVEL = 9

EXTENDED_TABLES = mastr_db.EXTENDED_TABLES


def _lit(value: object) -> str:
    """Single-quoted SQL literal. Needed because DuckDB rejects placeholders in
    ATTACH and in CREATE VIEW bodies (a view persists its query text, so a
    prepared parameter has nothing to bind to on later reads)."""
    return "'" + str(value).replace("'", "''") + "'"


# open-mastr's SQLAlchemy type -> (DuckDB cast, pandas dtype). SQLite has no
# date or boolean type, so it stores those as TEXT and INTEGER 0/1; parquet
# does have them. Normalising from the ORM in both population paths keeps the
# store's schema independent of how it was built.
# Microseconds, not nanoseconds: MaStR carries dates as far back as year 0100
# (market_actors.Taetigkeitsbeginn), which overflows the pandas ns range of
# 1677-2262 and made mixed-precision parts fail to merge. us also matches the
# us truncation open-mastr's SQLite writer applied via strftime.
_ORM_TYPE_MAP = {
    "String":   ("VARCHAR",   "string"),
    "Integer":  ("BIGINT",    "Int64"),
    "Float":    ("DOUBLE",    "float64"),
    "Boolean":  ("BOOLEAN",   "boolean"),
    "Date":     ("DATE",      "datetime64[us]"),
    "DateTime": ("TIMESTAMP", "datetime64[us]"),
}


def orm_dtypes(table: str) -> dict[str, tuple[str, str]]:
    """{column: (duckdb_cast, pandas_dtype)} from open-mastr's ORM for `table`.

    Imported lazily so that *reading* the store never requires open-mastr —
    only the two paths that populate it do.
    """
    from open_mastr.utils.orm import tablename_mapping

    for entry in tablename_mapping.values():
        cls = entry.get("__class__") if isinstance(entry, dict) else None
        if cls is None or cls.__table__.name != table:
            continue
        return {
            col.name: _ORM_TYPE_MAP[type(col.type).__name__]
            for col in cls.__table__.columns
            if type(col.type).__name__ in _ORM_TYPE_MAP
        }
    return {}


def orm_primary_key(table: str) -> str | None:
    """Primary-key column for `table`, or None if the ORM does not define one.

    Used to reproduce SQLite's `INSERT ... ON CONFLICT DO NOTHING` dedup when
    writing parquet, which has no constraints of its own.
    """
    from open_mastr.utils.orm import tablename_mapping

    for entry in tablename_mapping.values():
        cls = entry.get("__class__") if isinstance(entry, dict) else None
        if cls is None or cls.__table__.name != table:
            continue
        pks = [c.name for c in cls.__table__.primary_key.columns]
        return pks[0] if len(pks) == 1 else None
    return None


def table_path(table: str, parquet_dir: Path | None = None) -> Path:
    return (parquet_dir or PARQUET_DIR) / f"{table}.parquet"


def available_tables(parquet_dir: Path | None = None) -> list[str]:
    """Table names backed by a parquet file in the store (empty if no store)."""
    d = parquet_dir or PARQUET_DIR
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.parquet"))


def list_tables(parquet_dir: Path | None = None) -> list[str]:
    """Alias matching `mastr_db.list_tables` so callers can swap backends."""
    return available_tables(parquet_dir)


def connect(parquet_dir: Path | None = None) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with one view per parquet file, named after the table.

    Views (not table copies) keep this O(1) — DuckDB reads the parquet lazily
    and prunes to the projected columns. Because the views carry the original
    table names, `mastr_db.pipeline_sql` runs unchanged against this connection.
    """
    d = parquet_dir or PARQUET_DIR
    tables = available_tables(d)
    if not tables:
        raise FileNotFoundError(
            f"No parquet store at {d}. Run `pixi run db-mastr-core` to download "
            "straight to parquet, or `pixi run db-mastr-parquet` to convert an "
            "existing open-mastr.db."
        )
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    for t in tables:
        con.execute(
            f'CREATE VIEW "{t}" AS '
            f"SELECT * FROM read_parquet({_lit(table_path(t, d))})"
        )
    return con


def load(
    tech: str,
    columns: list[str] | None = None,
    where: str | None = None,
    parquet_dir: Path | None = None,
) -> pd.DataFrame:
    """Load an extended table for one technology. Mirrors `mastr_db.load`."""
    if tech not in EXTENDED_TABLES:
        raise ValueError(f"unknown tech {tech!r}; expected one of {list(EXTENDED_TABLES)}")
    table = EXTENDED_TABLES[tech]
    cols = ", ".join(f'"{c}"' for c in columns) if columns else "*"
    sql = f'SELECT {cols} FROM "{table}"'
    if where:
        sql += f" WHERE {where}"
    with connect(parquet_dir) as con:
        return con.execute(sql).df()


def load_geo(
    tech: str,
    columns: list[str] | None = None,
    where: str | None = None,
    drop_anonymised: bool = True,
    parquet_dir: Path | None = None,
) -> gpd.GeoDataFrame:
    """Load an extended table as a GeoDataFrame in EPSG:4326.

    drop_anonymised: when True (default) rows with missing Laengengrad or
    Breitengrad are dropped — typical for units < 30 kW under MaStR's
    publication rules. Unlike the SQLite path this filters in the scan, so the
    dropped rows are never materialised.
    """
    if drop_anonymised:
        coord_filter = "Laengengrad IS NOT NULL AND Breitengrad IS NOT NULL"
        where = f"({where}) AND {coord_filter}" if where else coord_filter
    df = load(tech, columns=columns, where=where, parquet_dir=parquet_dir)
    geom = gpd.points_from_xy(df["Laengengrad"], df["Breitengrad"], crs="EPSG:4326")
    return gpd.GeoDataFrame(df, geometry=geom, crs="EPSG:4326")


def count(tech: str, parquet_dir: Path | None = None) -> int:
    table = EXTENDED_TABLES[tech]
    with connect(parquet_dir) as con:
        return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def load_for_pipeline(
    tech: str, parquet_dir: Path | None = None, psh_backfill: bool = True,
) -> pd.DataFrame:
    """Parquet-backed twin of `mastr_db.load_for_pipeline`.

    Runs the identical SQL and the identical post-processing, so the returned
    frame matches the SQLite backend row for row.
    """
    if tech not in ("wind", "pv", "bess"):
        raise ValueError(f"unknown tech {tech!r}; expected wind|pv|bess")

    with connect(parquet_dir) as con:
        tables = {t.lower() for t in available_tables(parquet_dir)}
        if "market_actors" not in tables:
            raise RuntimeError(
                "market_actors.parquet missing; the owner_name JOIN would be NULL "
                "for every row. Re-run the download including the market table."
            )
        n_actors = con.execute("SELECT COUNT(*) FROM market_actors").fetchone()[0]
        if int(n_actors) == 0:
            raise RuntimeError(
                "market_actors is empty; owner_name JOIN would be NULL for every "
                "row. Run `pixi run db-mastr-market` to populate it."
            )
        storage_units_available = tech != "bess" or (
            "storage_units" in tables
            and int(con.execute("SELECT COUNT(*) FROM storage_units").fetchone()[0]) > 0
        )
        sql = mastr_db.pipeline_sql(
            tech, storage_units_available=storage_units_available
        )
        df = con.execute(sql).df()

    return mastr_db.finalise_pipeline_frame(df, tech, psh_backfill=psh_backfill)


# ---------------------------------------------------------------------------
# Incremental write path — used by scripts/mastr_download.py to land the bulk
# XML straight here, so the 11 GB SQLite is never created.
#
# One XML table can span many files (EinheitenSolar_1.xml ... _30.xml), so each
# is written as a "part" and merged by `finalise_table`. Parts, not one growing
# file, because parquet is immutable once written.
# ---------------------------------------------------------------------------

PARTS_SUBDIR = "_parts"
_PART_SEQ = "_part_seq"


def _orm_projection(
    con: duckdb.DuckDBPyConnection,
    from_expr: str,
    table: str,
    exclude: tuple[str, ...] = (),
) -> str:
    """Select list over `from_expr` with ORM types restored.

    Columns the ORM does not declare pass through unchanged, so extra columns
    the XML carries beyond the ORM survive.
    """
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {from_expr}").fetchall()]
    dtypes = orm_dtypes(table)
    parts = []
    for c in cols:
        if c in exclude or c == "_rn":
            continue
        cast = dtypes.get(c, (None, None))[0]
        parts.append(f'CAST("{c}" AS {cast}) AS "{c}"' if cast else f'"{c}"')
    return ", ".join(parts)


def parts_dir(table: str, parquet_dir: Path | None = None) -> Path:
    return (parquet_dir or PARQUET_DIR) / PARTS_SUBDIR / table


def write_part(
    df: pd.DataFrame, table: str, seq: int, parquet_dir: Path | None = None,
) -> Path:
    """Append one XML file's worth of rows as a parquet part.

    `seq` must increase in the order open-mastr yields files: `finalise_table`
    resolves duplicate primary keys by keeping the lowest seq, which is what
    SQLite's `INSERT ... ON CONFLICT DO NOTHING` does. Parts use zstd level 1 —
    they are transient, and the level-9 pass happens once at finalise.
    """
    d = parts_dir(table, parquet_dir)
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"part-{seq:05d}.parquet"
    df.assign(**{_PART_SEQ: seq}).to_parquet(
        dst, compression="zstd", compression_level=1, index=False
    )
    return dst


def finalise_table(
    table: str,
    parquet_dir: Path | None = None,
    compression_level: int = COMPRESSION_LEVEL,
) -> int:
    """Merge a table's parts into one deduplicated zstd parquet file.

    Returns the row count written. `union_by_name` covers parts with different
    column subsets (open-mastr adds columns to the SQLite table as it discovers
    them, so later XML files can carry columns the first ones lacked). Removes
    the parts directory on success.
    """
    d = parts_dir(table, parquet_dir)
    parts = sorted(d.glob("part-*.parquet"))
    if not parts:
        return 0
    out = parquet_dir or PARQUET_DIR
    out.mkdir(parents=True, exist_ok=True)
    dst = table_path(table, out)

    src = (f"read_parquet([{', '.join(_lit(p) for p in parts)}], "
           "union_by_name=true)")
    pk = orm_primary_key(table)

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    # Restore ORM types on the merged result so a directly-written store has the
    # same schema as a converted one (parts carry Date columns as us timestamps
    # because pandas has no date dtype; DuckDB narrows them back to DATE here).
    projection = _orm_projection(con, src, table, exclude=(_PART_SEQ,))
    if pk:
        inner = (f'SELECT *, row_number() OVER (PARTITION BY "{pk}" '
                 f'ORDER BY {_PART_SEQ}) AS _rn FROM {src}')
        query = f"SELECT {projection} FROM ({inner}) WHERE _rn = 1"
    else:
        query = f"SELECT {projection} FROM {src}"

    try:
        con.execute(
            f"COPY ({query}) TO {_lit(dst)} "
            f"(FORMAT parquet, COMPRESSION zstd, "
            f"COMPRESSION_LEVEL {int(compression_level)})"
        )
        n = int(con.execute(
            f"SELECT COUNT(*) FROM read_parquet({_lit(dst)})"
        ).fetchone()[0])
    finally:
        con.close()
    shutil.rmtree(d, ignore_errors=True)
    return n


# ---------------------------------------------------------------------------
# One-shot migration from an existing SQLite snapshot.
# ---------------------------------------------------------------------------


def convert_from_sqlite(
    db_path: Path | None = None,
    parquet_dir: Path | None = None,
    compression_level: int = COMPRESSION_LEVEL,
) -> dict[str, int]:
    """Convert every table in the open-mastr SQLite DB to zstd parquet.

    Returns {table: row_count}. Existing parquet files are overwritten. The
    SQLite DB is left untouched — delete it yourself once you have verified the
    store (it is regenerable via `pixi run db-mastr-core`).
    """
    src = db_path or mastr_db.DB_PATH
    if not Path(src).exists():
        raise FileNotFoundError(f"SQLite DB not found at {src}")
    out = parquet_dir or REPO_DIR
    out.mkdir(parents=True, exist_ok=True)

    written: dict[str, int] = {}
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("INSTALL sqlite; LOAD sqlite")
    con.execute(f"ATTACH {_lit(src)} AS s (TYPE sqlite, READ_ONLY)")
    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_catalog = 's' ORDER BY table_name"
        ).fetchall()]
        for t in tables:
            dst = table_path(t, out)
            # SQLite stores DATE as TEXT and BOOLEAN as INTEGER 0/1; the same
            # ORM projection used by finalise_table restores the real types, so
            # a converted store and a directly-written one share one schema.
            projection = _orm_projection(con, f's."{t}"', t)
            con.execute(
                f'COPY (SELECT {projection} FROM s."{t}") TO {_lit(dst)} '
                f"(FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL {int(compression_level)})"
            )
            written[t] = int(
                con.execute(f'SELECT COUNT(*) FROM s."{t}"').fetchone()[0]
            )
    finally:
        con.close()
    return written


def main() -> None:
    """`python mastr_parquet.py [sqlite_db]` — convert a SQLite snapshot."""
    import sys

    src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    written = convert_from_sqlite(db_path=src)
    total = sum(written.values())
    size = sum(p.stat().st_size for p in PARQUET_DIR.glob("*.parquet"))
    print(f"{len(written)} tables, {total} rows -> {PARQUET_DIR} "
          f"({size / 1e9:.2f} GB)")


if __name__ == "__main__":
    main()
