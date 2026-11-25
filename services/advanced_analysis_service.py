"""Advanced multi-expiry analysis orchestration.

Ephemeral: fetches its own ladder of N expiries via ``LiveFetchService`` with
``persist=False`` (no DB writes), runs the comprehensive GEX analysis, and returns
a JSON-safe analysis dict. Independent of the loaded chain state.
"""

from __future__ import annotations

from dataclasses import dataclass

from analytics.advanced_gex import build_comprehensive_advanced_analysis
from analytics.report_builder import time_to_expiry_years
from domain.requests import RequestSpec
from repositories.asset_repository import AssetRepository
from services.live_fetch_service import LiveFetchService
from state.selectors import filter_report_rows_by_strike_range


@dataclass
class AdvancedAnalysisService:
    api_service: object
    asset_repository: AssetRepository
    live_fetch_service: LiveFetchService

    def _resolve_expiries(self, asset_id: int, segment_code: str, expiry_count: int) -> list[str]:
        """Cache-first expiry list; force-refetch when the cache yields fewer than N."""
        expiries = self.api_service.get_expiry_list(asset_id, segment_code, use_cache=True)
        if len(expiries) < expiry_count:
            expiries = self.api_service.get_expiry_list(asset_id, segment_code, use_cache=False)
        return expiries[:expiry_count]

    def run(self, asset_id: int, segment_code: str, expiry_count: int, strike_range: str = "All") -> dict | None:
        asset = self.asset_repository.get_asset(asset_id, segment_code)
        if asset is None:
            return None
        expiries = self._resolve_expiries(asset_id, segment_code, expiry_count)
        if not expiries:
            return {"error": "No expiries available for this asset."}

        specs = [
            RequestSpec(
                asset_id=asset.asset_id,
                segment_code=asset.segment_code,
                expiry=expiry,
                display_name=asset.display_name,
                lot_size=asset.lot_size,
                strike_size=asset.strike_size,
            )
            for expiry in expiries
        ]
        result = self.live_fetch_service.fetch_request_group(specs, persist=False, fetch_vix=False)
        if not result.pair_results:
            return {"error": "Advanced fetch returned no data."}

        expiry_rows: dict[str, list[dict]] = {}
        underlying = 0.0
        for pair in result.pair_results:
            report = dict(pair.report)
            rows = filter_report_rows_by_strike_range(report, strike_range)
            expiry_rows[pair.request.expiry] = rows
            if underlying <= 0:
                underlying = float(report.get("underlying_price", 0) or 0)

        # Rate + per-expiry time-to-expiry feed the Perfiliev zero-gamma profile (Black-Scholes
        # gamma recomputation). Sourced off the LiveFetchService rate already wired for this run.
        market = "MCX" if segment_code == "MCX_COMM" else "NSE"
        rate = self.live_fetch_service.risk_free_rate_mcx if market == "MCX" else self.live_fetch_service.risk_free_rate_nse
        tte_by_expiry = {
            expiry: time_to_expiry_years(expiry, market, holidays=self.live_fetch_service.holidays)
            for expiry in expiry_rows
        }
        analysis = build_comprehensive_advanced_analysis(
            expiry_rows, underlying, rate=rate or 0.0, tte_by_expiry=tte_by_expiry
        )
        analysis["request"] = {
            "instrument_key": specs[0].instrument_key.value,
            "display_name": asset.display_name,
            "expiry_count_requested": expiry_count,
            "expiry_count_fetched": len(expiry_rows),
        }
        return analysis
