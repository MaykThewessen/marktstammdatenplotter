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
import warnings
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import geopandas as gpd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mastr_plot  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIG = ROOT / "fig"
DOCS = ROOT / "docs" / "assets"
SNAP = pd.Timestamp("2026-05-01")  # Aligned with the final frame of the animation GIFs


def _save_choropleth_pair(gdf, col, cmap, bins, title, legend_label, out_name,
                          title_fontsize=13):
    """Render and save the Jenks-binned choropleth SVG."""
    import matplotlib.colors as mcolors
    vmax = float(gdf[col].replace(0, np.nan).max()) if len(gdf) else 1.0
    if not np.isfinite(vmax) or vmax <= 0:
        vmax = 1.0
    norm = mcolors.BoundaryNorm(bins, ncolors=256)
    stem = Path(out_name).stem
    fig, ax = plt.subplots(figsize=(9, 11), dpi=120)
    gdf.plot(
        column=col, ax=ax, cmap=cmap, norm=norm,
        edgecolor="white", linewidth=0.3, legend=True,
        legend_kwds={"label": legend_label, "shrink": 0.6},
        missing_kwds={"color": "#eeeeee"},
    )
    gdf.dissolve().boundary.plot(ax=ax, color="black", linewidth=0.8, zorder=5)
    ax.set_axis_off()
    ax.set_title(title, fontsize=title_fontsize)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / f"{stem}.svg", format="svg", bbox_inches="tight")
    plt.close(fig)


def render_map(records, units, energy_type, title_prefix, cmap, out_name, unit="GW"):
    agg, active = mastr_plot.aggregate_by_unit(records, units, SNAP.date(), energy_type)
    positive = agg["power_gw"][agg["power_gw"] > 0].to_numpy()
    bins = mastr_plot.jenks_bins(positive, k=7)
    total_gw = active["power"].sum() / 1e6
    shown_gw = agg["power_gw"].sum()
    title = (
        f"{title_prefix} — {SNAP.date()}\n"
        f"{len(active):,} plants · {round(total_gw, 1)} {unit}"
    )
    if total_gw - shown_gw > 1.0:
        title += f"\n({round(shown_gw, 1)} {unit} shown on map · " \
                 f"{round(total_gw - shown_gw, 1)} {unit} offshore / out-of-Kreis)"
    _save_choropleth_pair(agg, "power_gw", cmap, bins, title, f"Capacity [{unit}]", out_name)


def _load_planned_gw_monthly(energy_type: str, snap: pd.Timestamp) -> pd.Series | None:
    """Read In-Planung plants with a future planned date from raw MaStR CSV.

    Returns a monthly cumulative GW Series (Timestamp index) representing
    capacity additions expected AFTER snap, or None if data unavailable.
    Caches to a small parquet so repeat renders are fast.
    """
    bulk_dir = mastr_plot.find_bulk_dir()
    if bulk_dir is None:
        return None

    raw_map = {
        "Solare Strahlungsenergie": ("bnetza_mastr_solar_raw.csv",  "Bruttoleistung", "pv"),
        "Wind":                     ("bnetza_mastr_wind_raw.csv",   "Bruttoleistung", "wind"),
    }
    if energy_type not in raw_map:
        return None

    csv_name, power_col, tech_key = raw_map[energy_type]
    raw_path   = bulk_dir / csv_name
    cache_path = bulk_dir / f"planned-{tech_key}.parquet"

    if not raw_path.exists():
        return None

    if cache_path.exists() and cache_path.stat().st_mtime > raw_path.stat().st_mtime:
        planned_df = pd.read_parquet(cache_path)
    else:
        print(f"  Reading planned {tech_key} from {csv_name} (one-off, ~15 s) …")
        df = pd.read_csv(
            raw_path,
            usecols=["EinheitBetriebsstatus", "GeplantesInbetriebnahmedatum", power_col],
            dtype={"EinheitBetriebsstatus": "category", power_col: str,
                   "GeplantesInbetriebnahmedatum": str},
            low_memory=False,
        )
        df[power_col] = pd.to_numeric(df[power_col], errors="coerce")
        df["planned_date"] = pd.to_datetime(
            df["GeplantesInbetriebnahmedatum"], errors="coerce", utc=False
        )
        planned_df = df[
            (df["EinheitBetriebsstatus"] == "In Planung")
            & df["planned_date"].notna()
            & (df[power_col] > 0)
        ][["planned_date", power_col]].rename(columns={power_col: "power_kw"}).copy()
        planned_df.to_parquet(cache_path, index=False)
        print(f"    Cached → {cache_path.name}  ({len(planned_df):,} rows)")

    future = planned_df[planned_df["planned_date"] > snap].copy()
    if future.empty:
        return None

    monthly = (
        future.assign(month=future["planned_date"].dt.to_period("M"))
              .groupby("month")["power_kw"].sum().div(1e6).cumsum()
    )
    monthly.index = monthly.index.to_timestamp()
    return monthly


def render_growth(records, energy_type, label, color_fill, color_line, out_name,
                  xlim_start="2000-01-01", xlim_end=None, unit="GW"):
    sub = records[records["energy_type"] == energy_type].copy()
    monthly = (
        sub.assign(month=sub["install_date"].dt.to_period("M"))
           .groupby("month")["power"].sum().div(1e6).cumsum()
    )
    monthly.index = monthly.index.to_timestamp()

    planned = _load_planned_gw_monthly(energy_type, SNAP)
    last_val = float(monthly.iloc[-1]) if len(monthly) else 0.0

    fig, ax = plt.subplots(figsize=(9, 3.5), dpi=120)
    ax.fill_between(monthly.index, monthly.values, color=color_fill, alpha=0.35)
    ax.plot(monthly.index, monthly.values, color=color_line, linewidth=2,
            label="Commissioned")
    if planned is not None:
        plan_line = planned + last_val
        # Connect planned line to historical endpoint
        join_idx = pd.DatetimeIndex([SNAP]).append(plan_line.index)
        join_vals = np.concatenate([[last_val], plan_line.values])
        ax.plot(join_idx, join_vals, color=color_line, linewidth=1.8,
                linestyle="--", alpha=0.7, label="Planned (MaStR)")
    ax.set_xlim(
        left=pd.Timestamp(xlim_start),
        right=pd.Timestamp(xlim_end) if xlim_end else None,
    )
    ax.set_ylabel(f"Cumulative {label} [{unit}]")
    ax.set_title(f"Cumulative {label} capacity over time")
    ax.legend(loc="upper left", fontsize=9)
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
    # Bulk parquet anonymises owner_name for residential rows; skip those.
    if "owner_name" not in active.columns or active["owner_name"].isna().all():
        print("render_top_operators: no owner_name column available — skipping.")
        return
    active = active[active["owner_name"].notna()]
    if active.empty:
        print("render_top_operators: no rows with owner_name — skipping.")
        return
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
    ax.barh(y, top["PV"], left=top["Wind"], color="#f59e0b", label="PV")
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
        f"(snapshot of {len(records):,} plants in this scrape)"
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
    title = (
        f"Capacity added during {year} per Kreis\n"
        f"(Wind + PV · {round(geo['mw_added'].sum() / 1e3, 1)} GW total)"
    )
    _save_choropleth_pair(geo, "mw_added", "RdPu", bins, title,
                          f"New capacity {year} [MW]", out_name)


