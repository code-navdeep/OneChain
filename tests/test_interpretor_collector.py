"""Phase 2 tests — the collector hook that advances + persists the interpretor state."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from services.live_collector import _update_interpretor, get_or_create_interpretor_state


T = datetime(2026, 7, 20, 10, 0, 0)  # Monday 10:00 IST (≥ 09:15)
VIEW_KEY = "13|IDX_I|2026-07-30"


def _rows(call_oi: dict, put_oi: dict):
    rows = []
    for strike, oi in call_oi.items():
        rows.append({"option_type": "CE", "strike": float(strike), "oi_lots": float(oi), "pain_point": float(strike)})
    for strike, oi in put_oi.items():
        rows.append({"option_type": "PE", "strike": float(strike), "oi_lots": float(oi), "pain_point": float(strike)})
    return rows


def _result(view_key: str = VIEW_KEY, spot: float = 22500):
    report = {"rows": _rows({22600: 5000}, {22400: 5000}), "underlying_price": spot, "strike_size": 50.0}
    request = SimpleNamespace(pair_key=SimpleNamespace(value=view_key))
    return SimpleNamespace(pair_results=[SimpleNamespace(report=report, request=request)])


def _services(tmp_path):
    return SimpleNamespace(interpretor_json_path=tmp_path / "interpretor.json")


def test_update_interpretor_computes_and_writes(tmp_path):
    services = _services(tmp_path)
    _update_interpretor(services, _result(), T)

    state = get_or_create_interpretor_state(services)
    assert state.date == "2026-07-20"
    assert state.pairs[VIEW_KEY]["call"]["primary"] == 22600
    data = json.loads((tmp_path / "interpretor.json").read_text())
    assert data["date"] == "2026-07-20"
    assert VIEW_KEY in data["pairs"]


def test_update_interpretor_persists_across_waves(tmp_path):
    services = _services(tmp_path)
    _update_interpretor(services, _result(), T)
    state = get_or_create_interpretor_state(services)
    assert state.pairs[VIEW_KEY]["baseline_taken"] is True
    # a second wave carries the same pair forward (does not re-baseline)
    _update_interpretor(services, _result(), datetime(2026, 7, 20, 10, 0, 5))
    assert state.pairs[VIEW_KEY]["call"]["primary"] == 22600


def test_update_interpretor_wipes_on_new_day(tmp_path):
    services = _services(tmp_path)
    state = get_or_create_interpretor_state(services)
    state.loaded = True                                  # skip the file-load path
    state.date = "2026-07-19"
    state.pairs = {"stale|pair": {"baseline_taken": True}}
    _update_interpretor(services, _result(), T)          # T is 2026-07-20 → new day
    assert "stale|pair" not in state.pairs
    assert state.date == "2026-07-20"
    assert VIEW_KEY in state.pairs


def test_update_interpretor_reloads_todays_file_on_first_wave(tmp_path):
    services = _services(tmp_path)
    (tmp_path / "interpretor.json").write_text(
        json.dumps({"date": "2026-07-20", "pairs": {"prior|pair": {"baseline_taken": True}}})
    )
    _update_interpretor(services, _result(), T)
    state = get_or_create_interpretor_state(services)
    assert "prior|pair" in state.pairs   # reloaded from today's file (mid-day restart)
    assert VIEW_KEY in state.pairs        # + this wave's pair


def test_update_interpretor_ignores_stale_dated_file(tmp_path):
    services = _services(tmp_path)
    (tmp_path / "interpretor.json").write_text(
        json.dumps({"date": "2026-07-19", "pairs": {"yesterday|pair": {"baseline_taken": True}}})
    )
    _update_interpretor(services, _result(), T)  # today 2026-07-20 → discard yesterday's file
    state = get_or_create_interpretor_state(services)
    assert "yesterday|pair" not in state.pairs
    assert state.date == "2026-07-20"


def test_update_interpretor_guards_errors(tmp_path):
    services = _services(tmp_path)
    bad = SimpleNamespace(pair_results=[SimpleNamespace(report=None, request=None)])
    _update_interpretor(services, bad, T)  # must not raise (guarded, like _update_rate_rings)
    assert VIEW_KEY not in get_or_create_interpretor_state(services).pairs
