"""A2 — the pure 09:20 snapshot compute + the server-side capture hook (_update_hover)."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from analytics.hover_snapshot import build_hover_snapshot
from services.live_collector import _update_hover, get_or_create_hover_state


def _rows(call: dict, put: dict):
    rows = []
    for strike, (oi, iv) in call.items():
        rows.append({"option_type": "CE", "strike": float(strike), "oi_lots": float(oi), "iv": float(iv)})
    for strike, (oi, iv) in put.items():
        rows.append({"option_type": "PE", "strike": float(strike), "oi_lots": float(oi), "iv": float(iv)})
    return rows


# --- build_hover_snapshot (pure) --------------------------------------------------------------- #


def test_build_hover_snapshot_per_strike_values():
    snap = build_hover_snapshot(_rows({22600: (5000, 12.0)}, {22600: (3000, 13.0)}), 22500.0)
    entry = snap["strikes"]["22600"]
    assert entry["call_oi"] == 5000.0 and entry["put_oi"] == 3000.0
    assert entry["call_iv"] == 12.0 and entry["put_iv"] == 13.0
    assert entry["pcr"] == 3000.0 / 5000.0
    assert entry["straddle_oi"] == 8000.0


def test_build_hover_snapshot_pcr_none_without_call_oi():
    strikes = build_hover_snapshot(_rows({}, {22400: (3000, 13.0)}), 22500.0)["strikes"]
    assert strikes["22400"]["pcr"] is None
    assert strikes["22400"]["call_oi"] is None
    assert strikes["22400"]["straddle_oi"] == 3000.0


def test_build_hover_snapshot_band_limits_strikes():
    call = {22000 + 50 * i: (100 * i + 1, 10.0) for i in range(40)}  # 22000..23950
    snap = build_hover_snapshot(_rows(call, {}), 22500.0, band=2)
    assert set(snap["strikes"]) == {"22400", "22450", "22500", "22550"}  # 2 below + 2 at/above spot


def test_build_hover_snapshot_empty_without_spot_or_rows():
    assert build_hover_snapshot(_rows({22600: (5000, 12)}, {}), None) == {}
    assert build_hover_snapshot([], 22500.0) == {}


def test_build_hover_snapshot_totals_block():
    # Full-chain per-side OI totals, OI-PCR, and bracketed-ATM IV per side (spot 22500 brackets 22400/22600).
    rows = _rows({22400: (2000, 11.0), 22600: (5000, 12.0)}, {22400: (3000, 13.0), 22600: (1000, 14.0)})
    totals = build_hover_snapshot(rows, 22500.0)["totals"]
    assert totals["call_oi"] == 7000.0 and totals["put_oi"] == 4000.0  # per-side full-chain sums
    assert totals["pcr"] == 4000.0 / 7000.0
    assert totals["call_atm_iv"] == (11.0 + 12.0) / 2  # avg of the two bracketing call strikes
    assert totals["put_atm_iv"] == (13.0 + 14.0) / 2


def test_build_hover_snapshot_totals_pcr_none_without_call_oi():
    totals = build_hover_snapshot(_rows({}, {22400: (3000, 13.0)}), 22500.0)["totals"]
    assert totals["call_oi"] == 0.0 and totals["put_oi"] == 3000.0
    assert totals["pcr"] is None  # no call OI


# --- _update_hover (collector hook) — window [09:20:00, 09:21:00) ------------------------------- #

VIEW_KEY = "13|IDX_I|2026-07-30"
BEFORE = datetime(2026, 7, 20, 9, 10)         # before the 09:20 window
IN_WINDOW = datetime(2026, 7, 20, 9, 20, 30)  # inside [09:20:00, 09:21:00)
AFTER = datetime(2026, 7, 20, 9, 25)          # after the window
ROWS = _rows({22600: (5000, 12.0)}, {22400: (3000, 13.0)})


def _services(tmp_path):
    return SimpleNamespace(hover_json_path=tmp_path / "hover.json")


def _result(view_key, spot, rows):
    request = SimpleNamespace(pair_key=SimpleNamespace(value=view_key))
    return SimpleNamespace(pair_results=[SimpleNamespace(report={"rows": rows, "underlying_price": spot}, request=request)])


def test_update_hover_captures_inside_window(tmp_path):
    # First (and only) fetch lands in [09:20, 09:21) → captured; no pre-09:20 sighting needed.
    services = _services(tmp_path)
    _update_hover(services, _result(VIEW_KEY, 22500.0, ROWS), IN_WINDOW)
    state = get_or_create_hover_state(services)
    assert "22600" in state.pairs[VIEW_KEY]["strikes"] and "22400" in state.pairs[VIEW_KEY]["strikes"]
    data = json.loads((tmp_path / "hover.json").read_text())
    assert data["date"] == "2026-07-20" and VIEW_KEY in data["pairs"]


def test_update_hover_no_capture_before_the_window(tmp_path):
    services = _services(tmp_path)
    _update_hover(services, _result(VIEW_KEY, 22500.0, ROWS), BEFORE)  # 09:10 → outside the window
    assert get_or_create_hover_state(services).pairs == {}
    assert not (tmp_path / "hover.json").exists()


def test_update_hover_no_capture_after_the_window(tmp_path):
    services = _services(tmp_path)
    _update_hover(services, _result(VIEW_KEY, 22500.0, ROWS), AFTER)  # 09:25 → outside the window → nothing
    assert get_or_create_hover_state(services).pairs == {}
    assert not (tmp_path / "hover.json").exists()


def test_update_hover_no_wave_in_window_no_capture(tmp_path):
    # A pair present only outside the window (before + after) never gets a snapshot.
    services = _services(tmp_path)
    _update_hover(services, _result(VIEW_KEY, 22500.0, ROWS), BEFORE)
    _update_hover(services, _result(VIEW_KEY, 22500.0, ROWS), AFTER)
    assert get_or_create_hover_state(services).pairs == {}


def test_update_hover_captures_once_then_frozen(tmp_path):
    services = _services(tmp_path)
    _update_hover(services, _result(VIEW_KEY, 22500.0, ROWS), IN_WINDOW)
    state = get_or_create_hover_state(services)
    assert state.pairs[VIEW_KEY]["strikes"]["22600"]["call_oi"] == 5000.0
    # a later in-window wave with different OI must NOT overwrite the frozen 09:20 snapshot
    changed = _rows({22600: (9999, 12.0)}, {22400: (3000, 13.0)})
    _update_hover(services, _result(VIEW_KEY, 22500.0, changed), datetime(2026, 7, 20, 9, 20, 50))
    assert state.pairs[VIEW_KEY]["strikes"]["22600"]["call_oi"] == 5000.0
    # and a later out-of-window wave likewise leaves it frozen
    _update_hover(services, _result(VIEW_KEY, 22500.0, changed), AFTER)
    assert state.pairs[VIEW_KEY]["strikes"]["22600"]["call_oi"] == 5000.0


def test_update_hover_reloads_todays_file_on_first_wave(tmp_path):
    services = _services(tmp_path)
    (tmp_path / "hover.json").write_text(
        json.dumps({"date": "2026-07-20", "pairs": {"prior|pair": {"22600": {"call_oi": 1}}}})
    )
    _update_hover(services, _result(VIEW_KEY, 22500.0, ROWS), AFTER)  # first wave, outside the window
    state = get_or_create_hover_state(services)
    assert "prior|pair" in state.pairs   # reloaded from today's file (mid-day restart)
    assert VIEW_KEY not in state.pairs    # this wave is outside the 09:20 window → not captured


def test_update_hover_wipes_on_new_day(tmp_path):
    services = _services(tmp_path)
    state = get_or_create_hover_state(services)
    state.loaded = True
    state.date = "2026-07-19"
    state.pairs = {"stale|pair": {"22600": {}}}
    _update_hover(services, _result(VIEW_KEY, 22500.0, ROWS), IN_WINDOW)  # IN_WINDOW is 2026-07-20 → new day
    assert "stale|pair" not in state.pairs
    assert state.date == "2026-07-20"
    assert VIEW_KEY in state.pairs         # today's in-window wave captured


def test_update_hover_guards_errors(tmp_path):
    services = _services(tmp_path)
    bad = SimpleNamespace(pair_results=[SimpleNamespace(report={"rows": [], "underlying_price": 1}, request=None)])
    _update_hover(services, bad, IN_WINDOW)  # request=None → raises inside → guarded, must not propagate
    assert get_or_create_hover_state(services).pairs == {}
