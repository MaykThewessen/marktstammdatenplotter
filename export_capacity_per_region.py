"""
Export total wind capacity per smallest administrative region, with
historical snapshots as separate columns.

Data sources (merged):
  1. Zenodo base  — goal100 corrected MaStR wind dataset (2026-02-19)
  2. MaStR API    — incremental JSON files from non-pv-data/mastr-api/
                    (fetched via: python fetch_mastr.py --energy wind --mode incremental)

Sources:
  Zenodo: DOI 10.5281/zenodo.18697247
  MaStR API: https://www.marktstammdatenregister.de

Output columns:
  region, bundesland, type, turbines,
  capacity_MW_<snapshot>  for each snapshot date

Snapshot dates are defined in SNAPSHOTS below.
"""

import glob
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
ZENODO_CSV = (
    "non-pv-data/goal100_mastr_wind_corrected_epsg_25832_2026_02_19/"
    "goal100_mastr_wind_corrected_epsg_25832_2026_02_19.csv"
)
ZENODO_DATE   = "2026-02-19"
KREISE_GPKG   = "germany_kreise.gpkg"
API_JSON_GLOB = "non-pv-data/mastr-api/mastr_wind_incremental_*.json"

SNAPSHOTS = {
    "2013": pd.Timestamp("2013-12-31"),
    "2024": pd.Timestamp("2024-12-31"),
    "2025": pd.Timestamp("2025-12-31"),
    "latest": None,   # None = use all active, ignore removal_date threshold
}

# Nordsee bounding box (lon 3–9, lat 53–56) to classify API offshore entries
# whose bundesland is missing
def infer_sea_from_coords(lon, lat) -> str | None:
    if lon is None or lat is None:
        return None
    if 3 <= lon <= 9 and 53 <= lat <= 56:
        return "Nordsee"
    if 9 < lon <= 15 and 53 <= lat <= 56:
        return "Ostsee"
    return None

def offshore_label(bundesland, lon=None, lat=None) -> str | None:
    bl = str(bundesland) if bundesland else ""
    if "Nordsee" in bl or "Niedersachsen, Küstenmeer" in bl:
        return "Nordsee"
    if "Ostsee" in bl or "Mecklenburg-Vorpommern, Küstenmeer" in bl:
        return "Ostsee"
    return infer_sea_from_coords(lon, lat)   # fallback for API entries with no bundesland

def parse_dotnet_date(s) -> datetime | None:
    if not s:
        return None
    try:
        ms = int(str(s).strip("/Date()").split("+")[0].split("-")[0])
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (ValueError, AttributeError):
        return None

# ── 1. Load Zenodo base ───────────────────────────────────────────────────────
print("Loading Zenodo base dataset...")
raw = pd.read_csv(ZENODO_CSV)
zenodo = pd.DataFrame({
    "mastr_id":    raw["einheit_mastr_nummer"],
    "install_date": pd.to_datetime(raw["datum_inbetriebnahme"], errors="coerce"),
    "removal_date": pd.to_datetime(raw["datum_endgueltige_stilllegung"], errors="coerce"),
    "capacity_kW":  raw["nettonennleistung"],
    "status":       raw["einheit_betriebsstatus"],
    "landkreis":    raw["landkreis"],
    "bundesland":   raw["bundesland"],
    "longitude":    raw["lon_x"],
    "latitude":     raw["lat_y"],
    "offshore":     raw["wind_an_land_oder_auf_see"].apply(
                        lambda v: "offshore" if v == "Windkraft auf See" else "land"),
    "source":       "zenodo",
})
print(f"  {len(zenodo):,} entries")

# Build landkreis → bundesland lookup (for filling missing bundesland in API entries)
kreis_to_bl = (
    zenodo.dropna(subset=["landkreis", "bundesland"])
    .drop_duplicates("landkreis")
    .set_index("landkreis")["bundesland"]
)

# ── 2. Load incremental API files ─────────────────────────────────────────────
api_files = sorted(glob.glob(API_JSON_GLOB))
api_rows  = []
latest_api_date = None

