# Changelog

All notable changes to this project. Follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Daily-fresh open-mastr SQLite snapshot as primary data source.**
  `pixi run db-mastr-core` populates `data/mastr/open-mastr.db` (~6 GB,
  gitignored) with wind, solar, storage, storage_units, and market_actors
  tables via the open-mastr bulk XML → SQLite pipeline. The home-default
  `~/.open-MaStR/data/sqlite/open-mastr.db` is symlinked to the repo file so
  open-mastr refresh writes in place.
- `mastr_db.py` — thin SQLAlchemy loader exposing `load()`, `load_geo()`,
  `load_for_pipeline()`, `count()`, `list_tables()`. The
  `load_for_pipeline(tech)` adapter returns a DataFrame shaped to the same
  Zenodo-parquet column contract that `mastr_plot.load_from_bulk` consumes,
  so the downstream rename + decoration block applies unchanged.
- `mastr_plot.load_from_bulk(tech, source="auto")` — new `source` parameter
  selects between open-mastr SQLite, the legacy Zenodo parquet directory,
  or auto-detection. `load_records()` and `load_bess()` inherit the same
  switch, so notebooks transparently use SQLite when present.
- `tests/test_open_mastr_parity.py` — 19 integration tests covering schema
  contract, row-count floors, capacity bounds, enum translation, and the
  strict-mode raise when `market_actors` is empty.
- New pixi tasks: `db-mastr-wind`, `db-mastr-solar`, `db-mastr-storage`,
  `db-mastr-storage-units`, `db-mastr-market`, `db-mastr-core`, `db-mastr-all`.
- Weekly CI workflow now refreshes the SQLite snapshot before rendering
  (`SQLITE_DATABASE_PATH` points open-mastr at the repo path; ZIP cleaned up
  post-parse to free disk).

### Changed
- `scripts/build_full_bess.py` rewritten as a thin
  `mastr_db.load_for_pipeline("bess") → parquet` pass; the Zenodo-base
  plus JSON-API delta merge is no longer needed (SQLite is daily-fresh).
  Output path and column schema unchanged for downstream compatibility.

### Added (legacy Zenodo path — retained as fallback)
- **Full-registry bulk loader (open-MaStR Zenodo).** New
  `mastr_plot.load_from_bulk(tech)` reads pre-converted parquet
  snapshots (`solar.parquet`, `wind.parquet`, `storage.parquet`)
  from a sibling `BNetzA_MaStR/` directory. `load_records()` and
  `load_bess()` now auto-prefer this source when present —
  4.86 M PV plants instead of 200 k, **100.9 GW** active
  (was 73 GW), 1.73 M BESS units (was 193 k).
- `mastr_plot.normalise_kreis_name()` for fuzzy Kreis-name matching
  between MaStR post-reform names and the pre-reform GPKG.
  Recovers ~ 84 % of solar GW via name match; remaining ~ 16 % sits
  in Kreise merged during the 2007 / 2011 admin reforms (documented
  in the docstring).
- `mastr_plot.aggregate_by_landkreis_name()` for direct per-Kreis
  rollups without a 4.9 M-point spatial join.
- `scripts/convert_storage_to_parquet.py` — joins the two storage
  CSVs into a unified slim parquet (51 MB, 1.75 M rows).
- `docs/data-sources/zenodo-open-mastr.md` documenting the Zenodo
  bulk dump source, including the limitations (snapshot frozen at
  2025-02-09, residential coordinate anonymisation, geographic-
  reform Kreis-name gap).
- `docs/data-sources/zenodo-wind-goal100.md` documenting the
  Goal100 wind-specific Zenodo dataset (referenced earlier).
- **Pumped-hydro storage (PSH) split out of BESS.** Following the
  battery-charts.de / BVES / EASE / EU SET-Plan reporting convention,
  PSH gets its own section + chart set (`sample-psh-map.svg`,
  `sample-psh-summary.svg`, `sample-psh-top.svg`). Battery-only stats
  in the BESS section now drop from 12.97 GW / 936.9 GWh combined to
  the true Li-ion-and-friends number: **6.49 GW / 9.4 GWh**.
- `mastr_plot.split_bess_storage()` returns three disjoint slices —
  batteries, psh, other (Wasserstoff/Druckluft/Schwungrad pilots).

### Changed
- `render_bess_charts()` now expects the **batteries-only** slice. The
  legacy multi-tech "tech-mix" subchart was dropped (post-split it'd be
  a single bar) and `sample-bess-tech-mix.svg` removed.
- Wind size-bin chart now aggregates **per-project** (`owner_name`
  proxy) instead of per-turbine. Big windparks no longer disappear into
  the 1-10 MW bin — the new bins show 100-1000 MW projects as a real
  thing (offshore farms have one SPV per farm).
- Size-bin bars no longer annotate the unit count above each bar — value
  only.

