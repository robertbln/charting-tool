"""A self-hosted charting tool -- price charts with indicators, behind a sign-in.

Started in the container via uvicorn (see Dockerfile). Create a user with:

    docker compose exec chart python -m src.cli adduser NAME

The code is English throughout; the interface is German by default and can be
switched to English (see i18n.py and /lang below). Everything a visitor gets
to read comes from there, nothing is written inline.
"""

import datetime
import hmac
import json
import logging
import os
import sys
import threading
import time
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from starlette.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import audit, auth, beta, config, db, fundamentals, i18n, portfolio, notices, series, yahoo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("chart")

if not config.SECRET_KEY or len(config.SECRET_KEY) < 32:
    raise SystemExit(
        "SECRET_KEY is missing or too short. Set it in .env:\n"
        '  SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
    )

app = FastAPI(title=config.BRAND, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Compressed in the app rather than in the vhost: Apache's deflate.conf lists
# text/html, CSS and JavaScript, but not application/json -- every chart left
# the server uncompressed (1y: 182 KiB instead of 63, max: 4.9 MiB instead of
# 1.4; measured on 2026-11-09). Level 5 rather than Starlette's default 9:
# for a "max" chart that is a fraction of the time for nearly the same size.
# Apache passes a response that is already encoded through unchanged.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


def brand_parts() -> tuple[str, str]:
    """config.BRAND split for the two weights the template renders it in.

    Up to the first dot goes into the strong part, the dot and everything
    after it into the muted one. A name without a dot stays in one piece.
    """
    head, dot, tail = config.BRAND.partition(".")
    return head, dot + tail


def _asset_version() -> str:
    """Identifier for the static files, from their modification time.

    Without it the browser keeps serving its cached app.js after a rebuild --
    the page itself is dynamic and therefore always fresh. Old and new then no
    longer fit together: an element the new template no longer contains makes
    the old script break off with "Cannot read properties of null", and it
    does so before the first API call. That shows up as an empty page without
    favorites and without a chart. Exactly that happened on 2026-09-05, when
    the "Anzeigen" button was dropped.
    """
    newest = 0.0
    for name in ("static/app.js", "static/admin.js", "static/style.css", "static/favicon.png",
                 "static/favicon.ico", "static/apple-touch-icon.png"):
        try:
            newest = max(newest, os.path.getmtime(name))
        except OSError:
            pass
    return str(int(newest))


ASSET_VERSION = _asset_version()

db.init()
db.migrate()
db.cleanup()
audit.init()


def _backfill_last_login() -> None:
    """users.last_login from the access logs, for sign-ins made before the
    column existed (2026-10-16). Only ever moves a value forward; from then
    on auth.verify() keeps it current itself."""
    try:
        latest = audit.last_logins()
    except Exception:
        log.exception("Reading the last sign-ins from the access log failed")
        return
    with db.cursor() as conn:
        conn.executemany(
            "UPDATE users SET last_login = ? WHERE username = ? "
            "AND (last_login IS NULL OR last_login < ?)",
            [(ts, name, ts) for name, ts in latest.items()])


_backfill_last_login()


def _cleanup_loop() -> None:
    """Clears expired sessions and old failed attempts every hour.

    db.cleanup() used to run only at startup. Between two rebuilds
    login_attempts therefore grew without bound -- with no effect on the
    lockout, which filters by timestamp, but the table never got smaller
    again.

    A thread of its own suffices: db.cursor() opens its own SQLite connection
    per call, so there is nothing to share. daemon=True, so that it does not
    hold up shutting the container down.
    """
    while True:
        time.sleep(3600)
        try:
            db.cleanup()
        except Exception:
            log.exception("Periodic clean-up failed")


threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup").start()

# Everything is served locally, so the policy gets by without 'unsafe-*' and
# without foreign sources.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def client_ip(request: Request) -> str:
    """The visitor's real address.

    Deliberately X-Real-IP ONLY, which the vhost writes with
    "RequestHeader set" -- "set" replaces a value sent along, so the client
    cannot get past it.

    X-Forwarded-For is expressly NOT evaluated any more: mod_proxy appends the
    real address to an existing header instead of replacing it. Whoever sends
    "X-Forwarded-For: 1.2.3.4" themselves therefore stands in first place. The
    first entry read here previously was thus freely choosable -- and with it
    the key auth.is_locked() counts against. The lockout after five failed
    attempts could be bypassed entirely with a different header per request
    (verified on 2026-09-05).

    If the header is missing, the request is not going through the vhost --
    then the address of the connection itself will do.
    """
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    return request.client.host if request.client else "?"


def current_user(request: Request):
    return auth.lookup_session(request.cookies.get(auth.SESSION_COOKIE))


def require_user(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail=i18n.translate(i18n.from_request(request), "error.not_signed_in"),
        )
    # Signed in but in no group: the session may predate a change in the
    # user management. Nothing is shown.
    if not auth.can(user, "app"):
        raise HTTPException(
            status_code=403,
            detail=i18n.translate(i18n.from_request(request), "error.no_access"),
        )
    return user


def require(permission: str):
    """Dependency: a signed-in user who also holds `permission`."""
    def check(request: Request, user=Depends(require_user)):
        if not auth.can(user, permission):
            raise HTTPException(
                status_code=403,
                detail=i18n.translate(i18n.from_request(request), "error.forbidden"),
            )
        return user
    return check


def require_write(permission: str):
    """As require(), for requests that change something.

    The session cookie is SameSite=Strict, which already keeps foreign pages
    from sending it along. On top of that every changing call has to carry
    X-Requested-With -- a header a foreign page cannot set without a CORS
    preflight, which this app never answers.
    """
    def check(request: Request, user=Depends(require(permission))):
        if request.headers.get("x-requested-with") != "chart":
            raise HTTPException(
                status_code=403,
                detail=i18n.translate(i18n.from_request(request), "error.forbidden"),
            )
        return user
    # Lets _assert_writes_guarded() recognise the check on a route.
    check.requires_write = True
    return check


# --- Language -------------------------------------------------------------

@app.get("/lang/{code}")
def set_language(code: str, request: Request):
    """Switches the interface language and returns to the page it came from.

    A cookie rather than a setting per user: the choice concerns the browser
    in front of you, and the sign-in page needs it before there is a user at
    all. Everything else follows from it -- the templates render in this
    language, and app.js reads it back from <html lang>.
    """
    language = i18n.normalize(code)
    response = RedirectResponse(_safe_back(request), status_code=303)
    response.set_cookie(
        i18n.COOKIE, language, max_age=i18n.COOKIE_MAX_AGE, httponly=False,
        secure=config.COOKIE_SECURE, samesite="lax", path="/",
    )
    return response


def _safe_back(request: Request) -> str:
    """The page to return to after switching, taken from the Referer.

    Only our own pages, and only path plus query -- a Referer is freely
    chooseable by the client, and forwarding it unchecked would turn this into
    an open redirect. Anything else lands on the start page.
    """
    referer = request.headers.get("referer", "")
    if not referer:
        return "/"
    parts = urlsplit(referer)
    if parts.netloc and parts.netloc != request.url.netloc:
        return "/"
    if not parts.path.startswith("/") or parts.path.startswith("//"):
        return "/"
    if parts.path.startswith("/lang/"):
        # Do not bounce back into the switch itself.
        return "/"
    return parts.path + (f"?{parts.query}" if parts.query else "")


# --- Sign-in --------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    language = i18n.from_request(request)
    csrf = auth.new_csrf_token()
    response = templates.TemplateResponse(
        request, "login.html",
        {"csrf": csrf, "error": None, "asset_version": ASSET_VERSION,
         "lang": language, "t": i18n.translator(language),
         "brand": brand_parts()}
    )
    # Double submit cookie: the form carries the same value as a hidden field,
    # a foreign form could not get at the cookie.
    #
    # httponly=True, even though the procedure is called plain "double
    # submit": the value is written into the form server-side by the template
    # (login.html), no script reads the cookie -- "chart_csrf" does not appear
    # in app.js. Being readable without need therefore buys nothing here.
    response.set_cookie(
        "chart_csrf", csrf, max_age=3600, httponly=True,
        secure=config.COOKIE_SECURE, samesite="strict", path="/",
    )
    return response


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(""),
    remember: str = Form(""),
):
    ip = client_ip(request)
    language = i18n.from_request(request)
    t = i18n.translator(language)

    def fail(message: str, status: int = 401):
        token = auth.new_csrf_token()
        resp = templates.TemplateResponse(
            request, "login.html",
            {"csrf": token, "error": message, "asset_version": ASSET_VERSION,
             "lang": language, "t": t, "brand": brand_parts()},
            status_code=status
        )
        resp.set_cookie(
            "chart_csrf", token, max_age=3600, httponly=True,
            secure=config.COOKIE_SECURE, samesite="strict", path="/",
        )
        return resp

    # Constant-time comparison -- as for the session in auth.lookup_session.
    # With a random token practically uncritical, but there is no reason to do
    # it right in one place and differently in the other.
    cookie_csrf = request.cookies.get("chart_csrf", "")
    if not csrf or not cookie_csrf or not hmac.compare_digest(csrf, cookie_csrf):
        return fail(t("error.session_expired"), 400)

    if auth.is_locked(ip):
        log.warning("Locked sign-in attempt for '%s' from %s", username, ip)
        audit.event("LOCKED", username, ip, "too many failed attempts")
        return fail(t("error.too_many_attempts", minutes=config.LOCKOUT_MINUTES), 429)

    # Every attempt costs a quarter second -- imperceptible when signing in,
    # but expensive for someone working through passwords.
    time.sleep(0.25)

    user_id = auth.verify(username, password, ip)
    if user_id is None:
        audit.event("FAILED", username, ip, "wrong name or password")
        return fail(t("error.bad_credentials"))
    if not auth.groups_of(user_id):
        # The password was right, so saying why is no leak.
        audit.event("FAILED", username, ip, "no group")
        return fail(t("error.no_access"), 403)

    token, ttl = auth.start_session(
        user_id, remember == "on", ip, request.headers.get("user-agent", "")
    )
    log.info("Successful sign-in for '%s' from %s", username, ip)
    duration = (
        f"{config.SESSION_REMEMBER_DAYS} days (stay signed in)"
        if remember == "on"
        else f"{config.SESSION_HOURS} hours"
    )
    audit.event(
        "LOGIN", username, ip,
        f"session {duration}, {request.headers.get('user-agent', '?')[:60]}",
    )

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=ttl, httponly=True,
        secure=config.COOKIE_SECURE, samesite="strict", path="/",
    )
    response.delete_cookie("chart_csrf", path="/")
    return response


