"""Trader-Werkzeuge: Rebalancing, Budget, Risiko-Check, News, 13F-Copycat, Kongress-Trades.

Die Rechenlogik stammt aus github.com/Jakeschincariol/trader-skill (MIT, siehe
trader_tools/LICENSE). Diese Datei verbindet sie mit dem Lock-In-Portfolio in
der Datenbank. Es wird nie eine Order platziert: alles sind Vorschläge.

Alle Beträge laufen in der Basiswährung aus den Einstellungen (Standard CHF).
Die Tools nennen ihre Felder "usd" – gemeint ist hier immer die Basiswährung.
"""

import datetime as _dt
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import psycopg2
import psycopg2.extras
import yfinance as yf
from flask import Blueprint, jsonify, request

from finance import fetch_crypto_data, fetch_stock_data
from trader_tools import check as risk_tool
from trader_tools import edgar13f as E
from trader_tools import news as N
from trader_tools import ptr as P
from trader_tools import rebalance as R
from trader_tools import size as S

trader_bp = Blueprint("trader", __name__)

DEFAULTS = {
    "base_currency": "CHF",
    "cash": 0.0,                 # Cash auf dem Depot, in Basiswährung
    "cash_buffer_pct": 10.0,
    "max_position_pct": 25.0,
    "max_order_usd": 2000.0,     # max. Order, in Basiswährung
    "min_order_usd": 5.0,
    "rebalance_band_pct": 5.0,
    "max_orders_per_day": 10,
    "fractional": True,
    "blocked": [],
    "pelosi_members": ["Nancy Pelosi"],
    "pelosi_push": True,
}
NUMBER_FIELDS = ("cash", "cash_buffer_pct", "max_position_pct", "max_order_usd",
                 "min_order_usd", "rebalance_band_pct", "max_orders_per_day")
CRYPTO_WORDS = ("crypto", "krypto")
PTR_DIR = os.path.join(tempfile.gettempdir(), "lockin-ptr")


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(os.environ.get("DATABASE_URL", ""))


def init_trader_db(cur):
    """Wird aus app.init_db() mit dessen Cursor aufgerufen."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trader_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            config JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trader_targets (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            weight NUMERIC NOT NULL,
            source TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trader_seen (
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (kind, key)
        )
    """)
    for table in ("trader_settings", "trader_targets", "trader_seen"):
        cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def _f(v):
    return float(v) if isinstance(v, Decimal) else v


def load_settings():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT config FROM trader_settings WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    cfg = dict(DEFAULTS)
    if row and isinstance(row[0], dict):
        cfg.update({k: v for k, v in row[0].items() if k in DEFAULTS})
    return cfg


