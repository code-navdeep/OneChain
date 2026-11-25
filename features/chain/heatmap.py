"""Tier-2 Layer 3 — the full rate **Surface** view (toggled beside Greeks).

A dedicated rate surface built exactly like the main chain table — plain cells whose colour comes from
``style_data_conditional`` (the same mechanism the chain uses for its ITM/OTM washes), the shared
chain chrome (`CHAIN_STYLE_*`), and no markdown. The chain skeleton (strikes in the middle, the spot
row as an in-row legend) carries **3 rules × 3 timeframes** columns per side, **mirror-symmetric around
the strike column** — outward from strikes it reads **Volume → OI → IV**, windows **1 → 5 → 30**
(1-min innermost). Each cell is coloured green/red/neutral from the **same** ``compute_rate_crossings``
as the Layer-1 markers (the shared-source invariant).
"""

from __future__ import annotations

from typing import Any, Mapping

import dash_bootstrap_components as dbc
from dash import dash_table, html

from analytics.rate_alerts import contract_id
from features.chain.table_model import (
    CHAIN_STYLE_CELL,
    CHAIN_STYLE_HEADER,
    CHAIN_TABLE_CSS,
    _STRIKE_TXT,
    _STRIKE_WASH,
    _UNDER_BG,
    _UNDER_STRIKE_WASH,
)
from ui import theme

_RULE_LABEL = {"oi": "OI", "iv": "IV", "vol": "Vol"}
_PREFIX = {"CE": "call", "PE": "put"}

# Order OUTWARD from the strike column (innermost → outermost): Volume, then OI, then IV; windows 1/5/30.
_OUTWARD_RULES = ("vol", "oi", "iv")
_OUTWARD_WINDOWS = (1, 5, 30)
# PE (right of strike) reads strike-outward = the natural order; CE (left) is its mirror.
_PE_SEQ = [(rule, w) for rule in _OUTWARD_RULES for w in _OUTWARD_WINDOWS]
_CE_SEQ = list(reversed(_PE_SEQ))
_SIDE_SEQ = (("CE", _CE_SEQ), ("PE", _PE_SEQ))


def _heatmap_columns() -> list[dict]:
    columns: list[dict] = []
    for rule, window in _CE_SEQ:
        columns.append({"name": [f"CE {_RULE_LABEL[rule]}", f"{window}m"], "id": f"call_{rule}_{window}", "type": "text"})
    columns.append({"name": ["", "STRIKE"], "id": "strike", "type": "text"})
    for rule, window in _PE_SEQ:
        columns.append({"name": [f"PE {_RULE_LABEL[rule]}", f"{window}m"], "id": f"put_{rule}_{window}", "type": "text"})
    return columns


def build_rate_heatmap_table(
    report: Mapping[str, Any],
    crossings: Mapping[str, Mapping[str, Mapping[int, str]]],
    display_rows: list[dict],
) -> dbc.Card:
    """The full rate surface for one pair — a strike-indexed grid of green/red/neutral cells, built as a
    plain chain-style table (colours via ``style_data_conditional``, keyed by ``row_index``/``column_id``)."""
    underlying = float(report.get("underlying_price", 0) or 0)
    strikes = sorted({float(r.get("strike")) for r in display_rows if r.get("strike") is not None})

    data: list[dict] = []
    colour_styles: list[dict] = []
    spot_index: int | None = None

    def _append_spot() -> None:
        nonlocal spot_index
        spot_index = len(data)
        row: dict = {"strike": f"₹{underlying:,.2f}"}
        for side, seq in _SIDE_SEQ:
            for rule, window in seq:
                row[f"{_PREFIX[side]}_{rule}_{window}"] = f"{_RULE_LABEL[rule]}-{window}"  # in-row legend
        data.append(row)

    boundary_done = False
    for strike in strikes:
        if not boundary_done and underlying and strike >= underlying:
            _append_spot()  # spot row splits ITM/OTM + labels the columns
            boundary_done = True
        row_index = len(data)
        row: dict = {"strike": f"{strike:g}"}
        for side, seq in _SIDE_SEQ:
            states = crossings.get(contract_id(strike, side), {})
            for rule, window in seq:
                col = f"{_PREFIX[side]}_{rule}_{window}"
                row[col] = ""  # empty — the colour is a style rule, so the whole cell fills
                state = states.get(rule, {}).get(window)
                if state in ("up", "down"):
                    colour_styles.append({
                        "if": {"row_index": row_index, "column_id": col},
                        "backgroundColor": theme.UP if state == "up" else theme.DOWN,
                    })
        data.append(row)
    if not boundary_done:
        _append_spot()  # spot above every displayed strike → legend at the bottom

    style_data_conditional = [
        *colour_styles,
        {"if": {"column_id": "strike"}, "backgroundColor": _STRIKE_WASH, "color": _STRIKE_TXT, "fontWeight": "bold"},
    ]
    if spot_index is not None:  # the spot (legend) row — the chain's underlying band
        style_data_conditional.append(
            {"if": {"row_index": spot_index}, "backgroundColor": _UNDER_BG, "fontWeight": "bold", "border": "none"}
        )
        style_data_conditional.append(
            {"if": {"row_index": spot_index, "column_id": "strike"}, "backgroundColor": _UNDER_STRIKE_WASH, "color": theme.TEAL}
        )

    table = dash_table.DataTable(
        id="rate-heatmap-table",
        data=data,
        columns=_heatmap_columns(),
        merge_duplicate_headers=True,
        sort_action="none",
        # Full alignment with the main chain: identical cell / header / hover chrome.
        style_cell=CHAIN_STYLE_CELL,
        style_header=CHAIN_STYLE_HEADER,
        css=CHAIN_TABLE_CSS,
        style_data_conditional=style_data_conditional,
    )
    return dbc.Card(
        dbc.CardBody(html.Div(table, style={"overflowX": "auto"})),
        className="onechain-chain-card",
    )
