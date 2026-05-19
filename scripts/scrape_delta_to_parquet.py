#!/usr/bin/env python
"""Stream-scrape MaStR JSON-API → slim Parquet for the *delta* since the last
open-MaStR Zenodo snapshot (cutoff 09.02.2025).

Why this exists
---------------
The open-MaStR Zenodo CSV snapshot is monthly. To get fresh data we hit the
public JSON API:
  https://www.marktstammdatenregister.de/MaStR/Einheit/EinheitJson/
  GetErweiterteOeffentlicheEinheitStromerzeugung

Instead of staging every page as a 1-3 MB JSON file (~7 GB of throw-away I/O
for the full delta), we stream-parse each HTTP response, project to a slim
schema that matches the existing wind/solar/storage Parquet column families,
and append to a single `pyarrow.ParquetWriter` per technology. Final disk
footprint: ~100-300 MB Zstd-compressed Parquet per tech, no JSON on disk.

Async httpx with 8-way concurrency is ~3-4x faster than `curl | xargs` for
this workload (no subprocess overhead, no shell-quoting pain, HTTP/2-ready).

Outputs (under BNetzA_MaStR/):
  delta-solar.parquet
  delta-bess.parquet
  delta-wind.parquet

Run with the global pixi env:
    ~/.pixi/envs/main/bin/python scripts/scrape_delta_to_parquet.py

Energieträger codes:
  2495  Solar (Solare Strahlungsenergie)
  2496  Speicher (BESS / pumped-hydro / H2)
  2497  Wind
"""
from __future__ import annotations

import argparse
import asyncio
import math
import re
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


URL = (
    "https://www.marktstammdatenregister.de/MaStR/Einheit/EinheitJson/"
    "GetErweiterteOeffentlicheEinheitStromerzeugung"
)
CUTOFF = "09.02.2025"  # matches the Zenodo snapshot date — keeps full ≥ delta union complete

OUT_DIR = Path(
    "/Users/mayk/DE_Wind_marktstammdatenplotter/marktstammdatenplotter/BNetzA_MaStR"
)

# httpx settings: MaStR's server tends to rate-limit at ~10 concurrent.
# 8-way concurrency is the sweet spot — empirically saturates throughput
# without provoking 429s.
PAGE_SIZE = 25_000
# 8-way concurrency triggered frequent ReadError('') drops from MaStR's
# load balancer on the BESS tech (29 pages of 25k rows each). 4-way is the
# observed stable ceiling; throughput is still ~3x curl-xargs and the server
# never 5xx'd or 429'd at this rate.
CONCURRENCY = 4
TIMEOUT = 180.0
MAX_RETRIES = 6

TECHS = {
    "solar": 2495,
    "bess": 2496,
    "wind": 2497,
}


# ---------------------------------------------------------------------------
# Vectorised /Date(ms)/ parser
# ---------------------------------------------------------------------------
_DOTNET_RE = re.compile(r"/Date\((-?\d+)\)/")


def parse_dotnet_dates(s: pd.Series) -> pd.Series:
    """Vectorised parse of MaStR's /Date(ms)/ strings → UTC tz-aware us-precision.

    Anything that doesn't match the regex → NaT. Empty / None / non-string
    values are handled safely.
    """
    # str.extract handles NaN → NaN automatically.
    ms = s.astype("string").str.extract(_DOTNET_RE, expand=False)
    ts = pd.to_datetime(pd.to_numeric(ms, errors="coerce"), unit="ms", utc=True)
    return ts.astype("datetime64[us, UTC]")


# ---------------------------------------------------------------------------
# Per-tech slim projection
# ---------------------------------------------------------------------------
def project_solar(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
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
        # `EegInstallierteLeistung` aligns with the historical `installed_capacity_kw`
        # column (EEG-registered nameplate). Falls back to Bruttoleistung when null,
        # matching observed behaviour in the Zenodo CSV.
        "installed_capacity_kw": pd.to_numeric(
            df.get("EegInstallierteLeistung"), errors="coerce"
        ),
    })
    return out


def project_wind(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "mastr_id": df.get("MaStRNummer").astype("string"),
        "gross_capacity_kw": pd.to_numeric(df.get("Bruttoleistung"), errors="coerce"),
        "status": df.get("BetriebsStatusName").astype("string"),
        "wind_park": df.get("WindparkName").astype("string"),
        "location_type": df.get("WindAnLandOderSeeBezeichnung").astype("string"),
        "manufacturer": df.get("HerstellerWindenergieanlageBezeichnung").astype("string"),
        "turbine_type": df.get("Typenbezeichnung").astype("string"),
        "hub_height_m": pd.to_numeric(df.get("NabenhoeheWindenergieanlage"), errors="coerce"),
        "rotor_diameter_m": pd.to_numeric(df.get("RotordurchmesserWindenergieanlage"), errors="coerce"),
        "bundesland": df.get("Bundesland").astype("string"),
        "landkreis": df.get("Landkreis").astype("string"),
        "municipality": df.get("Gemeinde").astype("string"),
        "municipality_key": df.get("Gemeindeschluessel").astype("string"),
        "longitude": pd.to_numeric(df.get("Laengengrad"), errors="coerce"),
        "latitude": pd.to_numeric(df.get("Breitengrad"), errors="coerce"),
        "commissioning_date": parse_dotnet_dates(df.get("InbetriebnahmeDatum")),
        "decommissioning_date": parse_dotnet_dates(df.get("EndgueltigeStilllegungDatum")),
        "net_capacity_kw": pd.to_numeric(df.get("Nettonennleistung"), errors="coerce"),
    })
    return out


