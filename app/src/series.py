"""Builds the finished response for the frontend out of the price store."""

import calendar
import datetime
import logging
import math

import numpy as np
import pandas as pd

from . import i18n, indicators, signals, store, yahoo

log = logging.getLogger("chart.series")

# Displayed window in calendar days. Cutting happens by date, not by number of
# bars: shares trade on roughly 252 days a year, crypto on 365. A fixed bar
# count would be only three quarters of a year for BTC-USD.
RANGE_DAYS = {
    "6mo": 183,
    "1y": 365,
    "2y": 730,
    "5y": 1826,
    "max": None,
}

# Run-up before the visible window, so that SMA 200 and EMA 200 have settled at
# the left edge of the chart. 420 calendar days correspond to roughly 288
# trading days and safely cover the 200 bars needed.
WARMUP_DAYS = 420


def _clean(value):
    """NaN and Inf cannot be represented in JSON -- they become null."""
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _floats(values) -> list:
    """A whole column as a JSON-ready list: floats, NaN and Inf as None.

    The same result as _clean() value by value, but the test for NaN runs in
    numpy over the whole array and only the gaps are touched in Python.
    Value by value it was 266,000 calls for a "max" chart -- 0.36 s, more
    than half of building the response (measured on 2026-11-18).
    """
    array = np.asarray(values, dtype="float64")
    out = array.tolist()
    for i in np.flatnonzero(~np.isfinite(array)).tolist():
        out[i] = None
    return out


def _dates(index) -> list:
    """The dates of an index as "YYYY-MM-DD", formatted in one go.

    numpy rather than index.strftime(): pandas formats every date through
    Python's strftime, 0.16 s for the dates of a "max" chart (measured on
    2026-11-18). tz_localize(None) keeps the wall time of the index's zone,
    so the date is the one strftime would have printed.
    """
    if not len(index):
        return []
    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)
    return np.datetime_as_string(index.values, unit="D").tolist()


def _ohlc(df: pd.DataFrame) -> list:
    """Candles in ECharts order: open, close, low, high."""
    return [list(bar) for bar in zip(
        _floats(df["Open"]), _floats(df["Close"]),
        _floats(df["Low"]), _floats(df["High"]),
    )]


def _crosses(window: pd.DataFrame, flag: str) -> list:
    """The days a cross column marks, with the close of that day."""
    hits = window["Close"][window[flag].astype(bool)]
    return [{"date": d, "value": v} for d, v in zip(_dates(hits.index), _floats(hits))]


def _column(df: pd.DataFrame, name: str) -> list:
    if name not in df:
        return []
    return _floats(df[name])


# Window for the watchlist: enough for the sparkline and the daily change,
# small enough not to load years straight away for a symbol not yet known.
QUOTE_DAYS = 45


def quote(symbol: str) -> dict:
    """Short overview of a symbol for the watchlist.

    Uses the same store as the chart. If the symbol is already there and
    current, the call costs no request to Yahoo.
    """
    start_ts = int(
        (pd.Timestamp.utcnow() - pd.Timedelta(days=QUOTE_DAYS)).timestamp()
    )
    result = store.load(symbol, "1d", start_ts)
    closes = result.frame["Close"].dropna()
    if closes.empty:
        raise yahoo.YahooError(
            f"No data for {symbol}", "error.no_data", symbol=symbol
        )

    last = float(closes.iloc[-1])
    # Previous day's close. During the trading day the last bar is the running
    # one -- so the change correctly refers to yesterday.
    previous = float(closes.iloc[-2]) if len(closes) > 1 else last
    meta = result.meta or {}

    return {
        "symbol": symbol.upper(),
        "currency": meta.get("currency", ""),
        "exchange": meta.get("fullExchangeName", ""),
        "last": _clean(last),
        "change_pct": _clean((last - previous) / previous * 100) if previous else None,
        "history": _floats(closes.tail(30)),
        "stale": result.stale,
    }


