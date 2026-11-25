"""Dash entrypoint for the modular OneChain application."""

from __future__ import annotations

import hmac
import logging
import logging.handlers
import os
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import dash
import dash_mantine_components as dmc
from dash_auth import BasicAuth
from flask import request

# dash-mantine-components 0.14 (Mantine 7) requires React 18 — must be set before any Dash app is
# constructed. dbc/dcc/dash_table all render fine under React 18; Phase 0 verifies that.
dash._dash_renderer._set_react_version("18.2.0")

from config import AppConfig, get_app_config
from health import register_health_routes
from features.advanced import register_advanced_callbacks
from features.alerts import register_alerts_callbacks
from features.automation import register_automation_callbacks
from features.bottom_panels import register_bottom_panel_callbacks
from features.controls import register_controls_callbacks
from features.live_core import register_live_core_callbacks
from features.mark_moment import register_mark_moment_callbacks
from features.options_analysis import register_options_analysis_callbacks
from features.playback import register_playback_callbacks
from features.required_move_drawer import register_required_move_drawer_callbacks
from services.dhan_api_service import DhanApiService
from services.eod_scheduler import get_or_create_eod_state, start_eod_scheduler
from services.live_collector import (
    get_or_create_alert_state,
    get_or_create_hover_state,
    get_or_create_interpretor_state,
    get_or_create_live_collector_state,
    get_or_create_rate_ring_state,
    start_live_collector,
)
from services.runtime import build_runtime_services
from services.token_renewal_service import renew_access_token
from ui.shell import build_shell_layout, register_shell_callbacks


IST = ZoneInfo("Asia/Kolkata")

# Minimal "Midnight Quant" Mantine theme (fleshed out in later phases). forceColorScheme="dark"
# makes every DMC component dark-native.
_MANTINE_THEME = {
    "primaryColor": "teal",
    "defaultRadius": "sm",
    "fontFamily": "General Sans, Segoe UI, Helvetica Neue, Arial, sans-serif",
    "fontFamilyMonospace": "Geist Mono, SF Mono, ui-monospace, monospace",
}


