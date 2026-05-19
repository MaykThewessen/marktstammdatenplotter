"""
Parity tests for the open-mastr SQLite adapter (mastr_db.load_for_pipeline).

These tests verify that the SQLite-backed loader produces a DataFrame that
satisfies the column contract consumed by mastr_plot.load_from_bulk, and that
aggregate capacity / row counts land in plausible ranges for the live German
registry.

The tests skip when the SQLite DB or market_actors table is missing — typical
in fresh checkouts where `pixi run db-mastr-core && pixi run db-mastr-market`
hasn't run yet. Run those once locally to enable the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# Allow `from mastr_db import ...` when pytest is invoked from any CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mastr_db  # noqa: E402

# ---------------------------------------------------------------------------
# Skip the whole module if the DB or market_actors are missing — these tests
# are integration-level and require a populated SQLite snapshot.
# ---------------------------------------------------------------------------

_DB_OK = mastr_db.DB_PATH.exists()
if _DB_OK:
    try:
        _MARKET_ROWS = pd.read_sql(
            "SELECT COUNT(*) AS n FROM market_actors",
            con=mastr_db.engine(),
        ).iloc[0, 0]
    except Exception:
        _MARKET_ROWS = 0
else:
    _MARKET_ROWS = 0

pytestmark = pytest.mark.skipif(
    not _DB_OK or _MARKET_ROWS == 0,
    reason=(
        "open-mastr SQLite or market_actors not populated. Run "
        "`pixi run db-mastr-core && pixi run db-mastr-market` to enable."
    ),
)

# ---------------------------------------------------------------------------
# Expected schema columns produced by load_for_pipeline. These are the names
# that mastr_plot.load_from_bulk renames + decorates downstream.
# ---------------------------------------------------------------------------

_COMMON_COLS = {
    "mastr_id",
    "einheit_name",
    "gross_capacity_kw",
    "commissioning_date",
    "decommissioning_date",
    "longitude",
    "latitude",
    "landkreis",
    "bundesland",
    "municipality_key",
    "owner_name",
}

_EXPECTED_COLS = {
    "wind": _COMMON_COLS | {"location_type", "seelage", "wind_park"},
    "pv":   _COMMON_COLS | {"location_type", "usage_type", "orientation", "tilt"},
    "bess": _COMMON_COLS | {
        "usable_capacity_kwh",
        "planned_commissioning_date",
        "storage_technology",
    },
}

# Plausible total installed-capacity (lifetime, kW summed including retired
# units) ranges for Germany. Loose bounds — registry grows daily.
_CAPACITY_BOUNDS_GW = {
    "wind": (60, 250),     # ~70 GW operational + retired ~30 GW
    "pv":   (50, 300),     # ~80 GW operational + small residential pile
    "bess": (5, 100),      # rapidly growing
}

_ROW_COUNT_MIN = {
    "wind": 40_000,
    "pv":   5_000_000,
    "bess": 2_000_000,
}


# ---------------------------------------------------------------------------
# Fixtures — load once per session, reuse across tests for speed.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def wind_df() -> pd.DataFrame:
    return mastr_db.load_for_pipeline("wind")


@pytest.fixture(scope="module")
def bess_df() -> pd.DataFrame:
    return mastr_db.load_for_pipeline("bess")


@pytest.fixture(scope="module")
def pv_df() -> pd.DataFrame:
    # 6+ M rows; loaded once, reused.
    return mastr_db.load_for_pipeline("pv")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSchemaContract:
    def test_wind_columns(self, wind_df: pd.DataFrame) -> None:
        missing = _EXPECTED_COLS["wind"] - set(wind_df.columns)
        assert not missing, f"wind missing columns: {missing}"

    def test_pv_columns(self, pv_df: pd.DataFrame) -> None:
        missing = _EXPECTED_COLS["pv"] - set(pv_df.columns)
        assert not missing, f"pv missing columns: {missing}"

    def test_bess_columns(self, bess_df: pd.DataFrame) -> None:
        missing = _EXPECTED_COLS["bess"] - set(bess_df.columns)
        assert not missing, f"bess missing columns: {missing}"

    @pytest.mark.parametrize("tech_fixture", ["wind_df", "bess_df", "pv_df"])
    def test_commissioning_date_is_tz_naive_datetime(
        self, tech_fixture: str, request: pytest.FixtureRequest,
    ) -> None:
        df: pd.DataFrame = request.getfixturevalue(tech_fixture)
        dtype = df["commissioning_date"].dtype
        assert "datetime64" in str(dtype), f"{tech_fixture} commissioning_date dtype={dtype}"
        # tz-naive: no `tz` attribute or attribute is None
        tz = getattr(dtype, "tz", None)
        assert tz is None, f"{tech_fixture} commissioning_date should be tz-naive; got tz={tz}"


class TestRowCounts:
    def test_wind_rows(self, wind_df: pd.DataFrame) -> None:
        assert len(wind_df) >= _ROW_COUNT_MIN["wind"]

    def test_pv_rows(self, pv_df: pd.DataFrame) -> None:
        assert len(pv_df) >= _ROW_COUNT_MIN["pv"]

    def test_bess_rows(self, bess_df: pd.DataFrame) -> None:
        assert len(bess_df) >= _ROW_COUNT_MIN["bess"]


class TestCapacitySums:
    @pytest.mark.parametrize("tech,fixture", [
        ("wind", "wind_df"), ("pv", "pv_df"), ("bess", "bess_df"),
    ])
    def test_total_gw_in_plausible_range(
        self, tech: str, fixture: str, request: pytest.FixtureRequest,
    ) -> None:
        df: pd.DataFrame = request.getfixturevalue(fixture)
        gw = df["gross_capacity_kw"].sum() / 1e6
        lo, hi = _CAPACITY_BOUNDS_GW[tech]
        assert lo <= gw <= hi, f"{tech} total {gw:.1f} GW outside [{lo}, {hi}]"


class TestEnumValues:
    def test_wind_location_type_values(self, wind_df: pd.DataFrame) -> None:
        actual = set(wind_df["location_type"].dropna().unique())
        expected = {"Windkraft an Land", "Windkraft auf See"}
        assert actual <= expected, f"unexpected wind location_type values: {actual - expected}"

    def test_pv_location_type_translated_to_zenodo_strings(self, pv_df: pd.DataFrame) -> None:
        # Must match the keys of mastr_plot.py's loc_map (lines 519–526).
        allowed = {
            "Bauliche Anlagen (Hausdach, Gebäude und Fassade)",
            "Steckerfertige Solaranlage (sog. Balkonkraftwerk)",
            "Bauliche Anlagen (Sonstige)",
            "Freifläche",
            "Großparkplatz",
            "Gewässer",
        }
        actual = set(pv_df["location_type"].dropna().unique())
        leftover = actual - allowed
        assert not leftover, f"pv location_type has untranslated values: {leftover}"

    def test_pv_usage_type_translated_to_zenodo_strings(self, pv_df: pd.DataFrame) -> None:
        # Must overlap with usage_map keys (mastr_plot.py:529–536).
        expected_zenodo = {
            "Haushalt",
            "Gewerbe, Handel und Dienstleistungen",
            "Industrie",
            "Land-, Forst- und Fischereiwirtschaft",
            "Öffentliche Einrichtungen",
        }
        actual = set(pv_df["usage_type"].dropna().unique())
        # Intersection should be non-empty; "Sonstige" passes through and is OK.
        assert actual & expected_zenodo, (
            f"pv usage_type missing Zenodo-compatible values; got {actual}"
        )

    def test_bess_storage_technology_values(self, bess_df: pd.DataFrame) -> None:
        actual = set(bess_df["storage_technology"].dropna().unique())
        # Open-mastr emits 5 values; only Batterie/Pumpspeicher are exercised
        # downstream (is_battery / is_psh flags).
        assert {"Batterie", "Pumpspeicher"} <= actual

    def test_pv_tilt_categories(self, pv_df: pd.DataFrame) -> None:
        # Tilt is tuple|int|str|None; just verify we got *some* mapped values.
        non_null = pv_df["tilt"].dropna()
        assert len(non_null) > 0
        assert (non_null == (21, 40)).any(), "expected canonical tilt tuple in PV data"


class TestMarketActorsJoin:
    def test_wind_owner_name_join_has_some_hits(self, wind_df: pd.DataFrame) -> None:
        # Commercial wind operators should overwhelmingly resolve to a company
        # name (Personenart=Organisation in MaStR). Allow ≥40% non-null as a
        # conservative bar.
        non_null = wind_df["owner_name"].notna().sum()
        assert non_null / len(wind_df) >= 0.40, (
            f"wind owner_name JOIN coverage {non_null}/{len(wind_df)} "
            f"({100*non_null/len(wind_df):.1f}%); market_actors likely incomplete"
        )

    def test_strict_mode_raises_when_market_empty(self, monkeypatch) -> None:
        # Mock _ensure_market_actors's underlying SQL count to 0 and verify
        # load_for_pipeline raises with the documented diagnostic.
        def fake_read_sql(sql: str, con):  # noqa: ARG001
            return pd.DataFrame({"n": [0]})

        monkeypatch.setattr(mastr_db.pd, "read_sql", fake_read_sql)
        with pytest.raises(RuntimeError, match="market_actors table is empty"):
            mastr_db.load_for_pipeline("wind")