### Added
- **Per-unit size-bin breakdown for Wind / PV / BESS.** Single shared
  axis of seven log-spaced bins (0-10 kW, 10-100 kW, 100 kW-1 MW,
  1-10 MW, 10-100 MW, 100-1000 MW, 1 GW+). BESS chart has two panels
  (GW + GWh); Wind + PV charts have one panel each (GW only).
  Headline finds:
  - Wind: 75 GW of the 79.7 GW total sits in the 1-10 MW bin.
  - PV (top-200k slice): 22.7 GW in 100 kW-1 MW + 24.9 GW in 1-10 MW.
    Only 6 plants ≥ 100 MW.
  - BESS: 192k units carry 2.9 GW / 3.5 GWh of residential (10-100 kW);
    the 30 units in 100-1000 MW carry 6.2 GW / **765 GWh** alone
    (Pumpspeicher + biggest Li-ion). The 1 GW+ bin stays empty until
    Jänschwalde commissions.
- **BESS three-sector split** (HSS / CSS / LSS) matching the
  battery-charts.de (RWTH Aachen · Figgener et al.) convention also used
  by BVES, EASE, and the EU SET-Plan. Boundaries: < 30 kWh / 30 kWh –
  1 MWh / ≥ 1 MWh.
  - `mastr_plot.bess_sector()` classifier + `BESS_SECTORS` constant.
  - `load_bess()` adds a `sector` column.
  - Three new sample SVGs: per-sector summary (power / energy /
    headcount bars), cumulative-by-sector stacked area, per-sector
    duration histogram.
  - 4 new pytest cases for the classifier; 61 / 61 total green.
  - README + docs: new sector subsection cross-referencing
    battery-charts.de, BVES, EASE, EU SET-Plan.
- **Battery + electricity storage (BESS) coverage.** Scrapes
  `Energieträger=2496` (Speicher) from the same `Stromerzeugung`
  endpoint — top 200 000 units by `Bruttoleistung` (`pixi run scrape-bess`).
  Result: 193 678 active units / **7.86 GW / 98.9 GWh** at 2026-05-01;
  4 193 planned units in the pipeline (Jänschwalde 1 GW, Boxberg 400 MW,
  …) pointing at 25 GW / 2 565 GWh by ~ 2028.
- `parser.BatteryUnit` dataclass with `parser.load_bess()`. Energy
  capacity (`NutzbareSpeicherkapazitaet`), storage tech
  (`Stromspeichertechnologie`), planned commissioning date, voltage
  level, status — all preserved. Kreis + Bundesland are pre-joined in
  source JSON, no spatial join needed.
- `mastr_plot.load_bess()`, `mastr_plot.aggregate_bess_by_unit()` with
  optional `include_planned=True` for the pipeline view.
- Five new BESS sample SVGs: power-per-Kreis choropleth, energy-per-Kreis
  choropleth, capacity-weighted duration histogram (Batterie only — bimodal
  at 1 h hybrid PV + 2-4 h grid-services), tech mix (power + energy
  side-by-side), cumulative growth dual-axis (GW + GWh).
- `bess-2005-may2026.gif` + `.mp4` animations alongside wind + PV.
- 5 new pytest cases covering `BatteryUnit.from_json` (round-trip,
  non-BESS rejection, planned-only records, private-owner flag, missing
  capacity). 57 tests total, all passing in 0.59 s.
- `pixi run scrape-bess`, `pixi run render-bess`, plus `render-gifs`
  bumped from `both` to `all` (wind + PV + BESS).
- New "Battery + electricity storage (BESS)" section in README + docs
  site sidebar.
- Solar PV orientation analysis (capacity-weighted facing + tilt distribution).
- Wind turbine age histogram + repowering signal + upsizing curve.
- `CHANGELOG.md` (this file) and `CONTRIBUTING.md`.
- Animation GIFs extended past year-end: final frame now at 2026-05-01
  captures YTD additions (4 981 new plants registered in 2026 alone).
  New filenames `wind-2005-may2026.gif` + `pv-2005-may2026.gif`;
  `snapshot_dates()` helper makes the cap configurable in one place.
- README "What this method does **not** track" table listing every
  systematically-excluded category (sub-49 kW PV, NaT install rows, NaN
  coords, offshore choropleth, batteries, heat-only, etc.).

### Changed
- Intermediate frames switched from PNG to lossless WebP. Same fidelity
  for ffmpeg, smaller on disk, gentler on the `fig/frames*/` working dirs.
- Animation outputs now emit **two formats per technology** at 1233×1440 px:
  `.gif` (universal autoplay) **and** `.mp4` (H.264, LinkedIn-native,
  ~30 % smaller, sharper colors). `docs/index.html` uses `<video>` with
  `<img>` fallback so MP4 plays inline when supported and GIF carries the
  poster + fallback.

  | Output | Wind | PV |
  |---|---|---|
  | GIF | 688 KB | 1.1 MB |
  | MP4 | 476 KB | 916 KB |

