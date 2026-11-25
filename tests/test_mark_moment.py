from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from analytics.option_chain import build_enhanced_option_rows, parse_option_chain_response, stamp_option_rows
from domain.requests import RequestSpec
from features.mark_moment.callbacks import _parse_bookmark_token, _valid_playback_tokens
from features.playback.helpers import build_bookmark_marks
from repositories.asset_repository import AssetRepository
from repositories.market_data_repository import MarketDataRepository
from repositories.saved_moment_repository import SavedMomentRepository
from services.historical_replay_service import HistoricalReplayService
from services.mark_moment_service import MarkMomentService
from state.builders import build_loaded_report_state

ASSETS_CSV = Path(__file__).resolve().parents[1] / "assets" / "assets_select.csv"


def _spec(asset_id: int, segment: str, expiry: str, name: str) -> RequestSpec:
    return RequestSpec(
        asset_id=asset_id,
        segment_code=segment,
        expiry=expiry,
        display_name=name,
        lot_size=65,
        strike_size=50,
    )


def _payload() -> dict:
    return {
        "last_price": 22500,
        "oc": {
            "22500": {
                "ce": {
                    "last_price": 100, "oi": 1000, "previous_oi": 900, "volume": 500,
                    "implied_volatility": 14.0,
                    "greeks": {"delta": 0.5, "gamma": 0.01, "theta": -4.0, "vega": 7.0},
                    "top_bid_price": 99, "top_ask_price": 101,
                },
                "pe": {
                    "last_price": 90, "oi": 950, "previous_oi": 925, "volume": 430,
                    "implied_volatility": 15.0,
                    "greeks": {"delta": -0.5, "gamma": 0.011, "theta": -4.0, "vega": 6.8},
                    "top_bid_price": 89, "top_ask_price": 91,
                },
            }
        },
    }


def test_save_loaded_state_persists_each_pair_separately(tmp_path):
    repo = SavedMomentRepository(tmp_path / "market.db")
    service = MarkMomentService(repo, AssetRepository(ASSETS_CSV))

    specs = [
        _spec(13, "IDX_I", "2026-06-26", "Nifty"),
        _spec(25, "IDX_I", "2026-06-26", "BankNifty"),
    ]
    loaded = build_loaded_report_state(
        source="live",
        request_specs=specs,
        fetch_group_id="G1",
        pair_reports={
            specs[0].pair_key.value: {"underlying_price": 22500, "fetch_group_id": "G1"},
            specs[1].pair_key.value: {"underlying_price": 48000, "fetch_group_id": "G1"},
        },
    )

    result = service.save_loaded_state(loaded, None, "my mark")
    assert result.save_count == 2

    nifty = repo.list_moments(13, "IDX_I", "2026-06-26")
    bank = repo.list_moments(25, "IDX_I", "2026-06-26")
    # each instrument is its own row, not a merged combined instrument
    assert len(nifty) == 1 and len(bank) == 1
    assert nifty[0]["underlying_asset_id"] == 13
    assert bank[0]["underlying_asset_id"] == 25
    # shared label + one save batch ties them together
    assert nifty[0]["label"] == "my mark" == bank[0]["label"]
    assert nifty[0]["save_batch_id"] == bank[0]["save_batch_id"]
    assert nifty[0]["fetch_group_id"] == "G1"


def test_list_saved_moments_scoped_to_instrument_expiry(tmp_path):
    repo = SavedMomentRepository(tmp_path / "market.db")
    service = MarkMomentService(repo, AssetRepository(ASSETS_CSV))
    repo.save_moment(13, "IDX_I", "2026-06-26", label="a", fetch_group_id="G1")
    repo.save_moment(13, "IDX_I", "2026-07-31", label="b", fetch_group_id="G2")
    repo.save_moment(25, "IDX_I", "2026-06-26", label="c", fetch_group_id="G3")

    rows = service.list_saved_moments(13, "IDX_I", "2026-06-26")
    assert [row["label"] for row in rows] == ["a"]
    assert rows[0]["display_label"]  # display label attached


@dataclass
class _FakeMoment:
    fetch_group_id: str


def test_build_bookmark_marks_highlights_only_bookmarked():
    moments = [_FakeMoment("G1"), _FakeMoment("G2"), _FakeMoment("G3")]
    base = {0: {"label": "09:15"}, 1: {"label": "09:16"}, 2: {"label": "09:17"}}
    marks = build_bookmark_marks(moments, {"G2"}, base)

    assert marks[1]["label"].startswith("★")
    assert marks[1]["style"]["color"] == "#ffd700"
    # untouched marks keep their label and carry no highlight style
    assert marks[0]["label"] == "09:15" and "style" not in marks[0]
    assert marks[2]["label"] == "09:17" and "style" not in marks[2]


def test_parse_bookmark_token_handles_pipes_in_fetch_group_id():
    fgid, asset_id, segment, expiry = _parse_bookmark_token("a|b|c|13|IDX_I|2026-06-26")
    assert fgid == "a|b|c"
    assert asset_id == 13 and segment == "IDX_I" and expiry == "2026-06-26"

    # malformed token returns Nones (caller skips)
    assert _parse_bookmark_token("garbage") == ("garbage", None, None, None)


class _StubServices:
    def __init__(self, replay):
        self.historical_replay_service = replay


def test_valid_playback_tokens_intersects_live_catalog(tmp_path):
    market = MarketDataRepository(tmp_path / "market.db")
    stamped = stamp_option_rows(
        parse_option_chain_response(_payload(), 13, "IDX_I", "2026-06-26"),
        "G1",
        timestamp="2026-06-26T03:45:00+00:00",
    )
    market.insert_option_rows(
        build_enhanced_option_rows(stamped, request_spec=_spec(13, "IDX_I", "2026-06-26", "Nifty"),
                                   risk_free_rate=None, market_volatility=None)
    )
    replay = HistoricalReplayService(market, AssetRepository(ASSETS_CSV))

    tokens = _valid_playback_tokens(_StubServices(replay), "13|IDX_I", "2026-06-26")
    assert "G1|13|IDX_I|2026-06-26" in tokens
    # a bookmark whose fetch group was pruned (not in the catalog) is excluded
    assert "ORPHAN|13|IDX_I|2026-06-26" not in tokens
