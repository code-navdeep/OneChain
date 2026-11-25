"""Dense option-chain table model and rendering helpers."""

from __future__ import annotations

from collections import defaultdict

from dash import dash_table

from analytics.hover_snapshot import strike_key
from services.view_data_service import ViewDataService
from ui import theme
from ui.formatters import format_strike, interp_hover_line


# Chain-table palette (Midnight Quant dark). The CONDITIONS in build_conditional_styles are
# unchanged — only the colours, with RESTRAINT: low-alpha washes + bright/legible text + hairline
# borders, never the full-saturation cell fills of the failed attempt.
_BASE_BG = "#0e1524"          # base cell bg (cells no conditional covers)
_ITM_BG = "#18233c"           # ITM wash — slightly lighter
_ITM_TXT = "#e8edf7"          # bright
_OTM_BG = "#0f1626"           # OTM wash — slightly darker
_OTM_TXT = "#9fb0cc"          # dimmer than ITM but still clearly legible
_UNDER_BG = "#0a0f1e"         # underlying (spot) row band
_STRIKE_WASH = "rgba(245,181,68,0.16)"   # amber wash on the strike column
_STRIKE_TXT = "#f5b544"
_UNDER_STRIKE_WASH = "rgba(34,211,168,0.16)"  # teal wash on the spot row's strike cell
_RANK1_WASH = "rgba(245,181,68,0.22)"    # 1st — amber wash + amber text
_RANK1_TXT = "#f5b544"
_RANK2_WASH = "rgba(34,211,168,0.18)"    # 2nd — teal wash + teal text
_RANK2_TXT = "#2dd4a0"

# Tier-2 Layer-1 rate marker fills. Deliberately white / sky-blue — NOT red/green — so they
# can't merge with the adjacent red/green OI-change & BS-valuation borders. Down only appears on OI/IV
# markers (Volume is up-only). Applied natively as a style_data_conditional background.
_MARKER_UP = "#ffffff"     # crossed the upper threshold (build / spike / surge)
_MARKER_DOWN = "#38bdf8"   # crossed the lower threshold (unwind / crush)


# The rate marker columns are included in the side's ITM/OTM wash so an EMPTY marker cell
# adopts the same background as its neighbours and the thin column is invisible until a rule fires (the
# native marker background then overrides the wash).
CALL_COLUMNS_NO_GREEKS = [
    "call_vex", "call_iv", "call_iv_marker", "call_oi_change", "call_oi", "call_oi_marker",
    "call_volume", "call_volume_marker", "call_last_price",
]
PUT_COLUMNS_NO_GREEKS = [
    "put_last_price", "put_volume", "put_volume_marker", "put_oi", "put_oi_marker",
    "put_oi_change", "put_iv", "put_iv_marker", "put_vex",
]
CALL_COLUMNS_GREEKS = ["call_vega", "call_theta", "call_gamma", "call_delta"]
PUT_COLUMNS_GREEKS = ["put_delta", "put_gamma", "put_theta", "put_vega"]
TROPHY_ICONS = ["🥇", "🥈", "🥉"]


def format_lot_number(lot_value: float) -> str:
    is_negative = lot_value < 0
    abs_lot_value = abs(lot_value)
    if abs_lot_value >= 10000000:
        result = f"{abs_lot_value / 10000000:.1f}Cr"
    elif abs_lot_value >= 100000:
        result = f"{abs_lot_value / 100000:.1f}L"
    elif abs_lot_value >= 1000:
        result = f"{abs_lot_value / 1000:.1f}K"
    else:
        result = f"{abs_lot_value:,.0f}"
    return f"-{result}" if is_negative else result


def format_indian_number(number: float) -> str:
    is_negative = number < 0
    abs_number = abs(number)
    if abs_number >= 10000000:
        result = f"{abs_number / 10000000:.1f}Cr"
    elif abs_number >= 100000:
        result = f"{abs_number / 100000:.1f}L"
    elif abs_number >= 1000:
        result = f"{abs_number / 1000:.1f}K"
    else:
        result = f"{abs_number:,.0f}"
    return f"-{result}" if is_negative else result


def extract_numeric_value(value_str) -> float:
    if not value_str or value_str == "-" or value_str == "":
        return 0.0
    str_val = str(value_str).strip()
    if "\n" in str_val:
        str_val = str_val.split("\n")[0]
    str_val = str_val.replace("₹", "").replace("%", "").replace("(", "").replace(")", "").strip()
    if not str_val or str_val in {"-", "N/A", "nan"}:
        return 0.0
    if str_val.endswith("Cr"):
        return float(str_val[:-2]) * 10000000
    if str_val.endswith("L"):
        return float(str_val[:-1]) * 100000
    if str_val.endswith("K"):
        return float(str_val[:-1]) * 1000
    return float(str_val.replace(",", ""))


def extract_strike_value(strike_display) -> float:
    if isinstance(strike_display, (int, float)):
        return float(strike_display)
    if isinstance(strike_display, str):
        first_line = strike_display.split("\n")[0].strip().replace("₹", "")
        try:
            return float(first_line)
        except ValueError:
            return 0.0
    return 0.0


def _collect_aggregate_expiries(report: dict) -> list[str]:
    expiries: list[str] = []
    for pair_key in report.get("aggregate_member_pairs", []) or []:
        parts = str(pair_key).split("|", 2)
        if len(parts) != 3:
            continue
        expiry = parts[2].strip()
        if expiry:
            expiries.append(expiry)
    return list(dict.fromkeys(expiries))


def _create_bs_price_hover_info(row: dict) -> str:
    bs_price = row.get("bs_price")
    return f"{float(bs_price):,.2f}" if bs_price not in (None, "") else ""


def _create_pain_point_hover_info(row: dict) -> str:
    pain_point = row.get("pain_point")
    return f"{float(pain_point):,.2f}" if pain_point not in (None, "") else ""


def _format_gex_number(gex_value: float) -> str:
    if not gex_value:
        return "-"
    if abs(gex_value) >= 10000000:
        return f"{gex_value / 10000000:.2f}Cr"
    if abs(gex_value) >= 100000:
        return f"{gex_value / 100000:.2f}L"
    if abs(gex_value) >= 1000:
        return f"{gex_value / 1000:.2f}K"
    return f"{gex_value:.2f}"


# The 6 Tier-2 rate marker columns (Layer 1): ultra-thin (5px) plain spacer columns whose colour is a
# native style_data_conditional background (per-window OI/IV/Volume rate crossings, one solid colour per
# cell = the shortest crossed window). Empty by default → they inherit the ITM/OTM wash and are invisible
# until a rule fires. (They were markdown-gradient cells, but Dash strips inline style from markdown so
# nothing rendered — hence the switch to native cell colouring, like the Surface view.)
RATE_MARKER_COLUMN_IDS = (
    "call_iv_marker", "call_oi_marker", "call_volume_marker",
    "put_volume_marker", "put_oi_marker", "put_iv_marker",
)
# (side, rule) → marker column id, for mapping crossings to marker cells in build_conditional_styles.
_MARKER_COLS = {
    ("CE", "iv"): "call_iv_marker", ("CE", "oi"): "call_oi_marker", ("CE", "vol"): "call_volume_marker",
    ("PE", "iv"): "put_iv_marker", ("PE", "oi"): "put_oi_marker", ("PE", "vol"): "put_volume_marker",
}


def _marker_column(cid: str) -> dict:
    return {"name": "", "id": cid, "type": "text"}


