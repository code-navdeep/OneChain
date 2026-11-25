"""Live-fetch orchestration."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from uuid import uuid4

from analytics.option_chain import (
    build_enhanced_option_rows,
    parse_option_chain_response,
    stamp_option_rows,
)
from analytics.report_builder import build_pair_report, flatten_pair_report
from config import get_mcx_volatility
from domain.requests import RequestSpec
from repositories.asset_repository import AssetRepository
from repositories.market_data_repository import MarketDataRepository
from services.vix_service import resolve_bs_volatility
from state.builders import build_loaded_report_state
from state.models import LoadedReportState


@dataclass
class LiveFetchPairResult:
    """One fetched pair result within a live fetch wave."""

    request: RequestSpec
    report: dict
    row_count: int


@dataclass
class LiveFetchResult:
    """Live-fetch result contract for the new architecture."""

    fetch_group_id: str
    loaded_report_state: LoadedReportState
    pair_results: list[LiveFetchPairResult] = field(default_factory=list)
    status_text: str = ""
    metadata: dict = field(default_factory=dict)


class LiveFetchService:
    """Owns the live fetch/store/report pipeline."""

    def __init__(
        self,
        api_service,
        market_data_repository: MarketDataRepository,
        vix_service=None,
        *,
        asset_repository: AssetRepository | None = None,
        risk_free_rate_nse: float | None = None,
        risk_free_rate_mcx: float | None = None,
        holidays=None,
    ) -> None:
        self.api_service = api_service
        self.market_data_repository = market_data_repository
        self.vix_service = vix_service
        self.asset_repository = asset_repository
        self.risk_free_rate_nse = risk_free_rate_nse
        self.risk_free_rate_mcx = risk_free_rate_mcx
        self.holidays = holidays
        # Serializes all fetch waves through this one shared client — see fetch_request_group.
        self._fetch_lock = threading.Lock()

    def _market_volatility(self, request_spec: RequestSpec, vix_value: float | None) -> float | None:
        mcx_volatility = None
        if request_spec.segment_code == "MCX_COMM" and self.asset_repository is not None:
            asset = self.asset_repository.get_asset(request_spec.asset_id, request_spec.segment_code)
            mcx_volatility = get_mcx_volatility(asset.asset_name if asset is not None else None)
        return resolve_bs_volatility(
            request_spec.segment_code, vix_value=vix_value, mcx_volatility=mcx_volatility
        )

    def fetch_request_group(
        self,
        request_specs: list[RequestSpec],
        *,
        source: str = "live",
        fetch_group_id: str | None = None,
        fetch_vix: bool = False,
        persist: bool = True,
    ) -> LiveFetchResult:
        """Fetch + enrich + report each request spec.

        ``persist=False`` skips the live + enhanced DB writes — used by the ephemeral
        Advanced analysis, which only needs the in-memory per-expiry rows and never
        accumulates data.

        Serialized through ``self._fetch_lock``: the server-side collector thread and the Advanced
        Run callback both call this on the **same** ``LiveFetchService`` instance, so the lock
        guarantees they never hit the Dhan API concurrently (which would rate-limit us). While one
        wave holds the lock the other waits — so an Advanced Run naturally pauses live collection
        until it finishes, then collection resumes on its own. No UI toggling, no cross-feature flags.
        """
        with self._fetch_lock:
            return self._fetch_request_group_locked(
                request_specs,
                source=source,
                fetch_group_id=fetch_group_id,
                fetch_vix=fetch_vix,
                persist=persist,
            )

    def _fetch_request_group_locked(
        self,
        request_specs: list[RequestSpec],
        *,
        source: str = "live",
        fetch_group_id: str | None = None,
        fetch_vix: bool = False,
        persist: bool = True,
    ) -> LiveFetchResult:
        resolved_group_id = fetch_group_id or str(uuid4())
        pair_results: list[LiveFetchPairResult] = []
        failures: list[str] = []
        vix_snapshot = self.vix_service.fetch_and_store() if fetch_vix and self.vix_service is not None else None
        # Single market VIX for this wave, via vix_status(): the latest VIX fetched today at/after
        # 09:15 (any age, carried all session), else DEFAULT_VIX (after the startup grace), else None
        # (BS not computed). The just-stored snapshot, if any, is already reflected (it reads the latest row).
        vix_value = self.vix_service.vix_status().value if self.vix_service is not None else None

        for request_spec in request_specs:
            raw_payload = self.api_service.fetch_option_chain(
                request_spec.asset_id,
                request_spec.segment_code,
                request_spec.expiry,
            )
            if not raw_payload:
                failures.append(request_spec.pair_key.value)
                continue

            normalized_rows = parse_option_chain_response(
                raw_payload,
                asset_id=request_spec.asset_id,
                segment_code=request_spec.segment_code,
                expiry=request_spec.expiry,
            )
            stamped_rows = stamp_option_rows(normalized_rows, resolved_group_id)
            if not stamped_rows:
                failures.append(request_spec.pair_key.value)
                continue

            rate = (
                self.risk_free_rate_mcx
                if request_spec.segment_code == "MCX_COMM"
                else self.risk_free_rate_nse
            )
            enhanced_rows = build_enhanced_option_rows(
                stamped_rows,
                request_spec=request_spec,
                risk_free_rate=rate,
                market_volatility=self._market_volatility(request_spec, vix_value),
                as_of=None,  # live fetch — capture time is now
                holidays=self.holidays,
            )
            report = build_pair_report(request_spec, enhanced_rows, holidays=self.holidays)
            if persist:
                # Enrich once, store once: the enriched rows (which carry timestamp +
                # fetch_group_id) plus the per-pair summary row. Nothing re-derives later.
                self.market_data_repository.insert_option_rows(enhanced_rows)
                capture_ts = str(enhanced_rows[0].get("timestamp")) if enhanced_rows else ""
                self.market_data_repository.insert_pair_report(
                    {
                        **flatten_pair_report(report),
                        "timestamp": capture_ts,
                        "fetch_group_id": resolved_group_id,
                    }
                )
            pair_results.append(
                LiveFetchPairResult(
                    request=request_spec,
                    report=report,
                    row_count=len(enhanced_rows),
                )
            )

        successful_request_specs = [item.request for item in pair_results]
        pair_reports = {item.request.pair_key.value: item.report for item in pair_results}
        loaded_report_state = build_loaded_report_state(
            source=source,
            request_specs=successful_request_specs,
            fetch_group_id=resolved_group_id,
            pair_reports=pair_reports,
            status_text=f"Fetched {len(successful_request_specs)} of {len(request_specs)} request(s)",
        )
        status_text = f"Fetched {len(successful_request_specs)} of {len(request_specs)} request(s)"
        metadata = {
            "failed_pair_keys": failures,
            "pair_reports": pair_reports,
            "vix_snapshot": {
                "value": vix_snapshot.value,
                "metadata": vix_snapshot.metadata,
            }
            if vix_snapshot is not None
            else None,
        }
        return LiveFetchResult(
            fetch_group_id=resolved_group_id,
            loaded_report_state=loaded_report_state,
            pair_results=pair_results,
            status_text=status_text,
            metadata=metadata,
        )


def build_live_fetch_result(
    request_specs: list[RequestSpec],
    source: str = "live",
    fetch_group_id: str | None = None,
) -> LiveFetchResult:
    """Build the shape returned by the live-fetch service."""
    resolved_group_id = fetch_group_id or str(uuid4())
    return LiveFetchResult(
        fetch_group_id=resolved_group_id,
        loaded_report_state=build_loaded_report_state(
            source=source,
            request_specs=request_specs,
            fetch_group_id=resolved_group_id,
            status_text=f"Prepared {len(request_specs)} request(s)",
        ),
        pair_results=[],
        status_text=f"Prepared {len(request_specs)} request(s)",
    )
