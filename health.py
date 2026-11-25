"""Readiness and health helpers for OneChain."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config import AppConfig

if TYPE_CHECKING:
    from services.runtime import RuntimeServices


APP_LABEL = "OneChain"


@dataclass(frozen=True)
class ReadinessReport:
    """Serializable readiness report for runtime health checks."""

    app: str
    ready: bool
    status: str
    project_root: str
    data_dir: str
    asset_count: int
    has_api_service: bool
    has_live_fetch_service: bool
    storage_under_project: bool
    storage_paths: dict[str, str]
    db_files_present: dict[str, bool]
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_public_dict(self) -> dict[str, Any]:
        """The HTTP-safe view: the full report minus the filesystem-path fields.

        SECURITY: `/healthz` and `/readyz` are sealed (Host allowlist + mandatory auth), but we also
        strip every path-bearing field (`project_root`, `data_dir`, `storage_paths`) so even an
        authenticated response leaks nothing about the host's layout. The boolean readiness signal
        (`storage_under_project`, `db_files_present`) and counts/issues are retained. `to_dict()` keeps
        the full report for in-process callers (`validate.py`, tests)."""
        public = self.to_dict()
        for path_field in ("project_root", "data_dir", "storage_paths"):
            public.pop(path_field, None)
        return public


def _path_is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def ensure_runtime_storage(config: AppConfig, services: "RuntimeServices") -> None:
    """Initialize storage surfaces so readiness can be evaluated concretely."""
    config.ensure_runtime_dirs()
    services.market_data_repository.init_schema()
    services.snapshot_repository.init_schema()
    services.saved_moment_repository.init_schema()


def build_readiness_report(config: AppConfig, services: "RuntimeServices") -> ReadinessReport:
    """Build the canonical runtime readiness report."""
    ensure_runtime_storage(config, services)

    storage_paths = {
        "assets_csv": str(config.assets_csv_path),
        "expiry_cache": str(config.expiry_cache_path),
        "market_data_db": str(config.market_data_db_path),
        "snapshot_db": str(config.snapshot_db_path),
    }
    storage_under_project = all(
        _path_is_within(Path(path_value), config.project_root)
        for path_value in storage_paths.values()
    )
    db_files_present = {
        "market_data_db": config.market_data_db_path.exists(),
        "snapshot_db": config.snapshot_db_path.exists(),
    }

    issues: list[str] = []
    try:
        asset_count = len(services.asset_repository.list_assets())
    except Exception as exc:  # pragma: no cover - defensive runtime reporting
        asset_count = 0
        issues.append(f"Asset repository failed: {exc}")

    if asset_count <= 0:
        issues.append("No assets are available from assets_select.csv.")
    if not storage_under_project:
        issues.append("One or more storage paths resolve outside the project root.")
    if not services.api_service:
        issues.append("CLIENT_ID or ACCESS_TOKEN is missing; live fetch is disabled.")
    if not services.live_fetch_service:
        issues.append("Live fetch service is unavailable.")
    for db_name, present in db_files_present.items():
        if not present:
            issues.append(f"{db_name} is not initialized.")

    ready = not issues
    return ReadinessReport(
        app=APP_LABEL,
        ready=ready,
        status="ready" if ready else "degraded",
        project_root=str(config.project_root),
        data_dir=str(config.data_dir),
        asset_count=asset_count,
        has_api_service=services.api_service is not None,
        has_live_fetch_service=services.live_fetch_service is not None,
        storage_under_project=storage_under_project,
        storage_paths=storage_paths,
        db_files_present=db_files_present,
        issues=issues,
    )


def register_health_routes(app, config: AppConfig, services: "RuntimeServices") -> None:
    """Register runtime health routes on the Dash/Flask server."""
    from flask import jsonify

    @app.server.get("/healthz")
    def healthz():
        # No project_root / paths in the body — see ReadinessReport.to_public_dict.
        return jsonify({"status": "ok", "app": APP_LABEL, "port": config.port})

    @app.server.get("/readyz")
    def readyz():
        report = build_readiness_report(config, services)
        status_code = 200 if report.ready else 503
        # Path-stripped body (to_public_dict); the routes are also Host-allowlisted + auth-gated.
        return jsonify(report.to_public_dict()), status_code
