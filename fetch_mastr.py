"""
Fetch energy unit data from the Marktstammdatenregister (MaStR) API.

Supported energy types:
  wind   Windenergie (code 2497) — ~42k entries, 2 pages full / 1 page incremental
  solar  Solare Strahlungsenergie (code 2495) — ~5.9M entries, use incremental only!

Two modes:
  --mode full        Download all entries (wind: ~2 pages; solar: ~237 pages!)
  --mode incremental Download only entries commissioned after --since date
                     (sorts descending, stops at cutoff — typically 1 page)

Date format in API: /Date(milliseconds_utc)/

Usage:
  python fetch_mastr.py --energy wind --mode incremental --since 2026-02-19
  python fetch_mastr.py --energy wind --mode full
  python fetch_mastr.py --energy solar --mode incremental --since 2026-03-01
"""

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = (
    "https://www.marktstammdatenregister.de"
    "/MaStR/Einheit/EinheitJson/GetErweiterteOeffentlicheEinheitStromerzeugung"
)

ENERGY_TYPES = {
    "wind":  {"code": "2497", "label": "Windenergie",             "pages_full": "~2"},
    "solar": {"code": "2495", "label": "Solare Strahlungsenergie","pages_full": "~237 — use incremental!"},
}

PAGE_SIZE = 25000
REQUEST_DELAY = 2   # seconds between pages to avoid rate-limiting


def fetch_page(page: int, energy_code: str, sort: str = "") -> dict:
    params = urllib.parse.urlencode({
        "sort":       sort,
        "page":       page,
        "pageSize":   PAGE_SIZE,
        "group":      "",
        "filter":     f"Energieträger~eq~'{energy_code}'",
        "forExport":  "true",
    })
    req = urllib.request.urlopen(f"{API_URL}?{params}", timeout=60)
    return json.loads(req.read())


def parse_dotnet_date(date_str: str | None) -> datetime | None:
    """Convert /Date(ms)/ to a UTC datetime."""
    if not date_str:
        return None
    try:
        ms = int(date_str.strip("/Date()").split("+")[0].split("-")[0])
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (ValueError, AttributeError):
        return None


def run_full(energy_code: str, energy_name: str, out_dir: Path):
    """Download all entries for the given energy type."""
    first = fetch_page(1, energy_code)
    total = first["Total"]
    pages = -(-total // PAGE_SIZE)
    print(f"Full mode [{energy_name}]: {total:,} entries across {pages} pages")

    all_entries = list(first["Data"])
    print(f"  Page 1/{pages}: {len(all_entries)} entries")

    for page in range(2, pages + 1):
        time.sleep(REQUEST_DELAY)
        data = fetch_page(page, energy_code)
        all_entries.extend(data["Data"])
        print(f"  Page {page}/{pages}: +{len(data['Data'])} entries (total {len(all_entries):,})")

    save(all_entries, energy_name, "full", out_dir)
    return all_entries


def run_incremental(since: datetime, energy_code: str, energy_name: str, out_dir: Path):
    """
    Fetch only entries commissioned after `since`, sorting descending by date.
    Stops as soon as all entries on a page pre-date the cutoff.
    """
    print(f"Incremental mode [{energy_name}]: entries commissioned after {since.date()}")
    all_entries = []
    page = 1

    while True:
        data = fetch_page(page, energy_code, sort="InbetriebnahmeDatum-desc")
        entries = data["Data"]
        total = data["Total"]
        print(f"  Page {page} ({total:,} total in API): {len(entries)} entries scanned")

        new_count = 0
        cutoff_hit = False
        for e in entries:
            dt = parse_dotnet_date(e.get("InbetriebnahmeDatum"))
            if dt is None or dt > since:
                all_entries.append(e)
                new_count += 1
            else:
                cutoff_hit = True

        print(f"    → {new_count} newer than cutoff")

        if cutoff_hit or len(entries) < PAGE_SIZE:
            break

        page += 1
        time.sleep(REQUEST_DELAY)

    print(f"\nTotal new entries fetched: {len(all_entries)}")
    if all_entries:
        save(all_entries, energy_name, "incremental", out_dir)
    return all_entries


def save(entries: list, energy_name: str, label: str, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")
    out_path = out_dir / f"mastr_{energy_name}_{label}_{ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"Total": len(entries), "Data": entries}, f, ensure_ascii=False)
    print(f"Saved {len(entries)} entries → {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--energy", choices=list(ENERGY_TYPES), default="wind",
                    help="Energy type to fetch (default: wind)")
    ap.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    ap.add_argument("--since", default="2026-02-19",
                    help="Cutoff date for incremental mode (YYYY-MM-DD)")
    ap.add_argument("--out-dir", default="non-pv-data/mastr-api")
    args = ap.parse_args()

    cfg = ENERGY_TYPES[args.energy]

    if args.mode == "full" and args.energy == "solar":
        print(f"WARNING: Solar full download = {cfg['pages_full']} pages. This will take a very long time.")
        print("Consider using --mode incremental --since <date> instead.")
        print("Proceeding in 5 seconds... (Ctrl-C to abort)")
        time.sleep(5)

    out_dir = Path(args.out_dir)

    if args.mode == "full":
        run_full(cfg["code"], args.energy, out_dir)
    else:
        since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        run_incremental(since_dt, cfg["code"], args.energy, out_dir)
