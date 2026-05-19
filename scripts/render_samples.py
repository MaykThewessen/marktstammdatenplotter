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
SNAP = pd.Timestamp("2026-05-01")  # Aligned with the final frame of the animation GIFs


def render_map(records, units, energy_type, title_prefix, cmap, out_name):
    agg, active = mastr_plot.aggregate_by_unit(records, units, SNAP.date(), energy_type)
    positive = agg["power_gw"][agg["power_gw"] > 0].to_numpy()
    bins = mastr_plot.jenks_bins(positive, k=7)
    total_gw = active["power"].sum() / 1e6
    shown_gw = agg["power_gw"].sum()
    title = (
        f"{title_prefix} — {SNAP.date()}\n"
        f"{len(active):,} plants · {round(total_gw, 1)} GW"
    )
    if total_gw - shown_gw > 1.0:
        title += f"\n({round(shown_gw, 1)} GW shown on map · " \
                 f"{round(total_gw - shown_gw, 1)} GW offshore / out-of-Kreis)"
    fig = mastr_plot.plot_choropleth(agg, SNAP.date(), title, bins=bins, cmap=cmap)
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


def render_energy_mix(records, out_name):
    active = records[
        (records["install_date"] <= SNAP)
        & (records["removal_date"].isna() | (records["removal_date"] > SNAP))
    ]
    mix = (
        active.groupby("energy_type")["power"]
              .agg(gw=lambda s: s.sum() / 1e6, count="size")
              .sort_values("gw")
    )
    top_n = 8
    top = mix.tail(top_n).copy()
    rest = mix.iloc[:-top_n]
    if len(rest):
        top.loc["Andere (" + str(len(rest)) + " Typen)"] = [
            float(rest["gw"].sum()), int(rest["count"].sum()),
        ]
    top = top.sort_values("gw")

    colors_map = {
        "Wind": "#0ea5e9",
        "Solare Strahlungsenergie": "#f59e0b",
        "Biomasse": "#84cc16",
        "Wasser": "#06b6d4",
        "Erdgas": "#6b7280",
        "Steinkohle": "#1f2937",
        "Braunkohle": "#52525b",
        "Mineralölprodukte": "#7c2d12",
        "Kernenergie": "#a855f7",
        "Geothermie": "#dc2626",
    }
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=120)
    y = range(len(top))
    ax.barh(y, top["gw"], color=[colors_map.get(n, "#94a3b8") for n in top.index],
            edgecolor="#1e293b", linewidth=0.4)
    for i, (gw, n) in enumerate(zip(top["gw"], top["count"])):
        ax.text(gw + 0.5, i, f"{int(n):,} plants", va="center", fontsize=9, color="#475569")
    ax.set_yticks(list(y))
    ax.set_yticklabels(top.index)
    ax.set_xlabel("Installed capacity [GW]")
    ax.set_title(
        f"MaStR energy-type mix — installed capacity at {SNAP.date()}\n"
        f"(snapshot of {len(records):,} plants in this scrape · "
        f"PV slice is top 200 k)"
    )
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_yoy_additions(records, units, out_name, year: int = 2024):
    import matplotlib.colors as mcolors

    y0 = pd.Timestamp(f"{year}-01-01")
    y1 = pd.Timestamp(f"{year}-12-31")
    added = records[
        (records["install_date"] >= y0) & (records["install_date"] <= y1)
        & records["energy_type"].isin(["Wind", "Solare Strahlungsenergie"])
    ].copy()

    pts = gpd.GeoDataFrame(
        added,
        geometry=gpd.points_from_xy(added["longitude"], added["latitude"], crs="EPSG:4326"),
    )
    j = gpd.sjoin(pts, units[["name", "geometry"]], predicate="within", how="left")
    agg = j.groupby("index_right")["power"].sum() / 1e3  # kW → MW
    geo = units.copy()
    geo["mw_added"] = geo.index.map(agg).astype(float).fillna(0.0)

    bins = mastr_plot.jenks_bins(geo["mw_added"][geo["mw_added"] > 0].to_numpy(), k=7)
    if len(bins) < 2:
        bins = [0.0, max(1.0, float(geo["mw_added"].max() or 1.0))]
    norm = mcolors.BoundaryNorm(bins, ncolors=256)

    fig, ax = plt.subplots(figsize=(9, 11), dpi=120)
    geo.plot(
        column="mw_added", ax=ax, cmap="RdPu", norm=norm,
        edgecolor="white", linewidth=0.3, legend=True,
        legend_kwds={"label": f"New capacity {year} [MW]", "shrink": 0.6},
        missing_kwds={"color": "#eeeeee"},
    )
    ax.set_axis_off()
    ax.set_title(
        f"Capacity added during {year} per Kreis\n"
        f"(Wind + PV ≥49 kW from this scrape · {round(geo['mw_added'].sum() / 1e3, 1)} GW total)",
        fontsize=13,
    )
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_density_map(records, units, energy_type, cmap, out_name, title_prefix):
    import matplotlib.colors as mcolors

    units = units.copy()
    if "area_km2" not in units.columns:
        units["area_km2"] = units.to_crs(epsg=25832).geometry.area / 1e6
    agg, active = mastr_plot.aggregate_by_unit(records, units, SNAP.date(), energy_type)
    agg["mw_per_km2"] = agg["power_gw"] * 1000.0 / agg["area_km2"]
    positive = agg["mw_per_km2"][agg["mw_per_km2"] > 0].to_numpy()
    bins = mastr_plot.jenks_bins(positive, k=7)
    if len(bins) < 2:
        bins = [0.0, max(1.0, float(positive.max() or 1.0))]
    norm = mcolors.BoundaryNorm(bins, ncolors=256)

    fig, ax = plt.subplots(figsize=(9, 11), dpi=120)
    agg.plot(
        column="mw_per_km2", ax=ax, cmap=cmap, norm=norm,
        edgecolor="white", linewidth=0.3, legend=True,
        legend_kwds={"label": "Capacity density [MW/km²]", "shrink": 0.6},
        missing_kwds={"color": "#eeeeee"},
    )
    ax.set_axis_off()
    ax.set_title(
        f"{title_prefix} per km² — {SNAP.date()}\n"
        f"{len(active):,} active · {round(active['power'].sum() / 1e6, 1)} GW total",
        fontsize=13,
    )
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


