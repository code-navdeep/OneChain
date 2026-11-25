"""Server-side Tier-1 alert collector — the _AlertState accessor + _update_alerts hook + the
telegram_forward category filter (Phases 3-4)."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from config import _load_telegram_forward_config
from services.live_collector import _AlertState, _update_alerts, get_or_create_alert_state


def test_get_or_create_alert_state_is_create_once():
    services = SimpleNamespace()
    state = get_or_create_alert_state(services)
    assert isinstance(state, _AlertState)
    assert state.journal == {}
    assert state.loaded is False
    assert get_or_create_alert_state(services) is state  # same instance on re-access


# --- _update_alerts hook (Phase 4) ------------------------------------------------------------- #

T = datetime(2026, 7, 20, 10, 0, 0)  # Monday 10:00 IST
T2 = datetime(2026, 7, 20, 10, 0, 5)
KEY_A = "13|IDX_I|2026-07-30"


def _report(*, instrument_key="13|IDX_I", rr=None):
    """A meta-only report carrying just the scalars detect_alerts reads (RR flip is the test lever)."""
    return {
        "instrument_key": instrument_key,
        "underlying_price": None,
        "strike_size": 50.0,
        "skew": {"rr_25": rr},
        "gex_analysis": {"market_regime": None},
        "totals": {"put_call_ratio": None},
        "levels": {},
    }


class _FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)


def _pair(view_key, report, *, display_name="Nifty", expiry="2026-07-30"):
    request = SimpleNamespace(pair_key=SimpleNamespace(value=view_key), display_name=display_name, expiry=expiry)
    return SimpleNamespace(report=report, request=request)


def _result(*pairs):
    return SimpleNamespace(pair_results=list(pairs))


def _services(tmp_path, notifier=None):
    return SimpleNamespace(alerts_cache_path=tmp_path / "alerts_cache.json", telegram_notifier=notifier)


def test_update_alerts_computes_and_writes(tmp_path):
    services = _services(tmp_path)
    _update_alerts(services, _result(_pair(KEY_A, _report(rr=-2.0))), T)          # seed (silent)
    _update_alerts(services, _result(_pair(KEY_A, _report(rr=2.0))), T2)          # flip → an alert
    state = get_or_create_alert_state(services)
    assert state.journal["date"] == "2026-07-20"
    assert len(state.journal["alerts"][KEY_A]) == 1
    assert "flipped" in state.journal["alerts"][KEY_A][0]["body"]
    data = json.loads((tmp_path / "alerts_cache.json").read_text())
    assert data["date"] == "2026-07-20"
    assert KEY_A in data["alerts"]


def test_update_alerts_persists_baseline_on_first_wave(tmp_path):
    services = _services(tmp_path)
    _update_alerts(services, _result(_pair(KEY_A, _report(rr=-2.0))), T)
    data = json.loads((tmp_path / "alerts_cache.json").read_text())
    assert KEY_A in data["baselines"]  # baseline seeded + written even though nothing was alerted


def test_update_alerts_wipes_on_new_day(tmp_path):
    services = _services(tmp_path)
    state = get_or_create_alert_state(services)
    state.loaded = True                                              # skip the file-load path
    state.journal = {"date": "2026-07-19", "baselines": {"stale": {}}, "alerts": {"stale": [{"x": 1}]}}
    _update_alerts(services, _result(_pair(KEY_A, _report(rr=-2.0))), T)   # T is 2026-07-20 → new day
    assert "stale" not in state.journal["baselines"]
    assert state.journal["date"] == "2026-07-20"


def test_update_alerts_reloads_todays_file_on_first_wave(tmp_path):
    services = _services(tmp_path)
    (tmp_path / "alerts_cache.json").write_text(json.dumps({
        "date": "2026-07-20",
        "baselines": {"prior": {"instrument_key": "99|X"}},
        "alerts": {"prior": [{"body": "old"}]},
    }))
    _update_alerts(services, _result(_pair(KEY_A, _report(rr=-2.0))), T)
    state = get_or_create_alert_state(services)
    assert "prior" in state.journal["alerts"]   # reloaded today's file (mid-day restart)
    assert KEY_A in state.journal["baselines"]   # + this wave's pair seeded


def test_update_alerts_ignores_stale_dated_file(tmp_path):
    services = _services(tmp_path)
    (tmp_path / "alerts_cache.json").write_text(json.dumps({
        "date": "2026-07-19",
        "baselines": {"yesterday": {}},
        "alerts": {"yesterday": [{"body": "old"}]},
    }))
    _update_alerts(services, _result(_pair(KEY_A, _report(rr=-2.0))), T)  # today → discard yesterday's file
    state = get_or_create_alert_state(services)
    assert "yesterday" not in state.journal.get("baselines", {})
    assert state.journal["date"] == "2026-07-20"


def test_update_alerts_guards_errors(tmp_path):
    services = _services(tmp_path)
    bad = SimpleNamespace(pair_results=[SimpleNamespace(report=None, request=None)])
    _update_alerts(services, bad, T)  # must not raise (guarded, like _update_rate_rings)
    assert KEY_A not in get_or_create_alert_state(services).journal.get("alerts", {})


def test_update_alerts_forwards_new_alerts(tmp_path, monkeypatch):
    """The filter is pinned here (``rr`` enabled), so the test does not depend on ``config.json``."""
    notifier = _FakeNotifier()
    services = _services(tmp_path, notifier=notifier)
    monkeypatch.setattr("services.live_collector.TELEGRAM_FORWARD_CONFIG", {"tier1": {"rr": True}})
    _update_alerts(services, _result(_pair(KEY_A, _report(rr=-2.0))), T)   # seed → nothing forwarded
    _update_alerts(services, _result(_pair(KEY_A, _report(rr=2.0))), T2)   # flip → forwarded (rr enabled)
    assert len(notifier.sent) == 1
    assert "flipped" in notifier.sent[0]


def test_update_alerts_forward_respects_category_filter(tmp_path, monkeypatch):
    notifier = _FakeNotifier()
    services = _services(tmp_path, notifier=notifier)
    monkeypatch.setattr("services.live_collector.TELEGRAM_FORWARD_CONFIG", {"tier1": {"rr": False}})
    _update_alerts(services, _result(_pair(KEY_A, _report(rr=-2.0))), T)   # seed
    _update_alerts(services, _result(_pair(KEY_A, _report(rr=2.0))), T2)   # flip, but rr disabled
    assert notifier.sent == []  # default-deny for the disabled category


# --- config._load_telegram_forward_config (tolerant, default-deny) ----------------------------- #


def test_load_telegram_forward_config_parses(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"telegram_forward": {"tier1": {"rr": True, "gex": False}, "tier2": {"oi": True}}}))
    assert _load_telegram_forward_config(path) == {"tier1": {"rr": True, "gex": False}, "tier2": {"oi": True}}


def test_load_telegram_forward_config_missing_block_returns_empty(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"other": {}}))
    assert _load_telegram_forward_config(path) == {}


def test_load_telegram_forward_config_missing_file_returns_empty(tmp_path):
    assert _load_telegram_forward_config(tmp_path / "nope.json") == {}


def test_load_telegram_forward_config_malformed_is_tolerated(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not valid json")
    assert _load_telegram_forward_config(path) == {}
