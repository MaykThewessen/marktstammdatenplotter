"""Re-render the sample SVGs that ship with the docs site.

Outputs (idempotent):
    fig/sample-pv-map.svg
    fig/sample-pv-growth.svg
    fig/sample-wind-map.svg
    fig/sample-wind-growth.svg
    fig/sample-by-bundesland.svg
and mirrors them into docs/assets/.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import geopandas as gpd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mastr_plot  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIG = ROOT / "fig"
DOCS = ROOT / "docs" / "assets"
SNAP = pd.Timestamp("2025-01-01")


def render_map(records, units, energy_type, title_prefix, cmap, out_name):
    agg, active = mastr_plot.aggregate_by_unit(records, units, SNAP.date(), energy_type)
    positive = agg["power_gw"][agg["power_gw"] > 0].to_numpy()
    bins = mastr_plot.jenks_bins(positive, k=7)
    fig = mastr_plot.plot_choropleth(
        agg, SNAP.date(),
        f"{title_prefix} — {len(active):,} plants · {round(agg['power_gw'].sum(),1)} GW",
        bins=bins, cmap=cmap,
    )
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_growth(records, energy_type, label, color_fill, color_line, out_name):
    sub = records[records["energy_type"] == energy_type].copy()
    monthly = (
        sub.assign(month=sub["install_date"].dt.to_period("M"))
           .groupby("month")["power"].sum().div(1e6).cumsum()
    )
    monthly.index = monthly.index.to_timestamp()
    fig, ax = plt.subplots(figsize=(9, 3.5), dpi=120)
    ax.fill_between(monthly.index, monthly.values, color=color_fill, alpha=0.35)
    ax.plot(monthly.index, monthly.values, color=color_line, linewidth=2)
    ax.set_ylabel(f"Cumulative {label} [GW]")
    ax.set_title(f"Cumulative {label} capacity over time")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_bundesland_chart(records, units, out_name):
    def state_totals(sub):
        active = sub[
            (sub["install_date"] <= SNAP)
            & (sub["removal_date"].isna() | (sub["removal_date"] > SNAP))
        ]
        pts = gpd.GeoDataFrame(
            active.copy(),
            geometry=gpd.points_from_xy(active["longitude"], active["latitude"], crs="EPSG:4326"),
        )
        j = gpd.sjoin(pts, units[["bundesland", "geometry"]], predicate="within", how="left")
        return j.groupby("bundesland")["power"].sum().div(1e6)

    wind_s = state_totals(records[records["energy_type"] == "Wind"]).rename("Wind")
    pv_s = state_totals(records[records["energy_type"] == "Solare Strahlungsenergie"]).rename("PV (≥200 kW)")
    combined = pd.concat([wind_s, pv_s], axis=1).fillna(0.0).sort_values("Wind")
    fig, ax = plt.subplots(figsize=(10, 6), dpi=120)
    y = range(len(combined))
    ax.barh(y, combined["Wind"], color="#0ea5e9", label="Wind")
    ax.barh(y, combined["PV (≥200 kW)"], left=combined["Wind"], color="#f59e0b", label="PV (≥200 kW)")
    ax.set_yticks(list(y))
    ax.set_yticklabels(combined.index)
    ax.set_xlabel("Installed capacity [GW]")
    ax.set_title(
        f"Installed capacity by Bundesland — {SNAP.date()}\n"
        "(MaStR · wind: full · PV: top-50k ≥200 kW)"
    )
    ax.legend(loc="lower right")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


def main():
    records, demo = mastr_plot.load_records()
    if demo:
        print("WARNING: no MaStR scrape on disk — rendering demo data.")
    units, demo_units = mastr_plot.load_admin_units()
    if demo_units:
        print("WARNING: no germany_kreise.gpkg — rendering demo polygons.")

    render_map(
        records, units, "Solare Strahlungsenergie",
        title_prefix="PV ≥200 kW",
        cmap="YlOrRd",
        out_name="sample-pv-map.svg",
    )
    render_map(
        records, units, "Wind",
        title_prefix="Wind",
        cmap="GnBu",
        out_name="sample-wind-map.svg",
    )
    render_growth(
        records, "Solare Strahlungsenergie",
        label="PV (top 50k)",
        color_fill="#f59e0b", color_line="#b45309",
        out_name="sample-pv-growth.svg",
    )
    render_growth(
        records, "Wind",
        label="wind",
        color_fill="#86efac", color_line="#16a34a",
        out_name="sample-wind-growth.svg",
    )
    render_bundesland_chart(records, units, "sample-by-bundesland.svg")
    print("Sample renders complete.")


if __name__ == "__main__":
    main()