def render_density_map(records, units, energy_type, cmap, out_name, title_prefix,
                       unit="GW"):
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
    title = (
        f"{title_prefix} per km² — {SNAP.date()}\n"
        f"{len(active):,} active · {round(active['power'].sum() / 1e6, 1)} {unit} total"
    )
    _save_choropleth_pair(agg, "mw_per_km2", cmap, bins, title,
                          "Capacity density [MW/km²]", out_name)


def export_largest_plants(records, units, out_name, top_n: int = 10):
    """Write JSON listing the top-N largest single plants per technology."""
    import json

    def topn(et):
        sub = records[records["energy_type"] == et].copy()
        sub = sub.sort_values("power", ascending=False).head(top_n)
        if sub.empty:
            return sub
        if "landkreis" in sub.columns:
            sub["kreis"] = sub["landkreis"]
        else:
            sub["kreis"] = None
        if "bundesland" not in sub.columns:
            sub["bundesland"] = None
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
        f"({fac_known.sum():.1f} GWp with known facing)"
    )

    til_known = til.drop("unknown", errors="ignore")
    axs[1].barh(range(len(til_known)), til_known.values,
                color="#0ea5e9", edgecolor="#1e293b", linewidth=0.4)
    axs[1].set_yticks(range(len(til_known)))
    axs[1].set_yticklabels(til_known.index)
    axs[1].set_xlabel("Installed capacity [GWp]")
    axs[1].set_title("PV capacity by tilt angle")
    axs[1].grid(axis="x", alpha=0.3)
    fig.suptitle("Solar PV orientation analysis", fontsize=13)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_pv_orientation_polar(out_name):
    """Polar heatmap: PV capacity by compass orientation & commissioning year.

    Reads BNetzA_MaStR/solar.parquet directly (needs the `orientation` column
    which is not present in the mastr_plot records DataFrame).  Skips silently
    if the parquet is not on disk.
    """
    from scipy.interpolate import interp1d

    parquet = ROOT / "BNetzA_MaStR" / "solar.parquet"
    if not parquet.exists():
        print(f"render_pv_orientation_polar: {parquet} not found — skipping.")
        return

    df = pd.read_parquet(
        parquet,
        columns=["orientation", "installed_capacity_kw", "commissioning_date", "status"],
    )
    active = df[df["status"] == "In Betrieb"].dropna(
        subset=["commissioning_date", "orientation"]
    )
    active = active.assign(year=active["commissioning_date"].dt.year.astype("Int64"))
    active = active[active["year"].between(2000, SNAP.year)]

    MASTR_NAMES = {
        "N": "Nord", "NO": "Nord-Ost", "O": "Ost", "SO": "Süd-Ost",
        "S": "Süd", "SW": "Süd-West", "W": "West", "NW": "Nord-West",
    }
    FULL_NAMES = {
        "N": "Nord", "NO": "NO", "O": "Ost", "SO": "SO",
        "S": "Süd", "SW": "SW", "W": "West", "NW": "NW",
    }
    LABELS = list(MASTR_NAMES.keys())
    DEGREES = [0, 45, 90, 135, 180, 225, 270, 315]

    ow = active[active["orientation"] == "Ost-West"].copy()
    ow_half = ow.assign(installed_capacity_kw=ow["installed_capacity_kw"] / 2)

    rows = {}
    for code, name in MASTR_NAMES.items():
        rows[code] = active[active["orientation"] == name][
            ["year", "installed_capacity_kw"]
        ]
    rows["O"] = pd.concat([rows["O"], ow_half[["year", "installed_capacity_kw"]]])
    rows["W"] = pd.concat([rows["W"], ow_half[["year", "installed_capacity_kw"]]])

    years = list(range(2000, 2026))
    matrix = np.zeros((len(years), len(LABELS)))
    for j, code in enumerate(LABELS):
        grouped = rows[code].groupby("year")["installed_capacity_kw"].sum() / 1e6
        for i, yr in enumerate(years):
            matrix[i, j] = grouped.get(yr, 0.0)

    N_THETA = 720
    theta_fine_deg = np.linspace(0, 360, N_THETA, endpoint=False)
    deg_arr = np.array(DEGREES, dtype=float)

    matrix_smooth = np.zeros((len(years), N_THETA))
    for i in range(len(years)):
        vals = matrix[i, :]
        deg_wrap = np.append(deg_arr, deg_arr[0] + 360.0)
        vals_wrap = np.append(vals, vals[0])
        f = interp1d(deg_wrap, vals_wrap, kind="cubic")
        matrix_smooth[i, :] = np.clip(f(theta_fine_deg), 0, None)

    theta_edges = np.deg2rad(np.linspace(0, 360, N_THETA + 1))
    r_edges = np.array(years + [2026], dtype=float)
    THETA, R = np.meshgrid(theta_edges, r_edges)

    import matplotlib.colors as mcolors
    from matplotlib.cm import ScalarMappable

    cmap = mcolors.LinearSegmentedColormap.from_list(
        "green_yellow_red", ["#006400", "#32cd32", "#ffff00", "#ff4500", "#8b0000"]
    )
    norm = mcolors.Normalize(vmin=0, vmax=matrix_smooth.max())

    fig = plt.figure(figsize=(12, 12), facecolor="#0d0d0d")
    ax = fig.add_subplot(111, projection="polar")
    ax.set_facecolor("#0d0d0d")
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)

    ax.pcolormesh(THETA, R, matrix_smooth, cmap=cmap, norm=norm,
                  shading="flat", rasterized=True)

    ax.set_ylim(r_edges[0], r_edges[-1])
    ax.set_yticks(years[::5])
    ax.set_yticklabels(
        [str(y) for y in years[::5]], fontsize=7.5, color="white", fontfamily="monospace"
    )
    ax.set_rlabel_position(8)

    tick_degs = np.arange(0, 360, 30)
    ax.set_xticks(np.deg2rad(tick_degs))
    ax.set_xticklabels([f"{d}°" for d in tick_degs], fontsize=8.5, color="white")
    ax.tick_params(axis="x", pad=8, colors="white")

    for yr in years[::5]:
        theta_ring = np.linspace(0, 2 * np.pi, 500)
        ax.plot(theta_ring, [yr] * 500, color="white", lw=0.4, alpha=0.35)

    COMPASS_LABELS = {
        0: "Nord", 90: "Ost", 180: "Süd", 270: "West",
        45: "NO", 135: "SO", 225: "SW", 315: "NW",
    }
    for deg, lbl in COMPASS_LABELS.items():
        bold = deg % 90 == 0
        ax.text(
            np.deg2rad(deg), 2027, lbl,
            ha="center", va="center",
            fontsize=10 if bold else 8,
            fontweight="bold" if bold else "normal",
            color="white",
        )

    total_per_dir = matrix.sum(axis=0)
    for j, (code, deg) in enumerate(zip(LABELS, DEGREES)):
        gw = total_per_dir[j]
        ax.text(
            np.deg2rad(deg), 2024.5,
            f"{gw:.0f} GWp" if gw >= 10 else f"{gw:.1f}",
            ha="center", va="center", fontsize=6.5, color="white", alpha=0.85,
        )

    theta_border = np.linspace(0, 2 * np.pi, 600)
    ax.plot(theta_border, [r_edges[-1]] * 600, color="white", lw=1.2, alpha=0.7)

    cbar = fig.colorbar(
        ScalarMappable(norm=norm, cmap=cmap),
        ax=ax, pad=0.08, fraction=0.025, aspect=35, shrink=0.7,
    )
    cbar.set_label("GWp commissioned per year", fontsize=9, color="white", labelpad=8)
    cbar.ax.tick_params(labelsize=8, colors="white")
    plt.setp(cbar.ax.yaxis.get_ticklines(), color="white")

    total_gw_all = matrix.sum()
    peak_yr = years[int(np.argmax(matrix.sum(axis=1)))]
    ax.set_title(
        f"German PV — capacity by orientation & commissioning year\n"
        f"MaStR · {total_gw_all:.0f} GWp total · peak year: {peak_yr}",
        fontsize=12, pad=24, color="white",
    )

    plt.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight",
                    facecolor="#0d0d0d")
    plt.close(fig)


