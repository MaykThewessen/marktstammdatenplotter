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


# ---------------------------------------------------------------------------
# Pipeline adapter — emit columns matching the Zenodo-parquet contract that
# mastr_plot.load_from_bulk consumes. Adapter outputs *Zenodo-style* column
# names + string values so load_from_bulk's existing rename/decoration block
# does the final translation with no new branches needed there.
# ---------------------------------------------------------------------------

# open-mastr ArtDerSolaranlage → Zenodo location_type (best-effort, lossy for
# water/parking_lot categories which open-mastr buckets under "Sonstige").
_PV_ART_TO_LOCATION = {
    "Gebäudesolaranlage":                                "Bauliche Anlagen (Hausdach, Gebäude und Fassade)",
    "Freiflächensolaranlage":                            "Freifläche",
    "Sonstige Solaranlage":                              "Bauliche Anlagen (Sonstige)",
    "Steckerfertige Solaranlage (sog. Balkonkraftwerk)": "Steckerfertige Solaranlage (sog. Balkonkraftwerk)",
}

# open-mastr Nutzungsbereich → Zenodo usage_type (drives load_from_bulk's
# usage_map for building_type). Two strings differ from Zenodo:
#   "Öffentliches Gebäude" vs "Öffentliche Einrichtungen"
#   "Landwirtschaft"       vs "Land-, Forst- und Fischereiwirtschaft"
# Unmapped values (e.g. "Sonstige") pass through unchanged → building_type=None.
_PV_NUTZUNG_TO_USAGE = {
    "Haushalt":                              "Haushalt",
    "Landwirtschaft":                        "Land-, Forst- und Fischereiwirtschaft",
    "Gewerbe, Handel und Dienstleistungen":  "Gewerbe, Handel und Dienstleistungen",
    "Industrie":                             "Industrie",
    "Öffentliches Gebäude":                  "Öffentliche Einrichtungen",
}

# open-mastr HauptausrichtungNeigungswinkel → tilt tuple matching parser.py.
# Adapter emits final `tilt` column directly (load_from_bulk defaults to None
# when missing — providing it here is strictly more informative).
_PV_NEIGUNG_TO_TILT = {
    "unter 5 Grad (horizontal)": (0, 5),
    "5 - 20 Grad":               (5, 20),
    "21 - 40 Grad":               (21, 40),
    "41 - 60 Grad":               (41, 60),
    "61 - 89 Grad":               (61, 89),
    "90 Grad (vertikal)":         90,
    "90 Grad (vertikal) ":        90,   # trailing space variant observed in DB
    "Nachgeführt":                "tracked",
}

_PIPELINE_COMMON_COLS = (
    "EinheitMastrNummer AS mastr_id",
    "NameStromerzeugungseinheit AS einheit_name",
    "Bruttoleistung AS gross_capacity_kw",
    "Inbetriebnahmedatum AS commissioning_date",
    "DatumEndgueltigeStilllegung AS decommissioning_date",
    "Laengengrad AS longitude",
    "Breitengrad AS latitude",
    "Landkreis AS landkreis",
    "Bundesland AS bundesland",
    "Gemeindeschluessel AS municipality_key",
)

_PIPELINE_DATE_COLS = (
    "commissioning_date",
    "decommissioning_date",
    "planned_commissioning_date",
)


def _ensure_market_actors(db_path: Path) -> None:
    """Strict-mode owner_name guard. Raises if market_actors is empty so
    callers get a clear diagnosis instead of silent NULL owner_name."""
    n = pd.read_sql(
        "SELECT COUNT(*) AS n FROM market_actors", con=engine(db_path),
    ).iloc[0, 0]
    if int(n) == 0:
        raise RuntimeError(
            "market_actors table is empty; owner_name JOIN would be NULL for "
            "every row. Run `pixi run db-mastr-market` to populate it."
        )


