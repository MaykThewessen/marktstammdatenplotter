# Wind data source — Goal100 / Zenodo

**Reference dataset:** *Corrected and supplemented unit data on approved wind turbines in Germany*

| | |
|---|---|
| **Zenodo DOI** | [10.5281/zenodo.18697247](https://doi.org/10.5281/zenodo.18697247) |
| **Record URL** | <https://zenodo.org/records/18697247> |
| **Version** | `2026_02_19` |
| **Publication date** | 2026-02-19 |
| **License** | [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) |
| **File** | `goal100_mastr_wind_corrected_epsg_25832_2026_02_19.zip` (~4.9 MB) |
| **CRS (native)** | EPSG:25832 — ETRS89 / UTM zone 32N |
| **CRS (CSV extras)** | EPSG:4326 — WGS84 (`lon_x`, `lat_y` columns) |
| **Publisher** | [Goal100](https://goal100.org) (Berlin) |

## Creators

- Oehmichen, Gunnar — Goal100
- Krämer, Kevin — Goal100
- Carstensen, Bahne — Goal100
- Alefs, Katharina — Goal100
- Debski, Rosmarie — Goal100
- Ortmann, Jakob — Goal100 ([ORCID 0000-0002-3967-1333](https://orcid.org/0000-0002-3967-1333))

## What it is

A **cleaned, corrected, augmented** version of the German wind-turbine slice
of the **Marktstammdatenregister (MaStR)** maintained by the
Bundesnetzagentur. The upstream MaStR is the same source that
`marktstammdatenplotter` scrapes via its public JSON API; the Goal100
dataset is one step downstream — already enriched with the corrections
listed below.

## Why use it instead of raw MaStR

The raw MaStR has well-known data-quality issues for wind. Goal100 fixes
three of them systematically:

### 1. Approval-date overwrites
The original `Genehmigungsdatum` of a wind unit can be **silently overwritten**
in the live MaStR by the operator or project developer. Goal100 retrieves
the MaStR daily via [open-mastr](https://github.com/OpenEnergyPlatform/open-MaStR)
(Hülk et al. 2025), tracks the overwrites, and keeps the *originally entered*
approval date — making historical aggregations reproducible.

### 2. Conflicting administrative-area assignments
Over **7 000 wind units** (as of 2025-02-27) show conflicts between the
declared administrative area (`Bundesland` / `Landkreis` / `Gemeinde`) and
the declared geographical coordinates. Goal100 applies corrections published
by **Manske (2025)** for power, height, rotor diameter, and geolocation,
and trusts those over the raw MaStR. Manske's corrections override the
daily MaStR pull for the affected records.

13 recently added units (`einheit_betriebsstatus IN ('In Realisierung',
'In Betrieb')`) have coordinates that BNetzA published *outside of
Germany* and aren't yet in Manske's corrections. Goal100 keeps the rows
but blanks out the coordinates.

### 3. Permit-seeking project supplement
Goal100 collected permit data independently from **nine German federal
states**. Where a permit-seeking record can be matched to a MaStR unit by
coordinates + technical specs, Goal100 overwrites the MaStR's application
and approval dates with the state-supplied dates.

## How to download

```bash
# Direct (no auth required)
curl -L -o goal100_mastr_wind.zip \
  'https://zenodo.org/api/records/18697247/files/goal100_mastr_wind_corrected_epsg_25832_2026_02_19.zip/content'
unzip goal100_mastr_wind.zip

# Or via Python
python -c "
import urllib.request, json
r = json.loads(urllib.request.urlopen('https://zenodo.org/api/records/18697247').read())
url = r['files'][0]['links']['self']
urllib.request.urlretrieve(url, 'goal100_mastr_wind.zip')
"
```

## Citation

```bibtex
@dataset{goal100_2026_18697247,
  author       = {Oehmichen, Gunnar and
                  Krämer, Kevin and
                  Carstensen, Bahne and
                  Alefs, Katharina and
                  Debski, Rosmarie and
                  Ortmann, Jakob},
  title        = {Corrected and supplemented unit data on approved
                  wind turbines in Germany},
  month        = feb,
  year         = 2026,
  publisher    = {Zenodo},
  version      = {2026_02_19},
  doi          = {10.5281/zenodo.18697247},
  url          = {https://doi.org/10.5281/zenodo.18697247}
}
```

## Related references

- **MaStR** — Marktstammdatenregister: <https://www.marktstammdatenregister.de/MaStR>
  · Licence: [dl-de/by-2-0](https://www.govdata.de/dl-de/by-2-0)
- **open-mastr** (Hülk et al. 2025) — Python wrapper that downloads the full
  MaStR XML dump and parses it to SQLite/Parquet:
  <https://github.com/OpenEnergyPlatform/open-MaStR>
- **Manske (2025)** — Coordinate + geometric-property corrections for wind
  units (referenced in the Zenodo description).
- **Goal100 public dashboard** — <https://goal100.org>

## Comparison to this repo's MaStR scrape

| | This repo (live JSON API) | Goal100 Zenodo |
|---|---|---|
| Source | MaStR public JSON endpoint | MaStR XML bulk dump (via open-mastr) |
| Coverage | Top-N by `Bruttoleistung` | All German wind units, including permit-seeking |
| Approval-date stability | Live (overwritten by operators) | Frozen at first-observed value |
| Coordinate corrections | None | Manske (2025) overrides applied |
| Cross-state permit data | Not included | Nine states cross-referenced |
| Refresh cadence | On-demand (weekly CI) | Roughly monthly |
| Format | JSON pages | ZIP of CSV (EPSG:25832 + 4326 columns) |

## Disclaimer (from Goal100)

> The data provided by Goal100, including the published "corrected master
> data" (korrigierte Stammdaten) on wind turbines, are compiled with the
> utmost care and regularly updated. Nevertheless, Goal100 makes no warranty
> for the accuracy, completeness, currentness, or continuous availability of
> the data. All information provided is for general informational purposes
> only. It does not constitute technical, economic, or legal advice and is
> not to be understood as a basis for investment, planning, or operational
> decisions. Users bear the sole responsibility for the evaluation of the
> data and its use in their own applications or business processes.
>
> Goal100 shall not be liable for any property damage or financial loss
> arising from the use, evaluation, or interpretation of the data, including
> indirect damages, such as lost profits, misjudgments in site selection,
> or operational disruptions.