def define_table_columns(show_greeks: bool) -> list[dict]:
    all_columns = [
        {"name": "GEX", "id": "strike_gex", "type": "text"},
        {"name": "PCR", "id": "strike_pcr", "type": "text"},
        {"name": "VEX", "id": "call_vex", "type": "text"},
        {"name": "Vega", "id": "call_vega", "type": "text"},
        {"name": "Theta", "id": "call_theta", "type": "text"},
        {"name": "Gamma", "id": "call_gamma", "type": "text"},
        {"name": "Delta", "id": "call_delta", "type": "text"},
        {"name": "IV", "id": "call_iv", "type": "text"},
        _marker_column("call_iv_marker"),
        {"name": "OI Chg", "id": "call_oi_change", "type": "text"},
        {"name": "OI", "id": "call_oi", "type": "text"},
        _marker_column("call_oi_marker"),
        {"name": "Volume", "id": "call_volume", "type": "text"},
        _marker_column("call_volume_marker"),
        {"name": "LTP", "id": "call_last_price", "type": "numeric"},
        {"name": "STRIKE", "id": "strike", "type": "text"},
        {"name": "LTP", "id": "put_last_price", "type": "numeric"},
        # Put markers sit to the LEFT of their cell (call markers to the right) → both face the strike
        # column, mirror-symmetric.
        _marker_column("put_volume_marker"),
        {"name": "Volume", "id": "put_volume", "type": "text"},
        _marker_column("put_oi_marker"),
        {"name": "OI", "id": "put_oi", "type": "text"},
        {"name": "OI Chg", "id": "put_oi_change", "type": "text"},
        _marker_column("put_iv_marker"),
        {"name": "IV", "id": "put_iv", "type": "text"},
        {"name": "Delta", "id": "put_delta", "type": "text"},
        {"name": "Gamma", "id": "put_gamma", "type": "text"},
        {"name": "Theta", "id": "put_theta", "type": "text"},
        {"name": "Vega", "id": "put_vega", "type": "text"},
        {"name": "VEX", "id": "put_vex", "type": "text"},
        {"name": "Straddle", "id": "strike_straddle", "type": "text"},
    ]
    if show_greeks:
        # Greeks view drops the LTP columns (BS-valuation borders live on LTP, so they're
        # naturally absent here) — a bare greeks grid, strikes in the middle. The rate markers are
        # absent here too (they hang off OI/IV/Volume, which the greeks view swaps out — decided).
        included_columns = [
            "call_vega", "call_theta", "call_gamma", "call_delta",
            "strike",
            "put_delta", "put_gamma", "put_theta", "put_vega",
        ]
    else:
        included_columns = [
            "strike_gex", "strike_pcr",
            "call_vex", "call_iv", "call_iv_marker", "call_oi_change", "call_oi", "call_oi_marker",
            "call_volume", "call_volume_marker", "call_last_price",
            "strike",
            "put_last_price", "put_volume", "put_volume_marker", "put_oi", "put_oi_marker",
            "put_oi_change", "put_iv", "put_iv_marker", "put_vex",
            "strike_straddle",
        ]
    return [column for column in all_columns if column["id"] in included_columns]


def build_table_rows(report: dict, lot_size: int) -> tuple[list[dict], list[dict], list[dict]]:
    rows = [dict(row) for row in report.get("rows", [])]
    underlying_price = float(report.get("underlying_price", 0) or 0)
    call_rows = [row for row in rows if row.get("option_type") == "CE"]
    put_rows = [row for row in rows if row.get("option_type") == "PE"]
    all_strikes = sorted({float(row.get("strike", 0) or 0) for row in rows})
    # Per-strike GEX, derived from the stored gamma_exposure on the rows (works for live
    # and aggregate alike; no float-key JSON round-trip from the store).
    gex_by_strike: dict[float, float] = defaultdict(float)
    for gex_row in rows:
        gex_by_strike[float(gex_row.get("strike", 0) or 0)] += float(gex_row.get("gamma_exposure", 0) or 0)

    def format_lot_based_number(lot_value):
        # Counts arrive already in lots — format with K/L/Cr, no division.
        if lot_value in (None, "") or float(lot_value) == 0:
            return "-"
        lot_num = float(lot_value)
        abs_lot_num = abs(lot_num)
        sign = "-" if lot_num < 0 else ""
        if abs_lot_num >= 10000000:
            return f"{sign}{abs_lot_num / 10000000:.1f}Cr"
        if abs_lot_num >= 100000:
            return f"{sign}{abs_lot_num / 100000:.1f}L"
        if abs_lot_num >= 1000:
            return f"{sign}{abs_lot_num / 1000:.1f}K"
        return f"{lot_num:.1f}"

    table_data: list[dict] = []
    for strike in all_strikes:
        strike_calls = [row for row in call_rows if float(row.get("strike", 0) or 0) == strike]
        strike_puts = [row for row in put_rows if float(row.get("strike", 0) or 0) == strike]
        is_itm_call = strike < underlying_price
        is_itm_put = strike > underlying_price

        call_price = float(strike_calls[0].get("last_price", 0) or 0) if strike_calls else 0.0
        put_price = float(strike_puts[0].get("last_price", 0) or 0) if strike_puts else 0.0
        straddle_price = call_price + put_price
        call_bs_price = float(strike_calls[0].get("bs_price", 0) or 0) if strike_calls else 0.0
        put_bs_price = float(strike_puts[0].get("bs_price", 0) or 0) if strike_puts else 0.0
        total_bs_straddle = call_bs_price + put_bs_price

        straddle_indicator = ""
        if straddle_price > 0 and total_bs_straddle > 0:
            pct_diff = ((straddle_price - total_bs_straddle) / total_bs_straddle) * 100
            if pct_diff >= 20:
                straddle_indicator = " 🔺🔺"
            elif pct_diff >= 10:
                straddle_indicator = " 🔺"
            elif pct_diff <= -20:
                straddle_indicator = " 🔹🔹"
            elif pct_diff <= -10:
                straddle_indicator = " 🔹"

        if straddle_price > 0:
            price_formatted = f"₹{straddle_price:,.0f}" if straddle_price >= 1000 else f"₹{straddle_price:.0f}"
            strike_display = f"{format_strike(strike)}\n({price_formatted}){straddle_indicator}"
        else:
            strike_display = format_strike(strike)

        row_data = {
            "strike": strike_display,
            "strike_gex": _format_gex_number(float(gex_by_strike.get(strike, 0) or 0)),
        }

        strike_hover_parts = []
        if strike_calls:
            call_pain_hover = _create_pain_point_hover_info(strike_calls[0])
            if call_pain_hover:
                strike_hover_parts.append(f"🔴 {call_pain_hover}")
        if strike_puts:
            put_pain_hover = _create_pain_point_hover_info(strike_puts[0])
            if put_pain_hover:
                strike_hover_parts.append(f"🟢 {put_pain_hover}")
        if total_bs_straddle > 0:
            strike_hover_parts.append(f"(₹{total_bs_straddle:,.2f})")
        row_data["strike_hover"] = "\n".join(strike_hover_parts) if strike_hover_parts else ""

        call_oi = float(strike_calls[0].get("oi_lots", 0) or 0) if strike_calls else 0.0
        put_oi = float(strike_puts[0].get("oi_lots", 0) or 0) if strike_puts else 0.0
        call_volume = float(strike_calls[0].get("volume_lots", 0) or 0) if strike_calls else 0.0
        put_volume = float(strike_puts[0].get("volume_lots", 0) or 0) if strike_puts else 0.0
        pcr_oi = put_oi / call_oi if call_oi > 0 else 0.0
        pcr_volume = put_volume / call_volume if call_volume > 0 else 0.0
        row_data["strike_pcr"] = "-" if pcr_oi == 0 and pcr_volume == 0 else f"{pcr_oi:.2f}\n({pcr_volume:.2f})"

        call_iv = float(strike_calls[0].get("iv", 0) or 0) if strike_calls else 0.0
        put_iv = float(strike_puts[0].get("iv", 0) or 0) if strike_puts else 0.0
        straddle_oi = call_oi + put_oi
        straddle_iv = ((call_iv + put_iv) / 2.0) if call_iv > 0 and put_iv > 0 else 0.0
        call_previous_oi = float(strike_calls[0].get("previous_oi_lots", 0) or 0) if strike_calls else 0.0
        put_previous_oi = float(strike_puts[0].get("previous_oi_lots", 0) or 0) if strike_puts else 0.0
        straddle_previous_oi = call_previous_oi + put_previous_oi
        row_data["strike_straddle"] = "-" if straddle_oi == 0 else f"{format_lot_based_number(straddle_oi)}\n({straddle_iv:.2f})"
        row_data["straddle_previous_oi"] = straddle_previous_oi

        if strike_calls:
            call_row = strike_calls[0]
            ltp_display = f"{float(call_row.get('last_price', 0) or 0):.2f}"
            if call_row.get("risk_reward_ratio") not in (None, ""):
                ltp_display = f"{ltp_display}\n({float(call_row['risk_reward_ratio']):.2f})"
            call_vol_oi_ratio = (call_volume / call_oi) if call_oi > 0 else 0.0
            row_data.update(
                {
                    "call_last_price": ltp_display,
                    "call_hover": _create_bs_price_hover_info(call_row),
                    "call_oi": format_lot_based_number(call_row.get("oi_lots", 0)),
                    "call_previous_oi": call_row.get("previous_oi_lots", 0),
                    "call_oi_change": format_lot_based_number(call_row.get("oi_change_lots", 0)),
                    "call_volume": format_lot_based_number(call_row.get("volume_lots", 0)),
                    "call_volume_hover": f"Vol/OI: {call_vol_oi_ratio:.2f}" if call_vol_oi_ratio > 0 else None,
                    "call_iv": f"{float(call_row.get('iv', 0) or 0):.2f}" if float(call_row.get("iv", 0) or 0) > 0 else "-",
                    "call_delta": f"{float(call_row.get('delta', 0) or 0):.4f}" if call_row.get("delta") not in (None, "") else "-",
                    "call_gamma": f"{float(call_row.get('gamma', 0) or 0):.4f}" if call_row.get("gamma") not in (None, "") else "-",
                    "call_theta": f"{float(call_row.get('theta', 0) or 0):.4f}" if call_row.get("theta") not in (None, "") else "-",
                    "call_vega": f"{float(call_row.get('vega', 0) or 0):.4f}" if call_row.get("vega") not in (None, "") else "-",
                    "call_vex": format_indian_number(float(call_row.get("volatility_exposure", 0) or 0)) if float(call_row.get("volatility_exposure", 0) or 0) != 0 else "-",
                    "call_is_itm": is_itm_call,
                }
            )
        else:
            row_data.update(
                {
                    "call_last_price": "-",
                    "call_hover": "",
                    "call_oi": "-",
                    "call_oi_change": "-",
                    "call_volume": "-",
                    "call_iv": "-",
                    "call_delta": "-",
                    "call_gamma": "-",
                    "call_theta": "-",
                    "call_vega": "-",
                    "call_vex": "-",
                    "call_is_itm": is_itm_call,
                }
            )

        if strike_puts:
            put_row = strike_puts[0]
            ltp_display = f"{float(put_row.get('last_price', 0) or 0):.2f}"
            if put_row.get("risk_reward_ratio") not in (None, ""):
                ltp_display = f"{ltp_display}\n({float(put_row['risk_reward_ratio']):.2f})"
            put_vol_oi_ratio = (put_volume / put_oi) if put_oi > 0 else 0.0
            row_data.update(
                {
                    "put_last_price": ltp_display,
                    "put_hover": _create_bs_price_hover_info(put_row),
                    "put_oi": format_lot_based_number(put_row.get("oi_lots", 0)),
                    "put_previous_oi": put_row.get("previous_oi_lots", 0),
                    "put_oi_change": format_lot_based_number(put_row.get("oi_change_lots", 0)),
                    "put_volume": format_lot_based_number(put_row.get("volume_lots", 0)),
                    "put_volume_hover": f"Vol/OI: {put_vol_oi_ratio:.2f}" if put_vol_oi_ratio > 0 else None,
                    "put_iv": f"{float(put_row.get('iv', 0) or 0):.2f}" if float(put_row.get("iv", 0) or 0) > 0 else "-",
                    "put_delta": f"{float(put_row.get('delta', 0) or 0):.4f}" if put_row.get("delta") not in (None, "") else "-",
                    "put_gamma": f"{float(put_row.get('gamma', 0) or 0):.4f}" if put_row.get("gamma") not in (None, "") else "-",
                    "put_theta": f"{float(put_row.get('theta', 0) or 0):.4f}" if put_row.get("theta") not in (None, "") else "-",
                    "put_vega": f"{float(put_row.get('vega', 0) or 0):.4f}" if put_row.get("vega") not in (None, "") else "-",
                    "put_vex": format_indian_number(float(put_row.get("volatility_exposure", 0) or 0)) if float(put_row.get("volatility_exposure", 0) or 0) != 0 else "-",
                    "put_is_itm": is_itm_put,
                }
            )
        else:
            row_data.update(
                {
                    "put_last_price": "-",
                    "put_hover": "",
                    "put_oi": "-",
                    "put_oi_change": "-",
                    "put_volume": "-",
                    "put_iv": "-",
                    "put_delta": "-",
                    "put_gamma": "-",
                    "put_theta": "-",
                    "put_vega": "-",
                    "put_vex": "-",
                    "put_is_itm": is_itm_put,
                }
            )
        table_data.append(row_data)

    return table_data, call_rows, put_rows