def load_for_pipeline(tech: str, db_path: Path = DB_PATH) -> pd.DataFrame:
    """Load `tech` shaped to the Zenodo-parquet column contract consumed by
    mastr_plot.load_from_bulk.

    tech ∈ {"wind", "pv", "bess"}.

    Returns a DataFrame with columns that match the bulk-parquet schema, so
    downstream rename + decoration in load_from_bulk works unchanged. Strict
    mode: raises if market_actors is empty (owner_name JOIN would be all NULL).
    """
    if tech not in ("wind", "pv", "bess"):
        raise ValueError(f"unknown tech {tech!r}; expected wind|pv|bess")

    _ensure_market_actors(db_path)
    eng = engine(db_path)
    common = ",\n    ".join(f"e.{c}" for c in _PIPELINE_COMMON_COLS)

    if tech == "wind":
        sql = f"""
        SELECT
            {common},
            e.WindAnLandOderAufSee AS location_type,
            e.Seelage              AS seelage,
            m.Firmenname           AS owner_name,
            e.NameWindpark         AS wind_park
        FROM wind_extended e
        LEFT JOIN market_actors m ON m.MastrNummer = e.AnlagenbetreiberMastrNummer
        """
    elif tech == "pv":
        sql = f"""
        SELECT
            {common},
            e.ArtDerSolaranlage              AS art_solaranlage,
            e.Nutzungsbereich                AS usage_type,
            e.Hauptausrichtung               AS orientation,
            e.HauptausrichtungNeigungswinkel AS neigung_raw,
            m.Firmenname                     AS owner_name
        FROM solar_extended e
        LEFT JOIN market_actors m ON m.MastrNummer = e.AnlagenbetreiberMastrNummer
        """
    else:  # bess
        # NutzbareSpeicherkapazitaet sits in `storage_units` (one-to-one with
        # storage_extended via VerknuepfteEinheit), NOT directly in
        # storage_extended where the column is always NULL. Skip the JOIN
        # gracefully if storage_units is empty (e.g. fresh download of only
        # `data=['storage']`).
        has_storage_units = (
            pd.read_sql("SELECT COUNT(*) AS n FROM storage_units", con=eng).iloc[0, 0] > 0
        )
        if has_storage_units:
            energy_select  = "su.NutzbareSpeicherkapazitaet AS usable_capacity_kwh"
            energy_join    = ("LEFT JOIN storage_units su "
                              "ON su.VerknuepfteEinheit = e.EinheitMastrNummer")
        else:
            energy_select  = "NULL AS usable_capacity_kwh"
            energy_join    = ""
        sql = f"""
        SELECT
            {common},
            {energy_select},
            e.GeplantesInbetriebnahmedatum AS planned_commissioning_date,
            e.Technologie                  AS storage_technology,
            m.Firmenname                   AS owner_name
        FROM storage_extended e
        LEFT JOIN market_actors m ON m.MastrNummer = e.AnlagenbetreiberMastrNummer
        {energy_join}
        """

    df = pd.read_sql(sql, con=eng, parse_dates=list(_PIPELINE_DATE_COLS))

    # PV-only post-processing: translate enum strings to Zenodo-style values
    # so load_from_bulk's loc_map / usage_map / facing_map matches verbatim.
    if tech == "pv":
        df["location_type"] = df["art_solaranlage"].map(_PV_ART_TO_LOCATION)
        df["tilt"] = df["neigung_raw"].map(_PV_NEIGUNG_TO_TILT)
        # Translate open-mastr Nutzungsbereich strings → Zenodo usage_type;
        # rows the map doesn't cover keep their original value (which then
        # becomes building_type=None downstream — matches existing behaviour).
        df["usage_type"] = df["usage_type"].map(_PV_NUTZUNG_TO_USAGE).fillna(df["usage_type"])
        df = df.drop(columns=["art_solaranlage", "neigung_raw"])

    # Wind off_shore label: load_from_bulk inspects location_type for the
    # offshore boolean and then uses longitude split for Nordsee/Ostsee, so
    # nothing more to derive here.

    return df
