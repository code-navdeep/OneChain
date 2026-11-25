"""Pure-compute OHLC aggregation for the bottom-panel price chart.

Takes the underlying price series (``{timestamp, underlying_price}`` rows from
``MarketDataRepository.list_underlying_price_in_range``) and buckets it into OHLC candles.
No I/O, no project imports. Works on ISO-string timestamps.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _parse_timestamp(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def aggregate_ohlc(price_rows: list[dict], timeframe_minutes: int = 1) -> list[dict]:
    """Aggregate an underlying price series into OHLC candles.

    ``price_rows`` is a list of ``{"timestamp": str, "underlying_price": float}``.
    Returns an ascending list of ``{"timestamp": str, "open", "high", "low",
    "close"}`` where ``timestamp`` is the ISO string of the bucket start. Rows with
    an unparseable timestamp or a non-positive price are skipped. Empty input → ``[]``.
    """
    if not price_rows:
        return []
    bucket = max(1, int(timeframe_minutes))

    parsed_points: list[tuple[datetime, float]] = []
    for row in price_rows:
        parsed = _parse_timestamp(row.get("timestamp"))
        price = float(row.get("underlying_price", 0) or 0)
        if parsed is None or price <= 0:
            continue
        parsed_points.append((parsed, price))

    parsed_points.sort(key=lambda item: item[0])

    candles: dict[int, dict] = {}
    order: list[int] = []
    for parsed, price in parsed_points:
        minutes_since_epoch = int(parsed.timestamp() // 60)
        bucket_start = (minutes_since_epoch // bucket) * bucket
        bucket_dt = datetime.fromtimestamp(bucket_start * 60, tz=IST).replace(tzinfo=None)
        bucket_key = bucket_start
        if bucket_key not in candles:
            candles[bucket_key] = {
                "timestamp": bucket_dt.isoformat(),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            }
            order.append(bucket_key)
        else:
            candle = candles[bucket_key]
            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price

    return [candles[key] for key in order]
