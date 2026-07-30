# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Marktstammdatenregister plotter** — Python toolkit that loads the German
Marktstammdatenregister (MaStR) registry of electricity-generating units, joins
records to Kreis (county) polygons, and renders time-lapse choropleth maps and
analysis charts of installed wind, solar and storage capacity.

- Upstream fork of <https://github.com/emmericp/marktstammdatenplotter>
- Data source: <https://www.marktstammdatenregister.de>
- Output: lossless WebP frames → animated GIF + H.264 MP4, plus SVG charts.

Research-grade code, mostly AI-generated and deliberately verbose. Rendered
outputs are published to a GitHub Pages site under `docs/`.

## Commands

Everything runs through pixi (`pixi install` first). The repo pins its own env;
do not use the global `~/.pixi/envs/main` python here.

```bash
pixi run test                   # pytest tests/ -v
pixi run tests                  # alias
```

Run a single test or class:

```bash
pixi run python -m pytest tests/test_parser.py::TestLeistungsbegrenzung -v
pixi run python -m pytest tests/test_parquet_store.py -q
```

Data refresh (see **Data sources** for which to use):

```bash
pixi run db-mastr-core          # wind+solar+storage+storage_units+market -> parquet
pixi run db-mastr-all           # every table
pixi run db-mastr-wind          # single tech; also -solar -storage -market
pixi run db-mastr-parquet       # convert an existing open-mastr.db to parquet
pixi run scrape-bess            # incremental JSON-API pull (resumable)
pixi run fetch-kreise           # rebuild germany_kreise.gpkg from BKG VG2500
```

Rendering and docs:

```bash
pixi run render-samples         # all sample SVGs + Bundesland/operator charts
pixi run render-gifs            # multi-year animations (also -wind -pv -bess)
pixi run render-wind-monthly    # finer cadence: halfyear | monthly variants
pixi run build-kreise           # per-Kreis JSON for the sortable docs table
pixi run build-downloads        # docs/data/ CSV + parquet bulk artefacts
pixi run wind-edit              # marimo editor (also pv-edit, wind-run, pv-run)
pixi run docs-build             # re-export both notebooks to docs/*.html
```

## Architecture

The pipeline is **load → normalise → aggregate → render**, with `mastr_plot.py`
as the hub. Understanding the loader dispatch is the key to this codebase.

### Loading: three interchangeable backends behind one contract

`mastr_plot.load_from_bulk(tech, source=...)` is the single entry point for
bulk data, where `tech ∈ {"pv", "wind", "bess"}`. `_resolve_bulk_source`
picks the store; `source="auto"` (default) prefers, in order:

1. **`parquet`** — `data/mastr/parquet/`, one zstd file per table, read through
   DuckDB (`mastr_parquet.py`). The default and fastest path.
2. **`sqlite`** — `data/mastr/open-mastr.db`, the legacy open-mastr snapshot
   (`mastr_db.py`). Not present by default; only written with `--sqlite`.
3. **`zenodo`** — frozen Zenodo parquet dump under `BNetzA_MaStR/`, which still
   carries a few fields open-mastr drops (see below).

All three emit the **same column contract** (`mastr_id`, `gross_capacity_kw`,
`commissioning_date`, `longitude`, `landkreis`, `owner_name`, …), which
`load_from_bulk` then renames to the JSON-scrape schema (`power`,
`install_date`, `removal_date`) so downstream rendering is backend-agnostic.
`tests/test_open_mastr_parity.py` and `tests/test_parquet_store.py` enforce it.

**The SQL lives in exactly one place.** `mastr_db.pipeline_sql()` builds the
query and `mastr_db.finalise_pipeline_frame()` does the post-processing (date
coercion, PV enum translation, PSH energy backfill); `mastr_parquet` executes
that same SQL against DuckDB views named after the tables. When changing what
the pipeline selects, change `pipeline_sql` — never fork it per backend, or the
stores drift apart silently.

A fourth path, the **custom JSON-API scrape** (`fetch_mastr.py` + `parser.py`),
is separate: resumable per-page caches for incremental top-N-by-power pulls.
`mastr_plot.load_records` falls back to it, then to synthetic demo data, so the
notebooks always render.

