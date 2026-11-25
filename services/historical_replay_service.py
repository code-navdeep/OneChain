"""Historical replay and playback reconstruction."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from analytics.report_builder import build_pair_report, inflate_pair_report
from domain.requests import RequestSpec
from repositories.asset_repository import AssetRepository
from repositories.market_data_repository import MarketDataRepository
from state.builders import build_loaded_report_state
from state.models import LoadedPairState, LoadedReportState, PlaybackMoment
from ui.formatters import format_expiry_label


IST = ZoneInfo("Asia/Kolkata")


def _parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def _format_local_time(value: str) -> str:
    return _parse_timestamp(value).astimezone(IST).strftime("%H:%M:%S")


def _local_date(value: str) -> str:
    return _parse_timestamp(value).astimezone(IST).date().isoformat()


def _split_instrument_key(instrument_key: str) -> tuple[int, str] | None:
    try:
        asset_text, segment_code = instrument_key.split("|", 1)
        return int(asset_text), segment_code
    except (TypeError, ValueError):
        return None


class HistoricalReplayService:
    """Rebuild stored fetch groups back into loaded state."""

    def __init__(
        self,
        market_data_repository: MarketDataRepository,
        asset_repository: AssetRepository,
        holidays=None,
    ) -> None:
        self.market_data_repository = market_data_repository
        self.asset_repository = asset_repository
        self.holidays = holidays
        self._playback_catalog_cache: list[dict] | None = None
        self._playback_catalog_signature: tuple[int, int] | None = None

    @staticmethod
    def _catalog_entry(row: dict) -> dict:
        fetch_group_id = str(row.get("fetch_group_id") or "")
        asset_id = int(row["underlying_asset_id"])
        segment_code = str(row["segment_code"])
        expiry = str(row["expiry"])
        start_ts = str(row["start_ts"])
        return {
            "token": f"{fetch_group_id}|{asset_id}|{segment_code}|{expiry}",
            "fetch_group_id": fetch_group_id,
            "asset_id": asset_id,
            "segment_code": segment_code,
            "instrument_key": f"{asset_id}|{segment_code}",
            "expiry": expiry,
            "start_ts": start_ts,
            "end_ts": str(row["end_ts"]),
            "date": _local_date(start_ts),
            "time": _format_local_time(start_ts),
            "pair_count": int(row.get("pair_count", 0) or 0),
            "_max_row_id": int(row.get("max_row_id", 0) or 0),
        }

    def _build_playback_catalog(self) -> list[dict]:
        """Return the playback catalog, rebuilt incrementally.

        The signature is the O(1) ``(MIN(id), MAX(id))`` bounds — unchanged ⇒ serve the cache
        with no scan. On change: drop entries a prune deleted (``_max_row_id < min_id``) and
        append only the new fetch groups (rows with ``id > previous MAX(id)``). A cold start
        (no cache) does one full build via ``...since(0)``. Replaces the old per-access full
        GROUP BY scan (and its full-scan signature).
        """
        min_id, max_id = self.market_data_repository.fetch_group_id_bounds()
        signature = (min_id, max_id)
        if self._playback_catalog_signature == signature and self._playback_catalog_cache is not None:
            return list(self._playback_catalog_cache)

        if max_id == 0:  # option_rows is empty (AUTOINCREMENT ids start at 1)
            self._playback_catalog_signature = signature
            self._playback_catalog_cache = []
            return []

        prev = self._playback_catalog_signature
        if prev is None or self._playback_catalog_cache is None:
            rows = self.market_data_repository.list_fetch_group_pair_summaries_since(0)
            catalog = [self._catalog_entry(row) for row in rows]
        else:
            prev_min, prev_max = prev
            catalog = list(self._playback_catalog_cache)
            if min_id > prev_min:  # a prune ran — drop fully-deleted groups
                catalog = [entry for entry in catalog if entry["_max_row_id"] >= min_id]
            if max_id > prev_max:  # new fetch groups — append only the new rows' groups
                existing = {entry["token"] for entry in catalog}
                new_rows = self.market_data_repository.list_fetch_group_pair_summaries_since(prev_max)
                for row in new_rows:
                    entry = self._catalog_entry(row)
                    if entry["token"] not in existing:
                        catalog.append(entry)

        catalog.sort(key=lambda item: str(item["start_ts"]), reverse=True)
        self._playback_catalog_signature = signature
        self._playback_catalog_cache = list(catalog)
        return list(catalog)

    def list_playback_assets(self) -> list[dict[str, str]]:
        catalog = self._build_playback_catalog()
        if not catalog:
            return []
        seen: set[str] = set()
        options: list[dict[str, str]] = []
        for item in catalog:
            instrument_key = str(item["instrument_key"])
            if instrument_key in seen:
                continue
            seen.add(instrument_key)
            resolved = _split_instrument_key(instrument_key)
            if resolved is None:
                continue
            asset_id, segment_code = resolved
            asset = self.asset_repository.get_asset(asset_id, segment_code)
            label = asset.display_name if asset is not None else instrument_key
            options.append({"label": label, "value": instrument_key})
        options.sort(key=lambda item: item["label"])
        return options

    def list_playback_expiries(self, instrument_key: str | None) -> list[str]:
        if not instrument_key:
            return []
        catalog = self._build_playback_catalog()
        expiries = {
            str(item["expiry"])
            for item in catalog
            if str(item["instrument_key"]) == instrument_key
        }
        return sorted(expiries)

    def list_playback_dates(
        self,
        instrument_key: str | None = None,
        expiry: str | None = None,
    ) -> list[str]:
        catalog = self._build_playback_catalog()
        ordered_dates: list[str] = []
        seen: set[str] = set()
        for item in catalog:
            if instrument_key and str(item["instrument_key"]) != instrument_key:
                continue
            if expiry and str(item["expiry"]) != expiry:
                continue
            date_value = str(item["date"])
            if date_value in seen:
                continue
            seen.add(date_value)
            ordered_dates.append(date_value)
        return ordered_dates

    def list_playback_moments(
        self,
        selected_date: str | None,
        instrument_key: str | None = None,
        expiry: str | None = None,
    ) -> list[PlaybackMoment]:
        if not selected_date:
            return []
        catalog = self._build_playback_catalog()
        moments: list[PlaybackMoment] = []
        for item in catalog:
            if str(item["date"]) != selected_date:
                continue
            if instrument_key and str(item["instrument_key"]) != instrument_key:
                continue
            if expiry and str(item["expiry"]) != expiry:
                continue
            expiry_label = format_expiry_label(str(item["expiry"]))
            moments.append(
                PlaybackMoment(
                    token=str(item["token"]),
                    date=selected_date,
                    label=f"{str(item['time'])} | {expiry_label} | {int(item['pair_count'])} pair(s)",
                    start_ts=str(item["start_ts"]),
                    end_ts=str(item["end_ts"]),
                    fetch_group_id=str(item["fetch_group_id"]),
                    pair_count=int(item["pair_count"]),
                )
            )
        # Oldest-first (ascending) so the playback scrubber runs left→right = forward in time. (The
        # catalog is newest-first for the incremental-append signature; the browser wants chronological.)
        moments.sort(key=lambda moment: moment.start_ts)
        return moments

    def _build_request_spec(self, rows: list[dict]) -> RequestSpec | None:
        if not rows:
            return None
        first_row = rows[0]
        asset_id = int(first_row["underlying_asset_id"])
        segment_code = str(first_row["segment_code"])
        expiry = str(first_row["expiry"])
        asset = self.asset_repository.get_asset(asset_id, segment_code)
        if asset is None:
            return None
        return RequestSpec(
            asset_id=asset_id,
            segment_code=segment_code,
            expiry=expiry,
            display_name=asset.display_name,
            lot_size=asset.lot_size,
            strike_size=asset.strike_size,
        )

    def _build_report(self, fetch_group_id: str, request_spec: RequestSpec, pair_rows: list[dict]) -> dict:
        """Reconstruct the report from the stored per-pair summary + rows (no re-enrichment).

        Reads the precomputed scalar summary from ``pair_reports`` and inflates it with the
        cheap per-strike bits derived from the stored rows. Falls back to a one-off compute
        only when the summary row is missing (e.g. legacy data)."""
        columns = self.market_data_repository.get_pair_report(
            fetch_group_id, request_spec.asset_id, request_spec.segment_code, request_spec.expiry
        )
        if columns is not None:
            return inflate_pair_report(columns, pair_rows, request_spec)
        return build_pair_report(request_spec, pair_rows, holidays=self.holidays)

    def load_fetch_group(self, fetch_group_id: str, *, source: str = "playback") -> LoadedReportState | None:
        rows = self.market_data_repository.list_rows_for_fetch_group(fetch_group_id)
        if not rows:
            return None

        rows_by_pair: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            pair_key = f"{int(row['underlying_asset_id'])}|{row['segment_code']}|{row['expiry']}"
            rows_by_pair[pair_key].append(dict(row))

        request_specs: list[RequestSpec] = []
        pair_reports: dict[str, dict] = {}
        skipped_count = 0
        for pair_key, pair_rows in rows_by_pair.items():
            request_spec = self._build_request_spec(pair_rows)
            if request_spec is None:
                skipped_count += 1
                continue
            capture_timestamp = min(str(row["timestamp"]) for row in pair_rows)
            report = self._build_report(fetch_group_id, request_spec, pair_rows)
            report["fetch_group_id"] = fetch_group_id
            report["capture_timestamp"] = capture_timestamp
            report["capture_timestamp_local"] = _parse_timestamp(capture_timestamp).astimezone(IST).isoformat()
            request_specs.append(request_spec)
            pair_reports[pair_key] = report

        if not request_specs:
            return None

        total = len(request_specs) + skipped_count
        if skipped_count:
            status = f"Loaded {len(request_specs)} of {total} pairs ({skipped_count} asset(s) no longer in CSV)."
        else:
            status = f"Loaded {len(request_specs)} pair(s) from stored fetch group."
        return build_loaded_report_state(
            source=source,
            request_specs=request_specs,
            fetch_group_id=fetch_group_id,
            pair_reports=pair_reports,
            status_text=status,
        )

    def load_pair_from_fetch_group(
        self,
        fetch_group_id: str,
        *,
        asset_id: int,
        segment_code: str,
        expiry: str,
        view_key: str | None = None,
        display_label: str | None = None,
        metadata: dict | None = None,
    ) -> LoadedPairState | None:
        rows = self.market_data_repository.list_rows_for_fetch_group(fetch_group_id)
        pair_rows = [
            dict(row)
            for row in rows
            if int(row["underlying_asset_id"]) == asset_id
            and str(row["segment_code"]) == segment_code
            and str(row["expiry"]) == expiry
        ]
        if not pair_rows:
            return None

        request_spec = self._build_request_spec(pair_rows)
        if request_spec is None:
            return None
        capture_timestamp = min(str(row["timestamp"]) for row in pair_rows)
        report = self._build_report(fetch_group_id, request_spec, pair_rows)
        report["fetch_group_id"] = fetch_group_id
        report["capture_timestamp"] = capture_timestamp
        report["capture_timestamp_local"] = _parse_timestamp(capture_timestamp).astimezone(IST).isoformat()
        return LoadedPairState(
            request=request_spec,
            display_label=display_label or f"{request_spec.display_name} | {request_spec.expiry}",
            view_key=view_key or request_spec.pair_key.value,
            report=report,
            metadata=dict(metadata or {}),
        )