def project_bess(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    # battery_technology_code may arrive as either an int code or a German
    # text label depending on the season — historical Zenodo dumps store it
    # as nullable Int64. Coerce to Int64 if numeric, else keep as string.
    bt_raw = df.get("Batterietechnologie")
    bt_num = pd.to_numeric(bt_raw, errors="coerce")
    if bt_num.notna().any():
        bt_col = bt_num.astype("Int64")
    else:
        bt_col = bt_raw.astype("string")

    out = pd.DataFrame({
        "mastr_id": df.get("MaStRNummer").astype("string"),
        "spe_mastr_id": df.get("SpeicherEinheitMastrNummer").astype("string"),
        "gross_capacity_kw": pd.to_numeric(df.get("Bruttoleistung"), errors="coerce"),
        "net_capacity_kw": pd.to_numeric(df.get("Nettonennleistung"), errors="coerce"),
        "usable_capacity_kwh": pd.to_numeric(df.get("NutzbareSpeicherkapazitaet"), errors="coerce"),
        "storage_technology": df.get("StromspeichertechnologieBezeichnung").astype("string"),
        "battery_technology_code": bt_col,
        "status": df.get("BetriebsStatusName").astype("string"),
        "voltage_level": df.get("SpannungsebenenNamen").astype("string"),
        "feed_in_mode": df.get("VollTeilEinspeisungBezeichnung").astype("string"),
        "bundesland": df.get("Bundesland").astype("string"),
        "landkreis": df.get("Landkreis").astype("string"),
        "municipality": df.get("Gemeinde").astype("string"),
        "municipality_key": df.get("Gemeindeschluessel").astype("string"),
        "longitude": pd.to_numeric(df.get("Laengengrad"), errors="coerce"),
        "latitude": pd.to_numeric(df.get("Breitengrad"), errors="coerce"),
        "commissioning_date": parse_dotnet_dates(df.get("InbetriebnahmeDatum")),
        "planned_commissioning_date": parse_dotnet_dates(df.get("GeplantesInbetriebsnahmeDatum")),
        "decommissioning_date": parse_dotnet_dates(df.get("EndgueltigeStilllegungDatum")),
        "owner_name": df.get("AnlagenbetreiberName").astype("string"),
        "einheit_name": df.get("EinheitName").astype("string"),
        # Typ is an int enum for unit-type ("Stromerzeugungseinheit" etc.) —
        # keep as nullable Int64; the converter script writes it as string,
        # so cast to string here for schema parity.
        "unit_type": df.get("Typ").astype("string"),
    })
    return out


PROJECTORS = {
    "solar": project_solar,
    "wind": project_wind,
    "bess": project_bess,
}


# ---------------------------------------------------------------------------
# Async fetcher
# ---------------------------------------------------------------------------
async def fetch_page(
    client: httpx.AsyncClient,
    code: int,
    page: int,
    *,
    stats: dict,
) -> tuple[list[dict], int | None]:
    """Fetch one page. Retries on 5xx / 429 / timeout with capped exponential
    backoff. Returns (rows, total) — total only on page 1, else None.
    """
    params = {
        "sort": "Bruttoleistung-desc",
        "page": page,
        "pageSize": PAGE_SIZE,
        "group": "",
        "forExport": "true",
        "filter": f"Energieträger~eq~'{code}'~and~Inbetriebnahmedatum der Einheit~gt~'{CUTOFF}'",
    }
    for attempt in range(MAX_RETRIES):
        t0 = time.monotonic()
        try:
            r = await client.get(URL, params=params)
            dt = time.monotonic() - t0
            stats["latencies"].append(dt)
            stats["max_latency"] = max(stats["max_latency"], dt)
            if r.status_code in (429, 500, 502, 503, 504):
                stats["http_errors"][r.status_code] = stats["http_errors"].get(r.status_code, 0) + 1
                raise httpx.HTTPStatusError(
                    f"retryable {r.status_code}", request=r.request, response=r
                )
            r.raise_for_status()
            d = r.json()
            rows = d.get("Data", []) or []
            total = d.get("Total") if page == 1 else None
            return rows, total
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as exc:
            wait = min(2 ** attempt, 30) + 0.5 * attempt
            stats["retries"] += 1
            print(f"  page {page} attempt {attempt + 1} failed ({exc!r}); "
                  f"backing off {wait:.1f}s", flush=True)
            if attempt == MAX_RETRIES - 1:
                raise
            await asyncio.sleep(wait)


async def scrape_tech(tech: str, code: int) -> dict:
    """Scrape one technology end-to-end. Streams pages → projected DataFrame
    → ParquetWriter. Returns a stats dict.
    """
    out_path = OUT_DIR / f"delta-{tech}.parquet"
    print(f"\n=== {tech} (code {code}) → {out_path.name} ===")

    stats = {
        "latencies": [],
        "max_latency": 0.0,
        "http_errors": {},
        "retries": 0,
        "rows": 0,
        "pages": 0,
        "t0": time.monotonic(),
    }

    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=limits, http2=False) as client:
        # Page 1 first to learn Total → page count.
        rows1, total = await fetch_page(client, code, 1, stats=stats)
        if total is None:
            raise RuntimeError(f"{tech}: page 1 response lacked Total")
        n_pages = max(1, math.ceil(total / PAGE_SIZE))
        print(f"  Total={total:,} → {n_pages} pages @ pageSize={PAGE_SIZE}")

        writer: pq.ParquetWriter | None = None
        projector = PROJECTORS[tech]

        def write_rows(rows: list[dict]) -> int:
            """Project + append. Returns rows written."""
            nonlocal writer
            if not rows:
                return 0
            df = projector(rows)
            table = pa.Table.from_pandas(df, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(out_path, table.schema, compression="zstd")
            else:
                # Successive pages should share the schema — cast defensively
                # in case nullable-int columns flip between Int64 and string
                # mid-stream (only seen for `battery_technology_code`).
                if not table.schema.equals(writer.schema):
                    table = table.cast(writer.schema, safe=False)
            writer.write_table(table)
            return len(df)

        # Persist page 1 immediately so a crash mid-scrape isn't a total loss.
        stats["rows"] += write_rows(rows1)
        stats["pages"] += 1

        # Fetch remaining pages concurrently, but write sequentially so the
        # single ParquetWriter stays single-threaded (parquet is not async).
        if n_pages > 1:
            sem = asyncio.Semaphore(CONCURRENCY)

            async def one(p: int):
                async with sem:
                    rows, _ = await fetch_page(client, code, p, stats=stats)
                    return p, rows

            tasks = [asyncio.create_task(one(p)) for p in range(2, n_pages + 1)]
            # as_completed gives us bandwidth-bound parallelism but in-order
            # writing isn't required — we dedupe later by mastr_id anyway.
            for fut in asyncio.as_completed(tasks):
                p, rows = await fut
                n = write_rows(rows)
                stats["rows"] += n
                stats["pages"] += 1
                if stats["pages"] % 5 == 0 or stats["pages"] == n_pages:
                    elapsed = time.monotonic() - stats["t0"]
                    print(f"  [{stats['pages']:>3}/{n_pages}] "
                          f"page {p:>3}: +{n:,} rows "
                          f"(total {stats['rows']:,}, {elapsed:.1f}s)",
                          flush=True)

        if writer is not None:
            writer.close()

    stats["elapsed_s"] = time.monotonic() - stats["t0"]
    stats["size_mb"] = out_path.stat().st_size / 1e6 if out_path.exists() else 0
    stats["expected_total"] = total
    print(f"  done: {stats['rows']:,} rows in {stats['elapsed_s']:.1f}s "
          f"({stats['size_mb']:.1f} MB)")
    return stats


async def main_async(techs: list[str]) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    overall_t0 = time.monotonic()
    all_stats: dict[str, dict] = {}
    for tech in techs:
        code = TECHS[tech]
        all_stats[tech] = await scrape_tech(tech, code)

    elapsed = time.monotonic() - overall_t0
    print("\n=== summary ===")
    print(f"total wallclock: {elapsed:.1f}s ({elapsed/60:.2f} min)")
    for tech, s in all_stats.items():
        lats = s["latencies"]
        p95 = float(np.percentile(lats, 95)) if lats else 0
        print(f"  {tech:5s}: {s['rows']:>9,} rows · {s['size_mb']:>6.1f} MB "
              f"· {s['elapsed_s']:>6.1f}s · max-lat {s['max_latency']:.2f}s "
              f"· p95-lat {p95:.2f}s · retries {s['retries']} · "
              f"errors {s['http_errors'] or '{}'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--techs", nargs="+", default=list(TECHS.keys()),
                    choices=list(TECHS.keys()),
                    help="Subset of technologies to scrape (default: all 3)")
    args = ap.parse_args()
    return asyncio.run(main_async(args.techs))


if __name__ == "__main__":
    sys.exit(main())
