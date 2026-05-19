"""
Process the open-MaStR Zenodo dump into clean parquet files.

Source: https://zenodo.org/records/14783581
Downloaded to: BNetzA_MaStR/

Why not fetch_mastr.py for solar?
  The MaStR web API endpoint (GetErweiterteOeffentlicheEinheitStromerzeugung
  with forExport=true) hard-caps at ~200,000 records server-side regardless
  of pagination. Solar has ~4.9M entries — the API only returns the first 200k
  in default sort order. This script reads the full bulk export directly.

Outputs (written to BNetzA_MaStR/):
  wind.parquet     ~40k turbines
  solar.parquet    ~4.9M PV units
  storage.parquet  ~1.75M storage units (batteries + pumped hydro)
                   includes NutzbareSpeicherkapazitaet joined from storage_units
"""

from pathlib import Path

import pandas as pd

DATA_DIR = Path("BNetzA_MaStR")

WIND_CSV          = DATA_DIR / "bnetza_mastr_wind_raw.csv"
SOLAR_CSV         = DATA_DIR / "bnetza_mastr_solar_raw.csv"
STORAGE_CSV       = DATA_DIR / "bnetza_mastr_storage_raw.csv"
STORAGE_UNITS_CSV = DATA_DIR / "bnetza_mastr_storage_units_raw.csv"

COMMON_COLS = {
    "EinheitMastrNummer":          "mastr_id",
    "Inbetriebnahmedatum":         "commissioning_date",
    "DatumEndgueltigeStilllegung": "decommissioning_date",
    "EinheitBetriebsstatus":       "status",
    "Nettonennleistung":           "net_capacity_kw",
    "Bruttoleistung":              "gross_capacity_kw",
    "Bundesland":                  "bundesland",
    "Landkreis":                   "landkreis",
    "Gemeinde":                    "municipality",
    "Gemeindeschluessel":          "municipality_key",
    "Laengengrad":                 "longitude",
    "Breitengrad":                 "latitude",
    "Lage":                        "location_type",
}

WIND_EXTRA = {
    "NameWindpark":    "wind_park",
    "Nabenhoehe":      "hub_height_m",
    "Rotordurchmesser":"rotor_diameter_m",
    "Hersteller":      "manufacturer",
    "Typenbezeichnung":"turbine_type",
}

SOLAR_EXTRA = {
    "Nutzungsbereich":    "usage_type",
    "Hauptausrichtung":   "orientation",
    "AnzahlModule":       "module_count",
    "InstallierteLeistung":"installed_capacity_kw",
}

STORAGE_EXTRA = {
    "Technologie":                    "technology",
    "Batterietechnologie":            "battery_type",
    "Pumpspeichertechnologie":        "pumped_hydro_type",
    "AcDcKoppelung":                  "ac_dc_coupling",
    "Einsatzort":                     "use_case",
    "LeistungsaufnahmeBeimEinspeichern": "charge_power_kw",
}

NUMERIC_COLS = [
    "net_capacity_kw", "gross_capacity_kw", "installed_capacity_kw",
    "hub_height_m", "rotor_diameter_m", "latitude", "longitude", "module_count",
    "charge_power_kw", "usable_capacity_kwh",
]
DATE_COLS    = ["commissioning_date", "decommissioning_date"]
CATEGORY_COLS = ["status", "bundesland", "landkreis", "location_type", "usage_type",
                 "orientation", "manufacturer", "turbine_type",
                 "technology", "battery_type", "pumped_hydro_type", "ac_dc_coupling", "use_case"]


def load(csv_path: Path, col_map: dict) -> pd.DataFrame:
    print(f"\nReading {csv_path.name} ...")
    df = pd.read_csv(
        csv_path,
        usecols=list(col_map.keys()),
        dtype=str,
        low_memory=False,
    )
    df = df.rename(columns=col_map)

    for col in DATE_COLS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)

    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in CATEGORY_COLS:
        if col in df.columns:
            df[col] = df[col].astype("category")

    mb = df.memory_usage(deep=True).sum() / 1e6
    print(f"  {len(df):,} rows  |  {len(df.columns)} columns  |  {mb:.0f} MB in memory")
    return df