def insert_underlying_row(table_data: list[dict], underlying_price: float) -> list[dict]:
    final_table_data: list[dict] = []
    underlying_row_inserted = False
    for index, row in enumerate(table_data):
        current_strike = extract_strike_value(row["strike"])
        prev_strike = extract_strike_value(table_data[index - 1]["strike"]) if index > 0 else 0
        if not underlying_row_inserted and current_strike >= underlying_price and (index == 0 or prev_strike < underlying_price):
            underlying_row = {
                "strike": f"₹{underlying_price:.2f}",
                "is_underlying_row": True,
                **{col: "" for col in row.keys() if col not in {"strike", "is_underlying_row"}},
            }
            final_table_data.append(underlying_row)
            underlying_row_inserted = True
        final_table_data.append(row)
    return final_table_data


def calculate_percentages(final_table_data: list[dict]) -> dict[str, float]:
    data_rows = [row for row in final_table_data if not row.get("is_underlying_row", False)]
    column_mappings = [
        ("call_oi", "call_oi"),
        ("put_oi", "put_oi"),
        ("call_oi_change", "call_oi_change"),
        ("put_oi_change", "put_oi_change"),
        ("call_volume", "call_volume"),
        ("put_volume", "put_volume"),
        ("strike_gex", "gex"),
        ("call_vex", "call_vex"),
        ("put_vex", "put_vex"),
        ("strike_straddle", "straddle_oi"),
    ]
    max_values: dict[str, float] = {}
    for column_id, column_type in column_mappings:
        values = []
        for row in data_rows:
            if column_id in row and row[column_id] is not None:
                numeric_value = extract_numeric_value(row[column_id])
                if numeric_value != 0:
                    values.append(numeric_value if column_type == "gex" else abs(numeric_value))
        max_values[column_type] = max(abs(value) for value in values) if (values and column_type == "gex") else (max(values) if values else 1.0)

    def calculate_percentage_with_sign(original_value_str, column_type):
        numeric_value = extract_numeric_value(original_value_str)
        if numeric_value == 0 or column_type not in max_values:
            return ""
        max_value = max_values[column_type]
        if max_value == 0:
            return ""
        if column_type == "gex":
            gex_values = [extract_numeric_value(row["strike_gex"]) for row in data_rows if row.get("strike_gex") not in (None, "-", "")]
            highest_positive = max([value for value in gex_values if value > 0], default=1)
            percentage = (numeric_value / highest_positive) * 100 if highest_positive else 0
        else:
            percentage = (numeric_value / max_value) * 100
        return f"({percentage:.0f}%)"

    for row in final_table_data:
        if row.get("is_underlying_row", False):
            continue
        for column_id, column_type in column_mappings:
            if column_id in row and row[column_id] not in ("-", None):
                percentage_str = calculate_percentage_with_sign(row[column_id], column_type)
                if not percentage_str:
                    continue
                row[f"_{column_id}_percentage"] = float(percentage_str.replace("(", "").replace("%)", ""))
                if column_id == "strike_straddle":
                    lines = row[column_id].split("\n")
                    if len(lines) >= 2:
                        lines[0] = f"{lines[0]} {percentage_str}"
                        row[column_id] = "\n".join(lines)
                else:
                    row[column_id] = f"{row[column_id]}\n{percentage_str}"
    return max_values


