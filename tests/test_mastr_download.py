"""Unit tests for the parquet writer and the partial download behind
scripts/mastr_download.py.

Synthetic tables only: no network, no MaStR export and no parquet store needed.
The store-level tests in test_parquet_store.py cover the real data.
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
import sqlalchemy as sa
from open_mastr.utils.sqlalchemy_tables import CatalogInteger, CatalogString

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import mastr_parquet  # noqa: E402


def _load_downloader():
    """Import scripts/mastr_download.py fresh, so module state starts empty."""
    spec = importlib.util.spec_from_file_location(
        "mastr_download", REPO / "scripts" / "mastr_download.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _col(name: str, type_, **kwargs) -> sa.Column:
    """A column as open-mastr generates it from the XSD, info included."""
    return sa.Column(name, type_, info={"original_name": name}, **kwargs)


def _wind_table() -> sa.Table:
    return sa.Table(
        "EinheitenWind",
        sa.MetaData(),
        _col("EinheitMastrNummer", sa.String, primary_key=True),
        _col("Bundesland", CatalogString),
        _col("Technologie", CatalogInteger),
        _col("Bruttoleistung", sa.Float),
        _col("Rotorblattenteisungssystem", sa.Boolean),
        _col("Inbetriebnahmedatum", sa.Date),
        _col("DatumLetzteAktualisierung", sa.DateTime(timezone=True)),
        # Declared by the XSD but absent from every XML file.
        _col("Nachtkennzeichnung", sa.Boolean),
    )


def _surrogate_keyed_table() -> sa.Table:
    """A table BNetzA gives no key, so open-mastr prepends an OpenMastrId."""
    return sa.Table(
        "EinheitenAenderungNetzbetreiberzuordnungen",
        sa.MetaData(),
        sa.Column("OpenMastrId", sa.Integer, primary_key=True, autoincrement=True),
        _col("EinheitMastrNummer", sa.String),
        _col("Netzbetreiberzuordnung", sa.String),
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every store path at a temporary directory."""
    monkeypatch.setattr(mastr_parquet, "REPO_DIR", tmp_path)
    monkeypatch.setattr(mastr_parquet, "PARQUET_DIR", tmp_path)
    return tmp_path


class TestColumnTypes:
    def test_xsd_types_map_to_store_types(self) -> None:
        types = mastr_parquet.column_types(_wind_table())
        assert {c: duck for c, (duck, _) in types.items()} == {
            "EinheitMastrNummer": "VARCHAR",
            "Bundesland": "VARCHAR",
            "Technologie": "BIGINT",
            "Bruttoleistung": "DOUBLE",
            "Rotorblattenteisungssystem": "BOOLEAN",
            "Inbetriebnahmedatum": "DATE",
            "DatumLetzteAktualisierung": "TIMESTAMP",
            "Nachtkennzeichnung": "BOOLEAN",
        }

    def test_reflected_sqlite_types_map_like_declared_ones(self, tmp_path: Path) -> None:
        """convert_from_sqlite reflects VARCHAR/DATE/BOOLEAN, not the XSD types."""
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
        _wind_table().create(engine)
        reflected = sa.Table("EinheitenWind", sa.MetaData(), autoload_with=engine)
        assert mastr_parquet.column_types(reflected) == mastr_parquet.column_types(
            _wind_table()
        )

    def test_primary_key(self) -> None:
        assert mastr_parquet.primary_key(_wind_table()) == ["EinheitMastrNummer"]

    def test_surrogate_key_is_no_primary_key(self) -> None:
        assert mastr_parquet.primary_key(_surrogate_keyed_table()) == []


