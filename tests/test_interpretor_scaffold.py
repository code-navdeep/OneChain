"""Phase 0 scaffolding tests — the interpretor config loader + the durable JSON adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.json_state_cache import read_json_state, write_json_state
from config import _load_interpretor_config


_VALID = {
    "interpretor": {
        "enabled": True,
        "shift_confirm_seconds": 60,
        "bias_suppress_seconds": 1800,
        "hit_tolerance_pct": 0.1,
        "confidence": {
            "primary": {"start": 100, "n": 4},
            "closer_second": {"start": 50, "n": 5},
            "further_second": {"start": 100, "n": 4},
        },
    }
}


def _write(tmp_path: Path, payload) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- _load_interpretor_config (tolerant `enabled` gate; tunables fail-loud only when enabled) -- #


def test_load_interpretor_config_happy_path(tmp_path):
    cfg = _load_interpretor_config(_write(tmp_path, _VALID))
    assert cfg["enabled"] is True
    assert cfg["shift_confirm_seconds"] == 60.0
    assert cfg["bias_suppress_seconds"] == 1800.0
    assert cfg["hit_tolerance_pct"] == 0.1
    assert cfg["confidence"]["closer_second"] == {"start": 50.0, "n": 5}
    # Leaves are coerced: starts to float, N to int.
    assert isinstance(cfg["confidence"]["primary"]["start"], float)
    assert isinstance(cfg["confidence"]["primary"]["n"], int)


def test_load_interpretor_config_missing_block_disabled(tmp_path):
    # A missing `interpretor` block is tolerant → disabled (not fail-loud).
    assert _load_interpretor_config(_write(tmp_path, {"rate_alert_floors": {"oi": 1000.0}})) == {"enabled": False}


def test_load_interpretor_config_empty_block_disabled(tmp_path):
    assert _load_interpretor_config(_write(tmp_path, {"interpretor": {}})) == {"enabled": False}


def test_load_interpretor_config_enabled_false_disabled(tmp_path):
    # Explicit off → disabled; tunables need not be present or valid when disabled.
    assert _load_interpretor_config(_write(tmp_path, {"interpretor": {"enabled": False}})) == {"enabled": False}


def test_load_interpretor_config_missing_enabled_key_disabled_despite_bad_tunables(tmp_path):
    # No `enabled` key → disabled BEFORE any tunable validation, so malformed tunables never raise.
    payload = {"interpretor": {"shift_confirm_seconds": "nonsense", "confidence": "bad"}}
    assert _load_interpretor_config(_write(tmp_path, payload)) == {"enabled": False}


def test_load_interpretor_config_missing_key_raises(tmp_path):
    payload = json.loads(json.dumps(_VALID))
    del payload["interpretor"]["hit_tolerance_pct"]
    with pytest.raises(RuntimeError):
        _load_interpretor_config(_write(tmp_path, payload))


def test_load_interpretor_config_missing_confidence_position_raises(tmp_path):
    payload = json.loads(json.dumps(_VALID))
    del payload["interpretor"]["confidence"]["further_second"]
    with pytest.raises(RuntimeError):
        _load_interpretor_config(_write(tmp_path, payload))


def test_load_interpretor_config_confidence_not_object_raises(tmp_path):
    payload = json.loads(json.dumps(_VALID))
    payload["interpretor"]["confidence"] = "nope"
    with pytest.raises(RuntimeError):
        _load_interpretor_config(_write(tmp_path, payload))


def test_load_interpretor_config_missing_file_raises(tmp_path):
    with pytest.raises(RuntimeError):
        _load_interpretor_config(tmp_path / "does_not_exist.json")


def test_load_interpretor_config_bad_json_raises(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        _load_interpretor_config(path)


# --- adapters/json_state_cache (durable {date, …} JSON) --------------------------------------- #


def test_json_state_round_trip(tmp_path):
    path = tmp_path / "interpretor.json"
    state = {"date": "2026-07-19", "pairs": {"13|IDX_I|2026-07-30": {"call": {}, "put": {}}}}
    write_json_state(path, state)
    assert read_json_state(path) == state


def test_json_state_missing_returns_empty(tmp_path):
    assert read_json_state(tmp_path / "nope.json") == {}


def test_json_state_corrupt_returns_empty(tmp_path):
    path = tmp_path / "interpretor.json"
    path.write_text("{broken", encoding="utf-8")
    assert read_json_state(path) == {}


def test_json_state_non_dict_returns_empty(tmp_path):
    path = tmp_path / "interpretor.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_json_state(path) == {}