@app.post("/logout")
def logout(request: Request):
    # Look up who is leaving before ending it -- afterwards the session no
    # longer yields the name.
    user = current_user(request)
    if user:
        audit.event("LOGOUT", user["username"], client_ip(request))
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# --- Interface ------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    language = i18n.from_request(request)
    if not auth.can(user, "app"):
        return _no_access(request, user, language)
    return templates.TemplateResponse(
        request, "index.html",
        {"username": user["username"], "asset_version": ASSET_VERSION,
         "lang": language, "t": i18n.translator(language),
         "brand": brand_parts(),
         "can_portfolio": auth.can(user, "portfolio_view"),
         "can_portfolio_edit": auth.can(user, "portfolio_edit"),
         "is_admin": auth.can(user, "admin")})


def _no_access(request: Request, user: dict, language: str):
    """Signed in, but in no group (any more): end the session, back to sign-in."""
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    audit.event("LOGOUT", user["username"], client_ip(request), "no group")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# --- API ------------------------------------------------------------------

def _parse_date(value: str | None, field: str, t):
    """ISO date from the address, or None."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=t("error.invalid_date", field=t("field." + field), value=repr(value)),
        ) from exc


@app.get("/api/chart")
def api_chart(
    request: Request,
    symbol: str = Query(..., min_length=1, max_length=20),
    range: str = Query("1y"),
    # Freely chosen period. If they are set, they take precedence over range.
    start: str | None = Query(None, max_length=10),
    end: str | None = Query(None, max_length=10),
    user=Depends(require_user),
):
    language = i18n.from_request(request)
    t = i18n.translator(language)

    symbol = symbol.strip().upper()
    if not yahoo.valid_symbol(symbol):
        raise HTTPException(status_code=400, detail=t("error.invalid_symbol"))

    start_date = _parse_date(start, "start", t)
    end_date = _parse_date(end, "end", t)
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=400, detail=t("error.start_after_end"))
    if start_date and start_date > datetime.date.today():
        raise HTTPException(status_code=400, detail=t("error.start_in_future"))

    # What goes into the log as the period: the free selection if there is
    # one, otherwise the button (1y, 5y, ...).
    period = f"{start_date}..{end_date}" if (start_date or end_date) else range

    try:
        data = series.build(symbol, range, start_date, end_date, language)
    except yahoo.YahooError as exc:
        audit.event(
            "CHART", user["username"], client_ip(request),
            f"{symbol}  {period}  ERROR: {exc}",
        )
        # An empty period is an input by the visitor, not an outage at Yahoo
        # -- hence 400 instead of 502. Told apart by the exception class, not
        # by searching the message for a word (see yahoo.EmptyRange).
        code = 400 if isinstance(exc, yahoo.EmptyRange) else 502
        detail = t(exc.key, **exc.params) if exc.key else str(exc)
        raise HTTPException(status_code=code, detail=detail) from exc

    data["beta"] = beta.get(symbol)

    # The number of fetches goes into the log as well: it shows whether the
    # request came from our own store (0) or whether Yahoo had to be asked.
    fetch = data.get("fetch", {})
    audit.event(
        "CHART", user["username"], client_ip(request),
        f"{symbol}  {period}  {len(data['daily']['dates'])} days, "
        f"{fetch.get('requests', 0)} Yahoo request(s)",
    )
    return _json_response(data)


def _json_default(value):
    """numpy scalars that slipped through -- as the plain Python value."""
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _json_response(data: dict) -> Response:
    """The chart, serialized straight away.

    Returning the dict would let FastAPI walk it with jsonable_encoder first
    -- every one of the hundreds of thousands of values of a "max" chart,
    which cost 580 ms on top of the 280 ms of json.dumps itself (measured on
    2026-11-09). The data is plain lists and numbers already; the settings
    match Starlette's JSONResponse, NaN included: allow_nan=False.
    """
    body = json.dumps(data, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), default=_json_default)
    return Response(body.encode("utf-8"), media_type="application/json")


@app.get("/api/closes")
def api_closes(
    request: Request,
    symbol: str = Query(..., min_length=1, max_length=20),
    range: str = Query("1y"),
    start: str | None = Query(None, max_length=10),
    end: str | None = Query(None, max_length=10),
    user=Depends(require_user),
):
    """Daily closes of a second symbol, to lay over the chart for comparison."""
    t = i18n.translator(i18n.from_request(request))
    symbol = symbol.strip().upper()
    if not yahoo.valid_symbol(symbol):
        raise HTTPException(status_code=400, detail=t("error.invalid_symbol"))
    try:
        return series.closes(symbol, range, _parse_date(start, "start", t),
                             _parse_date(end, "end", t))
    except yahoo.YahooError as exc:
        code = 400 if isinstance(exc, yahoo.EmptyRange) else 502
        detail = t(exc.key, **exc.params) if exc.key else str(exc)
        raise HTTPException(status_code=code, detail=detail) from exc


@app.get("/api/fundamentals")
def api_fundamentals(symbol: str = Query(..., min_length=1, max_length=20),
                     user=Depends(require_user)):
    """Free cash flow of a symbol. Its own call, so the chart does not wait."""
    return {"fcf": fundamentals.free_cash_flow(symbol.strip().upper())}


@app.get("/api/search")
def api_search(q: str = Query("", max_length=40), user=Depends(require_user)):
    return {"results": yahoo.search(q)}


# Panels of the daily chart. The titles live in the frontend; all that counts
# here is that the key is known -- that way nobody can put nonsense into the
# database. Only the set of valid keys, NO order: the default order lives with
# titles and heights in PANELS in app.js, so that everything visible sits in
# one place. An order prescribed here would silently override it.
PANEL_KEYS = ["price", "volume", "macd", "rsi", "bollinger", "vix", "ribbon"]


def _normalize_panels(wanted, t) -> list[dict]:
    """Brings a wanted panel list into a valid shape.

    Unknown keys drop out, duplicates are removed, and panels missing from the
    stored list move to the end as visible. That way a panel added later shows
    up by itself in existing settings instead of staying invisible.
    """
    out, seen = [], set()
    for entry in wanted or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if key not in PANEL_KEYS or key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "visible": bool(entry.get("visible", True))})

    for key in PANEL_KEYS:
        if key not in seen:
            out.append({"key": key, "visible": True})

    if not any(p["visible"] for p in out):
        raise HTTPException(status_code=400, detail=t("error.panel_min_one"))
    return out


@app.get("/api/settings")
def api_settings_get(request: Request, user=Depends(require_user)):
    t = i18n.translator(i18n.from_request(request))
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE user_id = ?", (user["user_id"],)
        ).fetchall()
    stored = {r["key"]: json.loads(r["value"]) for r in rows}
    if "panels" not in stored:
        # An empty list means: no choice of their own made. The frontend then
        # takes its own default order from PANELS.
        return {"panels": []}
    return {"panels": _normalize_panels(stored["panels"], t)}


@app.put("/api/settings/panels")
def api_settings_panels(
    request: Request,
    panels: list = Body(..., embed=True),
    user=Depends(require_write("app")),
):
    t = i18n.translator(i18n.from_request(request))
    cleaned = _normalize_panels(panels, t)
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (?, 'panels', ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user["user_id"], json.dumps(cleaned, separators=(",", ":"))),
        )
    return {"panels": cleaned}


@app.delete("/api/settings/panels")
def api_settings_panels_reset(user=Depends(require_write("app"))):
    with db.cursor() as conn:
        conn.execute(
            "DELETE FROM settings WHERE user_id = ? AND key = 'panels'",
            (user["user_id"],),
        )
    # As when reading: empty means "default from app.js".
    return {"panels": []}


@app.get("/api/watchlist")
def api_watchlist_get(user=Depends(require_user)):
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT symbol, label FROM watchlist WHERE user_id = ? ORDER BY sort, symbol",
            (user["user_id"],),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/watchlist")
def api_watchlist_add(
    request: Request,
    symbol: str = Form(...),
    label: str = Form(""),
    user=Depends(require_write("app")),
):
    t = i18n.translator(i18n.from_request(request))
    symbol = symbol.strip().upper()
    if not yahoo.valid_symbol(symbol):
        raise HTTPException(status_code=400, detail=t("error.invalid_symbol"))

    # Only take in what really exists. The call loads the prices into the
    # store anyway, from which the watchlist reads right afterwards -- so it
    # is not wasted.
    try:
        series.quote(symbol)
    except yahoo.YahooError as exc:
        raise HTTPException(
            status_code=400, detail=t("error.symbol_not_found", symbol=symbol)
        ) from exc

    # Without a label supplied, look the name up once. Yahoo's price query
    # returns no company name, the symbol search does, and its result then
    # sits in the cache for a day.
    if not label.strip():
        for hit in yahoo.search(symbol, limit=5):
            if hit["symbol"].upper() == symbol:
                label = hit["name"]
                break

    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO watchlist (user_id, symbol, label) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, symbol) DO UPDATE SET label = excluded.label",
            (user["user_id"], symbol, label[:60]),
        )
    return {"ok": True}


@app.get("/api/watchlist/quotes")
def api_watchlist_quotes(user=Depends(require_user)):
    """Favorites with their current price, daily change and history."""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT symbol, label FROM watchlist WHERE user_id = ? ORDER BY sort, symbol",
            (user["user_id"],),
        ).fetchall()

    items = []
    for row in rows:
        entry = {"symbol": row["symbol"], "label": row["label"] or ""}
        try:
            entry.update(series.quote(row["symbol"]))
        except yahoo.YahooError as exc:
            # A single stubborn symbol must not tip over the list.
            log.warning("Favorite %s not available: %s", row["symbol"], exc)
            entry["error"] = str(exc)
        items.append(entry)
    return {"items": items}


@app.delete("/api/watchlist/{symbol}")
def api_watchlist_del(symbol: str, user=Depends(require_write("app"))):
    with db.cursor() as conn:
        conn.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND symbol = ?",
            (user["user_id"], symbol.strip().upper()),
        )
    return {"ok": True}


# --- Signals --------------------------------------------------------------

def _held_symbols(user_id: int) -> set[str]:
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT symbol FROM positions WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {r["symbol"] for r in rows}


@app.get("/api/signals")
def api_signals(user=Depends(require_user)):
    """Where each favorite stands today -- the source of the notices."""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT symbol, label FROM watchlist WHERE user_id = ? ORDER BY sort, symbol",
            (user["user_id"],),
        ).fetchall()
    held = _held_symbols(user["user_id"])

    items = []
    for row in rows:
        try:
            signal = notices.current_signal(row["symbol"], holding=row["symbol"] in held)
        except yahoo.YahooError as exc:
            # One unavailable symbol must not take the whole list down.
            log.warning("Signal for %s not available: %s", row["symbol"], exc)
            continue
        if signal:
            signal["label"] = row["label"] or ""
            items.append(signal)
    return {"items": items}


@app.get("/api/positions")
def api_positions_get(user=Depends(require_user)):
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT symbol, bought_at, price, stake FROM positions "
            "WHERE user_id = ? ORDER BY symbol",
            (user["user_id"],),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/positions")
def api_positions_add(
    request: Request,
    symbol: str = Form(...),
    price: float | None = Form(None),
    stake: float | None = Form(None),
    user=Depends(require_write("app")),
):
    """Mark an instrument as held, so that it gets sell notices instead of buy ones."""
    t = i18n.translator(i18n.from_request(request))
    symbol = symbol.strip().upper()
    if not yahoo.valid_symbol(symbol):
        raise HTTPException(status_code=400, detail=t("error.invalid_symbol"))
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO positions (user_id, symbol, bought_at, price, stake) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, symbol) DO UPDATE SET "
            "bought_at = excluded.bought_at, price = excluded.price, stake = excluded.stake",
            (user["user_id"], symbol, int(time.time()), price, stake),
        )
    return {"ok": True}


@app.delete("/api/positions/{symbol}")
def api_positions_del(symbol: str, user=Depends(require_write("app"))):
    with db.cursor() as conn:
        conn.execute(
            "DELETE FROM positions WHERE user_id = ? AND symbol = ?",
            (user["user_id"], symbol.strip().upper()),
        )
    return {"ok": True}


# --- Portfolio Tracker --------------------------------------------------------

PF_MAX_ROWS = 200
PF_FIRST_DATE = datetime.date(2000, 1, 1)


def _pf_rows(symbol: str | None = None) -> list[dict]:
    """The journal. Shared since the groups (2026-10-16): there is one
    tracked portfolio, entered by an admin and read by the "portfolio"
    group. user_id stays as the author of an entry."""
    query = "SELECT id, symbol, side, trade_date, shares, price, note FROM portfolio_trades"
    params: list = []
    if symbol:
        query += " WHERE symbol = ?"
        params.append(symbol)
    with db.cursor() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def _pf_clean(entry, t) -> tuple:
    """One trade from the browser, checked; raises 400 naming what is wrong."""
    if not isinstance(entry, dict):
        raise HTTPException(status_code=400, detail=t("error.pf_invalid"))
    symbol = str(entry.get("symbol") or "").strip().upper().lstrip("$")
    if not yahoo.valid_symbol(symbol):
        raise HTTPException(status_code=400, detail=t("error.invalid_symbol"))
    side = entry.get("side")
    if side not in portfolio.SIDES:
        raise HTTPException(status_code=400, detail=t("error.pf_invalid"))
    day = _parse_date(str(entry.get("date") or ""), "date", t)
    if day is None or day < PF_FIRST_DATE or day > datetime.date.today():
        raise HTTPException(status_code=400, detail=t("error.pf_date", symbol=symbol))

    def number(name):
        value = entry.get(name)
        if value in (None, ""):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = -1
        if not (0 < value < 1e9):
            raise HTTPException(status_code=400,
                                detail=t("error.pf_number", field=t("field." + name), symbol=symbol))
        return value

    shares, price = number("shares"), number("price")
    # Only a sell may leave the count open (= the whole position).
    if shares is None and side == "buy":
        raise HTTPException(status_code=400, detail=t("error.pf_shares", symbol=symbol))
    note = str(entry.get("note") or "")[:200]
    return symbol, side, day.isoformat(), shares, price, note


@app.get("/api/portfolio")
def api_portfolio(user=Depends(require("portfolio_view"))):
    """Open positions with today's prices, realized results, the journal."""
    return portfolio.summary(_pf_rows())


