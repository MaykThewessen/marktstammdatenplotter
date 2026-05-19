"""Build the bulk-data downloads that ship with the docs site.

Outputs (all under docs/data/):
    mastr-snapshot.parquet       Full cleaned per-plant table, zstd-compressed
    mastr-snapshot.csv.gz        Same data, gzip-compressed CSV
    mastr-by-kreis.csv.gz        Per-Kreis × energy_type roll-up (active at snap)

Owner names are *kept* — they're public information in the registry — but
trim them out manually if redistributing.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sys
from pathlib import Path

import pandas as pd
import geopandas as gpd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mastr_plot  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
SNAP = pd.Timestamp("2026-05-01")

KEEP_COLS = [
    "id", "energy_type", "power", "install_date", "removal_date",
    "longitude", "latitude", "off_shore", "installation_type", "building_type",
    "facing", "tilt", "postal_code", "is_private",
]
STRING_COLS = ("facing", "tilt", "off_shore", "installation_type", "building_type")


def gzip_in_place(src: Path) -> None:
    dst = src.with_suffix(src.suffix + ".gz")
    with src.open("rb") as fin, gzip.open(dst, "wb", compresslevel=9) as fout:
        shutil.copyfileobj(fin, fout)
    src.unlink()


def build_snapshot(df, units):
    out = df[KEEP_COLS].copy().rename(columns={"power": "power_kw"})
    for c in STRING_COLS:
        out[c] = out[c].astype("string")

    pts = gpd.GeoDataFrame(
        out.reset_index(drop=True),
        geometry=gpd.points_from_xy(out["longitude"], out["latitude"], crs="EPSG:4326"),
    )
    joined = gpd.sjoin(pts, units[["name", "bundesland", "geometry"]], predicate="within", how="left")
    joined = joined.loc[~joined.index.duplicated(keep="first")]
    out["kreis"] = joined["name"].reindex(out.reset_index(drop=True).index).astype("string").values
    out["bundesland"] = joined["bundesland"].reindex(out.reset_index(drop=True).index).astype("string").values
    out["active_at_snap"] = (
        (out["install_date"].isna() | (out["install_date"] <= SNAP))
        & (out["removal_date"].isna() | (out["removal_date"] > SNAP))
    )
    return out


def build_summary(out):
    return (
        out[out["active_at_snap"]]
        .groupby(["kreis", "bundesland", "energy_type"])
        .agg(plants=("id", "size"),
             mw=("power_kw", lambda s: round(s.sum() / 1e3, 2)))
        .reset_index()
    )


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    df, demo = mastr_plot.load_records()
    if demo:
        print("WARNING: building downloads from demo data.")
    units, _ = mastr_plot.load_admin_units()

    snap = build_snapshot(df, units)
    snap.to_parquet(OUT / "mastr-snapshot.parquet", compression="zstd", index=False)

    csv_path = OUT / "mastr-snapshot.csv"
    snap.to_csv(csv_path, index=False)
    gzip_in_place(csv_path)

    summary = build_summary(snap)
    summary_path = OUT / "mastr-by-kreis.csv"
    summary.to_csv(summary_path, index=False)
    gzip_in_place(summary_path)

    for f in sorted(OUT.iterdir()):
        size_mb = f.stat().st_size / 1e6
        print(f"  {f.name}  {size_mb:.2f} MB")


if __name__ == "__main__":
    main()