if api_files:
    latest_api_date = max(
        re.search(r"(\d{4}-\d{2}-\d{2})", Path(f).name).group(1)
        for f in api_files
    )
    zenodo_ids = set(zenodo["mastr_id"].astype(str))

    for path in api_files:
        data = json.load(open(path))
        for e in data["Data"]:
            mid = str(e.get("Id") or e.get("EinheitMastrNummer", ""))
            if mid in zenodo_ids:
                continue
            lon = e.get("Laengengrad")
            lat = e.get("Breitengrad")
            is_offshore = e.get("WindAnLandOderSeeId") == 889
            def _ts(s):
                dt = parse_dotnet_date(s)
                return pd.Timestamp(dt).tz_localize(None) if dt else pd.NaT

            api_rows.append({
                "mastr_id":    mid,
                "install_date": _ts(e.get("InbetriebnahmeDatum")),
                "removal_date": _ts(e.get("EndgueltigeStilllegungDatum")),
                "capacity_kW":  e.get("Nettonennleistung") or e.get("Bruttoleistung"),
                "status":       e.get("EinheitBetriebsstatusName"),
                "landkreis":    e.get("LandkreisName"),
                "bundesland":   e.get("BundeslandName"),
                "longitude":    lon,
                "latitude":     lat,
                "offshore":     "offshore" if is_offshore else "land",
                "source":       "mastr-api",
            })

    print(f"  {len(api_rows)} new entries from {len(api_files)} API file(s)")
else:
    print("  No API files found — Zenodo only")

api_df = pd.DataFrame(api_rows) if api_rows else pd.DataFrame(columns=zenodo.columns)

# ── 3. Spatial join + bundesland fill for API land entries ────────────────────
land_api = api_df[api_df["offshore"] == "land"].copy() if not api_df.empty else pd.DataFrame()

if not land_api.empty:
    # Spatial join to assign landkreis
    no_kreise = land_api[land_api["landkreis"].isna() & land_api["longitude"].notna()]
    if not no_kreise.empty:
        print(f"\nSpatial join: {len(no_kreise)} API land entries...")
        kreise_gdf = gpd.read_file(KREISE_GPKG, layer="multipolygons")
        pts = gpd.GeoDataFrame(
            no_kreise,
            geometry=gpd.points_from_xy(no_kreise["longitude"], no_kreise["latitude"]),
            crs="EPSG:4326",
        )
        joined = gpd.sjoin(pts, kreise_gdf[["name", "geometry"]], how="left", predicate="within")
        api_df.loc[no_kreise.index, "landkreis"] = joined["name"].values
        print(f"  Matched {joined['name'].notna().sum()}/{len(no_kreise)}")

    # Fill missing bundesland from lookup
    missing_bl = api_df[(api_df["offshore"] == "land") & api_df["bundesland"].isna() & api_df["landkreis"].notna()]
    api_df.loc[missing_bl.index, "bundesland"] = (
        missing_bl["landkreis"].map(kreis_to_bl)
    )

# Fix offshore sea area for API entries where bundesland is still missing
if not api_df.empty:
    off_api = api_df[api_df["offshore"] == "offshore"]
    for idx, row in off_api.iterrows():
        if pd.isna(row["bundesland"]) or row["bundesland"] is None:
            sea = infer_sea_from_coords(row["longitude"], row["latitude"])
            api_df.loc[idx, "bundesland"] = sea   # store inferred sea name as bundesland

# ── 4. Merge and compute capacity_kW column ───────────────────────────────────
all_turbines = pd.concat([zenodo, api_df], ignore_index=True)
all_turbines["capacity_kW"] = pd.to_numeric(all_turbines["capacity_kW"], errors="coerce")
all_turbines["capacity_MW"] = all_turbines["capacity_kW"] / 1000

# Offshore: derive unified sea area label from bundesland
all_turbines["sea_area"] = all_turbines.apply(
    lambda r: offshore_label(r["bundesland"], r["longitude"], r["latitude"])
    if r["offshore"] == "offshore" else None,
    axis=1,
)

