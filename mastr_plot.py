"""Shared helpers for the marimo notebooks.

Provides:
  * load_records()        — read cached MaStR JSON, fall back to synthetic demo
  * load_admin_units()    — read germany_kreise.gpkg, fall back to a coarse demo
  * plot_choropleth()     — render a choropleth for a given date / energy_type
  * jenks_bins()          — Jenks natural-breaks bin edges
"""

from __future__ import annotations

import os
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


def find_data_dir() -> Path | None:
    for candidate in DEFAULT_DATA_DIRS:
        if candidate.exists() and any(candidate.glob("*.json")):
            return candidate
    return None


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
                 prefer_bulk: bool = True) -> tuple[pd.DataFrame, bool]:
    """Load records as a DataFrame.

    Returns (df, is_demo). When `is_demo` is True the rows are synthetic.

    If `prefer_bulk=True` (default) and an open-MaStR bulk parquet dump is
    on disk (BNetzA_MaStR/solar.parquet + wind.parquet), it is preferred
    over the JSON-API scrape — covers the full ~ 5 M PV registry instead
    of the top-200 k slice. Falls back to the JSON scrape automatically.

    If `data_dir` is None, all default dirs that hold *.json scrapes are merged
    so a single DataFrame contains wind + PV when both have been scraped.
    """
    if prefer_bulk and data_dir is None and find_bulk_dir() is not None:
        bulk_dir = find_bulk_dir()
        frames = []
        for tech in ("pv", "wind"):
            path = bulk_dir / {"pv": "solar.parquet", "wind": "wind.parquet"}[tech]
            if path.exists():
                df, _ = load_from_bulk(tech, bulk_dir)
                frames.append(df)
        if frames:
            df = pd.concat(frames, ignore_index=True)
            # Drop tz so downstream snap-date comparisons work.
            for col in ("install_date", "removal_date"):
                if col in df.columns and hasattr(df[col].dtype, "tz") and df[col].dtype.tz is not None:
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
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
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
    gdf["geometry"] = gdf["geometry"].simplify(0.005, preserve_topology=True)
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


