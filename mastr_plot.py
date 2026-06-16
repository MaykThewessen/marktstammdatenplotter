"""Shared helpers for the marimo notebooks.

Provides:
  * load_records()        — read cached MaStR JSON, fall back to synthetic demo
  * load_admin_units()    — read germany_kreise.gpkg, fall back to a coarse demo
  * plot_choropleth()     — render a choropleth for a given date / energy_type
  * jenks_bins()          — Jenks natural-breaks bin edges
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import parser as mastr_parser


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIRS = [
    REPO_ROOT / "data",
    REPO_ROOT / "data-pv",
    REPO_ROOT.parent / "non-pv-data",
    REPO_ROOT / "non-pv-data",
]
DEFAULT_GPKG_PATHS = [
    REPO_ROOT / "germany_kreise.gpkg",
    REPO_ROOT.parent / "germany_kreise.gpkg",
]


def find_data_dirs() -> list[Path]:
    """All default dirs that contain *.json scrapes."""
    return [c for c in DEFAULT_DATA_DIRS if c.exists() and any(c.glob("*.json"))]


def find_gpkg() -> Path | None:
    for candidate in DEFAULT_GPKG_PATHS:
        if candidate.exists():
            return candidate
    return None


def _synthetic_records(n_pv: int = 4000, n_wind: int = 600, seed: int = 42) -> pd.DataFrame:
    """Demo data when no scrape is present.

    Returns the same columns as the real loader so the notebooks render even
    without a fresh MaStR pull.
    """
    rng = np.random.default_rng(seed)

    # Germany rough bbox
    lon_lo, lon_hi = 6.0, 14.5
    lat_lo, lat_hi = 47.4, 54.8

    def cluster(n, centers, scale):
        idx = rng.integers(0, len(centers), size=n)
        c = np.asarray(centers)[idx]
        return c + rng.normal(0, scale, size=(n, 2))

    pv_centers = [(11.5, 48.1), (8.6, 50.1), (9.2, 49.0), (13.4, 52.5), (10.0, 53.5), (7.0, 51.2)]
    pv_xy = cluster(n_pv, pv_centers, scale=0.9)
    pv_install = pd.to_datetime(
        rng.integers(pd.Timestamp("2005-01-01").value // 10**9,
                     pd.Timestamp("2025-09-01").value // 10**9, size=n_pv), unit="s"
    ).tz_localize(None)
    pv = pd.DataFrame({
        "id": np.arange(n_pv),
        "energy_type": "Solare Strahlungsenergie",
        "power": rng.lognormal(mean=2.5, sigma=1.0, size=n_pv) * 5.0,  # kW
        "longitude": np.clip(pv_xy[:, 0], lon_lo, lon_hi),
        "latitude": np.clip(pv_xy[:, 1], lat_lo, lat_hi),
        "install_date": pv_install,
        "removal_date": pd.NaT,
        "off_shore": None,
        "installation_type": rng.choice(
            ["building", "free", "balkonkraftwerk", "parking_lot"],
            size=n_pv, p=[0.55, 0.30, 0.10, 0.05]),
        "is_private": rng.random(n_pv) > 0.4,
    })

    wind_centers = [(7.8, 53.7), (9.1, 53.9), (8.5, 52.5), (11.0, 53.2), (13.0, 53.6)]
    wind_xy = cluster(n_wind, wind_centers, scale=0.6)
    wind_install = pd.to_datetime(
        rng.integers(pd.Timestamp("2000-01-01").value // 10**9,
                     pd.Timestamp("2025-09-01").value // 10**9, size=n_wind), unit="s"
    ).tz_localize(None)
    offshore_mask = rng.random(n_wind) < 0.06
    offshore_label = np.where(rng.random(n_wind) < 0.5, "Nordsee", "Ostsee")
    wind = pd.DataFrame({
        "id": np.arange(n_wind) + 10**6,
        "energy_type": "Wind",
        "power": rng.lognormal(mean=7.5, sigma=0.4, size=n_wind),  # kW (~MW range)
        "longitude": np.clip(wind_xy[:, 0], lon_lo, lon_hi),
        "latitude": np.clip(wind_xy[:, 1], lat_lo, lat_hi),
        "install_date": wind_install,
        "removal_date": pd.NaT,
        "off_shore": np.where(offshore_mask, offshore_label, None),
        "installation_type": None,
        "is_private": False,
    })

    df = pd.concat([pv, wind], ignore_index=True)
    return df


def load_records(data_dir: Path | None = None, demo_if_missing: bool = True,
                 prefer_bulk: bool = True,
                 source: str = "auto") -> tuple[pd.DataFrame, bool]:
    """Load records as a DataFrame.

    Returns (df, is_demo). When `is_demo` is True the rows are synthetic.

    If `prefer_bulk=True` (default) and a bulk snapshot is available — either
    the open-mastr SQLite at data/mastr/open-mastr.db (preferred) or the
    Zenodo parquet dir BNetzA_MaStR/ — it is loaded instead of the JSON-API
    scrape (which covers only the top-200 k slice). Falls back to the JSON
    scrape if neither bulk source is present.

    `source` is forwarded to load_from_bulk: "auto" picks SQLite when
    available, "sqlite" forces it, "zenodo" forces the legacy parquet path.

    If `data_dir` is None, all default dirs that hold *.json scrapes are merged
    so a single DataFrame contains wind + PV when both have been scraped.
    """
    if prefer_bulk and data_dir is None:
        frames = []
        for tech in ("pv", "wind"):
            try:
                df, _ = load_from_bulk(tech, source=source)
                frames.append(df)
            except FileNotFoundError:
                # No bulk source for this tech — fall through to JSON scrape.
                continue
        if frames:
            df = pd.concat(frames, ignore_index=True)
            # Drop tz so downstream snap-date comparisons work.
            for col in ("install_date", "removal_date"):
                if col in df.columns and isinstance(df[col].dtype, pd.DatetimeTZDtype):
                    df[col] = df[col].dt.tz_localize(None)
            return df, False

    if data_dir is not None:
        dirs = [Path(data_dir)]
    else:
        dirs = find_data_dirs()
    if not dirs:
        if not demo_if_missing:
            raise FileNotFoundError("No data-*.json files found in any default location.")
        return _synthetic_records(), True

    frames = []
    for d in dirs:
        records = mastr_parser.load_data(str(d))
        if records:
            frames.append(pd.DataFrame([r.__dict__ for r in records]))
    if not frames:
        if not demo_if_missing:
            raise FileNotFoundError(f"No parseable records in {dirs}.")
        return _synthetic_records(), True
    df = pd.concat(frames, ignore_index=True)
    df["install_date"] = pd.to_datetime(df["install_date"]).dt.tz_localize(None)
    df["removal_date"] = pd.to_datetime(df["removal_date"]).dt.tz_localize(None)
    return df, False


def _synthetic_admin_units():
    """A coarse 4x4 grid over Germany. Only used when the GPKG is missing."""
    import geopandas as gpd
    from shapely.geometry import Polygon

    lon_lo, lon_hi = 6.0, 15.0
    lat_lo, lat_hi = 47.5, 55.0
    nx, ny = 6, 6
    polys = []
    names = []
    for i in range(nx):
        for j in range(ny):
            x0 = lon_lo + (lon_hi - lon_lo) * i / nx
            x1 = lon_lo + (lon_hi - lon_lo) * (i + 1) / nx
            y0 = lat_lo + (lat_hi - lat_lo) * j / ny
            y1 = lat_lo + (lat_hi - lat_lo) * (j + 1) / ny
            polys.append(Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]))
            names.append(f"demo_{i}_{j}")
    return gpd.GeoDataFrame({"name": names, "admin_level": "6"}, geometry=polys, crs="EPSG:4326")


def load_admin_units(gpkg_path: Path | None = None, demo_if_missing: bool = True):
    import geopandas as gpd

    chosen = Path(gpkg_path) if gpkg_path else find_gpkg()
    if chosen is None:
        if not demo_if_missing:
            raise FileNotFoundError("germany_kreise.gpkg not found in default locations.")
        return _synthetic_admin_units(), True

    gdf = gpd.read_file(chosen, layer="multipolygons")
    return gdf, False


#: BESS sector boundaries (kWh). Matches battery-charts.de (RWTH Aachen),
#: BVES, and Figgener et al. conventions used across German + EU reporting.
BESS_HSS_KWH_MAX = 30.0          # Home storage
BESS_CSS_KWH_MAX = 1000.0        # Commercial / industrial
# LSS = anything ≥ 1 MWh                       (Großspeicher)

BESS_SECTORS = ["HSS (<30 kWh)", "CSS (30 kWh – 1 MWh)", "LSS (≥1 MWh)"]


def bess_sector(energy_kwh: float | None) -> str:
    """Classify a battery unit into HSS / CSS / LSS by usable kWh.

    Matches the convention used by https://battery-charts.de (RWTH Aachen,
    Figgener et al.) and reproduced by BVES, EASE, and the EU SET-Plan.
    Unknown / zero capacity → 'unknown' so the caller can filter it
    explicitly.
    """
    if energy_kwh is None or pd.isna(energy_kwh) or energy_kwh <= 0:
        return "unknown"
    if energy_kwh < BESS_HSS_KWH_MAX:
        return BESS_SECTORS[0]
    if energy_kwh < BESS_CSS_KWH_MAX:
        return BESS_SECTORS[1]
    return BESS_SECTORS[2]


def bess_sector_series(energy_kwh) -> pd.Series:
    """Vectorised HSS/CSS/LSS classifier. Equivalent to bess_sector row-wise
    but uses pd.cut on the whole column — required for the 1.75M-row bulk
    dump where .apply() is unacceptably slow.
    """
    s = pd.to_numeric(energy_kwh, errors="coerce")
    bins = [0.0, BESS_HSS_KWH_MAX, BESS_CSS_KWH_MAX, np.inf]
    cats = pd.cut(s, bins=bins, labels=BESS_SECTORS, right=False).astype(object)
    return cats.where(s > 0, "unknown")


def load_bess(data_dir: Path | None = None, demo_if_missing: bool = False,
              prefer_bulk: bool = True,
              source: str = "auto") -> tuple[pd.DataFrame, bool]:
    """Load BESS records as a tidy DataFrame.

    Returns (df, is_demo). With demo_if_missing=False (default) a missing
    scrape raises FileNotFoundError — the BESS-specific synthetic dataset
    is not implemented yet because the per-Kreis names are pre-joined in
    the source JSON and the demo data wouldn't reflect that.

    If `prefer_bulk=True` (default) a bulk snapshot is preferred — either
    the open-mastr SQLite at data/mastr/open-mastr.db (`source="auto"`,
    default) or BNetzA_MaStR/storage.parquet. Falls back to the JSON scrape
    if neither bulk source is present.
    """
    if prefer_bulk and data_dir is None:
        try:
            df, _ = load_from_bulk("bess", source=source)
            return df, False
        except FileNotFoundError:
            pass  # fall through to JSON scrape below

    candidates = [
        REPO_ROOT / "data-bess",
        REPO_ROOT.parent / "data-bess",
    ]
    if data_dir is not None:
        chosen = Path(data_dir)
    else:
        chosen = next((c for c in candidates if c.exists() and any(c.glob("*.json"))), None)
    if chosen is None:
        if not demo_if_missing:
            raise FileNotFoundError(
                "No BESS scrape found. Run `pixi run scrape-bess` first."
            )
        return pd.DataFrame(), True

    units = mastr_parser.load_bess(str(chosen))
    df = pd.DataFrame([u.__dict__ for u in units])
    df["install_date"] = pd.to_datetime(df["install_date"]).dt.tz_localize(None)
    df["planned_date"] = pd.to_datetime(df["planned_date"]).dt.tz_localize(None)
    df["removal_date"] = pd.to_datetime(df["removal_date"]).dt.tz_localize(None)
    df["effective_date"] = df["install_date"].fillna(df["planned_date"])
    df["duration_h"] = df["energy_kwh"] / df["power_kw"].replace(0, np.nan)
    df["sector"] = bess_sector_series(df["energy_kwh"])
    df["is_battery"] = df["storage_tech"] == "Batterie"
    df["is_psh"] = df["storage_tech"] == "Pumpspeicher"
    # Pre-compute normalised Kreis names — enables name-based fallback in
    # aggregate_bess_by_unit for residential rows with anonymised coords.
    if "landkreis" in df.columns:
        df["landkreis_norm"] = normalise_kreis_series(df["landkreis"])
    return df, False


#: Default search paths for the open-MaStR bulk parquet dump.
#: Walks several parents to find a sibling BNetzA_MaStR/ — works whether the
#: code runs from the main repo root or a git worktree.
def _build_bulk_dir_candidates():
    out = []
    env = os.environ.get("MASTR_BULK_DIR")
    if env:
        out.append(Path(env))
    p = REPO_ROOT
    for _ in range(6):
        out.append(p / "BNetzA_MaStR")
        p = p.parent
    return out


DEFAULT_BULK_DIRS = _build_bulk_dir_candidates()


def find_bulk_dir() -> Path | None:
    """Locate an open-MaStR bulk parquet directory if present.

    Honors the MASTR_BULK_DIR env var first, then walks up the directory
    tree from REPO_ROOT looking for a sibling BNetzA_MaStR/ folder.
    """
    for c in DEFAULT_BULK_DIRS:
        if c.exists() and any(c.glob("*.parquet")):
            return c
    return None


# BKG VG2500 names → Destatis 5-digit Kreisschlüssel for the 23 city/Landkreis
# pairs that collide after normalisation (e.g. MaStR `landkreis="Passau"` maps
# to both "Passau, Kreisfreie Stadt" and "Passau, Landkreis"). Used by
# `aggregate_by_unit` / `aggregate_bess_by_unit` to disambiguate anonymised
# rows via the first 5 digits of MaStR's `municipality_key` (AGS) field.
KREIS_NAME_TO_AGS5 = {
    # Bavaria
    "München, Kreisfreie Stadt":          "09162",
    "München, Landkreis":                 "09184",
    "Rosenheim, Kreisfreie Stadt":        "09163",
    "Rosenheim, Landkreis":               "09187",
    "Landshut, Kreisfreie Stadt":         "09261",
    "Landshut, Landkreis":                "09274",
    "Passau, Kreisfreie Stadt":           "09262",
    "Passau, Landkreis":                  "09275",
    "Regensburg, Kreisfreie Stadt":       "09362",
    "Regensburg, Landkreis":              "09375",
    "Bamberg, Kreisfreie Stadt":          "09461",
    "Bamberg, Landkreis":                 "09471",
    "Bayreuth, Kreisfreie Stadt":         "09462",
    "Bayreuth, Landkreis":                "09472",
    "Coburg, Kreisfreie Stadt":           "09463",
    "Coburg, Landkreis":                  "09473",
    "Hof, Kreisfreie Stadt":              "09464",
    "Hof, Landkreis":                     "09475",
    "Ansbach, Kreisfreie Stadt":          "09561",
    "Ansbach, Landkreis":                 "09571",
    "Fürth, Kreisfreie Stadt":            "09563",
    "Fürth, Landkreis":                   "09573",
    "Aschaffenburg, Kreisfreie Stadt":    "09661",
    "Aschaffenburg, Landkreis":           "09671",
    "Schweinfurt, Kreisfreie Stadt":      "09662",
    "Schweinfurt, Landkreis":             "09678",
    "Würzburg, Kreisfreie Stadt":         "09663",
    "Würzburg, Landkreis":                "09679",
    "Augsburg, Kreisfreie Stadt":         "09761",
    "Augsburg, Landkreis":                "09772",
    # Baden-Württemberg
    "Heilbronn, Stadtkreis":              "08121",
    "Heilbronn, Landkreis":               "08125",
    "Karlsruhe, Stadtkreis":              "08212",
    "Karlsruhe, Landkreis":               "08215",
    # Hessen
    "Kassel, Kreisfreie Stadt":           "06611",
    "Kassel, Landkreis":                  "06633",
    # Rheinland-Pfalz
    "Kaiserslautern, Kreisfreie Stadt":   "07312",
    "Kaiserslautern, Landkreis":          "07335",
    # Niedersachsen
    "Oldenburg (Oldenburg), Kreisfreie Stadt": "03403",
    "Oldenburg, Landkreis":               "03458",
    "Osnabrück, Kreisfreie Stadt":        "03404",
    "Osnabrück, Landkreis":               "03459",
    # Mecklenburg-Vorpommern
    "Rostock, Kreisfreie Stadt":          "13003",
    "Landkreis Rostock":                  "13072",
    # Sachsen
    "Leipzig, Kreisfreie Stadt":          "14713",
    "Leipzig":                            "14729",
}


_KREIS_PREFIX_RE = re.compile(r"^(Stadt|Landkreis|Kreis|LK)\s+", re.IGNORECASE)
_KREIS_PAREN_RE = re.compile(r"\s+\([^)]+\)$")
_KREIS_STADTE_RE = re.compile(r"\s+St[aä]dte$", re.IGNORECASE)
_KREIS_SUFFIX_RE = re.compile(r"\s+-?Kreis$", re.IGNORECASE)
# BKG VG2500 names use ", <type>" suffix: "Passau, Landkreis",
# "München, Kreisfreie Stadt", "Karlsruhe, Stadtkreis", "Berlin, Stadt".
_KREIS_COMMA_TYPE_RE = re.compile(
    r",\s*(Landkreis|Kreisfreie\s+Stadt|Stadtkreis|Stadt|Kreis)\s*$",
    re.IGNORECASE,
)
_KREIS_DOT_WS_RE = re.compile(r"\s*\.\s*")
_KREIS_WS_RE = re.compile(r"\s+")
_UMLAUT_TRANS = str.maketrans(
    {"ä": "a", "ö": "o", "ü": "u", "ß": "ss", "Ä": "a", "Ö": "o", "Ü": "u"}
)


def normalise_kreis_name(s):
    """Normalise a German Landkreis name for fuzzy matching across sources.

    Bridges the open-MaStR bulk dump's `landkreis` text values against the
    BKG VG2500-derived GPKG `name` values. Mismatch patterns observed:
      * Parenthetical suffixes:        "Frankfurt (Oder)" → "frankfurt"
      * -Kreis suffix (MaStR):         "Börde-Kreis" → "borde"
      * Städte suffix (GPKG city):     "Amberg Städte" → "amberg"
      * Stadt prefix:                  "Stadt Berlin" → "berlin"
      * Landkreis prefix (rare):       "Landkreis Augsburg" → "augsburg"
      * Whitespace + casing            normalised
      * German umlauts                 collapsed to ASCII
      * Abbreviation spacing (Bayern): "Neumarkt i.d. OPf." vs
                                       "Neumarkt i.d.OPf." — all whitespace
                                       around `.` is stripped so both
                                       collapse to the same key.

    Combined recovery on this dataset is ~97 % of solar GW. Residual
    unmatched rows are typically MaStR records with `landkreis="Null"`
    or municipality-only entries (no Kreis assigned at registration).
    """
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    s = str(s).strip()
    s = _KREIS_COMMA_TYPE_RE.sub("", s)
    s = _KREIS_PREFIX_RE.sub("", s)
    s = _KREIS_PAREN_RE.sub("", s)
    s = _KREIS_STADTE_RE.sub("", s)
    s = _KREIS_SUFFIX_RE.sub("", s)
    s = s.translate(_UMLAUT_TRANS).lower()
    s = _KREIS_DOT_WS_RE.sub(".", s)
    s = _KREIS_WS_RE.sub(" ", s).strip()
    return s


def normalise_kreis_series(s: pd.Series) -> pd.Series:
    """Vectorised wrapper around `normalise_kreis_name`.

    Cardinality of Kreis names is ~400 across Germany while MaStR record
    counts run into millions; normalising via `.apply` reapplies the regex
    chain per row. Compute on the unique values once and map back.
    """
    uniq = s.dropna().unique()
    lut = {v: normalise_kreis_name(v) for v in uniq}
    return s.map(lut)


def _resolve_bulk_source(
    source: str, bulk_dir: Path | None,
) -> tuple[str, Path | None]:
    """Decide between open-mastr SQLite and the Zenodo parquet directory.

    Returns (resolved_source, bulk_path_or_None). resolved_source is one of
    "sqlite" or "zenodo". For "auto", the SQLite path wins when the DB exists
    AND has rows; otherwise we fall back to the parquet directory.
    """
    if source not in ("zenodo", "sqlite", "auto"):
        raise ValueError(f"unknown source {source!r}; expected zenodo|sqlite|auto")

    if source == "sqlite":
        # Force: import + return without touching the parquet path.
        import mastr_db
        if not mastr_db.DB_PATH.exists():
            raise FileNotFoundError(
                f"source='sqlite' requested but {mastr_db.DB_PATH} does not exist. "
                "Run `pixi run db-mastr-core` first."
            )
        return "sqlite", None

    if source == "zenodo":
        chosen = Path(bulk_dir) if bulk_dir else find_bulk_dir()
        if chosen is None:
            raise FileNotFoundError(
                "No open-MaStR bulk dump found. Expected solar.parquet / wind.parquet"
                " / storage.parquet under BNetzA_MaStR/."
            )
        return "zenodo", chosen

    # auto: prefer SQLite when present + populated; otherwise fall back.
    import mastr_db
    if mastr_db.DB_PATH.exists():
        try:
            # Cheap row probe — sqlite_master has no penalty.
            tables = mastr_db.list_tables()
            if {"wind_extended", "solar_extended", "storage_extended"} <= set(tables):
                return "sqlite", None
        except Exception:
            pass  # fall through to parquet
    chosen = Path(bulk_dir) if bulk_dir else find_bulk_dir()
    if chosen is None:
        raise FileNotFoundError(
            "source='auto' could not locate either the open-mastr SQLite DB or "
            "a Zenodo parquet bulk dir (BNetzA_MaStR/). Run "
            "`pixi run db-mastr-core` or place the Zenodo dump."
        )
    return "zenodo", chosen


def load_from_bulk(
    tech: str,
    bulk_dir: Path | None = None,
    source: str = "auto",
) -> tuple[pd.DataFrame, bool]:
    """Load an open-MaStR bulk snapshot for a single technology.

    `source` selects the backing store:
      * "auto"   — open-mastr SQLite if present, else Zenodo parquet (default).
      * "sqlite" — force open-mastr SQLite via mastr_db.load_for_pipeline.
      * "zenodo" — force the legacy Zenodo parquet path.

    Returns (df, is_bulk). DataFrame matches the column schema used by the
    existing JSON-scrape loaders (`load_records`, `load_bess`) so downstream
    rendering works unchanged. Specifically: install_date, removal_date,
    power, power_kw, energy_kwh, longitude, latitude, landkreis, bundesland,
    energy_type, off_shore, owner_name.

    `tech` ∈ {"pv", "wind", "bess"}.
    """
    if tech not in ("pv", "wind", "bess"):
        raise ValueError(f"unknown tech: {tech!r}")

    resolved, chosen = _resolve_bulk_source(source, bulk_dir)

    energy_type = {
        "pv":   "Solare Strahlungsenergie",
        "wind": "Wind",
        "bess": "Speicher",
    }[tech]

    if resolved == "sqlite":
        import mastr_db
        df = mastr_db.load_for_pipeline(tech)
    else:
        # Prefer the merged historical+delta `full-*.parquet` when present;
        # fall back to the bare Zenodo-snapshot file otherwise.
        bulk_filename = {
            "pv":   ("full-solar.parquet",   "solar.parquet"),
            "wind": ("full-wind.parquet",    "wind.parquet"),
            "bess": ("full-storage.parquet", "storage.parquet"),
        }[tech]
        path = chosen / bulk_filename[0]
        if not path.exists():
            path = chosen / bulk_filename[1]
        if not path.exists():
            raise FileNotFoundError(f"{path} not present in bulk dir {chosen}")
        df = pd.read_parquet(path)

    # Common renames + decorations to match the existing scrape schema.
    rename = {
        "mastr_id": "id",
        "gross_capacity_kw": "power",
        "usable_capacity_kwh": "energy_kwh",
        "commissioning_date": "install_date",
        "decommissioning_date": "removal_date",
        "planned_commissioning_date": "planned_date",
        "storage_technology": "storage_tech",
        "einheit_name": "name",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    df["energy_type"] = energy_type
    # Strip tz so the rest of the pipeline (which uses tz-naive snap dates)
    # can compare without warning.
    for col in ("install_date", "removal_date", "planned_date"):
        if col in df.columns and isinstance(df[col].dtype, pd.DatetimeTZDtype):
            df[col] = df[col].dt.tz_localize(None)

    # power_kw alias for BESS-style aggregations.
    if "power" in df.columns:
        df["power_kw"] = df["power"]

    # Bulk dump anonymises owner_name for residential rows — column may
    # be absent entirely. For wind, populate from `wind_park` (the
    # MaStR project / Windpark name) so the per-project size-bin chart
    # still produces a meaningful aggregation. Other techs get None.
    if "owner_name" not in df.columns:
        if tech == "wind" and "wind_park" in df.columns:
            df["owner_name"] = df["wind_park"]
        else:
            df["owner_name"] = None

    # Off-shore label only meaningful for wind in the JSON scrape; add
    # a placeholder so concat with wind-only off_shore doesn't choke.
    if "off_shore" not in df.columns:
        df["off_shore"] = None

    # Map bulk PV `location_type` (free German text) onto the short
    # enum-decoded labels used by the JSON-scrape PV pipeline.
    if tech == "pv" and "location_type" in df.columns:
        loc_map = {
            "Bauliche Anlagen (Hausdach, Gebäude und Fassade)": "building",
            "Steckerfertige Solaranlage (sog. Balkonkraftwerk)": "balkonkraftwerk",
            "Bauliche Anlagen (Sonstige)": "building_other",
            "Freifläche": "free",
            "Großparkplatz": "parking_lot",
            "Gewässer": "water",
        }
        df["installation_type"] = df["location_type"].map(loc_map)
        # Also derive `building_type` from `usage_type` for PV-by-use-case
        # charts (Haushalt → household, etc.).
        usage_map = {
            "Haushalt": "household",
            "Gewerbe, Handel und Dienstleistungen": "commercial",
            "Industrie": "industry",
            "Land-, Forst- und Fischereiwirtschaft": "farming",
            "Öffentliche Einrichtungen": "public",
        }
        if "usage_type" in df.columns:
            df["building_type"] = df["usage_type"].map(usage_map).where(
                df["usage_type"].notna(), None,
            )
        # Map `orientation` text → `facing` decimal-degrees value matching
        # the JSON-scrape's PowerPlant.facing field semantics.
        if "orientation" in df.columns:
            facing_map = {
                "Nord": 0, "Nord-Ost": 45, "Ost": 90,
                "Süd-Ost": 135, "Süd": 180, "Süd-West": 225,
                "West": 270, "Nord-West": 315,
                "Nachgeführt": "tracked", "Ost-West": "east-west",
            }
            df["facing"] = df["orientation"].map(facing_map)
        else:
            df["facing"] = None
        # Tilt isn't in the bulk schema; downstream charts already accept None.
        if "tilt" not in df.columns:
            df["tilt"] = None
        # is_private flag — bulk dump anonymises residential rows, but we can
        # treat single-module Haushalt installations as private.
        if "is_private" not in df.columns:
            df["is_private"] = (df["usage_type"] == "Haushalt") if "usage_type" in df.columns else False

    # Add a normalised landkreis column for downstream name-based joins
    # (saves re-running normalise_kreis_name() at every aggregation step).
    if "landkreis" in df.columns:
        df["landkreis_norm"] = normalise_kreis_series(df["landkreis"])

    # off_shore label for wind. The open-mastr SQLite carries an authoritative
    # `seelage` column (Nordsee / Ostsee, MaStR-assigned; NULL for onshore) —
    # prefer it. The Zenodo parquet has no such column, so fall back to a
    # longitude split (Nordsee west of ~10 deg E, Ostsee east) for offshore.
    if tech == "wind" and "seelage" in df.columns and df["seelage"].notna().any():
        df["off_shore"] = df["seelage"]
    elif tech == "wind" and "location_type" in df.columns:
        is_offshore = df["location_type"] == "Windkraft auf See"
        df["off_shore"] = None
        if is_offshore.any():
            if "longitude" in df.columns:
                df.loc[is_offshore & (df["longitude"] < 10.0), "off_shore"] = "Nordsee"
                df.loc[is_offshore & (df["longitude"] >= 10.0), "off_shore"] = "Ostsee"
            # Fall back to Nordsee for rows with no coordinates (shouldn't
            # happen for offshore: MaStR keeps offshore lat/lon) so we avoid
            # dropping bars from the offshore chart.
            df.loc[is_offshore & df["off_shore"].isna(), "off_shore"] = "Nordsee"

    # BESS-style helper columns so aggregate_bess_by_unit / sector / etc.
    # all work against the bulk loader output.
    if tech == "bess":
        if "energy_kwh" in df.columns and "power_kw" in df.columns:
            df["duration_h"] = df["energy_kwh"] / df["power_kw"].replace(0, np.nan)
        if "energy_kwh" in df.columns:
            df["sector"] = bess_sector_series(df["energy_kwh"])
        if "storage_tech" in df.columns:
            df["is_battery"] = df["storage_tech"] == "Batterie"
            df["is_psh"] = df["storage_tech"] == "Pumpspeicher"

    if "install_date" in df.columns:
        if "planned_date" in df.columns:
            df["effective_date"] = df["install_date"].fillna(df["planned_date"])
        else:
            df["effective_date"] = df["install_date"]

    return df, True


def _ensure_wgs84(admin_units):
    """Reproject admin_units to EPSG:4326 if needed.

    All record coordinates (`longitude`, `latitude`) are EPSG:4326. Silently
    accepting a mismatched-CRS admin_units would produce wrong spatial joins,
    so reproject defensively. No-op when already WGS84 or CRS is missing
    (synthetic units case).
    """
    if admin_units.crs is not None and admin_units.crs.to_epsg() != 4326:
        return admin_units.to_crs("EPSG:4326")
    return admin_units


def aggregate_by_landkreis_name(
    records: pd.DataFrame,
    admin_units,
    plot_date: date,
    *,
    energy_type: str | None = None,
    value_col: str = "power",
    out_col: str = "power_gw",
    divisor: float = 1e6,
):
    """Per-Kreis aggregation by **name** match instead of spatial join.

    Critical for the bulk parquet dump where ~95 % of residential plants
    have NaN lat/lon (BNetzA anonymises them) but `landkreis_norm` text
    is present. Name match recovers ~86 % of records with the built-in
    normalisation.
    """
    ts = pd.Timestamp(plot_date)
    sub = records
    if energy_type is not None:
        sub = sub[sub["energy_type"] == energy_type]
    sub = sub[
        (sub["install_date"] <= ts)
        & (sub["removal_date"].isna() | (sub["removal_date"] > ts))
    ].copy()
    if sub.empty:
        out = admin_units.copy()
        out[out_col] = 0.0
        return out, sub

    by_kreis = sub.groupby("landkreis_norm")[value_col].sum() / divisor
    out = admin_units.copy()
    # admin_units `name` may or may not need normalising. Build a lookup.
    key = normalise_kreis_series(out["name"])
    out[out_col] = key.map(by_kreis).astype(float).fillna(0.0)
    return out, sub


def split_bess_storage(df: pd.DataFrame) -> dict:
    """Return three disjoint slices: batteries, pumped-hydro, and "other".

    Convention follows battery-charts.de / BVES — pumped-hydro is reported
    separately from batteries because it has a fundamentally different
    technology, lifecycle (50+ years vs 10-15), and grid-services profile.
    "Other" picks up Wasserstoffspeicher / Druckluft / Schwungrad which
    are all sub-MW and mostly pilot installations.
    """
    return {
        "batteries": df[df["is_battery"]].copy(),
        "psh":       df[df["is_psh"]].copy(),
        "other":     df[~df["is_battery"] & ~df["is_psh"]].copy(),
    }


def aggregate_bess_by_unit(
    records: pd.DataFrame,
    admin_units,
    plot_date: date,
    *,
    include_planned: bool = False,
):
    """Aggregate active BESS to admin units, summing power (GW) and energy (GWh).

    Uses spatial join for units that have coordinates, and a name-based
    landkreis join for the majority of residential units whose coordinates
    are anonymised in the BNetzA bulk dump.

    `include_planned=True` adds units whose `install_date` is NaT but whose
    `planned_date` precedes `plot_date`.
    """
    import geopandas as gpd

    admin_units = _ensure_wgs84(admin_units)
    ts = pd.Timestamp(plot_date)
    date_col = "effective_date" if include_planned else "install_date"
    sub = records[
        (records[date_col] <= ts)
        & (records["removal_date"].isna() | (records["removal_date"] > ts))
    ].copy()
    if sub.empty:
        out = admin_units.copy()
        out["power_gw"] = 0.0
        out["energy_gwh"] = 0.0
        return out, sub

    has_coords = sub["latitude"].notna() & sub["longitude"].notna()
    pwr_by_idx = pd.Series(0.0, index=admin_units.index, dtype=float)
    enr_by_idx = pd.Series(0.0, index=admin_units.index, dtype=float)

    # --- spatial join for units with known coordinates ---
    if has_coords.any():
        geo_sub = sub[has_coords]
        geom = gpd.points_from_xy(geo_sub["longitude"], geo_sub["latitude"], crs="EPSG:4326")
        pts = gpd.GeoDataFrame(geo_sub, geometry=geom, crs="EPSG:4326")
        joined = gpd.sjoin(pts, admin_units[["geometry"]], predicate="within", how="left")
        pwr_by_idx = pwr_by_idx.add(
            joined.groupby("index_right")["power_kw"].sum() / 1e6, fill_value=0.0
        )
        enr_by_idx = enr_by_idx.add(
            joined.groupby("index_right")["energy_kwh"].sum() / 1e6, fill_value=0.0
        )

    # --- name-based join for anonymised rows (no coordinates) ---
    if (~has_coords).any() and "landkreis_norm" in sub.columns:
        anon = sub[~has_coords]
        key = normalise_kreis_series(admin_units["name"])
        # MaStR landkreis text is just the city/county name (e.g. "Passau")
        # but BKG VG2500 has two entries — "Passau, Landkreis" and
        # "Passau, Kreisfreie Stadt" — that both normalise to "passau".
        # MaStR does not disambiguate. Drop duplicate-name lookup keys and
        # attribute deterministically to the first matching Kreis.
        name_to_idx = pd.Series(admin_units.index, index=key.values)
        name_to_idx = name_to_idx[~name_to_idx.index.duplicated(keep="first")]
        anon_idx = anon["landkreis_norm"].map(name_to_idx)
        # AGS5 override (see aggregate_by_unit for explanation).
        if "municipality_key" in anon.columns:
            ags5_to_idx = {
                KREIS_NAME_TO_AGS5[n]: i
                for i, n in admin_units["name"].items()
                if n in KREIS_NAME_TO_AGS5
            }
            # Use `.str[:5]` directly so NaN municipality_key stays NaN
            # (instead of being coerced to the literal string "nan", which
            # would never match an AGS5 key and silently bypass the override
            # without surfacing the gap).
            anon_ags5 = anon["municipality_key"].str[:5]
            ags_override = anon_ags5.map(ags5_to_idx)
            anon_idx = ags_override.combine_first(anon_idx)
        pwr_name = anon.groupby(anon_idx)["power_kw"].sum() / 1e6
        enr_name = anon.groupby(anon_idx)["energy_kwh"].sum() / 1e6
        pwr_by_idx = pwr_by_idx.add(pwr_name, fill_value=0.0)
        enr_by_idx = enr_by_idx.add(enr_name, fill_value=0.0)

    out = admin_units.copy()
    out["power_gw"] = pwr_by_idx.reindex(out.index).fillna(0.0)
    out["energy_gwh"] = enr_by_idx.reindex(out.index).fillna(0.0)
    return out, sub


def jenks_bins(values: np.ndarray, k: int = 7) -> list[float]:
    """Jenks-style natural breaks; falls back to quantiles if mapclassify missing."""
    values = np.asarray(values)
    values = values[~np.isnan(values)]
    if values.size == 0:
        return [0.0, 1.0]
    try:
        import mapclassify
        nb = mapclassify.NaturalBreaks(values, k=min(k, max(1, values.size - 1)))
        return [float(values.min())] + [float(b) for b in nb.bins]
    except Exception:
        qs = np.linspace(0, 1, k + 1)
        return [float(x) for x in np.quantile(values, qs)]


def aggregate_by_unit(
    records: pd.DataFrame,
    admin_units,
    plot_date: date,
    energy_type: str,
    *,
    include_planned: bool = False,
):
    """Aggregate active plants to admin units, summing power (GW).

    Uses spatial join for units with coordinates, and a name-based landkreis
    join for anonymised rows (no lat/lon but `landkreis_norm` present). For
    PV in the bulk dump this recovers ~95% of installed capacity that the
    spatial-only path drops.

    `include_planned=True` counts a unit as active once its `effective_date`
    (= install_date, falling back to planned commissioning date) precedes
    `plot_date`. With it False (default) only commissioned units count, so
    historical frames are unaffected. Mirrors aggregate_bess_by_unit.
    """
    import geopandas as gpd

    admin_units = _ensure_wgs84(admin_units)
    ts = pd.Timestamp(plot_date)
    date_col = (
        "effective_date"
        if include_planned and "effective_date" in records.columns
        else "install_date"
    )
    sub = records[
        (records["energy_type"] == energy_type)
        & (records[date_col] <= ts)
        & (records["removal_date"].isna() | (records["removal_date"] > ts))
    ].copy()
    if sub.empty:
        admin_units = admin_units.copy()
        admin_units["power_gw"] = 0.0
        return admin_units, sub

    has_coords = sub["latitude"].notna() & sub["longitude"].notna()
    pwr_by_idx = pd.Series(0.0, index=admin_units.index, dtype=float)

    if has_coords.any():
        geo_sub = sub[has_coords]
        geom = gpd.points_from_xy(geo_sub["longitude"], geo_sub["latitude"], crs="EPSG:4326")
        pts = gpd.GeoDataFrame(geo_sub, geometry=geom, crs="EPSG:4326")
        joined = gpd.sjoin(pts, admin_units[["geometry"]], predicate="within", how="left")
        pwr_by_idx = pwr_by_idx.add(
            joined.groupby("index_right")["power"].sum() / 1e6, fill_value=0.0
        )

    if (~has_coords).any() and "landkreis_norm" in sub.columns:
        anon = sub[~has_coords]
        key = normalise_kreis_series(admin_units["name"])
        name_to_idx = pd.Series(admin_units.index, index=key.values)
        name_to_idx = name_to_idx[~name_to_idx.index.duplicated(keep="first")]
        anon_idx = anon["landkreis_norm"].map(name_to_idx)
        # AGS5 override: when a row carries MaStR `municipality_key`, use the
        # first 5 digits (canonical Kreisschlüssel) to disambiguate Stadt vs.
        # Landkreis (e.g. 09262 → Passau Stadt, 09275 → Passau Landkreis).
        if "municipality_key" in anon.columns:
            ags5_to_idx = {
                KREIS_NAME_TO_AGS5[n]: i
                for i, n in admin_units["name"].items()
                if n in KREIS_NAME_TO_AGS5
            }
            # Use `.str[:5]` directly so NaN municipality_key stays NaN
            # (instead of being coerced to the literal string "nan", which
            # would never match an AGS5 key and silently bypass the override
            # without surfacing the gap).
            anon_ags5 = anon["municipality_key"].str[:5]
            ags_override = anon_ags5.map(ags5_to_idx)
            anon_idx = ags_override.combine_first(anon_idx)
        pwr_name = anon.groupby(anon_idx)["power"].sum() / 1e6
        pwr_by_idx = pwr_by_idx.add(pwr_name, fill_value=0.0)

    out = admin_units.copy()
    out["power_gw"] = pwr_by_idx.reindex(out.index).fillna(0.0)
    return out, sub


def plot_choropleth(
    aggregated,
    plot_date: date,
    title: str,
    bins: list[float] | None = None,
    cmap: str = "viridis",
    scale: str = "jenks",
    col: str = "power_gw",
    legend_label: str = "Capacity [GW]",
):
    """Return a matplotlib Figure showing the choropleth.

    `scale` selects the color-normalization strategy:
      * ``"jenks"`` (default) — discrete Jenks-natural-breaks classes. Best for
        skewed distributions; legend reads as bands.
      * ``"linear"`` — continuous linear scale from 0 → max. Clearer absolute
        comparison; less detail at the bottom of skewed distributions.
      * ``"log"``   — symmetric-log normalization. Useful when capacity spans
        several orders of magnitude (e.g. balcony PV vs. utility parks).
    """
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    fig, ax = plt.subplots(figsize=(9, 11), dpi=120)
    values = aggregated[col].to_numpy()
    vmax = float(values.max()) if values.size else 1.0
    if vmax <= 0:
        vmax = 1.0

    if scale == "linear":
        norm = mcolors.Normalize(vmin=0.0, vmax=vmax)
        legend_kwds = {"label": legend_label, "shrink": 0.6}
    elif scale == "log":
        # symlog handles zeros gracefully without raising
        linthresh = max(vmax * 1e-3, 1e-4)
        norm = mcolors.SymLogNorm(linthresh=linthresh, vmin=0.0, vmax=vmax)
        legend_kwds = {"label": f"{legend_label} (log)", "shrink": 0.6}
    else:  # jenks
        if bins is None:
            bins = jenks_bins(values[values > 0]) if (values > 0).any() else [0.0, vmax]
        if len(bins) < 2:
            bins = [0.0, vmax]
        norm = mcolors.BoundaryNorm(bins, ncolors=256)
        legend_kwds = {"label": legend_label, "shrink": 0.6}

    aggregated.plot(
        column=col, ax=ax, cmap=cmap, norm=norm,
        edgecolor="white", linewidth=0.3, legend=True,
        legend_kwds=legend_kwds,
        missing_kwds={"color": "#eeeeee"},
    )
    aggregated.dissolve().boundary.plot(ax=ax, color="black", linewidth=0.8, zorder=5)
    ax.set_axis_off()
    # `title` is used verbatim — callers control where the date appears.
    ax.set_title(title, fontsize=13)
    fig.tight_layout()
    return fig
