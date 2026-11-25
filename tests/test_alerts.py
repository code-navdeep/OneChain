from __future__ import annotations

from dataclasses import dataclass

from dash import no_update

from adapters.json_state_cache import read_json_state, write_json_state
from analytics.alerts import Alert, apply_live_wave, build_details_feed, detect_alerts
from features.alerts.callbacks import DISPLAY_CAP, _critical_signal, _render_ticker


def _report(
    *,
    instrument_key: str = "13|IDX_I",
    rr=None,
    regime=None,
    coi=None,
    poi=None,
    cvol=None,
    pvol=None,
    s_coi=None,
    s_poi=None,
    s_cvol=None,
    s_pvol=None,
    spot=None,
    pcr=None,
    max_pain=None,
    pin=None,
    resistance_strong=None,
    support_strong=None,
    strike_size=50.0,
) -> dict:
    return {
        "instrument_key": instrument_key,
        "underlying_price": spot,
        "strike_size": strike_size,
        "max_pain_strike": max_pain,
        "skew": {"rr_25": rr},
        "gex_analysis": {"market_regime": regime},
        "totals": {"put_call_ratio": pcr},
        "pinning": {"pin_strike": pin},
        "levels": {
            "max_call_oi_strike": coi,
            "max_put_oi_strike": poi,
            "max_call_volume_strike": cvol,
            "max_put_volume_strike": pvol,
            "second_max_call_oi_strike": s_coi,
            "second_max_put_oi_strike": s_poi,
            "second_max_call_volume_strike": s_cvol,
            "second_max_put_volume_strike": s_pvol,
            "resistance_strong": resistance_strong,
            "support_strong": support_strong,
        },
    }


def _baseline(report: dict) -> dict:
    alerts, snapshot = detect_alerts(None, report, now_hhmmss="12:00:00")
    assert alerts == []
    return snapshot


# --- Typed Alert -------------------------------------------------------------------------------


def test_alert_round_trip():
    a = Alert(time="12:00:00", category="cross", severity="critical", body="Spot 22,500 crossed above Pin 22,400")
    assert a.to_dict() == {
        "time": "12:00:00",
        "category": "cross",
        "severity": "critical",
        "body": "Spot 22,500 crossed above Pin 22,400",
    }


# --- Reset / seed ------------------------------------------------------------------------------


def test_none_baseline_resets_and_emits_nothing():
    alerts, snap = detect_alerts(None, _report(rr=-2.0), now_hhmmss="12:00:00")
    assert alerts == []
    assert snap["instrument_key"] == "13|IDX_I"
    assert snap["rr_committed"] == "Put skew"


def test_instrument_switch_resets_baseline_without_alert():
    prev = _baseline(_report(instrument_key="13|IDX_I", rr=-2.0))
    alerts, snapshot = detect_alerts(prev, _report(instrument_key="25|IDX_I", rr=2.0), now_hhmmss="12:01:00")
    assert alerts == []
    assert snapshot["instrument_key"] == "25|IDX_I"
    assert snapshot["rr_committed"] == "Call skew"


def test_identical_report_emits_nothing():
    report = _report(rr=-2.0, regime="Stabilizing", coi=22500, poi=22000, spot=22500, pin=22400, pcr=1.0)
    prev = _baseline(report)
    alerts, _ = detect_alerts(prev, report, now_hhmmss="12:01:00")
    assert alerts == []


# --- RR flip (warning, ±0.2 dead-band) ---------------------------------------------------------


def test_rr_flip_emits():
    prev = _baseline(_report(rr=-2.0))  # Put skew
    alerts, _ = detect_alerts(prev, _report(rr=2.0), now_hhmmss="12:01:00")
    assert len(alerts) == 1
    assert alerts[0].category == "rr" and alerts[0].severity == "warning"
    assert alerts[0].body == "RR flipped Put skew → Call skew"


