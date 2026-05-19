"""Unit tests for the parser module.

Run:    pixi run pytest
        pixi run python -m pytest tests/ -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import parser as mp  # noqa: E402


# ---------------------------------------------------------------------------
# parse_dotnet_date
# ---------------------------------------------------------------------------


class TestParseDotnetDate:
    def test_valid_epoch_zero(self):
        assert mp.parse_dotnet_date("/Date(0)/") == datetime(
            1970, 1, 1, tzinfo=timezone.utc
        )

    def test_known_date(self):
        # 2020-01-01 00:00:00 UTC = 1577836800000 ms
        got = mp.parse_dotnet_date("/Date(1577836800000)/")
        assert got == datetime(2020, 1, 1, tzinfo=timezone.utc)

    def test_subsecond_precision(self):
        got = mp.parse_dotnet_date("/Date(1577836800500)/")
        assert got.year == 2020 and got.microsecond == 500_000

    @pytest.mark.parametrize("bad", [None, "", "2020-01-01", "garbage"])
    def test_invalid_returns_none(self, bad):
        assert mp.parse_dotnet_date(bad) is None

    def test_returns_tz_aware(self):
        got = mp.parse_dotnet_date("/Date(1577836800000)/")
        assert got is not None and got.tzinfo is timezone.utc


# ---------------------------------------------------------------------------
# Enum decoding (PowerPlant.from_json)
# ---------------------------------------------------------------------------


BASE_ENTRY = {
    "Id": 1,
    "Nettonennleistung": 100.0,
    "Leistungsbegrenzung": 802,
    "HauptausrichtungSolarModule": None,
    "HauptneigungswinkelSolarmodule": None,
    "ArtDerSolaranlageId": None,
    "Bruttoleistung": 200.0,
    "AnzahlSolarModule": None,
    "NutzungsbereichGebSA": None,
    "WindAnLandOderSeeId": None,
    "StandortAnonymisiert": "",
    "InbetriebnahmeDatum": "/Date(1577836800000)/",
    "EndgueltigeStilllegungDatum": None,
    "Plz": "10115",
    "AnlagenbetreiberPersonenArt": 0,
    "AnlagenbetreiberName": "Test GmbH",
    "EnergietraegerName": "Solare Strahlungsenergie",
    "Laengengrad": 13.4,
    "Breitengrad": 52.5,
}


def make_entry(**overrides):
    e = dict(BASE_ENTRY)
    e.update(overrides)
    return e


class TestLeistungsbegrenzung:
    @pytest.mark.parametrize(
        "code,expected_factor",
        [(805, 0.5), (804, 0.6), (803, 0.7), (802, 1.0), (1535, 1.0), (9999, 1.0)],
    )
    def test_inverter_factor(self, code, expected_factor):
        pp = mp.PowerPlant.from_json(make_entry(Leistungsbegrenzung=code))
        assert pp.inverter == pytest.approx(100.0 * expected_factor)


class TestFacing:
    @pytest.mark.parametrize(
        "code,expected",
        [
            (695, 0), (696, 45), (697, 90), (698, 135), (699, 180),
            (700, 225), (701, 270), (702, 315),
            (703, "tracked"), (704, "east-west"),
        ],
    )
    def test_known_codes(self, code, expected):
        pp = mp.PowerPlant.from_json(make_entry(HauptausrichtungSolarModule=code))
        assert pp.facing == expected

    def test_unknown_returns_none(self):
        pp = mp.PowerPlant.from_json(make_entry(HauptausrichtungSolarModule=42))
        assert pp.facing is None


class TestTilt:
    @pytest.mark.parametrize(
        "code,expected",
        [
            (810, (0, 19)), (809, (20, 40)), (808, (40, 60)),
            (807, (61, 90)), (806, 90), (811, "tracked"),
        ],
    )
    def test_known_codes(self, code, expected):
        pp = mp.PowerPlant.from_json(make_entry(HauptneigungswinkelSolarmodule=code))
        assert pp.tilt == expected

    def test_unknown_returns_none(self):
        pp = mp.PowerPlant.from_json(make_entry(HauptneigungswinkelSolarmodule=99))
        assert pp.tilt is None


class TestInstallationType:
    @pytest.mark.parametrize(
        "code,expected",
        [
            (853, "building"), (2484, "building_other"), (852, "free"),
            (3002, "water"), (3058, "parking_lot"), (2961, "balkonkraftwerk"),
        ],
    )
    def test_known_codes(self, code, expected):
        pp = mp.PowerPlant.from_json(make_entry(ArtDerSolaranlageId=code))
        assert pp.installation_type == expected


class TestBuildingType:
    @pytest.mark.parametrize(
        "code,expected",
        [
            (713, "household"), (714, "commercial"), (715, "industry"),
            (716, "farming"), (717, "public"), (718, "other"),
        ],
    )
    def test_known_codes(self, code, expected):
        pp = mp.PowerPlant.from_json(make_entry(NutzungsbereichGebSA=code))
        assert pp.building_type == expected


class TestOffshore:
    def test_nordsee(self):
        pp = mp.PowerPlant.from_json(make_entry(
            WindAnLandOderSeeId=889, StandortAnonymisiert="Nordsee bei Borkum",
        ))
        assert pp.off_shore == "Nordsee"

    def test_ostsee(self):
        pp = mp.PowerPlant.from_json(make_entry(
            WindAnLandOderSeeId=889, StandortAnonymisiert="Ostsee bei Rügen",
        ))
        assert pp.off_shore == "Ostsee"

    def test_onshore_returns_none(self):
        pp = mp.PowerPlant.from_json(make_entry(
            WindAnLandOderSeeId=888, StandortAnonymisiert="Niedersachsen",
        ))
        assert pp.off_shore is None


class TestPanelDropoutHeuristic:
    def test_small_panel_ratio_drops_to_none(self):
        # 100 kW for 10 panels = 10 kW/panel — too good, drop count.
        # 100 kW for 1001 panels = 0.1 kW/panel exactly — also drops.
        pp = mp.PowerPlant.from_json(make_entry(
            Bruttoleistung=100.0, AnzahlSolarModule=1001,
        ))
        assert pp.num_panels is None

    def test_normal_panel_ratio_kept(self):
        # 100 kW for 250 panels = 0.4 kW/panel — realistic, kept.
        pp = mp.PowerPlant.from_json(make_entry(
            Bruttoleistung=100.0, AnzahlSolarModule=250,
        ))
        assert pp.num_panels == 250


class TestPrivateFlag:
    def test_private_code_518(self):
        pp = mp.PowerPlant.from_json(make_entry(AnlagenbetreiberPersonenArt=518))
        assert pp.is_private is True

    def test_other_code_is_not_private(self):
        pp = mp.PowerPlant.from_json(make_entry(AnlagenbetreiberPersonenArt=1))
        assert pp.is_private is False


class TestBessSector:
    """mastr_plot.bess_sector — battery-charts.de classification."""

    def setup_method(self):
        import importlib, sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        self.mp_mod = importlib.import_module("mastr_plot")

    def test_hss_bounds(self):
        assert self.mp_mod.bess_sector(5) == "HSS (<30 kWh)"
        assert self.mp_mod.bess_sector(29.99) == "HSS (<30 kWh)"

    def test_css_bounds(self):
        assert self.mp_mod.bess_sector(30) == "CSS (30 kWh – 1 MWh)"
        assert self.mp_mod.bess_sector(999.99) == "CSS (30 kWh – 1 MWh)"

    def test_lss_bounds(self):
        assert self.mp_mod.bess_sector(1000) == "LSS (≥1 MWh)"
        assert self.mp_mod.bess_sector(4_000_000) == "LSS (≥1 MWh)"

    def test_unknown_for_missing_or_zero(self):
        assert self.mp_mod.bess_sector(None) == "unknown"
        assert self.mp_mod.bess_sector(0) == "unknown"
        assert self.mp_mod.bess_sector(-5) == "unknown"


class TestBatteryUnit:
    """BatteryUnit.from_json — Energieträger 2496 only."""

    BESS_BASE = {
        "Id": 42,
        "EinheitName": "Test Battery",
        "EnergietraegerId": 2496,
        "EnergietraegerName": "Speicher",
        "Bruttoleistung": 1000.0,
        "Nettonennleistung": 1000.0,
        "NutzbareSpeicherkapazitaet": 2000.0,
        "InbetriebnahmeDatum": "/Date(1577836800000)/",   # 2020-01-01 UTC
        "GeplantesInbetriebsnahmeDatum": None,
        "EndgueltigeStilllegungDatum": None,
        "Stromspeichertechnologie": 524,
        "StromspeichertechnologieBezeichnung": "Batterie",
        "Batterietechnologie": 727,
        "VollTeilEinspeisungBezeichnung": "Volleinspeisung",
        "BetriebsStatusName": "In Betrieb",
        "SpannungsebenenNamen": "Mittelspannung",
        "Laengengrad": 13.4,
        "Breitengrad": 52.5,
        "Plz": "10115",
        "Gemeinde": "Testgemeinde",
        "Landkreis": "Test-Kreis",
        "Bundesland": "Berlin",
        "AnlagenbetreiberName": "Test Battery GmbH",
        "AnlagenbetreiberPersonenArt": 517,
    }

    def test_skips_non_bess_records(self):
        e = dict(self.BESS_BASE)
        e["EnergietraegerId"] = 2497  # Wind
        assert mp.BatteryUnit.from_json(e) is None

    def test_round_trip(self):
        u = mp.BatteryUnit.from_json(dict(self.BESS_BASE))
        assert u is not None
        assert u.id == 42
        assert u.power_kw == 1000.0
        assert u.energy_kwh == 2000.0
        assert u.storage_tech == "Batterie"
        assert u.battery_tech_code == 727
        assert u.feed_in_mode == "Volleinspeisung"
        assert u.status == "In Betrieb"
        assert u.voltage_level == "Mittelspannung"
        assert u.landkreis == "Test-Kreis"
        assert u.bundesland == "Berlin"
        assert u.is_private is False

    def test_planned_only_record(self):
        e = dict(self.BESS_BASE)
        e["InbetriebnahmeDatum"] = None
        e["GeplantesInbetriebsnahmeDatum"] = "/Date(1820000000000)/"
        u = mp.BatteryUnit.from_json(e)
        assert u.install_date is None
        assert u.planned_date is not None

    def test_private_owner_flag(self):
        e = dict(self.BESS_BASE)
        e["AnlagenbetreiberPersonenArt"] = 518
        u = mp.BatteryUnit.from_json(e)
        assert u.is_private is True

    def test_missing_capacity_defaults_to_zero(self):
        e = dict(self.BESS_BASE)
        e["NutzbareSpeicherkapazitaet"] = None
        e["Bruttoleistung"] = None
        u = mp.BatteryUnit.from_json(e)
        assert u.power_kw == 0.0
        assert u.energy_kwh == 0.0


class TestCoreFields:
    def test_round_trip(self):
        pp = mp.PowerPlant.from_json(make_entry())
        assert pp.id == 1
        assert pp.power == 200.0
        assert pp.postal_code == "10115"
        assert pp.energy_type == "Solare Strahlungsenergie"
        assert pp.longitude == 13.4
        assert pp.latitude == 52.5
        assert pp.install_date == datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert pp.removal_date is None