### Fixed
- **Title GW under-reported on choropleths.** `render_samples.py` and
  `render_wind_gif.py` titles previously used `agg["power_gw"].sum()` —
  the per-Kreis-aggregated total — which silently excludes points
  outside any Kreis polygon. For wind that's 3 098 turbines /
  11.3 GW of offshore + slightly-misaligned coastal capacity. Titles
  now use the active-set total (`active["power"].sum() / 1e6`) and
  the map adds a footnote when offshore / out-of-Kreis capacity > 1 GW.
- **`active_at_snap` flag in the parquet/CSV downloads previously
  treated NaT install_date as active**, inconsistent with every other
  filter in the codebase. Now matches `mastr_plot.aggregate_by_unit`.
- **Documentation said offshore lat/lon was anonymised — false.**
  All 1 909 offshore rows carry real `Laengengrad` / `Breitengrad`.
  Only the `StandortAnonymisiert` string is a "Nordsee/Ostsee" label.
  README, CLAUDE.md, and docs/index.html corrected.

### Changed
- Renamed: `wind-2005-2025.gif` → `wind-2005-may2026.gif`,
  `pv-2005-2025.gif` → `pv-2005-may2026.gif`. README + docs/index.html
  + OpenGraph meta tags updated.
- Snapshot date bumped from `2025-01-01` to `2026-05-01` across all
  three derived-data scripts (render_samples, build_kreise_json,
  build_downloads). Density choropleths, top-Kreise table, downloads,
  per-Bundesland chart and the inline doc-site captions now all reflect
  the same point-in-time. New active-flag column in the parquet/CSV
  download renamed `active_2025_01_01` → `active_at_snap`.

## [0.5.0] — 2026-05-18

### Added
- Capacity-density choropleths (MW / km²) for wind and PV.
- Top-10 largest individual plants table.
- README badges: CI status, parser-test count, pixi, marimo.
- `.github/ISSUE_TEMPLATE/` (bug + feature) and `PULL_REQUEST_TEMPLATE.md`.

## [0.4.0] — 2026-05-17

### Added
- Per-Bundesland cumulative stacked-area ramp (2000 → 2025).
- PV-by-installation-type breakdown (building / free / parking_lot / …).
- Energy-type mix chart (Wind, PV, coal, gas, biomass, …).
- 2024 year-over-year additions choropleth (per Kreis).
- Bulk-data downloads (`mastr-snapshot.parquet`, `mastr-snapshot.csv.gz`,
  `mastr-by-kreis.csv.gz`).
- 52 `pytest` cases for `parser.py`.
- `CITATION.cff` for citing the software + upstream data sources.

## [0.3.0] — 2026-05-17

### Added
- PV multi-year animation GIF (2005 → 2025).
- Generalised `scripts/render_wind_gif.py` to handle wind + PV (`{wind|pv|both}`).
- PV scrape widened from 2 to 8 pages of `Bruttoleistung-desc` (top 200 k
  plants ≥ 49 kW).
- Open Graph + Twitter Card meta tags so link previews show the wind GIF.
- Custom SVG favicon (turbine + sun).
- Top-30 operators chart.
- Top-25 offshore-wind operators chart (Nordsee + Ostsee).

### Changed
- Workflow now scrapes 8 PV pages by default; commit-back add-list extended
  for `pv-*.gif`.

## [0.2.0] — 2026-05-17

### Added
- Sortable, filterable per-Kreis HTML table (all 434 Kreise) with search.
- `scale=jenks | linear | log` picker on both marimo notebooks.
- GitHub Actions weekly refresh workflow.
- Helper scripts under `scripts/` (render_samples, render_wind_gif,
  build_kreise_json).

## [0.1.0] — 2026-05-16

### Added
- Read-the-Docs-styled GitHub Pages site at `/docs`.
- Interactive marimo notebooks: `pv.py` (PV explorer) + `wind.py`
  (Wind explorer), exported as standalone HTML.
- Shared helpers module `mastr_plot.py` with synthetic-demo fallback.
- Four SVG pipeline diagrams (flowchart, architecture, enum-decoding,
  timeline).
- Wind multi-year animation GIF (2005 → 2025).
- Per-Bundesland stacked-bar chart.
- `pixi.toml` for reproducible env (osx-arm64 / osx-64 / linux-64 / win-64).
- `germany_kreise.gpkg` built from `isellsoap/deutschlandGeoJSON` (~1 MB)
  instead of the 3.5 GB OSM extract, with a `bundesland` column.
- `CLAUDE.md` with project conventions.

### Changed
- PV sample switched from "first 125 k by default sort" to "top 50 k by
  `Bruttoleistung-desc`" — 20× the spatial signal at the same row count.

## [0.0.1] — 2025-07-20

### Added
- Initial fork of [emmericp/marktstammdatenplotter](https://github.com/emmericp/marktstammdatenplotter).
- Original `parser.py` (`PowerPlant` dataclass + JSON-to-record decoder).
- Original `wind.ipynb` notebook.
- README with scrape / OSM / ffmpeg recipes.