def test_rr_deadband_suppresses_flip():
    prev = _baseline(_report(rr=-2.0))  # committed Put skew
    # +0.1 is inside the ±0.2 dead-band → still Put skew, no flip
    alerts, base2 = detect_alerts(prev, _report(rr=0.1), now_hhmmss="12:01:00")
    assert alerts == []
    assert base2["rr_committed"] == "Put skew"
    # +0.3 clears the dead-band → flips to Call skew
    alerts2, _ = detect_alerts(base2, _report(rr=0.3), now_hhmmss="12:02:00")
    assert len(alerts2) == 1 and alerts2[0].body == "RR flipped Put skew → Call skew"


# --- GEX flip (critical→Destabilizing / info→Stabilizing, 2-wave confirmation) ------------------


def test_gex_flip_requires_two_waves_and_is_critical():
    prev = _baseline(_report(regime="Stabilizing"))
    a1, base1 = detect_alerts(prev, _report(regime="Destabilizing"), now_hhmmss="12:01:00")
    assert a1 == []
    assert base1["gex_pending"] == "Destabilizing" and base1["gex_pending_count"] == 1
    a2, base2 = detect_alerts(base1, _report(regime="Destabilizing"), now_hhmmss="12:02:00")
    assert len(a2) == 1 and a2[0].category == "gex" and a2[0].severity == "critical"
    assert a2[0].body == "GEX regime → Destabilizing"
    assert base2["gex_committed"] == "Destabilizing"


def test_gex_flip_to_stabilizing_is_info():
    prev = _baseline(_report(regime="Destabilizing"))
    a1, base1 = detect_alerts(prev, _report(regime="Stabilizing"), now_hhmmss="12:01:00")
    assert a1 == []
    a2, _ = detect_alerts(base1, _report(regime="Stabilizing"), now_hhmmss="12:02:00")
    assert len(a2) == 1 and a2[0].severity == "info" and a2[0].body == "GEX regime → Stabilizing"


def test_gex_flip_debounce_resets_on_return():
    prev = _baseline(_report(regime="Stabilizing"))
    _a1, base1 = detect_alerts(prev, _report(regime="Destabilizing"), now_hhmmss="12:01:00")  # pending 1
    a2, base2 = detect_alerts(base1, _report(regime="Stabilizing"), now_hhmmss="12:02:00")  # back → clear
    assert a2 == []
    assert base2["gex_pending"] is None and base2["gex_pending_count"] == 0
    assert base2["gex_committed"] == "Stabilizing"


# --- PCR band exit (warning, 0.80/1.30 with 0.85/1.25 re-arm) -----------------------------------


def test_pcr_cross_emits_and_rearm():
    prev = _baseline(_report(pcr=1.0))  # normal
    a1, base1 = detect_alerts(prev, _report(pcr=1.35), now_hhmmss="12:01:00")
    assert len(a1) == 1 and a1[0].category == "pcr" and a1[0].severity == "warning"
    assert base1["pcr_zone"] == "high"
    # 1.28 is still above the 1.25 re-arm → no new alert, stays high
    a2, base2 = detect_alerts(base1, _report(pcr=1.28), now_hhmmss="12:02:00")
    assert a2 == [] and base2["pcr_zone"] == "high"
    # below 1.25 → re-arm to normal, silently
    a3, base3 = detect_alerts(base2, _report(pcr=1.20), now_hhmmss="12:03:00")
    assert a3 == [] and base3["pcr_zone"] == "normal"
    # crosses above again → fires again
    a4, _ = detect_alerts(base3, _report(pcr=1.40), now_hhmmss="12:04:00")
    assert len(a4) == 1 and a4[0].body.startswith("PCR 1.40 above")


def test_pcr_low_band_exit():
    prev = _baseline(_report(pcr=1.0))
    a1, _ = detect_alerts(prev, _report(pcr=0.7), now_hhmmss="12:01:00")
    assert len(a1) == 1 and a1[0].category == "pcr" and a1[0].body.startswith("PCR 0.70 below")


# --- Spot crosses a wall (critical, strike_size/2 dead-band) ------------------------------------