def _yesterday_iv_hover(avg: object, atm: object) -> str | None:
    """Totals-row yesterday IV hover (A1) — ``Yesterday: {avg} | {atm}``, or **avg-only** when the
    bracketing-ATM IV is unreachable (``None``): the ATM half is dropped, never fabricated. ``None`` when
    there's no usable average (unchanged from the pre-A1 behaviour)."""
    avg_value = float(avg) if avg not in (None, "") else 0.0
    if avg_value <= 0:
        return None
    if atm is None:
        return f"Yesterday: {avg_value:.2f}"
    return f"Yesterday: {avg_value:.2f} | {float(atm):.2f}"


def populate_underlying_row_totals(
    final_table_data: list[dict],
    report: dict,
    yesterday_totals: dict | None,
    max_values: dict[str, float] | None = None,
) -> None:
    lot_size = int(report.get("lot_size", 1) or 1)
    data_rows = [row for row in final_table_data if not row.get("is_underlying_row", False)]
    # Underlying-row totals are summed from the full-chain report rows (already lot-scaled
    # counts + stored exposures), not by re-parsing the abbreviated display strings ("1.2K")
    # — so they are exact and never shift with the strike-range display window.
    chain_rows = report.get("rows", []) or []

    def _sum_raw(option_type: str, field: str) -> float:
        return sum(
            float(chain_row.get(field, 0) or 0)
            for chain_row in chain_rows
            if chain_row.get("option_type") == option_type
        )

    totals = {
        "call_oi_total": _sum_raw("CE", "oi_lots"),
        "put_oi_total": _sum_raw("PE", "oi_lots"),
        "call_oi_change_total": _sum_raw("CE", "oi_change_lots"),
        "put_oi_change_total": _sum_raw("PE", "oi_change_lots"),
        "call_volume_total": _sum_raw("CE", "volume_lots"),
        "put_volume_total": _sum_raw("PE", "volume_lots"),
        "call_vex_total": _sum_raw("CE", "volatility_exposure"),
        "put_vex_total": _sum_raw("PE", "volatility_exposure"),
        "gex_total": sum(float(chain_row.get("gamma_exposure", 0) or 0) for chain_row in chain_rows),
    }

    pcr_oi_total = totals["put_oi_total"] / totals["call_oi_total"] if totals["call_oi_total"] > 0 else 0
    pcr_volume_total = totals["put_volume_total"] / totals["call_volume_total"] if totals["call_volume_total"] > 0 else 0
    oi_diff = totals["put_oi_total"] - totals["call_oi_total"]
    oi_diff_display = format_indian_number(abs(oi_diff))
    if oi_diff < 0:
        oi_diff_display = f"-{oi_diff_display}"
    underlying_price = float(report.get("underlying_price", 0) or 0)

    for row in final_table_data:
        if not row.get("is_underlying_row"):
            continue
        row.update(
            {
                "strike": f"₹{underlying_price:.2f}\n({oi_diff_display})",
                "strike_gex": format_indian_number(totals["gex_total"]) if totals["gex_total"] else "-",
                "strike_pcr": f"{pcr_oi_total:.2f}\n({pcr_volume_total:.2f})" if pcr_oi_total > 0 or pcr_volume_total > 0 else "-",
                "call_oi": format_lot_number(totals["call_oi_total"]) if totals["call_oi_total"] else "-",
                "put_oi": format_lot_number(totals["put_oi_total"]) if totals["put_oi_total"] else "-",
                "call_oi_change": format_lot_number(totals["call_oi_change_total"]) if totals["call_oi_change_total"] else "-",
                "put_oi_change": format_lot_number(totals["put_oi_change_total"]) if totals["put_oi_change_total"] else "-",
                "call_volume": format_lot_number(totals["call_volume_total"]) if totals["call_volume_total"] else "-",
                "put_volume": format_lot_number(totals["put_volume_total"]) if totals["put_volume_total"] else "-",
                "call_vex": format_indian_number(totals["call_vex_total"]) if totals["call_vex_total"] else "-",
                "put_vex": format_indian_number(totals["put_vex_total"]) if totals["put_vex_total"] else "-",
            }
        )
        row["call_volume_hover"] = f"Vol/OI: {(totals['call_volume_total'] / totals['call_oi_total']):.2f}" if totals["call_oi_total"] > 0 else None
        row["put_volume_hover"] = f"Vol/OI: {(totals['put_volume_total'] / totals['put_oi_total']):.2f}" if totals["put_oi_total"] > 0 else None
        if yesterday_totals:
            row["call_oi_hover"] = f"Yesterday: {format_lot_number(float(yesterday_totals['total_call_oi']))}"
            row["put_oi_hover"] = f"Yesterday: {format_lot_number(float(yesterday_totals['total_put_oi']))}"
            row["strike_pcr_hover"] = f"Yesterday: {float(yesterday_totals['pcr_oi']):.2f} ({float(yesterday_totals['pcr_volume']):.2f})"
            row["call_iv_hover"] = _yesterday_iv_hover(yesterday_totals["avg_call_iv"], yesterday_totals.get("atm_call_iv"))
            row["put_iv_hover"] = _yesterday_iv_hover(yesterday_totals["avg_put_iv"], yesterday_totals.get("atm_put_iv"))
            row["call_oi_change_hover"] = f"Yesterday: {format_lot_number(float(yesterday_totals['total_call_oi_change']))}"
            row["put_oi_change_hover"] = f"Yesterday: {format_lot_number(float(yesterday_totals['total_put_oi_change']))}"
            row["strike_gex_hover"] = f"Yesterday: {format_indian_number(float(yesterday_totals['total_gex']))}"
            yesterday_oi_diff = float(yesterday_totals["total_put_oi"]) - float(yesterday_totals["total_call_oi"])
            yesterday_oi_diff_display = format_indian_number(abs(yesterday_oi_diff))
            if yesterday_oi_diff < 0:
                yesterday_oi_diff_display = f"-{yesterday_oi_diff_display}"
            # A single pipe-separated line of bare numbers — OI diff, then full-range skew, then
            # ATM skew (matching the live cell's line-3 bracket order). dash_table's per-cell string
            # tooltip does NOT honor embedded "\n" as a real line break (confirmed: labeled multi-line
            # text just word-wrapped at the tooltip's fixed width, splitting labels mid-word), so this
            # sticks to one line instead of fighting that constraint.
            strike_hover_values = [yesterday_oi_diff_display]
            if float(yesterday_totals["avg_call_iv"]) > 0 and float(yesterday_totals["avg_put_iv"]) > 0:
                window_skew_y = float(yesterday_totals["avg_put_iv"]) - float(yesterday_totals["avg_call_iv"])
                strike_hover_values.append(f"{window_skew_y:.2f}")
            yesterday_atm_gap = yesterday_totals.get("atm_gap")
            if yesterday_atm_gap is not None:
                strike_hover_values.append(f"{float(yesterday_atm_gap):.2f}")
            row["strike_hover"] = "  |  ".join(strike_hover_values)
        break

    underlying_row = next((row for row in final_table_data if row.get("is_underlying_row")), None)
    if underlying_row is None:
        return

    call_iv_values = []
    put_iv_values = []
    for row in data_rows:
        if row.get("call_iv") not in ("-", ""):
            call_iv_values.append(float(str(row["call_iv"]).split("\n")[0]))
        if row.get("put_iv") not in ("-", ""):
            put_iv_values.append(float(str(row["put_iv"]).split("\n")[0]))
    avg_call_iv = (sum(call_iv_values) / len(call_iv_values)) if call_iv_values else None
    avg_put_iv = (sum(put_iv_values) / len(put_iv_values)) if put_iv_values else None

    # Bracketing-ATM IV per side (the avg of the two strikes straddling spot) — already computed
    # at fetch and carried on report["skew"]. Shown on a SECOND, bracketed line under the existing
    # displayed-range average (which is left untouched). "(-)" when a leg is unreachable.
    skew = report.get("skew") or {}
    atm_call_iv = skew.get("atm_call_iv")
    atm_put_iv = skew.get("atm_put_iv")
    atm_gap = skew.get("atm_gap")
    atm_call_line = f"({atm_call_iv:.2f})" if atm_call_iv is not None else "(-)"
    atm_put_line = f"({atm_put_iv:.2f})" if atm_put_iv is not None else "(-)"
    underlying_row["call_iv"] = (f"{avg_call_iv:.2f}" if avg_call_iv is not None else "-") + f"\n{atm_call_line}"
    underlying_row["put_iv"] = (f"{avg_put_iv:.2f}" if avg_put_iv is not None else "-") + f"\n{atm_put_line}"

    # Line 3 of the underlying cell: the existing displayed-range IV skew bracket, then the
    # bracketing-ATM gap (atm_put − atm_call, same put−call sign) in a second bracket beside it.
    window_skew = f"({(avg_put_iv - avg_call_iv):.2f})" if (avg_call_iv is not None and avg_put_iv is not None) else "(-)"
    atm_gap_bracket = f"({atm_gap:.2f})" if atm_gap is not None else "(-)"
    # No space between the two brackets — they're already visually distinct, and dropping the gap
    # helps the cell stay at 3 lines (not wrap to 4) on narrow screens. See also the smaller font
    # for the underlying strike cell in build_chain_table_style.
    underlying_row["strike"] = f"{underlying_row['strike']}\n{window_skew}{atm_gap_bracket}"

    if not max_values:
        return

    totals_columns_mapping = [
        ("strike_gex", "gex", totals["gex_total"]),
        ("call_oi", "call_oi", totals["call_oi_total"]),
        ("put_oi", "put_oi", totals["put_oi_total"]),
        ("call_oi_change", "call_oi_change", totals["call_oi_change_total"]),
        ("put_oi_change", "put_oi_change", totals["put_oi_change_total"]),
        ("call_volume", "call_volume", totals["call_volume_total"]),
        ("put_volume", "put_volume", totals["put_volume_total"]),
        ("call_vex", "call_vex", totals["call_vex_total"]),
        ("put_vex", "put_vex", totals["put_vex_total"]),
    ]
    for column_id, column_type, total_value in totals_columns_mapping:
        max_value = max_values.get(column_type, 0)
        if not total_value or not max_value:
            continue
        if column_type == "gex":
            gex_values = [
                extract_numeric_value(row["strike_gex"])
                for row in final_table_data
                if not row.get("is_underlying_row") and row.get("strike_gex") not in (None, "-", "")
            ]
            highest_positive = max([value for value in gex_values if value > 0], default=1)
            percentage = (total_value / highest_positive) * 100 if highest_positive else 0
        else:
            percentage = (total_value / max_value) * 100
        current_display = underlying_row.get(column_id)
        if current_display and current_display != "-":
            underlying_row[column_id] = f"{current_display}\n({percentage:.0f}%)"