def load_bess(data_dir: Path | None = None, demo_if_missing: bool = False,
              prefer_bulk: bool = True) -> tuple[pd.DataFrame, bool]:
    """Load BESS records as a tidy DataFrame.

    Returns (df, is_demo). With demo_if_missing=False (default) a missing
    scrape raises FileNotFoundError — the BESS-specific synthetic dataset
    is not implemented yet because the per-Kreis names are pre-joined in
    the source JSON and the demo data wouldn't reflect that.

    If `prefer_bulk=True` (default) and BNetzA_MaStR/storage.parquet is on
    disk, that 1.75 M-row full registry slice is used instead of the
    top-200k JSON scrape.
    """
    if prefer_bulk and data_dir is None and find_bulk_dir() is not None:
        bulk_dir = find_bulk_dir()
        if (bulk_dir / "storage.parquet").exists():
            df, _ = load_from_bulk("bess", bulk_dir)
            return df, False

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
    df["sector"] = df["energy_kwh"].apply(bess_sector)
    df["is_battery"] = df["storage_tech"] == "Batterie"
    df["is_psh"] = df["storage_tech"] == "Pumpspeicher"
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
    import re
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    s = str(s).strip()
    # Strip prefixes
    s = re.sub(r"^(Stadt|Landkreis|Kreis|LK)\s+", "", s, flags=re.IGNORECASE)
    # Strip suffixes
    s = re.sub(r"\s+\([^)]+\)$", "", s)
    s = re.sub(r"\s+St[aä]dte$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+-?Kreis$", "", s, flags=re.IGNORECASE)
    # Collapse whitespace, lowercase, ASCII-fold the umlauts so encoding
    # quirks don't break the join.
    umlaut = {"ä": "a", "ö": "o", "ü": "u", "ß": "ss",
              "Ä": "a", "Ö": "o", "Ü": "u"}
    for k, v in umlaut.items():
        s = s.replace(k, v)
    s = s.lower()
    # Strip spaces adjacent to periods so Bavarian abbreviations like
    # "neumarkt i.d. opf." and "neumarkt i.d.opf." converge.
    s = re.sub(r"\s*\.\s*", ".", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def load_from_bulk(
    tech: str,
    bulk_dir: Path | None = None,
) -> tuple[pd.DataFrame, bool]:
    """Load an open-MaStR Zenodo parquet snapshot for a single technology.

    Returns (df, is_bulk). DataFrame matches the column schema used by the
    existing JSON-scrape loaders (`load_records`, `load_bess`) so downstream
    rendering works unchanged. Specifically: install_date, removal_date,
    power, power_kw, energy_kwh, longitude, latitude, landkreis, bundesland,
    energy_type, off_shore, owner_name.

    `tech` ∈ {"pv", "wind", "bess"}.
    """
    chosen = Path(bulk_dir) if bulk_dir else find_bulk_dir()
    if chosen is None:
        raise FileNotFoundError(
            "No open-MaStR bulk dump found. Expected solar.parquet / wind.parquet"
            " / storage.parquet under BNetzA_MaStR/."
        )

    # Prefer the merged historical+delta `full-*.parquet` when present;
    # fall back to the bare Zenodo-snapshot file otherwise.
    if tech == "pv":
        path = chosen / "full-solar.parquet"
        if not path.exists():
            path = chosen / "solar.parquet"
        energy_type = "Solare Strahlungsenergie"
    elif tech == "wind":
        path = chosen / "full-wind.parquet"
        if not path.exists():
            path = chosen / "wind.parquet"
        energy_type = "Wind"
    elif tech == "bess":
        path = chosen / "full-storage.parquet"
        if not path.exists():
            path = chosen / "storage.parquet"
        energy_type = "Speicher"
    else:
        raise ValueError(f"unknown tech: {tech!r}")

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
        if col in df.columns:
            ts = df[col]
            if hasattr(ts.dtype, "tz") and ts.dtype.tz is not None:
                df[col] = ts.dt.tz_localize(None)

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
            df["is_private"] = (df.get("usage_type") == "Haushalt") if "usage_type" in df.columns else False

    # Add a normalised landkreis column for downstream name-based joins
    # (saves re-running normalise_kreis_name() at every aggregation step).
    if "landkreis" in df.columns:
        df["landkreis_norm"] = df["landkreis"].apply(normalise_kreis_name)

    # off_shore label for wind compatibility with the JSON-scrape schema.
    if tech == "wind" and "location_type" in df.columns:
        df["off_shore"] = df["location_type"].map(
            {"Windkraft auf See": "Nordsee"}
        )

    # BESS-style helper columns so aggregate_bess_by_unit / sector / etc.
    # all work against the bulk loader output.
    if tech == "bess":
        if "energy_kwh" in df.columns and "power_kw" in df.columns:
            df["duration_h"] = df["energy_kwh"] / df["power_kw"].replace(0, np.nan)
        if "energy_kwh" in df.columns:
            df["sector"] = df["energy_kwh"].apply(bess_sector)
        if "storage_tech" in df.columns:
            df["is_battery"] = df["storage_tech"] == "Batterie"
            df["is_psh"] = df["storage_tech"] == "Pumpspeicher"

    if "install_date" in df.columns:
        df["effective_date"] = df.get("install_date")
        if "planned_date" in df.columns:
            df["effective_date"] = df["install_date"].fillna(df["planned_date"])

    return df, True


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
    key = out["name"].apply(normalise_kreis_name)
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
        key = admin_units["name"].apply(normalise_kreis_name)
        name_to_idx = pd.Series(admin_units.index, index=key.values)
        anon_idx = anon["landkreis_norm"].map(name_to_idx)
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
):
    """Spatial-join active plants to admin units and sum power (GW)."""
    import geopandas as gpd
    from shapely.geometry import Point

    ts = pd.Timestamp(plot_date)
    sub = records[
        (records["energy_type"] == energy_type)
        & (records["install_date"] <= ts)
        & (records["removal_date"].isna() | (records["removal_date"] > ts))
    ].copy()
    if sub.empty:
        admin_units = admin_units.copy()
        admin_units["power_gw"] = 0.0
        return admin_units, sub

    geom = gpd.points_from_xy(sub["longitude"], sub["latitude"], crs="EPSG:4326")
    pts = gpd.GeoDataFrame(sub, geometry=geom, crs="EPSG:4326")
    joined = gpd.sjoin(pts, admin_units[["geometry"]], predicate="within", how="left")
    agg = joined.groupby("index_right")["power"].sum() / 1e6  # kW → GW
    out = admin_units.copy()
    out["power_gw"] = out.index.map(agg).astype(float).fillna(0.0)
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
