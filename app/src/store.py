"""Price store that reloads only the missing stretches.

Instead of fetching the whole series from Yahoo on every call, each bar sits
once in the table "bars". On a further call we compare what is already there
and load only the gap:

    present:   |=================|
    needed:    |===========================|
    loaded:                      |=========|   <- this part only

That applies to both edges. Switching the period from 1 to 2 years loads only
the missing first year; opening the same chart the next day loads only the new
days.

Two things have to hold for that:

  * A stock split changes all prices retroactively. Every reload therefore
    overlaps the last bars of the store. If a closing price differs there, the
    whole store for that symbol is invalid and is reloaded.
  * A symbol has a first trading day. Without a memory we would ask for the
    period before it on every call -- which is what coverage.asked_first is
    for: it remembers how far back we have already asked.
"""

import datetime
import json
import logging
import math
import time

import pandas as pd

from . import config, db, yahoo

log = logging.getLogger("chart.store")

DAY = 86_400

# How far a reload reaches into the store. Five bars suffice to notice a
# retroactive change (split, correction) reliably.
OVERLAP_BARS = 5

# From this relative deviation of an overlapping closing price on, the store
# counts as superseded. 0.1 percent is far above rounding noise and far below
# any real split.
DRIFT_TOLERANCE = 0.001


class Result:
    """Result of a query, plus what had to be reloaded for it."""

    def __init__(self, frame, meta, requests, new_bars, stale):
        self.frame = frame
        self.meta = meta
        self.requests = requests      # number of requests to Yahoo
        self.new_bars = new_bars      # number of newly stored bars
        self.stale = stale            # Yahoo unreachable, store used instead


