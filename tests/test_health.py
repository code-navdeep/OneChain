from __future__ import annotations

import base64
import os
from pathlib import Path

from app import create_app
from config import AppConfig
from health import build_readiness_report
from services.runtime import build_runtime_services
from validate import run_architecture_checks


def _make_config(tmp_path: Path) -> AppConfig:
    project_root = tmp_path / "onechain"
    data_dir = project_root / "data"
    assets_csv_path = project_root / "assets_select.csv"
    project_root.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    assets_csv_path.write_text(
        "\n".join(
            [
                "asset_id,asset_name,exchange,segment_code,strike_size,lot_size,display_name",
                "13,Nifty,NSE,IDX_I,50,65,Nifty",
            ]
        ),
        encoding="utf-8",
    )
    return AppConfig(
        project_root=project_root,
        data_dir=data_dir,
        env_file_path=project_root / ".env",
        assets_csv_path=assets_csv_path,
        expiry_cache_path=data_dir / "expiry_cache.json",
        alerts_cache_path=data_dir / "alerts_cache.json",
        holidays_path=data_dir / "holidays.txt",
        market_data_db_path=data_dir / "market.db",
        snapshot_db_path=data_dir / "snapshot.db",
        interpretor_json_path=data_dir / "interpretor.json",
        hover_json_path=data_dir / "hover.json",
        log_dir=data_dir / "logs",
        port=8053,
        debug=False,
    )


def test_readiness_report_is_degraded_without_credentials(tmp_path: Path):
    config = _make_config(tmp_path)
    old_client_id = os.environ.pop("CLIENT_ID", None)
    old_access_token = os.environ.pop("ACCESS_TOKEN", None)
    try:
        services = build_runtime_services(config)
        report = build_readiness_report(config, services)
    finally:
        if old_client_id is not None:
            os.environ["CLIENT_ID"] = old_client_id
        if old_access_token is not None:
            os.environ["ACCESS_TOKEN"] = old_access_token

    assert report.ready is False
    assert report.asset_count == 1
    assert report.has_api_service is False
    assert report.db_files_present["market_data_db"] is True


def test_readiness_report_is_ready_with_dummy_credentials(tmp_path: Path):
    config = _make_config(tmp_path)
    old_client_id = os.environ.get("CLIENT_ID")
    old_access_token = os.environ.get("ACCESS_TOKEN")
    try:
        os.environ["CLIENT_ID"] = "dummy-client"
        os.environ["ACCESS_TOKEN"] = "dummy-token"
        services = build_runtime_services(config)
        report = build_readiness_report(config, services)
    finally:
        if old_client_id is None:
            os.environ.pop("CLIENT_ID", None)
        else:
            os.environ["CLIENT_ID"] = old_client_id
        if old_access_token is None:
            os.environ.pop("ACCESS_TOKEN", None)
        else:
            os.environ["ACCESS_TOKEN"] = old_access_token

    assert report.ready is True
    assert report.has_api_service is True
    assert report.has_live_fetch_service is True
    assert report.storage_under_project is True


def test_validate_architecture_checks_pass():
    result = run_architecture_checks()
    assert result["ok"] is True


# --- HTTP route hardening: Host allowlist + auth + path-stripped bodies ---------------- #

_PATH_FIELDS = ("project_root", "data_dir", "storage_paths")


def _auth_header() -> dict[str, str]:
    # conftest supplies DASHBOARD_USER / DASHBOARD_PASSWORD (fail-closed auth needs them).
    raw = f"{os.environ['DASHBOARD_USER']}:{os.environ['DASHBOARD_PASSWORD']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _client(tmp_path: Path):
    # Isolated tmp config so /readyz init_schema never touches the real data/ DBs.
    return create_app(_make_config(tmp_path)).server.test_client()


def test_healthz_body_has_no_path_fields(tmp_path: Path):
    resp = _client(tmp_path).get(
        "/healthz", base_url="http://localhost:8053/", headers=_auth_header()
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert "project_root" not in body  # path stripped


def test_readyz_body_strips_path_fields(tmp_path: Path):
    resp = _client(tmp_path).get(
        "/readyz", base_url="http://localhost:8053/", headers=_auth_header()
    )
    assert resp.status_code in (200, 503)
    body = resp.get_json()
    assert "ready" in body and "status" in body  # readiness signal retained
    for field_name in _PATH_FIELDS:
        assert field_name not in body  # every path field stripped


def test_foreign_host_is_rejected_before_auth(tmp_path: Path):
    # DNS-rebinding defense: a non-loopback Host is 403'd even with valid credentials.
    resp = _client(tmp_path).get(
        "/healthz", base_url="http://evil.com/", headers=_auth_header()
    )
    assert resp.status_code == 403


def test_missing_auth_is_401(tmp_path: Path):
    resp = _client(tmp_path).get("/healthz", base_url="http://localhost:8053/")
    assert resp.status_code == 401