def export_largest_plants(records, units, out_name, top_n: int = 10):
    """Write JSON listing the top-N largest single plants per technology."""
    import json

    def topn(et):
        sub = records[records["energy_type"] == et].copy()
        sub = sub.sort_values("power", ascending=False).head(top_n)
        if sub.empty:
            return sub
        pts = gpd.GeoDataFrame(
            sub.reset_index(drop=True),
            geometry=gpd.points_from_xy(sub["longitude"], sub["latitude"], crs="EPSG:4326"),
        )
        j = gpd.sjoin(pts, units[["name", "bundesland", "geometry"]],
                      predicate="within", how="left")
        j = j.loc[~j.index.duplicated(keep="first")]
        sub["kreis"] = j["name"].reindex(sub.reset_index(drop=True).index).values
        sub["bundesland"] = j["bundesland"].reindex(sub.reset_index(drop=True).index).values
        sub["mw"] = sub["power"] / 1000.0
        sub["install_year"] = pd.to_datetime(sub["install_date"]).dt.year
        return sub

    big_pv = topn("Solare Strahlungsenergie")
    big_wind = topn("Wind")
    records_out = []
    for tech, df_top in (("PV", big_pv), ("Wind", big_wind)):
        for _, r in df_top.iterrows():
            records_out.append({
                "tech": tech,
                "mw": round(float(r["mw"]), 2),
                "owner": str(r["owner_name"])[:80],
                "kreis": str(r["kreis"]) if pd.notna(r["kreis"]) else None,
                "bundesland": str(r["bundesland"]) if pd.notna(r["bundesland"]) else None,
                "install_year": int(r["install_year"]) if pd.notna(r["install_year"]) else None,
            })
    (DOCS / out_name).write_text(json.dumps(records_out, indent=2))


