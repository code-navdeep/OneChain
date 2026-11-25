"""Read-only adapters for cached expiry metadata."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

IST = ZoneInfo("Asia/Kolkata")


def read_cached_expiries(cache_path: Path | str, asset_id: int, segment_code: str) -> list[str]:
    """Read valid cached expiries for one instrument key.

    This is kept local and read-only so controls can populate quickly
    without hitting the live API.
    """
    resolved_path = Path(cache_path)
    if not resolved_path.exists():
        return []

    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    instrument_key = f"{asset_id}|{segment_code}"
    entry = payload.get(instrument_key, {})
    expiry_list = entry.get("expiry_list", [])
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return [str(expiry) for expiry in expiry_list if str(expiry) >= today]
