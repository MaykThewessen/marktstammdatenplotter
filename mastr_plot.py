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


def load_records(data_dir: Path | None = None, demo_if_missing: bool = True) -> tuple[pd.DataFrame, bool]:
    """Load records as a DataFrame.

    Returns (df, is_demo). When `is_demo` is True the rows are synthetic.
    """
    chosen = Path(data_dir) if data_dir else find_data_dir()
    if chosen is None:
        if not demo_if_missing:
            raise FileNotFoundError("No data-*.json files found in any default location.")
        return _synthetic_records(), True

    records = mastr_parser.load_data(str(chosen))
    df = pd.DataFrame([r.__dict__ for r in records])
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
):
    """Return a matplotlib Figure showing the choropleth."""
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    fig, ax = plt.subplots(figsize=(9, 11), dpi=120)
    values = aggregated["power_gw"].to_numpy()
    if bins is None:
        bins = jenks_bins(values[values > 0]) if (values > 0).any() else [0.0, 1.0]
    if len(bins) < 2:
        bins = [0.0, max(1.0, float(values.max() or 1.0))]
    norm = mcolors.BoundaryNorm(bins, ncolors=256)
    aggregated.plot(
        column="power_gw", ax=ax, cmap=cmap, norm=norm,
        edgecolor="white", linewidth=0.3, legend=True,
        legend_kwds={"label": "Capacity [GW]", "shrink": 0.6},
        missing_kwds={"color": "#eeeeee"},
    )
    ax.set_axis_off()
    ax.set_title(f"{title}\n{plot_date.isoformat()}", fontsize=13)
    fig.tight_layout()
    return fig
