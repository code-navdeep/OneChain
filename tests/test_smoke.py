from __future__ import annotations

import logging
import os

import dash
import pytest

from app import _apply_dashboard_auth, _configure_logging, create_app
from config import get_app_config, load_env


def test_create_app_returns_dash_instance():
    app = create_app()
    assert isinstance(app, dash.Dash)


def test_auth_is_fail_closed_without_credentials(monkeypatch):
    # SECURITY invariant: no run-without-auth path. Missing creds → the app refuses to start.
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        _apply_dashboard_auth(dash.Dash(__name__))


def test_auth_is_fail_closed_on_empty_credentials(monkeypatch):
    # Empty strings are not credentials — still fail-closed.
    monkeypatch.setenv("DASHBOARD_USER", "")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")
    with pytest.raises(RuntimeError):
        _apply_dashboard_auth(dash.Dash(__name__))


def test_auth_applies_when_credentials_present(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USER", "u")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "p")
    _apply_dashboard_auth(dash.Dash(__name__))  # must not raise


def test_config_uses_storage_names():
    config = get_app_config()
    assert config.market_data_db_path.name == "market.db"
    assert config.snapshot_db_path.name == "snapshot.db"
    assert (config.project_root / "app.py").is_file()  # the app's own folder, whatever it is named
    assert config.env_file_path.name == ".env"
    assert config.assets_csv_path.name == "assets_select.csv"


def test_load_env_reads_dotenv_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("CLIENT_ID=test_client\nACCESS_TOKEN=test_token\n", encoding="utf-8")

    old_client_id = os.environ.pop("CLIENT_ID", None)
    old_access_token = os.environ.pop("ACCESS_TOKEN", None)
    try:
        resolved_path = load_env(tmp_path)
        assert resolved_path == env_file
        assert os.environ.get("CLIENT_ID") == "test_client"
        assert os.environ.get("ACCESS_TOKEN") == "test_token"
    finally:
        if old_client_id is None:
            os.environ.pop("CLIENT_ID", None)
        else:
            os.environ["CLIENT_ID"] = old_client_id
        if old_access_token is None:
            os.environ.pop("ACCESS_TOKEN", None)
        else:
            os.environ["ACCESS_TOKEN"] = old_access_token


def test_configure_logging_writes_ist_stamped_file(tmp_path):
    """The file sink persists exactly what the terminal shows: a WARNING emitted after setup
    lands in logs/onechain.log with the IST stamp (dir auto-created; delay=True opens on first
    record). Config wires the dir to data/logs. Restores a plain stream config afterwards so
    the suite's logging is untouched."""
    log_dir = tmp_path / "logs"
    try:
        _configure_logging(log_dir)
        logging.getLogger("onechain.test").warning("smoke-log-line")
        content = (log_dir / "onechain.log").read_text(encoding="utf-8")
        assert "smoke-log-line" in content
        assert " IST  WARNING  onechain.test" in content
        assert get_app_config().log_dir == get_app_config().data_dir / "logs"
    finally:
        logging.basicConfig(level=logging.WARNING, handlers=[logging.StreamHandler()], force=True)


def test_shell_layout_contains_frozen_store_ids_and_controls():
    app = create_app()
    layout_text = repr(app.layout)
    assert "control-draft-store" in layout_text
    assert "request-plan-store" in layout_text
    # Each loaded/live state is split into a small meta store + a rows store.
    assert "live-loaded-report-meta-store" in layout_text
    assert "live-loaded-report-rows-store" in layout_text
    assert "loaded-report-meta-store" in layout_text
    assert "loaded-report-rows-store" in layout_text
    assert "active-view-context-store" in layout_text
    assert "playback-state-store" in layout_text
    assert "vix-heartbeat-store" in layout_text
    assert "advanced-report-store" in layout_text
    assert "alerts-state-store" in layout_text
    assert "critical-alert-signal-store" in layout_text  # bumped on a new critical → clientside beep
    assert "alert-sound-sink" in layout_text
    assert "rate-sound-signal-store" in layout_text  # Tier-2 rate-cluster sound
    assert "rate-sound-sink" in layout_text
    assert "rate-heatmap-toggle" in layout_text  # Tier-2 Layer-3 heatmap view toggle
    # The browser render poll (fetching + VIX moved server-side to services/live_collector.py).
    assert "live-render-interval" in layout_text
    assert "last-rendered-group-store" in layout_text
    assert "snapshot-automation-interval" in layout_text
    assert "request-mode-selector" in layout_text
    assert "asset-selector" in layout_text
    assert "loaded-pair-tabs-row" in layout_text
    assert "bottom-panels-container" in layout_text
    assert "bottom-panel-window-selector" in layout_text
    assert "bottom-panel-collapse" in layout_text
    assert "bottom-panel-toggle-button" in layout_text
    assert "bottom-panel-toggle-label" in layout_text
    assert "bottom-panel-ticker" in layout_text
    assert "bottom-panel-details-content" in layout_text  # change feed, in the panel's right 25%
    assert "fetch-live-button" in layout_text
    assert "controls-collapse-button" in layout_text
    assert "advanced-tab-nav" in layout_text
    assert "vix-status-indicator" in layout_text
    assert "chain-fetch-indicator" in layout_text
    assert "charts-container" in layout_text  # Charts tab = strike-distribution grid only
    assert "charts-distribution-content" in layout_text
    # Time-series charts are LAZY-MOUNTED on first surface open, so their graph IDs
    # (oa-aggregate-gex-chart / oa-rr-chart / oa-chart-1 / bottom-price-chart-graph) are NOT in the
    # cold layout — only their mount points + one-shot flags are. The mount callbacks inject the
    # charts the first time the bottom panel / left drawer opens.
    assert "bottom-charts-mount" in layout_text     # the 4 bottom-panel charts mount here on first open
    assert "bottom-charts-mounted" in layout_text   # one-shot lazy-mount flag
    assert "metric-charts-mount" in layout_text     # the 2 metric charts mount here on first drawer open
    assert "metric-charts-mounted" in layout_text   # one-shot lazy-mount flag
    assert "metric-drawer" in layout_text  # left drawer hosting the two metric charts
    assert "rmd-clear-button" in layout_text  # Clear button in the required-move drawer header
    assert "aggregate-mode-selector" in layout_text  # view toggle, now on the tab bar
    assert "greeks-toggle" in layout_text  # view toggle, now on the tab bar
    assert "mark-open-button" in layout_text  # Mark popover trigger on the tab bar
    assert "mark-name-input" in layout_text
    assert "mark-save-button" in layout_text
    assert "playback-drawer" in layout_text  # bottom Playback overlay drawer
    assert "playback-drawer-toggle" in layout_text
    assert "playback-bookmark-jump-selector" in layout_text  # now inside the drawer
    assert "advanced-dashboard-content" in layout_text  # Advanced controls strip + dashboard slot
    assert "advanced-expiry-count" in layout_text  # Advanced controls now live on the Advanced tab