def _monthly_block(symbol: str, window: pd.DataFrame) -> tuple[dict, dict, dict]:
    """Monthly series, status figures and the levels for the daily overlay.

    The monthly history is always loaded in full (period1=0). That is roughly
    340 bars, a single fetch, and afterwards they are in the store -- the
    run-up for an EMA 21 on a monthly basis is 21 months after all.
    """
    result = store.load(symbol, "1mo", 0)
    frame = result.frame
    if frame.empty:
        # Three values, all of the type of the normal case. When a list stood
        # here as the third element, the caller broke off with an
        # AttributeError because it calls overlay.get(...) -- and did so as an
        # unhandled 500.
        return {}, {}, {}

    monthly = indicators.compute_monthly(frame)

    # Is the last bar still running? Compared over the calendar month, not
    # over the time of day -- for the running month Yahoo sets sometimes the
    # first of the month, sometimes the current moment.
    today = pd.Timestamp.utcnow()
    last = monthly.index[-1]
    running = (last.year, last.month) == (today.year, today.month)

    levels = indicators.settled_levels(monthly["Close"], running)
    price = float(monthly["Close"].iloc[-1])

    ema8, ema12 = levels.get(8), levels.get(12)
    if ema8 is None:
        # Without EMA8 there is nothing to judge; the frontend then hides the
        # card.
        position = None
        uptrend = None
    elif ema12 is None:
        position = "above_ema8" if price >= ema8 else "below_ema8"
        uptrend = None
    else:
        if price >= ema8 and price >= ema12:
            position = "above_ema8"
        elif price < min(ema8, ema12):
            position = "below_both"
        else:
            position = "between"
        # If EMA12 sits below EMA8, it is the next line of support underneath.
        # If it sits above, it is no target downwards but resistance.
        uptrend = ema12 < ema8

    # Remaining exchange days in the running month. Public holidays are not
    # accounted for -- that would need a calendar per exchange.
    today_date = datetime.date.today()
    last_day = calendar.monthrange(today_date.year, today_date.month)[1]
    days_left = sum(
        1 for day in range(today_date.day, last_day + 1)
        if datetime.date(today_date.year, today_date.month, day).weekday() < 5
    )

    status = {
        "price": _clean(price),
        "ema8": _clean(ema8),
        "ema12": _clean(ema12),
        "ema21": _clean(levels.get(21)),
        # Distance FROM THE PRICE to the respective level, not the other way
        # round. Positive means: the level lies above the price, so it is that
        # many percent up to it. The reference is the price -- the question is
        # "how far is it to EMA8", not "how far is the price below EMA8". Both
        # readings give different numbers, because the denominator differs.
        "dist8": _clean((ema8 - price) / price * 100) if ema8 and price else None,
        "dist12": _clean((ema12 - price) / price * 100) if ema12 and price else None,
        "dist21": _clean((levels.get(21) - price) / price * 100)
                  if levels.get(21) and price else None,
        # A key, not a phrase: the frontend picks both the wording and the
        # colour class from it, and it stays the same in either language.
        "position": position,
        "uptrend": uptrend,
        # If one of the lines is missing, we say why -- rather than showing a
        # half-settled number.
        "short_history": [
            n for n in indicators.MONTHLY_LENGTHS if levels.get(n) is None
        ],
        "settle_factor": indicators.SETTLE_FACTOR,
        "running": running,
        "trading_days_left": days_left,
        "months": len(monthly),
    }

    block = {
        "dates": _dates(monthly.index),
        "ohlc": _ohlc(monthly),
        "close": _column(monthly, "Close"),
        "ema8": _column(monthly, "EMA_8"),
        "ema12": _column(monthly, "EMA_12"),
        "ema21": _column(monthly, "EMA_21"),
        "running": running,
    }

    # Levels for the daily overlay: during a month the value of the last
    # completed month applies -- hence the shift by one period. A staircase
    # results, and during the month that is exactly the level at which the
    # rule is decided.
    # The computation runs over ALL months, the running one included -- and
    # only afterwards shifts by one period. The EMA is causal: the value of a
    # month depends only on it and its predecessors. After the shift every day
    # therefore carries the state of the last completed month, and the open
    # closing price of the running month lands on the following month, which
    # lies outside the window.
    #
    # Previously the running month was additionally cut off beforehand. That
    # shifted twice: the running month stayed entirely empty, and the days
    # before it carried the value of the month before last (measured on MSTR:
    # 1-4 September without a value, 31 August showing 144.80 from July
    # instead of 142.16 from August).
    #
    # tz_convert(None) before to_period: otherwise pandas warns on every call
    # that the time zone is lost. For mapping day -> month it is irrelevant
    # anyway, both series are in UTC.
    periods = window.index.tz_convert(None).to_period("M")
    overlay = {}
    for n in (8, 12):
        values = indicators.ema(monthly["Close"], n)
        values.index = values.index.tz_convert(None).to_period("M")
        overlay[f"m_ema{n}"] = _floats(values.shift(1).reindex(periods))

    return block, status, overlay