@app.get("/api/portfolio/symbol/{symbol}")
def api_pf_symbol(symbol: str, user=Depends(require("portfolio_view"))):
    """Trades and the open position of one symbol -- the marks in the chart.

    Without today's price: the chart has its own, and the call stays free of
    requests to Yahoo unless a trade lacks a price.
    """
    symbol = symbol.strip().upper()
    if not yahoo.valid_symbol(symbol):
        return {"trades": [], "position": None}
    book = portfolio.ledger(_pf_rows(symbol))
    return {"trades": book["trades"],
            "position": book["positions"][0] if book["positions"] else None}


@app.post("/api/portfolio/trades")
def api_pf_add(
    request: Request,
    trades: list = Body(..., embed=True),
    user=Depends(require_write("portfolio_edit")),
):
    t = i18n.translator(i18n.from_request(request))
    if not trades or len(trades) > PF_MAX_ROWS:
        raise HTTPException(status_code=400, detail=t("error.pf_invalid"))
    cleaned = [_pf_clean(entry, t) for entry in trades]
    now = int(time.time())
    with db.cursor() as conn:
        conn.executemany(
            "INSERT INTO portfolio_trades (user_id, symbol, side, trade_date, shares, price, "
            "note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(user["user_id"], *row, now) for row in cleaned],
        )
    audit.event("PF_ADD", user["username"], client_ip(request),
                ", ".join(f"{r[1]} {r[0]}" for r in cleaned)[:200])
    return {"ok": True, "count": len(cleaned)}


