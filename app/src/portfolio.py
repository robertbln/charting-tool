"""Portfolio Tracker: following the published trades of one trader.

Many traders publish their buys and sells as short texts -- in a newsletter,
a community, a channel ("I'm adding; 30 shares of AAPL at an average cost of
$190."). They are entered here by hand or pasted as a whole post, and this
module turns the journal into what one wants to know:
which positions are still open at what average cost, how they stand today,
and what the closed trades brought.

Rules of the bookkeeping:

  * Lots are matched first in, first out, as brokers do by default. Checked
    against a published portfolio: after partial sales the remaining lots
    matched it to the share.
  * A sell without a share count closes the whole position ("I'm closing my
    KO position at $62.40").
  * A trade without a price (posted without one, e.g. "QQQ closed") is
    valued at that day's close and marked as estimated, so the figure is
    there but is not mistaken for a posted one.
  * Selling more than is held only closes what is held; the rest is reported,
    not silently turned into a short position.
"""

import datetime
import logging
import re
import threading

import pandas as pd

from . import beta, series, store, yahoo

log = logging.getLogger("chart.portfolio")

SIDES = ("buy", "sell")


# --- Reading a post -------------------------------------------------------

_NUMBER = r"(\d[\d,]*(?:\.\d+)?)"
_SYMBOL = r"\$?([A-Z][A-Z0-9]{0,5}(?:\.[A-Z]{1,2})?)"

_SELL_WORDS = re.compile(
    r"\b(sell|selling|sold|trim|trimming|trimmed|clos(?:e|es|ing|ed)|"
    r"exit|exiting|exited|cut|cutting|reduc\w*|tak(?:e|ing) (?:some )?profits?|took (?:some )?profits?)\b",
    re.I)
_BUY_WORDS = re.compile(
    r"\b(buy|buying|bought|add|adding|added|open|opening|opened|"
    r"initiat\w*|starter|increas\w*|enter\w*|purchas\w*)\b", re.I)

# "30 shares of AAPL at an average cost of $190"
_BY_SHARES = re.compile(
    _NUMBER + r"\s+shares?\s+(?:of\s+)?" + _SYMBOL + r"\b(?:[^$\d]*?\$\s?" + _NUMBER + r")?")
# "$10,000 of XOM at $110" / "$10k of XOM at $110"
_BY_AMOUNT = re.compile(
    r"\$\s?" + _NUMBER + r"\s*(k)?\s+(?:worth\s+)?(?:of\s+)?" + _SYMBOL
    + r"\b[^$\d]*?\$\s?" + _NUMBER, re.I)
# "200 KO at $62" -- the count without the word "shares"
_BY_COUNT = re.compile(
    _NUMBER + r"\s+" + _SYMBOL + r"\s+at\s+\$?\s?" + _NUMBER)
# Any ticker-like word: "closing my KO position at $62.40", "QQQ closed"
_TICKER = re.compile(r"(?<![\w$])\$?([A-Z]{1,6}(?:\.[A-Z]{1,2})?)\b")
_PRICE = re.compile(r"\$\s?" + _NUMBER)

# Capitals that are words, not tickers.
_NOT_TICKERS = {"I", "A", "AM", "PM", "ET", "EST", "EDT", "US", "USD", "OK",
                "AI", "CEO", "IPO", "ETF", "ATH", "EPS", "FOMC", "CPI"}


def _num(text: str | None) -> float | None:
    return float(text.replace(",", "")) if text else None


def _by_amount(m) -> dict | None:
    price = _num(m.group(4))
    if not price:
        return None
    amount = _num(m.group(1)) * (1000 if m.group(2) else 1)
    return {"symbol": m.group(3), "shares": round(amount / price, 4), "price": price}


# Most specific first: the count-without-"shares" form would also match
# inside "30 shares of AMD at ...", were it not tried after it.
_PATTERNS = (
    (_BY_SHARES, lambda m: {"symbol": m.group(2), "shares": _num(m.group(1)),
                            "price": _num(m.group(3))}),
    (_BY_AMOUNT, _by_amount),
    (_BY_COUNT, lambda m: {"symbol": m.group(2), "shares": _num(m.group(1)),
                           "price": _num(m.group(3))}),
)


