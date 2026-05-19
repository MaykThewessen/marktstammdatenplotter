#!/usr/bin/env python
"""Convert the open-MaStR storage CSV dumps to a slim, typed Parquet file.

Inputs (open-MaStR Zenodo snapshot):
  * bnetza_mastr_storage_units_raw.csv   — per-Speicher (SSE...) usable capacity
  * bnetza_mastr_storage_raw.csv         — per-unit (SEE...) extended metadata

Output:
  * storage.parquet  — slim typed Parquet matching the wind/solar schema family.

The two CSVs share the SpeMastrNummer key:
  ext.SpeMastrNummer  ==  units.MastrNummer  (the SSE-prefixed Speicher id)

Run with the global pixi env:
    ~/.pixi/envs/main/bin/python scripts/convert_storage_to_parquet.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/Users/mayk/DE_Wind_marktstammdatenplotter/marktstammdatenplotter/BNetzA_MaStR")
UNITS_CSV = ROOT / "bnetza_mastr_storage_units_raw.csv"
EXT_CSV = ROOT / "bnetza_mastr_storage_raw.csv"
OUT_PARQUET = ROOT / "storage.parquet"

CHUNKSIZE = 200_000

# Columns we want from the extended (per-unit) CSV. Anything optional that may
# not exist in older Zenodo snapshots is requested via getattr-style fallback
# during chunk loading.
EXT_REQUIRED = [
    "EinheitMastrNummer",
    "SpeMastrNummer",
    "Einheittyp",
    "Bruttoleistung",
    "Nettonennleistung",
    "Batterietechnologie",
    "EinheitBetriebsstatus",
    "Anlagenbetreiber",
    "NameStromerzeugungseinheit",
    "Einspeisungsart",
    "Bundesland",
    "Landkreis",
    "Gemeinde",
    "Gemeindeschluessel",
    "Laengengrad",
    "Breitengrad",
    "Inbetriebnahmedatum",
    "GeplantesInbetriebnahmedatum",
    "DatumEndgueltigeStilllegung",
    "Technologie",
]

# Truly optional — kept only if the CSV header includes them.
EXT_OPTIONAL = [
    "StromspeichertechnologieBezeichnung",
    "SpannungsebenenNamen",
    "VollTeilEinspeisungBezeichnung",
]

UNITS_COLS = ["MastrNummer", "NutzbareSpeicherkapazitaet"]


def parse_dates_utc(s: pd.Series) -> pd.Series:
    """Parse ISO date strings to UTC tz-aware microsecond timestamps."""
    out = pd.to_datetime(s, errors="coerce", utc=True)
    # pyarrow writes ns->us automatically; cast for explicitness/portability.
    return out.astype("datetime64[us, UTC]")


def detect_optional(csv_path: Path, candidates: list[str]) -> list[str]:
    head = pd.read_csv(csv_path, nrows=0)
    present = [c for c in candidates if c in head.columns]
    missing = [c for c in candidates if c not in head.columns]
    if missing:
        print(f"  optional columns absent: {missing}")
    return present


def main() -> int:
    print(f"Reading extended CSV: {EXT_CSV.name}")
    optional_present = detect_optional(EXT_CSV, EXT_OPTIONAL)
    ext_cols = EXT_REQUIRED + optional_present

    ext_chunks: list[pd.DataFrame] = []
    total_rows = 0
    for i, chunk in enumerate(
        pd.read_csv(
            EXT_CSV,
            usecols=ext_cols,
            chunksize=CHUNKSIZE,
            low_memory=False,
            dtype={"Gemeindeschluessel": "string"},
        )
    ):
        ext_chunks.append(chunk)
        total_rows += len(chunk)
        print(f"  ext chunk {i}: cumulative rows = {total_rows:,}")
    ext = pd.concat(ext_chunks, ignore_index=True)
    del ext_chunks
    print(f"extended rows: {len(ext):,}")

    print(f"\nReading units CSV: {UNITS_CSV.name}")
    units_chunks: list[pd.DataFrame] = []
    total_rows = 0
    for i, chunk in enumerate(
        pd.read_csv(
            UNITS_CSV,
            usecols=UNITS_COLS,
            chunksize=CHUNKSIZE,
            low_memory=False,
        )
    ):
        units_chunks.append(chunk)
        total_rows += len(chunk)
        print(f"  units chunk {i}: cumulative rows = {total_rows:,}")
    units = pd.concat(units_chunks, ignore_index=True)
    del units_chunks
    units = units.rename(columns={
        "MastrNummer": "SpeMastrNummer",
        "NutzbareSpeicherkapazitaet": "_usable_capacity_kwh_units",
    })
    print(f"units rows: {len(units):,}")

    # Inner join on SpeMastrNummer. Use left so we keep all units with a Spe id;
    # drop rows that have no Spe key in the extended table.
    print("\nJoining on SpeMastrNummer ...")
    ext_keyed = ext[ext["SpeMastrNummer"].notna()].copy()
    dropped = len(ext) - len(ext_keyed)
    if dropped:
        print(f"  dropped {dropped} extended rows lacking SpeMastrNummer")
    merged = ext_keyed.merge(units, on="SpeMastrNummer", how="left")
    print(f"merged rows: {len(merged):,}")

    # Schema mapping
    print("\nBuilding slim frame ...")
    out = pd.DataFrame({
        "mastr_id": merged["EinheitMastrNummer"].astype("string"),
        "spe_mastr_id": merged["SpeMastrNummer"].astype("string"),
        "gross_capacity_kw": pd.to_numeric(merged["Bruttoleistung"], errors="coerce"),
        "net_capacity_kw": pd.to_numeric(merged["Nettonennleistung"], errors="coerce"),
        "usable_capacity_kwh": pd.to_numeric(
            merged["_usable_capacity_kwh_units"], errors="coerce"
        ),
        # In this Zenodo snapshot Batterietechnologie is already the German
        # text label ("Lithium-Batterie", "Blei-Batterie", ...), not a numeric
        # code. We try numeric first and fall back to string if that yields all
        # NA — this keeps the script forward-compatible with future snapshots
        # that may switch back to raw integer codes.
        "battery_technology_code": (
            pd.to_numeric(merged["Batterietechnologie"], errors="coerce").astype("Int64")
            if pd.to_numeric(merged["Batterietechnologie"], errors="coerce").notna().any()
            else merged["Batterietechnologie"].astype("string")
        ),
        "status": merged["EinheitBetriebsstatus"].astype("string"),
        "bundesland": merged["Bundesland"].astype("string"),
        "landkreis": merged["Landkreis"].astype("string"),
        "municipality": merged["Gemeinde"].astype("string"),
        "municipality_key": merged["Gemeindeschluessel"].astype("string"),
        "longitude": pd.to_numeric(merged["Laengengrad"], errors="coerce"),
        "latitude": pd.to_numeric(merged["Breitengrad"], errors="coerce"),
        "commissioning_date": parse_dates_utc(merged["Inbetriebnahmedatum"]),
        "planned_commissioning_date": parse_dates_utc(
            merged["GeplantesInbetriebnahmedatum"]
        ),
        "decommissioning_date": parse_dates_utc(merged["DatumEndgueltigeStilllegung"]),
        "owner_name": merged["Anlagenbetreiber"].astype("string"),
        "einheit_name": merged["NameStromerzeugungseinheit"].astype("string"),
        "unit_type": merged["Einheittyp"].astype("string"),
    })

    # storage_technology: prefer StromspeichertechnologieBezeichnung if present,
    # else fall back to the numeric Technologie code's text label if available.
    if "StromspeichertechnologieBezeichnung" in merged.columns:
        out["storage_technology"] = merged["StromspeichertechnologieBezeichnung"].astype("string")
    else:
        # No textual storage-technology column in this snapshot — use numeric
        # Technologie column as a fallback string (will be coded ints, not the
        # ideal human label, but better than null).
        out["storage_technology"] = merged["Technologie"].astype("string")

    out["voltage_level"] = (
        merged["SpannungsebenenNamen"].astype("string")
        if "SpannungsebenenNamen" in merged.columns
        else pd.Series(pd.NA, index=merged.index, dtype="string")
    )

    if "VollTeilEinspeisungBezeichnung" in merged.columns:
        out["feed_in_mode"] = merged["VollTeilEinspeisungBezeichnung"].astype("string")
    else:
        # Einspeisungsart already carries "Volleinspeisung" / "Teileinspeisung ..."
        out["feed_in_mode"] = merged["Einspeisungsart"].astype("string")

    # Reorder columns to match the user-spec schema order.
    column_order = [
        "mastr_id",
        "spe_mastr_id",
        "gross_capacity_kw",
        "net_capacity_kw",
        "usable_capacity_kwh",
        "storage_technology",
        "battery_technology_code",
        "status",
        "voltage_level",
        "feed_in_mode",
        "bundesland",
        "landkreis",
        "municipality",
        "municipality_key",
        "longitude",
        "latitude",
        "commissioning_date",
        "planned_commissioning_date",
        "decommissioning_date",
        "owner_name",
        "einheit_name",
        "unit_type",
    ]
    out = out[column_order]

    print(f"\nWriting {OUT_PARQUET} ...")
    out.to_parquet(OUT_PARQUET, compression="zstd", index=False)
    size_mb = OUT_PARQUET.stat().st_size / 1e6
    print(f"  size: {size_mb:.1f} MB")

    # Sanity check
    print("\n=== sanity check ===")
    print(f"rows:                  {len(out):,}")
    gross_gw = out["gross_capacity_kw"].sum() / 1e6
    net_gw = out["net_capacity_kw"].sum() / 1e6
    usable_gwh = out["usable_capacity_kwh"].sum() / 1e6
    print(f"gross capacity total:  {gross_gw:,.2f} GW")
    print(f"net capacity total:    {net_gw:,.2f} GW")
    print(f"usable energy total:   {usable_gwh:,.2f} GWh")

    print("\nstatus breakdown:")
    print(out["status"].value_counts(dropna=False).head(15).to_string())

    print("\nstorage_technology breakdown (top 15):")
    print(out["storage_technology"].value_counts(dropna=False).head(15).to_string())

    print("\nsample (3 rows):")
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(out.head(3).to_string())

    return 0


if __name__ == "__main__":
    sys.exit(main())