def test_spot_crosses_wall_fires_once_with_hysteresis():
    prev = _baseline(_report(spot=22500, support_strong=22400, strike_size=50.0))  # spot above support, side +1
    # dips to 22390 (10 below support, within the 25 margin) → no cross
    a1, base1 = detect_alerts(prev, _report(spot=22390, support_strong=22400, strike_size=50.0), now_hhmmss="12:01:00")
    assert [x for x in a1 if x.category == "cross"] == []
    # drops to 22360 (40 below support > 25 margin) → critical cross below
    a2, base2 = detect_alerts(base1, _report(spot=22360, support_strong=22400, strike_size=50.0), now_hhmmss="12:02:00")
    crosses = [x for x in a2 if x.category == "cross"]
    assert len(crosses) == 1 and crosses[0].severity == "critical"
    assert "crossed below Support 22,400" in crosses[0].body
    # committed side now -1; staying below → no repeat
    a3, _ = detect_alerts(base2, _report(spot=22350, support_strong=22400, strike_size=50.0), now_hhmmss="12:03:00")
    assert [x for x in a3 if x.category == "cross"] == []


def test_spot_cross_seeds_and_skips_none_levels():
    prev = _baseline(_report(spot=22500, strike_size=50.0))  # every wall level None
    assert prev["wall_sides"] == {}
    a1, _ = detect_alerts(prev, _report(spot=20000, strike_size=50.0), now_hhmmss="12:01:00")
    assert [x for x in a1 if x.category == "cross"] == []


def test_spot_crosses_pain_point_wall():
    # spot below the resistance wall (pain point of the highest-call-OI strike), then crosses above it.
    prev = _baseline(_report(spot=22500, resistance_strong=22600, strike_size=50.0))
    a, _ = detect_alerts(
        prev, _report(spot=22640, resistance_strong=22600, strike_size=50.0), now_hhmmss="12:01:00"
    )
    crosses = [x for x in a if x.category == "cross"]
    assert len(crosses) == 1 and crosses[0].severity == "critical"
    assert "crossed above Resistance 22,600" in crosses[0].body


# --- Strike-rank moves (info) ------------------------------------------------------------------


def test_max_oi_strike_change_emits_info():
    prev = _baseline(_report(coi=22500))
    alerts, _ = detect_alerts(prev, _report(coi=22600), now_hhmmss="12:01:00")
    assert len(alerts) == 1 and alerts[0].category == "oi" and alerts[0].severity == "info"
    assert alerts[0].body == "Max Call OI 22,500 → 22,600"


def test_second_max_volume_strike_change_emits():
    prev = _baseline(_report(s_pvol=23000))
    alerts, _ = detect_alerts(prev, _report(s_pvol=23100), now_hhmmss="12:01:00")
    assert len(alerts) == 1 and alerts[0].category == "vol"
    assert alerts[0].body == "2nd Max Put Vol 23,000 → 23,100"


# --- Per-pair journal + persistence (apply_live_wave / adapter / _render_ticker) ---------------


@dataclass
class _FakeCtx:
    source: str
    kind: str
    pair_key: str


_KEY_A = "13|IDX_I|2026-04-30"
_KEY_B = "25|IDX_I|2026-04-30"


def _alert_dict(body, category="rr", severity="info"):
    return {"time": "12:00:00", "category": category, "severity": severity, "body": body}


def test_alerts_cache_round_trip(tmp_path):
    path = tmp_path / "alerts_cache.json"
    state = {
        "date": "2026-06-10",
        "baselines": {_KEY_A: {"instrument_key": "13|IDX_I", "rr_committed": "Put skew"}},
        "alerts": {_KEY_A: [_alert_dict("RR flipped Put skew → Call skew", category="rr", severity="warning")]},
    }
    write_json_state(path, state)
    assert read_json_state(path) == state


