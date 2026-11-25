"""Canonical instrument and pair key builders."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentKey:
    """Stable identity for an instrument across the app."""

    asset_id: int
    segment_code: str

    @property
    def value(self) -> str:
        return f"{self.asset_id}|{self.segment_code}"

    @classmethod
    def from_value(cls, value: str) -> "InstrumentKey":
        asset_id_raw, segment_code = value.split("|", 1)
        return cls(asset_id=int(asset_id_raw), segment_code=segment_code)

    def to_dict(self) -> dict:
        return {"asset_id": self.asset_id, "segment_code": self.segment_code}


@dataclass(frozen=True)
class PairKey:
    """Stable identity for an option-chain pair."""

    asset_id: int
    segment_code: str
    expiry: str

    @property
    def instrument_key(self) -> InstrumentKey:
        return InstrumentKey(asset_id=self.asset_id, segment_code=self.segment_code)

    @property
    def value(self) -> str:
        return f"{self.asset_id}|{self.segment_code}|{self.expiry}"

    @classmethod
    def from_value(cls, value: str) -> "PairKey":
        asset_id_raw, segment_code, expiry = value.split("|", 2)
        return cls(asset_id=int(asset_id_raw), segment_code=segment_code, expiry=expiry)

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "segment_code": self.segment_code,
            "expiry": self.expiry,
        }
