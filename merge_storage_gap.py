"""Merge incremental MaStR API storage JSON into the Zenodo storage.parquet.

Usage:
    # 1. Fetch the gap first:
    python fetch_mastr.py --energy storage --mode incremental \
        --since 2025-02-10 --out-dir BNetzA_MaStR/gap

    # 2. Then merge:
    python merge_storage_gap.py

Reads:
    BNetzA_MaStR/gap/mastr_storage_incremental_<date>.json
    BNetzA_MaStR/storage.parquet  (Zenodo base)

Writes:
    BNetzA_MaStR/storage.parquet  (updated in-place, old copy → storage.parquet.bak)

Dedup key: mastr_id (EinheitMastrNummer / MaStRNummer). Gap records whose
mastr_id already exists in the Zenodo base are silently dropped — the Zenodo
schema is treated as authoritative for those rows.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

GAP_DIR = Path("BNetzA_MaStR/gap")
PARQUET = Path("BNetzA_MaStR/storage.parquet")


def parse_dotnet_date(s: str | None) -> pd.Timestamp | None:
    if not s:
        return None
    try:
        ms = int(s.strip("/Date()").split("+")[0].split("-")[0])
        return pd.Timestamp(ms, unit="ms", tz="UTC")
    except (ValueError, AttributeError):
        return None


def load_gap_json() -> pd.DataFrame:
    files = sorted(GAP_DIR.glob("mastr_storage_incremental_*.json"))
    if not files:
        raise FileNotFoundError(f"No incremental JSON files found in {GAP_DIR}")

    rows = []
    for f in files:
        print(f"  Reading {f.name} ...")
        data = json.loads(f.read_bytes())
        rows.extend(data.get("Data", []))
    print(f"  Total gap records: {len(rows):,}")

    records = []
    for r in rows:
        records.append({
            "mastr_id":                  r.get("MaStRNummer"),
            "spe_mastr_id":              r.get("SpeicherEinheitMastrNummer"),
            "gross_capacity_kw":         r.get("Bruttoleistung"),
            "net_capacity_kw":           r.get("Nettonennleistung"),
            "usable_capacity_kwh":       r.get("NutzbareSpeicherkapazitaet"),
            "storage_technology":        r.get("StromspeichertechnologieBezeichnung"),
            "battery_technology_code":   r.get("Batterietechnologie"),
            "status":                    r.get("BetriebsStatusName"),
            "voltage_level":             r.get("SpannungsebenenNamen"),
            "feed_in_mode":              r.get("VollTeilEinspeisungBezeichnung"),
            "bundesland":                r.get("Bundesland"),
            "landkreis":                 r.get("Landkreis"),
            "municipality":              r.get("Gemeinde"),
            "municipality_key":          r.get("Gemeindeschluessel"),
            "longitude":                 r.get("Laengengrad"),
            "latitude":                  r.get("Breitengrad"),
            "commissioning_date":        parse_dotnet_date(r.get("InbetriebnahmeDatum")),
            "planned_commissioning_date":parse_dotnet_date(r.get("GeplantesInbetriebsnahmeDatum")),
            "decommissioning_date":      parse_dotnet_date(r.get("EndgueltigeStilllegungDatum")),
            "owner_name":                r.get("AnlagenbetreiberName"),
            "einheit_name":              r.get("EinheitName"),
            "unit_type":                 r.get("EnergietraegerName"),
        })

    df = pd.DataFrame(records)
    df["mastr_id"] = df["mastr_id"].astype("string")
    df["spe_mastr_id"] = df["spe_mastr_id"].astype("string")
    for col in ("gross_capacity_kw", "net_capacity_kw", "usable_capacity_kwh",
                "longitude", "latitude"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("storage_technology", "battery_technology_code", "status",
                "voltage_level", "feed_in_mode", "bundesland", "landkreis",
                "municipality", "municipality_key", "owner_name",
                "einheit_name", "unit_type"):
        df[col] = df[col].astype("string")
    # battery_technology_code arrives as int from API; cast to Int64 to match base.
    df["battery_technology_code"] = pd.to_numeric(
        df["battery_technology_code"], errors="coerce"
    ).astype("Int64")
    # Timestamps already UTC from parse_dotnet_date; cast to us precision.
    for col in ("commissioning_date", "planned_commissioning_date", "decommissioning_date"):
        df[col] = df[col].dt.as_unit("us")

    return df


def main() -> None:
    print("Loading gap JSON ...")
    gap = load_gap_json()

    print(f"\nLoading Zenodo base: {PARQUET} ...")
    base = pd.read_parquet(PARQUET)
    print(f"  Base rows: {len(base):,}")

    existing_ids = set(base["mastr_id"].dropna())
    new_rows = gap[~gap["mastr_id"].isin(existing_ids)]
    dupes = len(gap) - len(new_rows)
    print(f"  Gap new rows: {len(new_rows):,}  (skipped {dupes:,} already in base)")

    if len(new_rows) == 0:
        print("Nothing to add — parquet is already up to date.")
        return

    # Column order must match base exactly.
    new_rows = new_rows[list(base.columns)]

    merged = pd.concat([base, new_rows], ignore_index=True)
    print(f"  Merged rows: {len(merged):,}")

    bak = PARQUET.with_suffix(".parquet.bak")
    shutil.copy2(PARQUET, bak)
    print(f"\nBacked up original → {bak}")

    merged.to_parquet(PARQUET, compression="zstd", index=False)
    size_mb = PARQUET.stat().st_size / 1e6
    print(f"Saved → {PARQUET}  ({size_mb:.0f} MB)")

    # Quick sanity check
    active = merged[merged["status"] == "In Betrieb"]
    batteries = active[active["storage_technology"] == "Batterie"]
    snap = pd.Timestamp("2026-05-01", tz="UTC")
    snap_b = batteries[
        (batteries["commissioning_date"] <= snap)
        & (batteries["decommissioning_date"].isna() | (batteries["decommissioning_date"] > snap))
    ]
    print(f"\nSanity @ 2026-05-01: {len(snap_b):,} active batteries"
          f"  {snap_b['net_capacity_kw'].sum()/1e6:.2f} GW"
          f"  {snap_b['usable_capacity_kwh'].sum()/1e6:.1f} GWh")


if __name__ == "__main__":
    main()
