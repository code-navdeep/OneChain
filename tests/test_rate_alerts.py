from __future__ import annotations

from datetime import datetime, timedelta

from analytics.rate_alerts import (
    RING_MAXLEN,
    compute_rate_crossings,
    contract_id,
    detect_rate_clusters,
    lookback,
    marker_state,
    update_ring,
)

_T = datetime(2026, 7, 13, 10, 0, 0)  # Monday, after 09:45


def _row(strike, side="CE", oi=2000.0, iv=15.0, vol=2000.0):
    return {"strike": strike, "option_type": side, "oi_lots": oi, "iv": iv, "volume_lots": vol}


# --- update_ring ------------------------------------------------------------------------------


def test_update_ring_buckets_and_minute_dedup():
    ring: dict = {}
    key = contract_id(100, "CE")
    update_ring(ring, [_row(100, oi=1000)], now=_T, spot=100)
    assert key in ring and len(ring[key]) == 1
    # same minute, new value → replaces (one bucket per minute, latest wins)
    update_ring(ring, [_row(100, oi=1500)], now=_T.replace(second=40), spot=100)
    assert len(ring[key]) == 1 and ring[key][-1][1] == 1500
    # next minute → a new bucket
    update_ring(ring, [_row(100, oi=1600)], now=_T + timedelta(minutes=1), spot=100)
    assert len(ring[key]) == 2


def test_update_ring_skips_before_0945():
    ring: dict = {}
    update_ring(ring, [_row(100)], now=datetime(2026, 7, 13, 9, 30), spot=100)
    assert ring == {}


def test_update_ring_caps_to_32_strikes_each_side():
    ring: dict = {}
    rows = [_row(100 + i) for i in range(-40, 41)]  # strikes 60..140 (81), spot on 100
    update_ring(ring, rows, now=_T, spot=100)
    assert len(ring) == 65  # 32 below + 32 above + the exact spot strike


def test_update_ring_respects_maxlen():
    ring: dict = {}
    for m in range(RING_MAXLEN + 10):
        update_ring(ring, [_row(100)], now=_T + timedelta(minutes=m), spot=100)
    assert len(ring[contract_id(100, "CE")]) == RING_MAXLEN


# --- lookback (higher-side / missing) ---------------------------------------------------------


def test_lookback_higher_side_and_missing():
    ring: dict = {}
    for m in range(11):
        update_ring(ring, [_row(100, oi=1000 + m * 100)], now=_T + timedelta(minutes=m), spot=100)
    dq = ring[contract_id(100, "CE")]
    now = _T + timedelta(minutes=10)
    assert lookback(dq, 5, "oi", now=now) == 1500   # bucket exactly 5 min old
    assert lookback(dq, 30, "oi", now=now) is None   # nothing that old → warm-up


# --- compute_rate_crossings -------------------------------------------------------------------


def _two_bucket_ring(rule_field, then_val, now_val):
    ring: dict = {}
    kw = {rule_field: then_val}
    update_ring(ring, [_row(100, **kw)], now=_T, spot=100)
    kw = {rule_field: now_val}
    update_ring(ring, [_row(100, **kw)], now=_T + timedelta(minutes=1), spot=100)
    return ring, _T + timedelta(minutes=1)


def test_crossing_oi_up_and_down():
    ring, now = _two_bucket_ring("oi", 1000, 1200)  # +20% ≥ 15% (1m upper)
    assert compute_rate_crossings(ring, now=now)[contract_id(100, "CE")]["oi"][1] == "up"
    ring, now = _two_bucket_ring("oi", 1000, 800)   # -20% ≤ -15%
    assert compute_rate_crossings(ring, now=now)[contract_id(100, "CE")]["oi"][1] == "down"


def test_crossing_gate_a_floor_skips():
    ring, now = _two_bucket_ring("oi", 500, 900)  # +80% but lookback base 500 < 1000 floor
    assert compute_rate_crossings(ring, now=now) == {}


