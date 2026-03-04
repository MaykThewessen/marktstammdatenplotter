"""
Export latest total wind capacity per smallest administrative region.

Land turbines   → grouped by Landkreis (directly from Zenodo column)
Offshore turbines → grouped by sea area (Nordsee / Ostsee / Küstenmeer)

Source: Zenodo DOI 10.5281/zenodo.18697247
        "Corrected and supplemented unit data on approved wind turbines in Germany"
        Version 2026_02_19 — based on Marktstammdatenregister (MaStR)

Output filename: DE_wind_installed_capacity_per_region_MaStR_Zenodo_upto_<date>.csv
Columns: region, bundesland, type, turbines, capacity_MW
"""

import pandas as pd

# Dataset version date is encoded in the filename
ZENODO_VERSION_DATE = "2026-02-19"

CSV = (
    "non-pv-data/goal100_mastr_wind_corrected_epsg_25832_2026_02_19/"
    "goal100_mastr_wind_corrected_epsg_25832_2026_02_19.csv"
)

raw = pd.read_csv(CSV)

# Only currently operating turbines
active = raw[raw["einheit_betriebsstatus"] == "In Betrieb"].copy()
active["capacity_MW"] = active["nettonennleistung"] / 1000

print(f"Active turbines: {len(active)}")

# ── Land turbines ─────────────────────────────────────────────────────────────
land = active[active["wind_an_land_oder_auf_see"] == "Windkraft an Land"]

land_agg = (
    land.groupby(["landkreis", "bundesland"], dropna=False)
    .agg(turbines=("capacity_MW", "count"), capacity_MW=("capacity_MW", "sum"))
    .reset_index()
    .rename(columns={"landkreis": "region"})
)
land_agg["type"] = "land"

# ── Offshore turbines ─────────────────────────────────────────────────────────
offshore = active[active["wind_an_land_oder_auf_see"] == "Windkraft auf See"]

offshore_agg = (
    offshore.groupby("bundesland")
    .agg(turbines=("capacity_MW", "count"), capacity_MW=("capacity_MW", "sum"))
    .reset_index()
    .rename(columns={"bundesland": "region"})
)
offshore_agg["bundesland"] = offshore_agg["region"]
offshore_agg["type"] = "offshore"

# ── Combine and sort ──────────────────────────────────────────────────────────
result = (
    pd.concat([land_agg, offshore_agg], ignore_index=True)
    [["region", "bundesland", "type", "turbines", "capacity_MW"]]
    .sort_values(["type", "capacity_MW"], ascending=[True, False])
    .reset_index(drop=True)
)

result["capacity_MW"] = result["capacity_MW"].round(3)

OUTPUT = f"DE_wind_installed_capacity_per_region_MaStR_Zenodo_upto_{ZENODO_VERSION_DATE}.csv"
result.to_csv(OUTPUT, index=False)

print(f"\nExported {len(result)} regions to '{OUTPUT}'")
print(f"  Land regions:    {(result['type']=='land').sum()}")
print(f"  Offshore regions: {(result['type']=='offshore').sum()}")
print(f"\nTotal installed capacity: {result['capacity_MW'].sum():.1f} MW "
      f"({result['capacity_MW'].sum()/1000:.2f} GW)")
print(f"\nTop 10 regions by capacity:")
print(result.nlargest(10, "capacity_MW")
      [["region", "bundesland", "turbines", "capacity_MW"]].to_string(index=False))
