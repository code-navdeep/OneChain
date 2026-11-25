"""Request specification models."""

from __future__ import annotations

from dataclasses import dataclass

from domain.keys import InstrumentKey, PairKey


@dataclass(frozen=True)
class RequestSpec:
    """Canonical fetch unit for the app."""

    asset_id: int
    segment_code: str
    expiry: str
    display_name: str
    lot_size: int
    strike_size: float

    @property
    def instrument_key(self) -> InstrumentKey:
        return InstrumentKey(asset_id=self.asset_id, segment_code=self.segment_code)

    @property
    def pair_key(self) -> PairKey:
        return PairKey(
            asset_id=self.asset_id,
            segment_code=self.segment_code,
            expiry=self.expiry,
        )

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "segment_code": self.segment_code,
            "expiry": self.expiry,
            "display_name": self.display_name,
            "lot_size": self.lot_size,
            "strike_size": self.strike_size,
            "instrument_key": self.instrument_key.value,
            "pair_key": self.pair_key.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RequestSpec":
        return cls(
            asset_id=int(data["asset_id"]),
            segment_code=str(data["segment_code"]),
            expiry=str(data["expiry"]),
            display_name=str(data["display_name"]),
            lot_size=int(data["lot_size"]),
            strike_size=float(data["strike_size"]),
        )
