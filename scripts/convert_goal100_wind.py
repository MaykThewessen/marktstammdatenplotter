"""Convert the Goal100-corrected wind CSV into a parquet matching the
existing `wind.parquet` schema, so it can be used as a drop-in
historical-base replacement.

Goal100 (Zenodo 18697247, version 2026-02-19) supplies:
  - corrected coordinates (Manske 2025 overrides)
  - stable original approval dates (no operator overwrites)
  - cross-state permit info

Cutoff: 2026-02-19 — only 88 days behind today (2026-05-18), vs
the open-MaStR Zenodo dump (2025-02-05) which is ~ 15 months behind.

Output: BNetzA_MaStR/goal100-wind.parquet
Schema: matches BNetzA_MaStR/wind.parquet
  (mastr_id, gross_capacity_kw, status, wind_park, location_type,
   manufacturer, turbine_type, hub_height_m, rotor_diameter_m,
   bundesland, landkreis, municipality, municipality_key, longitude,
   latitude, commissioning_date, decommissioning_date, net_capacity_kw)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

SRC = Path(
    "/Users/mayk/DE_Wind_marktstammdatenplotter/marktstammdatenplotter/"
    "non-pv-data/goal100_mastr_wind_corrected_epsg_25832_2026_02_19/"
    "goal100_mastr_wind_corrected_epsg_25832_2026_02_19.csv"
)
DST = Path(
    "/Users/mayk/DE_Wind_marktstammdatenplotter/marktstammdatenplotter/"
    "BNetzA_MaStR/goal100-wind.parquet"
)

RENAME = {
    "einheit_mastr_nummer": "mastr_id",
    "nettonennleistung": "net_capacity_kw",
    "name_windpark": "wind_park",
    "einheit_betriebsstatus": "status",
    "wind_an_land_oder_auf_see": "location_type",
    "hersteller": "manufacturer",
    "typenbezeichnung": "turbine_type",
    "nabenhoehe": "hub_height_m",
    "rotordurchmesser": "rotor_diameter_m",
    "ags_gemeinde": "municipality_key",
    "bundesland": "bundesland",
    "landkreis": "landkreis",
    "gemeinde": "municipality",
    "lon_x": "longitude",
    "lat_y": "latitude",
    "datum_inbetriebnahme": "commissioning_date",
    "datum_endgueltige_stilllegung": "decommissioning_date",
}


def main():
    if not SRC.exists():
        sys.exit(f"missing Goal100 CSV: {SRC}")

    print(f"reading {SRC.name} ...")
    df = pd.read_csv(SRC)
    print(f"  raw rows: {len(df):,}")

    df = df.rename(columns=RENAME)

    # Goal100 column is `nettonennleistung` (kW). The historical
    # parquet schema expects both `net_capacity_kw` and `gross_capacity_kw`.
    # For wind, gross ≈ net so we duplicate; downstream charts mostly use
    # gross.
    df["gross_capacity_kw"] = df["net_capacity_kw"]

    # Dates: Goal100 ships ISO YYYY-MM-DD strings. Cast to tz-aware UTC.
    for col in ("commissioning_date", "decommissioning_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)

    # `installed_capacity_kw` is present in BNetzA wind.parquet too but
    # not in Goal100 — copy from gross so the schema lines up.
    df["installed_capacity_kw"] = df["gross_capacity_kw"]

    # Trim to the schema set used elsewhere (drop Goal100-only columns
    # the downstream pipeline doesn't consume).
    keep = [
        "mastr_id", "gross_capacity_kw", "status", "wind_park",
        "location_type", "manufacturer", "turbine_type",
        "hub_height_m", "rotor_diameter_m",
        "bundesland", "landkreis", "municipality", "municipality_key",
        "longitude", "latitude",
        "commissioning_date", "decommissioning_date",
        "net_capacity_kw", "installed_capacity_kw",
    ]
    df = df[[c for c in keep if c in df.columns]]

    DST.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(DST, compression="zstd", index=False)

    size_mb = DST.stat().st_size / 1e6
    on = (df["location_type"] == "Windkraft an Land").sum()
    off = (df["location_type"] == "Windkraft auf See").sum()
    gw_total = df["gross_capacity_kw"].sum() / 1e6
    max_date = df["commissioning_date"].max()
    print(f"  wrote {DST}  {size_mb:.1f} MB")
    print(f"  rows: {len(df):,}  ·  onshore: {on:,}  ·  offshore: {off:,}")
    print(f"  total {gw_total:.2f} GW  ·  max install_date: {max_date}")


if __name__ == "__main__":
    main()
