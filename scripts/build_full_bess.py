"""Build BNetzA_MaStR/full-storage.parquet from Zenodo base + API delta.

Pipeline:
  1. Read BNetzA_MaStR/storage.parquet (Zenodo 2025-02-09 cutoff).
  2. Scrape MaStR JSON API for storage rows commissioned after 2025-02-09
     (filter Energieträger=2496). Stream-parse + slim-column-project +
     append to a single zstd-Parquet writer. No raw-JSON intermediate.
  3. Concat + dedupe by mastr_id (delta wins). Sort by commissioning_date.
  4. Write BNetzA_MaStR/full-storage.parquet.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx
import pandas as pd

BULK = Path(__file__).resolve().parent.parent / "BNetzA_MaStR"
BASE = BULK / "storage.parquet"
OUT = BULK / "full-storage.parquet"

CUTOFF = "09.02.2025"
TECH_CODE = "2496"
URL = ("https://www.marktstammdatenregister.de/MaStR/Einheit/EinheitJson/"
       "GetErweiterteOeffentlicheEinheitStromerzeugung")

JSON_COLS = [
    "Id", "EinheitName", "Bruttoleistung", "Nettonennleistung",
    "NutzbareSpeicherkapazitaet",
    "InbetriebnahmeDatum", "GeplantesInbetriebsnahmeDatum",
    "EndgueltigeStilllegungDatum",
    "BetriebsStatusName",
    "StromspeichertechnologieBezeichnung", "Batterietechnologie",
    "VollTeilEinspeisungBezeichnung", "SpannungsebenenNamen",
    "Bundesland", "Landkreis", "Gemeinde", "Gemeindeschluessel",
    "Laengengrad", "Breitengrad",
    "AnlagenbetreiberName",
]

JSON_TO_PARQUET = {
    "Id": "mastr_id",
    "EinheitName": "einheit_name",
    "Bruttoleistung": "gross_capacity_kw",
    "Nettonennleistung": "net_capacity_kw",
    "NutzbareSpeicherkapazitaet": "usable_capacity_kwh",
    "InbetriebnahmeDatum": "commissioning_date",
    "GeplantesInbetriebsnahmeDatum": "planned_commissioning_date",
    "EndgueltigeStilllegungDatum": "decommissioning_date",
    "BetriebsStatusName": "status",
    "StromspeichertechnologieBezeichnung": "storage_technology",
    "Batterietechnologie": "battery_technology_code",
    "VollTeilEinspeisungBezeichnung": "feed_in_mode",
    "SpannungsebenenNamen": "voltage_level",
    "Bundesland": "bundesland",
    "Landkreis": "landkreis",
    "Gemeinde": "municipality",
    "Gemeindeschluessel": "municipality_key",
    "Laengengrad": "longitude",
    "Breitengrad": "latitude",
    "AnlagenbetreiberName": "owner_name",
}

STRING_COLS = (
    "mastr_id", "einheit_name", "status", "storage_technology",
    "battery_technology_code", "feed_in_mode", "voltage_level",
    "bundesland", "landkreis", "municipality", "municipality_key",
    "owner_name",
)


def parse_dotnet_date(s):
    if not s:
        return pd.NaT
    m = re.match(r"/Date\((\d+)\)/", str(s))
    if not m:
        return pd.NaT
    return pd.to_datetime(int(m.group(1)), unit="ms", utc=True)


async def fetch_delta(total_rows: int) -> pd.DataFrame:
    n_pages = (total_rows + 24_999) // 25_000
    print(f"  scraping {n_pages} pages for {total_rows:,} delta rows")
    async with httpx.AsyncClient(timeout=180, http2=False) as client:
        sem = asyncio.Semaphore(6)
        async def fetch(page):
            async with sem:
                r = await client.get(URL, params={
                    "sort": "Bruttoleistung-desc",
                    "page": page,
                    "pageSize": "25000",
                    "group": "",
                    "forExport": "true",
                    "filter": (f"Energieträger~eq~'{TECH_CODE}'~and~"
                               f"Inbetriebnahmedatum der Einheit~gt~'{CUTOFF}'"),
                })
                r.raise_for_status()
                return page, r.json().get("Data", []) or []
        results = []
        for coro in asyncio.as_completed([fetch(p) for p in range(1, n_pages + 1)]):
            page, rows = await coro
            results.append(rows)
            print(f"    page {page}: {len(rows):,} rows")
    flat = [row for rows in results for row in rows]
    if not flat:
        return pd.DataFrame()
    df = pd.DataFrame(flat)
    df = df[[c for c in JSON_COLS if c in df.columns]]
    df = df.rename(columns=JSON_TO_PARQUET)
    for col in ("commissioning_date", "planned_commissioning_date",
                "decommissioning_date"):
        if col in df.columns:
            df[col] = df[col].apply(parse_dotnet_date)
    return df


def probe_total() -> int:
    import json
    import urllib.parse
    import urllib.request
    qs = urllib.parse.urlencode({
        "sort": "Bruttoleistung-desc", "page": "1", "pageSize": "1",
        "group": "", "forExport": "true",
        "filter": (f"Energieträger~eq~'{TECH_CODE}'~and~"
                   f"Inbetriebnahmedatum der Einheit~gt~'{CUTOFF}'"),
    })
    with urllib.request.urlopen(f"{URL}?{qs}", timeout=60) as fh:
        return json.load(fh).get("Total", 0)


def main():
    if not BASE.exists():
        sys.exit(f"missing {BASE}")

    base = pd.read_parquet(BASE)
    print(f"Base (storage.parquet): {len(base):,} rows · "
          f"{base['gross_capacity_kw'].sum() / 1e6:.2f} GW · "
          f"{base['usable_capacity_kwh'].sum() / 1e6:.2f} GWh")

    total = probe_total()
    print(f"Delta probe: {total:,} new storage units after {CUTOFF}")

    delta = asyncio.run(fetch_delta(total)) if total > 0 else pd.DataFrame()
    if len(delta):
        # Force string dtype on key/text columns so concat doesn't drift.
        for df_ in (base, delta):
            for col in STRING_COLS:
                if col in df_.columns:
                    df_[col] = df_[col].astype("string")
        merged = pd.concat([base, delta], ignore_index=True)
    else:
        merged = base.copy()

    merged = merged.drop_duplicates(subset=["mastr_id"], keep="last")
    merged = merged.sort_values("commissioning_date").reset_index(drop=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(OUT, compression="zstd", index=False)

    size_mb = OUT.stat().st_size / 1e6
    active = merged[merged["status"] == "In Betrieb"]
    print(f"\nOutput: {OUT}  ·  {size_mb:.1f} MB  ·  {len(merged):,} rows")
    print(f"  In Betrieb: {len(active):,} · "
          f"{active['gross_capacity_kw'].sum() / 1e6:.2f} GW · "
          f"{active['usable_capacity_kwh'].sum() / 1e6:.2f} GWh")
    print(f"  max commissioning_date: {merged['commissioning_date'].max()}")


if __name__ == "__main__":
    main()
