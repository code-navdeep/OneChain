"""CSV-backed asset repository."""

from __future__ import annotations

import csv
from pathlib import Path

from domain.assets import AssetDefinition
from domain.keys import InstrumentKey


class AssetRepository:
    """Resolve assets from the shared CSV in a segment-safe way."""

    def __init__(self, csv_path: Path | str):
        self.csv_path = self._resolve_csv_path(csv_path)
        self._assets_cache: list[AssetDefinition] | None = None

    @staticmethod
    def _resolve_csv_path(csv_path: Path | str) -> Path:
        requested = Path(csv_path)
        if requested.is_absolute():
            return requested

        module_root = Path(__file__).resolve().parents[1]
        project_root = module_root.parent
        candidates = [
            Path.cwd() / requested,
            module_root / requested,
            project_root / requested,
        ]

        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return candidates[0]

    def list_assets(self) -> list[AssetDefinition]:
        if self._assets_cache is None:
            with self.csv_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self._assets_cache = [AssetDefinition.from_mapping(row) for row in reader]
        return list(self._assets_cache)

    def get_asset(self, asset_id: int, segment_code: str) -> AssetDefinition | None:
        for asset in self.list_assets():
            if asset.asset_id == asset_id and asset.segment_code == segment_code:
                return asset
        return None

    def get_asset_by_instrument_key(self, instrument_key: str | InstrumentKey) -> AssetDefinition | None:
        key = (
            instrument_key
            if isinstance(instrument_key, InstrumentKey)
            else InstrumentKey.from_value(instrument_key)
        )
        return self.get_asset(key.asset_id, key.segment_code)