def render_pv_orientation_polar_snapshot(out_name):
    """Polar rose: total PV capacity by compass orientation at SNAP date.

    Single-state snapshot (no time axis). Radial axis = GW installed.
    Smooth cubic interpolation between 8 compass directions → full 360° fill.
    Reads BNetzA_MaStR/solar.parquet directly.
    """
    from scipy.interpolate import interp1d
    import matplotlib.colors as mcolors
    from matplotlib.cm import ScalarMappable

    parquet = ROOT / "BNetzA_MaStR" / "solar.parquet"
    if not parquet.exists():
        print(f"render_pv_orientation_polar_snapshot: {parquet} not found — skipping.")
        return

    snap = SNAP.tz_localize("UTC") if SNAP.tzinfo is None else SNAP
    df = pd.read_parquet(
        parquet,
        columns=["orientation", "installed_capacity_kw",
                 "commissioning_date", "decommissioning_date"],
    )
    active = df[
        (df["commissioning_date"] <= snap)
        & (df["decommissioning_date"].isna() | (df["decommissioning_date"] > snap))
    ].dropna(subset=["orientation"])

    MASTR_NAMES = {
        "N": "Nord", "NO": "Nord-Ost", "O": "Ost", "SO": "Süd-Ost",
        "S": "Süd", "SW": "Süd-West", "W": "West", "NW": "Nord-West",
    }
    DEGREES = [0, 45, 90, 135, 180, 225, 270, 315]
    LABELS = list(MASTR_NAMES.keys())

    ow = active[active["orientation"] == "Ost-West"]
    gw_by_dir = {}
    for code, name in MASTR_NAMES.items():
        sub = active[active["orientation"] == name]
        gw_by_dir[code] = sub["installed_capacity_kw"].sum() / 1e6
    gw_by_dir["O"] += ow["installed_capacity_kw"].sum() / 1e6 / 2
    gw_by_dir["W"] += ow["installed_capacity_kw"].sum() / 1e6 / 2

    tracker_gw = active[active["orientation"] == "nachgeführt"]["installed_capacity_kw"].sum() / 1e6
    total_gw = sum(gw_by_dir.values()) + tracker_gw

    gw_vals = np.array([gw_by_dir[c] for c in LABELS])
    deg_arr = np.array(DEGREES, dtype=float)

    # Smooth circular interpolation → 1440 theta points
    N_THETA = 1440
    theta_fine_deg = np.linspace(0, 360, N_THETA, endpoint=False)
    deg_wrap = np.append(deg_arr, deg_arr[0] + 360.0)
    gw_wrap = np.append(gw_vals, gw_vals[0])
    f = interp1d(deg_wrap, gw_wrap, kind="cubic")
    gw_smooth = np.clip(f(theta_fine_deg), 0, None)

    theta_rad = np.deg2rad(theta_fine_deg)
    # Close the curve
    theta_closed = np.append(theta_rad, theta_rad[0])
    gw_closed = np.append(gw_smooth, gw_smooth[0])

    # Colormap: map GW value → color
    cmap = mcolors.LinearSegmentedColormap.from_list(
        "green_yellow_red", ["#006400", "#32cd32", "#ffff00", "#ff4500", "#8b0000"]
    )
    norm = mcolors.Normalize(vmin=0, vmax=gw_vals.max())

    fig = plt.figure(figsize=(11, 11), facecolor="#0d0d0d")
    ax = fig.add_subplot(111, projection="polar")
    ax.set_facecolor("#0d0d0d")
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)

    # Fill with gradient: draw many thin wedges coloured by local GW value
    for i in range(N_THETA):
        t0 = theta_rad[i]
        t1 = theta_rad[(i + 1) % N_THETA]
        r = gw_smooth[i]
        color = cmap(norm(r))
        ax.fill_between([t0, t1], 0, [r, r], color=color, linewidth=0)

    # Outline curve
    ax.plot(theta_closed, gw_closed, color="white", lw=1.0, alpha=0.7)

    # Radial grid lines
    gw_max = gw_vals.max()
    r_ticks = [10, 20, 30, 40, 50]
    r_ticks = [r for r in r_ticks if r <= gw_max * 1.1]
    for r in r_ticks:
        ring = np.linspace(0, 2 * np.pi, 500)
        ax.plot(ring, [r] * 500, color="white", lw=0.3, alpha=0.25, linestyle="--")
        ax.text(np.deg2rad(15), r, f"{r} GWp",
                fontsize=6.5, color="white", alpha=0.6, va="bottom")

    # Compass labels + GW values at each direction
    COMPASS_FULL = {
        0: "Nord", 45: "NO", 90: "Ost", 135: "SO",
        180: "Süd", 225: "SW", 270: "West", 315: "NW",
    }
    for code, deg in zip(LABELS, DEGREES):
        gw = gw_by_dir[code]
        bold = deg % 90 == 0
        r_label = gw_max * 1.15
        ax.text(
            np.deg2rad(deg), r_label,
            COMPASS_FULL[deg],
            ha="center", va="center",
            fontsize=11 if bold else 8.5,
            fontweight="bold" if bold else "normal",
            color="white",
        )
        ax.text(
            np.deg2rad(deg), gw_max * 1.05,
            f"{gw:.1f} GWp",
            ha="center", va="center",
            fontsize=7.5, color=cmap(norm(gw)), fontweight="bold",
        )

    ax.set_ylim(0, gw_max * 1.25)
    ax.set_yticks([])
    tick_degs = np.arange(0, 360, 30)
    ax.set_xticks(np.deg2rad(tick_degs))
    ax.set_xticklabels([f"{d}°" for d in tick_degs], fontsize=7.5, color="#aaa")
    ax.tick_params(axis="x", pad=6)

    # Colorbar
    cbar = fig.colorbar(
        ScalarMappable(norm=norm, cmap=cmap),
        ax=ax, pad=0.09, fraction=0.025, aspect=35, shrink=0.65,
    )
    cbar.set_label("Installed capacity [GWp]", fontsize=9, color="white", labelpad=8)
    cbar.ax.tick_params(labelsize=8, colors="white")
    plt.setp(cbar.ax.yaxis.get_ticklines(), color="white")

    ax.set_title(
        f"German PV — orientation of installed capacity · {SNAP.date()}\n"
        f"Active units · MaStR · {total_gw:.0f} GWp total"
        f" · trackers {tracker_gw:.2f} GWp (excluded from rose)",
        fontsize=11, pad=22, color="white",
    )

    plt.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight",
                    facecolor="#0d0d0d")
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
    merged = merged[(merged["install_year"] >= 1990) & (merged["install_year"] <= SNAP.year)]

    active = wind.dropna(subset=["install_year"])
    mean_mw = active.groupby("install_year")["mw"].mean()
    mean_mw = mean_mw[(mean_mw.index >= 1995) & (mean_mw.index <= SNAP.year)]

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


