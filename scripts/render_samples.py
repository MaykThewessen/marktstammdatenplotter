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


def _normalize_op(s):
    return (
        s.fillna("—").astype(str)
         .str.replace(r"\s+", " ", regex=True).str.strip()
         .str.replace(r"(?i)\s*(GmbH|Co\. KG|＆|&)\s*", " ", regex=True)
         .str.replace(r"\s+", " ", regex=True).str.strip()
    )


def render_top_operators(records, out_name, top_n: int = 30):
    sub = records[records["energy_type"].isin(["Wind", "Solare Strahlungsenergie"])]
    active = sub[
        (sub["install_date"] <= SNAP)
        & (sub["removal_date"].isna() | (sub["removal_date"] > SNAP))
    ].copy()
    active["op"] = _normalize_op(active["owner_name"])
    active["tech"] = active["energy_type"].map(
        {"Wind": "Wind", "Solare Strahlungsenergie": "PV"}
    )
    active = active[
        ~active["op"].str.fullmatch(
            r"(—|n\.?\s*v\.?|n/?a|privat(personen)?)", case=False, na=False
        )
    ]
    agg = (
        active.groupby(["op", "tech"])["power"].sum()
              .div(1e6).unstack(fill_value=0.0)
    )
    for c in ("Wind", "PV"):
        if c not in agg.columns:
            agg[c] = 0.0
    agg["total"] = agg["Wind"] + agg["PV"]
    top = agg.nlargest(top_n, "total").sort_values("total")

    fig, ax = plt.subplots(figsize=(11, 9), dpi=120)
    y = range(len(top))
    ax.barh(y, top["Wind"], color="#0ea5e9", label="Wind")
    ax.barh(y, top["PV"], left=top["Wind"], color="#f59e0b", label="PV (≥200 kW)")
    ax.set_yticks(list(y))
    ax.set_yticklabels([n[:55] for n in top.index], fontsize=9)
    ax.set_xlabel("Installed capacity [GW]")
    ax.set_title(
        f"Top {top_n} operators by installed wind + PV capacity\n"
        f"MaStR · snapshot {SNAP.date()}"
    )
    ax.legend(loc="lower right")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_offshore_chart(records, out_name, top_n: int = 25):
    import matplotlib.patches as mpatches

    offshore = records[
        (records["energy_type"] == "Wind")
        & records["off_shore"].notna()
        & (records["install_date"] <= SNAP)
        & (records["removal_date"].isna() | (records["removal_date"] > SNAP))
    ].copy()
    park = (
        offshore.groupby("owner_name")
                .agg(
                    n=("id", "size"),
                    gw=("power", lambda s: s.sum() / 1e6),
                    sea=("off_shore", "first"),
                    install=("install_date", "min"),
                )
                .reset_index()
                .sort_values("gw", ascending=False)
    )
    top_parks = park.head(top_n).iloc[::-1]
    colors = top_parks["sea"].map({"Nordsee": "#0ea5e9", "Ostsee": "#a78bfa"})

    fig, ax = plt.subplots(figsize=(11, 9), dpi=120)
    y = range(len(top_parks))
    ax.barh(y, top_parks["gw"], color=colors, edgecolor="#1e293b", linewidth=0.4)
    for i, (gw, n, ins) in enumerate(
        zip(top_parks["gw"], top_parks["n"], top_parks["install"])
    ):
        yr = ins.year if pd.notna(ins) else "—"
        ax.text(gw + 0.005, i, f"{n} turbines · {yr}",
                va="center", fontsize=8, color="#475569")
    ax.set_yticks(list(y))
    ax.set_yticklabels([n[:55] for n in top_parks["owner_name"]], fontsize=9)
    ax.set_xlabel("Installed capacity [GW]")
    ax.set_title(
        f"Top {top_n} offshore wind operators · Nordsee + Ostsee\n"
        f"MaStR · snapshot {SNAP.date()}"
    )
    ax.legend(handles=[
        mpatches.Patch(color="#0ea5e9", label="Nordsee"),
        mpatches.Patch(color="#a78bfa", label="Ostsee"),
    ], loc="lower right")
    ax.grid(axis="x", alpha=0.3)
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
    render_top_operators(records, "sample-top-operators.svg")
    render_offshore_chart(records, "sample-offshore-windparks.svg")
    print("Sample renders complete.")


if __name__ == "__main__":
    main()
