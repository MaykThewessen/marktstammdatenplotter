# open-mastr SQLite Integration Plan

**Date:** 2026-05-19 22h21 GMT+2
**Author:** Mayk (+ Claude)
**Goal:** Make open-mastr's daily-fresh SQLite snapshot a first-class data source for the pipeline, sitting alongside (and eventually replacing) the Zenodo bulk parquet + JSON-scrape paths.

---

## Phase 0 — Discovery (DONE)

Three parallel sweeps produced ground truth. Findings consolidated below.

### open-mastr v0.17.1 API surface
| Item | Reality | Source |
|---|---|---|
| `Mastr()` ctor | `Mastr(engine="sqlite", connect_to_translated_db=False)` — no DB path arg | `~/.pixi/envs/main/lib/python3.12/site-packages/open_mastr/mastr.py:75` |
| `download()` sig | `download(method="bulk", data=None, date=None, bulk_cleansing=True, keep_old_downloads=False, ...)` | `mastr.py:103` |
| Allowed `data=` values | `wind, solar, biomass, hydro, gsgk, combustion, nuclear, gas, storage, storage_units, electricity_consumer, location, market, grid, balancing_area, permit, deleted_units, deleted_market_actors, retrofit_units` | `mastr.py` |
| `method="API"` | Removed in >0.16; only `MaStRAPI` SOAP wrapper remains | `getting_started.md` |
| DB path control | `SQLITE_DATABASE_PATH` env var only; default `~/.open-MaStR/data/sqlite/open-mastr.db` | `helpers.py:50` |
| `bulk_cleansing=True` | Replaces ID enums with strings via `Katalogwerte.xml` | `xml_download/utils_cleansing_bulk.py:11` |

### Database state today
- DB at `data/mastr/open-mastr.db` (in-repo, 6.3 GB, gitignored). Symlinked from `~/.open-MaStR/data/sqlite/`.
- Populated: `wind_extended` (42,500), `solar_extended` (6,128,902), `storage_extended` (2,526,080)
- Empty: `market_actors`, `basic_units` — only populated if `data=["market"]` or full download.
- `Energietraeger` strings: `Wind`, `Solare Strahlungsenergie`, `Speicher` — **exact match** with existing pipeline constants.

### Existing pipeline contract
`mastr_plot.load_from_bulk(tech) → DataFrame` produces these columns (consumed across `mastr_plot.py`, `render_samples.py`, `wind.py`, `pv.py`, `render_wind_gif.py`, `scripts/build_full_bess.py`):

**Always:** `id, power, power_kw, install_date, removal_date, longitude, latitude, landkreis, landkreis_norm, municipality_key, energy_type, off_shore, owner_name, name, bundesland`
**PV-only:** `installation_type, building_type, facing, tilt, is_private`
**BESS-only:** `energy_kwh, planned_date, effective_date, duration_h, storage_tech, sector, is_battery, is_psh`

