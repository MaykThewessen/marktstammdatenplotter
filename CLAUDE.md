# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

**Marktstammdatenregister plotter** — Python toolkit that scrapes the German
Marktstammdatenregister (MaStR) public registry of electricity-generating
units, joins records to OSM-derived county polygons, and renders time-lapse
choropleth maps of installed wind and solar capacity.

- Upstream: <https://github.com/emmericp/marktstammdatenplotter>
- Data source: <https://www.marktstammdatenregister.de> (public JSON API)
- Output: per-month PNG frames → ffmpeg GIF/MP4 animations.

## Layout

```
parser.py           Dataclass + JSON-to-PowerPlant decoder for MaStR records
wind.ipynb          End-to-end map rendering (load → join → plot → save frames)
fig/                Rendered PNG/GIF outputs (gitignored)
docs/               Read-the-Docs-style site published via GitHub Pages
README.md           User-facing quickstart
```

## Data model

`parser.PowerPlant` decodes MaStR's numeric enum codes (`Leistungsbegrenzung`,
`HauptausrichtungSolarModule`, `HauptneigungswinkelSolarmodule`,
`ArtDerSolaranlageId`, `NutzungsbereichGebSA`, `WindAnLandOderSeeId`) into
human-readable strings. When extending the parser:

- Keep enum lookups as `match`/`case`; one branch per documented MaStR code.
- Treat unknown codes as `None`, never raise — the registry adds codes over time.
- Dates arrive as .NET `/Date(ms)/` strings; parse with `parse_dotnet_date`.
- Coordinates are EPSG:4326 (`Laengengrad`, `Breitengrad`).

## Conventions

- Use the pixi global `main` env: `~/.pixi/envs/main/bin/python`.
- Geo stack: `geopandas`, `shapely`, `pyogrio`, `mapclassify`, `matplotlib`.
- Tabular work: vectorize with pandas/numpy — no row-wise `for` or `.apply(lambda)`.
- Timestamps: store UTC, tz-aware. The notebook strips tz before comparing against
  date-only filters — keep that conversion local, do not propagate tz-naive values.
- Filenames for frames: `<series>-NNN.png` (zero-padded) so ffmpeg + sort glob works.
- Prefer feather/parquet over CSV for any intermediate cache.

## Common tasks

- **Refresh raw data**: re-run the `curl | xargs` block in README.md against the
  MaStR API. Cache JSON on disk — the API is slow and rate-limited. Re-runs
  should skip files that already exist.
- **Rebuild county polygons**: rerun the `osmfilter` + `ogr2ogr` chain from a
  fresh `germany-latest.o5m`. Output is `germany_kreise.gpkg`.
- **Render an animation**: open `wind.ipynb`, run all cells. Frames land in
  `fig/`, then assemble with the ffmpeg block in README.md.

## Gotchas

- ENTSO-E systematically under-reports NL solar (~10% of installed). This repo
  uses MaStR (DE only), so the issue does not apply here — but do **not** mix
  ENTSO-E NL solar/wind into any cross-check.
- The Hamburg `MultiPolygon` in OSM contains "Nationalpark Hamburgisches
  Wattenmeer" as part_id 2; the notebook strips it explicitly. Keep that fix
  if rebuilding from a newer OSM extract — verify the part_id has not shifted.
- `Bruttoleistung` is in kW; the notebook divides by 1e6 to get GW for legends.
- Off-shore turbines carry **real** `Laengengrad` / `Breitengrad` (verified
  for 1 909 / 1 909 offshore rows). The `StandortAnonymisiert` string is
  *labelled* "Nordsee" / "Ostsee" — but coordinates are intact. The original
  notebook draws synthetic sea rectangles anyway; that workaround is not
  required by the data and can be dropped if you want point-accurate offshore
  rendering. Aggregation to Kreis polygons still drops these points because
  no Kreis covers open sea — handle with a separate "sea zone" lookup if
  per-farm aggregation matters.

## Style

- Plotting code is research-grade and verbose. Do not aggressively refactor
  the notebook unless asked — preserve existing function signatures so saved
  animations stay reproducible.
- New helpers go in `parser.py` (or a new module), not the notebook.
- Match existing comment density: short, in English, only where intent is
  non-obvious.

## Docs site

`docs/` is a static, Read-the-Docs-styled site (no Sphinx). Edit
`docs/index.html` directly; SVG diagrams live in `docs/assets/` and `fig/`.
GitHub Pages serves from `/docs` on `main`.
