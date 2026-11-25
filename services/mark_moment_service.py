"""Mark This Moment persistence and retrieval helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4

from repositories.asset_repository import AssetRepository
from repositories.saved_moment_repository import SavedMomentRepository
from state.models import ActiveViewContext, LoadedReportState


IST = ZoneInfo("Asia/Kolkata")


def _format_saved_label(timestamp: str, label: str | None) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    base = parsed.astimezone(IST).strftime("%d %b %Y %H:%M:%S")
    if label:
        return f"{label} | {base}"
    return base


@dataclass(frozen=True)
class SaveCurrentViewResult:
    save_count: int
    save_batch_id: str | None
    timestamp: str | None
    source_mode: str | None
    view_kind: str | None
    status_text: str


class MarkMomentService:
    """Own pair-based Mark This Moment persistence."""

    def __init__(
        self,
        saved_moment_repository: SavedMomentRepository,
        asset_repository: AssetRepository,
    ) -> None:
        self.saved_moment_repository = saved_moment_repository
        self.asset_repository = asset_repository

    def save_loaded_state(
        self,
        loaded_report_state: LoadedReportState | None,
        active_view_context: ActiveViewContext | None = None,
        label: str | None = None,
    ) -> SaveCurrentViewResult:
        if loaded_report_state is None or not loaded_report_state.loaded_pairs:
            return SaveCurrentViewResult(0, None, None, None, None, "No loaded state available to save.")

        save_batch_id = str(uuid4())
        timestamp = datetime.now(IST).replace(tzinfo=None).isoformat()
        save_count = 0
        source_mode = loaded_report_state.source
        view_kind = active_view_context.kind if active_view_context is not None else "pair"
        for loaded_pair in loaded_report_state.loaded_pairs:
            report = dict(loaded_pair.report)
            pair_fetch_group_id = (
                loaded_pair.metadata.get("fetch_group_id")
                or report.get("fetch_group_id")
                or loaded_report_state.fetch_group_id
            )
            self.saved_moment_repository.save_moment(
                asset_id=loaded_pair.request.asset_id,
                segment_code=loaded_pair.request.segment_code,
                expiry=loaded_pair.request.expiry,
                label=label,
                underlying_price=float(report.get("underlying_price", 0) or 0),
                timestamp=timestamp,
                save_batch_id=save_batch_id,
                fetch_group_id=pair_fetch_group_id,
                source_mode=source_mode,
                view_kind=view_kind,
            )
            save_count += 1
        if source_mode == "playback" and view_kind == "aggregate":
            status_text = f"Saved {save_count} underlying pair(s) from playback aggregate view."
        elif source_mode == "playback":
            status_text = f"Saved {save_count} pair(s) from playback view."
        elif view_kind == "aggregate":
            status_text = f"Saved {save_count} underlying pair(s) from aggregate view."
        else:
            status_text = f"Saved {save_count} pair(s) to Mark This Moment."
        return SaveCurrentViewResult(
            save_count=save_count,
            save_batch_id=save_batch_id,
            timestamp=timestamp,
            source_mode=source_mode,
            view_kind=view_kind,
            status_text=status_text,
        )

    def list_saved_assets(self) -> list[dict]:
        options: list[dict] = []
        for row in self.saved_moment_repository.list_assets():
            asset_id = int(row["underlying_asset_id"])
            segment_code = str(row["segment_code"])
            asset = self.asset_repository.get_asset(asset_id, segment_code)
            label = asset.display_name if asset is not None else f"{asset_id}|{segment_code}"
            options.append(
                {
                    "label": label,
                    "value": f"{asset_id}|{segment_code}",
                }
            )
        return options

    def list_saved_expiries(self, asset_id: int, segment_code: str) -> list[str]:
        return self.saved_moment_repository.list_expiries(asset_id, segment_code)

    def list_saved_moments(self, asset_id: int, segment_code: str, expiry: str) -> list[dict]:
        rows = self.saved_moment_repository.list_moments(asset_id, segment_code, expiry)
        for row in rows:
            row["display_label"] = _format_saved_label(str(row["timestamp"]), row.get("label"))
        return rows

    def get_saved_moment(self, moment_id: int) -> dict | None:
        row = self.saved_moment_repository.get_moment(moment_id)
        if row is None:
            return None
        row["display_label"] = _format_saved_label(str(row["timestamp"]), row.get("label"))
        return row