def parse_post(text: str) -> dict:
    """Pulls the trades out of a pasted post.

    The direction of a line is set by the last buy or sell word seen, since
    he lists several purchases under a single "I'm adding;". Sentences that
    name a ticker but yield no trade come back as "unparsed", so the reader
    sees what was skipped instead of trusting a silent gap.
    """
    trades, unparsed = [], []
    side = None
    # Sentences end at ".", ";" or "!" followed by whitespace; a decimal
    # point is never followed by a space, so "$62.40." splits correctly.
    pieces = re.split(r"(?<=[.;!:])\s+|\n+", text or "")
    for raw in pieces:
        line = raw.strip()
        if not line:
            continue
        sell, buy = _SELL_WORDS.search(line), _BUY_WORDS.search(line)
        if sell and (not buy or sell.start() < buy.start()):
            side = "sell"
        elif buy:
            side = "buy"

        # All patterns run; a match overlapping an earlier one is dropped, so
        # "72 shares of MSFT at $410 and 200 KO at $62" yields both.
        found, taken = [], []
        for pattern, convert in _PATTERNS:
            for m in pattern.finditer(line):
                trade = convert(m)
                if (trade is None or trade["symbol"] in _NOT_TICKERS
                        or any(m.start() < end and start < m.end() for start, end in taken)):
                    continue
                taken.append(m.span())
                found.append(trade)
        tickers = [m.group(1) for m in _TICKER.finditer(line)
                   if m.group(1) not in _NOT_TICKERS]
        if not found and side == "sell" and sell and len(tickers) == 1:
            # A sell naming one ticker and no count closes the position.
            price = _PRICE.search(line)
            found.append({"symbol": tickers[0], "shares": None,
                          "price": _num(price.group(1)) if price else None})

        if found and side:
            for trade in found:
                trades.append(dict(trade, side=side))
        elif tickers:
            # Names an instrument but yields no trade: show it, do not guess.
            unparsed.append(line[:200])
    return {"trades": trades, "unparsed": unparsed}


# --- Prices ---------------------------------------------------------------

