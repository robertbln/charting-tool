"""Free cash flow from Yahoo Finance.

Straight from the fundamentals-timeseries endpoint that yfinance reads its
financials from as well -- same reasoning as in yahoo.py: no extra library,
and unlike quoteSummary it needs no cookie or crumb (verified 2026-10-15 on
AMD, PWR, SAP and BMW.DE). It covers non-US listings, which SEC EDGAR does
not, and needs no key, unlike FMP or Alpha Vantage.

Yahoo delivers the figure ready-made: operating cash flow minus capital
expenditure, as trailing twelve months (TTM), per fiscal year and per
quarter, in the currency the company reports in. Other providers draw the
line slightly differently (leases, for one), so a figure may differ from
StockAnalysis or the company's own "adjusted" number.

The result is kept for a day: it only changes with a new quarterly report.
"""

import logging
import time

import httpx

from . import db, yahoo

log = logging.getLogger("chart.fundamentals")

URL = "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{symbol}"
TYPES = ("trailingFreeCashFlow", "annualFreeCashFlow",
         "trailingOperatingCashFlow", "trailingCapitalExpenditure")
CACHE_SECONDS = 86400
YEARS = 6


def _series(result: dict) -> dict[str, list[dict]]:
    """type -> [{"date", "value", "currency"}, ...], oldest first, gaps dropped."""
    out: dict[str, list[dict]] = {}
    for entry in result.get("timeseries", {}).get("result") or []:
        kind = ((entry.get("meta") or {}).get("type") or [None])[0]
        if kind not in TYPES:
            continue
        points = []
        for point in entry.get(kind) or []:
            raw = ((point or {}).get("reportedValue") or {}).get("raw")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and point.get("asOfDate"):
                points.append({"date": str(point["asOfDate"])[:10], "value": float(raw),
                               "currency": str(point.get("currencyCode") or "")[:3]})
        out[kind] = sorted(points, key=lambda p: p["date"])
    return out


def free_cash_flow(symbol: str) -> dict | None:
    """The FCF of a symbol, or None where Yahoo has none (ETFs, indices, ...).

        {"ttm": 8.4e9, "date": "2026-06-30", "currency": "USD",
         "previous": 4.0e9,            # TTM a year earlier, if there is one
         "operating": 1.0e10, "capex": -1.7e9,
         "annual": [{"date": "2025-12-31", "value": 6.7e9}, ...]}
    """
    symbol = symbol.upper()
    if not yahoo.valid_symbol(symbol):
        return None
    key = f"fund|{symbol}"
    cached = db.cache_get(key, CACHE_SECONDS)
    if cached is not None:
        return cached.get("fcf")

    now = int(time.time())
    try:
        resp = yahoo.client.get(URL.format(symbol=symbol), params={
            "type": ",".join(TYPES),
            "period1": now - YEARS * 365 * 86400, "period2": now,
        }, timeout=10)
        resp.raise_for_status()
        series = _series(resp.json())
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        # Not cached: the next call tries again.
        log.warning("Fundamentals for %s not available: %s", symbol, exc)
        return None

    ttm = series.get("trailingFreeCashFlow") or []
    if not ttm:
        db.cache_put(key, {"fcf": None})
        return None
    latest = ttm[-1]

    def same_date(kind):
        hit = [p for p in series.get(kind) or [] if p["date"] == latest["date"]]
        return hit[0]["value"] if hit else None

    # The TTM of a year before, for the change. Yahoo sometimes skips years
    # (BMW.DE: 2022, then 2025), so only a value 11 to 13 months older counts.
    previous = None
    if len(ttm) > 1:
        months = (int(latest["date"][:4]) - int(ttm[-2]["date"][:4])) * 12 \
            + int(latest["date"][5:7]) - int(ttm[-2]["date"][5:7])
        if 11 <= months <= 13:
            previous = ttm[-2]["value"]

    fcf = {
        "ttm": latest["value"],
        "date": latest["date"],
        "currency": latest["currency"],
        "previous": previous,
        "operating": same_date("trailingOperatingCashFlow"),
        "capex": same_date("trailingCapitalExpenditure"),
        "annual": [{"date": p["date"], "value": p["value"]}
                   for p in (series.get("annualFreeCashFlow") or [])[-4:]],
    }
    db.cache_put(key, {"fcf": fcf})
    return fcf