def render_pv_orientation(records, out_name):
    """Capacity-weighted distribution of facing + tilt across PV plants."""
    pv = records[records["energy_type"] == "Solare Strahlungsenergie"].copy()

    def label_facing(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "unknown"
        if isinstance(v, str):
            return v
        deg_map = {
            0: "N (0°)", 45: "NE (45°)", 90: "E (90°)", 135: "SE (135°)",
            180: "S (180°)", 225: "SW (225°)", 270: "W (270°)", 315: "NW (315°)",
        }
        try:
            return deg_map.get(int(v), "unknown")
        except (ValueError, TypeError):
            return "unknown"

    def label_tilt(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "unknown"
        if isinstance(v, str):
            return v
        if isinstance(v, tuple) and len(v) == 2:
            return f"{v[0]}-{v[1]}°"
        return str(v)

    pv["facing_lbl"] = pv["facing"].apply(label_facing)
    pv["tilt_lbl"] = pv["tilt"].apply(label_tilt)
    fac = pv.groupby("facing_lbl")["power"].sum().div(1e6).sort_values(ascending=False)
    til = pv.groupby("tilt_lbl")["power"].sum().div(1e6).sort_values(ascending=False)

    fig, axs = plt.subplots(1, 2, figsize=(13, 5.5), dpi=120)
    colors_facing = {
        "S (180°)": "#fde047", "SE (135°)": "#f97316", "SW (225°)": "#fb7185",
        "E (90°)": "#fbbf24", "W (270°)": "#a78bfa", "east-west": "#10b981",
        "tracked": "#0ea5e9", "NE (45°)": "#94a3b8", "NW (315°)": "#94a3b8",
        "N (0°)": "#cbd5e1",
    }
    fac_known = fac.drop("unknown", errors="ignore")
    pcols = [colors_facing.get(n, "#94a3b8") for n in fac_known.index]
    axs[0].pie(
        fac_known.values, labels=fac_known.index, colors=pcols,
        autopct="%1.0f%%", startangle=90,
        textprops={"fontsize": 9},
        wedgeprops={"edgecolor": "white", "linewidth": 1},
    )
    axs[0].set_title(
        f"PV capacity by panel orientation\n"
        f"({fac_known.sum():.1f} GW with known facing)"
    )

    til_known = til.drop("unknown", errors="ignore")
    axs[1].barh(range(len(til_known)), til_known.values,
                color="#0ea5e9", edgecolor="#1e293b", linewidth=0.4)
    axs[1].set_yticks(range(len(til_known)))
    axs[1].set_yticklabels(til_known.index)
    axs[1].set_xlabel("Installed capacity [GW]")
    axs[1].set_title("PV capacity by tilt angle")
    axs[1].grid(axis="x", alpha=0.3)
    fig.suptitle("Solar PV orientation analysis · top 200 k plants", fontsize=13)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_wind_age(records, out_name):
    """Installs vs. decommissions per year + turbine-MW upsizing trend."""
    wind = records[records["energy_type"] == "Wind"].copy()
    wind["install_year"] = pd.to_datetime(wind["install_date"]).dt.year
    wind["removed"] = wind["removal_date"].notna()
    wind["mw"] = wind["power"] / 1000.0

    installs = (
        wind.groupby("install_year")
            .agg(installed_n=("id", "size"), installed_mw=("mw", "sum"))
            .reset_index()
    )
    rem = wind[wind["removed"]].copy()
    rem["remove_year"] = pd.to_datetime(rem["removal_date"]).dt.year
    removals = (
        rem.groupby("remove_year")
           .agg(removed_n=("id", "size"), removed_mw=("mw", "sum"))
           .reset_index()
           .rename(columns={"remove_year": "install_year"})
    )
    merged = (
        pd.merge(installs, removals, on="install_year", how="outer")
          .fillna(0.0)
          .sort_values("install_year")
    )
    merged = merged[(merged["install_year"] >= 1990) & (merged["install_year"] <= 2025)]

    active = wind.dropna(subset=["install_year"])
    mean_mw = active.groupby("install_year")["mw"].mean()
    mean_mw = mean_mw[(mean_mw.index >= 1995) & (mean_mw.index <= 2025)]

    fig, axs = plt.subplots(2, 1, figsize=(11, 8), dpi=120, sharex=True)
    ax = axs[0]
    ax.bar(merged["install_year"], merged["installed_mw"],
           color="#0ea5e9", label="Installed [MW]")
    ax.bar(merged["install_year"], -merged["removed_mw"],
           color="#dc2626", label="Decommissioned [MW]")
    ax.axhline(0, color="#1e293b", linewidth=0.5)
    ax.set_ylabel("MW per year (+install / −removal)")
    ax.set_title("German wind fleet: installs vs. decommissions per year")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)

    ax = axs[1]
    ax.plot(mean_mw.index, mean_mw.values, color="#16a34a",
            linewidth=2, marker="o", markersize=4)
    ax.fill_between(mean_mw.index, mean_mw.values, alpha=0.25, color="#16a34a")
    ax.set_xlabel("Year")
    ax.set_ylabel("Mean rotor capacity [MW] (commission year)")
    ax.set_title("Turbine upsizing trend — rotor MW by commission year")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


BESS_SECTOR_COLORS = {
    "HSS (<30 kWh)": "#86efac",
    "CSS (30 kWh – 1 MWh)": "#fbbf24",
    "LSS (≥1 MWh)": "#a78bfa",
}


def render_bess_sector_charts(bess_df, out_summary: str, out_growth: str,
                              out_duration: str):
    """Three-sector (HSS / CSS / LSS) breakdown per battery-charts.de / BVES."""
    from matplotlib.patches import Patch

    snap = SNAP
    active = bess_df[
        (bess_df["install_date"] <= snap)
        & (bess_df["removal_date"].isna() | (bess_df["removal_date"] > snap))
    ].copy()

    # -- 1. summary bars: power / energy / count per sector -------------------
    agg = (
        active.groupby("sector", observed=False)
              .agg(n=("id", "size"),
                   gw=("power_kw", lambda s: s.sum() / 1e6),
                   gwh=("energy_kwh", lambda s: s.sum() / 1e6))
              .reindex(mastr_plot.BESS_SECTORS).fillna(0.0)
    )
    fig, axs = plt.subplots(1, 3, figsize=(14, 4.2), dpi=120)
    for ax, col, label, fmt in [
        (axs[0], "gw", "Installed power [GW]", "{:.2f}"),
        (axs[1], "gwh", "Usable energy [GWh]", "{:.1f}"),
        (axs[2], "n", "Unit count", "{:,}"),
    ]:
        x = range(len(agg))
        ax.bar(x, agg[col],
               color=[BESS_SECTOR_COLORS[s] for s in agg.index],
               edgecolor="#1e293b", linewidth=0.4)
        ax.set_xticks(list(x))
        ax.set_xticklabels(agg.index, rotation=15, ha="right")
        ax.set_ylabel(label)
        ax.grid(axis="y", alpha=0.3)
        ymax = agg[col].max() if agg[col].max() > 0 else 1
        for i, v in enumerate(agg[col]):
            ax.text(i, v + ymax * 0.02,
                    fmt.format(int(v) if col == "n" else v),
                    ha="center", fontsize=9)
        ax.set_title(label)
    fig.suptitle(
        f"BESS sector split — {snap.date()} "
        "(battery-charts.de convention · RWTH Aachen / Figgener et al.)",
        fontsize=13,
    )
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_summary, format="svg", bbox_inches="tight")
    plt.close(fig)

    # -- 2. cumulative power + energy stacked by sector -----------------------
    df_h = bess_df[
        bess_df["install_date"].notna()
        & (bess_df["install_date"] >= "2010-01-01")
    ].copy()
    df_h["month"] = df_h["install_date"].dt.to_period("M")

    def cum_pivot(col):
        piv = (
            df_h.groupby(["month", "sector"])[col]
                .sum().div(1e6).unstack(fill_value=0).cumsum()
        )
        piv = piv[[c for c in mastr_plot.BESS_SECTORS if c in piv.columns]]
        piv.index = piv.index.to_timestamp()
        return piv

    piv_p = cum_pivot("power_kw")
    piv_e = cum_pivot("energy_kwh")

    fig, axs = plt.subplots(1, 2, figsize=(14, 4.2), dpi=120)
    for ax, piv, ylabel, title in [
        (axs[0], piv_p, "Cumulative power [GW]",
         "Cumulative BESS power by sector"),
        (axs[1], piv_e, "Cumulative energy [GWh]",
         "Cumulative BESS energy by sector"),
    ]:
        ax.stackplot(
            piv.index, piv.T.values, labels=piv.columns,
            colors=[BESS_SECTOR_COLORS[c] for c in piv.columns],
            alpha=0.85, edgecolor="white", linewidth=0.4,
        )
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(alpha=0.3)
    fig.suptitle("Battery + storage build-out by sector — HSS / CSS / LSS",
                 fontsize=13)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_growth, format="svg", bbox_inches="tight")
    plt.close(fig)

    # -- 3. per-sector duration distribution ----------------------------------
    b = bess_df[
        (bess_df["storage_tech"] == "Batterie")
        & bess_df["install_date"].notna()
        & (bess_df["install_date"] <= snap)
        & bess_df["duration_h"].notna()
        & (bess_df["duration_h"] > 0)
        & (bess_df["duration_h"] < 24)
    ]
    fig, axs = plt.subplots(1, 3, figsize=(15, 4.2), dpi=120)
    for ax, sec in zip(axs, mastr_plot.BESS_SECTORS):
        sub = b[b["sector"] == sec]
        if len(sub) == 0:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center")
            ax.set_title(sec)
            continue
        hist, edges = np.histogram(
            sub["duration_h"], bins=np.linspace(0, 10, 50),
            weights=sub["power_kw"] / 1000,
        )
        ax.bar(edges[:-1], hist, width=np.diff(edges), align="edge",
               color=BESS_SECTOR_COLORS[sec],
               edgecolor="#1e293b", linewidth=0.3)
        ax.set_xlabel("Duration [h]")
        ax.set_ylabel("Installed power per bin [MW]")
        ax.set_title(
            f"{sec}\n{len(sub):,} units · {sub['power_kw'].sum() / 1e6:.2f} GW"
        )
        ax.grid(alpha=0.3)
    fig.suptitle("Per-sector BESS duration (Batterie only)", fontsize=13)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_duration, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_bess_charts(bess_df, units, out_power: str, out_energy: str,
                       out_duration: str, out_techmix: str, out_growth: str):
    """Render all five BESS sample charts in one pass."""
    import matplotlib.colors as mcolors

    snap = SNAP
    agg, active = mastr_plot.aggregate_bess_by_unit(bess_df, units, snap.date())
    active_gw = active["power_kw"].sum() / 1e6
    active_gwh = active["energy_kwh"].sum() / 1e6

    # -- Power choropleth -----------------------------------------------------
    pos = agg["power_gw"][agg["power_gw"] > 0].to_numpy()
    bins = mastr_plot.jenks_bins(pos, k=7)
    if len(bins) < 2:
        bins = [0.0, max(1.0, float(pos.max() or 1.0))]
    norm = mcolors.BoundaryNorm(bins, ncolors=256)
    fig, ax = plt.subplots(figsize=(9, 11), dpi=120)
    agg.plot(
        column="power_gw", ax=ax, cmap="BuPu", norm=norm,
        edgecolor="white", linewidth=0.3, legend=True,
        legend_kwds={"label": "BESS power [GW]", "shrink": 0.6},
        missing_kwds={"color": "#eeeeee"},
    )
    ax.set_axis_off()
    ax.set_title(
        f"Battery storage power per Kreis — {snap.date()}\n"
        f"{len(active):,} units · {active_gw:.2f} GW · {active_gwh:.1f} GWh",
        fontsize=13,
    )
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_power, format="svg", bbox_inches="tight")
    plt.close(fig)

    # -- Energy choropleth ----------------------------------------------------
    pos_e = agg["energy_gwh"][agg["energy_gwh"] > 0].to_numpy()
    bins_e = mastr_plot.jenks_bins(pos_e, k=7)
    if len(bins_e) < 2:
        bins_e = [0.0, max(1.0, float(pos_e.max() or 1.0))]
    norm_e = mcolors.BoundaryNorm(bins_e, ncolors=256)
    fig, ax = plt.subplots(figsize=(9, 11), dpi=120)
    agg.plot(
        column="energy_gwh", ax=ax, cmap="BuPu", norm=norm_e,
        edgecolor="white", linewidth=0.3, legend=True,
        legend_kwds={"label": "BESS energy [GWh]", "shrink": 0.6},
        missing_kwds={"color": "#eeeeee"},
    )
    ax.set_axis_off()
    ax.set_title(
        f"Battery storage energy per Kreis — {snap.date()}\n"
        f"{len(active):,} units · {active_gwh:.1f} GWh total",
        fontsize=13,
    )
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_energy, format="svg", bbox_inches="tight")
    plt.close(fig)

    # -- Duration histogram (Batterie only) -----------------------------------
    b = bess_df[
        (bess_df["storage_tech"] == "Batterie")
        & bess_df["install_date"].notna()
        & (bess_df["install_date"] <= snap)
        & bess_df["duration_h"].notna()
        & (bess_df["duration_h"] > 0)
        & (bess_df["duration_h"] < 24)
    ]
    fig, ax = plt.subplots(figsize=(11, 5), dpi=120)
    hist, edges = np.histogram(
        b["duration_h"], bins=np.linspace(0, 10, 60),
        weights=b["power_kw"] / 1000,  # MW per bin
    )
    ax.bar(edges[:-1], hist, width=np.diff(edges), align="edge",
           color="#a78bfa", edgecolor="#5b21b6", linewidth=0.4)
    ax.axvline(2, color="#dc2626", linewidth=1.2, linestyle="--", alpha=0.7)
    ax.text(2.05, ax.get_ylim()[1] * 0.92,
            "2 h\nhybrid PV / grid-services split",
            color="#dc2626", fontsize=9)
    ax.set_xlabel("Duration [h] = energy / power")
    ax.set_ylabel("Installed power per bin [MW]")
    ax.set_title(
        f"BESS duration distribution — {snap.date()} (Batterie only)\n"
        f"{len(b):,} units · {b['power_kw'].sum() / 1e6:.2f} GW · capacity-weighted"
    )
    ax.grid(alpha=0.3)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_duration, format="svg", bbox_inches="tight")
    plt.close(fig)

    # -- Tech mix (power + energy) --------------------------------------------
    mix = (
        active.groupby("storage_tech")
              .agg(power_gw=("power_kw", lambda s: s.sum() / 1e6),
                   energy_gwh=("energy_kwh", lambda s: s.sum() / 1e6),
                   n=("id", "size"))
              .sort_values("power_gw")
    )
    fig, axs = plt.subplots(1, 2, figsize=(13, 4), dpi=120)
    y = range(len(mix))
    axs[0].barh(y, mix["power_gw"], color="#a78bfa",
                edgecolor="#1e293b", linewidth=0.4)
    axs[0].set_yticks(list(y))
    axs[0].set_yticklabels(mix.index)
    axs[0].set_xlabel("Power [GW]")
    axs[0].set_title("Storage technology · installed power")
    axs[0].grid(axis="x", alpha=0.3)
    if len(mix) and mix["power_gw"].max() > 0:
        for i, (gw, n) in enumerate(zip(mix["power_gw"], mix["n"])):
            axs[0].text(gw + mix["power_gw"].max() * 0.01, i,
                        f"{int(n):,} units", va="center",
                        fontsize=9, color="#475569")
    axs[1].barh(y, mix["energy_gwh"], color="#0ea5e9",
                edgecolor="#1e293b", linewidth=0.4)
    axs[1].set_yticks(list(y))
    axs[1].set_yticklabels(mix.index)
    axs[1].set_xlabel("Energy [GWh]")
    axs[1].set_title("Storage technology · usable energy capacity")
    axs[1].grid(axis="x", alpha=0.3)
    fig.suptitle(
        f"Battery + other electricity storage by technology — {snap.date()} (active)",
        fontsize=13,
    )
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_techmix, format="svg", bbox_inches="tight")
    plt.close(fig)

    # -- Cumulative growth (dual axis power + energy) --------------------------
    g = bess_df[
        bess_df["install_date"].notna()
        & (bess_df["install_date"] >= "2010-01-01")
    ].copy()
    g["month"] = g["install_date"].dt.to_period("M")
    monthly_p = g.groupby("month")["power_kw"].sum().div(1e6).cumsum()
    monthly_e = g.groupby("month")["energy_kwh"].sum().div(1e6).cumsum()
    monthly_p.index = monthly_p.index.to_timestamp()
    monthly_e.index = monthly_e.index.to_timestamp()
    fig, ax = plt.subplots(figsize=(11, 4.5), dpi=120)
    ax2 = ax.twinx()
    ax.fill_between(monthly_p.index, monthly_p.values,
                    color="#a78bfa", alpha=0.35)
    ax.plot(monthly_p.index, monthly_p.values, color="#6d28d9", linewidth=2.2)
    ax2.plot(monthly_e.index, monthly_e.values,
             color="#0ea5e9", linewidth=2.2, linestyle="--")
    ax.set_ylabel("Cumulative power [GW]", color="#6d28d9")
    ax2.set_ylabel("Cumulative energy [GWh]", color="#0ea5e9")
    ax.set_title("Cumulative BESS power + energy capacity in Germany")
    ax.grid(alpha=0.3)
    from matplotlib.lines import Line2D
    ax.legend(handles=[
        Line2D([0], [0], color="#6d28d9", linewidth=2, label="Power [GW]"),
        Line2D([0], [0], color="#0ea5e9", linewidth=2, linestyle="--", label="Energy [GWh]"),
    ], loc="upper left")
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_growth, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_state_ramp(records, units, out_name):
    sub = records[
        records["energy_type"].isin(["Wind", "Solare Strahlungsenergie"])
        & (records["install_date"] >= "2000-01-01")
    ].copy()
    pts = gpd.GeoDataFrame(
        sub.reset_index(drop=True),
        geometry=gpd.points_from_xy(sub["longitude"], sub["latitude"], crs="EPSG:4326"),
    )
    j = gpd.sjoin(pts, units[["bundesland", "geometry"]], predicate="within", how="left")
    j = j.loc[~j.index.duplicated(keep="first")]
    sub["bundesland"] = j["bundesland"].reindex(sub.reset_index(drop=True).index).values

    sub["month"] = sub["install_date"].dt.to_period("M")
    piv = (
        sub.groupby(["month", "bundesland"])["power"]
           .sum().div(1e6).unstack(fill_value=0.0).cumsum()
    )
    piv.index = piv.index.to_timestamp()
    order = piv.iloc[-1].sort_values(ascending=False).index.tolist()
    piv = piv[order]

    import numpy as np
    palette = plt.cm.tab20(np.linspace(0, 1, len(piv.columns)))

    fig, ax = plt.subplots(figsize=(11, 6.5), dpi=120)
    ax.stackplot(piv.index, piv.values.T, labels=piv.columns,
                 colors=palette, alpha=0.92, linewidth=0)
    ax.set_ylabel("Cumulative installed [GW] (Wind + PV ≥49 kW)")
    ax.set_title(
        "Renewable build-out per Bundesland, 2000 → 2025\n"
        "(MaStR · wind: full · PV: top 200 k by capacity)"
    )
    ax.legend(loc="upper left", ncol=2, fontsize=9, framealpha=0.9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_pv_by_type(records, out_name):
    pv = records[records["energy_type"] == "Solare Strahlungsenergie"].copy()
    pv = pv[pv["install_date"] >= "2000-01-01"]
    pv["installation_type"] = pv["installation_type"].fillna("unknown")
    pv["month"] = pv["install_date"].dt.to_period("M")
    piv = (
        pv.groupby(["month", "installation_type"])["power"]
          .sum().div(1e6).unstack(fill_value=0.0).cumsum()
    )
    piv.index = piv.index.to_timestamp()
    order = piv.iloc[-1].sort_values(ascending=False).index.tolist()
    piv = piv[order]

    colors_map = {
        "free": "#f59e0b",
        "building": "#0ea5e9",
        "building_other": "#7c3aed",
        "parking_lot": "#10b981",
        "water": "#06b6d4",
        "balkonkraftwerk": "#ef4444",
        "unknown": "#94a3b8",
    }
    palette = [colors_map.get(c, "#94a3b8") for c in piv.columns]

    fig, axs = plt.subplots(1, 2, figsize=(13, 5.5), dpi=120,
                            gridspec_kw={"width_ratios": [2, 1]})
    ax = axs[0]
    ax.stackplot(piv.index, piv.values.T, labels=piv.columns,
                 colors=palette, alpha=0.95, linewidth=0)
    ax.set_ylabel("Cumulative PV [GW] (top 200 k plants)")
    ax.set_title("PV build-out by installation type, 2000 → 2025")
    ax.legend(loc="upper left", fontsize=9, framealpha=0.92)
    ax.grid(alpha=0.3)

    ax2 = axs[1]
    latest = piv.iloc[-1]
    ax2.pie(latest.values, labels=latest.index, colors=palette,
            autopct="%1.0f%%", startangle=90,
            textprops={"fontsize": 9},
            wedgeprops={"edgecolor": "white", "linewidth": 1})
    ax2.set_title(f"Share at {piv.index[-1].date()}\n({latest.sum():.1f} GW total)")

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
    render_energy_mix(records, "sample-energy-mix.svg")
    render_yoy_additions(records, units, "sample-2024-additions.svg", year=2024)
    render_state_ramp(records, units, "sample-state-ramp.svg")
    render_pv_by_type(records, "sample-pv-by-type.svg")
    render_density_map(records, units, "Wind", "GnBu",
                       "sample-wind-density.svg", "Wind capacity density")
    render_density_map(records, units, "Solare Strahlungsenergie", "YlOrRd",
                       "sample-pv-density.svg", "PV capacity density (≥49 kW)")
    export_largest_plants(records, units, "largest-plants.json")
    render_pv_orientation(records, "sample-pv-orientation.svg")
    render_wind_age(records, "sample-wind-age.svg")

    # BESS slice lives in its own data-bess/ dir; render only if present.
    try:
        bess_df, _ = mastr_plot.load_bess()
    except FileNotFoundError:
        print("BESS scrape not present — skipping BESS charts.")
    else:
        render_bess_charts(
            bess_df, units,
            out_power="sample-bess-power-map.svg",
            out_energy="sample-bess-energy-map.svg",
            out_duration="sample-bess-duration.svg",
            out_techmix="sample-bess-tech-mix.svg",
            out_growth="sample-bess-growth.svg",
        )
        render_bess_sector_charts(
            bess_df,
            out_summary="sample-bess-sectors.svg",
            out_growth="sample-bess-sector-growth.svg",
            out_duration="sample-bess-sector-duration.svg",
        )

    print("Sample renders complete.")


if __name__ == "__main__":
    main()
