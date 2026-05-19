# Bulk MaStR data source — open-MaStR / Zenodo

**Reference dataset:** *open-MaStR — Marktstammdatenregister* (full bulk dump)

| | |
|---|---|
| **Zenodo URL** | <https://zenodo.org/records/14783581> |
| **Version** | `2025-02-09` |
| **Type** | Dataset · Open |
| **Format** | Compressed CSV per technology, pre-parsed from the BNetzA XML dump |
| **License** | Datenlizenz Deutschland 2.0 ([dl-de/by-2-0](https://www.govdata.de/dl-de/by-2-0)) |
| **Maintainer** | Reiner Lemoine Institut (RLI) + OpenEnergyPlatform |
| **Python wrapper** | [`open-mastr`](https://github.com/OpenEnergyPlatform/open-MaStR) |

## What it is

A pre-parsed snapshot of the **complete** Bundesnetzagentur
Marktstammdatenregister (MaStR), exported as CSV per technology.
This is the same authoritative source the MaStR website serves from —
upstream is the BNetzA daily XML dump, downloaded + parsed once by the
RLI team and shipped to Zenodo at a roughly-monthly cadence.

## Why use it instead of the JSON-API scrape

The MaStR public JSON API (`EinheitJson/...`) is paginated at 25 000
rows/page and was designed for the website's filtered browser, not for
bulk download. Full PV coverage via that API would mean ~ 245 pages
(~ 6 GB JSON, slow). The Zenodo bulk dump fixes this:

| | JSON API scrape | Zenodo bulk dump |
|---|---|---|
| PV row count | 200 000 (top-N sort cap) | **4.96 M** (full registry) |
| Wind row count | 32 107 | 38 325 |
| Storage row count | 200 000 (top-N) | **1.76 M** |
| Total download | ~ 1.6 GB JSON | ~ 5 GB CSV |
| Refresh cadence | Anytime (rate-limited) | ~ Monthly |
| Per-row Kreis | Spatial join required | Pre-joined text column |
| Per-row Bundesland | Spatial join required | Pre-joined text column |
| Owner name | Present | Anonymised for residential |
| Auth | None | None |

## Files in this snapshot

After unpacking the local download at
`/Users/mayk/DE_Wind_marktstammdatenplotter/marktstammdatenplotter/BNetzA_MaStR/`:

| File | Size | Rows | Content |
|---|---|---|---|
| `bnetza_mastr_solar_raw.csv` | 3.7 GB | 4 956 567 | Full PV registry |
| `bnetza_mastr_wind_raw.csv` | 40 MB | 40 339 | Full wind registry |
| `bnetza_mastr_storage_raw.csv` | 1.2 GB | 1 755 769 | Storage extended metadata |
| `bnetza_mastr_storage_units_raw.csv` | 170 MB | 1 755 232 | Storage units (per-unit rows) |
| `solar.parquet` | 125 MB | 4 953 448 | Slim parquet derived from solar CSV |
| `wind.parquet` | 2 MB | 38 325 | Slim parquet derived from wind CSV |
| `storage.parquet` | 51 MB | 1 755 302 | Built by `scripts/convert_storage_to_parquet.py` from the two storage CSVs |

The parquet files use **zstd compression** and English column names
(`gross_capacity_kw`, `commissioning_date`, `landkreis`, …) — see
`mastr_plot.load_from_bulk()` for the column-rename map.

## How `mastr_plot` consumes it

`load_records()` and `load_bess()` auto-detect the bulk parquet dump and
prefer it over the JSON scrape when present. Override with
`prefer_bulk=False` to force the legacy JSON path.

Search order:
1. `$MASTR_BULK_DIR` env var if set
2. Walk up from `REPO_ROOT` for a sibling `BNetzA_MaStR/` (works from
   git worktrees).

## How to refresh the snapshot

```bash
# Method A — open-mastr CLI (does its own XML→SQLite parse)
pixi add --pypi open-mastr
pixi run python -c "
from open_mastr import Mastr
db = Mastr()
db.download(method='bulk')
"

# Method B — pull the next Zenodo version directly
curl -L -o open-mastr.zip \
  'https://zenodo.org/api/records/<latest-record-id>/files-archive'
unzip open-mastr.zip -d BNetzA_MaStR/
pixi run python scripts/convert_storage_to_parquet.py
```

## Known limitations

- **Snapshot date is frozen** (2025-02-09). Plants commissioned after
  that don't appear until the RLI team publishes the next release.
- **Residential coordinates anonymised** — ~ 95 % of PV plants have
  NaN lat/lon. The `landkreis` and `bundesland` text columns are
  pre-joined per row, so per-Kreis aggregation works by name match
  (see `mastr_plot.normalise_kreis_name`) without a spatial join.
- **Geographic-reform mismatch** — the upstream GPKG
  (`germany_kreise.gpkg`, derived from `isellsoap/deutschlandGeoJSON`)
  still carries pre-2007 / pre-2011 Kreis names ("Anhalt-Zerbst",
  "Bördekreis", "Nordvorpommern") whereas MaStR uses the post-reform
  merged names ("Anhalt-Bitterfeld", "Börde", "Vorpommern-Greifswald").
  Current name match recovers ~ 84 % of solar GW.
- **Owner name absent for residential** — top-operators chart skips
  rows without `owner_name`. The Wind chart falls back to `wind_park`
  (the project / Windpark name) which is preserved.

## Citation

```bibtex
@dataset{open_mastr_zenodo_14783581,
  title        = {open-MaStR — Marktstammdatenregister bulk snapshot},
  version      = {2025-02-09},
  year         = 2025,
  publisher    = {Zenodo},
  doi          = {10.5281/zenodo.14783581},
  url          = {https://zenodo.org/records/14783581}
}
```

## Related
- BNetzA XML source: <https://www.marktstammdatenregister.de/MaStR/Datendownload>
- open-MaStR Python package: <https://github.com/OpenEnergyPlatform/open-MaStR>
- Goal100 wind-specific corrections: [zenodo-wind-goal100.md](zenodo-wind-goal100.md)
