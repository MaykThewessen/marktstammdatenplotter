#!/usr/bin/env python
"""Merge the JSON-API delta scrape into the historical open-MaStR parquet.

For each tech (pv, bess, wind):
  1. Load every ``data-delta-{tech}/*.json`` page produced by
     ``scrape_delta.py``.
  2. Parse the rows into a DataFrame whose columns match the historical
     parquet schema (BNetzA_MaStR/{solar,wind,storage}.parquet).
  3. Concatenate with the historical parquet, deduplicate by ``mastr_id``
     keeping the delta row (it is newer / more authoritative).
  4. Write to ``BNetzA_MaStR/full-{solar,wind,storage}.parquet`` with zstd
     compression.

The merge is incremental-safe: if a ``full-*.parquet`` already exists it is
used as the historical-side input (so re-runs against a re-downloaded delta
don't waste time re-loading the raw Zenodo snapshot).

Run with the global pixi env:

    ~/.pixi/envs/main/bin/python scripts/merge_delta_to_parquet.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from glob import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

#: Sibling BNetzA_MaStR/ directory holding the Zenodo parquet snapshot.
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


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

_DOTNET_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")


def parse_dotnet_date_array(s: pd.Series) -> pd.Series:
    """Vectorised parse of ``/Date(ms)/`` strings to UTC microsecond timestamps."""
    extracted = s.astype("string").str.extract(_DOTNET_DATE_RE, expand=False)
    ms = pd.to_numeric(extracted, errors="coerce")
    out = pd.to_datetime(ms, unit="ms", utc=True, errors="coerce")
    return out.astype("datetime64[us, UTC]")


def load_delta_json(tech: str) -> list[dict]:
    """Concatenate ``Data`` arrays from every page under data-delta-{tech}/."""
    files = sorted(glob(str(ROOT / f"data-delta-{tech}" / "*.json")))
    rows: list[dict] = []
    for fp in files:
        with open(fp, "rb") as f:
            d = json.load(f)
        rows.extend(d.get("Data") or [])
    print(f"  delta JSON pages: {len(files):>4d}, rows: {len(rows):>9,}")
    return rows


# ---------------------------------------------------------------------------
# Tech-specific parsers — produce a DataFrame matching the historical schema.
# ---------------------------------------------------------------------------

#: HauptausrichtungSolarModule code -> Bezeichnung text.
PV_ORIENTATION_MAP = {
    695: "Nord",
    696: "Nord-Ost",
    697: "Ost",
    698: "Süd-Ost",
    699: "Süd",
    700: "Süd-West",
    701: "West",
    702: "Nord-West",
    703: "nachgeführt",
    704: "Ost-West",
}

#: Batterietechnologie code -> Bezeichnung text (historical parquet labels).
BESS_BATT_TECH_MAP = {
    727: "Lithium-Batterie",
    728: "Blei-Batterie",
    729: "Nickel-Cadmium- / Nickel-Metallhydridbatterie",
    730: "Hochtemperaturbatterie",
    731: "Redox-Flow-Batterie",
    732: "Sonstige Batterie",
}


def parse_pv_delta(rows: list[dict]) -> pd.DataFrame:
    """Build a DataFrame matching ``solar.parquet`` schema."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)

    # Prefer the API's pre-decoded Bezeichnung where present; fall back to
    # mapping the numeric code so older API responses still resolve.
    orientation = df.get("HauptausrichtungSolarModuleBezeichnung")
    if orientation is None:
        orientation = df["HauptausrichtungSolarModule"].map(PV_ORIENTATION_MAP)
    else:
        orientation = orientation.fillna(
            df["HauptausrichtungSolarModule"].map(PV_ORIENTATION_MAP)
        )

    out = pd.DataFrame({
        "mastr_id": df["MaStRNummer"].astype("string"),
        "gross_capacity_kw": pd.to_numeric(df["Bruttoleistung"], errors="coerce"),
        "status": df["BetriebsStatusName"].astype("string"),
        "module_count": pd.to_numeric(df["AnzahlSolarModule"], errors="coerce"),
        "location_type": df.get("ArtDerSolaranlageBezeichnung",
                                pd.Series(pd.NA, index=df.index)).astype("string"),
        "orientation": orientation.astype("string"),
        "usage_type": df.get("NutzungsbereichGebSABezeichnung",
                             pd.Series(pd.NA, index=df.index)).astype("string"),
        "bundesland": df.get("Bundesland",
                             pd.Series(pd.NA, index=df.index)).astype("string"),
        "landkreis": df.get("Landkreis",
                            pd.Series(pd.NA, index=df.index)).astype("string"),
        "municipality": df.get("Gemeinde",
                               pd.Series(pd.NA, index=df.index)).astype("string"),
        "municipality_key": df.get("Gemeindeschluessel",
                                   pd.Series(pd.NA, index=df.index)).astype("string"),
        "longitude": pd.to_numeric(df.get("Laengengrad"), errors="coerce"),
        "latitude": pd.to_numeric(df.get("Breitengrad"), errors="coerce"),
        "commissioning_date": parse_dotnet_date_array(df["InbetriebnahmeDatum"]),
        "decommissioning_date": parse_dotnet_date_array(df["EndgueltigeStilllegungDatum"]),
        "net_capacity_kw": pd.to_numeric(df["Nettonennleistung"], errors="coerce"),
        "installed_capacity_kw": pd.to_numeric(df.get("EegInstallierteLeistung"),
                                               errors="coerce"),
    })
    return out


