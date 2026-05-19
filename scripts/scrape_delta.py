#!/usr/bin/env python
"""Scrape the MaStR JSON-API delta since the open-MaStR Zenodo snapshot (2025-02-09).

Downloads one page (pageSize=25000) per output file under ``data-delta-{tech}/``
for tech in {pv, bess, wind}. Resumable: a page is re-downloaded only if its
file is missing or its in-file ``Data`` length doesn't match expected page size
(or remainder on the last page).

Run with the global pixi env:

    ~/.pixi/envs/main/bin/python scripts/scrape_delta.py

Notes
-----
* The API filter uses literal spaces in field name ("Inbetriebnahmedatum der
  Einheit"); ``curl --data-urlencode`` handles the encoding.
* 4 parallel curl workers (well within the API's rate limits in practice).
  Drop to 2 if 4xx/5xx storms appear.
* Each page is ~ 100 MB of JSON; the full pull is ~ 7 GB on disk.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

API_URL = (
    "https://www.marktstammdatenregister.de/MaStR/Einheit/EinheitJson/"
    "GetErweiterteOeffentlicheEinheitStromerzeugung"
)

PAGE_SIZE = 25_000
CUTOFF_DDMMYYYY = "09.02.2025"   # day after the Zenodo snapshot
MAX_WORKERS = 4
TIMEOUT_SEC = 600                 # per-page curl timeout

# (tech, Energieträger code, expected total rows). Totals are the values
# reported by the probe in the task brief and were re-verified at run time.
TECHS = [
    ("pv",   "2495", 1_070_752),
    ("bess", "2496",   702_124),
    ("wind", "2497",     1_385),
]


def page_filename(out_dir: Path, page: int) -> Path:
    return out_dir / f"data-{page}.json"


def expected_rows_for_page(page: int, total: int, page_size: int) -> int:
    """Number of rows the JSON API should return on `page` for `total` rows."""
    if page < 1:
        return 0
    start = (page - 1) * page_size
    if start >= total:
        return 0
    return min(page_size, total - start)


def page_is_complete(path: Path, expected_rows: int) -> bool:
    """True iff `path` exists and its `Data` array length equals expected_rows."""
    if not path.exists() or path.stat().st_size < 100:
        return False
    try:
        with path.open("rb") as f:
            d = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    rows = len(d.get("Data") or [])
    return rows == expected_rows


def download_page(code: str, page: int, out_path: Path) -> tuple[int, bool, str]:
    """Run curl for one page. Returns (page, ok, message)."""
    tmp_path = out_path.with_suffix(".tmp")
    filter_str = (
        f"Energieträger~eq~'{code}'~and~"
        f"Inbetriebnahmedatum der Einheit~gt~'{CUTOFF_DDMMYYYY}'"
    )
    cmd = [
        "curl",
        "-sS",
        "--fail",
        "--retry", "3",
        "--retry-delay", "5",
        "--max-time", str(TIMEOUT_SEC),
        "-G", API_URL,
        "--data-urlencode", "sort=",
        "--data-urlencode", f"page={page}",
        "--data-urlencode", f"pageSize={PAGE_SIZE}",
        "--data-urlencode", "group=",
        "--data-urlencode", f"filter={filter_str}",
        "--data-urlencode", "forExport=true",
        "-o", str(tmp_path),
    ]
    try:
        proc = subprocess.run(
            cmd, check=False, capture_output=True, text=True,
            timeout=TIMEOUT_SEC + 30,
        )
    except subprocess.TimeoutExpired:
        tmp_path.unlink(missing_ok=True)
        return page, False, f"timeout after {TIMEOUT_SEC}s"
    if proc.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        return page, False, f"curl exit {proc.returncode}: {proc.stderr.strip()[:200]}"
    # Atomic move on success.
    shutil.move(tmp_path, out_path)
    return page, True, "ok"


def scrape_tech(tech: str, code: str, total: int) -> int:
    out_dir = ROOT / f"data-delta-{tech}"
    out_dir.mkdir(parents=True, exist_ok=True)
    n_pages = math.ceil(total / PAGE_SIZE)
    print(f"\n=== {tech} (code {code}) — total={total:,}, pages={n_pages} ===")

    # Decide which pages need downloading.
    todo: list[int] = []
    for page in range(1, n_pages + 1):
        expected = expected_rows_for_page(page, total, PAGE_SIZE)
        path = page_filename(out_dir, page)
        if page_is_complete(path, expected):
            continue
        todo.append(page)
    print(f"  pages already complete: {n_pages - len(todo)} / {n_pages}")
    if not todo:
        return 0

    failures: list[tuple[int, str]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(
                download_page, code, page, page_filename(out_dir, page),
            ): page
            for page in todo
        }
        for fut in as_completed(futures):
            page = futures[fut]
            try:
                _, ok, msg = fut.result()
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, f"exception: {exc!r}"
            done += 1
            if not ok:
                failures.append((page, msg))
                print(f"  [{done}/{len(todo)}] page {page}: FAIL — {msg}")
            elif done % 5 == 0 or done == len(todo):
                print(f"  [{done}/{len(todo)}] page {page}: ok")

    # Verify row counts.
    incomplete: list[tuple[int, int, int]] = []
    for page in range(1, n_pages + 1):
        expected = expected_rows_for_page(page, total, PAGE_SIZE)
        path = page_filename(out_dir, page)
        if not page_is_complete(path, expected):
            try:
                with path.open("rb") as f:
                    d = json.load(f)
                got = len(d.get("Data") or [])
            except Exception:  # noqa: BLE001
                got = -1
            incomplete.append((page, expected, got))
    if incomplete:
        print(f"  WARNING: {len(incomplete)} pages still incomplete after run:")
        for page, exp, got in incomplete[:10]:
            print(f"    page {page}: expected {exp} rows, got {got}")
    if failures:
        print(f"  {len(failures)} failures total.")
    return len(failures) + len(incomplete)


def main() -> int:
    errors = 0
    for tech, code, total in TECHS:
        errors += scrape_tech(tech, code, total)
    if errors:
        print(f"\nDONE with {errors} problem pages.")
        return 1
    print("\nAll pages downloaded and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
