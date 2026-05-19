#!/usr/bin/env python
"""Merge the slim ``delta-*.parquet`` files (produced by
``scrape_delta_to_parquet.py`` / ``finish_pv_delta.py``) into the historical
open-MaStR Zenodo parquet snapshots.

For each tech (pv, wind, bess):
  1. Read the historical parquet (``solar.parquet`` / ``wind.parquet`` /
     ``storage.parquet``).
  2. Read the slim ``delta-{solar,wind,bess}.parquet`` produced by the scrape
     pipeline. Its column names already match the historical schema.
  3. Align dtypes (the historical files use pandas-categoricals and pyarrow
     strings; the delta uses plain ``string``) and concat.
  4. Deduplicate by ``mastr_id`` keeping the *delta* row when both sides
     contain it (the delta is newer / more authoritative).
  5. Sort by ``commissioning_date`` and write
     ``full-{solar,wind,storage}.parquet`` with zstd compression.

This script used to ingest raw JSON pages from ``data-delta-{tech}/``. We
switched to reading the parquet output of the streaming scraper because the
raw JSON intermediate is ~ 7 GB of throw-away disk and gets deleted once the
parquet is built.

Run with the global pixi env:

    ~/.pixi/envs/main/bin/python scripts/merge_delta_to_parquet.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

BULK_DIR_CANDIDATES = [
    ROOT / "BNetzA_MaStR",
    ROOT.parent / "BNetzA_MaStR",
    ROOT.parent.parent / "BNetzA_MaStR",
    ROOT.parent.parent.parent / "BNetzA_MaStR",
    ROOT.parent.parent.parent.parent / "BNetzA_MaStR",
]


def find_bulk_dir() -> Path:
    for c in BULK_DIR_CANDIDATES:
        if c.exists() and any(c.glob("*.parquet")):
            return c
    raise FileNotFoundError("BNetzA_MaStR/ not found in any candidate location.")


TECH_SPECS = {
    "pv":   {"bulk_file": "solar.parquet",   "delta_file": "delta-solar.parquet",
             "full_file": "full-solar.parquet", "label": "PV"},
    "wind": {"bulk_file": "wind.parquet",    "delta_file": "delta-wind.parquet",
             "full_file": "full-wind.parquet", "label": "Wind"},
    "bess": {"bulk_file": "storage.parquet", "delta_file": "delta-bess.parquet",
             "full_file": "full-storage.parquet", "label": "BESS"},
}


# Historical storage.parquet stores battery_technology_code as a German text
# label, not the numeric Stammdatenregister code. The JSON-API scrape returns
# the integer code; map it to the same label vocabulary before concat so a
# round-trip of any delta row matches the historical schema.
BESS_BATT_LABEL = {
    "727": "Lithium-Batterie",
    "728": "Blei-Batterie",
    "729": "Nickel-Cadmium- / Nickel-Metallhydridbatterie",
    "730": "Hochtemperaturbatterie",
    "731": "Redox-Flow-Batterie",
    "732": "Sonstige Batterie",
}


def map_bess_codes(delta: pd.DataFrame) -> pd.DataFrame:
    """Cast Int64 codes that the historical schema stores as text labels."""
    delta = delta.copy()
    if "battery_technology_code" in delta.columns:
        s = delta["battery_technology_code"].astype("string")
        # Only re-map if the values look like numeric codes (3-digit ints).
        mask_numeric = s.str.match(r"^\d{3}$", na=False)
        if mask_numeric.any():
            delta["battery_technology_code"] = s.map(BESS_BATT_LABEL).where(
                mask_numeric, s
            )
        else:
            delta["battery_technology_code"] = s
    if "unit_type" in delta.columns:
        # Historical parquet uses the literal "Stromspeichereinheit" string.
        # The scrape's int `Typ` enum (e.g. 597) is meaningless without a
        # decoder; force the canonical label.
        delta["unit_type"] = pd.Series(
            "Stromspeichereinheit", index=delta.index, dtype="string"
        )
    return delta


def align_dtypes(delta: pd.DataFrame, historic: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cast both sides to a common, concat-friendly dtype per column.

    The historical parquet uses pandas categoricals and pyarrow-strings; the
    delta uses plain ``string``. We collapse everything to plain ``string`` so
    ``pd.concat`` doesn't widen to ``object``.
    """
    # Restrict to columns present in the historical schema; drop delta-only
    # columns (we don't want to widen the historical schema, e.g. add an
    # ``owner_name`` column to wind where the historical parquet lacks it).
    common = [c for c in historic.columns if c in delta.columns]
    h = historic[common].copy()
    d = delta[common].copy()
    for col in common:
        hcol, dcol = h[col], d[col]
        if pd.api.types.is_datetime64_any_dtype(hcol):
            h[col] = pd.to_datetime(hcol, utc=True).astype("datetime64[us, UTC]")
            d[col] = pd.to_datetime(dcol, utc=True).astype("datetime64[us, UTC]")
            continue
        if isinstance(hcol.dtype, pd.CategoricalDtype) or hcol.dtype == "string" \
                or str(hcol.dtype) == "string[pyarrow]" or hcol.dtype == object:
            h[col] = hcol.astype("string")
            d[col] = dcol.astype("string")
            continue
        # Numeric — leave as-is (concat upcasts int->float if needed).
    return d.reindex(columns=h.columns), h


