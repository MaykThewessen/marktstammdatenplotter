# Changelog

All notable changes to this project. Follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Solar PV orientation analysis (capacity-weighted facing + tilt distribution).
- Wind turbine age histogram + repowering signal + upsizing curve.
- `CHANGELOG.md` (this file) and `CONTRIBUTING.md`.

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