def _monthly_signals(monthly_dates, closes, ema8, ema12, running,
                     start_date, end_date, language) -> list[dict]:
    """Monthly closes below the EMA levels.

    The running month stays out of it -- its closing price is not settled yet,
    a signal from it would be premature.
    """
    out = []
    last = len(monthly_dates) - (1 if running else 0)
    for i in range(last):
        date = monthly_dates[i]
        if start_date is not None and date < start_date:
            continue
        if end_date is not None and date > end_date:
            continue
        close, e8, e12 = closes[i], ema8[i], ema12[i]
        if close is None:
            continue
        if e12 is not None and close < e12:
            kind, level = "month_below_ema12", e12
        elif e8 is not None and close < e8:
            kind, level = "month_below_ema8", e8
        else:
            continue
        out.append({
            "date": date,
            "kind": kind,
            "text": i18n.translate(language, "signal." + kind,
                                   close=f"{close:.2f}", level=f"{level:.2f}"),
            "value": round(close, 2),
        })
    return out


def _window_bounds(rng: str, start, end):
    """Lower and upper bound of the visible window.

    Returns (start, end, load_from). "start" and "end" cut the computed series
    to size, "load_from" is the point from which bars are loaded -- that is,
    including the run-up for the long averages.
    """
    now = pd.Timestamp.utcnow()

    if start is not None or end is not None:
        # Freely chosen period. If a bound is missing, it stays open.
        window_start = pd.Timestamp(start, tz="UTC") if start else None
        # The end day counts, hence up to the beginning of the following day.
        window_end = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1) if end else None
        if (window_start is not None and window_end is not None
                and window_end <= window_start):
            raise yahoo.EmptyRange(
                "The start date is after the end date", "error.start_after_end"
            )
        load_from = 0 if window_start is None else int(
            (window_start - pd.Timedelta(days=WARMUP_DAYS)).timestamp()
        )
        return window_start, window_end, load_from

    if rng not in RANGE_DAYS:
        raise yahoo.EmptyRange(f"Unknown period: {rng}", "error.unknown_range", range=rng)

    days = RANGE_DAYS[rng]
    if days is None:
        # The entire available history. period1=0 gets daily bars from Yahoo
        # even across decades -- unlike range=max, which silently switches to
        # monthly bars.
        return None, None, 0

    window_start = now - pd.Timedelta(days=days)
    return window_start, None, int(
        (window_start - pd.Timedelta(days=WARMUP_DAYS)).timestamp()
    )


def _cut(frame, window_start, window_end):
    """Cuts a series down to the window."""
    if window_start is not None:
        frame = frame[frame.index >= window_start]
    if window_end is not None:
        frame = frame[frame.index < window_end]
    return frame


