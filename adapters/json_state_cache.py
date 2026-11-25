"""Read/write adapter for a daily-JSON state file (a top-level trading ``date`` + a payload).

The generic durable-state adapter shared by the server-side interpretor and the alert journal: each
keeps its per-day state in a single JSON file (``interpretor.json`` / ``alerts_cache.json``) so it
survives a mid-day restart and is wiped fresh each trading day. This module is the only place that
touches those files — a pure stdlib read/write pair. The collector owns the ``{date, …}`` shape + the
daily-reset logic (the ``load_daily_payload`` / ``daily_is_stale`` / ``persist_daily_state`` helpers in
``services/live_collector``); this module just persists it atomically (temp file + ``os.replace`` so a
mid-write crash can't corrupt the state).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def read_json_state(cache_path: Path | str) -> dict[str, Any]:
    """Return the stored dict state, or ``{}`` when absent / unreadable / not a JSON object."""
    resolved_path = Path(cache_path)
    if not resolved_path.exists():
        return {}
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def write_json_state(cache_path: Path | str, state: dict[str, Any]) -> None:
    """Persist the state atomically (temp file + ``os.replace``) so a crash can't corrupt it."""
    resolved_path = Path(cache_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = resolved_path.with_suffix(resolved_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, resolved_path)
