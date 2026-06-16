"""Run the wind notebook logic as a plain Python script."""
# ── Cell 1: Load wind data from the open-mastr SQLite snapshot ────────────────
import pandas as pd
import mastr_db

# Bulk MaStR XML -> SQLite, refreshable to daily-fresh with `pixi run db-mastr-core`.
# Replaces the frozen Zenodo CSV (record 18697247, v2026_02_19).
raw = mastr_db.load("wind", columns=[
    "Inbetriebnahmedatum", "DatumEndgueltigeStilllegung", "Nettonennleistung",
    "Laengengrad", "Breitengrad", "Seelage",
    "Bundesland", "Landkreis", "EinheitBetriebsstatus",
])

WIND = pd.DataFrame({
    "install_date": pd.to_datetime(raw["Inbetriebnahmedatum"], errors="coerce"),
    "removal_date": pd.to_datetime(raw["DatumEndgueltigeStilllegung"], errors="coerce"),
    "power":        raw["Nettonennleistung"],   # kW (net == gross for wind)
    "longitude":    raw["Laengengrad"],
    "latitude":     raw["Breitengrad"],
    "energy_type":  "Wind",
    # Offshore zone straight from the decoded Seelage column (Nordsee / Ostsee /
    # None); replaces the old bundesland-string heuristic.
    "off_shore":    raw["Seelage"],
    "bundesland":   raw["Bundesland"],
    "landkreis":    raw["Landkreis"],
    "status":       raw["EinheitBetriebsstatus"],
})

print(f"Loaded {len(WIND)} wind turbines")
print(f"Status: {WIND['status'].value_counts().to_dict()}")
print(f"Offshore: Nordsee={( WIND['off_shore']=='Nordsee').sum()}, Ostsee={(WIND['off_shore']=='Ostsee').sum()}")

# ── Cell 2: Load map data ─────────────────────────────────────────────────────
import numpy as np
import geopandas as gpd
from shapely.geometry import Point, Polygon, MultiPolygon
import matplotlib
matplotlib.use("Agg")          # headless — saves PNGs instead of showing
import matplotlib.pyplot as plt
from datetime import date, datetime
import warnings
import mapclassify
import matplotlib.colors as mcolors
import matplotlib.patches as mpatches

gpkg_file_path = "germany_kreise.gpkg"
germany_admin_units = gpd.read_file(gpkg_file_path, layer="multipolygons")
print(f"\nLoaded {len(germany_admin_units)} admin units from '{gpkg_file_path}'")
print(f"admin_level=4 (city-states): {germany_admin_units[germany_admin_units['admin_level']=='4']['name'].tolist()}")

# ── Cell 3: Plot function ─────────────────────────────────────────────────────
plt.rcParams["figure.dpi"] = 150   # lower for faster test runs