@app.post("/api/portfolio/parse")
def api_pf_parse(text: str = Body(..., embed=True, max_length=20_000),
                 user=Depends(require_write("portfolio_edit"))):
    """Reads the trades out of a pasted post -- a proposal, nothing is saved."""
    return portfolio.parse_post(text)


@app.delete("/api/portfolio/trades/{trade_id}")
def api_pf_del(trade_id: int, request: Request, user=Depends(require_write("portfolio_edit"))):
    with db.cursor() as conn:
        conn.execute("DELETE FROM portfolio_trades WHERE id = ?", (trade_id,))
    audit.event("PF_DEL", user["username"], client_ip(request), str(trade_id))
    return {"ok": True}


# --- User management -----------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not auth.can(user, "admin"):
        return RedirectResponse("/", status_code=303)
    language = i18n.from_request(request)
    t = i18n.translator(language)
    return templates.TemplateResponse(
        request, "admin.html",
        {"username": user["username"], "asset_version": ASSET_VERSION,
         "lang": language, "t": t, "brand": brand_parts(),
         "groups": auth.GROUPS,
         # The texts admin.js draws, as a JSON data block: no inline script,
         # which the Content-Security-Policy would refuse anyway.
         # "</" escaped, so no text can ever close the <script> block early.
         "texts": json.dumps({k: v for k, v in i18n.TEXTS[language].items()
                              if k.startswith(("admin.", "group."))}).replace("</", "<\\/")})


