"""
Thin loader for the open-mastr SQLite snapshot of the Marktstammdatenregister.

The open-mastr package downloads MaStR's bulk XML export into a SQLite DB at
$HOME/.open-MaStR/data/sqlite/open-mastr.db. Tables of interest here are
wind_extended, solar_extended, storage_extended — all rendered as pandas
DataFrames (or GeoDataFrames in EPSG:4326 if coordinates are present).

Refresh the DB with one of the pixi tasks (e.g. `pixi run db-mastr-core`)
or directly: `python -c "from open_mastr import Mastr; Mastr().download()"`.

Anonymisation: units < 30 kW have NULL Laengengrad/Breitengrad. Aggregate to
Kreis via the existing name-fallback in mastr_plot.py if you need them.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

_REPO_DB = Path(__file__).resolve().parent / "data" / "mastr" / "open-mastr.db"
_HOME_DB = Path.home() / ".open-MaStR" / "data" / "sqlite" / "open-mastr.db"
# Prefer in-repo copy when present (gitignored); fall back to open-mastr default.
DB_PATH = _REPO_DB if _REPO_DB.exists() else _HOME_DB

EXTENDED_TABLES = {
    "wind":    "wind_extended",
    "solar":   "solar_extended",
    "storage": "storage_extended",
    "hydro":   "hydro_extended",
    "biomass": "biomass_extended",
}


def engine(db_path: Path = DB_PATH) -> Engine:
    if not db_path.exists():
        raise FileNotFoundError(
            f"open-mastr DB not found at {db_path}. "
            "Run `pixi run db-mastr-core` (or db-mastr-all) to populate it."
        )
    return create_engine(f"sqlite:///{db_path}")


def load(
    tech: str,
    columns: list[str] | None = None,
    where: str | None = None,
    db_path: Path = DB_PATH,
) -> pd.DataFrame:
    """Load an extended table for one technology.

    tech: 'wind' | 'solar' | 'storage' | 'hydro' | 'biomass'
    columns: optional whitelist of column names (None = SELECT *)
    where: optional raw SQL WHERE clause (without the 'WHERE' keyword)
    """
    if tech not in EXTENDED_TABLES:
        raise ValueError(f"unknown tech {tech!r}; expected one of {list(EXTENDED_TABLES)}")
    table = EXTENDED_TABLES[tech]
    cols = ", ".join(columns) if columns else "*"
    sql = f"SELECT {cols} FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return pd.read_sql(sql, con=engine(db_path), parse_dates=[
        "Inbetriebnahmedatum",
        "GeplantesInbetriebnahmedatum",
        "DatumEndgueltigeStilllegung",
        "Meldedatum",
    ])


def load_geo(
    tech: str,
    columns: list[str] | None = None,
    where: str | None = None,
    drop_anonymised: bool = True,
    db_path: Path = DB_PATH,
) -> gpd.GeoDataFrame:
    """Load an extended table as a GeoDataFrame in EPSG:4326.

    drop_anonymised: when True (default) rows with missing Laengengrad or
    Breitengrad are dropped — typical for units < 30 kW under MaStR's
    publication rules.
    """
    df = load(tech, columns=columns, where=where, db_path=db_path)
    lon = df["Laengengrad"]
    lat = df["Breitengrad"]
    if drop_anonymised:
        mask = lon.notna() & lat.notna()
        df = df.loc[mask].copy()
        lon, lat = df["Laengengrad"], df["Breitengrad"]
    geom = gpd.points_from_xy(lon, lat, crs="EPSG:4326")
    return gpd.GeoDataFrame(df, geometry=geom, crs="EPSG:4326")


def list_tables(db_path: Path = DB_PATH) -> list[str]:
    return pd.read_sql(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        con=engine(db_path),
    )["name"].tolist()


def count(tech: str, db_path: Path = DB_PATH) -> int:
    table = EXTENDED_TABLES[tech]
    return int(pd.read_sql(f"SELECT COUNT(*) AS n FROM {table}", con=engine(db_path)).iloc[0, 0])