def apply_trophy_systems(final_table_data: list[dict], oi_change_trophy_data: dict | None) -> None:
    for previous_oi_col, oi_column in [("call_previous_oi", "call_oi"), ("put_previous_oi", "put_oi")]:
        column_values = []
        for index, row in enumerate(final_table_data):
            if row.get("is_underlying_row"):
                continue
            value = row.get(previous_oi_col, 0)
            numeric_value = float(value) if value not in [0, "-", "", None] else 0.0
            if numeric_value > 0:
                column_values.append((index, numeric_value))
        column_values.sort(key=lambda item: item[1], reverse=True)
        for rank, (row_index, _) in enumerate(column_values[:3]):
            current_value = final_table_data[row_index].get(oi_column, "")
            if current_value and current_value != "-":
                final_table_data[row_index][oi_column] = f"{current_value} {TROPHY_ICONS[rank]}"

    straddle_values = []
    for index, row in enumerate(final_table_data):
        if row.get("is_underlying_row"):
            continue
        value = row.get("straddle_previous_oi", 0)
        numeric_value = float(value) if value not in [0, "-", "", None] else 0.0
        if numeric_value > 0:
            straddle_values.append((index, numeric_value))
    straddle_values.sort(key=lambda item: item[1], reverse=True)
    for rank, (row_index, _) in enumerate(straddle_values[:3]):
        current_value = final_table_data[row_index].get("strike_straddle", "")
        if current_value and current_value != "-":
            final_table_data[row_index]["strike_straddle"] = f"{current_value} {TROPHY_ICONS[rank]}"

    if not oi_change_trophy_data:
        return
    call_rankings = dict(oi_change_trophy_data.get("call_rankings", {}))
    put_rankings = dict(oi_change_trophy_data.get("put_rankings", {}))
    for row in final_table_data:
        if row.get("is_underlying_row"):
            continue
        strike = extract_strike_value(row.get("strike", ""))
        if strike in call_rankings and row.get("call_oi_change") not in ("", "-"):
            row["call_oi_change"] = f"{row['call_oi_change']} {call_rankings[strike]}"
        if strike in put_rankings and row.get("put_oi_change") not in ("", "-"):
            row["put_oi_change"] = f"{row['put_oi_change']} {put_rankings[strike]}"


def build_tooltips(final_table_data: list[dict]) -> list[dict]:
    tooltip_data = []
    hover_mappings = [
        ("call_hover", "call_last_price"),
        ("put_hover", "put_last_price"),
        ("strike_hover", "strike"),
        ("call_oi_hover", "call_oi"),
        ("put_oi_hover", "put_oi"),
        ("call_volume_hover", "call_volume"),
        ("put_volume_hover", "put_volume"),
        ("strike_pcr_hover", "strike_pcr"),
        ("call_iv_hover", "call_iv"),
        ("put_iv_hover", "put_iv"),
        ("call_oi_change_hover", "call_oi_change"),
        ("put_oi_change_hover", "put_oi_change"),
        ("strike_gex_hover", "strike_gex"),
        ("strike_straddle_hover", "strike_straddle"),
    ]
    for row in final_table_data:
        row_tooltips = {}
        for hover_key, column_id in hover_mappings:
            if row.get(hover_key):
                row_tooltips[column_id] = row[hover_key]
        tooltip_data.append(row_tooltips)
    return tooltip_data