def test_alerts_cache_missing_or_corrupt_returns_empty(tmp_path):
    assert read_json_state(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    assert read_json_state(bad) == {}


def test_apply_live_wave_journals_each_pair_separately():
    state = {"date": "2026-06-10", "baselines": {}, "alerts": {}}
    seed = [
        (_KEY_A, _report(instrument_key="13|IDX_I", rr=-2.0), "Nifty | 2026-04-30"),
        (_KEY_B, _report(instrument_key="25|IDX_I", rr=-2.0), "BankNifty | 2026-04-30"),
    ]
    state, new_alerts, dirty = apply_live_wave(state, seed, today="2026-06-10", now_hhmmss="12:00:00")
    assert new_alerts == []  # first sight of each pair seeds silently
    assert dirty is True

    flip_a = [
        (_KEY_A, _report(instrument_key="13|IDX_I", rr=2.0), "Nifty | 2026-04-30"),
        (_KEY_B, _report(instrument_key="25|IDX_I", rr=-2.0), "BankNifty | 2026-04-30"),
    ]
    state, new_alerts, dirty = apply_live_wave(state, flip_a, today="2026-06-10", now_hhmmss="12:01:00")
    # new_alerts is the wave's (pair_label, alert_dict) forward payload — only pair A flipped.
    assert len(new_alerts) == 1
    label, alert_dict = new_alerts[0]
    assert label == "Nifty | 2026-04-30"
    assert alert_dict["body"] == "RR flipped Put skew → Call skew"
    assert len(state["alerts"][_KEY_A]) == 1
    assert state["alerts"][_KEY_A][0]["body"] == "RR flipped Put skew → Call skew"
    assert state["alerts"][_KEY_A][0]["severity"] == "warning"
    assert _KEY_B not in state["alerts"]  # pair B unchanged → no entry


def test_apply_live_wave_wipes_on_date_change():
    old = {
        "date": "2026-06-09",
        "baselines": {_KEY_A: {"instrument_key": "13|IDX_I", "rr_committed": "Put skew"}},
        "alerts": {_KEY_A: [_alert_dict("yesterday's alert")]},
    }
    state = [(_KEY_A, _report(instrument_key="13|IDX_I", rr=2.0), "Nifty | 2026-04-30")]
    new, new_alerts, dirty = apply_live_wave(old, state, today="2026-06-10", now_hhmmss="09:20:00")
    assert new["date"] == "2026-06-10"
    assert new["alerts"] == {}  # full wipe; first sight after reset seeds silently
    assert new_alerts == []
    assert dirty is True


def _ticker_text(component) -> str:
    def walk(node) -> str:
        if isinstance(node, str):
            return node
        if isinstance(node, (list, tuple)):
            return "".join(walk(n) for n in node)
        children = getattr(node, "children", None)
        return walk(children) if children is not None else ""

    return walk(component)


def test_render_ticker_shows_active_pair_capped():
    working = {"alerts": {_KEY_A: [_alert_dict(f"alert {i}") for i in range(40)]}}
    ctx = _FakeCtx(source="live", kind="pair", pair_key=_KEY_A)
    out, rendered = _render_ticker(working, ctx)
    assert rendered is True
    text = _ticker_text(out)
    assert "alert 0" in text and f"alert {DISPLAY_CAP - 1}" in text
    assert f"alert {DISPLAY_CAP}" not in text  # display capped at DISPLAY_CAP newest
    assert working["_shown"] == {"key": _KEY_A, "n": 40}  # tracker counts the FULL journal


def test_render_ticker_blank_in_playback_and_aggregate():
    playback = _FakeCtx(source="playback", kind="pair", pair_key=_KEY_A)
    out, _ = _render_ticker({"alerts": {_KEY_A: [_alert_dict("a")]}}, playback)
    assert _ticker_text(out) == ""
    aggregate = _FakeCtx(source="live", kind="aggregate", pair_key="aggregate|13|IDX_I")
    out2, _ = _render_ticker({"alerts": {_KEY_A: [_alert_dict("a")]}}, aggregate)
    assert _ticker_text(out2) == ""


def test_render_ticker_renders_seed_then_skips_unchanged_then_reacts_to_new():
    ctx = _FakeCtx(source="live", kind="pair", pair_key=_KEY_A)
    working = {"alerts": {_KEY_A: [_alert_dict("a")]}}
    out, rendered = _render_ticker(working, ctx)
    assert rendered is True and _ticker_text(out) != ""
    out2, rendered2 = _render_ticker(working, ctx)
    assert rendered2 is False and out2 is no_update
    working["alerts"][_KEY_A] = [_alert_dict("b"), _alert_dict("a")]
    out3, rendered3 = _render_ticker(working, ctx)
    assert rendered3 is True and _ticker_text(out3) != ""


def test_render_ticker_swaps_on_pair_change():
    working = {"alerts": {_KEY_A: [_alert_dict("a-alert")], _KEY_B: [_alert_dict("b-alert")]}}
    out_a, _ = _render_ticker(working, _FakeCtx(source="live", kind="pair", pair_key=_KEY_A))
    assert "a-alert" in _ticker_text(out_a)
    out_b, rendered_b = _render_ticker(working, _FakeCtx(source="live", kind="pair", pair_key=_KEY_B))
    assert rendered_b is True and "b-alert" in _ticker_text(out_b)


# --- Critical-alert sound signal (_critical_signal) --------------------------------------------


def test_critical_signal_bumps_on_new_active_critical():
    crit = _alert_dict("Spot crossed below Pin 22,400", category="cross", severity="critical")
    ctx = _FakeCtx(source="live", kind="pair", pair_key=_KEY_A)
    working = {"alerts": {_KEY_A: [crit]}}
    assert _critical_signal(working, ctx) is no_update  # first sight → baseline, no beep
    assert working["_signalled"][_KEY_A] == 1
    assert _critical_signal(working, ctx) is no_update  # unchanged → no beep
    working["alerts"][_KEY_A] = [_alert_dict("x", category="cross", severity="critical"), crit]
    assert _critical_signal(working, ctx) == 1  # a new critical → bump
    assert _critical_signal(working, ctx) is no_update  # unchanged again → no beep


def test_critical_signal_silent_on_pair_switch_to_seen_pair():
    working = {
        "alerts": {
            _KEY_A: [_alert_dict("a", category="cross", severity="critical")],
            _KEY_B: [_alert_dict("b", category="cross", severity="critical")],
        }
    }
    ctx_a = _FakeCtx(source="live", kind="pair", pair_key=_KEY_A)
    ctx_b = _FakeCtx(source="live", kind="pair", pair_key=_KEY_B)
    assert _critical_signal(working, ctx_a) is no_update  # first sight A
    assert _critical_signal(working, ctx_b) is no_update  # first sight B
    assert _critical_signal(working, ctx_a) is no_update  # back to A, no new critical → silent


def test_critical_signal_blank_in_playback_or_aggregate():
    working = {"alerts": {_KEY_A: [_alert_dict("a", category="cross", severity="critical")]}}
    assert _critical_signal(working, _FakeCtx(source="playback", kind="pair", pair_key=_KEY_A)) is no_update
    assert _critical_signal(working, _FakeCtx(source="live", kind="aggregate", pair_key="aggregate|13|IDX_I")) is no_update


# --- Details panel feed (build_details_feed) ---------------------------------------------------


def test_build_details_feed_keeps_all_levels_and_latest_flips():
    def d(time, category, severity, body):
        return {"time": time, "category": category, "severity": severity, "body": body}

    alerts = [  # newest-first, as stored
        d("14:30", "rr", "warning", "RR flipped Call skew → Put skew"),    # latest RR
        d("14:29", "oi", "info", "Max Call OI 22,600 → 22,700"),          # level
        d("14:28", "gex", "critical", "GEX regime → Destabilizing"),      # latest GEX
        d("14:27", "vol", "info", "Max Put Vol 55,000 → 55,500"),         # level
        d("14:20", "rr", "warning", "RR flipped Put skew → Call skew"),   # older RR — dropped
        d("14:18", "gex", "info", "GEX regime → Stabilizing"),            # older GEX — dropped
        d("14:15", "oi", "info", "Max Call OI 22,500 → 22,600"),          # level
    ]
    assert build_details_feed(alerts) == [
        d("14:30", "rr", "warning", "RR flipped Call skew → Put skew"),
        d("14:29", "oi", "info", "Max Call OI 22,600 → 22,700"),
        d("14:28", "gex", "critical", "GEX regime → Destabilizing"),
        d("14:27", "vol", "info", "Max Put Vol 55,000 → 55,500"),
        d("14:15", "oi", "info", "Max Call OI 22,500 → 22,600"),
    ]