def _admin_user(user_id: int, t) -> dict:
    with db.cursor() as conn:
        row = conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=t("error.user_unknown"))
    return dict(row)


def _clean_groups(groups, t) -> list[str]:
    if not isinstance(groups, list) or any(g not in auth.GROUPS for g in groups):
        raise HTTPException(status_code=400, detail=t("error.group_unknown"))
    return [g for g in auth.GROUPS if g in groups]


def _check_password(password, t) -> str:
    if not isinstance(password, str) or not (auth.MIN_PASSWORD <= len(password) <= auth.MAX_PASSWORD):
        raise HTTPException(status_code=400,
                            detail=t("error.password_length", min=auth.MIN_PASSWORD))
    return password


def _admins_left(without: int) -> int:
    with db.cursor() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM user_groups WHERE grp = 'admin' AND user_id != ?",
            (without,)).fetchone()["n"]


@app.get("/api/admin/users")
def api_admin_users(user=Depends(require("admin"))):
    now = int(time.time())
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT u.id, u.username, u.created_at, u.last_login, "
            "(SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id AND s.expires_at > ?) "
            "AS sessions FROM users u ORDER BY u.username", (now,)).fetchall()
    return {"me": user["user_id"],
            "users": [dict(r, groups=auth.groups_of(r["id"])) for r in rows]}