SIZE_BIN_EDGES_KW = [0, 10, 100, 1_000, 10_000, 100_000, 1_000_000, 100_000_000]
SIZE_BIN_LABELS = [
    "0-10 kW", "10-100 kW", "100 kW-1 MW", "1-10 MW",
    "10-100 MW", "100-1000 MW", "1 GW+",
]


def _size_bin_stats(df, power_col="power_kw", energy_col=None):
    """Sum power (GW), energy (GWh, optional), and counts per size-bin."""
    df = df.copy()
    df["__bin__"] = pd.cut(
        df[power_col], bins=SIZE_BIN_EDGES_KW,
        labels=SIZE_BIN_LABELS, include_lowest=True, right=False,
    )
    gw = (df.groupby("__bin__", observed=False)[power_col].sum() / 1e6
            ).reindex(SIZE_BIN_LABELS).fillna(0.0)
    n = (df.groupby("__bin__", observed=False).size()
            ).reindex(SIZE_BIN_LABELS).fillna(0).astype(int)
    gwh = None
    if energy_col is not None:
        gwh = (df.groupby("__bin__", observed=False)[energy_col].sum() / 1e6
                  ).reindex(SIZE_BIN_LABELS).fillna(0.0)
    return gw, gwh, n


def _bar_with_labels(ax, values, color, ylabel, title, fmt="{:.2f}"):
    bars = ax.bar(SIZE_BIN_LABELS, values, color=color,
                  edgecolor="#1e293b", linewidth=0.4)
    ymax = max(values.max(), 1e-9)
    for i, v in enumerate(values):
        if v <= 0:
            continue
        ax.text(i, v + ymax * 0.02, fmt.format(v), ha="center", fontsize=9)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis="y", alpha=0.3)
    ax.set_xticks(range(len(SIZE_BIN_LABELS)))
    ax.set_xticklabels(SIZE_BIN_LABELS, rotation=25, ha="right")