class _Closes:
    """Closing prices per symbol and day, fetched once per request."""

    def __init__(self):
        self._frames: dict[str, tuple[str, pd.Series]] = {}

    def on(self, symbol: str, day: str) -> float | None:
        """Close of that day, or of the last trading day before it."""
        # Ten days of lead, so a weekend or holiday still finds the day before.
        since = (pd.Timestamp(day) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        cached = self._frames.get(symbol)
        if cached is None or cached[0] > since:
            try:
                start = int(pd.Timestamp(since, tz="UTC").timestamp())
                closes = store.load(symbol, "1d", start).frame["Close"].dropna()
            except yahoo.YahooError as exc:
                log.warning("No closes for %s: %s", symbol, exc)
                closes = pd.Series(dtype="float64")
            cached = self._frames[symbol] = (since, closes)
        closes = cached[1]
        upto = closes[closes.index.strftime("%Y-%m-%d") <= day]
        return float(upto.iloc[-1]) if not upto.empty else None


# --- Bookkeeping ----------------------------------------------------------

def ledger(rows: list[dict]) -> dict:
    """Open lots and realized results from the journal, without today's prices.

    rows: the trades as stored, in any order. Returned trades carry the price
    actually used and whether it was estimated.
    """
    closes = _Closes()
    ordered = sorted(rows, key=lambda r: (r["trade_date"], r["id"]))
    lots: dict[str, list[list]] = {}        # symbol -> [[shares, price, date], ...]
    first_buy: dict[str, str] = {}
    realized, trades, warnings = [], [], []

    for row in ordered:
        symbol, day = row["symbol"], row["trade_date"]
        price, estimated = row["price"], False
        if price is None:
            price = closes.on(symbol, day)
            estimated = True
        trade = dict(row, price_used=price, estimated=estimated)
        trades.append(trade)
        if price is None:
            warnings.append({"key": "pf.warn_no_price", "symbol": symbol, "date": day})
            continue

        held = lots.setdefault(symbol, [])
        if row["side"] == "buy":
            if not held:
                first_buy[symbol] = day
            held.append([row["shares"], price, day])
            continue

        # Sell: FIFO over the open lots.
        owned = sum(lot[0] for lot in held)
        if owned <= 1e-9:
            # Most likely the buy was never entered -- say so, do not guess.
            warnings.append({"key": "pf.warn_not_held", "symbol": symbol, "date": day})
            continue
        wanted = owned if row["shares"] is None else row["shares"]
        if wanted > owned + 1e-9:
            warnings.append({"key": "pf.warn_oversold", "symbol": symbol, "date": day,
                             "wanted": wanted, "owned": owned})
            wanted = owned
        trade["shares_used"] = wanted
        left, cost = wanted, 0.0
        while left > 1e-9 and held:
            lot = held[0]
            take = min(lot[0], left)
            cost += take * lot[1]
            lot[0] -= take
            left -= take
            if lot[0] <= 1e-9:
                held.pop(0)
        proceeds = wanted * price
        realized.append({
            "id": row["id"], "symbol": symbol, "date": day, "shares": wanted,
            "avg_cost": cost / wanted, "price": price, "estimated": estimated,
            "profit": proceeds - cost,
            "profit_pct": (proceeds - cost) / cost * 100 if cost else None,
        })

    positions = []
    for symbol, held in lots.items():
        shares = sum(lot[0] for lot in held)
        if shares <= 1e-9:
            continue
        invested = sum(lot[0] * lot[1] for lot in held)
        positions.append({
            "symbol": symbol, "shares": shares, "avg_cost": invested / shares,
            "invested": invested, "since": first_buy.get(symbol),
        })
    return {"positions": positions, "realized": realized,
            "trades": trades, "warnings": warnings}


def summary(rows: list[dict]) -> dict:
    """The ledger plus today's prices: what the open positions stand at."""
    book = ledger(rows)
    totals = {"invested": 0.0, "value": 0.0, "unrealized": 0.0, "day": 0.0,
              "realized": sum(r["profit"] for r in book["realized"]),
              "missing": 0}
    def fetch(pos):
        try:
            return series.quote(pos["symbol"])
        except yahoo.YahooError as exc:
            # One unavailable symbol must not take the whole list down.
            log.warning("Quote for %s not available: %s", pos["symbol"], exc)
            return None

    quotes = store.parallel(fetch, book["positions"])
    for pos, quote in zip(book["positions"], quotes):
        if quote is None:
            pos["error"] = True
            totals["missing"] += 1
            continue
        last, change = quote["last"], quote["change_pct"]
        pos.update(last=last, change_pct=change, currency=quote["currency"],
                   stale=quote["stale"])
        if last is None:
            totals["missing"] += 1
            continue
        pos["value"] = pos["shares"] * last
        pos["unrealized"] = pos["value"] - pos["invested"]
        pos["unrealized_pct"] = pos["unrealized"] / pos["invested"] * 100
        if change is not None:
            previous = last / (1 + change / 100)
            # Bought today: the day's move counts from the purchase.
            base = pos["avg_cost"] if pos["since"] == _today() else previous
            pos["day"] = pos["shares"] * (last - base)
            totals["day"] += pos["day"]
        totals["invested"] += pos["invested"]
        totals["value"] += pos["value"]
        totals["unrealized"] += pos["unrealized"]

    # The depot's beta: each position's beta weighted by its value. Positions
    # without a beta (too young) drop out of numerator and denominator alike.
    weighted, covered = 0.0, 0.0
    # One after the other on purpose, unlike the quotes above: every beta also
    # loads the benchmark, and four threads would fetch and write ^GSPC at
    # the same time. beta.get() caches per day, so this costs once a day.
    for pos in book["positions"]:
        if "value" in pos and totals["value"]:
            pos["weight"] = pos["value"] / totals["value"] * 100
        pos["beta"] = beta.get(pos["symbol"])
        if pos["beta"] and "value" in pos:
            weighted += pos["beta"]["value"] * pos["value"]
            covered += pos["value"]
    totals["beta"] = weighted / covered if covered else None
    book["positions"].sort(key=lambda p: -p.get("unrealized", float("-inf")))
    totals["unrealized_pct"] = (
        totals["unrealized"] / totals["invested"] * 100 if totals["invested"] else None)
    totals["total"] = totals["unrealized"] + totals["realized"]
    totals.update(_irr(book, totals))
    totals.update(_ttwror(book))
    book["totals"] = totals
    return book


def xirr(flows: list[tuple[datetime.date, float]]) -> float | None:
    """Annual internal rate of return of dated cash flows (Excel's XIRR).

    Solved by bisection on sum(cf / (1 + r) ** (days / 365)) = 0. Slower
    than Newton but it cannot run off, and a few dozen flows cost nothing.
    Without both an outflow and an inflow there is no rate to find.
    """
    if not any(cf < 0 for _, cf in flows) or not any(cf > 0 for _, cf in flows):
        return None
    start = min(day for day, _ in flows)

    def value(rate: float) -> float:
        return sum(cf / (1 + rate) ** ((day - start).days / 365) for day, cf in flows)

    low, high = -0.9999, 1.0
    # Widen upwards until the sign changes -- short periods annualise to
    # large rates.
    while value(high) > 0 and high < 1e9:
        high *= 10
    if value(low) * value(high) > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2
        if value(low) * value(mid) <= 0:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def _irr(book: dict, totals: dict) -> dict:
    """IZF of the whole journal: buys out, sells in, today's value as the end.

    A position without a price today enters with its cost, so it neither
    flatters nor spoils the rate; totals["missing"] already says so.
    """
    flows = []
    for trade in book["trades"]:
        if trade["side"] == "buy" and trade["price_used"] is not None:
            flows.append((datetime.date.fromisoformat(trade["trade_date"]),
                          -trade["shares"] * trade["price_used"]))
    for sale in book["realized"]:
        flows.append((datetime.date.fromisoformat(sale["date"]), sale["shares"] * sale["price"]))
    today = datetime.date.today()
    open_value = sum(p.get("value", p["invested"]) for p in book["positions"])
    if open_value:
        flows.append((today, open_value))
    rate = xirr(flows)
    if rate is None:
        return {"irr": None}
    start = min(day for day, _ in flows)
    days = (today - start).days
    return {
        "irr": rate * 100,
        # What the rate amounts to over the period itself, not per year.
        "irr_period": ((1 + rate) ** (days / 365) - 1) * 100,
        "irr_since": start.isoformat(),
        "irr_days": days,
    }


def _ttwror(book: dict) -> dict:
    """True time-weighted rate of return, as Portfolio Performance shows it.

    The depot is valued at every close from the first buy on. Each day's
    return is measured with that day's money moved in or out taken out of
    it, and the days are chained:

        r_t = (V_t + sold_t) / (V_t-1 + bought_t) - 1
        TTWROR = prod(1 + r_t) - 1

    Buys count as made at the start of the day, sells at its end -- so the
    first day of a position already earns from its purchase price to the
    close. Unlike the IZF the result does not depend on when how much was
    put in: it measures the picks, not the timing of the capital.
    """
    trades = [tr for tr in book["trades"] if tr["price_used"] is not None
              and (tr["side"] == "buy" or tr.get("shares_used"))]
    if not trades:
        return {"ttwror": None}
    first = min(tr["trade_date"] for tr in trades)
    since = int((pd.Timestamp(first, tz="UTC") - pd.Timedelta(days=10)).timestamp())

    # One missing symbol already leaves the TTWROR out. The rest are then not
    # fetched any more -- during a Yahoo outage that would only add load.
    failed = threading.Event()

    def load(symbol):
        if failed.is_set():
            return None
        try:
            return store.load(symbol, "1d", since).frame["Close"].dropna()
        except yahoo.YahooError as exc:
            log.warning("No closes for %s, TTWROR left out: %s", symbol, exc)
            failed.set()
            return None

    symbols = sorted({tr["symbol"] for tr in trades})
    frames = store.parallel(load, symbols)
    if any(frame is None for frame in frames):
        return {"ttwror": None}
    closes = {}
    for symbol, frame in zip(symbols, frames):
        frame.index = frame.index.strftime("%Y-%m-%d")
        closes[symbol] = frame[~frame.index.duplicated(keep="last")]
    prices = pd.DataFrame(closes).sort_index().ffill()
    days = [day for day in prices.index if day >= first]
    if not days:
        return {"ttwror": None}

    # A trade on a weekend or holiday belongs to the next trading day.
    def trading_day(date: str) -> str:
        return next((day for day in days if day >= date), days[-1])

    bought, sold = {}, {}
    moves: dict[str, list[tuple[str, float]]] = {}
    for tr in trades:
        day = trading_day(tr["trade_date"])
        if tr["side"] == "buy":
            amount, delta = tr["shares"] * tr["price_used"], tr["shares"]
            bought[day] = bought.get(day, 0.0) + amount
        else:
            amount, delta = tr["shares_used"] * tr["price_used"], -tr["shares_used"]
            sold[day] = sold.get(day, 0.0) + amount
        moves.setdefault(day, []).append((tr["symbol"], delta))

    held: dict[str, float] = {}
    growth, previous = 1.0, 0.0
    for day in days:
        for symbol, delta in moves.get(day, []):
            held[symbol] = held.get(symbol, 0.0) + delta
        value = sum(shares * prices.at[day, symbol]
                    for symbol, shares in held.items() if shares > 1e-9)
        base = previous + bought.get(day, 0.0)
        if base > 0:
            growth *= (value + sold.get(day, 0.0)) / base
        previous = value

    span = (datetime.date.today() - datetime.date.fromisoformat(first)).days
    total = growth - 1
    return {
        "ttwror": float(total * 100),
        "ttwror_pa": float(((growth ** (365 / span)) - 1) * 100) if span > 0 else None,
    }


def _today() -> str:
    return datetime.date.today().isoformat()
