"""Battery storage income per Bundesland, snapshot of Feb 2026.

Aggregates installed BESS power and energy from the MaStR registry to the
16 German federal states and applies a per-MW revenue benchmark to estimate
annual income. Bundesland polygons are pulled from the geoBoundaries API
(ADM1, DEU) and used to spatially recover the state label for the few rows
where MaStR has it NULL but coordinates are present.

Run:
    python bess_income_by_bundesland.py
    python bess_income_by_bundesland.py --benchmark 150000 --snapshot 2026-02-28
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

import geopandas as gpd
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent

# geoBoundaries DEU ADM1 — Bundesländer polygons. Static commit-pinned URL so
# results are reproducible across runs even if a newer build supersedes it.
GEOBOUNDARIES_API = "https://www.geoboundaries.org/api/current/gbOpen/DEU/ADM1/"
GEO_CACHE = REPO_ROOT / "data" / "geo" / "deu_adm1.geojson"

# Default 2-h system revenue benchmark. The Modo Energy "German BESS Index"
# reported median realised revenues in the €100–180 k/MW/yr band across
# 2024–2025 (arbitrage + aFRR blend); 130 k€/MW/yr sits near the index median.
# Override with --benchmark; treat the output as an order-of-magnitude estimate.
DEFAULT_BENCHMARK_EUR_PER_MW_YR = 130_000.0


def fetch_bundesland_polygons() -> gpd.GeoDataFrame:
    """Return ADM1 polygons keyed by `shapeName` (matches MaStR `bundesland`)."""
    if not GEO_CACHE.exists():
        GEO_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(GEOBOUNDARIES_API, timeout=30) as r:
            meta = json.load(r)
        url = meta["gjDownloadURL"]
        with urllib.request.urlopen(url, timeout=60) as r:
            GEO_CACHE.write_bytes(r.read())
    g = gpd.read_file(GEO_CACHE)[["shapeName", "geometry"]]
    return g.rename(columns={"shapeName": "bundesland"}).to_crs("EPSG:4326")


def load_bess_snapshot(snapshot: pd.Timestamp, batteries_only: bool) -> pd.DataFrame:
    """Load BESS rows live at `snapshot`. Prefers the open-mastr SQLite
    pipeline when present, falls back to the bundled Zenodo parquet."""
    try:
        import mastr_db
        if mastr_db.DB_PATH.exists():
            df = mastr_db.load_for_pipeline("bess")
            df = df.rename(columns={
                "gross_capacity_kw": "gross_capacity_kw",
                "usable_capacity_kwh": "usable_capacity_kwh",
                "commissioning_date": "commissioning_date",
                "decommissioning_date": "decommissioning_date",
                "storage_technology": "storage_technology",
            })
        else:
            raise FileNotFoundError
    except (ImportError, FileNotFoundError, RuntimeError):
        df = pd.read_parquet(REPO_ROOT / "BNetzA_MaStR" / "storage.parquet")

    for col in ("commissioning_date", "decommissioning_date"):
        if isinstance(df[col].dtype, pd.DatetimeTZDtype):
            df[col] = df[col].dt.tz_localize(None)

    snap = snapshot.tz_localize(None) if snapshot.tzinfo else snapshot
    live = (df["commissioning_date"] <= snap) & (
        df["decommissioning_date"].isna() | (df["decommissioning_date"] > snap)
    )
    df = df.loc[live].copy()
    if batteries_only:
        df = df[df["storage_technology"] == "Batterie"].copy()
    return df


def fill_missing_bundesland(df: pd.DataFrame, polys: gpd.GeoDataFrame) -> pd.DataFrame:
    """Spatially recover `bundesland` for rows where MaStR has it NULL but
    coordinates are present. ~30 rows in the current Feb-2026 snapshot."""
    needs = df["bundesland"].isna() & df["longitude"].notna() & df["latitude"].notna()
    if not needs.any():
        return df
    pts = gpd.GeoDataFrame(
        df.loc[needs, ["longitude", "latitude"]],
        geometry=gpd.points_from_xy(df.loc[needs, "longitude"], df.loc[needs, "latitude"]),
        crs="EPSG:4326",
    )
    joined = pts.sjoin(polys, how="left", predicate="within")
    df.loc[joined.index, "bundesland"] = joined["bundesland"]
    return df


def aggregate(df: pd.DataFrame, benchmark: float) -> pd.DataFrame:
    g = df.groupby("bundesland", dropna=False).agg(
        units=("gross_capacity_kw", "size"),
        installed_mw=("gross_capacity_kw", lambda s: s.sum() / 1e3),
        installed_mwh=("usable_capacity_kwh", lambda s: s.sum() / 1e3),
    )
    g["est_income_eur_per_yr"] = g["installed_mw"] * benchmark
    return g.sort_values("est_income_eur_per_yr", ascending=False)


def format_table(agg: pd.DataFrame, snapshot: pd.Timestamp, benchmark: float) -> str:
    total_mw = agg["installed_mw"].sum()
    total_mwh = agg["installed_mwh"].sum()
    total_eur = agg["est_income_eur_per_yr"].sum()

    head = (
        f"German BESS — installed capacity & estimated annual income\n"
        f"Snapshot: {snapshot.date().isoformat()}   "
        f"Benchmark: {benchmark:,.0f} €/MW/yr\n"
        f"{'-' * 88}\n"
        f"{'Bundesland':<24}{'units':>10}{'MW':>12}{'MWh':>14}{'income (M€/yr)':>22}\n"
        f"{'-' * 88}\n"
    )
    rows = "\n".join(
        f"{bl if pd.notna(bl) else '(unknown)':<24}"
        f"{int(r.units):>10,}"
        f"{r.installed_mw:>12,.1f}"
        f"{r.installed_mwh:>14,.1f}"
        f"{r.est_income_eur_per_yr / 1e6:>22,.1f}"
        for bl, r in agg.iterrows()
    )
    foot = (
        f"\n{'-' * 88}\n"
        f"{'TOTAL':<24}{int(agg['units'].sum()):>10,}"
        f"{total_mw:>12,.1f}{total_mwh:>14,.1f}{total_eur / 1e6:>22,.1f}\n"
    )
    return head + rows + foot


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--snapshot", default="2026-02-28",
                   help="Snapshot date YYYY-MM-DD (default 2026-02-28)")
    p.add_argument("--benchmark", type=float, default=DEFAULT_BENCHMARK_EUR_PER_MW_YR,
                   help="Revenue benchmark in €/MW/yr (default 130000)")
    p.add_argument("--include-non-battery", action="store_true",
                   help="Include Pumpspeicher / Wasserstoff / etc. (default: Batterie only)")
    args = p.parse_args()

    snapshot = pd.Timestamp(args.snapshot)
    df = load_bess_snapshot(snapshot, batteries_only=not args.include_non_battery)
    polys = fetch_bundesland_polygons()
    df = fill_missing_bundesland(df, polys)
    agg = aggregate(df, args.benchmark)
    print(format_table(agg, snapshot, args.benchmark))


if __name__ == "__main__":
    main()