def print_stats(df: pd.DataFrame, tech: str) -> None:
    active = df[df["status"] == "In Betrieb"]
    cap_gw = active["net_capacity_kw"].sum() / 1_000_000

    print(f"\n{'═'*50}")
    print(f"  {tech.upper()}")
    print(f"{'═'*50}")
    print(f"  Total entries:          {len(df):>10,}")
    print(f"  Active (In Betrieb):    {len(active):>10,}")
    print(f"  Decommissioned:         {df['decommissioning_date'].notna().sum():>10,}")
    print(f"  Active net capacity:    {cap_gw:>10.1f} GW")

    if "commissioning_date" in active.columns:
        by_year = (
            active.dropna(subset=["commissioning_date"])
            .assign(year=lambda d: d["commissioning_date"].dt.year)
            .groupby("year", observed=True)["net_capacity_kw"]
            .sum()
            .div(1_000_000)
        )
        print(f"\n  Added capacity (GW) — last 10 years:")
        print(by_year.tail(10).rename("GW_added").to_frame().to_string())

    if "bundesland" in active.columns:
        by_bl = (
            active.groupby("bundesland", observed=True)["net_capacity_kw"]
            .sum()
            .div(1_000_000)
            .sort_values(ascending=False)
            .rename("GW")
        )
        print(f"\n  Capacity by Bundesland (GW):")
        print(by_bl.to_frame().to_string())

    if tech == "solar" and "location_type" in active.columns:
        by_lage = (
            active.groupby("location_type", observed=True)["net_capacity_kw"]
            .agg(count="count", capacity_gw=lambda x: x.sum() / 1_000_000)
        )
        print(f"\n  By location type:")
        print(by_lage.to_string())

    if tech == "wind" and "location_type" in active.columns:
        by_lage = (
            active.groupby("location_type", observed=True)["net_capacity_kw"]
            .agg(count="count", capacity_gw=lambda x: x.sum() / 1_000_000)
        )
        print(f"\n  Onshore vs offshore:")
        print(by_lage.to_string())


def print_storage_stats(df: pd.DataFrame) -> None:
    active = df[df["status"] == "In Betrieb"]
    cap_gw  = active["net_capacity_kw"].sum() / 1_000_000
    cap_gwh = active["usable_capacity_kwh"].sum() / 1_000_000

    print(f"\n{'═'*50}")
    print(f"  STORAGE")
    print(f"{'═'*50}")
    print(f"  Total entries:          {len(df):>10,}")
    print(f"  Active (In Betrieb):    {len(active):>10,}")
    print(f"  Decommissioned:         {df['decommissioning_date'].notna().sum():>10,}")
    print(f"  Active discharge cap:   {cap_gw:>10.1f} GW")
    print(f"  Active usable storage:  {cap_gwh:>10.1f} GWh")

    if "commissioning_date" in active.columns:
        by_year = (
            active.dropna(subset=["commissioning_date"])
            .assign(year=lambda d: d["commissioning_date"].dt.year)
            .groupby("year", observed=True)[["net_capacity_kw", "usable_capacity_kwh"]]
            .sum()
            .div(1_000_000)
            .rename(columns={"net_capacity_kw": "GW_added", "usable_capacity_kwh": "GWh_added"})
        )
        print(f"\n  Added capacity — last 10 years:")
        print(by_year.tail(10).to_string())

    if "technology" in active.columns:
        by_tech = (
            active.groupby("technology", observed=True)
            .agg(
                count=("net_capacity_kw", "count"),
                discharge_gw=("net_capacity_kw", lambda x: x.sum() / 1_000_000),
                usable_gwh=("usable_capacity_kwh", lambda x: x.sum() / 1_000_000),
            )
            .sort_values("discharge_gw", ascending=False)
        )
        print(f"\n  By technology:")
        print(by_tech.to_string())

    if "battery_type" in active.columns:
        batteries = active[active["technology"] == "Batterie"]
        by_btype = (
            batteries.groupby("battery_type", observed=True)
            .agg(
                count=("net_capacity_kw", "count"),
                discharge_gw=("net_capacity_kw", lambda x: x.sum() / 1_000_000),
                usable_gwh=("usable_capacity_kwh", lambda x: x.sum() / 1_000_000),
            )
            .sort_values("discharge_gw", ascending=False)
        )
        print(f"\n  Battery chemistry breakdown:")
        print(by_btype.to_string())

    if "bundesland" in active.columns:
        by_bl = (
            active.groupby("bundesland", observed=True)
            .agg(
                discharge_gw=("net_capacity_kw", lambda x: x.sum() / 1_000_000),
                usable_gwh=("usable_capacity_kwh", lambda x: x.sum() / 1_000_000),
            )
            .sort_values("discharge_gw", ascending=False)
        )
        print(f"\n  By Bundesland:")
        print(by_bl.to_string())