def clean_settings(data, current):
    cfg = dict(current)
    for k in NUMBER_FIELDS:
        if k in data and data[k] not in (None, ""):
            try:
                v = float(data[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k} ist keine Zahl")
            if v < 0:
                raise ValueError(f"{k} darf nicht negativ sein")
            cfg[k] = int(v) if k == "max_orders_per_day" else v
    if not 0 <= cfg["cash_buffer_pct"] < 100:
        raise ValueError("Cash-Puffer muss zwischen 0 und 100 % liegen")
    if not 0 < cfg["max_position_pct"] <= 100:
        raise ValueError("Max. Positionsgrösse muss zwischen 0 und 100 % liegen")
    if "base_currency" in data:
        cur = str(data["base_currency"] or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", cur):
            raise ValueError("Basiswährung muss ein 3-Buchstaben-Code sein (z.B. CHF)")
        cfg["base_currency"] = cur
    for k in ("fractional", "pelosi_push"):
        if k in data:
            cfg[k] = bool(data[k])
    if "blocked" in data:
        cfg["blocked"] = _split_list(data["blocked"], upper=True)
    if "pelosi_members" in data:
        cfg["pelosi_members"] = _split_list(data["pelosi_members"])
    return cfg


def _split_list(v, upper=False):
    items = v if isinstance(v, list) else str(v or "").split(",")
    out = [str(x).strip() for x in items if str(x).strip()]
    return [x.upper() for x in out] if upper else out


def load_targets():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT ticker, name, weight, source FROM trader_targets ORDER BY weight DESC, ticker")
    rows = [{k: _f(v) for k, v in r.items()} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def save_targets(rows, source):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM trader_targets")
    for r in rows:
        cur.execute(
            "INSERT INTO trader_targets (ticker, name, weight, source) VALUES (%s, %s, %s, %s)",
            (r["ticker"], r.get("name") or None, r["weight"], source),
        )
    conn.commit()
    cur.close()
    conn.close()


def claim_seen(kind, keys):
    """Speichert keys als gesehen; gibt die zurück, die vorher noch neu waren."""
    if not keys:
        return set()
    conn = get_db()
    cur = conn.cursor()
    fresh = set()
    for k in keys:
        cur.execute(
            "INSERT INTO trader_seen (kind, key) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING key",
            (kind, k),
        )
        if cur.fetchone():
            fresh.add(k)
    conn.commit()
    cur.close()
    conn.close()
    return fresh


def seen_count(kind):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM trader_seen WHERE kind = %s", (kind,))
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


def load_portfolio_rows():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM user_portfolio ORDER BY asset_name")
    rows = [{k: _f(v) for k, v in r.items()} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


# ── Kurse & Währungen ─────────────────────────────────────────────────────────

_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, fn, keep=lambda v: True):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    if keep(value):
        with _cache_lock:
            _cache[key] = (now, value)
    return value


def is_crypto(asset_type):
    return any(w in (asset_type or "").lower() for w in CRYPTO_WORDS)


def yf_symbol(ticker):
    """BRK.B -> BRK-B für Yahoo; Börsen-Suffixe wie VUSA.L oder NESN.SW bleiben."""
    t = ticker.upper()
    return t.replace(".", "-") if re.fullmatch(r"[A-Z]{1,5}\.[A-E]", t) else t


def fx_rate(cur, base):
    if not cur or cur == base:
        return 1.0

    def load():
        try:
            rate = yf.Ticker(f"{cur}{base}=X").fast_info.last_price
            return float(rate) if rate else None
        except Exception:
            return None
    return cached(("fx", cur, base), 900, load, keep=lambda v: v is not None)


def market(ticker, asset_type):
    """Kurs in Originalwährung: {"price", "change_pct", "currency"} (5 Min. gecacht)."""
    def load():
        for _ in range(2):  # yfinance scheitert unter Last manchmal einmalig
            m = fetch_crypto_data(ticker) if is_crypto(asset_type) else fetch_stock_data(yf_symbol(ticker))
            if m.get("price"):
                break
        return m
    return cached(("q", ticker, is_crypto(asset_type)), 300, load, keep=lambda m: bool(m.get("price")))


def quote(ticker, asset_type, base):
    """(Preis in Basiswährung, Originalwährung) oder (None, None)."""
    m = market(ticker, asset_type)
    price, cur = m.get("price"), m.get("currency")
    if not price:
        return None, None
    rate = fx_rate(cur, base)
    if rate is None:
        return None, cur
    return float(price) * rate, cur


def quotes(items, base):
    """items: [(ticker, asset_type)] -> {ticker: Preis in Basiswährung}"""
    items = list(dict.fromkeys(items))
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda it: quote(it[0], it[1], base), items))
    return {t: p for (t, _), (p, _) in zip(items, results) if p}


def snapshot(cfg, extra_tickers=()):
    """Das Portfolio im Format der Tools, Preise in Basiswährung, plus Hinweise."""
    base = cfg["base_currency"]
    notes, names, types = [], {}, {}
    holdings = []
    for r in load_portfolio_rows():
        t = (r.get("ticker") or "").strip().upper()
        if not t:
            notes.append(f"{r['asset_name']} hat keinen Ticker und wird nicht mitgerechnet.")
            continue
        holdings.append({"ticker": t, "shares": float(r["quantity"])})
        names.setdefault(t, r["asset_name"])
        types.setdefault(t, r.get("asset_type") or "")
    want = [(t, types.get(t, "")) for t in names] + [(t, "") for t in extra_tickers if t not in names]
    prices = quotes(want, base)
    kept = []
    for h in holdings:
        if h["ticker"] in prices:
            kept.append(h)
        else:
            notes.append(f"Kein Kurs für {h['ticker']}: Position wird nicht mitgerechnet.")
    portfolio = {"cash": float(cfg.get("cash") or 0), "positions": kept}
    return portfolio, prices, names, types, list(dict.fromkeys(notes))


def risk_check(orders, cfg, portfolio, prices):
    rcfg = dict(cfg)
    rcfg["paper"] = True
    res = risk_tool.check(R.clean(orders), rcfg, portfolio=portfolio, prices=prices)
    for o, v in zip(orders, res["results"]):
        o["verdict"] = v["verdict"]
        o["reasons"] = v["reasons"]
        o["warnings"] = v["warnings"]
        o["value"] = v["value_usd"]
    return res


_DOLLAR = re.compile(r"\$(\d[\d,]*(?:\.\d+)?)")


def in_currency(res, cur):
    """Die Tools schreiben Beträge als "$1,234" – hier ist es die Basiswährung."""
    fix = lambda s: _DOLLAR.sub(lambda m: f"{m.group(1)} {cur}", s) if isinstance(s, str) else s
    res["notes"] = [fix(n) for n in res.get("notes", [])]
    for o in res.get("orders", []):
        o["why"] = fix(o.get("why"))
        o["reasons"] = [fix(r) for r in o.get("reasons", [])]
        o["warnings"] = [fix(w) for w in o.get("warnings", [])]
    for d in res.get("dropped", []):
        d["reason"] = fix(d.get("reason"))
    return res


def err(msg, code=400):
    return jsonify({"error": msg}), code


# ── Live-Positionen in Basiswährung ───────────────────────────────────────────

def positions_data(cfg=None):
    """Jede Position mit aktuellem Kurs, Wert in Basiswährung, Gewinn und Gewicht.

    Annahme: der Kaufpreis ist in derselben Währung erfasst, in der die Börse
    den Titel handelt (so zeigen es Broker als "Einstandskurs")."""
    cfg = cfg or load_settings()
    base = cfg["base_currency"]
    rows = load_portfolio_rows()
    with ThreadPoolExecutor(max_workers=4) as ex:
        marks = list(ex.map(lambda r: market((r.get("ticker") or "").strip().upper(), r.get("asset_type"))
                            if (r.get("ticker") or "").strip() else {}, rows))
    out, missing = [], []
    tot = {"value": 0.0, "cost": 0.0, "day": 0.0}
    for r, m in zip(rows, marks):
        p = {k: r.get(k) for k in ("id", "asset_name", "ticker", "asset_type", "quantity", "buy_price", "notes")}
        price, cur, chg = m.get("price"), m.get("currency"), m.get("change_pct")
        rate = fx_rate(cur, base) if price else None
        if not price or rate is None:
            p.update(price=None, currency=cur, value=None)
            missing.append(r.get("ticker") or r["asset_name"])
            out.append(p)
            continue
        qty, buy = float(r["quantity"]), float(r["buy_price"] or 0)
        value = qty * price * rate
        cost = qty * buy * rate
        day = value * chg / (100 + chg) if chg is not None else 0.0
        p.update(price=round(price, 4), currency=cur, change_pct=chg, fx=rate,
                 value=round(value, 2), cost=round(cost, 2), gain=round(value - cost, 2),
                 gain_pct=round(100 * (price - buy) / buy, 2) if buy else None,
                 day_change=round(day, 2))
        tot["value"] += value
        tot["cost"] += cost
        tot["day"] += day
        out.append(p)
    for p in out:
        p["weight"] = round(100 * p["value"] / tot["value"], 2) if p.get("value") and tot["value"] else None
    total = {"value": round(tot["value"], 2), "cost": round(tot["cost"], 2),
             "gain": round(tot["value"] - tot["cost"], 2),
             "gain_pct": round(100 * (tot["value"] - tot["cost"]) / tot["cost"], 2) if tot["cost"] else None,
             "day_change": round(tot["day"], 2)}
    return {"positions": out, "total": total, "currency": base, "missing": missing}


@trader_bp.route("/api/trader/positions", methods=["GET"])
def positions():
    return jsonify(positions_data())


# ── Einstellungen & Zielgewichte ──────────────────────────────────────────────

@trader_bp.route("/api/trader/settings", methods=["GET"])
def get_settings():
    cfg = load_settings()
    cfg["sec_ready"] = bool(E.user_agent(None, None))
    return jsonify(cfg)


@trader_bp.route("/api/trader/settings", methods=["PUT"])
def put_settings():
    try:
        cfg = clean_settings(request.json or {}, load_settings())
    except ValueError as e:
        return err(str(e))
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO trader_settings (id, config) VALUES (1, %s)
           ON CONFLICT (id) DO UPDATE SET config = EXCLUDED.config""",
        (psycopg2.extras.Json(cfg),),
    )
    conn.commit()
    cur.close()
    conn.close()
    cfg["sec_ready"] = bool(E.user_agent(None, None))
    return jsonify(cfg)


@trader_bp.route("/api/trader/targets", methods=["GET"])
def get_targets():
    rows = load_targets()
    return jsonify({"targets": rows, "source": rows[0]["source"] if rows else None})


@trader_bp.route("/api/trader/targets", methods=["PUT"])
def put_targets():
    data = request.json or {}
    try:
        rows = S.parse_weights(data.get("targets") or [])
    except S.SizeError as e:
        return err(str(e))
    rows = [r for r in rows if r["weight"] > 0]
    tickers = [r["ticker"] for r in rows]
    if len(tickers) != len(set(tickers)):
        return err("Ein Ticker ist doppelt in den Zielgewichten.")
    total = sum(r["weight"] for r in rows)
    rows = [dict(r, weight=round(100.0 * r["weight"] / total, 4)) for r in rows] if total else []
    save_targets(rows, str(data.get("source") or "Manuell")[:200])
    return get_targets()


@trader_bp.route("/api/trader/targets/from-portfolio", methods=["POST"])
def targets_from_portfolio():
    """Die heutigen Gewichte als Ziel übernehmen (Startpunkt zum Anpassen)."""
    cfg = load_settings()
    portfolio, prices, names, _, notes = snapshot(cfg)
    values = {}
    for p in portfolio["positions"]:
        values[p["ticker"]] = values.get(p["ticker"], 0.0) + p["shares"] * prices[p["ticker"]]
    total = sum(values.values())
    if not total:
        return err("Keine Positionen mit Kurs gefunden.")
    rows = [{"ticker": t, "name": names.get(t, ""), "weight": round(100.0 * v / total, 4)}
            for t, v in sorted(values.items(), key=lambda kv: -kv[1])]
    save_targets(rows, "Aktuelles Portfolio")
    out = get_targets().get_json()
    out["notes"] = notes
    return jsonify(out)


# ── Rebalancing ───────────────────────────────────────────────────────────────

@trader_bp.route("/api/trader/rebalance", methods=["POST"])
def rebalance():
    data = request.json or {}
    cfg = load_settings()
    targets = load_targets()
    if not targets:
        return err("Noch keine Zielgewichte. Übernimm dein aktuelles Portfolio oder ein 13F-Portfolio als Ziel.")
    try:
        weights = R.parse_targets({"weights": targets}, cfg.get("blocked"))
    except R.RebalanceError as e:
        return err(str(e))
    portfolio, prices, names, _, notes = snapshot(cfg, extra_tickers=list(weights))
    missing = [t for t in weights if t not in prices]
    for t in missing:
        notes.append(f"Kein Kurs für Ziel-Ticker {t}: wird beim Rebalancing ausgelassen.")
        weights.pop(t)
    if not weights:
        return err("Für keinen Ziel-Ticker gibt es einen Kurs.")
    band = data.get("band")
    try:
        res = R.plan(portfolio, prices, weights, cfg,
                     band=float(band) if band not in (None, "") else None,
                     sell_untracked=bool(data.get("sell_untracked")))
    except (R.RebalanceError, ValueError) as e:
        return err(str(e))
    tnames = {t["ticker"]: t.get("name") for t in targets}
    for row in res["rows"]:
        row["name"] = names.get(row["ticker"]) or tnames.get(row["ticker"]) or ""
    risk = risk_check(res["orders"], cfg, portfolio, prices) if res["orders"] else {"warnings": []}
    res["notes"] = notes + res["notes"] + risk.get("warnings", [])
    res["currency"] = cfg["base_currency"]
    return jsonify(in_currency(res, cfg["base_currency"]))


# ── Neues Geld verteilen (Budget) ─────────────────────────────────────────────

@trader_bp.route("/api/trader/budget", methods=["POST"])
def budget():
    data = request.json or {}
    cfg = load_settings()
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        return err("Betrag ist keine Zahl.")
    if amount <= 0:
        return err("Bitte einen Betrag grösser als 0 eingeben.")
    targets = load_targets()
    if not targets:
        return err("Noch keine Zielgewichte. Lege zuerst fest, wie das Geld verteilt werden soll.")
    portfolio, prices, names, _, notes = snapshot(cfg, extra_tickers=[t["ticker"] for t in targets])
    rows = [{"ticker": t["ticker"], "name": t.get("name") or "", "weight": t["weight"]}
            for t in targets if t["ticker"] in prices]
    for t in targets:
        if t["ticker"] not in prices:
            notes.append(f"Kein Kurs für {t['ticker']}: wird nicht gekauft.")
    whole = bool(data.get("whole_shares")) or not cfg.get("fractional", True)
    try:
        res = S.size(rows, cfg, budget=amount, prices=prices, whole=whole)
    except S.SizeError as e:
        return err(str(e))
    orders = S.orders_from(res)
    for o in orders:
        o["_usd"] = next(p["dollars"] for p in res["positions"] if p["ticker"] == o["symbol"])
    # Für den Risiko-Check: das neue Geld liegt als Cash bereit; der Puffer
    # ist der Budget-Puffer, nicht ein Prozentsatz des ganzen Depots.
    acct = dict(portfolio, cash=portfolio["cash"] + amount)
    total = acct["cash"] + sum(p["shares"] * prices[p["ticker"]] for p in acct["positions"])
    rcfg = dict(cfg, cash_buffer_pct=100.0 * (res["cash_buffer"] + portfolio["cash"]) / total if total else 0)
    risk = risk_check(orders, rcfg, acct, prices) if orders else {"warnings": []}
    for p in res["positions"]:
        p["name"] = p.get("name") or names.get(p["ticker"], "")
    res["orders"] = orders
    res["notes"] = notes + risk.get("warnings", [])
    res["currency"] = cfg["base_currency"]
    return jsonify(in_currency(res, cfg["base_currency"]))


# ── News zu deinen Positionen ─────────────────────────────────────────────────

def _news_ciks(fetcher):
    state = {}
    return cached(("ciks",), 86400, lambda: N.cik_map(fetcher, state))


def news_watch(rows):
    """{Yahoo-Ticker: Name} für alle Positionen; Krypto als BTC-USD."""
    watch = {}
    for r in rows:
        t = (r.get("ticker") or "").strip().upper()
        if not t:
            continue
        if is_crypto(r.get("asset_type")):
            t = f"{t}-USD" if "-" not in t else t
        watch.setdefault(t, r["asset_name"])
    return watch


@trader_bp.route("/api/trader/news", methods=["GET"])
def news():
    try:
        hours = max(1, min(int(request.args.get("hours", 48)), 24 * 14))
    except ValueError:
        hours = 48
    return jsonify(collect_news(news_watch(load_portfolio_rows()), hours))


def collect_news(watch, hours=48, sec=True):
    """Yahoo-Schlagzeilen (+ SEC 8-K, wenn SEC_USER_AGENT gesetzt) für watch."""
    if not watch:
        return {"items": [], "errors": ["Keine Positionen mit Ticker."], "sec": False, "hours": hours}

    ua = E.user_agent(None, None) if sec else None
    fetcher = N.Fetcher(sec_ua=ua)
    errors = []
    state = {}
    if ua:
        try:
            state["ciks"] = dict(_news_ciks(fetcher))
        except N.NewsError as e:
            errors.append(f"SEC-Tickerliste: {e}")
    # SEC 8-K gibt es nur für US-Firmen: nur Ticker ohne Börsen-Suffix fragen
    us = {t: n for t, n in watch.items() if "." not in t and "-USD" not in t}
    items = N.collect(fetcher, watch, state, sec=False, errors=errors)
    if ua and state.get("ciks"):
        for t in sorted(us):
            cik = state["ciks"].get(t)
            if not cik:
                continue
            try:
                data = fetcher.get(N.SEC_FEED % (cik, "8-K", 10), "sec-8-K-%s.xml" % cik, sec=True)
                items += N.parse_sec_atom(data, t)
            except N.NewsError as e:
                errors.append(f"SEC 8-K für {t}: {e}")
    N.tag(items, watch)
    items = N.dedupe(items)
    now = _dt.datetime.now(_dt.timezone.utc)
    sel = N.select(items, {}, now, hours=hours, show_all=True)
    return {"items": N._json_ready(sel), "errors": errors, "sec": bool(ua), "hours": hours}


# ── Copycat: 13F-Portfolios berühmter Investoren ──────────────────────────────

def _edgar():
    ua = E.user_agent(None, None)
    if not ua:
        raise E.EdgarError("SEC_USER_AGENT fehlt in der .env (Format: \"Dein Name deine@mail.ch\"). "
                           "Die SEC verlangt das für jede Abfrage.")
    return E.Fetcher(ua)


def _ticker_index(fetcher):
    return cached(("tickerindex",), 86400, lambda: E.ticker_index(fetcher))


def _quarter(fetcher, cik, before=None):
    meta, rows, _ = E.load_quarter(fetcher, cik, before=before)
    index = _ticker_index(fetcher)
    overrides = E.load_overrides([E.CLASSES])
    positions, total = E.build(meta, rows, index, overrides, combine=True)
    return meta, positions, total


def copycat_report(cik):
    fetcher = _edgar()
    meta, positions, total = _quarter(fetcher, cik)
    changes, prior_period = [], None
    try:
        pmeta, ppositions, _ = _quarter(fetcher, cik, before=meta["period"])
        changes = E.diff(positions, ppositions)
        prior_period = pmeta["period"]
    except E.EdgarError as e:
        meta["notes"].append(f"Vorquartal nicht verfügbar: {e}")
    return {"meta": meta, "positions": positions, "total": total,
            "changes": changes, "prior_period": prior_period}


@trader_bp.route("/api/trader/copycat/filers", methods=["GET"])
def copycat_filers():
    q = (request.args.get("q") or "").strip()
    bundled = E.load_filers()
    hits = E.find_bundled(q, bundled) if q else bundled
    out = [{"cik": f["cik"], "name": f["name"], "person": f.get("person", ""), "bundled": True}
           for f in hits]
    if q and len(out) < 5 and E.user_agent(None, None):
        try:
            known = {f["cik"] for f in out}
            out += [dict(h, person="", bundled=False) for h in E.search_sec(_edgar(), q)[:10]
                    if h["cik"] not in known]
        except E.EdgarError as e:
            return jsonify({"filers": out, "error": str(e)})
    return jsonify({"filers": out, "sec_ready": bool(E.user_agent(None, None))})


@trader_bp.route("/api/trader/copycat/<cik>", methods=["GET"])
def copycat(cik):
    try:
        top = int(request.args.get("top", 10))
        rep = cached(("13f", E.cik10(cik)), 6 * 3600, lambda: copycat_report(cik))
        weights, skipped = E.copy_weights(rep["positions"], top=top)
    except (E.EdgarError, ValueError) as e:
        return err(str(e), 502 if isinstance(e, E.EdgarError) else 400)
    stocks = [p for p in rep["positions"] if E.is_stock(p)][:25]
    return jsonify({
        "meta": rep["meta"], "total": rep["total"], "prior_period": rep["prior_period"],
        "holdings": [{k: p.get(k) for k in ("name", "issuer", "ticker", "value", "shares", "pct_of_13f",
                                            "ticker_check", "tickers")} for p in stocks],
        "changes": [c for c in rep["changes"] if c["status"] != "UNCHANGED"][:30],
        "weights": weights, "skipped": skipped[:20], "caveats": E.CAVEATS,
    })


@trader_bp.route("/api/trader/copycat/<cik>/apply", methods=["POST"])
def copycat_apply(cik):
    data = request.json or {}
    try:
        top = int(data.get("top", 10))
        rep = cached(("13f", E.cik10(cik)), 6 * 3600, lambda: copycat_report(cik))
        weights, _ = E.copy_weights(rep["positions"], top=top, exclude=_split_list(data.get("exclude"), True))
    except (E.EdgarError, ValueError) as e:
        return err(str(e), 502 if isinstance(e, E.EdgarError) else 400)
    rows = [{"ticker": w["ticker"], "name": w["name"], "weight": w["weight"]} for w in weights]
    m = rep["meta"]
    save_targets(rows, f"13F {m['filer']} ({m['period']}, Top {top})")
    return get_targets()


# ── Kongress-Trades (House PTRs) ──────────────────────────────────────────────

def ptr_index(year):
    return cached(("ptrindex", year), 1800, lambda: P.load_index(year))


def ptr_filings(members, days):
    today = _dt.date.today()
    since = (today - _dt.timedelta(days=days)).isoformat()
    rows = []
    for year in sorted({today.year, int(since[:4])}, reverse=True):
        try:
            rows += ptr_index(year)
        except P.PtrError:
            if year == today.year:
                raise
    return P.select(rows, members or None, kind="P", since=since)


def ptr_report(doc_id, year, filed=None):
    def load():
        path, report = P._fetch_and_read(doc_id, year, PTR_DIR, filed=filed)
        if report is None:
            raise P.PtrError("PDF konnte nicht gelesen werden")
        report.pop("pdf", None)
        return report
    return cached(("ptr", doc_id), 7 * 86400, load)


@trader_bp.route("/api/trader/pelosi", methods=["GET"])
def pelosi():
    cfg = load_settings()
    members = _split_list(request.args["members"]) if "members" in request.args else cfg["pelosi_members"]
    try:
        days = max(1, min(int(request.args.get("days", 90)), 365))
        rows = ptr_filings(members, days)
    except (P.PtrError, ValueError) as e:
        return err(str(e), 502)
    first_time = seen_count("ptr_ui") == 0
    fresh = claim_seen("ptr_ui", [r["doc_id"] for r in rows])
    for r in rows:
        r["new"] = r["doc_id"] in fresh and not first_time
    return jsonify({"filings": rows[:100], "members": members, "days": days})


@trader_bp.route("/api/trader/pelosi/ptr/<doc_id>", methods=["GET"])
def pelosi_ptr(doc_id):
    if not re.fullmatch(r"\d{4,12}", doc_id):
        return err("Ungültige Dokument-ID.")
    try:
        year = int(request.args.get("year") or _dt.date.today().year)
        filed = request.args.get("filed") or None
        if filed and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", filed):
            filed = None
        report = dict(ptr_report(doc_id, year, filed))
    except (P.PtrError, ValueError) as e:
        return err(str(e), 502)
    mine = {(r.get("ticker") or "").strip().upper() for r in load_portfolio_rows()} - {""}
    report["rows"] = [dict(r, in_portfolio=(r.get("ticker") or "").upper() in mine) for r in report["rows"]]
    return jsonify(report)


def pelosi_job(push):
    """Scheduler: neue PTRs der beobachteten Abgeordneten per Pushover melden."""
    try:
        cfg = load_settings()
        if not cfg.get("pelosi_push") or not cfg.get("pelosi_members"):
            return
        first_time = seen_count("ptr_push") == 0
        rows = ptr_filings(cfg["pelosi_members"], 7 if first_time else 60)
        fresh = claim_seen("ptr_push", [r["doc_id"] for r in rows])
        if first_time:
            return  # erster Lauf: nur merken, nicht alles auf einmal melden
        for r in rows:
            if r["doc_id"] not in fresh:
                continue
            lines = [f"{r['name']} ({r['district']}) hat am {r['filed']} Trades gemeldet."]
            try:
                rep = ptr_report(r["doc_id"], r["year"], r["filed"])
                for t in rep["rows"][:5]:
                    lines.append(f"• {t['type_name']} {t.get('ticker') or t['asset']}: {t['amount']} ({t['date']})")
            except P.PtrError:
                pass
            lines.append(r["url"])
            push("Neue Kongress-Trades 🏛️", "\n".join(lines))
    except Exception:
        pass