Derived inside `load_from_bulk` (we don't need to provide): `landkreis_norm`, `effective_date`, `duration_h`, `sector`, `is_battery`, `is_psh`.

### Tests state
- `tests/test_parser.py` (310 LOC) — unit tests for enum decoding only. No parity, no integration.
- No `conftest.py`, no fixtures. CI runs `pixi run test` weekly.
- Zero capacity/row-count assertions anywhere.

### Schema mapping (authoritative)

| Pipeline column | open-mastr SQL column | Tech | Notes |
|---|---|---|---|
| `id` | `EinheitMastrNummer` | all | str |
| `power` / `power_kw` | `Bruttoleistung` | all | kW; copy to both |
| `name` | `NameStromerzeugungseinheit` | all | |
| `install_date` | `Inbetriebnahmedatum` | all | DATE → datetime64[ns]; **tz-naive already** |
| `removal_date` | `DatumEndgueltigeStilllegung` | all | |
| `planned_date` | `GeplantesInbetriebnahmedatum` | BESS | |
| `longitude` | `Laengengrad` | all | NULL for <30 kW |
| `latitude` | `Breitengrad` | all | NULL for <30 kW |
| `landkreis` | `Landkreis` | all | always populated |
| `bundesland` | `Bundesland` | all | |
| `municipality_key` | `Gemeindeschluessel` | all | 8-digit AGS string |
| `energy_type` | `Energietraeger` | all | already correct strings |
| `energy_kwh` | `NutzbareSpeicherkapazitaet` | BESS | kWh |
| `storage_tech` | `Speichertechnologie` | BESS | already string |
| `off_shore` | derive: `Seelage` field or `WindAnLandOderAufSee == "Windkraft auf See"` + lon-based Nordsee/Ostsee | wind | match existing logic at `mastr_plot.py:567–578` |
| `owner_name` | LEFT JOIN `market_actors ON market_actors.MastrNummer = e.AnlagenbetreiberMastrNummer` SELECT `market_actors.Firmenname` (or fallback to `Personenart`) | all | **requires `market_actors` populated** |
| `installation_type` | `Lage` | PV | open-mastr decoded string; map to existing enums (`Bauliche Anlagen`, `Freifläche`, `Steckersolar`) |
| `building_type` | `Nutzungsbereich` | PV | map to existing strings |
| `facing` | `HauptausrichtungSolarmodule` | PV | already string → translate to int degrees per `parser.py:42–70` |
| `tilt` | `HauptneigungswinkelSolarmodule` | PV | already string → map to `(min, max)` tuple |
| `is_private` | derive: `Nutzungsbereich == "Haushalt"` | PV | |

---

## Phase 1 — Schema adapter in `mastr_db.py`

**What to implement:** Add a function `load_for_pipeline(tech: Literal["wind","pv","bess"]) -> pd.DataFrame` that returns the exact column contract that `mastr_plot.load_from_bulk` produces for the source columns (derived columns stay in `load_from_bulk`).

**File:** `mastr_db.py` (append new function; do not refactor existing `load`/`load_geo`).

**Pattern to follow:** Copy the rename+decoration block from `mastr_plot.py:476–597` as a checklist of which columns must exist post-adapter. Do not invent new column names.

**Implementation steps:**
1. Define module-level `_COLUMN_MAP` dict per tech (mirrors the table in Phase 0 above).
2. Pull only mapped columns from SQLite via `SELECT col1, col2, ... FROM <table>` to avoid loading 50+ unused columns (solar_extended has 96 cols × 6.1M rows = wasted memory).
3. LEFT JOIN to `market_actors` for `owner_name`. If `market_actors` is empty, fill with NULL (graceful — existing pipeline already handles this).
4. Apply per-tech enum translations for PV (`facing`, `tilt`, `installation_type`, `building_type`). Reuse `parser.py`'s match/case blocks — copy, don't re-derive.
5. Wind: derive `off_shore` exactly per `mastr_plot.py:567–578`.
6. BESS: do NOT compute `effective_date`/`duration_h`/`sector`/`is_battery`/`is_psh` — `load_from_bulk` does these.
7. Set `energy_type` per tech constant (sanity vs DB).
8. Strip tz on all date columns (open-mastr DATE columns parse tz-naive but be defensive).

**Doc references:**
- Column contract source: `mastr_plot.py:430–597` (read the rename map + every `if col in df.columns` branch).
- PV enum maps: `parser.py:42–143` (copy match/case for facing + tilt + installation/building).
- Offshore logic: `mastr_plot.py:567–578`.
- AGS5 disambiguation already lives in `mastr_plot.py:316–366` — adapter must NOT duplicate it; only ensure `municipality_key` is provided as 8-digit string.

**Verification checklist:**
- [ ] `set(mastr_db.load_for_pipeline("wind").columns)` ⊇ `{id, power, power_kw, install_date, removal_date, longitude, latitude, landkreis, municipality_key, energy_type, off_shore, owner_name, name, bundesland}`
- [ ] PV adapter also produces `installation_type, building_type, facing, tilt, is_private`
- [ ] BESS adapter also produces `energy_kwh, planned_date, storage_tech`
- [ ] `df["install_date"].dtype == 'datetime64[ns]'` (tz-naive)
- [ ] `df["energy_type"].unique()` matches `["Wind"]` / `["Solare Strahlungsenergie"]` / `["Speicher"]`
- [ ] Row counts: `wind ≥ 42_000`, `solar ≥ 6_000_000`, `storage ≥ 2_500_000`
- [ ] No SQLAlchemy or pandas warnings (`PerformanceWarning`, `SettingWithCopyWarning`).

**Anti-patterns to avoid:**
- ❌ Do NOT reuse `mastr_db.load_geo()` here — that drops anonymised rows; the pipeline needs them for Kreis-name fallback.
- ❌ Do NOT invent open-mastr columns. PRAGMA verified columns are the only valid source.
- ❌ Do NOT compute `effective_date` or `duration_h` — `load_from_bulk` already does. Duplication would diverge.
- ❌ Do NOT pull `SELECT *` from `solar_extended` (96 cols × 6.1M = memory pain).

---

## Phase 2 — Wire adapter into `mastr_plot.load_from_bulk`

**What to implement:** Add `source` parameter to `load_from_bulk`:
```python
def load_from_bulk(
    tech: str,
    bulk_dir: Path | None = None,
    source: Literal["zenodo", "sqlite", "auto"] = "auto",
) -> tuple[pd.DataFrame, bool]:
```

Auto-resolution:
- `auto`: if `mastr_db.DB_PATH.exists()` AND it has data → use SQLite. Else fall back to existing parquet/Zenodo path.
- `sqlite`: force SQLite; raise if DB missing/empty.
- `zenodo`: force existing parquet path (current behavior).

**Doc references:**
- Current `load_from_bulk` body: `mastr_plot.py:430–597`. Keep the rename+decoration block — it stays the contract enforcer. Adapter from Phase 1 produces a DataFrame ready to bypass the renames; the bulk-cleansing path should detect "already adapted" via presence of `power_kw` and skip rename.
- Better: have adapter produce raw `gross_capacity_kw`/`commissioning_date`/etc. names so renames apply uniformly. Decide in implementation; prefer second option (smaller diff to current code).

**Verification checklist:**
- [ ] `load_from_bulk("wind", source="auto")` and `load_from_bulk("wind", source="sqlite")` return identical schemas (column sets equal).
- [ ] `load_from_bulk("wind", source="zenodo")` still works against existing `BNetzA_MaStR/*.parquet` (no regression).
- [ ] Function signature backward-compatible (callers without `source=` see no behavior change unless DB exists).

**Anti-patterns:**
- ❌ Do NOT remove the parquet path. Keep it as fallback — Zenodo is the legacy ground truth.
- ❌ Do NOT auto-switch silently if SQLite row counts look anomalous (e.g., <10% of parquet). Add a sanity guard that falls back to parquet with a warning.

---

## Phase 3 — Replace `scripts/build_full_bess.py` with SQLite-direct path

**What to implement:** Current script merges `BNetzA_MaStR/storage.parquet` (Zenodo base) + JSON-API delta (`data-bess/`) into `full-storage.parquet`. With SQLite as primary, the merge becomes:

```python
df = mastr_db.load_for_pipeline("bess")
df.to_parquet("BNetzA_MaStR/full-storage.parquet")
```

The output parquet stays so legacy callers and CI artefact uploads keep working.

**Doc references:**
- Current script: `scripts/build_full_bess.py` (read top-to-bottom; ~150 LOC).
- Output schema: must match `load_from_bulk("bess")` post-adapter contract (Phase 1 + Phase 2 work).

**Verification checklist:**
- [ ] Output `full-storage.parquet` has same column set as before.
- [ ] Row count within ±5% of pre-replacement file (SQLite is fresher; expect more rows).
- [ ] Capacity sum (`df.power_kw.sum() / 1e6`) within ±2 GW of pre-replacement.
- [ ] `pixi run build-full-bess` runs in <30s (vs current ~2 min Zenodo+delta merge).

**Anti-patterns:**
- ❌ Do NOT drop the delta JSON cache. `fetch_mastr.py` + JSON pages stay for incremental top-N use cases. Just bypass them in this script.
- ❌ Do NOT rename the output parquet. Path is referenced in `pixi.toml`, `render_samples.py`, and `mastr_plot.find_bulk_dir`.

---

## Phase 4 — Marimo notebooks (`wind.py`, `pv.py`)

**What to implement:** Swap initial data-load cell from `load_records(...)` to `load_from_bulk(tech, source="auto")`. The rest of each notebook is plotting code — unchanged.

**Doc references:**
- `wind.py:50–60` and `pv.py:50–60` — data-load cells (verify exact line numbers when implementing).
- Both already use tz-naive comparisons (`mastr_plot.py:160`) — no extra date handling needed.

**Verification checklist:**
- [ ] `pixi run wind-edit` loads + renders without errors.
- [ ] `pixi run pv-edit` same.
- [ ] HTML exports (`pixi run docs-build`) match pre-change byte-for-byte (or within <5% page-size delta).
- [ ] No new tz-aware warnings.

**Anti-patterns:**
- ❌ Do NOT refactor plotting cells. Per `CLAUDE.md`: "Plotting code is research-grade and verbose. Do not aggressively refactor."

---

## Phase 5 — Parity test harness

**What to implement:** New file `tests/test_open_mastr_parity.py` (~120 LOC) with markers to skip if DB missing.

Test cases:
1. `test_wind_row_count` — open-mastr wind ≥ 42_000 rows.
2. `test_solar_row_count` — open-mastr solar ≥ 6_000_000 rows.
3. `test_storage_row_count` — open-mastr storage ≥ 2_500_000 rows.
4. `test_wind_capacity_total` — sum(power_kw)/1e6 within `[60, 200]` GW (lifetime including retired).
5. `test_solar_capacity_total` — within `[60, 200]` GW.
6. `test_storage_capacity_total` — within `[5, 60]` GW.
7. `test_schema_contract` — `set(df.columns) ⊇ expected_columns_set` for each tech.
8. `test_datetime_naive` — install_date is tz-naive datetime64.
9. `test_energy_type_constants` — distinct values match `["Wind"]`/`["Solare Strahlungsenergie"]`/`["Speicher"]`.
10. `test_zenodo_vs_sqlite_top10_kreise_wind` — top-10 Kreise by wind GW agree on names + ranks within 1 swap (Zenodo is older, exact match unrealistic).

**Doc references:**
- Test patterns: `tests/test_parser.py` (existing style — pytest, no fixtures, plain asserts).
- `pixi.toml:139–141` for how `pixi run test` invokes pytest.
- Add `@pytest.mark.skipif(not mastr_db.DB_PATH.exists(), reason="DB not populated")` on every test — CI without DB shouldn't fail.

**Verification checklist:**
- [ ] All 10 tests pass locally.
- [ ] `pixi run test` exits 0 even if `data/mastr/open-mastr.db` is missing (skip, not fail).
- [ ] Add `pixi run db-mastr-core` step to CI before tests OR keep test as opt-in only.

**Anti-patterns:**
- ❌ Do NOT hard-code expected exact row counts (registry grows daily). Use `>=` bounds.
- ❌ Do NOT compare per-row to Zenodo parquet — Zenodo is months older; row IDs differ. Compare aggregates.

---

## Phase 6 — Documentation + cleanup

**What to update:**
1. `CLAUDE.md` — already updated (Phase 0 of session) with paths, symlink note, refresh task.
2. `README.md` — add a "Data sources" section near the top mentioning open-mastr as primary, Zenodo as legacy fallback. Reference `pixi run db-mastr-core`.
3. `CHANGELOG.md` — add entry for v0.3.0: "Switch primary data source to open-mastr SQLite; Zenodo parquet retained as fallback."
4. `pixi.toml` — add `db-mastr-market` task: `python -c "from open_mastr import Mastr; Mastr().download(data=['market'])"` so `owner_name` JOIN works.
5. `.github/workflows/refresh-docs.yml` — optionally add `pixi run db-mastr-core` step before render. Adds ~30 min to weekly job. Decide based on CI runner disk space (6 GB DB).

**Verification checklist:**
- [ ] `grep -r "open-mastr" README.md CHANGELOG.md` returns hits.
- [ ] `pixi task list | grep db-mastr` shows 6 tasks (existing 5 + new `db-mastr-market`).
- [ ] If CI updated: `pixi run db-mastr-market && pixi run test` runs green.

**Anti-patterns:**
- ❌ Do NOT remove `fetch_mastr.py` or its pixi tasks. Incremental top-N use case (matched-power scrape for BESS) is still useful.
- ❌ Do NOT commit the DB file (already in `.gitignore`, but double-check before push).

---

## Final Phase — Verification

1. Run full parity test suite: `pixi run test`.
2. Grep for anti-patterns:
   - `rg "SELECT \* FROM (wind|solar|storage)_extended" mastr_db.py` — should be empty (always project columns).
   - `rg "Mastr\(.*data=" pixi.toml | grep -v "'market'" | grep -v "'wind'" | grep -v "'solar'" | grep -v "'storage'"` — verify task strings only use documented `data=` values.
3. Re-render sample SVGs: `pixi run render-samples`. Compare a sample (e.g., `sample-wind-map.svg`) byte size against `git show HEAD:fig/sample-wind-map.svg | wc -c`. Acceptable deviation: ±10%.
4. Rebuild animations: `pixi run render-bess`. Spot-check first 3 frames visually.
5. Commit per phase. Final commit message: `Switch primary data source to open-mastr SQLite`.

---

## Risks (re-stated)

1. **`market_actors` table empty** — first action of Phase 1 must be to populate it via `pixi run db-mastr-market` (adds <1 min to download). Without it `owner_name` is NULL for all rows; `render_samples.py` operator-ranking chart shows "Unknown" group instead of real names.
2. **Solar 6.1M rows** — `load_for_pipeline("pv")` is 30 s + ~3 GB RAM. May need pre-filter (`Bruttoleistung > 0`, drop NULL Inbetriebnahmedatum) at SQL level for callers that only need aggregated views.
3. **PV enum translation drift** — open-mastr's bulk-cleansing emits German strings (e.g., `Lage = "Bauliche Anlagen (Hausdach, Gebäude und Fassade)"`); existing pipeline categories are English-ish (e.g., `installation_type = "Bauliche Anlagen"`). Need exact string-match table — verify against `parser.py` enum lookup before claiming compatibility.
4. **DB freshness in CI** — GitHub Actions runner has ~14 GB free. 6.3 GB DB + 2 GB ZIP + work cache fits but tight. If CI fails on disk, gate `db-mastr-core` behind a manual workflow_dispatch only.

---

## Suggested execution order

| Step | Phase | Est. effort | Blocker for |
|---|---|---|---|
| 0 | Populate `market_actors` (`pixi run db-mastr-market`) | 5 min | Phase 1 owner_name |
| 1 | Phase 1 adapter `mastr_db.load_for_pipeline` | 1.5 h | Phases 2–4 |
| 2 | Phase 5 parity tests (write first, fail until Phase 1 lands) | 1 h | Validates Phase 1 |
| 3 | Phase 2 wire into `load_from_bulk` | 30 min | Phase 4 |
| 4 | Phase 3 replace `build_full_bess.py` | 30 min | independent |
| 5 | Phase 4 notebooks | 15 min | Phase 6 docs |
| 6 | Phase 6 docs + CI | 30 min | — |

**Total:** ~4.5 hours focused work.

---

## Decision points (need user input before Phase 1)

1. **Should `data/mastr/` stay in repo or move to a sibling dir?** Currently 6.3 GB lives in the working tree. If you'd rather it sit at `~/data/` or similar, change `_REPO_DB` in `mastr_db.py` and the symlink.
2. **CI policy:** include `pixi run db-mastr-core` in the weekly refresh job, or keep DB as developer-local? Affects whether parity tests run in CI.
3. **Strict mode for `owner_name`:** if `market_actors` empty, should adapter raise or silently NULL-fill? Recommendation: NULL-fill + log a one-time warning.