def parse_wind_delta(rows: list[dict]) -> pd.DataFrame:
    """Build a DataFrame matching ``wind.parquet`` schema."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "mastr_id": df["MaStRNummer"].astype("string"),
        "gross_capacity_kw": pd.to_numeric(df["Bruttoleistung"], errors="coerce"),
        "status": df["BetriebsStatusName"].astype("string"),
        "wind_park": df.get("WindparkName",
                            pd.Series(pd.NA, index=df.index)).astype("string"),
        "location_type": df.get("WindAnLandOderSeeBezeichnung",
                                pd.Series(pd.NA, index=df.index)).astype("string"),
        "manufacturer": df.get("HerstellerWindenergieanlageBezeichnung",
                               pd.Series(pd.NA, index=df.index)).astype("string"),
        "turbine_type": df.get("Typenbezeichnung",
                               pd.Series(pd.NA, index=df.index)).astype("string"),
        "hub_height_m": pd.to_numeric(df.get("NabenhoeheWindenergieanlage"),
                                      errors="coerce"),
        "rotor_diameter_m": pd.to_numeric(df.get("RotordurchmesserWindenergieanlage"),
                                          errors="coerce"),
        "bundesland": df.get("Bundesland",
                             pd.Series(pd.NA, index=df.index)).astype("string"),
        "landkreis": df.get("Landkreis",
                            pd.Series(pd.NA, index=df.index)).astype("string"),
        "municipality": df.get("Gemeinde",
                               pd.Series(pd.NA, index=df.index)).astype("string"),
        "municipality_key": df.get("Gemeindeschluessel",
                                   pd.Series(pd.NA, index=df.index)).astype("string"),
        "longitude": pd.to_numeric(df.get("Laengengrad"), errors="coerce"),
        "latitude": pd.to_numeric(df.get("Breitengrad"), errors="coerce"),
        "commissioning_date": parse_dotnet_date_array(df["InbetriebnahmeDatum"]),
        "decommissioning_date": parse_dotnet_date_array(df["EndgueltigeStilllegungDatum"]),
        "net_capacity_kw": pd.to_numeric(df["Nettonennleistung"], errors="coerce"),
    })
    return out


def parse_bess_delta(rows: list[dict]) -> pd.DataFrame:
    """Build a DataFrame matching ``storage.parquet`` schema."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)

    # Battery technology label (numeric code in API; text label in parquet).
    batt_code = pd.to_numeric(df.get("Batterietechnologie"), errors="coerce")
    battery_label = batt_code.map(BESS_BATT_TECH_MAP).astype("string")

    out = pd.DataFrame({
        "mastr_id": df["MaStRNummer"].astype("string"),
        "spe_mastr_id": df.get("SpeicherEinheitMastrNummer",
                               pd.Series(pd.NA, index=df.index)).astype("string"),
        "gross_capacity_kw": pd.to_numeric(df["Bruttoleistung"], errors="coerce"),
        "net_capacity_kw": pd.to_numeric(df["Nettonennleistung"], errors="coerce"),
        "usable_capacity_kwh": pd.to_numeric(df.get("NutzbareSpeicherkapazitaet"),
                                             errors="coerce"),
        "storage_technology": df.get("StromspeichertechnologieBezeichnung",
                                     pd.Series(pd.NA, index=df.index)).astype("string"),
        "battery_technology_code": battery_label,
        "status": df["BetriebsStatusName"].astype("string"),
        "voltage_level": df.get("SpannungsebenenNamen",
                                pd.Series(pd.NA, index=df.index)).astype("string"),
        "feed_in_mode": df.get("VollTeilEinspeisungBezeichnung",
                               pd.Series(pd.NA, index=df.index)).astype("string"),
        "bundesland": df.get("Bundesland",
                             pd.Series(pd.NA, index=df.index)).astype("string"),
        "landkreis": df.get("Landkreis",
                            pd.Series(pd.NA, index=df.index)).astype("string"),
        "municipality": df.get("Gemeinde",
                               pd.Series(pd.NA, index=df.index)).astype("string"),
        "municipality_key": df.get("Gemeindeschluessel",
                                   pd.Series(pd.NA, index=df.index)).astype("string"),
        "longitude": pd.to_numeric(df.get("Laengengrad"), errors="coerce"),
        "latitude": pd.to_numeric(df.get("Breitengrad"), errors="coerce"),
        "commissioning_date": parse_dotnet_date_array(df["InbetriebnahmeDatum"]),
        "planned_commissioning_date": parse_dotnet_date_array(
            df.get("GeplantesInbetriebsnahmeDatum",
                   pd.Series(pd.NA, index=df.index))
        ),
        "decommissioning_date": parse_dotnet_date_array(df["EndgueltigeStilllegungDatum"]),
        "owner_name": df.get("AnlagenbetreiberName",
                             pd.Series(pd.NA, index=df.index)).astype("string"),
        "einheit_name": df.get("EinheitName",
                               pd.Series(pd.NA, index=df.index)).astype("string"),
        "unit_type": pd.Series("Stromspeichereinheit", index=df.index, dtype="string"),
    })
    return out