def _normalize_ts(interval: str, ts: int) -> int:
    """Snaps a timestamp to the beginning of its bar.

    Needed because Yahoo appends the running price as an extra bar carrying
    the current time -- verified on MSTR: next to the weekly bar
    "2026-08-31 04:00" came a second entry "2026-09-04 15:07". Without
    normalisation another phantom bar accumulated with every fetch.
    Yahoo also shifts the time across summer and winter time, which would
    create the same trading day twice.
    """
    ts = int(ts)
    if interval == "1mo":
        # First of the month, 00:00 UTC. Verified on MSTR: next to the monthly
        # bar "2026-09-01 04:00" came a live entry "2026-09-04 16:36".
        moment = datetime.datetime.fromtimestamp(ts, datetime.UTC)
        first = datetime.datetime(moment.year, moment.month, 1, tzinfo=datetime.UTC)
        return int(first.timestamp())
    if interval == "1wk":
        # Monday 00:00 UTC of the respective week. 1970-01-01 was a Thursday,
        # so the first Monday was 1970-01-05 = 4 * DAY -- hence subtract
        # first, then add back. With the signs swapped (adding first) one ends
        # up on the Sunday before: Monday 2026-08-31 became 2026-08-30.
        return ((ts - 4 * DAY) // (7 * DAY)) * (7 * DAY) + 4 * DAY
    return (ts // DAY) * DAY


def _number(value):
    """Turns a value from the Yahoo response into a number -- or None.

    Sounds superfluous, is not: SQLite has type affinity, not type checking. A
    text that cannot be converted losslessly into a number stays in the column
    "close REAL" as text -- measured on 2026-09-05: typeof(close) = 'text'.
    Since db.cleanup() deliberately never tidies the bars table (it IS the
    store), a single such bar poisoned the symbol permanently: every further
    call ended in a 500 until somebody reached into the database by hand.

    NaN and Inf go out with it. Neither survives json.dumps() as valid JSON,
    and as a price neither makes sense anyway.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _merge_bars(interval: str, timestamps, quote) -> dict[int, tuple]:
    """Collapses the bars of a response per period.

    If the appended live bar falls into a period that is already present, it
    is merged with it: open from the first, high and low as extremes, close
    and volume from the most recent entry.

    All values run through _number(). The loop also only runs as far as the
    SHORTEST list reaches: Yahoo has delivered responses in which timestamp
    and close had different lengths, which used to end here in an IndexError
    -- and therefore in a 500 rather than a 502.
    """
    def col(name):
        values = quote.get(name)
        return values if isinstance(values, list) else []

    opens, highs, lows, closes, volumes = (
        col("open"), col("high"), col("low"), col("close"), col("volume")
    )
    length = min(len(timestamps), len(opens), len(highs),
                 len(lows), len(closes), len(volumes))

    merged: dict[int, tuple] = {}
    for i in range(length):
        raw = timestamps[i]
        close_i = _number(closes[i])
        if close_i is None:
            continue
        opens_i, highs_i = _number(opens[i]), _number(highs[i])
        lows_i, volumes_i = _number(lows[i]), _number(volumes[i])
        key = _normalize_ts(interval, raw)
        current = merged.get(key)
        if current is None:
            merged[key] = (opens_i, highs_i, lows_i, close_i, volumes_i)
            continue
        o, h, l, c, v = current
        merged[key] = (
            o if o is not None else opens_i,
            max([x for x in (h, highs_i) if x is not None], default=None),
            min([x for x in (l, lows_i) if x is not None], default=None),
            close_i,
            max([x for x in (v, volumes_i) if x is not None], default=None),
        )
    return merged


# --- Access to the tables -------------------------------------------------

def _coverage(symbol: str, interval: str):
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT * FROM coverage WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        ).fetchone()
    return dict(row) if row else None


def _save_bars(symbol: str, interval: str, result: dict,
               max_ts: int | None = None) -> int:
    """Writes the bars of a Yahoo response and returns how many there were.

    max_ts limits them to the gap actually asked for. Needed when reloading
    backwards: Yahoo aggregates weekly bars within the requested window, and a
    bar cut off at the edge would carry wrong high, low and close values.
    """
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    if not timestamps:
        return 0

    rows = [
        (symbol, interval, ts, o, h, l, c, v)
        for ts, (o, h, l, c, v) in sorted(_merge_bars(interval, timestamps, quote).items())
        if max_ts is None or ts <= max_ts
    ]

    if rows:
        with db.cursor() as conn:
            conn.executemany(
                "INSERT INTO bars (symbol, interval, ts, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol, interval, ts) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume",
                rows,
            )
    return len(rows)


def _stored_closes(symbol: str, interval: str, since: int) -> dict[int, float]:
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT ts, close FROM bars WHERE symbol = ? AND interval = ? AND ts >= ?",
            (symbol, interval, since),
        ).fetchall()
    return {r["ts"]: r["close"] for r in rows}


def _drifted(symbol: str, interval: str, result: dict, newest: int | None,
             max_ts: int | None) -> bool:
    """Do overlapping bars differ from the store? Then there was a split.

    The most recent stored bar stays out of it: as long as the trading day or
    the week is running, its closing price changes on every fetch, entirely
    regularly. Without this exception every update during exchange hours would
    feign a split.
    """
    timestamps = result.get("timestamp") or []
    if not timestamps:
        return False

    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    merged = _merge_bars(interval, timestamps, quote)
    known = _stored_closes(symbol, interval, min(merged) if merged else 0)

    for ts, (_, _, _, close, _) in merged.items():
        # The bar cut off at the window edge and the still-running most recent
        # bar are no evidence of a split.
        if max_ts is not None and ts > max_ts:
            continue
        if newest is not None and ts >= newest:
            continue
        old = known.get(ts)
        if old is None or close is None or old == 0:
            continue
        if abs(close - old) / abs(old) > DRIFT_TOLERANCE:
            log.info(
                "%s/%s: price on %s changed from %.4f to %.4f -- "
                "reloading the store (presumably a split)",
                symbol, interval, ts, old, close,
            )
            return True
    return False


def _forget(symbol: str, interval: str) -> None:
    with db.cursor() as conn:
        conn.execute(
            "DELETE FROM bars WHERE symbol = ? AND interval = ?", (symbol, interval)
        )
        conn.execute(
            "DELETE FROM coverage WHERE symbol = ? AND interval = ?", (symbol, interval)
        )


def _update_coverage(symbol: str, interval: str, asked_first: int,
                     meta: dict, market_open: bool,
                     fetched_at: int | None = None) -> None:
    """Carry the bounds of the store forward.

    fetched_at states when the edge was really last fetched. Without it "now"
    applies. Skipping this value made the store count as fresh for up to
    twelve hours after a failed reload attempt, although the new bars were
    missing -- and a marginally backdated start time pushed the deadline
    further and further out without anything ever being fetched.
    """
    with db.cursor() as conn:
        bounds = conn.execute(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM bars "
            "WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        ).fetchone()
        previous = conn.execute(
            "SELECT asked_first FROM coverage WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        ).fetchone()
        earliest = min(asked_first, previous["asked_first"]) if previous else asked_first

        conn.execute(
            "INSERT INTO coverage (symbol, interval, asked_first, first_ts, last_ts, "
            "fetched_at, market_open, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, interval) DO UPDATE SET "
            "asked_first=excluded.asked_first, first_ts=excluded.first_ts, "
            "last_ts=excluded.last_ts, fetched_at=excluded.fetched_at, "
            "market_open=excluded.market_open, meta=excluded.meta",
            (symbol, interval, earliest, bounds["lo"], bounds["hi"],
             int(time.time()) if fetched_at is None else int(fetched_at),
             1 if market_open else 0, json.dumps(meta, separators=(",", ":"))),
        )


def _frame(symbol: str, interval: str, start_ts: int) -> pd.DataFrame:
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM bars "
            "WHERE symbol = ? AND interval = ? AND ts >= ? ORDER BY ts",
            (symbol, interval, start_ts),
        ).fetchall()

    frame = pd.DataFrame(
        [dict(r) for r in rows],
        columns=["ts", "open", "high", "low", "close", "volume"],
    )
    frame.index = pd.to_datetime(frame.pop("ts"), unit="s", utc=True)
    frame.columns = ["Open", "High", "Low", "Close", "Volume"]
    return frame


# --- Freshness ------------------------------------------------------------

def _session_start(cov: dict) -> int | None:
    """Start of the regular session that lay ahead at the last fetch, if any."""
    try:
        meta = json.loads(cov["meta"]) if cov.get("meta") else {}
        start = meta["currentTradingPeriod"]["regular"]["start"]
    except (ValueError, KeyError, TypeError):
        return None
    if isinstance(start, bool) or not isinstance(start, (int, float)):
        return None
    return int(start) if start > cov["fetched_at"] else None


def _is_current(cov: dict, now: int) -> bool:
    """Is the last stored bar young enough?

    A store fetched while the exchange was closed is only good until the next
    session opens, even if CACHE_TTL_CLOSED reaches further. Without that a
    US share opened at 9:00 in Berlin counted as current for twelve hours --
    and the whole session from 15:30 on was never fetched (seen in the access
    log, e.g. TSLA on 2026-09-14: fetched 09:00 UTC, shown at 18:03 UTC
    without a reload).
    """
    age = now - cov["fetched_at"]
    if cov["market_open"]:
        return age < config.CACHE_TTL_INTRADAY
    if age >= config.CACHE_TTL_CLOSED:
        return False
    start = _session_start(cov)
    return start is None or now < start


# --- Main access ----------------------------------------------------------

def load(symbol: str, interval: str, start_ts: int) -> Result:
    """Returns the series from start_ts on, reloading only what is missing."""
    symbol = symbol.upper()
    start_ts = max(0, int(start_ts))
    now = int(time.time())

    cov = _coverage(symbol, interval)
    requests = 0
    new_bars = 0
    stale = False
    meta = json.loads(cov["meta"]) if cov and cov["meta"] else {}

    # Which windows are missing? At the start possibly older bars, at the end
    # the ones that have arrived since the last fetch.
    step = _bar_seconds(interval)
    windows: list[tuple[int, int, int | None]] = []   # (from, to, cap when storing)
    if cov is None:
        windows.append((start_ts, now, None))
    elif cov["last_ts"] is None:
        # A symbol for which Yahoo delivered nothing. Without this check it was
        # requested again on every call -- and since the watchlist loads for
        # every entry, one dead symbol produced a request on every call.
        if not _is_current(cov, now):
            windows.append((start_ts, now, None))
    else:
        if start_ts < cov["asked_first"] - DAY:
            # Up to a whole bar beyond the start of the store, so that the
            # boundary bar is delivered complete; it is only stored up to that
            # bar though.
            windows.append((start_ts, cov["first_ts"] + step, cov["first_ts"] - 1))
        if not _is_current(cov, now):
            windows.append((cov["last_ts"] - OVERLAP_BARS * step, now, None))

    newest = cov["last_ts"] if cov else None

    for period1, period2, max_ts in windows:
        if period2 <= period1:
            continue
        try:
            result = yahoo.fetch(symbol, interval, period1=period1, period2=period2)
        except yahoo.YahooError as exc:
            if cov is None:
                raise
            # Yahoo is throttling or disturbed. The store is complete enough
            # to display -- only not freshly picked.
            log.warning("%s/%s: reload failed (%s)", symbol, interval, exc)
            stale = True
            break

        requests += 1
        if _drifted(symbol, interval, result, newest, max_ts):
            earliest = min(start_ts, cov["asked_first"]) if cov else start_ts

            # Fetch first, discard after. The other way round the store stood
            # empty for a moment -- and if the replacement fetch failed (Yahoo
            # likes to answer a second request in the same second with 429),
            # the entire history of the symbol was lost.
            try:
                full = yahoo.fetch(symbol, interval, period1=earliest, period2=now)
            except yahoo.YahooError as exc:
                log.warning(
                    "%s/%s: replacement fetch after a suspected split failed "
                    "(%s) -- the existing store stays in place",
                    symbol, interval, exc,
                )
                return Result(_frame(symbol, interval, start_ts), meta,
                              requests, new_bars, True)
            requests += 1

            if not (full.get("timestamp") or []):
                # An empty response is no reason to delete the store.
                log.warning("%s/%s: replacement fetch returned no bars",
                            symbol, interval)
                return Result(_frame(symbol, interval, start_ts), meta,
                              requests, new_bars, True)

            _forget(symbol, interval)
            new_bars += _save_bars(symbol, interval, full)
            meta = full.get("meta") or meta
            _update_coverage(symbol, interval, earliest, meta,
                             yahoo.market_is_open(meta))
            return Result(_frame(symbol, interval, start_ts), meta, requests,
                          new_bars, False)

        new_bars += _save_bars(symbol, interval, result, max_ts)
        meta = result.get("meta") or meta

    if requests:
        # Only if the current edge really arrived does the store count as
        # fresh. If the loop broke off after a partial success (stale), the
        # old timestamp stays and the next call tries again.
        _update_coverage(
            symbol, interval, start_ts, meta, yahoo.market_is_open(meta),
            fetched_at=(cov["fetched_at"] if stale and cov else None),
        )
    elif cov is not None and start_ts < cov["asked_first"]:
        # Nothing to fetch, but the wish reached further back than before --
        # remember that, so we do not try a second time. The deadline itself
        # stays untouched.
        _update_coverage(symbol, interval, start_ts, meta,
                         bool(cov["market_open"]),
                         fetched_at=cov["fetched_at"])

    return Result(_frame(symbol, interval, start_ts), meta, requests, new_bars, stale)


def _bar_seconds(interval: str) -> int:
    return {"1d": DAY, "1wk": 7 * DAY, "1mo": 31 * DAY}.get(interval, DAY)

