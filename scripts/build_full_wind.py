"""Build BNetzA_MaStR/full-wind.parquet from Goal100 + mini-delta.

Pipeline:
  1. Read BNetzA_MaStR/goal100-wind.parquet (40 k rows, cutoff 2026-02-19).
  2. Scrape MaStR JSON API for turbines commissioned after 2026-02-19
     (filter Energieträger=2497, Inbetriebnahmedatum>'19.02.2026').
     Probe says ~ 257 rows = 1 page.
  3. Concat + dedupe by mastr_id (delta wins).
  4. Write full-wind.parquet (zstd).

Run after scripts/convert_goal100_wind.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
BULK = Path(
    "/Users/mayk/DE_Wind_marktstammdatenplotter/marktstammdatenplotter/BNetzA_MaStR"
)
GOAL100 = BULK / "goal100-wind.parquet"
OUT = BULK / "full-wind.parquet"

CUTOFF = "19.02.2026"          # German DD.MM.YYYY for MaStR's filter
TECH_CODE = "2497"             # Wind
URL = ("https://www.marktstammdatenregister.de/MaStR/Einheit/EinheitJson/"
       "GetErweiterteOeffentlicheEinheitStromerzeugung")

# Slim columns kept from JSON-API delta rows.
JSON_COLS = [
    "Id", "EinheitName", "Bruttoleistung", "Nettonennleistung",
    "InbetriebnahmeDatum", "EndgueltigeStilllegungDatum",
    "BetriebsStatusName",
    "Bundesland", "Landkreis", "Gemeinde", "Gemeindeschluessel",
    "Laengengrad", "Breitengrad",
    "WindAnLandOderSeeId", "StandortAnonymisiert",
]

# Map JSON-API field → goal100-wind.parquet schema column.
JSON_TO_PARQUET = {
    "Id": "mastr_id",
    "EinheitName": "wind_park",
    "Bruttoleistung": "gross_capacity_kw",
    "Nettonennleistung": "net_capacity_kw",
    "InbetriebnahmeDatum": "commissioning_date",
    "EndgueltigeStilllegungDatum": "decommissioning_date",
    "BetriebsStatusName": "status",
    "Bundesland": "bundesland",
    "Landkreis": "landkreis",
    "Gemeinde": "municipality",
    "Gemeindeschluessel": "municipality_key",
    "Laengengrad": "longitude",
    "Breitengrad": "latitude",
}


def parse_dotnet_date(s):
    import re
    if not s:
        return pd.NaT
    m = re.match(r"/Date\((\d+)\)/", str(s))
    if not m:
        return pd.NaT
    return pd.to_datetime(int(m.group(1)), unit="ms", utc=True)


async def fetch_all_pages(total_rows: int) -> pd.DataFrame:
    n_pages = (total_rows + 24_999) // 25_000
    print(f"  scraping {n_pages} page(s) for {total_rows:,} new turbines")
    async with httpx.AsyncClient(timeout=180) as client:
        async def one(page):
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
            return r.json().get("Data", []) or []
        results = await asyncio.gather(*(one(p) for p in range(1, n_pages + 1)))
    rows = [row for page in results for row in page]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df[[c for c in JSON_COLS if c in df.columns]]
    df = df.rename(columns=JSON_TO_PARQUET)
    for col in ("commissioning_date", "decommissioning_date"):
        df[col] = df[col].apply(parse_dotnet_date)
    df["mastr_id"] = df["mastr_id"].astype(str)
    df["location_type"] = df.get("WindAnLandOderSeeId", pd.Series([])).map(
        {889: "Windkraft auf See"}
    ).where(lambda s: s.notna(), "Windkraft an Land")
    df["installed_capacity_kw"] = df["gross_capacity_kw"]
    df = df.drop(columns=[c for c in ("WindAnLandOderSeeId", "StandortAnonymisiert")
                          if c in df.columns])
    return df


def probe_total() -> int:
    """Synchronous probe for total delta row count."""
    import urllib.parse
    import urllib.request
    qs = urllib.parse.urlencode({
        "sort": "Bruttoleistung-desc", "page": "1", "pageSize": "1",
        "group": "", "forExport": "true",
        "filter": (f"Energieträger~eq~'{TECH_CODE}'~and~"
                   f"Inbetriebnahmedatum der Einheit~gt~'{CUTOFF}'"),
    })
    with urllib.request.urlopen(f"{URL}?{qs}", timeout=60) as fh:
        import json
        return json.load(fh).get("Total", 0)


def main():
    if not GOAL100.exists():
        sys.exit(f"missing {GOAL100} — run scripts/convert_goal100_wind.py first")

    base = pd.read_parquet(GOAL100)
    print(f"Base (Goal100 2026-02-19): {len(base):,} rows · "
          f"{base['gross_capacity_kw'].sum()/1e6:.2f} GW")

    total = probe_total()
    print(f"API delta probe: {total:,} turbines commissioned > {CUTOFF}")

    if total > 0:
        delta = asyncio.run(fetch_all_pages(total))
        print(f"  fetched {len(delta):,} rows")
    else:
        delta = pd.DataFrame()

    # Force string dtype on join keys / IDs so concat doesn't upcast/downcast.
    for df_ in (base, delta):
        if df_.empty:
            continue
        for col in ("mastr_id", "municipality_key", "wind_park",
                    "bundesland", "landkreis", "municipality",
                    "manufacturer", "turbine_type", "status",
                    "location_type"):
            if col in df_.columns:
                df_[col] = df_[col].astype("string")
    if len(delta):
        merged = pd.concat([base, delta], ignore_index=True)
    else:
        merged = base.copy()

    # Dedupe: prefer delta (it's newer than Goal100's snapshot).
    merged = merged.drop_duplicates(subset=["mastr_id"], keep="last")
    merged = merged.sort_values("commissioning_date").reset_index(drop=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(OUT, compression="zstd", index=False)

    size_mb = OUT.stat().st_size / 1e6
    active = merged[merged["status"] == "In Betrieb"]
    print(f"\nOutput: {OUT}  ·  {size_mb:.1f} MB  ·  {len(merged):,} rows")
    print(f"  In Betrieb: {len(active):,} · "
          f"{active['gross_capacity_kw'].sum()/1e6:.2f} GW")
    print(f"  max commissioning_date: {merged['commissioning_date'].max()}")


if __name__ == "__main__":
    main()