# ---------------------------------------------------------------------------
# Merge driver
# ---------------------------------------------------------------------------

TECH_SPECS = {
    "pv":   {"bulk_file": "solar.parquet",   "full_file": "full-solar.parquet",
             "parser": parse_pv_delta,   "label": "PV"},
    "wind": {"bulk_file": "wind.parquet",    "full_file": "full-wind.parquet",
             "parser": parse_wind_delta, "label": "Wind"},
    "bess": {"bulk_file": "storage.parquet", "full_file": "full-storage.parquet",
             "parser": parse_bess_delta, "label": "BESS"},
}


def coerce_categoricals(delta: pd.DataFrame, historic: pd.DataFrame) -> pd.DataFrame:
    """Align dtypes so concat doesn't upcast everything to object.

    Historical parquet uses pandas-categoricals and pyarrow ``string[pyarrow]``.
    The delta uses plain ``string``. We cast both sides to a common type per
    column (the non-categorical, plain ``string`` dtype).
    """
    common_cols = [c for c in historic.columns if c in delta.columns]
    historic = historic[common_cols].copy()
    delta = delta[common_cols].copy()
    for col in common_cols:
        h, d = historic[col], delta[col]
        # Datetime columns: keep tz-aware UTC microsecond timestamps.
        if pd.api.types.is_datetime64_any_dtype(h):
            historic[col] = pd.to_datetime(h, utc=True).astype("datetime64[us, UTC]")
            delta[col] = pd.to_datetime(d, utc=True).astype("datetime64[us, UTC]")
            continue
        # Categorical / pyarrow-string -> plain pandas string.
        if isinstance(h.dtype, pd.CategoricalDtype) or h.dtype == "string" \
                or str(h.dtype) == "string[pyarrow]":
            historic[col] = h.astype("string")
            delta[col] = d.astype("string")
            continue
        # Numeric — leave as-is; concat will upcast int->float if needed.
    return delta.reindex(columns=historic.columns), historic


def merge_tech(tech: str, bulk_dir: Path) -> None:
    spec = TECH_SPECS[tech]
    print(f"\n=== {spec['label']} ===")
    rows = load_delta_json(tech)
    if not rows:
        print(f"  no delta rows for {tech}; skipping")
        return
    delta = spec["parser"](rows)
    print(f"  delta parsed:           {len(delta):>9,} rows")

    full_path = bulk_dir / spec["full_file"]
    bulk_path = bulk_dir / spec["bulk_file"]

    # Incremental-safe: prefer an existing full-*.parquet as the historical side.
    historic_path = full_path if full_path.exists() else bulk_path
    print(f"  historic source:        {historic_path.name}")
    historic = pd.read_parquet(historic_path)
    print(f"  historic rows:          {len(historic):>9,}")

    delta_aligned, historic_aligned = coerce_categoricals(delta, historic)

    # Stack historic first, delta second — drop_duplicates(keep='last') keeps
    # the delta row when a mastr_id appears in both sides.
    combined = pd.concat([historic_aligned, delta_aligned], ignore_index=True)
    pre = len(combined)
    combined = combined.drop_duplicates(subset="mastr_id", keep="last").reset_index(
        drop=True,
    )
    print(f"  after dedup mastr_id:   {len(combined):>9,} "
          f"(dropped {pre - len(combined):,} dupes)")

    print(f"  writing {full_path.name} (zstd) ...")
    combined.to_parquet(full_path, compression="zstd", index=False)
    size_mb = full_path.stat().st_size / 1e6
    print(f"  file size: {size_mb:.1f} MB")

    # Summary stats
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
