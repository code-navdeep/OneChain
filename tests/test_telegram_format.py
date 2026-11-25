"""Tests for the Telegram message formatters (header line + monospace <pre> per-alert blocks)."""

from __future__ import annotations

from analytics.rate_alerts import Cluster
from services.telegram_format import format_tier1_message, format_tier2_message


def _alert(time, category, severity, body):
    return {"time": time, "category": category, "severity": severity, "body": body}


def test_tier1_empty_returns_blank():
    assert format_tier1_message([], now_hhmmss="14:32:11") == ""


def test_tier1_header_inside_pre_block():
    items = [("Nifty | 2026-04-30", _alert("14:32:10", "cross", "critical", "Spot crossed 22800"))]
    msg = format_tier1_message(items, now_hhmmss="15:10:59")
    # Single <pre> block; header line lives INSIDE it (no bold header outside), with a divider.
    assert msg.startswith("<pre>")
    assert "<b>" not in msg
    assert "⚠️ OneChain · Alerts · 15:10:59 IST" in msg
    assert "─" in msg  # divider under the header
    assert msg.split("<pre>")[1].lstrip().startswith("⚠️ OneChain · Alerts")


def test_tier1_block_structure_and_aligned_labels():
    items = [
        ("Nifty | 2026-04-30", _alert("14:32:10", "cross", "critical", "Spot crossed Resistance 22800")),
        ("BankNifty | 2026-04-30", _alert("14:32:11", "gex", "info", "GEX regime up")),
    ]
    msg = format_tier1_message(items, now_hhmmss="14:32:11")
    # One label/value per line, label column padded to 8 chars.
    assert "Pair    Nifty 30 Apr" in msg
    assert "Time    14:32:10" in msg
    assert "Sev     CRIT" in msg
    assert "Detail  Spot crossed Resistance 22800" in msg
    assert "Pair    BankNifty 30 Apr" in msg
    assert "Sev     INFO" in msg
    # Blank line between per-alert blocks (inside the escaped <pre>).
    assert "\n\n" in msg


def test_tier1_no_truncation_of_long_pair_label():
    items = [("MIDCPNIFTY | 2026-04-30", _alert("14:32:10", "oi", "info", "x"))]
    msg = format_tier1_message(items, now_hhmmss="14:32:11")
    assert "Pair    MIDCPNIFTY 30 Apr" in msg  # full label, not cut to a fixed width


def test_tier1_blocks_grouped_by_pair():
    items = [
        ("Nifty | 2026-04-30", _alert("14:32:10", "oi", "info", "A")),
        ("BankNifty | 2026-04-30", _alert("14:32:11", "oi", "info", "B")),
        ("Nifty | 2026-04-30", _alert("14:32:12", "oi", "info", "C")),
    ]
    msg = format_tier1_message(items, now_hhmmss="15:00:00")
    # Both Nifty blocks precede the BankNifty block (grouped, first-seen order).
    assert msg.index("14:32:10") < msg.index("14:32:12") < msg.index("14:32:11")


def test_tier1_html_escapes_body():
    items = [("Nifty | 2026-04-30", _alert("14:32:10", "oi", "info", "a < b & c"))]
    msg = format_tier1_message(items, now_hhmmss="14:32:11")
    assert "a &lt; b &amp; c" in msg
    assert "a < b & c" not in msg.split("<pre>")[1]  # raw specials never leak inside the block


def test_tier2_empty_returns_blank():
    assert format_tier2_message([], now_hhmmss="14:32:11") == ""


def test_tier2_header_structure_and_strike_span():
    items = [
        ("Nifty | 2026-04-30", Cluster("oi", "up", "CE", (22800.0, 22900.0, 23000.0))),
        ("BankNifty | 2026-04-30", Cluster("iv", "down", "PE", (47500.0, 48000.0, 48500.0, 49000.0))),
    ]
    msg = format_tier2_message(items, now_hhmmss="14:32:11")
    assert msg.startswith("<pre>")
    assert "<b>" not in msg
    assert "⚡ OneChain · Rate Clusters · 14:32:11 IST" in msg
    assert "Pair     Nifty 30 Apr" in msg
    assert "Rule     OI" in msg
    assert "Dir      up" in msg
    assert "Side     CE" in msg
    # Strike span: min-max (count).
    assert "Strikes  22800-23000 (3)" in msg
    assert "Strikes  47500-49000 (4)" in msg
