"""Asset domain models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from domain.keys import InstrumentKey


@dataclass(frozen=True)
class AssetDefinition:
    """Segment-safe asset metadata resolved from the CSV."""

    asset_id: int
    asset_name: str
    exchange: str
    segment_code: str
    strike_size: float
    lot_size: int
    display_name: str
    number_expiries: int = 1

    @property
    def instrument_key(self) -> InstrumentKey:
        return InstrumentKey(asset_id=self.asset_id, segment_code=self.segment_code)

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "AssetDefinition":
        return cls(
            asset_id=int(row["asset_id"]),
            asset_name=str(row["asset_name"]),
            exchange=str(row["exchange"]),
            segment_code=str(row["segment_code"]),
            strike_size=float(row["strike_size"]),
            lot_size=int(float(row["lot_size"])),
            display_name=str(row.get("display_name") or row["asset_name"]),
            number_expiries=max(1, int(float(row.get("number_expiries") or 1))),
        )