def build_conditional_styles(
    final_table_data: list[dict],
    columns: list[dict],
    report: dict,
    show_greeks: bool,
    display_rows: list[dict] | None = None,
    rate_markers: dict | None = None,
) -> list[dict]:
    underlying_price = float(report.get("underlying_price", 0) or 0)
    style_data_conditional: list[dict] = []
    # Raw per-contract last_price/bs_price/oi_change_lots for the BS-valuation + OI-change borders.
    # Built from the *displayed* (windowed) rows when available — the styling only ever queries the
    # displayed strikes, so a full-chain lookup would be identical but needlessly larger.
    lookup_rows = display_rows if display_rows is not None else report.get("rows", [])
    bs_lookup: dict[tuple[float, str], dict] = {}
    for source_row in lookup_rows:
        bs_lookup[(float(source_row.get("strike", 0) or 0), str(source_row.get("option_type", "")))] = source_row

    for index, row in enumerate(final_table_data):
        if row.get("is_underlying_row"):
            for column in columns:
                if column["id"] == "strike":
                    style_data_conditional.append(
                        {
                            "if": {"row_index": index, "column_id": column["id"]},
                            "backgroundColor": _UNDER_STRIKE_WASH,
                            "color": theme.TEAL,
                            "fontWeight": "bold",
                            # 14px (down from 16) so the 3-line cell (price / OI-diff / IV-skew) keeps
                            # to 3 rows on narrow screens instead of wrapping line 3 into a 4th.
                            "fontSize": "14px",
                        }
                    )
                else:
                    style_data_conditional.append(
                        {
                            "if": {"row_index": index, "column_id": column["id"]},
                            "backgroundColor": _UNDER_BG,
                            "color": theme.INK,
                            "fontWeight": "bold",
                            "border": "none",
                        }
                    )
            continue

        current_strike = extract_strike_value(row["strike"])
        call_columns = CALL_COLUMNS_GREEKS if show_greeks else CALL_COLUMNS_NO_GREEKS
        put_columns = PUT_COLUMNS_GREEKS if show_greeks else PUT_COLUMNS_NO_GREEKS

        for column_id in call_columns:
            style_data_conditional.append(
                {
                    "if": {"row_index": index, "column_id": column_id},
                    "backgroundColor": _ITM_BG if current_strike < underlying_price else _OTM_BG,
                    "color": _ITM_TXT if current_strike < underlying_price else _OTM_TXT,
                }
            )
        for column_id in put_columns:
            style_data_conditional.append(
                {
                    "if": {"row_index": index, "column_id": column_id},
                    "backgroundColor": _ITM_BG if current_strike > underlying_price else _OTM_BG,
                    "color": _ITM_TXT if current_strike > underlying_price else _OTM_TXT,
                }
            )

        style_data_conditional.append(
            {
                "if": {"row_index": index, "column_id": "strike"},
                "backgroundColor": _STRIKE_WASH,
                "color": _STRIKE_TXT,
                "fontWeight": "bold",
                "textAlign": "center",
            }
        )
        style_data_conditional.append({"if": {"row_index": index, "column_id": "strike_gex"}, "backgroundColor": theme.TEAL_TINT})
        style_data_conditional.append({"if": {"row_index": index, "column_id": "strike_straddle"}, "backgroundColor": theme.VIOLET_TINT})

        # BS over/under-valuation border on the LTP cells: green when the traded price is
        # below Black-Scholes (undervalued), red when above (overvalued); 2px/4px by magnitude.
        for option_type, col_id in (("CE", "call_last_price"), ("PE", "put_last_price")):
            source_row = bs_lookup.get((current_strike, option_type))
            if not source_row:
                continue
            bs_price = source_row.get("bs_price")
            ltp = float(source_row.get("last_price", 0) or 0)
            if bs_price in (None, "") or float(bs_price) <= 0 or ltp <= 0:
                continue  # no honest BS price (rate/vol unset) → no border
            pct_diff = (ltp - float(bs_price)) / float(bs_price) * 100
            border = None
            if pct_diff <= -20:
                border = f"2px solid {theme.UP}"
            elif pct_diff <= -10:
                border = f"2px dashed {theme.UP}"
            elif pct_diff >= 20:
                border = f"2px solid {theme.DOWN}"
            elif pct_diff >= 10:
                border = f"2px dashed {theme.DOWN}"
            if border:
                style_data_conditional.append({"if": {"row_index": index, "column_id": col_id}, "border": border})

    max_pain_strike = float(report.get("max_pain_strike", 0) or 0)
    if max_pain_strike > 0:
        for index, row in enumerate(final_table_data):
            if extract_strike_value(row.get("strike", 0)) == max_pain_strike:
                style_data_conditional.append({"if": {"row_index": index, "column_id": "strike"}, "border": f"2px solid {theme.VIOLET}"})
                break

    ranking_columns = ["call_volume", "put_volume", "call_oi", "put_oi", "call_vex", "put_vex", "strike_straddle", "call_previous_oi", "put_previous_oi"]
    for column in ranking_columns:
        percentage_key = f"_{column}_percentage"
        column_values = [(index, row[percentage_key]) for index, row in enumerate(final_table_data) if not row.get("is_underlying_row") and percentage_key in row]
        column_values.sort(key=lambda item: item[1], reverse=True)
        for rank, (row_index, _) in enumerate(column_values[:3]):
            if rank == 0:
                style_data_conditional.append({"if": {"row_index": row_index, "column_id": column}, "backgroundColor": _RANK1_WASH, "color": _RANK1_TXT, "fontWeight": "bold"})
            elif rank == 1:
                style_data_conditional.append({"if": {"row_index": row_index, "column_id": column}, "backgroundColor": _RANK2_WASH, "color": _RANK2_TXT, "fontWeight": "bold"})
            elif rank == 2:
                style_data_conditional.append({"if": {"row_index": row_index, "column_id": column}, "border": f"2px dashed {theme.INFO}", "fontWeight": "bold"})

    # OI-change rank borders, by TODAY's live oi_change_lots (independent of the
    # yesterday-sourced trophy glyphs). Per side: most-positive → solid green, 2nd →
    # dashed green; most-negative → solid red, 2nd → dashed red. Sides with no cell of a
    # given sign (all positive / all negative / a single value) simply skip that border.
    oi_change_green = [f"2px solid {theme.UP}", f"2px dashed {theme.UP}"]
    oi_change_red = [f"2px solid {theme.DOWN}", f"2px dashed {theme.DOWN}"]
    for option_type, col_id in (("CE", "call_oi_change"), ("PE", "put_oi_change")):
        positives: list[tuple[int, float]] = []
        negatives: list[tuple[int, float]] = []
        for index, row in enumerate(final_table_data):
            if row.get("is_underlying_row"):
                continue
            source_row = bs_lookup.get((extract_strike_value(row["strike"]), option_type))
            if not source_row:
                continue
            value = float(source_row.get("oi_change_lots", 0) or 0)
            if value > 0:
                positives.append((index, value))
            elif value < 0:
                negatives.append((index, value))
        positives.sort(key=lambda item: item[1], reverse=True)
        negatives.sort(key=lambda item: item[1])
        for rank, (row_index, _) in enumerate(positives[:2]):
            style_data_conditional.append({"if": {"row_index": row_index, "column_id": col_id}, "border": oi_change_green[rank]})
        for rank, (row_index, _) in enumerate(negatives[:2]):
            style_data_conditional.append({"if": {"row_index": row_index, "column_id": col_id}, "border": oi_change_red[rank]})

    for index, row in enumerate(final_table_data):
        if row.get("is_underlying_row"):
            continue
        for column in ("call_oi_change", "put_oi_change", "strike_gex"):
            numeric_value = extract_numeric_value(row.get(column, 0))
            if numeric_value > 0:
                style_data_conditional.append({"if": {"row_index": index, "column_id": column}, "color": theme.UP, "fontWeight": "bold"})
            elif numeric_value < 0:
                style_data_conditional.append({"if": {"row_index": index, "column_id": column}, "color": theme.DOWN, "fontWeight": "bold"})

    # Tier-2 Layer-1 rate markers — paint each crossed marker cell one solid colour (the shortest
    # crossed window's state, resolved in ui/shell). Appended LAST so the marker background overrides the
    # ITM/OTM wash on that thin column. Non-greeks only (the marker columns don't exist in Greeks view).
    if rate_markers and not show_greeks:
        for index, row in enumerate(final_table_data):
            if row.get("is_underlying_row"):
                continue
            skey = round(extract_strike_value(row.get("strike", "")), 4)
            for (side, rule), col in _MARKER_COLS.items():
                state = rate_markers.get((skey, side, rule))
                if state in ("up", "down"):
                    style_data_conditional.append({
                        "if": {"row_index": index, "column_id": col},
                        "backgroundColor": _MARKER_UP if state == "up" else _MARKER_DOWN,
                    })
    return style_data_conditional


# Greeks-view spot-row legend: the underlying row's greek cells name the greek (only shown in the
# greeks view; in the normal view these columns are swapped out, so the labels are invisible there).
_GREEK_LABELS = {
    "call_delta": "Delta", "call_gamma": "Gamma", "call_theta": "Theta", "call_vega": "Vega",
    "put_delta": "Delta", "put_gamma": "Gamma", "put_theta": "Theta", "put_vega": "Vega",
}


def _label_underlying_greeks(table_data: list[dict]) -> None:
    for row in table_data:
        if row.get("is_underlying_row"):
            for col, label in _GREEK_LABELS.items():
                row[col] = label
            break


# --- Interpretor overlay (intraday OI-structure read) — two-line per-side + instrument labels on the
# central (underlying) row, and Conf/Hits tooltips on the ≤4 tracked-strike OI cells. Threaded in from
# ui/shell for the active LIVE pair only; absent (None) in aggregate / playback and in the Greeks /
# Surface views (whose LTP/OI/Straddle columns don't exist).
_INTERP_SHIFT_LABEL = {"bullish": "Bullish", "bearish": "Bearish"}
_INTERP_BIAS_LABEL = {"bullish": "🟢", "bearish": "🔴"}