@app.post("/api/admin/users")
def api_admin_create(request: Request, body: dict = Body(...),
                     user=Depends(require_write("admin"))):
    t = i18n.translator(i18n.from_request(request))
    username = str(body.get("username") or "").strip().lower()
    if not auth.USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail=t("error.username_invalid"))
    password = _check_password(body.get("password"), t)
    groups = _clean_groups(body.get("groups") or [], t)
    if not groups:
        raise HTTPException(status_code=400, detail=t("error.group_none"))
    with db.cursor() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise HTTPException(status_code=400, detail=t("error.username_taken"))
    auth.create_user(username, password)
    with db.cursor() as conn:
        new_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
    auth.set_groups(new_id, groups)
    audit.event("ADMIN", user["username"], client_ip(request),
                f"created {username} ({', '.join(groups)})")
    return {"ok": True}


@app.put("/api/admin/users/{user_id}/groups")
def api_admin_groups(user_id: int, request: Request, groups: list = Body(..., embed=True),
                     user=Depends(require_write("admin"))):
    t = i18n.translator(i18n.from_request(request))
    target = _admin_user(user_id, t)
    groups = _clean_groups(groups, t)
    # Nobody takes the admin group away from themselves, and the last admin
    # stays one -- otherwise only the shell (cli groups) would get back in.
    if "admin" not in groups and (user_id == user["user_id"] or not _admins_left(user_id)):
        raise HTTPException(status_code=400, detail=t("error.admin_keep"))
    auth.set_groups(user_id, groups)
    if not groups:
        # Without a group there is nothing to see; end what is still open.
        auth.end_sessions_of(user_id)
    audit.event("ADMIN", user["username"], client_ip(request),
                f"groups of {target['username']}: {', '.join(groups) or '-'}")
    return {"ok": True}