# Canonical region key: landkreis for land, sea_area for offshore
all_turbines["region"] = all_turbines.apply(
    lambda r: r["sea_area"] if r["offshore"] == "offshore" else r["landkreis"],
    axis=1,
)

# Bundesland key: sea area for offshore (self-referential but consistent)
all_turbines.loc[all_turbines["offshore"] == "offshore", "bundesland"] = \
    all_turbines.loc[all_turbines["offshore"] == "offshore", "sea_area"]

print(f"\nTotal turbines merged: {len(all_turbines):,}")
print(f"  Null region: {all_turbines['region'].isna().sum()} (no coords + not matched)")

# ── 5. Active turbines (currently operating) ──────────────────────────────────
STATUS_ACTIVE   = {"In Betrieb"}
STATUS_INACTIVE = {"Endgültig stillgelegt", "Vorübergehend stillgelegt"}

def is_active_on(df: pd.DataFrame, snapshot: pd.Timestamp | None) -> pd.Series:
    """Return boolean mask for turbines active on `snapshot` (None = latest)."""
    # Must be commissioned
    commissioned = df["install_date"].notna() & (
        df["install_date"] <= snapshot if snapshot else df["install_date"].notna()
    )
    # Must not be decommissioned before snapshot
    not_removed = df["removal_date"].isna() | (
        df["removal_date"] > snapshot if snapshot else True
    )
    # Exclude explicitly inactive status (only applies when status is known)
    not_inactive = ~df["status"].isin(STATUS_INACTIVE)
    return commissioned & not_removed & not_inactive

# ── 6. Build per-region aggregation for each snapshot ─────────────────────────
# Identify all unique (region, bundesland, type) combos across all active turbines
latest_active = all_turbines[is_active_on(all_turbines, None)].copy()
regions_index = (
    latest_active.dropna(subset=["region"])
    .groupby(["region", "bundesland", "offshore"])
    .agg(turbines=("capacity_MW", "count"))
    .reset_index()
    .rename(columns={"offshore": "type"})
)

result = regions_index.copy()

for snap_name, snap_date in SNAPSHOTS.items():
    active = all_turbines[is_active_on(all_turbines, snap_date)]
    agg = (
        active.dropna(subset=["region"])
        .groupby(["region", "bundesland", "offshore"])
        ["capacity_MW"].sum()
        .reset_index()
        .rename(columns={"offshore": "type", "capacity_MW": f"capacity_MW_{snap_name}"})
    )
    result = result.merge(agg, on=["region", "bundesland", "type"], how="left")

# Fill NaN with 0 for snapshots where a region had no wind yet
cap_cols = [c for c in result.columns if c.startswith("capacity_MW_")]
result[cap_cols] = result[cap_cols].fillna(0).round(3)
result = result.sort_values(["type", "capacity_MW_latest"], ascending=[True, False]).reset_index(drop=True)

# ── 7. Validate & report ──────────────────────────────────────────────────────
print(f"\n=== Validation ===")
dups = result[result.duplicated("region", keep=False)]
print(f"Duplicate region names: {len(dups)} ({dups['region'].unique().tolist() if not dups.empty else 'none'})")
print(f"Null regions: {result['region'].isna().sum()}")
print(f"\nTotals per snapshot:")
for col in cap_cols:
    snap = col.replace("capacity_MW_", "")
    total = result[col].sum()
    n_turbines = f"  turbines={int(result['turbines'].sum())}" if snap == "latest" else ""
    print(f"  {snap:8s}: {total:8.1f} MW  ({total/1000:.2f} GW){n_turbines}")
print(f"\nTop 10 regions (latest):")
print(result.nlargest(10, "capacity_MW_latest")
      [["region", "bundesland", "turbines"] + cap_cols].to_string(index=False))

# ── 8. Save ───────────────────────────────────────────────────────────────────
api_part = f"_API_{latest_api_date}" if latest_api_date else ""
OUTPUT = f"DE_wind_installed_capacity_per_region_Zenodo_{ZENODO_DATE}{api_part}.csv"
result.to_csv(OUTPUT, index=False)
print(f"\nSaved {len(result)} regions → '{OUTPUT}'")
