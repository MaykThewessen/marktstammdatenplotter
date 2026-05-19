# Changelog

All notable changes to this project. Follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