def _configure_logging(log_dir: Path) -> None:
    """Route all logs through a stamped IST formatter to the terminal AND a daily-rotated
    file (``log_dir/onechain.log``), so messages (rate limits, timeouts, VIX failures, EOD
    progress) carry a time and survive the terminal session."""
    formatter = logging.Formatter(
        fmt="%(asctime)s IST  %(levelname)s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Host may not be on IST; render asctime in IST regardless.
    formatter.converter = lambda timestamp: datetime.fromtimestamp(timestamp, IST).timetuple()
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [handler]
    # File sink: the terminal stream, persisted — same formatter, no handler-level filter, so the
    # file records exactly what the terminal shows (root WARNING + the INFO-promoted onechain.*
    # loggers below; no new log lines anywhere). One file per calendar day (host runs IST, so day
    # boundaries match trading days), rotated at midnight to onechain.log.YYYY-MM-DD, 14 days kept.
    # delay=True → the file opens on the first record. Fail-open: if the handler can't be created
    # (permissions, disk), the app still starts on the stream handler alone — announced below,
    # never silent — because a logging-disk problem must not block a trading session.
    file_error: OSError | None = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_dir / "onechain.log", when="midnight", backupCount=14, encoding="utf-8", delay=True
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError as exc:
        file_error = exc
    # WARNING root level keeps the terminal to warnings/errors only (no library INFO
    # chatter); the app's own messages — rate limits, timeouts, VIX failures — are all
    # WARNING/exception, so they all surface, now with an IST timestamp.
    logging.basicConfig(level=logging.WARNING, handlers=handlers, force=True)
    if file_error is not None:
        logging.getLogger("onechain").warning(
            "File logging disabled — could not set up %s: %s", log_dir / "onechain.log", file_error
        )

    # Werkzeug pins its OWN logger to INFO whenever it is NOTSET (werkzeug/_internal.py) and logs a
    # line per HTTP request, so it must be set to WARNING explicitly — otherwise every Dash poll
    # floods the console. At WARNING the per-request access lines are suppressed while Werkzeug's own
    # warnings/errors still surface (they propagate to the root console handler). No access-log file.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # The EOD automation logger runs at INFO so its progress lines (which asset, stored
    # rows, RV computed, skips, prune, token renewal) surface in the terminal — without
    # flipping the whole app to INFO and unleashing library chatter. A healthy run should
    # visibly progress, not look hung.
    logging.getLogger("onechain.eod").setLevel(logging.INFO)
    # The day-start token-renewal step (main(), before create_app) logs its outcome here; INFO so
    # the success line surfaces in the terminal too (root stays WARNING, which would suppress it).
    logging.getLogger("onechain.startup").setLevel(logging.INFO)
    # The server-side live collector is a background daemon thread like the EOD scheduler; promote it
    # to INFO too so its progress (waves collected, market-closed stand-down) is visible in the
    # terminal instead of running invisibly. Without this it inherits root WARNING and any INFO line
    # is silently dropped — the exact "collector runs but shows nothing" gap this closes.
    logging.getLogger("onechain.live").setLevel(logging.INFO)


def _enforce_host_allowlist(app: dash.Dash, port: int) -> None:
    """Reject any request whose ``Host`` header isn't loopback:port — DNS-rebinding defense.

    Registered as the **first** ``before_request`` hook (before auth), so a request arriving with a
    foreign hostname (e.g. an attacker's domain rebound to 127.0.0.1) is refused with 403 before
    credentials are even considered. Real browsers send ``Host: localhost:<port>`` /
    ``127.0.0.1:<port>`` (the non-default port is always included), so legitimate traffic passes."""
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @app.server.before_request
    def _reject_foreign_host():
        if request.host not in allowed_hosts:
            return "Forbidden", 403


def _apply_dashboard_auth(app: dash.Dash) -> None:
    """Gate the entire app behind HTTP Basic Auth, with credentials read from .env
    (``DASHBOARD_USER`` / ``DASHBOARD_PASSWORD``). The browser shows a username/password
    dialog on first request each session; every route (incl. /healthz, /readyz) is gated.

    SECURITY — **fail-closed**: if either var is unset/empty the app **refuses to start**
    (``RuntimeError``). There is no run-without-auth path — auth is not optional. Credentials are
    compared in **constant time** (``hmac.compare_digest`` on both fields) so neither the username
    nor the password leaks via response timing. A fresh 256-bit Flask ``secret_key``
    (``secrets.token_hex(32)``) is set per process start so dash-auth's per-request session write
    succeeds (no log flood) — the signed cookie is **never an auth path** (every request re-runs the
    Basic check), so the key only signs an inert username note and changes nothing about access."""
    user = os.getenv("DASHBOARD_USER")
    password = os.getenv("DASHBOARD_PASSWORD")
    if not user or not password:
        raise RuntimeError(
            "Refusing to start: HTTP Basic Auth is mandatory but DASHBOARD_USER / DASHBOARD_PASSWORD "
            "are not both set in the environment (.env). Set them and restart. Auth is never optional."
        )
    expected_user = user.encode("utf-8")
    expected_password = password.encode("utf-8")

    def _verify(candidate_user: str, candidate_password: str) -> bool:
        # Evaluate both comparisons unconditionally (no short-circuit before they run) so the check is
        # constant-time per field — neither username nor password length/content leaks via timing.
        user_ok = hmac.compare_digest((candidate_user or "").encode("utf-8"), expected_user)
        password_ok = hmac.compare_digest((candidate_password or "").encode("utf-8"), expected_password)
        return user_ok and password_ok

    # dash-auth writes the authed user into flask.session on every request (for its group feature,
    # which we don't use). That write needs a secret_key, else Flask logs "Session is not available.
    # Have you set a secret key?" once per Dash poll. Set a fresh 256-bit in-memory key per startup so
    # the write succeeds silently. SECURITY: this is NOT an alternate auth path — Auth.before_request
    # re-runs is_authorized() (our constant-time Basic check) on EVERY request and never reads the
    # session to grant access; the signed cookie carries only the username (already sent in every Basic
    # header), holds no secret, and rotates each restart. Fixes the warning at the root, auth unchanged.
    app.server.secret_key = secrets.token_hex(32)
    BasicAuth(app, auth_func=_verify)
    logging.getLogger("onechain").warning("Dashboard HTTP Basic Auth enabled (user %r).", user)


def create_app(config: AppConfig | None = None) -> dash.Dash:
    """Create the Dash app shell — composes layout, services, and callbacks."""
    resolved_config = config or get_app_config()
    resolved_config.ensure_runtime_dirs()
    runtime_services = build_runtime_services(resolved_config)
    # Materialize the shared EOD state single-threaded, now, so the scheduler thread, the status
    # reflector, and the live-fetch guard never race to lazily create two different copies later.
    get_or_create_eod_state(runtime_services)
    # Same for the live-collector state — the collector thread, the commit callback, and the render
    # poll all read/write this one object, so materialize it single-threaded before any of them run.
    get_or_create_live_collector_state(runtime_services)
    # And the Tier-2 rate ring — written by the collector wave, read by the chain-marker / sound /
    # heatmap callbacks; materialize it single-threaded too.
    get_or_create_rate_ring_state(runtime_services)
    # And the interpretor state — written by the collector wave, read by the chain/panel display
    # callbacks; materialize it single-threaded too.
    get_or_create_interpretor_state(runtime_services)
    # And the Tier-1 alert journal state — written by the collector wave (_update_alerts), read by the
    # browser render-only mirror; materialize it single-threaded too.
    get_or_create_alert_state(runtime_services)
    # And the A2 09:20 hover-snapshot state — written by the collector wave (_update_hover), read by the
    # chain display; materialize it single-threaded too.
    get_or_create_hover_state(runtime_services)

    app = dash.Dash(
        __name__,
        suppress_callback_exceptions=True,
        # Bootstrap 5.3.1 is served **locally** from assets/bootstrap.min.css (Dash auto-loads
        # assets/*.css alphabetically, so it loads before onechain.css, which still overrides it) —
        # no runtime CDN. The component JS bundles already serve locally (serve_locally defaults True).
        title="OneChain",
    )
    _enforce_host_allowlist(app, resolved_config.port)  # DNS-rebinding defense — must precede auth
    _apply_dashboard_auth(app)  # HTTP Basic Auth from .env, before layout/callbacks are wired
    app.layout = dmc.MantineProvider(
        build_shell_layout(resolved_config),
        forceColorScheme="dark",
        theme=_MANTINE_THEME,
    )
    app._onechain_services = runtime_services
    register_health_routes(app, resolved_config, runtime_services)
    register_controls_callbacks(app, resolved_config, runtime_services)
    register_live_core_callbacks(app, runtime_services)
    register_advanced_callbacks(app, runtime_services)
    register_options_analysis_callbacks(app, runtime_services)
    register_required_move_drawer_callbacks(app, runtime_services)
    register_playback_callbacks(app, runtime_services)
    register_mark_moment_callbacks(app, runtime_services)
    register_automation_callbacks(app, runtime_services)
    register_alerts_callbacks(app, runtime_services)
    register_bottom_panel_callbacks(app, runtime_services)
    register_shell_callbacks(app, runtime_services)
    return app


def main() -> None:
    """Run the app shell."""
    config = get_app_config()
    _configure_logging(config.log_dir)
    _startup_logger = logging.getLogger("onechain.startup")
    if config.host != "127.0.0.1":
        # Expected only inside the Docker container (HOST=0.0.0.0 there, see config.py's `host` field
        # comment) — surfaced here so a non-default bind is visible in the terminal, never silent.
        _startup_logger.warning(
            "Non-default HOST=%r in effect — expected only inside the Docker container, where the "
            "published port is pinned to 127.0.0.1 on the host side (see docker-compose.yml).",
            config.host,
        )
    # Day-start Dhan token renewal — runs BEFORE create_app so every service is built with the fresh
    # token. Dhan tokens are valid 24h from issue and RenewToken needs the current token still alive;
    # renewing at startup (hours before the previous EOD token's expiry) plus the unchanged EOD
    # renewal keeps the gap between consecutive renewals well under 24h, so neither ever authenticates
    # against an expired token. Reuses the same never-raising helper EOD uses (writes .env in place,
    # auto-restores on any write failure — no backup file). On success we push the new token into
    # os.environ so create_app → build_runtime_services → DhanApiService.from_config builds with it.
    startup_token = renew_access_token(DhanApiService.from_config(config), config.env_file_path)
    if startup_token:
        os.environ["ACCESS_TOKEN"] = startup_token
        _startup_logger.info("Day-start Dhan token renewal succeeded.")
    else:
        _startup_logger.warning(
            "Day-start Dhan token renewal failed — continuing on the existing token "
            "(it may expire mid-session); a manual refresh may be needed."
        )
    app = create_app(config)
    # Start the server-side EOD scheduler (the daily 15:30 snapshot → prune → token-renewal trigger).
    # main() only — never create_app — so tests / validate.py that call create_app don't spawn a real
    # thread. use_reloader=False (below) guarantees one process → one scheduler.
    start_eod_scheduler(app._onechain_services)
    # Start the server-side live collector (the throttle-proof live fetch loop). main() only, so
    # tests / validate.py that call create_app don't spawn a real thread. Its VIX sub-cadence reuses
    # the (env-configurable, ≥60 s) VIX interval.
    start_live_collector(app._onechain_services, vix_seconds=config.vix_refresh_interval_ms // 1000)
    app.run(
        host=config.host,
        port=config.port,
        debug=False,  # SECURITY: hardcoded — never enable the Werkzeug debugger (evalex = RCE).
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
