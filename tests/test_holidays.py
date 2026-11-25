from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from adapters.holidays import load_holidays
from analytics.report_builder import _count_trading_days, time_to_expiry_years

IST = ZoneInfo("Asia/Kolkata")


def test_load_holidays_parses_and_skips_comments(tmp_path):
    path = tmp_path / "holidays.txt"
    path.write_text(
        "# comment\n\n14/04/2026\n# note\n01/05/2026\nbad-line\n",
        encoding="utf-8",
    )
    assert load_holidays(path) == frozenset({date(2026, 4, 14), date(2026, 5, 1)})


def test_load_holidays_missing_file_is_empty(tmp_path):
    assert load_holidays(tmp_path / "nope.txt") == frozenset()


def test_count_trading_days_excludes_holiday():
    # 22 Jun (Mon) → 30 Jun (Tue): weekdays 23,24,25,26,29,30 = 6.
    assert _count_trading_days(date(2026, 6, 22), date(2026, 6, 30)) == 6
    # Drop 26 Jun (Fri) as a holiday → 5.
    assert _count_trading_days(
        date(2026, 6, 22), date(2026, 6, 30), frozenset({date(2026, 6, 26)})
    ) == 5


def test_time_to_expiry_shrinks_across_a_holiday():
    as_of = datetime(2026, 6, 22, 11, 0, tzinfo=IST)
    base = time_to_expiry_years("2026-06-30", "NSE", as_of=as_of)
    with_holiday = time_to_expiry_years(
        "2026-06-30", "NSE", as_of=as_of, holidays=frozenset({date(2026, 6, 26)})
    )
    assert with_holiday < base
    assert abs(base - 6 / 252.0) < 1e-9
    assert abs(with_holiday - 5 / 252.0) < 1e-9