### Rendering

`mastr_plot.py` (~1000 lines) holds normalisation (`normalise_kreis_series`),
Kreis aggregation, and the choropleth/chart builders. `scripts/render_samples.py`
and `scripts/render_wind_gif.py` drive it for CI; `wind.py` / `pv.py` are marimo
notebooks that expose the same helpers interactively.

## Data sources

Prefer `pixi run db-mastr-core`: it downloads MaStR's bulk XML export and writes
**straight to parquet**, never materialising the SQLite DB. Measured on the
2026-06-16 export (34 tables, 38.0 M rows):

| Store | Size | PV pipeline load |
|---|---|---|
| SQLite | 11.32 GB | 107 s |
| parquet + zstd | 1.30 GB | 3.6 s |

The size drop is zstd over columnar chunks; the speedup is the
row-store/column-store split (SQLite stores all 96 columns of a solar row
contiguously, so projecting 16 still pages through the other 80).

`scripts/mastr_download.py` gets this by monkeypatching open-mastr, which only
knows how to write SQL. It replaces `create_database_table` (no-op) and
`add_table_to_sqlite_database` (writes a parquet part), leaving upstream's XML
parsing, katalogwerte decoding and cleansing untouched. Pass `--sqlite` for the
legacy path, `--date=YYYYMMDD --keep-old` to reprocess an export already on disk
(without `--keep-old`, open-mastr deletes the ~3 GB zip for any other date).

When touching that writer, three things are load-bearing:

- **It only works single-process.** `get_number_of_processes()` returns -1
  unless `NUMBER_OF_PROCESSES` / `USE_RECOMMENDED_NUMBER_OF_PROCESSES` is set;
  with a process pool the children re-import open-mastr and resolve the
  *original* function, silently writing SQLite. `_install_parquet_writer` hard-
  fails on those env vars.
- **Parquet has no constraints.** SQLite deduplicated via
  `INSERT ... ON CONFLICT DO NOTHING`; `finalise_table` reproduces it with
  `row_number() OVER (PARTITION BY <pk> ORDER BY _part_seq) = 1` (keep-first).
- **Timestamps are microseconds, not nanoseconds.** `market_actors.Taetigkeits-
  beginn` reaches back to year 0100, which overflows pandas' ns range of
  1677–2262. Parts with mismatched units cannot be merged at all.

**Where the store lives.** `mastr_parquet.PARQUET_DIR` prefers the in-checkout
`data/mastr/parquet/` and falls back to `~/.open-MaStR/data/parquet`, which is
symlinked to the main checkout's copy. That fallback is what makes git worktrees
work: a fresh worktree's own `data/mastr/` is empty, and without it `source="auto"`
walks past parquet and SQLite and silently serves **synthetic demo data** — which
looks like a successful render, not an error. Check
`mastr_plot.load_records()[1]` (`is_demo`) if numbers look wrong in a worktree.

Both population paths restore types from open-mastr's ORM (`orm_dtypes`), so
`Inbetriebnahmedatum` is a real `DATE` and booleans are `BOOLEAN` rather than
SQLite's TEXT and INTEGER 0/1. Columns the XML never populates are still
materialised as all-NULL, because SQLite got them from `CREATE TABLE` and
dropping them would break `SELECT Meldedatum FROM wind_extended`.

### Known open-mastr gaps (why the Zenodo path still exists)

- **PSH energy**: `NutzbareSpeicherkapazitaet` is NULL for ~85 % of pumped-hydro
  units. `mastr_db._backfill_psh_energy_from_zenodo` fills it from the Zenodo
  parquet when present.
- **PV location granularity**: open-mastr's `ArtDerSolaranlage` has only 4 values
  and collapses "Großparkplatz" (~93 rows) and "Gewässer" (~33 rows) into
  "Bauliche Anlagen (Sonstige)". The raw `Lage` column exists but is NULL for
  all 6.1 M rows. Use `source="zenodo"` if you need that distinction.
- **Storage energy lives in `storage_units`**, not `storage_extended` (where the
  column is always NULL), joined one-to-one via `VerknuepfteEinheit`.
