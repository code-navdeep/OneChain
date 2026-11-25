"""Pure Telegram message builders — one unified monospace ``<pre>`` block per message.

Each message is a Telegram ``parse_mode="HTML"`` string: a single ``<pre>`` block whose first line is a
plain (non-bold) header carrying one emoji, then a ``─`` divider sized to the header, then a stack of
**per-alert sub-blocks** — one ``label  value`` per line, the label column padded so every value aligns —
with a blank line between sub-blocks. Sub-blocks are **grouped by pair** (all of one pair's blocks
together, first-seen order). Keeping the header *inside* the ``<pre>`` makes the grey code box span the
full bubble width (the header is the widest line), so the box never looks narrower than the message.
Padding is on the raw (ASCII) labels and the whole block is ``html.escape``d **after** padding, so an
escaped ``&amp;`` renders one visible char and alignment is preserved. Empty input → "".

Lives in ``services/`` (not ``features/``) so both the server-side collector (Tier-1/Tier-2 forwarding)
and any browser callback can import it — services must not import features. It imports only
``analytics`` + ``ui.formatters`` (the latter precedented by ``services/historical_replay_service``).
"""

from __future__ import annotations

import html
from typing import Sequence

from analytics.rate_alerts import Cluster
from ui.formatters import format_expiry_label_short

_SEVERITY_TAG = {"critical": "CRIT", "warning": "WARN", "info": "INFO"}

_T1_LABEL_W = 8   # longest Tier-1 label "Detail" (6) + 2-space gap
_T2_LABEL_W = 9   # longest Tier-2 label "Strikes" (7) + 2-space gap


def _compact_label(label: object) -> str:
    """``"Nifty | 2026-04-30"`` → ``"Nifty 30 Apr"`` (reuses the app's short-expiry formatter)."""
    parts = [part.strip() for part in str(label).split("|")]
    if len(parts) == 2:
        return f"{parts[0]} {format_expiry_label_short(parts[1])}"
    return str(label).strip()


def _group_by_pair(items: Sequence[tuple[str, object]]) -> list[tuple[str, object]]:
    """Stable group so a pair's blocks stay adjacent; first-seen pair order preserved."""
    order: list[str] = []
    buckets: dict[str, list[tuple[str, object]]] = {}
    for label, payload in items:
        key = _compact_label(label)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append((key, payload))
    grouped: list[tuple[str, object]] = []
    for key in order:
        grouped.extend(buckets[key])
    return grouped


def _fmt_strike(value: float) -> str:
    return f"{float(value):g}"


def _sub_block(rows: list[tuple[str, object]], label_w: int) -> str:
    """One alert's block — ``label`` left-padded to ``label_w`` then the value, one pair per line."""
    return "\n".join(f"{label.ljust(label_w)}{value}" for label, value in rows)


def _render(header_title: str, header_emoji: str, now_hhmmss: str, blocks: list[str]) -> str:
    header_line = f"{header_emoji} OneChain · {header_title} · {now_hhmmss} IST"
    divider = "─" * len(header_line)
    table = "\n\n".join(blocks)  # blank line between per-alert blocks
    body = f"{header_line}\n{divider}\n{table}"  # header INSIDE the <pre> → box spans full width
    return f"<pre>\n{html.escape(body)}\n</pre>"


def format_tier1_message(items: Sequence[tuple[str, dict]], *, now_hhmmss: str) -> str:
    """Tier-1 alerts — one aligned block per alert (Pair/Time/Sev/Detail), grouped by pair."""
    if not items:
        return ""
    blocks: list[str] = []
    for pair_label, alert in _group_by_pair(items):
        sev = _SEVERITY_TAG.get(str(alert.get("severity")), str(alert.get("severity")).upper())
        blocks.append(_sub_block([
            ("Pair", pair_label),
            ("Time", alert.get("time", "")),
            ("Sev", sev),
            ("Detail", alert.get("body", "")),
        ], _T1_LABEL_W))
    return _render("Alerts", "⚠️", now_hhmmss, blocks)


def format_tier2_message(items: Sequence[tuple[str, Cluster]], *, now_hhmmss: str) -> str:
    """Tier-2 rate clusters — one aligned block per cluster (Pair/Rule/Dir/Side/Strikes)."""
    if not items:
        return ""
    blocks: list[str] = []
    for pair_label, cluster in _group_by_pair(items):
        strikes = cluster.strikes
        span = f"{_fmt_strike(strikes[0])}-{_fmt_strike(strikes[-1])} ({len(strikes)})" if strikes else "-"
        blocks.append(_sub_block([
            ("Pair", pair_label),
            ("Rule", cluster.rule.upper()),
            ("Dir", cluster.direction),
            ("Side", cluster.option_type),
            ("Strikes", span),
        ], _T2_LABEL_W))
    return _render("Rate Clusters", "⚡", now_hhmmss, blocks)
