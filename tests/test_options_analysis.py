from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from analytics.option_chain import (
    build_enhanced_option_rows,
    parse_option_chain_response,
    stamp_option_rows,
)
from domain.requests import RequestSpec
from features.options_analysis.rendering import (
    build_aggregate_gex_chart,
    build_metric_chart,
    build_risk_reversal_chart,
)
from repositories.market_data_repository import MarketDataRepository


def _request(expiry: str = "2026-04-30") -> RequestSpec:
    return RequestSpec(
        asset_id=13, segment_code="IDX_I", expiry=expiry,
        display_name="Nifty", lot_size=65, strike_size=50,
    )


def _chain(strike_count: int = 25, spot: float = 22500.0) -> dict:
    oc = {}
    start = spot - (strike_count // 2) * 50
    for i in range(strike_count):
        strike = int(start + i * 50)
        oc[str(strike)] = {
            "ce": {"last_price": 120, "oi": 1000 + i, "previous_oi": 900, "volume": 500,
                   "implied_volatility": 14.2,
                   "greeks": {"delta": 0.51, "gamma": 0.01, "theta": -4.5, "vega": 7.1},
                   "top_bid_price": 119, "top_ask_price": 121},
            "pe": {"last_price": 110, "oi": 950 + i, "previous_oi": 925, "volume": 430,
                   "implied_volatility": 15.4,
                   "greeks": {"delta": -0.49, "gamma": 0.011, "theta": -4.1, "vega": 6.8},
                   "top_bid_price": 109, "top_ask_price": 111},
        }
    return {"last_price": spot, "oc": oc}


def _enrich(payload: dict, *, rate, vol=0.15, as_of=None, expiry: str = "2026-04-30") -> list[dict]:
    rows = stamp_option_rows(
        parse_option_chain_response(payload, 13, "IDX_I", expiry),
        "grp",
        timestamp="2026-04-10T03:45:00+00:00",
    )
    return build_enhanced_option_rows(
        rows, request_spec=_request(expiry), risk_free_rate=rate, market_volatility=vol, as_of=as_of
    )


def test_bs_price_is_none_without_rate():
    rows = _enrich(_chain(3), rate=None)
    assert rows
    assert all(row["bs_price"] is None for row in rows)


def test_bs_price_is_none_without_market_volatility():
    # BS must use a market volatility (VIX/commodity), never the option's own IV.
    rows = _enrich(_chain(3), rate=0.065, vol=None)
    assert all(row["bs_price"] is None for row in rows)


def test_bs_price_ignores_per_contract_iv():
    # Same chain, same market vol → identical BS price regardless of the contract's own IV,
    # proving per-row IV is not the BS input.
    payload = _chain(3)
    for strike_payload in payload["oc"].values():
        strike_payload["ce"]["implied_volatility"] = 99.0  # absurd own-IV
    rows = _enrich(payload, rate=0.065, vol=0.15)
    baseline = _enrich(_chain(3), rate=0.065, vol=0.15)
    ce_rows = [r for r in rows if r["option_type"] == "CE"]
    ce_base = [r for r in baseline if r["option_type"] == "CE"]
    assert [r["bs_price"] for r in ce_rows] == [r["bs_price"] for r in ce_base]


def test_bs_price_present_and_per_contract_fields_with_rate():
    rows = _enrich(_chain(3), rate=0.065)
    assert all(isinstance(row["bs_price"], float) for row in rows)
    for row in rows:
        assert row["moneyness_category"] in {"ITM", "OTM"}
        assert "pain_point" in row
        assert "straddle_oi_lots" in row
        assert "risk_reward_ratio" in row


def test_bs_price_uses_capture_time_as_of():
    early = _enrich(_chain(3), rate=0.065, as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))
    late = _enrich(_chain(3), rate=0.065, as_of=datetime(2026, 4, 29, tzinfo=timezone.utc))
    # More time-to-expiry on the earlier capture → different BS price (frozen at capture,
    # not recomputed against "now").
    assert any(e["bs_price"] != l["bs_price"] for e, l in zip(early, late))


def test_market_data_roundtrips_columns_and_oa_reads(tmp_path: Path):
    repo = MarketDataRepository(tmp_path / "market.db")
    repo.insert_option_rows(_enrich(_chain(25), rate=0.065))

    contracts = repo.list_available_contracts(13, "IDX_I", "2026-04-30", since_iso="2026-04-10T00:00:00+00:00")
    assert contracts
    first = (float(contracts[0]["strike"]), str(contracts[0]["option_type"]))

    series = repo.list_option_metric_timeseries(
        13, "IDX_I", "2026-04-30", [first], "bs_price", since_iso="2026-04-10T00:00:00+00:00"
    )
    assert series and "value" in series[0]