def closes(symbol: str, rng: str, start=None, end=None) -> dict:
    """Only the daily closes of the window -- the comparison line in the chart.

    No indicators and no run-up: the line is rescaled onto the main symbol
    in the browser, nothing else is drawn from it.
    """
    window_start, window_end, _ = _window_bounds(rng, start, end)
    since = 0 if window_start is None else int(window_start.timestamp())
    result = store.load(symbol, "1d", since)
    frame = _cut(result.frame, window_start, window_end)["Close"].dropna()
    if frame.empty:
        raise yahoo.YahooError(f"No data for {symbol}", "error.no_data", symbol=symbol)
    return {
        "symbol": symbol.upper(),
        "currency": (result.meta or {}).get("currency", ""),
        "dates": _dates(frame.index),
        "close": _floats(frame),
    }


def build(symbol: str, rng: str, start=None, end=None, language: str = "de") -> dict:
    window_start, window_end, start_ts = _window_bounds(rng, start, end)

    # Daily series. The store only reloads what is still missing.
    daily_result = store.load(symbol, "1d", start_ts)
    if daily_result.frame.empty:
        if start is not None or end is not None:
            # EmptyRange rather than YahooError: an empty selection is an input
            # by the visitor, not an outage at Yahoo (400 instead of 502).
            raise yahoo.EmptyRange(
                f"No data for {symbol} in this period",
                "error.no_data_in_range", symbol=symbol,
            )
        raise yahoo.YahooError(f"No data for {symbol}", "error.no_data", symbol=symbol)

    daily = indicators.compute(daily_result.frame)
    # Cut to the window only after the computation: the indicators are thereby
    # valid from the first visible bar -- unlike in the template, where the
    # first day had an RSI of 0.00.
    window = _cut(daily, window_start, window_end)
    if window.empty:
        raise yahoo.EmptyRange(
            f"No trading days for {symbol} in this period",
            "error.no_trading_days_in_range", symbol=symbol,
        )

    # The Fibonacci levels depend on the visible window and are determined
    # afresh on every switch of the period.
    fib = indicators.fib_levels(window["Close"])

    # Guarded like the VIX below: if of all things the weekly fetch fails -- it
    # is the third request in a row and hits the throttling first -- that must
    # not discard the daily data just fetched and end the whole request with a
    # 502. The weekly chart then simply stays empty.
    try:
        weekly_result = store.load(symbol, "1wk", start_ts)
        weekly_all = indicators.compute_weekly(weekly_result.frame)
        weekly = _cut(weekly_all, window_start, window_end)
        weekly_requests = weekly_result.requests
        weekly_new = weekly_result.new_bars
        weekly_stale = weekly_result.stale
    except yahoo.YahooError as exc:
        log.warning("%s: weekly series not available (%s)", symbol, exc)
        weekly = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        weekly_requests, weekly_new, weekly_stale = 0, 0, True

    # VIX as a panel of its own. The mapping runs over the calendar date, not
    # over the timestamp: Yahoo delivers a different time per exchange
    # (NasdaqGS 13:30 UTC, Cboe 14:30 UTC), a direct reindex would yield only
    # empty values.
    vix_requests = 0
    try:
        vix_result = store.load("^VIX", "1d", start_ts)
        vix_requests = vix_result.requests
        vix_close = vix_result.frame["Close"]
        vix_close.index = vix_close.index.normalize()
        aligned = vix_close.reindex(window.index.normalize())

        # The VIX is only computed on exchange days. For an instrument that
        # trades seven days a week (BTC-USD) roughly 30 percent of the days are
        # missing as a result -- measured over half a year: 26 Saturdays, 26
        # Sundays and three holiday Fridays. The line fell apart into
        # five-day pieces.
        #
        # The last closing price is therefore carried across the non-trading
        # days: as long as the exchange is closed the VIX really does not
        # change -- so the horizontal stretch claims nothing false. The limit
        # of four days covers a long weekend including a holiday; a genuine
        # data outage stays visible as a gap instead of appearing as a
        # weeks-long straight line.
        aligned = aligned.ffill(limit=4)

        vix_values = _floats(aligned)
        if not any(v is not None for v in vix_values):
            vix_values = []
    except yahoo.YahooError:
        vix_values = []

    # Monthly series: a chart of its own, status figures and the levels that
    # sit as an overlay on the daily chart.
    try:
        monthly_block, monthly_status, overlay = _monthly_block(symbol, window)
    except yahoo.YahooError:
        monthly_block, monthly_status, overlay = {}, {}, {}

    meta = daily_result.meta or {}
    dates = _dates(window.index)

    return {
        "symbol": (meta.get("symbol") or symbol).upper(),
        "currency": meta.get("currency", ""),
        "exchange": meta.get("fullExchangeName", ""),
        "last": _clean(meta.get("regularMarketPrice")),
        "change_pct": _clean(meta.get("regularMarketChangePercent")),
        "range": rng,
        # Where the data came from -- the frontend shows it as a small note.
        "fetch": {
            "requests": daily_result.requests + weekly_requests + vix_requests,
            "new_bars": daily_result.new_bars + weekly_new,
            "stored_bars": len(daily_result.frame),
            "stale": daily_result.stale or weekly_stale,
        },
        "daily": {
            "dates": dates,
            "close": _column(window, "Close"),
            "open": _column(window, "Open"),
            "high": _column(window, "High"),
            "low": _column(window, "Low"),
            "volume": _column(window, "Volume"),
            "vol_spike": [bool(v) for v in window["Vol_Spike"].fillna(False)],
            "sma50": _column(window, "SMA_50"),
            "sma200": _column(window, "SMA_200"),
            "ema50": _column(window, "EMA_50"),
            "ema200": _column(window, "EMA_200"),
            "sma12": _column(window, "SMA_12"),
            "sma30": _column(window, "SMA_30"),
            "macd": _column(window, "MACD"),
            "macd_signal": _column(window, "MACD_Signal"),
            "macd_hist": _column(window, "MACD_Hist"),
            "rsi": _column(window, "RSI"),
            "bb_upper": _column(window, "BB_Upper"),
            "bb_middle": _column(window, "BB_Middle"),
            "bb_lower": _column(window, "BB_Lower"),
            "vix": vix_values,
            # Monthly EMA levels as a staircase over the daily series.
            "m_ema8": overlay.get("m_ema8", []),
            "m_ema12": overlay.get("m_ema12", []),
            "golden_cross": _crosses(window, "Golden_Cross"),
            "death_cross": _crosses(window, "Death_Cross"),
        },
        "weekly": {
            "dates": _dates(weekly.index),
            "ohlc": _ohlc(weekly),
            "close": _column(weekly, "Close"),
            "sma10": _column(weekly, "SMA_10"),
            "sma12": _column(weekly, "SMA_12"),
            "sma30": _column(weekly, "SMA_30"),
        },
        "monthly": monthly_block,
        "monthly_status": monthly_status,
        "fib": fib,
        "signals": _all_signals(
            window, fib, monthly_block,
            dates[0] if dates else None, dates[-1] if dates else None,
            language,
        ),
    }


def _all_signals(window, fib, monthly_block, first_date, last_date, language):
    """Daily and monthly signals in one list, sorted by date.

    The monthly series covers the full history -- the monthly chart has a zoom
    of its own. For the signal list, though, only what lies inside the chosen
    period counts, otherwise it would hold dates that are not visible in the
    chart.
    """
    found = signals.detect(window, fib, language)
    if monthly_block:
        found += _monthly_signals(
            monthly_block["dates"], monthly_block["close"],
            monthly_block["ema8"], monthly_block["ema12"],
            monthly_block["running"], first_date, last_date, language,
        )
        found.sort(key=lambda s: (s["date"], s["kind"]))
    return found