def plot_wind_power_by_county(plot_date: date, bins=None, save_plot=False):
    if germany_admin_units is None or germany_admin_units.empty:
        print("No admin units loaded.")
        return

    plot_date_ts = pd.Timestamp(plot_date)
    active = WIND[
        (WIND["install_date"] <= plot_date_ts) &
        (WIND["removal_date"].isna() | (WIND["removal_date"] > plot_date_ts))
    ].copy()
    active["power"] = active["power"] / 1e6     # kW → GW

    germany_uid = germany_admin_units.reset_index().rename(columns={"index": "unique_id"}).copy()

    nordsee_poly = Polygon([(8.0, 54.2), (8.5, 54.2), (8.5, 53.9), (8.0, 53.9)])
    ostsee_poly  = Polygon([(11.5, 54.6), (12.0, 54.6), (12.0, 54.3), (11.5, 54.3)])
    offshore_gdf = gpd.GeoDataFrame({
        "name": ["Nordsee", "Ostsee"],
        "admin_level": ["off_shore", "off_shore"],
        "geometry": [nordsee_poly, ostsee_poly],
        "unique_id": ["nordsee_offshore", "ostsee_offshore"],
    }, crs=germany_admin_units.crs)

    combined = pd.concat([germany_uid[["name", "geometry", "unique_id"]], offshore_gdf], ignore_index=True)
    power_by_uid = pd.Series(0.0, index=combined["unique_id"].values)
    uid_to_name = germany_uid.set_index("unique_id")["name"]

    land = active[active["off_shore"].isna()]
    nordsee = active[active["off_shore"] == "Nordsee"]
    ostsee  = active[active["off_shore"] == "Ostsee"]

    print(f"\n{plot_date}  active={len(active)}  land={len(land)}  Nordsee={len(nordsee)}  Ostsee={len(ostsee)}")
    print(f"  Total power: {active['power'].sum():.2f} GW")

    if not land.empty:
        gdf_land = gpd.GeoDataFrame(land, geometry=gpd.points_from_xy(land["longitude"], land["latitude"]), crs=germany_admin_units.crs)
        joined = gpd.sjoin(gdf_land, germany_uid[["unique_id", "geometry"]], how="inner", predicate="within")
        if not joined.empty:
            power_by_uid.update(joined.groupby("unique_id")["power"].sum())
        top3 = power_by_uid.drop(["nordsee_offshore","ostsee_offshore"], errors="ignore").nlargest(3)
        print("  Top 3 counties:", [(uid_to_name.get(uid, uid), f"{pw:.2f} GW") for uid, pw in top3.items()])

    if not nordsee.empty:
        power_by_uid["nordsee_offshore"] += nordsee["power"].sum()
    if not ostsee.empty:
        power_by_uid["ostsee_offshore"] += ostsee["power"].sum()

    merged = combined.merge(power_by_uid.rename("total_wind_power"), left_on="unique_id", right_index=True, how="left")
    merged["total_wind_power"] = merged["total_wind_power"].fillna(0)

    EPSILON = 1e-6
    if bins is None:
        pdata = merged["total_wind_power"]
        if pdata.sum() > 0 and len(pdata.unique()) > 1:
            try:
                mb = mapclassify.NaturalBreaks(pdata, k=8)
                plot_bins = [pdata.min()] + mb.bins.tolist()
                if abs(plot_bins[0]) < EPSILON:
                    plot_bins[0] = 0.0
            except ValueError:
                plot_bins = [0, pdata.max() or 1]
        else:
            plot_bins = [0, pdata.max() or 1]
    else:
        plot_bins = bins

    fig, ax = plt.subplots(figsize=(12, 14))
    merged.plot(column="total_wind_power", cmap="viridis", linewidth=0.1,
                edgecolor="black", ax=ax, legend=False,
                scheme="UserDefined", classification_kwds={"bins": plot_bins})
    ax.set_title(f"Windkraft ({plot_date})", fontsize=16)
    ax.set_axis_off()

    cmap = plt.cm.get_cmap("viridis")
    norm = mcolors.BoundaryNorm(plot_bins, cmap.N)
    patches = [mpatches.Patch(color=cmap(norm((plot_bins[i]+plot_bins[i+1])/2)),
                              label=f"{plot_bins[i]:.2f}–{plot_bins[i+1]:.2f} GW")
               for i in range(len(plot_bins)-1)]
    ax.legend(handles=patches, loc="lower center", ncol=4, bbox_to_anchor=(0.5, -0.02), frameon=False, fontsize=9)
    plt.subplots_adjust(bottom=0.15)

    if save_plot:
        import os; os.makedirs("fig", exist_ok=True)
        fname = f"fig/wind-{plot_date.strftime('%Y-%m-%d')}.png"
        plt.savefig(fname, dpi=fig.dpi, bbox_inches="tight", pad_inches=0.1)
        print(f"  Saved → {fname}")
        plt.close()
    else:
        plt.show()
    return plot_bins

# ── Cell 4: Single test plot ──────────────────────────────────────────────────
print("\n=== Test plot: 2025-01-01 ===")
BINS = plot_wind_power_by_county(date(2025, 1, 1), save_plot=True)
print(f"Bins: {[f'{b:.3f}' for b in BINS]}")
print("\nDone! Check fig/wind-2025-01-01.png")