def render_bess_by_size(bess_df, out_name: str):
    """Two-panel: GW per power-bin + GWh per power-bin (BESS)."""
    snap = SNAP
    active = bess_df[
        (bess_df["install_date"] <= snap)
        & (bess_df["removal_date"].isna() | (bess_df["removal_date"] > snap))
    ]
    gw, gwh, n = _size_bin_stats(active, "power_kw", "energy_kwh")

    purples = ["#ede9fe", "#ddd6fe", "#c4b5fd", "#a78bfa",
               "#8b5cf6", "#6d28d9", "#4c1d95"]
    fig, axs = plt.subplots(1, 2, figsize=(14, 4.5), dpi=120)
    _bar_with_labels(axs[0], gw, purples,
                     "Installed power [GW]",
                     "BESS installed power by per-unit size-bin",
                     fmt="{:.2f}")
    _bar_with_labels(axs[1], gwh, purples,
                     "Usable energy [GWh]",
                     "BESS usable energy by per-unit size-bin",
                     fmt="{:.1f}")
    fig.suptitle(
        f"BESS size-bin breakdown — {snap.date()} (active only · "
        f"{len(active):,} units · {gw.sum():.2f} GW · {gwh.sum():.1f} GWh)",
        fontsize=13,
    )
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_generation_by_size(records, energy_type: str, palette: list[str],
                              tech_label: str, noun: str, out_name: str,
                              aggregate_by: str | None = None, unit: str = "GW"):
    """Single-panel GW per power-bin for Wind / PV.

    `aggregate_by=None`: per-unit (per-turbine, per-plant) — default.
    `aggregate_by="owner_name"`: collapse units that share an owner into
      a single 'project' first, then bin by project power. Useful for
      Wind where turbines from the same Windpark share an SPV owner.
    """
    snap = SNAP
    sub = records[records["energy_type"] == energy_type]
    active = sub[
        (sub["install_date"] <= snap)
        & (sub["removal_date"].isna() | (sub["removal_date"] > snap))
    ].copy()
    # Don't introduce a duplicate `power_kw` column if the source already
    # exposes one (bulk parquet path).
    if "power_kw" not in active.columns:
        active = active.rename(columns={"power": "power_kw"})
    elif "power" in active.columns:
        active = active.drop(columns=["power"])

    if aggregate_by:
        # Drop rows with missing owner — they can't be grouped meaningfully.
        if aggregate_by not in active.columns:
            print(f"render_generation_by_size: no {aggregate_by!r} column — "
                  "falling back to per-unit aggregation.")
            aggregate_by = None
        else:
            active = active[active[aggregate_by].notna()
                            & (active[aggregate_by].astype(str).str.strip() != "")]
    if aggregate_by:
        agg = (active.groupby(aggregate_by, dropna=False)["power_kw"]
                     .sum().reset_index())
        unit_count = len(active)
        project_count = len(agg)
        total_gw = float(agg["power_kw"].sum()) / 1e6 if len(agg) else 0.0
        scope_label = (
            f"{project_count:,} projects (aggregated from {unit_count:,} "
            f"{noun} sharing an owner) · {total_gw:.1f} {unit} total"
        )
        binned_input = agg
    else:
        scope_label = (
            f"{len(active):,} active {noun} · "
            f"{active['power_kw'].sum() / 1e6:.1f} {unit} total"
        )
        binned_input = active

    gw, _, _ = _size_bin_stats(binned_input, "power_kw")

    fig, ax = plt.subplots(figsize=(11, 4.5), dpi=120)
    bin_basis = "per-project" if aggregate_by else "per-unit"
    _bar_with_labels(
        ax, gw, palette, f"Installed power [{unit}]",
        f"{tech_label} installed power by {bin_basis} size-bin — {snap.date()}\n"
        f"({scope_label})",
        fmt="{:.2f}",
    )
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)


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

    sector_order = list(reversed(mastr_plot.BESS_SECTORS))  # LSS, CSS, HSS

    def cum_pivot(col):
        piv = (
            df_h.groupby(["month", "sector"])[col]
                .sum().div(1e6).unstack(fill_value=0).cumsum()
        )
        piv = piv[[c for c in sector_order if c in piv.columns]]
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
    fig.suptitle("Battery + storage build-out by sector — LSS / CSS / HSS",
                 fontsize=13)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_growth, format="svg", bbox_inches="tight")
    plt.close(fig)

    # -- 3. per-sector duration distribution (overlaid on one axis) ----------
    b = bess_df[
        (bess_df["storage_tech"] == "Batterie")
        & bess_df["install_date"].notna()
        & (bess_df["install_date"] <= snap)
        & bess_df["duration_h"].notna()
        & (bess_df["duration_h"] > 0)
        & (bess_df["duration_h"] < 24)
    ]
    fig, ax = plt.subplots(figsize=(11, 5), dpi=120)
    bin_edges = np.linspace(0, 10, 50)
    bin_width = bin_edges[1] - bin_edges[0]
    legend_handles = []
    for sec in mastr_plot.BESS_SECTORS:
        sub = b[b["sector"] == sec]
        if sub.empty:
            continue
        hist, _ = np.histogram(
            sub["duration_h"], bins=bin_edges,
            weights=sub["power_kw"] / 1000,  # MW per bin
        )
        bars = ax.bar(
            bin_edges[:-1], hist, width=bin_width, align="edge",
            color=BESS_SECTOR_COLORS[sec], alpha=0.55,
            edgecolor=BESS_SECTOR_COLORS[sec], linewidth=0.6,
            label=f"{sec} · {len(sub):,} units · "
                  f"{sub['power_kw'].sum() / 1e6:.2f} GW",
        )
        legend_handles.append(bars)
    ax.set_xlabel("Duration [h] = energy / power")
    ax.set_ylabel("Installed power per bin [MW]")
    ax.set_title(
        f"BESS duration distribution by sector — {snap.date()} (Batterie only)\n"
        "HSS clusters at ~1 h (paired to PV self-consumption) · "
        "CSS at 1.5–2 h · LSS spreads across 1–4 h"
    )
    ax.legend(loc="upper right", fontsize=9, framealpha=0.92)
    ax.grid(alpha=0.3)
    ax.set_xlim(0, 10)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_duration, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_psh_charts(psh_df, units, out_map: str, out_summary: str, out_top: str):
    """Pumped-hydro storage: map + summary stats + top-N table.

    PSH is reported separately from batteries everywhere serious
    (battery-charts.de, BVES, EASE, EU SET-Plan) because it's a
    different technology with a different lifecycle and different
    grid-services profile.
    """
    import matplotlib.colors as mcolors

    snap = SNAP
    active = psh_df[
        (psh_df["install_date"] <= snap)
        & (psh_df["removal_date"].isna() | (psh_df["removal_date"] > snap))
    ].copy()
    active_gw = active["power_kw"].sum() / 1e6
    active_gwh = active["energy_kwh"].sum() / 1e6

    # -- 1. Choropleth (energy per Kreis — PSH is GWh-heavy) ----------------
    agg, _ = mastr_plot.aggregate_bess_by_unit(psh_df, units, snap.date())
    pos = agg["energy_gwh"][agg["energy_gwh"] > 0].to_numpy()
    bins = mastr_plot.jenks_bins(pos, k=min(6, max(2, len(pos) - 1)))
    if len(bins) < 2:
        bins = [0.0, max(1.0, float(pos.max() or 1.0))]
    title_psh = (
        f"Pumped-hydro storage (PSH) energy per Kreis — {snap.date()}\n"
        f"{len(active):,} sites · {active_gw:.2f} GW · {active_gwh:.1f} GWh "
        f"(roughly 100× the energy density of battery storage)"
    )
    _save_choropleth_pair(agg, "energy_gwh", "GnBu", bins, title_psh,
                          "PSH energy [GWh]", out_map, title_fontsize=12)

    # -- 2. Summary bars (power + energy + duration) -------------------------
    duration = active.copy()
    duration["duration_h"] = duration["energy_kwh"] / duration["power_kw"].replace(0, np.nan)
    fig, axs = plt.subplots(1, 3, figsize=(14, 4.2), dpi=120)
    # Power per Bundesland
    by_state = (
        active.groupby("bundesland", dropna=False)
              .agg(gw=("power_kw", lambda s: s.sum() / 1e6),
                   gwh=("energy_kwh", lambda s: s.sum() / 1e6),
                   n=("id", "size"))
              .sort_values("gw")
    )
    by_state.index = by_state.index.fillna("(unknown / cross-border)")
    y = range(len(by_state))
    axs[0].barh(y, by_state["gw"], color="#22c55e",
                edgecolor="#1e293b", linewidth=0.4)
    axs[0].set_yticks(list(y))
    axs[0].set_yticklabels(by_state.index, fontsize=9)
    axs[0].set_xlabel("Power [GW]")
    axs[0].set_title("PSH power by Bundesland")
    axs[0].grid(axis="x", alpha=0.3)

    axs[1].barh(y, by_state["gwh"], color="#0ea5e9",
                edgecolor="#1e293b", linewidth=0.4)
    axs[1].set_yticks(list(y))
    axs[1].set_yticklabels(by_state.index, fontsize=9)
    axs[1].set_xlabel("Energy [GWh]")
    axs[1].set_title("PSH energy by Bundesland")
    axs[1].grid(axis="x", alpha=0.3)

    # Duration histogram
    dur = duration["duration_h"].dropna()
    dur = dur[(dur > 0) & (dur < 200)]
    axs[2].hist(dur, bins=20, color="#a78bfa",
                edgecolor="#1e293b", linewidth=0.4)
    axs[2].set_xlabel("Duration [h]")
    axs[2].set_ylabel("Unit count")
    axs[2].set_title(f"PSH duration distribution\nmedian = {dur.median():.1f} h")
    axs[2].grid(axis="y", alpha=0.3)

    fig.suptitle(
        f"Pumped-hydro storage in Germany — {snap.date()} "
        f"({len(active)} sites · {active_gw:.2f} GW · {active_gwh:.1f} GWh)",
        fontsize=13,
    )
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_summary, format="svg", bbox_inches="tight")
    plt.close(fig)

    # -- 3. Top-15 sites table chart ----------------------------------------
    top = active.sort_values("power_kw", ascending=False).head(15).iloc[::-1]
    fig, ax = plt.subplots(figsize=(11, 6), dpi=120)
    y = range(len(top))
    ax.barh(y, top["power_kw"] / 1000, color="#0ea5e9",
            edgecolor="#1e293b", linewidth=0.4, label="Power [MW]")
    for i, (mw, gwh, ins) in enumerate(zip(
        top["power_kw"] / 1000, top["energy_kwh"] / 1000, top["install_date"]
    )):
        yr = ins.year if pd.notna(ins) else "—"
        ax.text(mw + max(top["power_kw"] / 1000) * 0.01, i,
                f"{gwh:.1f} GWh · {yr}",
                va="center", fontsize=8, color="#475569")
    ax.set_yticks(list(y))
    ax.set_yticklabels(top["name"].str[:50], fontsize=9)
    ax.set_xlabel("Installed power [MW]")
    ax.set_title(
        f"Top {len(top)} pumped-hydro storage sites · {snap.date()}\n"
        "Annotation = installed energy [GWh] · commissioning year"
    )
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_top, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_bess_charts(bess_df, units, out_power: str, out_energy: str,
                       out_duration: str, out_growth: str):
    """Render four battery-only sample charts in one pass.

    `bess_df` must be the **batteries-only** slice (Batterie chemistry);
    pumped-hydro lives in its own render_psh_charts. The old multi-tech
    'tech-mix' subchart was dropped post-split — it would have collapsed
    to a single bar.
    """
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
    title_bess_p = (
        f"Battery storage power per Kreis — {snap.date()}\n"
        f"{len(active):,} units · {active_gw:.2f} GW · {active_gwh:.1f} GWh"
    )
    _save_choropleth_pair(agg, "power_gw", "BuPu", bins, title_bess_p,
                          "BESS power [GW]", out_power)

    # -- Energy choropleth ----------------------------------------------------
    pos_e = agg["energy_gwh"][agg["energy_gwh"] > 0].to_numpy()
    bins_e = mastr_plot.jenks_bins(pos_e, k=7)
    if len(bins_e) < 2:
        bins_e = [0.0, max(1.0, float(pos_e.max() or 1.0))]
    title_bess_e = (
        f"Battery storage energy per Kreis — {snap.date()}\n"
        f"{len(active):,} units · {active_gwh:.1f} GWh total"
    )
    _save_choropleth_pair(agg, "energy_gwh", "BuPu", bins_e, title_bess_e,
                          "BESS energy [GWh]", out_energy)

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
    ax.fill_between(monthly_p.index, monthly_p.values,
                    color="#a78bfa", alpha=0.35)
    ax.plot(monthly_p.index, monthly_p.values, color="#6d28d9", linewidth=2.2,
            label="Power [GW]")
    ax.plot(monthly_e.index, monthly_e.values,
            color="#0ea5e9", linewidth=2.2, linestyle="--", label="Energy [GWh]")
    ax.set_ylabel("Cumulative GW / GWh")
    ax.set_title("Cumulative BESS power + energy capacity in Germany")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_growth, format="svg", bbox_inches="tight")
    plt.close(fig)