# Instrument-level (combined straddle-cell) display translation of the interpretor's semantic
# shift_layer / bias_layer labels: only "Strong …" abbreviates to "S.…", Range-Bound reads
# "In-Range", the biases become green/red dots (two for the strong instrument bias), and the
# conflict/neither cases stay a word ("Undecided" / "No Clear Bias") or blank ("No Bias").
_INTERP_INSTRUMENT_SHIFT = {
    "Strong Bullish": "S.Bull",
    "Strong Bearish": "S.Bear",
    "Bullish": "Bullish",
    "Bearish": "Bearish",
    "Undecided": "Undecided",
    "Range-Bound": "In-Range",
}
_INTERP_INSTRUMENT_BIAS = {
    "Strong Bullish Bias": "🟢🟢",
    "Strong Bearish Bias": "🔴🔴",
    "Bullish Bias": "🟢",
    "Bearish Bias": "🔴",
    "No Clear Bias": "No Clear Bias",
    "No Bias": "",
}


def _interpretor_side_cell(side: dict) -> str:
    """Two fixed lines for a per-side central-row cell: the shift word (a 🚩 appended when a shift is
    pending) over the bias dot (🟢 bull / 🔴 bear). A missing shift or bias line is blank; a fully
    neutral side (no shift, no bias) collapses to an empty cell. A pending 🚩 on an otherwise-blank
    shift line renders as the flag alone."""
    shift = _INTERP_SHIFT_LABEL.get(side.get("shift"), "")
    if side.get("pending"):
        shift = f"{shift} 🚩".strip()
    bias = _INTERP_BIAS_LABEL.get(side.get("bias"), "")
    if not shift and not bias:
        return ""
    return f"{shift}\n{bias}"


def _apply_interpretor(final_table_data: list[dict], interpretor: dict | None) -> None:
    """Overlay the interpretor read onto the assembled table in place: fills the underlying row's
    call/put per-side cells + the straddle instrument-read cell (two lines each), and the
    ``Conf X% · Hits N`` tooltip on the (≤4) tracked-strike OI cells. No-op when there is no read."""
    if not interpretor:
        return
    call = interpretor.get("call") or {}
    put = interpretor.get("put") or {}
    instrument = interpretor.get("instrument") or {}
    stamp = interpretor.get("stamp_0920")
    stamp_present = stamp is not None
    for row in final_table_data:
        if row.get("is_underlying_row"):
            row["call_last_price"] = _interpretor_side_cell(call)
            row["put_last_price"] = _interpretor_side_cell(put)
            shift_layer = instrument.get("shift_layer", "")
            bias_layer = instrument.get("bias_layer", "")
            shift_line = _INTERP_INSTRUMENT_SHIFT.get(shift_layer, shift_layer)
            bias_line = _INTERP_INSTRUMENT_BIAS.get(bias_layer, bias_layer)
            row["strike_straddle"] = f"{shift_line}\n{bias_line}"
            # A3 — the per-side streak + 09:20 stamp line as a hover on the two interpretor cells (the
            # underlying row's call_hover/put_hover are unset otherwise); late-start drops the 09:20 half.
            row["call_hover"] = interp_hover_line(call, (stamp or {}).get("call"), stamp_present=stamp_present)
            row["put_hover"] = interp_hover_line(put, (stamp or {}).get("put"), stamp_present=stamp_present)
            break

    # Tracked-strike OI-cell tooltips: strike -> per-side level readout (up to 4 strikes total).
    tracked: dict[float, dict[str, dict]] = {}
    for side_key, side in (("call", call), ("put", put)):
        for level in side.get("levels", []) or []:
            strike = level.get("strike")
            if strike is not None:
                tracked.setdefault(float(strike), {})[side_key] = level
    if not tracked:
        return
    for row in final_table_data:
        if row.get("is_underlying_row"):
            continue
        entry = tracked.get(extract_strike_value(row.get("strike", "")))
        if not entry:
            continue
        if "call" in entry:
            row["call_oi_hover"] = f"Conf {float(entry['call']['confidence']):.0f}% · Hits {int(entry['call']['hits'])}"
        if "put" in entry:
            row["put_oi_hover"] = f"Conf {float(entry['put']['confidence']):.0f}% · Hits {int(entry['put']['hits'])}"


def _apply_hover(final_table_data: list[dict], hover: dict | None) -> None:
    """Overlay the A2 09:20 reference snapshot (``{"strikes": …, "totals": …}``) onto the assembled table
    in place. On the **data rows** it sets the per-strike 09:20 OI / IV / PCR / Straddle hovers (Straddle
    carries ``| IV <avg>`` — the call+put IV average — only when both sides have IV), appending
    ``| 09:20 OI: <v>`` to the ≤4 interpretor-tracked OI cells (already carrying ``Conf X% · Hits N`` from
    ``_apply_interpretor``, which runs first). On the **underlying/Totals row** it appends the 09:20
    aggregates (the ``totals`` block) to the existing *yesterday* hovers with a ` · ` join — per-side total
    OI, OI-PCR, and the bracketed-ATM IV per side. A ``None`` value gets no hover. No-op when there is no
    snapshot (no wave landed in the 09:20 window)."""
    if not hover:
        return
    strikes = hover.get("strikes") or {}
    totals = hover.get("totals") or {}
    for row in final_table_data:
        if row.get("is_underlying_row"):
            _apply_hover_totals(row, totals)
            continue
        entry = strikes.get(strike_key(extract_strike_value(row.get("strike", ""))))
        if not entry:
            continue
        for side in ("call", "put"):
            oi_value = entry.get(f"{side}_oi")
            if oi_value is not None:
                oi_text = f"09:20 OI: {format_lot_number(float(oi_value))}"
                existing = row.get(f"{side}_oi_hover")
                row[f"{side}_oi_hover"] = f"{existing} | {oi_text}" if existing else oi_text
            iv_value = entry.get(f"{side}_iv")
            if iv_value is not None:
                row[f"{side}_iv_hover"] = f"09:20 IV: {float(iv_value):.2f}"
        pcr_value = entry.get("pcr")
        if pcr_value is not None:
            row["strike_pcr_hover"] = f"09:20 PCR: {float(pcr_value):.2f}"
        straddle_value = entry.get("straddle_oi")
        if straddle_value is not None:
            straddle_text = f"09:20 Straddle: {format_lot_number(float(straddle_value))}"
            call_iv, put_iv = entry.get("call_iv"), entry.get("put_iv")
            if call_iv is not None and put_iv is not None:  # both sides → straddle IV; one-sided → omit
                straddle_text += f" | IV {(float(call_iv) + float(put_iv)) / 2:.2f}"
            row["strike_straddle_hover"] = straddle_text


def _apply_hover_totals(row: dict, totals: dict) -> None:
    """Append the 09:20 Totals-row aggregates to the underlying row's existing *yesterday* hovers with a
    ` · ` join (or alone when a yesterday hover is empty): per-side total OI, OI-PCR, and the bracketed-ATM
    IV per side. A ``None`` value adds nothing. No-op when the totals block is empty."""
    if not totals:
        return

    def _append(key: str, text: str) -> None:
        existing = row.get(key)
        row[key] = f"{existing} · {text}" if existing else text

    for side in ("call", "put"):
        oi_value = totals.get(f"{side}_oi")
        if oi_value is not None:
            _append(f"{side}_oi_hover", f"09:20: {format_lot_number(float(oi_value))}")
        atm_iv = totals.get(f"{side}_atm_iv")
        if atm_iv is not None:
            _append(f"{side}_iv_hover", f"09:20: ATM IV - {float(atm_iv):.2f}")
    pcr_value = totals.get("pcr")
    if pcr_value is not None:
        _append("strike_pcr_hover", f"09:20: {float(pcr_value):.2f}")


