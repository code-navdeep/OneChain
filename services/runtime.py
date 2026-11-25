"""Runtime service composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from adapters.holidays import load_holidays
from config import AppConfig
from repositories.asset_repository import AssetRepository
from repositories.market_data_repository import MarketDataRepository
from repositories.saved_moment_repository import SavedMomentRepository
from repositories.snapshot_repository import SnapshotRepository
from services.advanced_analysis_service import AdvancedAnalysisService
from services.dhan_api_service import DhanApiService
from services.historical_replay_service import HistoricalReplayService
from services.live_fetch_service import LiveFetchService
from services.mark_moment_service import MarkMomentService
from services.snapshot_collection_service import SnapshotCollectionService
from services.telegram_notifier import TelegramNotifier
from services.view_data_service import ViewDataService
from services.vix_service import VixService


@dataclass
class RuntimeServices:
    asset_repository: AssetRepository
    api_service: DhanApiService | None
    market_data_repository: MarketDataRepository
    snapshot_repository: SnapshotRepository
    saved_moment_repository: SavedMomentRepository
    vix_service: VixService
    historical_replay_service: HistoricalReplayService
    mark_moment_service: MarkMomentService
    view_data_service: ViewDataService
    live_fetch_service: LiveFetchService | None
    snapshot_collection_service: SnapshotCollectionService | None
    advanced_analysis_service: AdvancedAnalysisService | None
    telegram_notifier: TelegramNotifier | None
    env_file_path: Path
    alerts_cache_path: Path
    interpretor_json_path: Path
    hover_json_path: Path
    holidays: frozenset[date]


def build_runtime_services(config: AppConfig) -> RuntimeServices:
    """Build the runtime service container."""
    asset_repository = AssetRepository(config.assets_csv_path)
    market_data_repository = MarketDataRepository(config.market_data_db_path)
    snapshot_repository = SnapshotRepository(config.snapshot_db_path)
    saved_moment_repository = SavedMomentRepository(config.market_data_db_path)
    api_service = DhanApiService.from_config(config)
    # Holiday-aware trading calendar (loaded once) — threaded into every BS time-to-expiry path
    # so the chain bs_price, report TTE, Advanced gamma profile, and reversal curve all subtract
    # market holidays as well as weekends.
    holidays = load_holidays(config.holidays_path)
    vix_service = VixService(
        market_data_repository,
        fetcher=api_service.get_vix_data if api_service is not None else None,
        default_vix=config.default_vix,
    )
    historical_replay_service = HistoricalReplayService(
        market_data_repository,
        asset_repository,
        holidays=holidays,
    )
    mark_moment_service = MarkMomentService(
        saved_moment_repository,
        asset_repository,
    )
    view_data_service = ViewDataService(
        market_data_repository=market_data_repository,
        snapshot_repository=snapshot_repository,
    )
    live_fetch_service = (
        LiveFetchService(
            api_service,
            market_data_repository,
            vix_service=vix_service,
            asset_repository=asset_repository,
            risk_free_rate_nse=config.risk_free_rate_nse,
            risk_free_rate_mcx=config.risk_free_rate_mcx,
            holidays=holidays,
        )
        if api_service is not None
        else None
    )
    snapshot_collection_service = (
        SnapshotCollectionService(api_service, asset_repository, snapshot_repository, holidays=holidays)
        if api_service is not None
        else None
    )
    advanced_analysis_service = (
        AdvancedAnalysisService(api_service, asset_repository, live_fetch_service)
        if api_service is not None and live_fetch_service is not None
        else None
    )
    telegram_notifier = TelegramNotifier.from_config(config)  # None when creds absent — feature simply off
    return RuntimeServices(
        asset_repository=asset_repository,
        api_service=api_service,
        market_data_repository=market_data_repository,
        snapshot_repository=snapshot_repository,
        saved_moment_repository=saved_moment_repository,
        vix_service=vix_service,
        historical_replay_service=historical_replay_service,
        mark_moment_service=mark_moment_service,
        view_data_service=view_data_service,
        live_fetch_service=live_fetch_service,
        snapshot_collection_service=snapshot_collection_service,
        advanced_analysis_service=advanced_analysis_service,
        telegram_notifier=telegram_notifier,
        env_file_path=config.env_file_path,
        alerts_cache_path=config.alerts_cache_path,
        interpretor_json_path=config.interpretor_json_path,
        hover_json_path=config.hover_json_path,
        holidays=holidays,
    )
