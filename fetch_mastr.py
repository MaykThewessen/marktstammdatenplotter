"""
Fetch energy unit data from the Marktstammdatenregister (MaStR) API.

Supported energy types:
  wind     Windenergie (code 2497)              — ~42k entries, ~2 pages full
  solar    Solare Strahlungsenergie (code 2495) — ~5.9M entries, incremental only!
  storage  Speicher (code 2496)                 — ~2.5M entries, use incremental or
                                                  --sort power --max-pages 8 for top-by-kW

Two modes:
  --mode full        Download pages (all, or up to --max-pages)
  --mode incremental Download only entries commissioned after --since date
                     (sorts descending by date, stops at cutoff — typically 1 page)

Flags:
  --sort FIELD       API sort key  (default: "" = API default order)
                     Useful values: Bruttoleistung-desc  InbetriebnahmeDatum-desc
  --max-pages N      Stop after N pages (default: all)
  --per-page         Save each page as page-NNN.json immediately (resumable).
                     Pages that already exist on disk are skipped — no HTTP request.
                     Without this flag a single merged file is written at the end.

Date format in API: /Date(milliseconds_utc)/

Usage examples:
  # Incremental wind fetch since a date
  python fetch_mastr.py --energy wind --mode incremental --since 2026-02-19

  # Full wind download (2 pages)
  python fetch_mastr.py --energy wind --mode full

  # BESS top-200k by installed power → data-bess/ (matches pixi scrape-bess)
  python fetch_mastr.py --energy storage --mode full \\
      --sort Bruttoleistung-desc --max-pages 8 --per-page --out-dir data-bess

  # Storage incremental since Zenodo cutoff
  python fetch_mastr.py --energy storage --mode incremental --since 2025-02-10
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
    "wind":    {"code": "2497", "label": "Windenergie",              "pages_full": "~2"},
    "solar":   {"code": "2495", "label": "Solare Strahlungsenergie", "pages_full": "~237 — use incremental!"},
    "storage": {"code": "2496", "label": "Speicher",                 "pages_full": "~102 — use incremental or --max-pages"},
}

PAGE_SIZE = 25000
REQUEST_DELAY = 2  # seconds between pages


def fetch_page(page: int, energy_code: str, sort: str = "") -> dict:
    params = urllib.parse.urlencode({
        "sort":      sort,
        "page":      page,
        "pageSize":  PAGE_SIZE,
        "group":     "",
        "filter":    f"Energieträger~eq~'{energy_code}'",
        "forExport": "true",
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


def _page_path(out_dir: Path, page: int) -> Path:
    return out_dir / f"page-{page:03d}.json"


def run_full(energy_code: str, energy_name: str, out_dir: Path,
             sort: str = "", max_pages: int | None = None, per_page: bool = False):
    """Download entries (all pages, or up to max_pages)."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # Probe total from page 1 (may skip HTTP if file exists in per-page mode).
    p1_path = _page_path(out_dir, 1) if per_page else None
    if per_page and p1_path.exists():
        with open(p1_path, encoding="utf-8") as f:
            first = json.load(f)
        print(f"  Page 1: loaded from cache ({len(first['Data'])} entries)")
    else:
        first = fetch_page(1, energy_code, sort)
        if per_page:
            _save_page(first["Data"], p1_path)
            print(f"  Page 1/{first['Total'] // PAGE_SIZE + 1}: {len(first['Data'])} entries → {p1_path.name}")

    total = first["Total"]
    pages = -(-total // PAGE_SIZE)
    if max_pages is not None:
        pages = min(pages, max_pages)
    print(f"Full mode [{energy_name}]: {total:,} total entries · fetching {pages} pages"
          + (f" (sort={sort})" if sort else ""))

    if not per_page:
        all_entries = list(first["Data"])
        print(f"  Page 1/{pages}: {len(all_entries)} entries")

    for page in range(2, pages + 1):
        p_path = _page_path(out_dir, page) if per_page else None
        if per_page and p_path.exists():
            with open(p_path, encoding="utf-8") as f:
                data = json.load(f)
            print(f"  Page {page}/{pages}: loaded from cache ({len(data['Data'])} entries)")
        else:
            time.sleep(REQUEST_DELAY)
            data = fetch_page(page, energy_code, sort)
            if per_page:
                _save_page(data["Data"], p_path)
                print(f"  Page {page}/{pages}: {len(data['Data'])} entries → {p_path.name}")

        if not per_page:
            all_entries.extend(data["Data"])
            print(f"  Page {page}/{pages}: +{len(data['Data'])} entries (total {len(all_entries):,})")

    if not per_page:
        save(all_entries, energy_name, "full", out_dir)
        return all_entries
    return None  # per-page mode writes files directly; callers should not use return value


def run_incremental(since: datetime, energy_code: str, energy_name: str, out_dir: Path):
    """
    Fetch only entries commissioned after `since`, sorting descending by date.
    Stops when a page contains entries older than the cutoff.
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
            if dt is not None and dt > since:
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


def _save_page(entries: list, path: Path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"Total": len(entries), "Data": entries}, f, ensure_ascii=False)


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
    ap.add_argument("--sort", default="",
                    help="API sort key, e.g. Bruttoleistung-desc or InbetriebnahmeDatum-desc")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Stop after this many pages (full mode only)")
    ap.add_argument("--per-page", action="store_true",
                    help="Save each page as page-NNN.json (resumable; skips existing)")
    ap.add_argument("--out-dir", default="non-pv-data/mastr-api")
    args = ap.parse_args()

    cfg = ENERGY_TYPES[args.energy]

    if args.mode == "full" and args.energy == "solar" and args.max_pages is None:
        print(f"WARNING: Solar full download = {cfg['pages_full']} pages.")
        print("Consider --mode incremental --since <date> or --max-pages N.")
        print("Proceeding in 5 seconds... (Ctrl-C to abort)")
        time.sleep(5)

    out_dir = Path(args.out_dir)

    if args.mode == "full":
        run_full(cfg["code"], args.energy, out_dir,
                 sort=args.sort, max_pages=args.max_pages, per_page=args.per_page)
    else:
        since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        run_incremental(since_dt, cfg["code"], args.energy, out_dir)
