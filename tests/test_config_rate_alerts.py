"""Tests for the config.json rate-alert loader (fail-loud, no fallback)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import _load_rate_alert_config


def _write(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_loads_thresholds_and_floors_with_int_window_keys(tmp_path):
    cfg = _write(
        tmp_path / "config.json",
        {
            "rate_alert_thresholds": {
                "oi": {"1": {"upper": 0.025, "lower": 0.025}, "5": {"upper": 0.1, "lower": 0.1}},
                "vol": {"1": {"upper": 0.03}},
            },
            "rate_alert_floors": {"oi": 1000.0, "iv": 5, "vol": 1000.0},
        },
    )

    thresholds, floors = _load_rate_alert_config(path=cfg)

    # Window keys are coerced back to int (JSON forces them to strings).
    assert set(thresholds["oi"].keys()) == {1, 5}
    assert all(isinstance(w, int) for w in thresholds["oi"])
    assert thresholds["oi"][1]["upper"] == 0.025
    assert thresholds["vol"][1] == {"upper": 0.03}
    # Floors stay string-keyed and coerce to float.
    assert floors == {"oi": 1000.0, "iv": 5.0, "vol": 1000.0}
    assert all(isinstance(v, float) for v in floors.values())


def test_missing_file_raises(tmp_path):
    with pytest.raises(RuntimeError):
        _load_rate_alert_config(path=tmp_path / "does_not_exist.json")


def test_corrupt_json_raises(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        _load_rate_alert_config(path=cfg)


def test_missing_top_level_key_raises(tmp_path):
    cfg = _write(tmp_path / "config.json", {"rate_alert_floors": {"oi": 1000.0}})
    with pytest.raises(RuntimeError):
        _load_rate_alert_config(path=cfg)


def test_empty_thresholds_raises(tmp_path):
    cfg = _write(
        tmp_path / "config.json",
        {"rate_alert_thresholds": {}, "rate_alert_floors": {"oi": 1000.0}},
    )
    with pytest.raises(RuntimeError):
        _load_rate_alert_config(path=cfg)


def test_non_coercible_threshold_value_raises(tmp_path):
    cfg = _write(
        tmp_path / "config.json",
        {
            "rate_alert_thresholds": {"oi": {"1": {"upper": "abc"}}},
            "rate_alert_floors": {"oi": 1000.0},
        },
    )
    with pytest.raises(RuntimeError):
        _load_rate_alert_config(path=cfg)


def test_non_coercible_floor_value_raises(tmp_path):
    cfg = _write(
        tmp_path / "config.json",
        {
            "rate_alert_thresholds": {"oi": {"1": {"upper": 0.025}}},
            "rate_alert_floors": {"oi": "not-a-number"},
        },
    )
    with pytest.raises(RuntimeError):
        _load_rate_alert_config(path=cfg)
