"""Option Reversal Curve — pure compute (no I/O).

For a single option the drawer shows price-vs-time lines over **today's session only**:

* **Current price** — the option's actual stored LTP over time (passed in as ``ltp_series``; no
  math here, just filtered to today and mapped onto the time axis), session-open → now.
* **Reversal price(s)** — forward Black-Scholes for the selected contract (its strike / IV / side /
  hours-TTE held fixed) with the underlying held at a pain-point spot level: what the option would be
  worth if the underlying sat at that level, drawn across **today's session** (open → close). The
  **self** line uses the contract's own ``pain_point``; up to four optional extra lines reuse the same
  contract with the spot swapped to the **highest-OI** and **2nd-highest-OI** call/put strikes' pain
  points (the only per-line difference is the spot; the 2nd walls are passed only when the panel has
  qualified them past its local 75% OI ratio). All use a **continuous
  hours-to-expiry** TTE (`_fractional_dte`) — ALWAYS, for any expiry (today or not) — so they decay
  through the day (slope = theta), computed at each of today's LTP timestamps (so the lines share an
  x-grid) plus a uniform continuation from now → today's session-close (pannable). The self line is
  suppressed (`draw_self=False`) when it would coincide with a wall line.

The chart is a real **datetime** x-axis (a single continuous session → no rangebreaks). The default
view is a **rolling 60-minute window**: the first data point sits at the LEFT edge early-session and
the window rolls once ``now`` passes ``first_t + 60min`` — no centering, with a permanent 5-min right
pad so the LTP line doesn't stick to the right edge (``x_range``).

Historical note: this module previously had an Intraday/Swing toggle and projected the reversal all
the way to expiry on a 15-min grid; both are gone. The public name ``build_required_move_curve`` and
the ``rmd-*`` ids are intentionally kept.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from analytics.chart_windows import rolling_window_range
from analytics.report_builder import _count_trading_days, black_scholes_price

IST = ZoneInfo("Asia/Kolkata")
SIGMA_FLOOR = 0.01
SIGMA_CAP = 3.0
TIME_FLOOR = 1e-4  # years; avoids the t -> 0 Black-Scholes singularity
# True NSE session 9:15–15:30 (the global 3:30 close); MCX 9:00–23:30.
_MARKET_HOURS = {"NSE": {"start": 9.25, "end": 15.5}, "MCX": {"start": 9.0, "end": 23.5}}
_FUTURE_STEP_MINUTES = 5  # step for the now→close reversal continuation
_WINDOW_MINUTES = 60  # default rolling-view width
_RIGHT_PAD_MINUTES = 5  # permanent right padding so the LTP line doesn't stick to the right edge


def _clamp_sigma(sigma: float) -> float:
    return max(SIGMA_FLOOR, min(SIGMA_CAP, sigma))


def _parse_ts(value: Any) -> datetime | None:
    """Parse a stored naive-IST ISO timestamp (tolerant of a ``Z``/offset suffix) → naive IST."""
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST).replace(tzinfo=None)
    return dt


def _fractional_dte(moment: datetime, expiry_dt, holidays, start_h: float, end_h: float) -> float:
    """Continuous trading-days-to-expiry at ``moment`` (intraday mode).

    = trading days strictly after ``moment``'s date (holiday-aware) + the remaining fraction of
    ``moment``'s own market session. Equals the integer-day value at each session close, and reduces
    to the same-day remaining-hours measure when ``moment`` is on the expiry day.
    """
    days_after = _count_trading_days(moment.date(), expiry_dt, holidays)
    d = moment.date()
    if d.weekday() < 5 and d not in holidays:
        h = moment.hour + moment.minute / 60.0 + moment.second / 3600.0
        frac = (end_h - max(h, start_h)) / (end_h - start_h)
        frac = min(1.0, max(0.0, frac))
    else:
        frac = 0.0
    return days_after + frac


def build_required_move_curve(
    *,
    strike: float,
    option_type: str,
    iv: float,
    rate: float | None,
    expiry: str,
    segment: str,
    pain_point: float,
    call_pain_point: float | None = None,
    put_pain_point: float | None = None,
    call_2nd_pain_point: float | None = None,
    put_2nd_pain_point: float | None = None,
    ltp_series: list[dict] | None = None,
    holidays=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Price-vs-time series for one option over **today's session**: actual LTP + up to three
    pain-point reversal lines.

    The reversal is a forward Black-Scholes price for the **selected** contract (its strike / IV /
    side / continuous hours-to-expiry TTE, ``_fractional_dte``) with the underlying held at a spot
    level — the *only* per-line difference being that spot:

    * ``reversal_y`` (self) — spot = the selected contract's own ``pain_point``.
    * ``call_reversal_y`` — spot = ``call_pain_point`` (the highest-OI call strike's pain point), ``[]``
      when not supplied / non-positive.
    * ``put_reversal_y`` — spot = ``put_pain_point`` (the highest-OI put strike's pain point).
    * ``call_2nd_reversal_y`` / ``put_2nd_reversal_y`` — spot = the **2nd-highest-OI** call/put strike's
      pain point (``call_2nd_pain_point`` / ``put_2nd_pain_point``), ``[]`` when not supplied. The panel
      passes these only when the 2nd strike clears its local 75% OI ratio, so a non-qualifying 2nd is
      simply a ``None`` here (no line).

    ``draw_self`` is ``False`` when the self pain point coincides with **any** wall pain point (highest
    or 2nd, call or put — the selected contract *is* that wall) — so the self line isn't drawn on top
    of a wall line.

    Returns a JSON-safe dict: ``ok``, ``x_is_datetime`` (always True), the shared reversal x-grid
    (``reversal_x``, today open→close) with ``reversal_y`` / ``call_reversal_y`` / ``put_reversal_y``,
    ``draw_self`` + ``call_pain_point`` / ``put_pain_point``, the LTP series (``ltp_x`` / ``ltp_y``,
    today open→now), the default rolling-60-minute ``x_range``, ``tickformat`` / ``x_title``, and a terse
    ``readout`` (the self line's pain/now/close).
    """
    result: dict[str, Any] = {
        "ok": False,
        "x_is_datetime": True,
        "reversal_x": [],
        "reversal_y": [],
        "call_reversal_y": [],
        "put_reversal_y": [],
        "call_2nd_reversal_y": [],
        "put_2nd_reversal_y": [],
        "draw_self": True,
        "call_pain_point": None,
        "put_pain_point": None,
        "call_2nd_pain_point": None,
        "put_2nd_pain_point": None,
        "ltp_x": [],
        "ltp_y": [],
        "x_range": None,
        "tickformat": "%H:%M",
        "x_title": "Time (IST)",
        "readout": None,
    }
    strike = float(strike or 0)
    pain_point = float(pain_point or 0)
    sigma_raw = float(iv or 0) / 100.0
    if option_type not in ("CE", "PE") or strike <= 0 or pain_point <= 0 or sigma_raw <= 0 or rate is None:
        return result
    try:
        expiry_dt = datetime.strptime(expiry, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return result

    sigma = _clamp_sigma(sigma_raw)
    rate = float(rate)
    if now is None:
        reference = datetime.now(IST).replace(tzinfo=None)
    elif now.tzinfo is None:
        reference = now  # naive == IST by the app-wide convention
    else:
        reference = now.astimezone(IST).replace(tzinfo=None)
    today = reference.date()
    market = "MCX" if segment == "MCX_COMM" else "NSE"
    holiday_set = holidays or frozenset()
    ltp_series = ltp_series or []
    hours = _MARKET_HOURS.get(market, _MARKET_HOURS["NSE"])
    start_h, end_h = hours["start"], hours["end"]

    # Nothing to draw if today isn't a trading day or its session is already over.
    reference_h = reference.hour + reference.minute / 60.0 + reference.second / 3600.0
    if today.weekday() >= 5 or today in holiday_set or reference_h >= end_h or expiry_dt < today:
        return result

    # The two wall pain points (highest-OI call/put strikes); each is a spot level for an extra
    # reversal line — same selected contract, only the spot differs. None / non-positive → no line.
    call_pain = float(call_pain_point) if call_pain_point not in (None, 0) and float(call_pain_point) > 0 else None
    put_pain = float(put_pain_point) if put_pain_point not in (None, 0) and float(put_pain_point) > 0 else None
    # The 2nd-highest-OI call/put walls (drawn only when the caller qualified them past its local 75%
    # OI ratio — the gate lives in the panel, not here; this just draws a line for any positive level).
    call_2nd_pain = float(call_2nd_pain_point) if call_2nd_pain_point not in (None, 0) and float(call_2nd_pain_point) > 0 else None
    put_2nd_pain = float(put_2nd_pain_point) if put_2nd_pain_point not in (None, 0) and float(put_2nd_pain_point) > 0 else None

    def bs(t_years: float, spot: float) -> float:
        return black_scholes_price(
            spot=spot, strike=strike, time_to_expiry=max(t_years, TIME_FLOOR),
            rate=rate, volatility=sigma, option_type=option_type,
        )

    def _tte_years(moment: datetime) -> float:
        # Always the continuous hours-to-expiry measure — for any expiry (today or not).
        return _fractional_dte(moment, expiry_dt, holiday_set, start_h, end_h) / 252.0

    # ---- LTP line: today's timestamps only, session-open → now (real second resolution) ----
    ltp_points: list[tuple[datetime, float]] = []
    for point in ltp_series:
        ts = _parse_ts(point.get("timestamp"))
        val = point.get("value")
        if ts is None or val is None or ts.date() != today:
            continue
        ltp_points.append((ts, round(float(val), 4)))
    ltp_points.sort(key=lambda p: p[0])
    ltp_times = [t for t, _ in ltp_points]
    ltp_x = [t.isoformat() for t in ltp_times]
    ltp_y = [v for _, v in ltp_points]

    # ---- Reversal line: at each today LTP timestamp (shared grid) + a 5-min continuation now→close ----
    midnight = datetime.combine(today, datetime.min.time())
    session_open = midnight + timedelta(hours=start_h)
    session_close = midnight + timedelta(hours=end_h)
    rev_moments: set[datetime] = set(ltp_times)
    cont = max(reference, session_open)
    while cont <= session_close:
        rev_moments.add(cont)
        cont += timedelta(minutes=_FUTURE_STEP_MINUTES)
    rev_moments.add(session_close)  # exact session-close anchor
    ordered = sorted(rev_moments)
    rev_x = [m.isoformat() for m in ordered]
    ttes = [_tte_years(m) for m in ordered]
    rev_y = [round(bs(t, pain_point), 4) for t in ttes]
    call_rev_y = [round(bs(t, call_pain), 4) for t in ttes] if call_pain is not None else []
    put_rev_y = [round(bs(t, put_pain), 4) for t in ttes] if put_pain is not None else []
    call_2nd_rev_y = [round(bs(t, call_2nd_pain), 4) for t in ttes] if call_2nd_pain is not None else []
    put_2nd_rev_y = [round(bs(t, put_2nd_pain), 4) for t in ttes] if put_2nd_pain is not None else []

    # Don't draw the self line when it coincides with a wall line (selected IS that wall) — else two
    # identical plots stack at the same value.
    draw_self = not (
        (call_pain is not None and abs(pain_point - call_pain) < 1e-6)
        or (put_pain is not None and abs(pain_point - put_pain) < 1e-6)
        or (call_2nd_pain is not None and abs(pain_point - call_2nd_pain) < 1e-6)
        or (put_2nd_pain is not None and abs(pain_point - put_2nd_pain) < 1e-6)
    )

    # ---- Default rolling-60min view: first point at the LEFT edge early-session, then rolling.
    # right_pad keeps a permanent gap past `now` so the LTP line doesn't stick to the right edge. ----
    first_t = ltp_times[0] if ltp_times else reference
    left_edge, right_edge = rolling_window_range(
        first_t=first_t, right_anchor=reference,
        window_minutes=_WINDOW_MINUTES, left_pad_minutes=0, right_pad_minutes=_RIGHT_PAD_MINUTES,
    )

    result.update(
        ok=bool(rev_y or ltp_y),
        reversal_x=rev_x, reversal_y=rev_y,
        call_reversal_y=call_rev_y, put_reversal_y=put_rev_y, draw_self=draw_self,
        call_2nd_reversal_y=call_2nd_rev_y, put_2nd_reversal_y=put_2nd_rev_y,
        call_pain_point=round(call_pain, 2) if call_pain is not None else None,
        put_pain_point=round(put_pain, 2) if put_pain is not None else None,
        call_2nd_pain_point=round(call_2nd_pain, 2) if call_2nd_pain is not None else None,
        put_2nd_pain_point=round(put_2nd_pain, 2) if put_2nd_pain is not None else None,
        ltp_x=ltp_x, ltp_y=ltp_y,
        x_range=[left_edge.isoformat(), right_edge.isoformat()],
        readout={
            "pain_point": round(pain_point, 2),
            "reversal_now": round(bs(_tte_years(reference), pain_point), 4) if rev_y else None,
            "reversal_close": rev_y[-1] if rev_y else None,
        },
    )
    return result
