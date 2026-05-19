"""Rebuild docs/assets/kreise.json — the per-Kreis dataset behind the sortable
table in the docs site.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import geopandas as gpd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mastr_plot  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "assets" / "kreise.json"
SNAP = pd.Timestamp("2026-05-01")


def per_kreis(df_sub, units, label):
    active = df_sub[
        (df_sub["install_date"] <= SNAP)
        & (df_sub["removal_date"].isna() | (df_sub["removal_date"] > SNAP))
    ]
    pts = gpd.GeoDataFrame(
        active.copy(),
        geometry=gpd.points_from_xy(active["longitude"], active["latitude"], crs="EPSG:4326"),
    )
    j = gpd.sjoin(pts, units[["name", "bundesland", "geometry"]], predicate="within", how="left")
    agg = j.groupby(["name", "bundesland"]).agg(
        power_gw=("power", lambda s: s.sum() / 1e6),
        count=("power", "size"),
    ).reset_index()
    agg["tech"] = label
    return agg


def main():
    df, _ = mastr_plot.load_records()
    units, _ = mastr_plot.load_admin_units()

    wind = per_kreis(df[df["energy_type"] == "Wind"], units, "wind")
    pv = per_kreis(df[df["energy_type"] == "Solare Strahlungsenergie"], units, "pv_top50k")

    w = wind.rename(columns={"power_gw": "wind_gw", "count": "wind_n"}).drop(columns="tech")
    p = pv.rename(columns={"power_gw": "pv_gw", "count": "pv_n"}).drop(columns="tech")
    combined = pd.merge(w, p, on=["name", "bundesland"], how="outer").fillna(0.0)
    combined["total_gw"] = combined["wind_gw"] + combined["pv_gw"]
    combined = combined.sort_values("total_gw", ascending=False)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"snapshot": SNAP.date().isoformat(), "kreise": combined.to_dict(orient="records")}
    OUT.write_text(json.dumps(payload))
    print(f"Wrote {OUT.relative_to(ROOT)} — {len(combined)} Kreise")


if __name__ == "__main__":
    main()