def test_crossing_volume_is_up_only():
    ring, now = _two_bucket_ring("vol", 4000, 2000)  # -50%, but vol has no lower band
    assert compute_rate_crossings(ring, now=now) == {}
    ring, now = _two_bucket_ring("vol", 2000, 4000)  # +100% ≥ 50% (1m upper)
    assert compute_rate_crossings(ring, now=now)[contract_id(100, "CE")]["vol"][1] == "up"


def test_crossing_below_threshold_is_neutral():
    ring, now = _two_bucket_ring("oi", 1000, 1010)  # +1% < 2.5% (oi 1m upper) → no crossing
    assert compute_rate_crossings(ring, now=now) == {}


def test_crossing_iv_no_data_does_not_fire_but_real_crush_does():
    # A missing IV parses to 0.0; update_ring reads a non-positive IV as no-data (None), so a strike
    # whose IV goes dark must NOT read as a −100% crush.
    ring, now = _two_bucket_ring("iv", 45.0, 0.0)   # real → no-data: the old phantom "down"
    assert compute_rate_crossings(ring, now=now) == {}
    ring, now = _two_bucket_ring("iv", 0.0, 45.0)   # no-data → real (appear): stays silent
    assert compute_rate_crossings(ring, now=now) == {}
    ring, now = _two_bucket_ring("iv", 45.0, 10.0)  # real crush -77.8% ≤ -10% (iv 1m lower): still fires
    assert compute_rate_crossings(ring, now=now)[contract_id(100, "CE")]["iv"][1] == "down"


# --- marker_state -----------------------------------------------------------------------------


def test_marker_state_shortest_window():
    assert marker_state(None) is None
    assert marker_state({}) is None
    # Multiple windows crossed → the SHORTEST (1m) wins.
    assert marker_state({1: "up", 5: "down"}) == "up"
    assert marker_state({5: "down", 30: "up"}) == "down"
    # A single crossed window returns its own state regardless of which window it is.
    assert marker_state({30: "up"}) == "up"


# --- detect_rate_clusters ---------------------------------------------------------------------


def test_cluster_3_consecutive_same_side_rule_direction_mixed_windows():
    crossings = {
        contract_id(100, "CE"): {"oi": {1: "up"}},
        contract_id(150, "CE"): {"oi": {5: "up"}},   # mixed window, still counts
        contract_id(200, "CE"): {"oi": {30: "up"}},
    }
    clusters = detect_rate_clusters(crossings, strike_size=50)
    assert len(clusters) == 1
    c = clusters[0]
    assert (c.rule, c.direction, c.option_type) == ("oi", "up", "CE")
    assert c.strikes == (100.0, 150.0, 200.0)


def test_no_cluster_when_not_grid_adjacent():
    crossings = {
        contract_id(100, "CE"): {"oi": {1: "up"}},
        contract_id(200, "CE"): {"oi": {1: "up"}},
        contract_id(300, "CE"): {"oi": {1: "up"}},  # gap 100, grid step 50 → not adjacent
    }
    assert detect_rate_clusters(crossings, strike_size=50) == []


def test_no_cluster_below_min_run():
    crossings = {
        contract_id(100, "CE"): {"oi": {1: "up"}},
        contract_id(150, "CE"): {"oi": {1: "up"}},
    }
    assert detect_rate_clusters(crossings, strike_size=50) == []


def test_cluster_needs_same_direction():
    crossings = {
        contract_id(100, "CE"): {"oi": {1: "up"}},
        contract_id(150, "CE"): {"oi": {1: "down"}},  # breaks the up run
        contract_id(200, "CE"): {"oi": {1: "up"}},
    }
    assert detect_rate_clusters(crossings, strike_size=50) == []


# --- Layer 3 heatmap builder constructs -------------------------------------------------------


def test_build_rate_heatmap_table_constructs():
    from features.chain.heatmap import build_rate_heatmap_table

    report = {"underlying_price": 100.0}
    display_rows = [{"strike": 90.0}, {"strike": 100.0}, {"strike": 110.0}]
    crossings = {contract_id(100, "CE"): {"oi": {1: "up"}}, contract_id(110, "PE"): {"iv": {5: "down"}}}
    card = build_rate_heatmap_table(report, crossings, display_rows)
    assert card is not None and card.children is not None  # a dbc.Card built without error