def process_storage() -> None:
    out = DATA_DIR / "storage.parquet"
    if out.exists():
        mtime = max(STORAGE_CSV.stat().st_mtime, STORAGE_UNITS_CSV.stat().st_mtime)
        if out.stat().st_mtime > mtime:
            print(f"\n{out} up-to-date, skipping conversion.")
            df = pd.read_parquet(out)
            print_storage_stats(df)
            return

    storage_cols = {**COMMON_COLS, **STORAGE_EXTRA}
    # COMMON_COLS has "Lage" → location_type, but storage has no Lage column — drop it
    storage_cols = {k: v for k, v in storage_cols.items() if k != "Lage"}

    df = load(STORAGE_CSV, storage_cols)

    # Join usable capacity from storage_units (one-to-many: aggregate per parent unit)
    print(f"\nReading {STORAGE_UNITS_CSV.name} for usable capacity ...")
    units = pd.read_csv(
        STORAGE_UNITS_CSV,
        usecols=["VerknuepfteEinheit", "NutzbareSpeicherkapazitaet"],
        dtype={"VerknuepfteEinheit": str, "NutzbareSpeicherkapazitaet": str},
    )
    units["NutzbareSpeicherkapazitaet"] = pd.to_numeric(
        units["NutzbareSpeicherkapazitaet"], errors="coerce"
    )
    cap_per_unit = (
        units.groupby("VerknuepfteEinheit")["NutzbareSpeicherkapazitaet"]
        .sum()
        .rename("usable_capacity_kwh")
    )
    df = df.merge(cap_per_unit, left_on="mastr_id", right_index=True, how="left")
    df["usable_capacity_kwh"] = pd.to_numeric(df["usable_capacity_kwh"], errors="coerce")
    print(f"  Usable capacity matched: {df['usable_capacity_kwh'].notna().sum():,} / {len(df):,} units")

    df.to_parquet(out, index=False, compression="snappy")
    size_mb = out.stat().st_size / 1e6
    print(f"  Saved → {out}  ({size_mb:.0f} MB)")
    print_storage_stats(df)


def process(csv_path: Path, col_map: dict, tech: str) -> None:
    out = DATA_DIR / f"{tech}.parquet"
    if out.exists():
        mtime_csv = csv_path.stat().st_mtime
        mtime_out = out.stat().st_mtime
        if mtime_out > mtime_csv:
            print(f"\n{out} up-to-date, skipping conversion.")
            df = pd.read_parquet(out)
            print_stats(df, tech)
            return

    df = load(csv_path, col_map)
    df.to_parquet(out, index=False, compression="snappy")
    size_mb = out.stat().st_size / 1e6
    print(f"  Saved → {out}  ({size_mb:.0f} MB)")
    print_stats(df, tech)


if __name__ == "__main__":
    wind_cols  = {**COMMON_COLS, **WIND_EXTRA}
    solar_cols = {**COMMON_COLS, **SOLAR_EXTRA}

    process(WIND_CSV,  wind_cols,  "wind")
    process(SOLAR_CSV, solar_cols, "solar")
    process_storage()
