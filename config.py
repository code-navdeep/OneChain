"""Central configuration for OneChain."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - fallback for stripped environments
    def load_dotenv(*_args, **_kwargs):
        return False


def load_env(project_root: Path) -> Path:
    """Load .env into process environment if present."""
    env_file_path = project_root / ".env"
    if env_file_path.exists():
        load_dotenv(env_file_path, override=False)
    return env_file_path


def get_mcx_volatility(asset_name: str | None) -> float | None:
    """Commodity volatility (decimal) for an MCX asset, read from env.

    Looks up ``MCX_VOLATILITY_<ASSET_NAME>`` (e.g. ``MCX_VOLATILITY_CRUDEOIL``). Returns
    ``None`` when the env var is absent or unparseable — never a fabricated default, so a
    missing config means the Black-Scholes price is simply not computed."""
    if not asset_name:
        return None
    key = f"MCX_VOLATILITY_{str(asset_name).upper().replace(' ', '_')}"
    raw = os.getenv(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# --- Tier-2 rate-alert tuning — the two user-tunable knobs, loaded from config.json (fail loud) -----
# RATE_ALERT_THRESHOLDS (percentage-change per rule × window(min) × direction — `up` fires on
# pct ≥ +upper, `down` on pct ≤ −lower; `vol` is UP-only) and RATE_ALERT_FLOORS (Gate A — minimum
# absolute base on the *lookback* value in stored units oi_lots / iv% / volume_lots; below this a
# strike's % change is ignored) are the settings that WILL need fine-tuning over live sessions. They
# live in config.json (repo root) so they can be edited without touching source. There is NO fallback:
# a missing / unparseable / malformed config.json raises RuntimeError here, so the app refuses to start
# rather than silently running fabricated values (mirrors the mandatory-auth fail-closed rule).
# Edits take effect on the next process start (loaded once, at import).
_CONFIG_JSON_PATH = Path(__file__).resolve().parent / "config.json"


def _load_rate_alert_config(
    path: Path = _CONFIG_JSON_PATH,
) -> tuple[dict[str, dict[int, dict[str, float]]], dict[str, float]]:
    """Load RATE_ALERT_THRESHOLDS + RATE_ALERT_FLOORS from ``path`` (config.json).

    Fail loud, no fallback: any problem (missing file, bad JSON, missing/empty top-level key, or a leaf
    that won't coerce) raises ``RuntimeError`` so the app refuses to start. JSON forces object keys to
    strings, so threshold window keys are coerced back to ``int`` (floors are string-keyed — untouched).
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise RuntimeError(f"config.json is missing or unreadable at {path}: {exc}") from exc
    except ValueError as exc:  # json.JSONDecodeError
        raise RuntimeError(f"config.json is not valid JSON at {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"config.json must be a JSON object at {path}, got {type(data).__name__}")

    raw_thresholds = data.get("rate_alert_thresholds")
    raw_floors = data.get("rate_alert_floors")
    if not isinstance(raw_thresholds, dict) or not raw_thresholds:
        raise RuntimeError(f"config.json is missing a non-empty 'rate_alert_thresholds' object at {path}")
    if not isinstance(raw_floors, dict) or not raw_floors:
        raise RuntimeError(f"config.json is missing a non-empty 'rate_alert_floors' object at {path}")

    thresholds: dict[str, dict[int, dict[str, float]]] = {}
    for rule, windows in raw_thresholds.items():
        if not isinstance(windows, dict):
            raise RuntimeError(f"config.json rate_alert_thresholds['{rule}'] must be an object at {path}")
        rule_map: dict[int, dict[str, float]] = {}
        for window, band in windows.items():
            if not isinstance(band, dict):
                raise RuntimeError(
                    f"config.json rate_alert_thresholds['{rule}']['{window}'] must be an object at {path}"
                )
            try:
                rule_map[int(window)] = {str(direction): float(value) for direction, value in band.items()}
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"config.json rate_alert_thresholds['{rule}']['{window}'] has a bad value at {path}: {exc}"
                ) from exc
        thresholds[str(rule)] = rule_map

    try:
        floors = {str(rule): float(value) for rule, value in raw_floors.items()}
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"config.json rate_alert_floors has a bad value at {path}: {exc}") from exc

    return thresholds, floors


RATE_ALERT_THRESHOLDS, RATE_ALERT_FLOORS = _load_rate_alert_config()
RATE_ALERT_WINDOWS: tuple[int, ...] = (1, 5, 30)   # minutes — fast / medium / slow
RATE_ALERT_TRACKING_START = (9, 45)                # Gate B — (hour, minute) IST; first ring sample
RATE_ALERT_CLUSTER_SIZE = 3                        # consecutive same-side strikes → a sound cluster
RATE_ALERT_SOUND_COOLDOWN_S = 60.0                 # per-(rule, direction) sound cooldown
RATE_ALERT_SOUND_THROTTLE_S = 15.0                 # global: no two rate sounds within this window