def merge_tech(tech: str, bulk_dir: Path) -> None:
    spec = TECH_SPECS[tech]
    print(f"\n=== {spec['label']} ===")

    delta_path = bulk_dir / spec["delta_file"]
    if not delta_path.exists():
        print(f"  WARN: {delta_path.name} not found — skipping {tech}")
        return

    delta = pd.read_parquet(delta_path)
    print(f"  delta:      {len(delta):>9,} rows from {delta_path.name}")

    if tech == "bess":
        delta = map_bess_codes(delta)

    # Incremental-safe: prefer an existing full-*.parquet as the historical
    # side. Lets a re-run against a re-scraped delta avoid touching the
    # original Zenodo snapshot.
    full_path = bulk_dir / spec["full_file"]
    bulk_path = bulk_dir / spec["bulk_file"]
    historic_path = full_path if full_path.exists() else bulk_path
    historic = pd.read_parquet(historic_path)
    print(f"  historical: {len(historic):>9,} rows from {historic_path.name}")

    delta_aligned, historic_aligned = align_dtypes(delta, historic)

    # Stack historic first, delta second → drop_duplicates(keep='last') keeps
    # the delta row when both sides contain the same ``mastr_id``.
    combined = pd.concat([historic_aligned, delta_aligned], ignore_index=True)
    pre = len(combined)
    combined = combined.drop_duplicates(subset="mastr_id", keep="last").reset_index(drop=True)
    print(f"  merged:     {len(combined):>9,} rows  "
          f"(dedup dropped {pre - len(combined):,})")

    # Sort by commissioning date for sensible downstream iteration.
    if "commissioning_date" in combined.columns:
        combined = combined.sort_values("commissioning_date", na_position="last").reset_index(drop=True)

    combined.to_parquet(full_path, compression="zstd", index=False)
    size_mb = full_path.stat().st_size / 1e6
    print(f"  wrote {full_path.name}: {size_mb:.1f} MB")

    # Summary
    gross_gw = combined["gross_capacity_kw"].sum() / 1e6
    print(f"  total gross capacity:   {gross_gw:,.2f} GW")
    if "usable_capacity_kwh" in combined.columns:
        gwh = combined["usable_capacity_kwh"].sum() / 1e6
        print(f"  total usable energy:    {gwh:,.2f} GWh")
    cd = combined["commissioning_date"].dropna()
    if len(cd):
        print(f"  commissioning range:    {cd.min()}  →  {cd.max()}")


def main() -> int:
    bulk_dir = find_bulk_dir()
    print(f"Bulk dir: {bulk_dir}")
    print(f"Started:  {datetime.now(timezone.utc).isoformat()}")
    for tech in ("pv", "wind", "bess"):
        merge_tech(tech, bulk_dir)
    print(f"\nDone:     {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