class TestWriter:
    def _part(self, **rows: list) -> pd.DataFrame:
        return pd.DataFrame(rows)

    def test_declared_columns_are_materialised_and_extras_kept(self, store: Path) -> None:
        """A declared column the XML never carries still exists (as SQLite's
        CREATE TABLE gave it), and an XML column the XSD lacks survives."""
        dl = _load_downloader()
        df = self._part(
            EinheitMastrNummer=["SEE1"], Bruttoleistung=["3000.5"],
            Rotorblattenteisungssystem=["true"], NeuesFeld=["x"],
        )
        out = dl._coerce_to_table_dtypes(df, _wind_table())
        assert list(out.columns) == [c.name for c in _wind_table().columns] + ["NeuesFeld"]
        assert out["Nachtkennzeichnung"].isna().all()
        assert out["Rotorblattenteisungssystem"].iloc[0]
        assert out["Bruttoleistung"].iloc[0] == 3000.5

    def test_finalise_keeps_first_duplicate_and_restores_types(self, store: Path) -> None:
        dl = _load_downloader()
        table = _wind_table()
        dl._reset_table_parts(table, engine=None)
        dl._add_table_to_parquet(
            self._part(EinheitMastrNummer=["SEE1", "SEE2"], Bruttoleistung=[1.0, 2.0],
                       Inbetriebnahmedatum=["2020-01-31", "0100-01-01"]),
            table, engine=None,
        )
        # Second XML file repeats SEE1 with another value and adds a column.
        dl._add_table_to_parquet(
            self._part(EinheitMastrNummer=["SEE1", "SEE3"], Bruttoleistung=[9.0, 3.0],
                       NeuesFeld=["a", "b"]),
            table, engine=None,
        )
        assert mastr_parquet.finalise_table(table) == 3

        con = duckdb.connect()
        path = mastr_parquet.table_path(table.name)
        rows = con.execute(
            f"SELECT EinheitMastrNummer, Bruttoleistung, NeuesFeld FROM '{path}' ORDER BY 1"
        ).fetchall()
        assert rows == [("SEE1", 1.0, None), ("SEE2", 2.0, None), ("SEE3", 3.0, "b")]
        types = dict(con.execute(f"SELECT column_name, column_type FROM (DESCRIBE FROM '{path}')").fetchall())
        assert types["Inbetriebnahmedatum"] == "DATE"
        assert types["Nachtkennzeichnung"] == "BOOLEAN"
        early = con.execute(
            f"SELECT Inbetriebnahmedatum FROM '{path}' WHERE EinheitMastrNummer = 'SEE2'"
        ).fetchone()[0]
        assert early.year == 100
        assert not mastr_parquet.parts_dir(table.name).exists()

    def test_surrogate_keyed_table_keeps_every_row(self, store: Path) -> None:
        """Regression: partitioning by the all-NULL OpenMastrId kept 1 of
        212448 EinheitenAenderungNetzbetreiberzuordnungen rows."""
        dl = _load_downloader()
        table = _surrogate_keyed_table()
        dl._reset_table_parts(table, engine=None)
        for unit in (["SEE1", "SEE2"], ["SEE1", "SEE3"]):
            dl._add_table_to_parquet(
                self._part(EinheitMastrNummer=unit, Netzbetreiberzuordnung=["a", "b"]),
                table, engine=None,
            )
        assert mastr_parquet.finalise_table(table) == 4
        path = mastr_parquet.table_path(table.name)
        cols = [r[0] for r in duckdb.connect().execute(f"DESCRIBE FROM '{path}'").fetchall()]
        assert "OpenMastrId" not in cols

    def test_reset_drops_stale_parts(self, store: Path) -> None:
        dl = _load_downloader()
        table = _wind_table()
        dl._reset_table_parts(table, engine=None)
        dl._add_table_to_parquet(self._part(EinheitMastrNummer=["OLD"]), table, engine=None)
        dl._reset_table_parts(table, engine=None)
        assert not list(mastr_parquet.parts_dir(table.name).glob("*.parquet"))


class TestConvertFromSqlite:
    def test_skips_compatibility_views(self, store: Path, tmp_path: Path) -> None:
        """open-mastr 1.0 creates views under the pre-1.0 names; converting them
        too would export every table twice."""
        db = tmp_path / "open-mastr.db"
        engine = sa.create_engine(f"sqlite:///{db}")
        _wind_table().create(engine)
        with engine.begin() as con:
            con.exec_driver_sql(
                "INSERT INTO EinheitenWind (EinheitMastrNummer, Inbetriebnahmedatum, "
                "Rotorblattenteisungssystem) VALUES ('SEE1', '2020-01-31', 1)"
            )
            con.exec_driver_sql("CREATE VIEW wind_extended AS SELECT * FROM EinheitenWind")

        written = mastr_parquet.convert_from_sqlite(db_path=db, parquet_dir=store)

        assert written == {"EinheitenWind": 1}
        types = dict(duckdb.connect().execute(
            "SELECT column_name, column_type FROM "
            f"(DESCRIBE FROM '{mastr_parquet.table_path('EinheitenWind', store)}')"
        ).fetchall())
        assert types["Inbetriebnahmedatum"] == "DATE"
        assert types["Rotorblattenteisungssystem"] == "BOOLEAN"