# --- Interpretor: an `enabled` gate + tunables, loaded from the same config.json --------------------
# The intraday OI-structure interpretor (analytics/interpretor.py) reads an `enabled` flag + its
# tunables from the `interpretor` block of config.json so both can be changed over live sessions
# without touching source: the on/off switch, the 60 s shift-confirmation window, the 30-min
# post-shift bias suppression, the ±10 % pain-point hit tolerance, and the per-level confidence
# start + N/step. (The OI floor it needs is reused from RATE_ALERT_FLOORS["oi"] — not duplicated
# here.) The `enabled` gate is TOLERANT, never fail-loud: a missing / empty / absent `interpretor`
# block, a missing `enabled` key, or `"enabled": false` all mean DISABLED — the loader returns
# {"enabled": False} and the tunables are neither required nor validated (the collector skips the
# whole feature and every display surface goes dark via its existing null-safety). ONLY when
# `"enabled": true` are the tunables required + validated fail-loud (a malformed value then raises
# RuntimeError so the app refuses to run fabricated interpretor numbers). Loaded once, at import.
_INTERPRETOR_CONFIDENCE_POSITIONS = ("primary", "closer_second", "further_second")


def _load_interpretor_config(path: Path = _CONFIG_JSON_PATH) -> dict:
    """Load + validate the `interpretor` block from ``path`` (config.json), gated by ``enabled``.

    The **on/off gate is tolerant, never fail-loud**: a missing / empty / absent `interpretor` block,
    a missing `enabled` key, or `"enabled": false` all return the disabled marker ``{"enabled": False}``
    (the collector then skips the whole feature and every display surface goes dark via its existing
    null-safety) — the tunables are neither required nor validated when disabled.

    **Only when `"enabled": true`** are the tunables loaded fail-loud (no fallback): any problem there
    (a leaf that won't coerce, a missing key, a bad confidence object) raises ``RuntimeError`` so the app
    refuses to start with fabricated interpretor numbers. A missing file / bad JSON still raises. The
    validated (enabled) return is a dict with coerced leaves::

        {"enabled": True, "shift_confirm_seconds": float, "bias_suppress_seconds": float,
         "hit_tolerance_pct": float,
         "confidence": {"primary"|"closer_second"|"further_second": {"start": float, "n": int}}}
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise RuntimeError(f"config.json is missing or unreadable at {path}: {exc}") from exc
    except ValueError as exc:  # json.JSONDecodeError
        raise RuntimeError(f"config.json is not valid JSON at {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"config.json must be a JSON object at {path}, got {type(data).__name__}")

    raw = data.get("interpretor")
    # The `enabled` gate is tolerant (never fail-loud): a missing / empty / absent block, a missing
    # `enabled` key, or `"enabled": false` all disable the interpretor. When disabled the tunables are
    # unused, so they are NOT required or validated — return the disabled marker straight away.
    if not isinstance(raw, dict) or not raw or not bool(raw.get("enabled", False)):
        return {"enabled": False}

    confidence_raw = raw.get("confidence")
    if not isinstance(confidence_raw, dict):
        raise RuntimeError(f"config.json interpretor['confidence'] must be an object at {path}")

    try:
        confidence = {
            position: {
                "start": float(confidence_raw[position]["start"]),
                "n": int(confidence_raw[position]["n"]),
            }
            for position in _INTERPRETOR_CONFIDENCE_POSITIONS
        }
        return {
            "enabled": True,
            "shift_confirm_seconds": float(raw["shift_confirm_seconds"]),
            "bias_suppress_seconds": float(raw["bias_suppress_seconds"]),
            "hit_tolerance_pct": float(raw["hit_tolerance_pct"]),
            "confidence": confidence,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"config.json 'interpretor' block is malformed at {path}: {exc}") from exc


INTERPRETOR_CONFIG = _load_interpretor_config()
INTERPRETOR_ENABLED = bool(INTERPRETOR_CONFIG.get("enabled", False))  # False ⇒ collector skips it, display dark


def _load_telegram_forward_config(path: Path = _CONFIG_JSON_PATH) -> dict:
    """The Telegram category filter (``config.json`` ``telegram_forward``) — namespaced ``tier1``/``tier2``,
    per-category yes/no. **Tolerant, never raises** (unlike the fail-loud blocks): any problem — a missing
    file/block, bad JSON, or wrong shape — yields ``{}``, and a missing category is simply treated as
    **off** (default-deny). It gates **Telegram forwarding only**, so the app always runs. Returns
    ``{tier: {category: bool}}`` keeping only the well-formed entries."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        raw = data.get("telegram_forward")
        if not isinstance(raw, dict):
            return {}
        result: dict[str, dict[str, bool]] = {}
        for tier, categories in raw.items():
            if isinstance(categories, dict):
                result[str(tier)] = {str(cat): bool(flag) for cat, flag in categories.items()}
        return result
    except (OSError, ValueError, TypeError):
        return {}