def render_state_ramp(records, units, out_name):
    sub = records[
        records["energy_type"].isin(["Wind", "Solare Strahlungsenergie"])
        & (records["install_date"] >= "2000-01-01")
    ].copy()
    # Prefer pre-joined `bundesland` from the bulk parquet — saves a 4 M-point
    # spatial join. Fall back to sjoin only when the column isn't present.
    if "bundesland" not in sub.columns or sub["bundesland"].isna().all():
        pts = gpd.GeoDataFrame(
            sub.reset_index(drop=True),
            geometry=gpd.points_from_xy(sub["longitude"], sub["latitude"], crs="EPSG:4326"),
        )
        j = gpd.sjoin(pts, units[["bundesland", "geometry"]], predicate="within", how="left")
        j = j.loc[~j.index.duplicated(keep="first")]
        sub["bundesland"] = j["bundesland"].reindex(sub.reset_index(drop=True).index).values
    sub = sub[sub["bundesland"].notna()]

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
    ax.set_ylabel("Cumulative installed [GW] (Wind + PV)")
    ax.set_title(
        f"Renewable build-out per Bundesland, 2000 → {SNAP.year}\n"
        "(MaStR · full registry)"
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
    pinned_last = ["building_other", "balkonkraftwerk"]
    base_order = [c for c in piv.iloc[-1].sort_values(ascending=False).index if c not in pinned_last]
    order = base_order + [c for c in pinned_last if c in piv.columns]
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
    ax.set_ylabel("Cumulative PV [GWp]")
    ax.set_title(f"PV build-out by installation type, 2000 → {SNAP.year}")
    ax.legend(loc="upper left", fontsize=9, framealpha=0.92)
    ax.grid(alpha=0.3)

    ax2 = axs[1]
    latest = piv.iloc[-1]
    ax2.pie(latest.values, labels=latest.index, colors=palette,
            autopct="%1.0f%%", startangle=90,
            textprops={"fontsize": 9},
            wedgeprops={"edgecolor": "white", "linewidth": 1})
    ax2.set_title(f"Share at {piv.index[-1].date()}\n({latest.sum():.1f} GWp total)")

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
        # If the source already carries `bundesland` (open-MaStR bulk
        # dump has it pre-joined per row), use it directly. Otherwise
        # fall back to a spatial join.
        if "bundesland" in active.columns and active["bundesland"].notna().any():
            return active.groupby("bundesland")["power"].sum().div(1e6)
        pts = gpd.GeoDataFrame(
            active.copy(),
            geometry=gpd.points_from_xy(active["longitude"], active["latitude"], crs="EPSG:4326"),
        )
        j = gpd.sjoin(pts, units[["bundesland", "geometry"]], predicate="within", how="left")
        return j.groupby("bundesland_right")["power"].sum().div(1e6)

    wind_s = state_totals(records[records["energy_type"] == "Wind"]).rename("Wind")
    pv_s = state_totals(records[records["energy_type"] == "Solare Strahlungsenergie"]).rename("PV")
    combined = pd.concat([wind_s, pv_s], axis=1).fillna(0.0).sort_values("Wind")
    fig, ax = plt.subplots(figsize=(10, 6), dpi=120)
    y = range(len(combined))
    ax.barh(y, combined["Wind"], color="#0ea5e9", label="Wind")
    ax.barh(y, combined["PV"], left=combined["Wind"], color="#f59e0b", label="PV")
    ax.set_yticks(list(y))
    ax.set_yticklabels(combined.index)
    ax.set_xlabel("Installed capacity [GW]")
    ax.set_title(
        f"Installed capacity by Bundesland — {SNAP.date()}\n"
        "(MaStR · full registry)"
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
        title_prefix="PV",
        cmap="YlOrRd",
        out_name="sample-pv-map.svg",
        unit="GWp",
    )
    render_map(
        records, units, "Wind",
        title_prefix="Wind",
        cmap="GnBu",
        out_name="sample-wind-map.svg",
    )
    render_growth(
        records, "Solare Strahlungsenergie",
        label="PV (all sizes)",
        color_fill="#f59e0b", color_line="#b45309",
        out_name="sample-pv-growth.svg",
        xlim_start="2005-01-01",
        xlim_end="2030-12-31",
        unit="GWp",
    )
    render_growth(
        records, "Wind",
        label="wind",
        color_fill="#86efac", color_line="#16a34a",
        out_name="sample-wind-growth.svg",
        xlim_start="1995-01-01",
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
                       "sample-pv-density.svg", "PV capacity density", unit="GWp")
    export_largest_plants(records, units, "largest-plants.json")
    render_pv_orientation(records, "sample-pv-orientation.svg")
    render_pv_orientation_polar("sample-pv-orientation-polar.svg")
    render_pv_orientation_polar_snapshot("sample-pv-orientation-polar-snapshot.svg")
    render_wind_age(records, "sample-wind-age.svg")

    # Wind: aggregate per project (owner_name proxy for SPV / Windpark)
    # before binning, since one turbine is rarely a project on its own.
    render_generation_by_size(
        records, "Wind",
        palette=["#cffafe", "#a5f3fc", "#67e8f9", "#22d3ee",
                 "#0891b2", "#155e75", "#1e3a8a"],
        tech_label="Wind", noun="turbines",
        out_name="sample-wind-by-size.svg",
        aggregate_by="owner_name",
    )
    render_generation_by_size(
        records, "Solare Strahlungsenergie",
        palette=["#fef3c7", "#fed7aa", "#fdba74", "#fb923c",
                 "#ea580c", "#9a3412", "#7c2d12"],
        tech_label="PV", noun="plants",
        out_name="sample-pv-by-size.svg",
        unit="GWp",
    )

    # Prefer the open-mastr SQLite snapshot (~2.5 M storage units) over the
    # 200 k JSON-scrape slice. Falls back to JSON-scrape if neither SQLite
    # nor a BESS parquet is present.
    try:
        bess_df, _ = mastr_plot.load_bess()
    except FileNotFoundError:
        print("BESS scrape not present — skipping BESS / PSH charts.")
    else:
        slices = mastr_plot.split_bess_storage(bess_df)
        batteries = slices["batteries"]
        psh = slices["psh"]

        # Battery-only charts (BESS in the strict sense; matches
        # battery-charts.de, BVES, EASE conventions).
        render_bess_charts(
            batteries, units,
            out_power="sample-bess-power-map.svg",
            out_energy="sample-bess-energy-map.svg",
            out_duration="sample-bess-duration.svg",
            out_growth="sample-bess-growth.svg",
        )
        render_bess_sector_charts(
            batteries,
            out_summary="sample-bess-sectors.svg",
            out_growth="sample-bess-sector-growth.svg",
            out_duration="sample-bess-sector-duration.svg",
        )
        render_bess_by_size(batteries, "sample-bess-by-size.svg")

        # Pumped-hydro storage reported separately.
        if len(psh):
            render_psh_charts(
                psh, units,
                out_map="sample-psh-map.svg",
                out_summary="sample-psh-summary.svg",
                out_top="sample-psh-top.svg",
            )

    # GWh trajectory chart uses the Zenodo bulk dump for complete historical
    # coverage (the 200k JSON-API cap misses ~1.5M older home batteries).
    render_bess_gwh_trajectory("sample-bess-gwh-trajectory.svg")

    print("Sample renders complete.")


def render_bess_gwh_trajectory(out_name: str) -> None:
    """Cumulative BESS GWh by sector — historical (Zenodo bulk) + planned forward zone.

    Why not use the 200k JSON scrape for history?
    The MaStR API sorts by commissioning date desc, so the 200k cap keeps only
    the ~2021-2026 tail and drops ~1.5M older home batteries. The Zenodo bulk
    (~1.75M rows) is required for accurate sector-level GWh history.

    The remaining gap vs battery-charts.de (21 GWh HSS vs ~15 GWh here) is
    MaStR under-registration: ~20-40% of residential batteries are never
    reported (Figgener et al. 2024).
    """
    # Prefer SQLite (full daily-fresh registry, ~2.5 M storage units) over the
    # frozen Zenodo parquet snapshot. Falls back to JSON-scrape only as last
    # resort; if no source resolves, we skip the chart cleanly.
    try:
        df, _ = mastr_plot.load_bess()
    except FileNotFoundError:
        print("No BESS source found — skipping GWh trajectory chart.")
        return

    b = df[df["storage_tech"] == "Batterie"].copy()

    # Classify by usable energy into HSS / CSS / LSS (battery-charts.de thresholds).
    def _sector(kwh):
        if pd.isna(kwh) or kwh <= 0:
            return None
        if kwh < 30:
            return "HSS (<30 kWh)"
        if kwh < 1000:
            return "CSS (30 kWh – 1 MWh)"
        return "LSS (≥1 MWh)"

    b["_sector"] = b["energy_kwh"].apply(_sector)
    b = b[b["_sector"].notna()].copy()

    SECTORS = ["HSS (<30 kWh)", "CSS (30 kWh – 1 MWh)", "LSS (≥1 MWh)"]
    COLORS_HIST = ["#86efac", "#fbbf24", "#a78bfa"]
    COLORS_PLAN = ["#bbf7d0", "#fde68a", "#ddd6fe"]

    # --- Historical: commissioned units from 2013 onwards ---
    # load_bess() returns tz-naive install_date / removal_date / planned_date.
    hist = b[
        b["install_date"].notna()
        & (b["install_date"] >= pd.Timestamp("2013-01-01"))
    ].copy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        hist["_month"] = hist["install_date"].dt.to_period("M")
    monthly_h = (
        hist.groupby(["_month", "_sector"])["energy_kwh"]
        .sum()
        .unstack(fill_value=0.0)
        .div(1e6)
    )
    for s in SECTORS:
        if s not in monthly_h.columns:
            monthly_h[s] = 0.0
    monthly_h = monthly_h[SECTORS]
    full_idx = pd.period_range(monthly_h.index.min(), monthly_h.index.max(), freq="M")
    monthly_h = monthly_h.reindex(full_idx, fill_value=0.0)
    cumhist = monthly_h.cumsum()
    last_date = cumhist.index.max()
    last_vals = cumhist.iloc[-1].copy()

    # --- Planned: entries with planned date but no commissioning date ---
    future = b[
        b["install_date"].isna()
        & b["planned_date"].notna()
        & b["removal_date"].isna()
        & (b["planned_date"] > last_date.to_timestamp())
        & (b["planned_date"] <= pd.Timestamp("2030-12-31"))
    ].copy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        future["_month"] = future["planned_date"].dt.to_period("M")
    has_future = len(future) > 0
    if has_future:
        monthly_f = (
            future.groupby(["_month", "_sector"])["energy_kwh"]
            .sum()
            .unstack(fill_value=0.0)
            .div(1e6)
        )
        for s in SECTORS:
            if s not in monthly_f.columns:
                monthly_f[s] = 0.0
        monthly_f = monthly_f[SECTORS]
        fut_idx = pd.period_range(monthly_f.index.min(), monthly_f.index.max(), freq="M")
        monthly_f = monthly_f.reindex(fut_idx, fill_value=0.0)
        cumfut = monthly_f.cumsum() + last_vals.values
        # Prepend the last historical row so lines/areas connect cleanly.
        join_row = pd.DataFrame([last_vals.values], columns=SECTORS,
                                index=pd.PeriodIndex([last_date], freq="M"))
        cumfut = pd.concat([join_row, cumfut])

    # --- Plot ---
    fig, ax = plt.subplots(figsize=(13, 5.5), dpi=120)

    hist_ts = cumhist.copy()
    hist_ts.index = cumhist.index.to_timestamp()

    # Stacked historical areas.
    bottoms = np.zeros(len(hist_ts))
    for s, c in zip(SECTORS, COLORS_HIST):
        vals = hist_ts[s].values
        ax.fill_between(hist_ts.index, bottoms, bottoms + vals,
                        color=c, alpha=0.85, label=s)
        bottoms += vals

    # Stacked planned areas (lighter, hatched).
    if has_future:
        fut_ts = cumfut.copy()
        fut_ts.index = cumfut.index.to_timestamp()
        bottoms_f = np.zeros(len(fut_ts))
        for s, c in zip(SECTORS, COLORS_PLAN):
            vals_f = fut_ts[s].values
            ax.fill_between(fut_ts.index, bottoms_f, bottoms_f + vals_f,
                            color=c, alpha=0.55, hatch="///",
                            label=f"{s.split('(')[0].strip()} — planned")
            bottoms_f += vals_f
        ax.axvline(last_date.to_timestamp(), color="#6b7280",
                   linewidth=1.0, linestyle=":", alpha=0.8)
        ax.text(last_date.to_timestamp(), ax.get_ylim()[1] * 0.5,
                "  MaStR\n  bulk\n  cutoff", color="#6b7280", fontsize=7.5, va="center")

    # Snap date marker.
    snap_ts = SNAP.to_pydatetime()
    ax.axvline(snap_ts, color="#dc2626", linewidth=1.5, linestyle="--", alpha=0.8)
    ymax = ax.get_ylim()[1]
    ax.text(snap_ts, ymax * 0.97,
            f" {SNAP.date()}", color="#dc2626", fontsize=8.5, va="top")

    total_hist = last_vals.sum()
    ax.set_ylabel("Cumulative installed GWh (usable)")
    ax.set_xlabel("")
    ax.set_title(
        "Germany BESS — cumulative installed energy by sector (MaStR Zenodo bulk dump)\n"
        f"Registered: {total_hist:.1f} GWh  ·  "
        "Market estimates ~21 GWh HSS (battery-charts.de / Figgener et al.) "
        "— gap = ~20-40% of residential batteries never registered in MaStR",
        fontsize=10,
    )
    ax.legend(loc="upper left", fontsize=8.5, ncol=2)
    ax.grid(alpha=0.3)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    for d in (FIG, DOCS):
        fig.savefig(d / out_name, format="svg", bbox_inches="tight")
    plt.close(fig)
    print(f"  GWh trajectory: {total_hist:.1f} GWh historical"
          + (f"  +{cumfut.sum(axis=1).iloc[-1] - total_hist:.1f} GWh planned" if has_future else ""))


if __name__ == "__main__":
    main()