class TestWriterFailures:
    def test_failed_part_write_fails_the_run(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """open-mastr's process_xml_file swallows every exception and only logs
        it, so the run has to fail on the logged errors and leave the store alone."""
        dl = _load_downloader()
        table = _wind_table()

        class FakeMastr:
            def __init__(self, engine):
                assert not Path(engine.url.database).exists(), "writer must not open a DB"

            def generate_data_model(self, data, date):
                return {"EinheitenWind": table}

            def download(self, **kwargs):
                # What write_mastr_xml_to_database and process_xml_file do.
                dl._uw.delete_all_existing_rows(table, None)
                try:
                    dl._uw.add_table_to_sqlite_database(
                        pd.DataFrame({"EinheitMastrNummer": ["SEE1"]}), table, None
                    )
                except Exception as e:
                    dl._uw.log.error(f"Error processing file 'EinheitenWind_1.xml': '{e}'")

        def broken_write_part(*args, **kwargs):
            raise OSError("disk full")

        # main() patches open-mastr's module; registering the originals here
        # makes monkeypatch restore them for the rest of the session.
        for name in ("delete_all_existing_rows",
                     "check_for_column_mismatch_and_try_to_solve_it",
                     "add_table_to_sqlite_database"):
            monkeypatch.setattr(dl._uw, name, getattr(dl._uw, name))
        monkeypatch.setattr(dl, "Mastr", FakeMastr)
        monkeypatch.setattr(mastr_parquet, "write_part", broken_write_part)
        monkeypatch.setattr(sys, "argv", ["mastr_download.py", "wind"])
        for var in ("NUMBER_OF_PROCESSES", "USE_RECOMMENDED_NUMBER_OF_PROCESSES"):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SystemExit, match=r"(?s)1 error.*EinheitenWind_1.xml.*disk full"):
            dl.main()
        assert not mastr_parquet.table_path("EinheitenWind").exists()


class _FakeRemoteZip:
    """Stands in for unzip_http.RemoteZipFile: a central directory of members,
    and an extractzip that appends each member to a local zip as upstream's
    does. ``short`` members arrive cut off, as after a dropped connection."""

    def __init__(self, members: dict[str, bytes], short: set[str] = frozenset()) -> None:
        self.members = members
        self.short = short
        self.files = {
            name: SimpleNamespace(file_size=len(data), compress_size=len(data))
            for name, data in members.items()
        }

    def namelist(self) -> list[str]:
        return list(self.members)

    def extractzip(self, member: str, path: Path) -> None:
        data = self.members[member]
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr(member, data[: len(data) // 2] if member in self.short else data)


_REMOTE_MEMBERS = {
    "EinheitenWind_1.xml": b"<wind/>" * 100,
    "Katalogwerte.xml": b"<katalog/>" * 100,
}


class TestPartialDownload:
    """The byte-bar copy of open-mastr 1.1.0's partial_download_with_unzip_http
    must keep upstream's atomic append (PR #843)."""

    def _download(
        self, monkeypatch: pytest.MonkeyPatch, save_path: Path, remote: _FakeRemoteZip
    ) -> None:
        dl = _load_downloader()
        monkeypatch.setattr(dl._bulk.unzip_http, "RemoteZipFile", lambda url: remote)
        dl._partial_download_with_progress(
            str(save_path), "https://example.invalid/x.zip", {"einheitenwind", "katalogwerte"}
        )

    def _cached_zip(self, tmp_path: Path) -> tuple[Path, bytes]:
        """A zip an earlier partial download left behind."""
        save_path = tmp_path / "Gesamtdatenexport.zip"
        with zipfile.ZipFile(save_path, "w") as zf:
            zf.writestr("EinheitenSolar_1.xml", b"<solar/>")
        return save_path, save_path.read_bytes()

    def test_members_are_appended_to_the_cached_zip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_path, _ = self._cached_zip(tmp_path)
        self._download(monkeypatch, save_path, _FakeRemoteZip(_REMOTE_MEMBERS))

        with zipfile.ZipFile(save_path) as zf:
            assert sorted(zf.namelist()) == sorted(["EinheitenSolar_1.xml", *_REMOTE_MEMBERS])
            assert zf.read("Katalogwerte.xml") == _REMOTE_MEMBERS["Katalogwerte.xml"]
        assert not Path(f"{save_path}.part").exists()

    def test_truncated_member_raises_and_keeps_the_cached_zip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A truncated member still closes into a valid zip, so only the size
        check stops it from being parsed as complete."""
        save_path, before = self._cached_zip(tmp_path)
        remote = _FakeRemoteZip(_REMOTE_MEMBERS, short={"EinheitenWind_1.xml"})

        with pytest.raises(OSError, match="Incomplete download of.*EinheitenWind_1.xml"):
            self._download(monkeypatch, save_path, remote)
        assert save_path.read_bytes() == before
        assert not Path(f"{save_path}.part").exists()
