"""Parquet store tests: native types, geo filtering, and SQLite equivalence.

The store is meant to be a drop-in replacement for the open-mastr SQLite DB
(11.32 GB -> 1.30 GB, ~30x faster pipeline loads), so the load-bearing test is
that `mastr_parquet.load_for_pipeline` returns the same frame as
`mastr_db.load_for_pipeline`. That runs only when both stores are present;
the rest of the module needs the parquet store alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# Allow `import mastr_parquet` when pytest is invoked from any CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mastr_db  # noqa: E402
import mastr_parquet  # noqa: E402

_REQUIRED = {"wind_extended", "solar_extended", "storage_extended", "market_actors"}
_STORE_TABLES = set(mastr_parquet.available_tables())

pytestmark = pytest.mark.skipif(
    not _REQUIRED <= _STORE_TABLES,
    reason=(
        "parquet store not populated. Run `pixi run db-mastr-core` (downloads "
        "straight to parquet) or `pixi run db-mastr-parquet` (converts an "
        "existing open-mastr.db) to enable."
    ),
)

_SQLITE_OK = mastr_db.DB_PATH.exists()
needs_sqlite = pytest.mark.skipif(
    not _SQLITE_OK, reason="open-mastr.db absent; cross-backend comparison skipped"
)


@pytest.fixture(scope="module")
def con():
    c = mastr_parquet.connect()
    yield c
    c.close()


class TestNativeTypes:
    """SQLite has no DATE or BOOLEAN; parquet does, and both population paths
    restore them from the ORM so the schema cannot depend on how it was built."""

    def test_date_column_is_date(self, con) -> None:
        t = con.execute(
            "SELECT column_type FROM (DESCRIBE SELECT Inbetriebnahmedatum "
            "FROM wind_extended)"
        ).fetchone()[0]
        assert t == "DATE", f"expected DATE, got {t}"

    def test_boolean_column_is_boolean(self, con) -> None:
        t = con.execute(
            "SELECT column_type FROM (DESCRIBE SELECT Rotorblattenteisungssystem "
            "FROM wind_extended)"
        ).fetchone()[0]
        assert t == "BOOLEAN", f"expected BOOLEAN, got {t}"

    def test_orm_columns_are_materialised(self, con) -> None:
        """Columns the XML never populates still exist, as SQLite's CREATE TABLE
        gave them for free. Dropping them would break `SELECT Meldedatum ...`."""
        cols = {r[0] for r in con.execute("DESCRIBE SELECT * FROM wind_extended").fetchall()}
        assert {"Meldedatum", "Lage"} <= cols


class TestGeo:
    def test_load_geo_drops_anonymised(self) -> None:
        gdf = mastr_parquet.load_geo(
            "wind", columns=["EinheitMastrNummer", "Laengengrad", "Breitengrad"]
        )
        assert gdf.crs.to_string() == "EPSG:4326"
        assert gdf["Laengengrad"].notna().all()
        assert not gdf.geometry.is_empty.any()

    def test_load_geo_keeps_anonymised_when_asked(self) -> None:
        cols = ["EinheitMastrNummer", "Laengengrad", "Breitengrad"]
        kept = mastr_parquet.load_geo("wind", columns=cols, drop_anonymised=False)
        dropped = mastr_parquet.load_geo("wind", columns=cols)
        assert len(kept) >= len(dropped)


class TestPipelineContract:
    @pytest.mark.parametrize("tech", ["wind", "bess"])
    def test_returns_expected_columns(self, tech: str) -> None:
        df = mastr_parquet.load_for_pipeline(tech)
        assert {"mastr_id", "gross_capacity_kw", "commissioning_date",
                "owner_name"} <= set(df.columns)

    def test_owner_name_is_populated(self) -> None:
        """Guards the market_actors JOIN: a wrong key silently yields all-NULL."""
        df = mastr_parquet.load_for_pipeline("wind")
        assert df["owner_name"].notna().any()

    def test_unknown_tech_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown tech"):
            mastr_parquet.load_for_pipeline("coal")


@needs_sqlite
class TestSqliteEquivalence:
    """PV is excluded: it is the same code path as wind/bess but costs ~107 s on
    the SQLite side, which is not worth it per run."""

    @pytest.mark.parametrize("tech", ["wind", "bess"])
    def test_same_shape_and_columns(self, tech: str) -> None:
        p = mastr_parquet.load_for_pipeline(tech)
        s = mastr_db.load_for_pipeline(tech)
        assert p.shape == s.shape
        assert list(p.columns) == list(s.columns)

    @pytest.mark.parametrize("tech", ["wind", "bess"])
    def test_same_ids_and_capacity(self, tech: str) -> None:
        p = mastr_parquet.load_for_pipeline(tech)
        s = mastr_db.load_for_pipeline(tech)
        assert set(p["mastr_id"]) == set(s["mastr_id"])
        assert p["gross_capacity_kw"].sum() == pytest.approx(
            s["gross_capacity_kw"].sum(), rel=1e-9
        )

    @pytest.mark.parametrize("tech", ["wind", "bess"])
    def test_same_non_null_counts_per_column(self, tech: str) -> None:
        p = mastr_parquet.load_for_pipeline(tech).sort_values("mastr_id")
        s = mastr_db.load_for_pipeline(tech).sort_values("mastr_id")
        diff = {c: (int(p[c].notna().sum()), int(s[c].notna().sum()))
                for c in p.columns
                if int(p[c].notna().sum()) != int(s[c].notna().sum())}
        assert not diff, f"non-null counts differ (parquet, sqlite): {diff}"

    def test_commissioning_dates_match(self) -> None:
        p = mastr_parquet.load_for_pipeline("wind").sort_values("mastr_id")
        s = mastr_db.load_for_pipeline("wind").sort_values("mastr_id")
        pa = pd.to_datetime(p["commissioning_date"].reset_index(drop=True))
        sa = pd.to_datetime(s["commissioning_date"].reset_index(drop=True))
        assert pa.equals(sa)


class TestSourceResolution:
    def test_auto_prefers_parquet(self) -> None:
        import mastr_plot
        resolved, _ = mastr_plot._resolve_bulk_source("auto", None)
        assert resolved == "parquet"

    def test_explicit_parquet(self) -> None:
        import mastr_plot
        resolved, path = mastr_plot._resolve_bulk_source("parquet", None)
        assert resolved == "parquet" and path is None

    def test_unknown_source_rejected(self) -> None:
        import mastr_plot
        with pytest.raises(ValueError, match="unknown source"):
            mastr_plot._resolve_bulk_source("csv", None)
