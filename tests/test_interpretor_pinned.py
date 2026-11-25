"""Phase 4 tests — the interpretor pinned block (signed streak + 09:20 walls) in the panel feed."""

from __future__ import annotations

from ui.shell import _build_interp_pinned, _interp_strike_text, _interp_streak_text


def _texts(component) -> list[str]:
    """Flatten every string leaf out of a Dash component tree."""
    out: list[str] = []
    children = getattr(component, "children", component if isinstance(component, str) else None)
    if isinstance(children, str):
        out.append(children)
    elif isinstance(children, (list, tuple)):
        for child in children:
            out.extend(_texts(child))
    elif children is not None and not isinstance(children, str):
        out.extend(_texts(children))
    return out


def test_streak_text_is_signed_by_direction():
    assert _interp_streak_text({"streak": 3, "shift": "bullish"}) == "+3"   # stepped up 3×
    assert _interp_streak_text({"streak": 2, "shift": "bearish"}) == "-2"   # stepped down 2×


def test_streak_text_em_dash_without_a_confirmed_shift():
    assert _interp_streak_text({"streak": 0, "shift": None}) == "—"
    assert _interp_streak_text({"streak": 4, "shift": None}) == "—"  # no direction → no sign


def test_strike_text_formats_and_dashes_missing():
    assert _interp_strike_text(22600.0) == "22600"
    assert _interp_strike_text(2.5) == "2.5"
    assert _interp_strike_text(None) == "-"


def test_pinned_block_is_none_without_a_read():
    assert _build_interp_pinned(None) is None
    assert _build_interp_pinned({}) is None


def test_pinned_block_renders_both_sides_with_walls():
    read = {
        "call": {"streak": 3, "shift": "bullish"},
        "put": {"streak": 2, "shift": "bearish"},
        "stamp_0920": {
            "call": {"primary": 22600.0, "second": 22400.0},
            "put": {"primary": 22300.0, "second": None},
        },
    }
    text = " ".join(_texts(_build_interp_pinned(read)))
    assert "CE" in text and "streak +3" in text and "22600" in text and "(2nd 22400)" in text
    assert "PE" in text and "streak -2" in text and "22300" in text and "(2nd -)" in text


def test_pinned_block_pre_0920_shows_dashes():
    read = {
        "call": {"streak": 0, "shift": None},
        "put": {"streak": 0, "shift": None},
        "stamp_0920": None,
    }
    text = " ".join(_texts(_build_interp_pinned(read)))
    assert "streak —" in text          # no shift yet
    assert "(2nd -)" in text           # no 09:20 stamp yet
