"""Frozen state contracts for the application."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from domain.requests import RequestSpec


@dataclass(frozen=True)
class ControlDraftState:
    """Represents the not-yet-fetched top control selections."""

    request_mode: str = "single_instrument"
    asset_value: Optional[str] = None
    selected_expiries: list[str] = field(default_factory=list)
    request_basket: list[dict[str, Any]] = field(default_factory=list)
    strike_range: str = "10"
    toggles: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ControlDraftState":
        payload = dict(data or {})
        return cls(
            request_mode=str(payload.get("request_mode", "single_instrument")),
            asset_value=payload.get("asset_value"),
            selected_expiries=[str(item) for item in payload.get("selected_expiries", [])],
            request_basket=[dict(item) for item in payload.get("request_basket", [])],
            strike_range=str(payload.get("strike_range", "10")),
            toggles=dict(payload.get("toggles", {})),
        )


@dataclass(frozen=True)
class LoadedPairState:
    """Minimal loaded-pair entry for store synchronization."""

    request: RequestSpec
    display_label: str
    view_key: Optional[str] = None
    report: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved_view_key(self) -> str:
        return self.view_key or self.request.pair_key.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "display_label": self.display_label,
            "view_key": self.resolved_view_key,
            "report": dict(self.report),
            "metadata": dict(self.metadata),
        }

    def to_meta_dict(self) -> dict[str, Any]:
        """Like ``to_dict`` but with the heavy ``report["rows"]`` stripped (for the meta store)."""
        meta_report = {key: value for key, value in self.report.items() if key != "rows"}
        return {
            "request": self.request.to_dict(),
            "display_label": self.display_label,
            "view_key": self.resolved_view_key,
            "report": meta_report,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoadedPairState":
        return cls(
            request=RequestSpec.from_dict(data["request"]),
            display_label=str(data["display_label"]),
            view_key=data.get("view_key"),
            report=dict(data.get("report", {})),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class RequestPlanState:
    """Canonical planned request set derived from the current controls."""

    source: str
    version: str
    request_mode: str
    pair_order: list[str]
    request_specs: list[RequestSpec]
    active_pair_key: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "request_mode": self.request_mode,
            "pair_order": list(self.pair_order),
            "request_specs": [item.to_dict() for item in self.request_specs],
            "active_pair_key": self.active_pair_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RequestPlanState | None":
        if data is None:
            return None
        return cls(
            source=str(data["source"]),
            version=str(data["version"]),
            request_mode=str(data["request_mode"]),
            pair_order=[str(item) for item in data.get("pair_order", [])],
            request_specs=[RequestSpec.from_dict(item) for item in data.get("request_specs", [])],
            active_pair_key=data.get("active_pair_key"),
        )


@dataclass(frozen=True)
class LoadedReportState:
    """Store-safe summary of the currently loaded report set."""

    source: str
    version: str
    pair_order: list[str]
    loaded_pairs: list[LoadedPairState]
    fetch_group_id: Optional[str] = None
    active_pair_key: Optional[str] = None
    status_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "pair_order": list(self.pair_order),
            "loaded_pairs": [item.to_dict() for item in self.loaded_pairs],
            "fetch_group_id": self.fetch_group_id,
            "active_pair_key": self.active_pair_key,
            "status_text": self.status_text,
        }

    def to_meta_dict(self) -> dict[str, Any]:
        """The store-safe summary WITHOUT per-strike rows (the ``*-meta-store``)."""
        return {
            "source": self.source,
            "version": self.version,
            "pair_order": list(self.pair_order),
            "loaded_pairs": [item.to_meta_dict() for item in self.loaded_pairs],
            "fetch_group_id": self.fetch_group_id,
            "active_pair_key": self.active_pair_key,
            "status_text": self.status_text,
        }

    def rows_payload(self) -> dict[str, Any]:
        """The per-pair rows keyed by ``resolved_view_key`` (the ``*-rows-store``)."""
        return {pair.resolved_view_key: list(pair.report.get("rows", [])) for pair in self.loaded_pairs}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoadedReportState":
        return cls(
            source=str(data["source"]),
            version=str(data["version"]),
            pair_order=[str(item) for item in data.get("pair_order", [])],
            loaded_pairs=[LoadedPairState.from_dict(item) for item in data.get("loaded_pairs", [])],
            fetch_group_id=data.get("fetch_group_id"),
            active_pair_key=data.get("active_pair_key"),
            status_text=str(data.get("status_text", "")),
        )

    @classmethod
    def from_meta_and_rows(
        cls, meta_data: dict[str, Any] | None, rows_data: dict[str, Any] | None
    ) -> "LoadedReportState | None":
        """Rehydrate a full state from the meta + rows stores, reattaching each pair's rows by
        ``view_key``. Returns ``None`` when meta is empty/None (nothing loaded)."""
        if not meta_data:
            return None
        rows_by_key = dict(rows_data or {})
        state = cls.from_dict(meta_data)
        if not rows_by_key:
            return state
        hydrated_pairs = []
        for pair in state.loaded_pairs:
            rows = rows_by_key.get(pair.resolved_view_key)
            if rows is None:
                hydrated_pairs.append(pair)
            else:
                report = dict(pair.report)
                report["rows"] = list(rows)
                hydrated_pairs.append(
                    LoadedPairState(
                        request=pair.request,
                        display_label=pair.display_label,
                        view_key=pair.view_key,
                        report=report,
                        metadata=pair.metadata,
                    )
                )
        return cls(
            source=state.source,
            version=state.version,
            pair_order=state.pair_order,
            loaded_pairs=hydrated_pairs,
            fetch_group_id=state.fetch_group_id,
            active_pair_key=state.active_pair_key,
            status_text=state.status_text,
        )


@dataclass(frozen=True)
class ActiveViewContext:
    """Represents the active runtime focus for the UI."""

    kind: str
    source: str
    instrument_key: Optional[str] = None
    pair_key: Optional[str] = None
    fetch_group_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActiveViewContext":
        return cls(
            kind=str(data["kind"]),
            source=str(data["source"]),
            instrument_key=data.get("instrument_key"),
            pair_key=data.get("pair_key"),
            fetch_group_id=data.get("fetch_group_id"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class PlaybackMoment:
    """Discrete playback moment anchored to stored data."""

    token: str
    date: str
    label: str
    start_ts: str
    end_ts: str
    fetch_group_id: Optional[str] = None
    pair_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlaybackMoment":
        return cls(
            token=str(data["token"]),
            date=str(data["date"]),
            label=str(data["label"]),
            start_ts=str(data["start_ts"]),
            end_ts=str(data["end_ts"]),
            fetch_group_id=data.get("fetch_group_id"),
            pair_count=int(data.get("pair_count", 0)),
        )


@dataclass(frozen=True)
class PlaybackState:
    """Playback-specific UI state."""

    selected_instrument_key: Optional[str] = None
    selected_expiry: Optional[str] = None
    selected_date: Optional[str] = None
    selected_moment_token: Optional[str] = None
    detached_from_live: bool = False
    available_moments: list[PlaybackMoment] = field(default_factory=list)
    newer_live_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_instrument_key": self.selected_instrument_key,
            "selected_expiry": self.selected_expiry,
            "selected_date": self.selected_date,
            "selected_moment_token": self.selected_moment_token,
            "detached_from_live": self.detached_from_live,
            "available_moments": [item.to_dict() for item in self.available_moments],
            "newer_live_count": self.newer_live_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PlaybackState":
        payload = dict(data or {})
        return cls(
            selected_instrument_key=payload.get("selected_instrument_key"),
            selected_expiry=payload.get("selected_expiry"),
            selected_date=payload.get("selected_date"),
            selected_moment_token=payload.get("selected_moment_token"),
            detached_from_live=bool(payload.get("detached_from_live", False)),
            available_moments=[PlaybackMoment.from_dict(item) for item in payload.get("available_moments", [])],
            newer_live_count=int(payload.get("newer_live_count", 0)),
        )


@dataclass(frozen=True)
class RefreshState:
    """Refresh-related state for live mode."""

    auto_refresh_enabled: bool = False
    last_live_request_specs: list[dict[str, Any]] = field(default_factory=list)
    last_strike_range: str = "10"
    last_fetch_timestamp: Optional[str] = None
    status_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RefreshState":
        payload = dict(data or {})
        return cls(
            auto_refresh_enabled=bool(payload.get("auto_refresh_enabled", False)),
            last_live_request_specs=[dict(item) for item in payload.get("last_live_request_specs", [])],
            last_strike_range=str(payload.get("last_strike_range", "10")),
            last_fetch_timestamp=payload.get("last_fetch_timestamp"),
            status_text=str(payload.get("status_text", "")),
        )


@dataclass(frozen=True)
class AutomationState:
    """Daily automation state for snapshot collection."""

    last_started_date: Optional[str] = None
    in_progress: bool = False
    last_status: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AutomationState":
        payload = dict(data or {})
        return cls(
            last_started_date=payload.get("last_started_date"),
            in_progress=bool(payload.get("in_progress", False)),
            last_status=payload.get("last_status"),
        )
