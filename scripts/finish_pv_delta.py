#!/usr/bin/env python
"""Finish the partial PV delta scrape: ingest already-cached JSON pages from
``data-delta-pv/``, stream-fetch the remaining pages via the MaStR JSON API,
project each page to the slim ``solar.parquet`` schema, and append to a single
``BNetzA_MaStR/delta-solar.parquet`` with zstd compression.

Why this is a separate script rather than a re-run of ``scrape_delta_to_parquet.py``:

  * ``scrape_delta_to_parquet.py`` always starts from page 1 — re-fetching 22+
    pages of 100 MB each just to recover from a mid-scrape crash is wasteful.
  * ``scrape_delta.py`` insists on staging every page as a JSON file under
    ``data-delta-pv/`` (~ 7 GB total). We already have ~ 26 pages on disk —
    no reason to write the remaining ~ 17 pages there too.

Strategy: re-use cached JSONs where present, async-fetch only the missing
page numbers, project + write each page to parquet as it arrives.

Run with the global pixi env:

    ~/.pixi/envs/main/bin/python scripts/finish_pv_delta.py
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import sys
import time
from glob import glob
from pathlib import Path

import httpx
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
JSON_DIR = ROOT / "data-delta-pv"
OUT_PATH = Path(
    "/Users/mayk/DE_Wind_marktstammdatenplotter/marktstammdatenplotter/BNetzA_MaStR"
) / "delta-solar.parquet"

URL = (
    "https://www.marktstammdatenregister.de/MaStR/Einheit/EinheitJson/"
    "GetErweiterteOeffentlicheEinheitStromerzeugung"
)
CUTOFF = "09.02.2025"
PV_CODE = "2495"
PAGE_SIZE = 25_000

# Match the empirically-stable concurrency cap from ``scrape_delta_to_parquet.py``;
# MaStR's load balancer starts dropping connections above ~ 6 in-flight.
CONCURRENCY = 6
TIMEOUT = 180.0
MAX_RETRIES = 6


# ---------------------------------------------------------------------------
# Vectorised /Date(ms)/ parser — copy of the helper in
# scrape_delta_to_parquet.py / merge_delta_to_parquet.py so this script has no
# cross-file imports.
# ---------------------------------------------------------------------------
_DOTNET_RE = re.compile(r"/Date\((-?\d+)\)/")


def parse_dotnet_dates(s: pd.Series) -> pd.Series:
    ms = s.astype("string").str.extract(_DOTNET_RE, expand=False)
    ts = pd.to_datetime(pd.to_numeric(ms, errors="coerce"), unit="ms", utc=True)
    return ts.astype("datetime64[us, UTC]")


def project_solar(rows: list[dict]) -> pd.DataFrame:
    """Slim-schema projection matching scrape_delta_to_parquet.project_solar."""
    df = pd.DataFrame(rows)
    return pd.DataFrame({
        "mastr_id": df.get("MaStRNummer").astype("string"),
        "gross_capacity_kw": pd.to_numeric(df.get("Bruttoleistung"), errors="coerce"),
        "status": df.get("BetriebsStatusName").astype("string"),
        "module_count": pd.to_numeric(df.get("AnzahlSolarModule"), errors="coerce"),
        "location_type": df.get("ArtDerSolaranlageBezeichnung").astype("string"),
        "orientation": df.get("HauptausrichtungSolarModuleBezeichnung").astype("string"),
        "usage_type": df.get("NutzungsbereichGebSABezeichnung").astype("string"),
        "bundesland": df.get("Bundesland").astype("string"),
        "landkreis": df.get("Landkreis").astype("string"),
        "municipality": df.get("Gemeinde").astype("string"),
        "municipality_key": df.get("Gemeindeschluessel").astype("string"),
        "longitude": pd.to_numeric(df.get("Laengengrad"), errors="coerce"),
        "latitude": pd.to_numeric(df.get("Breitengrad"), errors="coerce"),
        "commissioning_date": parse_dotnet_dates(df.get("InbetriebnahmeDatum")),
        "decommissioning_date": parse_dotnet_dates(df.get("EndgueltigeStilllegungDatum")),
        "net_capacity_kw": pd.to_numeric(df.get("Nettonennleistung"), errors="coerce"),
        "installed_capacity_kw": pd.to_numeric(df.get("EegInstallierteLeistung"), errors="coerce"),
    })


def load_cached_pages() -> dict[int, Path]:
    """Map of page number -> cached JSON file under data-delta-pv/."""
    out: dict[int, Path] = {}
    for fp in sorted(JSON_DIR.glob("data-*.json")):
        m = re.match(r"data-(\d+)\.json$", fp.name)
        if m:
            out[int(m.group(1))] = fp
    return out


async def fetch_page(client: httpx.AsyncClient, page: int) -> list[dict]:
    """Fetch one page with retry-with-backoff on transient failures."""
    params = {
        "sort": "Bruttoleistung-desc",
        "page": page,
        "pageSize": PAGE_SIZE,
        "group": "",
        "forExport": "true",
        "filter": f"Energieträger~eq~'{PV_CODE}'~and~"
                  f"Inbetriebnahmedatum der Einheit~gt~'{CUTOFF}'",
    }
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(URL, params=params)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"retryable {r.status_code}", request=r.request, response=r
                )
            r.raise_for_status()
            return r.json().get("Data") or []
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = min(2 ** attempt, 30) + 0.5 * attempt
            print(f"  page {page} attempt {attempt + 1} failed ({exc!r}); "
                  f"backing off {wait:.1f}s", flush=True)
            await asyncio.sleep(wait)
    return []  # unreachable


async def main_async() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if OUT_PATH.exists():
        # Idempotency: a stale prior run could leave an incomplete writer; nuke
        # it so this run produces a clean file.
        print(f"removing stale {OUT_PATH}")
        OUT_PATH.unlink()

    cached = load_cached_pages()
    print(f"cached JSON pages on disk: {len(cached)} "
          f"({min(cached) if cached else '-'}–{max(cached) if cached else '-'})")

    t0 = time.monotonic()

    # Probe page 1 (or use cached) to learn Total → derive page count.
    if 1 in cached:
        with open(cached[1], "rb") as f:
            d = json.load(f)
        total = d.get("Total")
        page1_rows = d.get("Data") or []
        print(f"page 1 (cached): Total={total:,}")
    else:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            d_full = (await client.get(URL, params={
                "sort": "Bruttoleistung-desc", "page": 1, "pageSize": PAGE_SIZE,
                "group": "", "forExport": "true",
                "filter": f"Energieträger~eq~'{PV_CODE}'~and~"
                          f"Inbetriebnahmedatum der Einheit~gt~'{CUTOFF}'",
            })).json()
        total = d_full.get("Total")
        page1_rows = d_full.get("Data") or []
        print(f"page 1 (fetched): Total={total:,}")

    n_pages = max(1, math.ceil(total / PAGE_SIZE))
    print(f"page count: {n_pages}")

    writer: pq.ParquetWriter | None = None
    rows_written = 0

    def write_rows(rows: list[dict], page: int) -> None:
        nonlocal writer, rows_written
        if not rows:
            return
        df = project_solar(rows)
        table = pa.Table.from_pandas(df, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(OUT_PATH, table.schema, compression="zstd")
        elif not table.schema.equals(writer.schema):
            # nullable columns can flip dtype between pages — cast defensively.
            table = table.cast(writer.schema, safe=False)
        writer.write_table(table)
        rows_written += len(df)
        print(f"  wrote page {page:>3}: +{len(df):,} rows (total {rows_written:,})",
              flush=True)

    # 1) Flush cached pages first, in order.
    for p in sorted(cached):
        if p == 1:
            write_rows(page1_rows, 1)
            continue
        with open(cached[p], "rb") as f:
            rows = (json.load(f) or {}).get("Data") or []
        write_rows(rows, p)

    # 2) Stream-fetch the remaining pages concurrently.
    todo = [p for p in range(1, n_pages + 1) if p not in cached]
    print(f"\nfetching {len(todo)} missing pages: {todo}")
    if todo:
        limits = httpx.Limits(
            max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY
        )
        async with httpx.AsyncClient(timeout=TIMEOUT, limits=limits) as client:
            sem = asyncio.Semaphore(CONCURRENCY)

            async def one(p: int):
                async with sem:
                    rows = await fetch_page(client, p)
                    return p, rows

            tasks = [asyncio.create_task(one(p)) for p in todo]
            for fut in asyncio.as_completed(tasks):
                p, rows = await fut
                write_rows(rows, p)

    if writer is not None:
        writer.close()

    elapsed = time.monotonic() - t0
    size_mb = OUT_PATH.stat().st_size / 1e6 if OUT_PATH.exists() else 0
    print(f"\nDONE: {rows_written:,} rows · {size_mb:.1f} MB · {elapsed:.1f}s "
          f"({elapsed/60:.2f} min)")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
