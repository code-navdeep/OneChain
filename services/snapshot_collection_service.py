"""Local snapshot collection service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

from analytics.historical_metrics import WINDOWS, build_historical_metric_payload
from analytics.option_chain import build_enhanced_option_rows, parse_option_chain_response, stamp_option_rows
from domain.assets import AssetDefinition
from domain.requests import RequestSpec
from repositories.asset_repository import AssetRepository
from repositories.snapshot_repository import SnapshotRepository
from services.dhan_api_service import DhanApiService


IST = ZoneInfo("Asia/Kolkata")
LogFn = Callable[[str], None]


@dataclass(frozen=True)
class SnapshotRunResult:
    """Collection summary for one snapshot run."""

    started_at: str
    completed_at: str
    groups_processed: int
    assets_processed: int
    pairs_processed: int
    rows_written: int
    status: str
    message: str


def _group_assets_for_collection(assets: Sequence[AssetDefinition]) -> list[list[AssetDefinition]]:
    groups: list[list[AssetDefinition]] = []
    single_group: list[AssetDefinition] = []

    for asset in assets:
        if asset.number_expiries > 1:
            if single_group:
                groups.append(single_group)
                single_group = []
            groups.append([asset])
            continue

        single_group.append(asset)
        if len(single_group) >= 10:
            groups.append(single_group)
            single_group = []

    if single_group:
        groups.append(single_group)
    return groups


def _select_expiries(expiries: Sequence[str], *, number_expiries: int, today_ist: str) -> list[str]:
    # Lex sort == chronological for ISO YYYY-MM-DD, so "first" reliably means "nearest expiry"
    # regardless of the API/cache ordering (the same-day drop below depends on that).
    ordered = sorted(str(item) for item in expiries)
    if ordered and ordered[0] == today_ist:
        ordered = ordered[1:]
    return ordered[: max(1, number_expiries)]


class SnapshotCollectionService:
    """Collect snapshot option-chain data into the local snapshot DB."""

    def __init__(
        self,
        api_service: DhanApiService,
        asset_repository: AssetRepository,
        snapshot_repository: SnapshotRepository,
        holidays=None,
    ) -> None:
        self.api_service = api_service
        self.asset_repository = asset_repository
        self.snapshot_repository = snapshot_repository
        self.holidays = holidays

    def collect_once(self, *, log: LogFn | None = None) -> SnapshotRunResult:
        log_fn = log or (lambda _message: None)
        now_ist = datetime.now(IST).replace(tzinfo=None)
        start_iso = now_ist.isoformat()
        today_ist = now_ist.date().isoformat()

        assets = self.asset_repository.list_assets()
        groups = _group_assets_for_collection(assets)

        total_assets = 0
        total_pairs = 0
        total_rows = 0
        success_count = 0
        failure_count = 0
        skipped_count = 0
        status = "success"
        message = "Snapshot collection completed."

        log_fn(f"EOD snapshot started — {len(assets)} asset(s) in {len(groups)} group(s).")
        asset_index = 0

        for group_index, group_assets in enumerate(groups, start=1):
            fetch_group_id = f"snapshot|{today_ist}|{group_index:03d}|{uuid4().hex[:8]}"
            log_fn(f"Group {group_index}/{len(groups)} ({len(group_assets)} asset(s))…")

            for asset in group_assets:
                total_assets += 1
                asset_index += 1
                label = f"[{asset_index}/{len(assets)}] {asset.display_name} ({asset.instrument_key.value})"
                try:
                    expiries = self.api_service.get_expiry_list(asset.asset_id, asset.segment_code, use_cache=True)
                    selected_expiries = _select_expiries(
                        expiries,
                        number_expiries=asset.number_expiries,
                        today_ist=today_ist,
                    )
                    if not selected_expiries:
                        self.snapshot_repository.record_collection_status(
                            asset_id=asset.asset_id,
                            segment_code=asset.segment_code,
                            status="skipped",
                            message="No valid expiry available after same-day skip.",
                            fetch_group_id=fetch_group_id,
                        )
                        skipped_count += 1
                        log_fn(f"{label} → skipped (no valid expiry)")
                        continue

                    log_fn(f"{label} → expiry {', '.join(selected_expiries)}")
                    asset_rows = 0
                    asset_pairs = 0

                    for expiry in selected_expiries:
                        payload = self.api_service.fetch_option_chain(asset.asset_id, asset.segment_code, expiry)
                        normalized_rows = parse_option_chain_response(payload, asset.asset_id, asset.segment_code, expiry)
                        if not normalized_rows:
                            self.snapshot_repository.record_collection_status(
                                asset_id=asset.asset_id,
                                segment_code=asset.segment_code,
                                expiry=expiry,
                                status="empty",
                                message="No rows returned by API payload parser.",
                                fetch_group_id=fetch_group_id,
                            )
                            log_fn(f"    – {expiry}: empty (no rows from API)")
                            continue

                        stamped = stamp_option_rows(normalized_rows, fetch_group_id=fetch_group_id)
                        # Enrich with the same pipeline as live (lots + gamma_exposure); bs_price
                        # is None here (no rate/vol needed for the snapshot's consumers).
                        enriched = build_enhanced_option_rows(
                            stamped,
                            request_spec=RequestSpec(
                                asset_id=asset.asset_id,
                                segment_code=asset.segment_code,
                                expiry=expiry,
                                display_name=asset.display_name,
                                lot_size=asset.lot_size,
                                strike_size=asset.strike_size,
                            ),
                            risk_free_rate=None,
                            market_volatility=None,
                            holidays=self.holidays,
                        )
                        rows_written = self.snapshot_repository.insert_option_rows(enriched)
                        self.snapshot_repository.record_collection_status(
                            asset_id=asset.asset_id,
                            segment_code=asset.segment_code,
                            expiry=expiry,
                            status="success",
                            message=f"Stored {rows_written} row(s).",
                            fetch_group_id=fetch_group_id,
                        )
                        total_pairs += 1
                        total_rows += rows_written
                        success_count += 1
                        asset_rows += rows_written
                        asset_pairs += 1

                    # One constant-maturity (CM-30) metric per instrument, computed once after all
                    # expiries are stored (the IV series variance-interpolates across them).
                    if asset_pairs:
                        max_window = max(WINDOWS)
                        metric_payload = build_historical_metric_payload(
                            date=today_ist,
                            asset_id=asset.asset_id,
                            segment_code=asset.segment_code,
                            daily_closes=self.snapshot_repository.list_daily_underlying_closes(
                                asset.asset_id,
                                asset.segment_code,
                                limit_days=max_window + 1,
                            ),
                            context_rows=self.snapshot_repository.list_daily_atm_context_rows(
                                asset.asset_id,
                                asset.segment_code,
                                lookback_days=max_window,
                            ),
                        )
                        self.snapshot_repository.upsert_historical_metric(metric_payload)
                        asset_rv_done = any(metric_payload.get(f"rv_{window}d") is not None for window in WINDOWS)
                        rv_note = "RV computed" if asset_rv_done else "RV: none yet (insufficient history)"
                        log_fn(f"    ✓ stored {asset_rows:,} rows · {rv_note}")
                except Exception as exc:  # pragma: no cover - defensive runtime guard
                    failure_count += 1
                    self.snapshot_repository.record_collection_status(
                        asset_id=asset.asset_id,
                        segment_code=asset.segment_code,
                        status="error",
                        message=str(exc),
                        fetch_group_id=fetch_group_id,
                    )
                    log_fn(f"{label} → ✗ error: {exc}")

        if failure_count > 0:
            if success_count == 0:
                status = "total_failure"
                message = "Snapshot collection failed for all assets."
            else:
                status = "partial_failure"
                message = "Snapshot collection completed with failures."

        log_fn(
            f"Snapshot done: {status} — {total_pairs} pair(s), {total_rows:,} row(s), "
            f"{skipped_count} skipped, {failure_count} error(s)."
        )
        completed_at = datetime.now(IST).replace(tzinfo=None).isoformat()
        return SnapshotRunResult(
            started_at=start_iso,
            completed_at=completed_at,
            groups_processed=len(groups),
            assets_processed=total_assets,
            pairs_processed=total_pairs,
            rows_written=total_rows,
            status=status,
            message=message,
        )