- **Anonymisation**: units < 30 kW have NULL coordinates under MaStR publication
  rules. `load_geo(..., drop_anonymised=True)` skips them; aggregate to Kreis via
  the name fallback in `mastr_plot.py` instead.

## Data model

`parser.PowerPlant` decodes MaStR's numeric enum codes (`Leistungsbegrenzung`,
`HauptausrichtungSolarModule`, `HauptneigungswinkelSolarmodule`,
`ArtDerSolaranlageId`, `NutzungsbereichGebSA`, `WindAnLandOderSeeId`) for the
JSON-API path only — the bulk paths arrive pre-decoded. When extending it:

- Keep enum lookups as `match`/`case`; one branch per documented MaStR code.
- Treat unknown codes as `None`, never raise — the registry adds codes over time.
- Dates arrive as .NET `/Date(ms)/` strings; parse with `parse_dotnet_date`.
- Coordinates are EPSG:4326 (`Laengengrad`, `Breitengrad`).

## Conventions

- Geo stack: `geopandas`, `shapely`, `pyogrio`, `mapclassify`, `matplotlib`;
  DuckDB for anything touching the parquet store.
- Vectorize with pandas/numpy — no row-wise `for` or `.apply(lambda)`. Note
  `normalise_kreis_series`: it maps over ~400 unique names rather than millions
  of rows. Follow that pattern.
- Timestamps: store UTC, tz-aware. The frame loop compares against tz-naive
  snapshot dates, so `load_from_bulk` strips tz at the boundary — keep that
  conversion local, do not propagate tz-naive values upward.
- `Bruttoleistung` is kW; divide by 1e6 for GW legends.
- Frame filenames are `<series>-NNN.webp` (zero-padded) so ffmpeg's sort glob works.
- Type hints on new functions; modern syntax (`list[str]`, `str | None`).
- Prefer parquet/feather over CSV for any intermediate cache.

## Gotchas

- **Offshore turbines carry real coordinates.** `StandortAnonymisiert` is only a
  sea *label* ("Nordsee" / "Ostsee"); lat/lon are intact (verified 1909/1909
  rows). The original notebook drew synthetic sea rectangles anyway — that
  workaround is not required by the data. Kreis aggregation still drops these
  points because no Kreis covers open sea, so offshore is reported separately
  via a sea-zone lookup.
- **Hamburg's MultiPolygon** contains "Nationalpark Hamburgisches Wattenmeer" as
  part_id 2, stripped explicitly. Re-verify the part_id if rebuilding the GPKG
  from a newer extract. `pixi run fetch-kreise` now uses BKG VG2500 (post-reform
  names, ~400 rows), which closed the ~16 % name-match gap left by the old
  OSM/isellsoap GeoJSON.
- **MaStR's API silently drops the second filter clause** when ANDed across
  different fields, e.g. `Energieträger~eq~'2495'~and~ArtDerSolaranlageId~eq~'852'`
  returns the same count as the first clause alone. Filter client-side instead.
- **`keep_old_downloads` defaults to False**, which deletes the cached 3 GB XML
  zip. Pass `keep_old_downloads=True` when re-running against a local export.
- ENTSO-E under-reports NL solar by ~10x. Irrelevant here (MaStR is DE-only) but
  do not mix ENTSO-E NL solar/wind into any cross-check.

## Style

- Plotting code is research-grade and verbose. Do not aggressively refactor it
  unless asked — preserve function signatures so saved animations stay
  reproducible.
- New helpers go in `mastr_plot.py`, `parser.py`, or a new module — not inline in
  the notebooks.
- Match existing comment density: short, English, only where intent is
  non-obvious. Comments should explain *why*, since the *what* is usually clear.

## Docs site

`docs/` is a static, Read-the-Docs-styled site (no Sphinx). Edit
`docs/index.html` directly; `docs/pv.html` and `docs/wind.html` are generated by
`pixi run docs-build`. SVGs live in `docs/assets/` and `fig/`. GitHub Pages
serves `/docs` on `main`, refreshed by `.github/workflows/refresh-docs.yml`.