@app.put("/api/admin/users/{user_id}/password")
def api_admin_password(user_id: int, request: Request, password: str = Body(..., embed=True),
                       user=Depends(require_write("admin"))):
    t = i18n.translator(i18n.from_request(request))
    target = _admin_user(user_id, t)
    # create_user() on an existing name sets the password and ends all of
    # that user's sessions (see there).
    auth.create_user(target["username"], _check_password(password, t))
    audit.event("ADMIN", user["username"], client_ip(request),
                f"password of {target['username']} set")
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}/sessions")
def api_admin_sessions(user_id: int, request: Request, user=Depends(require_write("admin"))):
    t = i18n.translator(i18n.from_request(request))
    target = _admin_user(user_id, t)
    ended = auth.end_sessions_of(user_id)
    audit.event("ADMIN", user["username"], client_ip(request),
                f"{ended} session(s) of {target['username']} ended")
    return {"ok": True, "ended": ended}


@app.delete("/api/admin/users/{user_id}")
def api_admin_delete(user_id: int, request: Request, user=Depends(require_write("admin"))):
    t = i18n.translator(i18n.from_request(request))
    target = _admin_user(user_id, t)
    if user_id == user["user_id"]:
        raise HTTPException(status_code=400, detail=t("error.delete_self"))
    with db.cursor() as conn:
        # The portfolio journal is shared; its entries outlive their
        # author and are handed to whoever deletes the account.
        conn.execute("UPDATE portfolio_trades SET user_id = ? WHERE user_id = ?",
                     (user["user_id"], user_id))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    audit.event("ADMIN", user["username"], client_ip(request), f"deleted {target['username']}")
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return JSONResponse({"status": "ok"})