TELEGRAM_FORWARD_CONFIG = _load_telegram_forward_config()


@dataclass(frozen=True)
class AppConfig:
    """Runtime configuration for the app."""

    project_root: Path
    data_dir: Path
    env_file_path: Path
    assets_csv_path: Path
    expiry_cache_path: Path
    alerts_cache_path: Path
    holidays_path: Path
    market_data_db_path: Path
    snapshot_db_path: Path
    interpretor_json_path: Path  # data/interpretor.json — the durable daily interpretor state
    hover_json_path: Path        # data/hover.json — the durable daily 09:20 per-strike hover snapshot (A2)
    log_dir: Path                # data/logs/ — daily-rotated log files (app._configure_logging)
    host: str = "127.0.0.1"  # SECURITY: loopback by default. Overridable ONLY via the HOST env var,
    #                          which is set to "0.0.0.0" exclusively inside the Docker container (see
    #                          docker-compose.yml) — Docker's own port-publish binding
    #                          ("127.0.0.1:PORT:PORT", never bare "PORT:PORT") is what preserves the
    #                          "unreachable from any other machine" guarantee in that case. Every other
    #                          way of running the app (bare `python3 app.py`, tests, validate.py) leaves
    #                          HOST unset and gets the original hardcoded loopback-only behavior. This is
    #                          defense-in-depth alongside mandatory auth and the Host-header allowlist,
    #                          neither of which depend on the bind address. Do not set HOST anywhere else.
    port: int = 8053
    debug: bool = False  # SECURITY: hardcoded False, never read from any env. The Werkzeug interactive
    #                      debugger (evalex console = remote code execution) must be impossible to enable;
    #                      `app.run` also passes debug=False literally, so even a mutated config can't arm it.
    vix_refresh_interval_ms: int = 60_000  # milliseconds; minimum 60 s — now the collector's VIX sub-cadence
    live_render_interval_ms: int = 1_000  # browser render-poll cadence: how often a tab checks the
    #                                        server collector for a new fetch group to repaint (no fetch)
    risk_free_rate_nse: float | None = None  # from RISK_FREE_RATE_NSE; None ⇒ no BS price
    risk_free_rate_mcx: float | None = None  # from RISK_FREE_RATE_MCX; None ⇒ no BS price
    default_vix: float | None = None  # from DEFAULT_VIX (percentage); freshness-gated NSE fallback

    def ensure_runtime_dirs(self) -> None:
        """Create runtime directories required by the app."""
        self.data_dir.mkdir(parents=True, exist_ok=True)


def get_app_config() -> AppConfig:
    """Build the default application configuration."""
    project_root = Path(__file__).resolve().parent
    env_file_path = load_env(project_root)
    data_dir = project_root / "data"

    port = int(os.getenv("PORT", "8053"))
    # No DEBUG env: debug is hardcoded False (see AppConfig.debug) so the Werkzeug debugger can never
    # be armed — there is deliberately no switch to turn it on.

    _vix_seconds = 60
    try:
        _vix_seconds = int(os.getenv("VIX_REFRESH_INTERVAL", "60"))
    except (TypeError, ValueError):
        pass
    if _vix_seconds < 60:
        _vix_seconds = 60
    vix_refresh_interval_ms = _vix_seconds * 1000

    def _env_rate(name: str) -> float | None:
        raw = os.getenv(name)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    risk_free_rate_nse = _env_rate("RISK_FREE_RATE_NSE")
    risk_free_rate_mcx = _env_rate("RISK_FREE_RATE_MCX")
    default_vix = _env_rate("DEFAULT_VIX")

    return AppConfig(
        project_root=project_root,
        data_dir=data_dir,
        env_file_path=env_file_path,
        assets_csv_path=project_root / "assets" / "assets_select.csv",
        expiry_cache_path=data_dir / "expiry_cache.json",
        alerts_cache_path=data_dir / "alerts_cache.json",
        holidays_path=data_dir / "holidays.txt",
        market_data_db_path=data_dir / "market.db",
        snapshot_db_path=data_dir / "snapshot.db",
        interpretor_json_path=data_dir / "interpretor.json",
        hover_json_path=data_dir / "hover.json",
        log_dir=data_dir / "logs",
        host=os.getenv("HOST", "127.0.0.1"),
        port=port,
        vix_refresh_interval_ms=vix_refresh_interval_ms,
        risk_free_rate_nse=risk_free_rate_nse,
        risk_free_rate_mcx=risk_free_rate_mcx,
        default_vix=default_vix,
    )
