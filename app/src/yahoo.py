"""Price data from Yahoo Finance.

Deliberately straight against the public chart/search endpoints rather than
through yfinance: no sign-in needed, one dependency fewer, and the response
structure has been stable for years. The only requirement is a browser-like
user agent -- without one Yahoo answers with 429.
"""

import logging
import re
import time

import httpx

from . import db

log = logging.getLogger("chart.yahoo")

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# One client for the whole process, so that the TLS connection to Yahoo is
# kept and reused. Previously every request opened a client of its own and
# with it a new connection and TLS handshake -- per chart up to four of them
# in a row. httpx.Client is safe to share between the worker threads.
client = httpx.Client(timeout=25, headers=HEADERS, follow_redirects=True,
                      limits=httpx.Limits(max_connections=8,
                                          max_keepalive_connections=4,
                                          keepalive_expiry=60))

# Allowed are letters, digits and the characters Yahoo uses in symbols: dot
# (BMW.DE), hyphen (BTC-USD), caret (^VIX), equals sign (EURUSD=X).
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-^=]{1,20}$")


class YahooError(RuntimeError):
    """A failure that is worth showing to the visitor.

    The message itself is English and serves the log. What the browser gets to
    read is built from `key` and `params` in the visitor's language (see
    i18n.py); without a key the English message is passed through, which is
    the right thing for anything that only ever appears in the log.
    """

    def __init__(self, message: str, key: str | None = None, **params):
        super().__init__(message)
        self.key = key
        self.params = params


class EmptyRange(YahooError):
    """The chosen window contains nothing.

    A separate class because it changes the status code: an empty selection is
    an input by the visitor, not an outage at Yahoo -- 400 rather than 502.
    Previously main.py told the two apart by searching the German message for
    the word "Zeitraum", which quietly broke as soon as the wording changed.
    """


def valid_symbol(symbol: str) -> bool:
    return bool(SYMBOL_RE.match(symbol))


def market_is_open(meta: dict) -> bool:
    """Rough estimate of whether the last bar is still moving."""
    now = time.time()
    start = meta.get("currentTradingPeriod", {}).get("regular", {}).get("start")
    end = meta.get("currentTradingPeriod", {}).get("regular", {}).get("end")
    if not start or not end:
        return False
    return start <= now <= end


def fetch(symbol: str, interval: str, *, period1: int | None = None,
          period2: int | None = None, rng: str | None = None) -> dict:
    """A single request to Yahoo -- without any caching.

    Either a time window (period1/period2, to the second) or a named period
    (range). Caching is store.py's job; it manages individual bars rather than
    whole responses.
    """
    if not valid_symbol(symbol):
        raise YahooError(f"Invalid symbol: {symbol!r}", "error.invalid_symbol")

    params = {"interval": interval, "includePrePost": "false", "events": "div,split"}
    if rng is not None:
        params["range"] = rng
    else:
        params["period1"] = int(period1)
        params["period2"] = int(period2)

    try:
        resp = client.get(CHART_URL.format(symbol=symbol), params=params)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise YahooError(
            f"Yahoo request failed: {exc}", "error.yahoo_failed", detail=str(exc)
        ) from exc

    error = (body.get("chart") or {}).get("error")
    if error:
        description = error.get("description")
        if description:
            # Yahoo's own wording -- English, and passed through unchanged.
            raise YahooError(description)
        raise YahooError("Unknown error from Yahoo", "error.yahoo_unknown")

    results = (body.get("chart") or {}).get("result") or []
    if not results:
        raise YahooError(f"No data for {symbol}", "error.no_data", symbol=symbol)
    return _validated(symbol, results[0])


# Outermost bounds for a timestamp. Generous downwards -- for old values Yahoo
# delivers data back to 1962, hence negative epoch times. Upwards a barrier
# against nonsense. Both sit safely inside the range pandas can represent with
# nanoseconds (1677 to 2262); a value outside that used to end in an
# OutOfBoundsDatetime.
TS_MIN = -3_000_000_000   # roughly 1875
TS_MAX = 4_000_000_000    # roughly 2096


def _validated(symbol: str, result: dict) -> dict:
    """Makes sure the response has the expected shape.

    Without this check, corrupt responses did not come through as a
    YahooError, but as IndexError, ValueError, AttributeError or
    OutOfBoundsDatetime -- and thus as an HTTP 500 instead of a 502 with
    readable text (verified on 2026-09-05 with injected responses).

    Only the shape is checked, not the content: that meta is an object and the
    timestamps are numbers in a plausible range. The price values themselves
    are store._number()'s business; it discards each individual one that is
    not a number -- the series then stays usable instead of dropping out
    entirely.
    """
    if not isinstance(result, dict):
        raise YahooError(
            f"Unexpected response for {symbol}", "error.unexpected_response",
            symbol=symbol,
        )

    meta = result.get("meta")
    if meta is None:
        result["meta"] = {}
    elif not isinstance(meta, dict):
        # Has arrived as a list; every later meta.get() then ran into an
        # AttributeError. The rest of the response stays usable regardless.
        log.warning("meta of %s is %s instead of dict -- discarded",
                    symbol, type(meta).__name__)
        result["meta"] = {}

    stamps = result.get("timestamp")
    if stamps is None:
        return result
    if not isinstance(stamps, list):
        raise YahooError(
            f"Unexpected timestamps for {symbol}", "error.unexpected_timestamps",
            symbol=symbol,
        )

    checked = []
    for value in stamps:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise YahooError(
                f"Timestamp of {symbol} is not a number: {value!r}",
                "error.timestamp_not_a_number", symbol=symbol, value=repr(value),
            )
        if not TS_MIN <= value <= TS_MAX:
            raise YahooError(
                f"Timestamp of {symbol} out of range: {value!r}",
                "error.timestamp_out_of_range", symbol=symbol, value=repr(value),
            )
        checked.append(int(value))
    result["timestamp"] = checked
    return result


def search(query: str, limit: int = 8) -> list[dict]:
    """Symbol search for the autocompletion in the input field."""
    query = query.strip()
    if len(query) < 1:
        return []

    key = f"search|{query.lower()}|{limit}"
    cached = db.cache_get(key, 86400)
    if cached is not None:
        return cached

    try:
        resp = client.get(
            SEARCH_URL,
            params={"q": query, "quotesCount": limit, "newsCount": 0},
            timeout=10,
        )
        resp.raise_for_status()
        quotes = resp.json().get("quotes") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Symbol search failed: %s", exc)
        return []

    out = [
        {
            "symbol": q.get("symbol", ""),
            "name": q.get("longname") or q.get("shortname") or "",
            "exchange": q.get("exchDisp") or q.get("exchange") or "",
            "type": q.get("typeDisp") or q.get("quoteType") or "",
        }
        for q in quotes
        if q.get("symbol") and q.get("isYahooFinance")
    ]
    db.cache_put(key, out)
    return out