@app.exception_handler(401)
def unauthorized(request: Request, exc: HTTPException):
    # Send page views to the sign-in, answer API calls cleanly with a 401.
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"detail": i18n.translate(i18n.from_request(request), "error.not_signed_in")},
            status_code=401,
        )
    return RedirectResponse("/login", status_code=303)


def _assert_writes_guarded() -> None:
    """Refuses to start if a changing /api/ route skips require_write().

    The X-Requested-With check only protects the routes that ask for it. A
    write endpoint declared with plain require_user or require() falls back to
    SameSite alone without anybody noticing. Checked once at import, after
    every route is registered, so such a route cannot reach production.

    Routes outside /api/ are left out on purpose: /login has its own
    double-submit CSRF check, and a forged /logout only signs the user out.
    """
    write_methods = {"POST", "PUT", "PATCH", "DELETE"}

    def guarded(dependant) -> bool:
        return any(getattr(dep.call, "requires_write", False) or guarded(dep)
                   for dep in dependant.dependencies)

    unguarded = [
        f"{'/'.join(sorted(route.methods & write_methods))} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/")
        and route.methods & write_methods and not guarded(route.dependant)
    ]
    if unguarded:
        raise SystemExit(
            "Changing API routes without require_write(): " + ", ".join(unguarded)
            + '\n  Declare them with user=Depends(require_write("<permission>")).'
        )


_assert_writes_guarded()
