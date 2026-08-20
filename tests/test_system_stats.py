"""Tests for FreshTomato raw system telemetry normalization."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


API_PATH = Path(__file__).parents[1] / "custom_components/freshtomato/api.py"
SPEC = importlib.util.spec_from_file_location("freshtomato_api_test", API_PATH)
assert SPEC and SPEC.loader
api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api
SPEC.loader.exec_module(api)


def test_load_and_memory_are_normalized_without_unit_conversion() -> None:
    stats = api.parse_sysinfo({
        "loads": [65536, 32768, 16384],
        "totalram": 268435456,
        "totalfreeram": 134217728,
    })
    assert (stats.load_1m, stats.load_5m, stats.load_15m) == (1.0, 0.5, 0.25)
    assert stats.memory_total == 268435456
    assert stats.memory_available == stats.memory_used == 134217728
    assert stats.memory_usage_percent == 50.0


def test_existing_js_parser_extracts_sysinfo() -> None:
    client = api.FreshTomatoAPI.__new__(api.FreshTomatoAPI)
    stats = client.parse_system_stats(
        """sysinfo = {
            loads: [65536, 32768, 16384],
            jiffies: '100 100 100 700',
            totalram: 268435456,
            totalfreeram: 134217728
        };"""
    )
    assert stats.load_1m == 1.0
    assert (stats.cpu_total_jiffies, stats.cpu_idle_jiffies) == (1000, 700)


@pytest.mark.parametrize(
    ("sysinfo", "expected_available"),
    [
        ({}, None),
        ({"loads": []}, None),
        ({"loads": ["bad"]}, None),
        ({"totalram": 0, "totalfreeram": 0}, None),
        ({"totalram": 100, "totalfreeram": 101}, None),
        ({"totalram": 100, "freeram": 40}, 40),
    ],
)
def test_missing_and_invalid_values_degrade_gracefully(
    sysinfo: dict, expected_available: int | None
) -> None:
    stats = api.parse_sysinfo(sysinfo)
    assert stats.memory_available == expected_available
    if sysinfo.get("loads") in ([], ["bad"]):
        assert stats.load_1m is None


def test_jiffies_usage_and_first_sample() -> None:
    assert api.calculate_cpu_usage(None, (1000, 700)) is None
    assert api.calculate_cpu_usage((1000, 700), (1100, 750)) == 50.0


def test_jiffies_io_wait_is_treated_as_idle() -> None:
    previous = api.parse_sysinfo({"jiffies": "100 0 0 700 100"})
    current = api.parse_sysinfo({"jiffies": "110 0 0 730 160"})

    assert (previous.cpu_total_jiffies, previous.cpu_idle_jiffies) == (900, 800)
    assert (current.cpu_total_jiffies, current.cpu_idle_jiffies) == (1000, 890)
    assert api.calculate_cpu_usage(
        (previous.cpu_total_jiffies, previous.cpu_idle_jiffies),
        (current.cpu_total_jiffies, current.cpu_idle_jiffies),
    ) == 10.0


def test_jiffies_reset_replaces_baseline_for_next_calculation() -> None:
    assert api.calculate_cpu_usage((1100, 750), (100, 70)) is None
    assert api.calculate_cpu_usage((100, 70), (200, 120)) == 50.0


@pytest.mark.parametrize("raw", ["", "1 2 3", "1 2 nope 4"])
def test_invalid_jiffies_are_ignored(raw: str) -> None:
    stats = api.parse_sysinfo({"jiffies": raw})
    assert stats.cpu_total_jiffies is None
    assert stats.cpu_idle_jiffies is None