def test_oa_metric_validation_rejects_unknown_column(tmp_path: Path):
    repo = MarketDataRepository(tmp_path / "market.db")
    repo.insert_option_rows(_enrich(_chain(3), rate=0.065))
    # Non-allowlisted metric (would be an injection vector) returns [] without querying.
    assert repo.list_option_metric_timeseries(
        13, "IDX_I", "2026-04-30", [(22500.0, "CE")], "underlying_price; DROP TABLE",
        since_iso="2026-04-10T00:00:00+00:00",
    ) == []


def test_oa_figure_builders_handle_empty_and_data():
    assert build_metric_chart([], "oi_lots", "auto") is not None
    assert build_aggregate_gex_chart([], "Nifty", "2026-04-30") is not None
    series = [{"timestamp": "2026-04-10T03:45:00+00:00", "strike": 22500, "option_type": "CE", "value": 1000}]
    figure = build_metric_chart(series, "oi_lots", "auto")
    assert figure.data
    # Axis titles were removed to reclaim panel space — neither axis carries a title now.
    assert figure.layout.yaxis.title.text is None
    assert figure.layout.xaxis.title.text is None


def test_risk_reversal_chart_handles_empty_and_data():
    assert build_risk_reversal_chart([], "Nifty", "2026-04-30") is not None
    series = [
        {"timestamp": "2026-04-10T09:20:00", "risk_reversal_25": -1.5,
         "call_iv_25d": 14.0, "put_iv_25d": 15.5, "atm_iv": 14.8},
        {"timestamp": "2026-04-10T09:25:00", "risk_reversal_25": -1.2,
         "call_iv_25d": 14.1, "put_iv_25d": 15.3, "atm_iv": 14.7},
    ]
    figure = build_risk_reversal_chart(series, "Nifty", "2026-04-30")
    assert figure.data
    assert figure.data[0].y == (-1.5, -1.2)


# ── Per-pair metric-chart selection persistence helpers ──
from features.options_analysis.callbacks import (
    read_pair_selection,
    read_restore_stash,
    write_pair_selection,
)

_PA = "13|IDX_I|2026-06-30"
_PB = "13|IDX_I|2026-07-31"


def test_write_then_read_pair_selection_roundtrips():
    store = write_pair_selection({}, _PA, 1, ["22500_CE_2026-06-30"])
    assert read_pair_selection(store, _PA, 1) == ["22500_CE_2026-06-30"]
    assert read_pair_selection(store, _PA, 2) == []  # the other chart stays empty


def test_pair_selections_isolated_per_pair_and_chart():
    store = write_pair_selection({}, _PA, 1, ["a"])
    store = write_pair_selection(store, _PB, 1, ["b"])   # different expiry → different pair
    store = write_pair_selection(store, _PA, 2, ["c"])   # different chart, same pair
    assert read_pair_selection(store, _PA, 1) == ["a"]
    assert read_pair_selection(store, _PB, 1) == ["b"]
    assert read_pair_selection(store, _PA, 2) == ["c"]


def test_read_pair_selection_filters_to_valid_options():
    store = write_pair_selection({}, _PA, 1, ["keep", "stale"])
    assert read_pair_selection(store, _PA, 1, valid={"keep"}) == ["keep"]  # stale dropped, order kept


def test_read_pair_selection_empty_and_missing_key():
    assert read_pair_selection({}, _PA, 1) == []
    assert read_pair_selection(None, _PA, 1) == []
    assert read_pair_selection({_PA: {"1": ["a"]}}, None, 1) == []  # no pair_key → empty


def test_write_pair_selection_does_not_mutate_input():
    original: dict = {}
    write_pair_selection(original, _PA, 1, ["a"])
    assert original == {}  # returns a new store, leaves the input alone


def test_read_restore_stash_roundtrips_both_charts():
    data_store = {"asset_id": 13, "restore": {"1": ["22500_CE_2026-06-30"], "2": []}}
    assert read_restore_stash(data_store) == (["22500_CE_2026-06-30"], [])


def test_read_restore_stash_absent_means_no_touch():
    # A same-pair refresh writes oa-data-store WITHOUT a stash → None → the restore
    # callback must no_update (never clobber an in-flight user pick on a live tick).
    assert read_restore_stash({"asset_id": 13}) is None
    assert read_restore_stash({}) is None
    assert read_restore_stash(None) is None


def test_read_restore_stash_empty_stash_clears():
    # The pair-is-None clearing write carries an EMPTY stash → two empty lists (clear
    # the pickers), which is distinct from carrying no stash at all.
    assert read_restore_stash({"restore": {"1": [], "2": []}}) == ([], [])