def build_chain_table_data(
    report: dict,
    *,
    view_data_service: ViewDataService | None = None,
    display_rows: list[dict] | None = None,
    interpretor: dict | None = None,
    hover: dict | None = None,
) -> list[dict]:
    """Assemble the chain's ``final_table_data`` — rows + ranking percentages + underlying-row
    totals + trophies. **Greeks-independent**: each row dict always carries *every* cell (LTP, OI,
    IV, VEX **and** the greeks), so the greeks toggle never changes this data — it only swaps the
    columns + styles (`build_chain_table_style`). This is what lets the toggle skip a full rebuild.

    The grid paints only the displayed strike window (``display_rows``); the underlying-row totals
    and max-pain border come from the full-chain ``report`` so the strike-range view never alters
    those market values.
    """
    lot_size = int(report.get("lot_size", 1) or 1)
    request = report.get("request", {})
    asset_id = int(request.get("asset_id", report.get("asset_id", 0) or 0))
    segment_code = str(request.get("segment_code", report.get("segment_code", "")))
    expiry = str(request.get("expiry", report.get("expiry", "")))
    aggregate_expiries = _collect_aggregate_expiries(report)
    is_aggregate_report = bool(aggregate_expiries) or str(report.get("pair_key", "")).startswith("aggregate|")

    grid_report = report if display_rows is None else {**report, "rows": display_rows}
    table_data, _, _ = build_table_rows(grid_report, lot_size)
    final_table_data = insert_underlying_row(table_data, float(report.get("underlying_price", 0) or 0))
    max_values = calculate_percentages(final_table_data)

    yesterday_totals = None
    oi_change_trophy_data = None
    # Yesterday-reference data (hovers + yesterday OI-change trophies) is SUPPRESSED in aggregate
    # mode: the EOD snapshot stamps each expiry at its own timestamp, so there is no honest
    # multi-expiry "yesterday" to compare a summed aggregate chain against — better absent than wrong.
    if view_data_service is not None and asset_id and segment_code and expiry and not is_aggregate_report:
        yesterday_totals = view_data_service.get_yesterday_totals(asset_id, segment_code, expiry)
        oi_change_trophy_data = view_data_service.get_oi_change_trophy_data(asset_id, segment_code, expiry)

    populate_underlying_row_totals(final_table_data, report, yesterday_totals, max_values)
    apply_trophy_systems(final_table_data, oi_change_trophy_data)
    _label_underlying_greeks(final_table_data)  # greeks-view spot row names each greek
    _apply_interpretor(final_table_data, interpretor)  # central-row labels + tracked-strike Conf/Hits
    _apply_hover(final_table_data, hover)  # A2 per-strike 09:20 reference hovers (runs after interpretor)
    return final_table_data


def build_chain_table_style(
    final_table_data: list[dict],
    report: dict,
    show_greeks: bool,
    display_rows: list[dict] | None = None,
    rate_markers: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """The **only** greeks-dependent outputs: the column set + the `style_data_conditional`. The
    greeks toggle recomputes just these two and patches the existing table (the data stays put).
    `display_rows` (the windowed strikes) scopes the conditional-style lookup to what's shown.
    `rate_markers` (`{(strike, side, rule): "up"|"down"}`) paints the Tier-2 Layer-1 marker cells."""
    columns = define_table_columns(show_greeks)
    style_data_conditional = build_conditional_styles(
        final_table_data, columns, report, show_greeks, display_rows=display_rows, rate_markers=rate_markers
    )
    return columns, style_data_conditional


# Tier-2 marker columns pinned to 5px (width is a native DataTable knob; the fill is a native
# style_data_conditional background). padding:0 so the colour fills the thin cell. The HEADER must be
# pinned too — a column's rendered width is the max of its header + data widths, and an unpinned (empty)
# header defaults to ~20px and forces the whole column wide.
_RATE_MARKER_CELL_STYLE = [
    {"if": {"column_id": cid}, "minWidth": "5px", "width": "5px", "maxWidth": "5px", "padding": "0"}
    for cid in RATE_MARKER_COLUMN_IDS
]
_RATE_MARKER_HEADER_STYLE = [
    {"if": {"column_id": cid}, "minWidth": "5px", "width": "5px", "maxWidth": "5px", "padding": "0"}
    for cid in RATE_MARKER_COLUMN_IDS
]

# Shared table chrome — the ONE source of the chain's cell/header/hover-tooltip styling. Both the main
# chain (below) and the Tier-2 Surface view (`features/chain/heatmap.py`) reference these so they can
# never drift (DataTable styles are inline-prop dicts, not CSS classes — there is no `--oc-*` token to
# share; the shared *dict* is the equivalent).
CHAIN_STYLE_CELL = {
    "textAlign": "center",
    "fontSize": "14px",
    "fontWeight": "bold",
    "padding": "12px",
    "fontFamily": "Geist Mono, monospace",
    "fontVariantNumeric": "tabular-nums",
    "whiteSpace": "pre-line",
    "lineHeight": "1.15",
    "backgroundColor": _BASE_BG,
    "color": _ITM_TXT,
    "border": f"1px solid {theme.BORDER}",
}
CHAIN_STYLE_HEADER = {
    "backgroundColor": theme.SURFACE_SOFT,
    "color": _ITM_TXT,
    "fontWeight": "bold",
    "fontSize": "16px",
    "textAlign": "center",
    "padding": "15px",
    "border": f"1px solid {theme.BORDER_STRONG}",
    "fontFamily": "Geist Mono, monospace",
}
CHAIN_TABLE_CSS = [
    # Row hover is DISABLED: it added no value and fought the conditional cell colours.
    # dash_table's built-in hover paints `background-color: var(--hover)` on the ROW (`tr`),
    # which bled through the translucent tinted cells (violet straddle / teal GEX / rank
    # washes) while opaque cells blocked it — an inconsistent mess. Forcing the hovered row's
    # background back to transparent makes hover look identical to rest; the per-cell
    # conditional backgrounds (set on the `td`s) are untouched.
    {"selector": ".dash-spreadsheet-inner tr:hover", "rule": "background-color: transparent !important;"},
    # Tooltip: the chain's hover tooltips render in `.dash-table-tooltip` — dark surface + legible text.
    {
        "selector": ".dash-table-tooltip",
        "rule": """
            background-color: #18233c !important;
            color: #e8edf7 !important;
            border: 1px solid #2a3a5c !important;
            border-radius: 4px !important;
            font-size: 15px !important;
            font-weight: bold !important;
            font-family: "Geist Mono", monospace !important;
            text-align: center !important;
            box-shadow: 0 8px 28px rgba(0,0,0,0.5) !important;
            z-index: 9999 !important;
        """,
    },
    {"selector": ".dash-table-tooltip *", "rule": "color: #e8edf7 !important; background-color: transparent !important;"},
]


def build_option_chain_table(
    report: dict,
    *,
    view_data_service: ViewDataService | None = None,
    show_greeks: bool = False,
    display_rows: list[dict] | None = None,
    rate_markers: dict | None = None,
    interpretor: dict | None = None,
    hover: dict | None = None,
) -> dash_table.DataTable:
    final_table_data = build_chain_table_data(
        report, view_data_service=view_data_service, display_rows=display_rows, interpretor=interpretor, hover=hover
    )
    columns, style_data_conditional = build_chain_table_style(
        final_table_data, report, show_greeks, display_rows=display_rows, rate_markers=rate_markers
    )
    tooltip_data = build_tooltips(final_table_data)

    return dash_table.DataTable(
        id="chain-option-table",
        data=final_table_data,
        columns=columns,
        # Tier-2 Layer-1: marker columns are 5px plain spacer cells; the crossing colour is a native
        # style_data_conditional background (Dash strips inline style from markdown, so no markdown here).
        style_cell_conditional=_RATE_MARKER_CELL_STYLE,
        style_header_conditional=_RATE_MARKER_HEADER_STYLE,
        sort_action="none",
        style_cell=CHAIN_STYLE_CELL,
        style_header=CHAIN_STYLE_HEADER,
        style_data_conditional=style_data_conditional,
        tooltip_data=tooltip_data,
        tooltip_duration=None,
        tooltip_delay=0,
        css=CHAIN_TABLE_CSS,
    )
