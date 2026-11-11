from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
import os
import requests
import anthropic
import yfinance as yf
from decimal import Decimal
from datetime import date, timedelta

finance_bp = Blueprint("finance", __name__)

# CoinGecko ticker → ID mapping for common cryptocurrencies
COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "ADA": "cardano",
    "DOT": "polkadot", "AVAX": "avalanche-2", "MATIC": "matic-network",
    "LINK": "chainlink", "UNI": "uniswap", "XRP": "ripple", "BNB": "binancecoin",
    "DOGE": "dogecoin", "LTC": "litecoin", "ATOM": "cosmos", "NEAR": "near",
    "FTM": "fantom", "ALGO": "algorand", "VET": "vechain", "SAND": "the-sandbox",
    "MANA": "decentraland", "CRO": "crypto-com-chain", "SHIB": "shiba-inu",
    "TRX": "tron", "ETC": "ethereum-classic", "XLM": "stellar",
}


def get_db():
    return psycopg2.connect(os.environ.get("DATABASE_URL", ""))


def _to_float(v):
    return float(v) if isinstance(v, Decimal) else v


def _row_to_dict(row):
    return {k: _to_float(v) for k, v in dict(row).items()}


# ── Market Data Fetchers ───────────────────────────────────────────────────────

def fetch_stock_data(ticker: str) -> dict:
    """Current price + day-change % via yfinance. Handles GBp→GBP conversion."""
    try:
        t = yf.Ticker(ticker)
        fi = t.fast_info
        current = fi.last_price
        prev = fi.previous_close
        currency = getattr(fi, "currency", "USD") or "USD"
        # LSE quotes in GBp (pence) → divide by 100 to get GBP
        if currency in ("GBp", "GBX"):
            if current: current = current / 100
            if prev:    prev    = prev / 100
            currency = "GBP"
        change_pct = round((current - prev) / prev * 100, 2) if current and prev else None
        return {
            "price": round(float(current), 4) if current else None,
            "change_pct": change_pct,
            "currency": currency,
        }
    except Exception:
        return {"price": None, "change_pct": None, "currency": None}


def fetch_crypto_data(ticker: str) -> dict:
    """Current price + 24 h change % via CoinGecko (free, no key)."""
    coin_id = COINGECKO_IDS.get(ticker.upper(), ticker.lower())
    try:
        url = (
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={coin_id}&vs_currencies=usd&include_24hr_change=true"
        )
        data = requests.get(url, timeout=8).json().get(coin_id, {})
        return {
            "price": data.get("usd"),
            "change_pct": round(data.get("usd_24h_change", 0), 2),
            "currency": "USD",
        }
    except Exception:
        return {"price": None, "change_pct": None, "currency": None}


# Obergrenze für Claudes Antwort. Hoch genug für eine vollständige Analyse; bezahlt
# wird nur, was Claude tatsächlich schreibt (typisch 2'000–4'000 Tokens).
MAX_ANALYSIS_TOKENS = 16000


def _response_text(msg):
    """Alle Textblöcke der Antwort zusammen (nicht nur den ersten)."""
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def _fmt(v, cur, d=2):
    if v is None:
        return "n/a"
    return f"{v:,.{d}f} {cur}".replace(",", "'")


def _headlines(rows, per_ticker=3):
    """Neueste Yahoo-Schlagzeilen pro Ticker (ohne API-Key), letzte 7 Tage."""
    from trader import collect_news, news_watch
    try:
        res = collect_news(news_watch(rows), hours=24 * 7, sec=False)
    except Exception:
        return {}
    out = {}
    for it in res["items"]:
        for t in (it.get("touches") or [it["ticker"]]):
            key = t[:-4] if t.endswith("-USD") else t
            if len(out.setdefault(key, [])) < per_ticker:
                out[key].append(it["title"])
    return out


# ── AI Analysis Endpoint ───────────────────────────────────────────────────────

@finance_bp.route("/api/finance/analyze", methods=["POST"])
def analyze_portfolio():
    from trader import build_plan, load_settings, positions_data

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM user_financial_profile WHERE id = 1")
    profile_row = cur.fetchone()
    cur.close()
    conn.close()
    profile = _row_to_dict(profile_row) if profile_row else {}

    cfg = load_settings()
    data = positions_data(cfg)
    if not data["positions"]:
        return jsonify({"error": "Portfolio ist leer – bitte zuerst Positionen hinzufügen."}), 400
    base = data["currency"]
    total = data["total"]
    news = _headlines(data["positions"])
    # Kern-Satellit-Ziel aus "Mein Ziel" (Topf und Zielanteil je Titel)
    plan = build_plan(cfg)
    plan_by_t = {i["ticker"]: i for i in plan["items"]}
    has_goal = plan["saved"]

    # Positionsblöcke für den Prompt: Werte in Basiswährung, Kurse in Originalwährung
    blocks = []
    for p in sorted(data["positions"], key=lambda p: -(p.get("value") or 0)):
        t = (p.get("ticker") or "").upper()
        cur_ = p.get("currency") or "?"
        lines = [f"**{p['asset_name']} ({t or '–'})** | {p['asset_type']}",
                 f"  Menge: {p['quantity']} × Kaufkurs {p['buy_price']} {cur_}"]
        if p.get("price") is None:
            lines.append("  Aktueller Kurs: nicht verfügbar")
        else:
            chg = p.get("change_pct")
            lines.append(f"  Aktueller Kurs: {p['price']} {cur_}" + (f" ({chg:+.2f}% heute)" if chg is not None else ""))
            lines.append("  Gewinn/Verlust seit Kauf: "
                         + (f"{p['gain_pct']:+.2f}% ({_fmt(p['gain'], base)})" if p.get("gain_pct") is not None else "n/a"))
            lines.append(f"  Positionswert: {_fmt(p['value'], base)} = {p['weight']:.1f}% des Portfolios")
        if has_goal and t in plan_by_t:
            it = plan_by_t[t]
            lines.append(f"  Topf: {'KERN' if it['bucket'] == 'core' else 'SATELLIT'} – Ziel {it['target_pct']:.1f}% des Portfolios")
        if news.get(t):
            lines.append("  Neueste Schlagzeilen:\n  " + "\n  ".join(news[t]))
        blocks.append("\n".join(lines))
    positions_text = "\n\n".join(blocks)

    risk = profile.get("risk_tolerance") or "nicht angegeben"
    goals = profile.get("investment_goals") or "nicht angegeben"
    budget = profile.get("monthly_budget")
    budget_str = f"{_fmt(budget, base, 0)}/Monat" if budget else "nicht angegeben"
    cash = cfg.get("cash") or 0
    sat_free = plan.get("satellite_monthly") or 0
    tp = plan.get("take_profit_pct")
    if has_goal:
        strategy = (f"## Strategie des Investors (Kern-Satellit, selbst festgelegt)\n"
                    f"- Ziel: {plan['core_share']:g}% Kern (breite ETFs, langfristig liegen lassen) / "
                    f"{100 - plan['core_share']:g}% Satellit (Einzelaktien und Themen, aktiv)\n"
                    f"- Kern heute: {plan['core_now_pct']:.0f}%. Umbau nur mit neuem Geld, ohne Verkäufe im Kern.\n"
                    f"- Vom Monatsbudget sind {_fmt(sat_free, base, 0)} frei für Satellit-Titel nach eigener Wahl.\n"
                    + (f"- Eigene Regel: Gewinne im Satelliten ab +{tp:g}% mitnehmen und in den Kern stecken.\n" if tp else ""))
        tasks = f"""### 1. Struktur-Check
Passt die Kern-Satellit-Aufteilung ({plan['core_share']:g}/{100 - plan['core_share']:g}) und die Aufteilung innerhalb des Kerns zum Profil? Klumpenrisiken, Region und Währung, Überschneidungen zwischen ETFs und Einzelaktien. Schlage keine andere Strategie vor, nur konkrete Hinweise, falls etwas nicht zum Profil passt.

### 2. Satellit-Titel
Nur für Titel im SATELLIT:
**[TICKER] → HALTEN / KAUFEN/AUFSTOCKEN / VERKAUFEN**
Denke: (1-2 Sätze: Was sagen Kursverlauf, Gewicht und News zusammen?)
Aktion: (konkret, mit Betrag in {base}. Käufe nur aus dem freien Satellit-Budget von {_fmt(sat_free, base, 0)} pro Monat; Verkäufe als Gewinnmitnahme mit Erlös in den Kern.)

### 3. Kern
Keine Kauf-/Verkaufsaufträge für Kern-Titel – sie werden automatisch mit neuem Geld aufgebaut und bleiben liegen. Höchstens 2 Sätze, ob die Kern-Titel ihre Aufgabe (breite Streuung) erfüllen.

### 4. Impuls-Check
Max. 3 Sätze: Warum die langfristige Strategie (Profil: {risk}, Ziel: {goals}) Vorrang vor kurzfristigen Marktreaktionen hat."""
    else:
        strategy = "## Strategie\nDer Investor hat in der App noch kein Kern-Satellit-Ziel gespeichert.\n"
        tasks = f"""### 1. Portfolio-Gesamtlage
Gesamtwert, Diversifikation nach Anlageklasse, Region und Währung, konkrete Klumpenrisiken (welche Position dominiert zu stark und warum ist das problematisch? Achte auf Überschneidungen zwischen ETFs und Einzelaktien).

### 2. Positions-Analyse
Für jede Position:
**[TICKER] → HALTEN / KAUFEN/AUFSTOCKEN / VERKAUFEN**
Denke: (1-2 Sätze Chain of Thought: Was sagen Kursverlauf, Gewicht und News zusammen?)
Aktion: (Konkrete Handlungsanweisung mit Betrag in {base} oder „Halten, kein Handlungsbedarf.")

### 3. Impuls-Check
Max. 3 Sätze: Warum die langfristige Strategie (Profil: {risk}, Ziel: {goals}) Vorrang vor kurzfristigen Marktreaktionen hat. Empfiehl, im Tab „Mein Ziel" eine Struktur festzulegen."""

    prompt = f"""Du bist ein hochdisziplinierter, risikobewusster Finanzanalyst. Dein Arbeitsprinzip: Schrittweises Denken vor jeder Empfehlung – dann präzise, konkrete Handlungsanweisungen. Respektiere die Strategie, die der Investor selbst festgelegt hat.

## Investorenprofil
- Risikotoleranz: {risk}
- Investitionsziele: {goals}
- Monatliches Budget: {budget_str}

{strategy}
## Portfolio
Aktueller Wert: {_fmt(total['value'], base)} (Einstand {_fmt(total['cost'], base)}, {total['gain_pct'] if total['gain_pct'] is not None else 0:+.2f}%), Cash im Depot: {_fmt(cash, base)}.
Alle Werte in {base} umgerechnet; Kurse in der Handelswährung des Titels.

{positions_text}

---

Analysiere das Portfolio auf Deutsch in genau diesen Abschnitten:

{tasks}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            # 1'200 Tokens reichten bei 7 Positionen nicht: der Text brach mitten im Satz ab
            max_tokens=MAX_ANALYSIS_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        analysis = _response_text(msg)
    except Exception as e:
        err = str(e)
        if "credit balance" in err or "billing" in err.lower():
            return jsonify({"error": "Kein API-Guthaben. Bitte unter console.anthropic.com aufladen."}), 402
        return jsonify({"error": "Claude-Fehler: " + err[:300]}), 502

    return jsonify({
        "analysis": analysis,
        "truncated": msg.stop_reason == "max_tokens",
        "portfolio_value": total["value"],
        "currency": base,
        "total": total,
    })


# ── Investment Recommendations ────────────────────────────────────────────────

@finance_bp.route("/api/finance/recommend_investments", methods=["POST"])
def recommend_investments():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    from trader import load_settings, positions_data

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM user_financial_profile WHERE id = 1")
    profile_row = cur.fetchone()
    cur.close()
    conn.close()

    profile = _row_to_dict(profile_row) if profile_row else {}
    data = positions_data(load_settings())
    base = data["currency"]
    risk = profile.get("risk_tolerance") or "nicht angegeben"
    goals = profile.get("investment_goals") or "nicht angegeben"
    budget = profile.get("monthly_budget")
    budget_str = f"{_fmt(budget, base, 0)}/Monat" if budget else "nicht angegeben"

    if data["positions"]:
        type_weight: dict = {}
        for p in data["positions"]:
            t = p.get("asset_type") or "Unbekannt"
            type_weight[t] = type_weight.get(t, 0) + (p.get("weight") or 0)
        portfolio_lines = "\n".join(
            f"- {p['asset_name']} ({p.get('ticker') or '–'}) [{p['asset_type']}]: "
            + (f"{_fmt(p['value'], base)} = {p['weight']:.1f}% des Portfolios, gehandelt in {p['currency']}"
               if p.get("value") is not None else "Kurs nicht verfügbar")
            for p in sorted(data["positions"], key=lambda p: -(p.get("value") or 0))
        )
        type_summary = ", ".join(f"{k}: {v:.0f}%" for k, v in type_weight.items())
        portfolio_lines += f"\nGesamtwert: {_fmt(data['total']['value'], base)}"
    else:
        portfolio_lines = "Noch kein Portfolio vorhanden."
        type_summary = "leer"

    prompt = f"""Du bist ein erfahrener, unabhängiger Anlageberater. Analysiere das bestehende Portfolio und empfiehl 3-4 konkrete neue Investments, die die Diversifikation sinnvoll ergänzen.

## Investorenprofil
- Risikotoleranz: {risk}
- Investitionsziele: {goals}
- Monatliches Budget: {budget_str}

## Bestehendes Portfolio
{portfolio_lines}
Aktuelle Zusammensetzung: {type_summary}

---

Empfiehl genau 3-4 neue Investments auf Deutsch. Format pro Empfehlung:

**[NAME] ([BÖRSENTICKER])**
Kategorie: ETF / Aktie / Anleihe / Sonstiges
Warum: (2 Sätze: Welche Lücke füllt dies? Warum passt es zu Risikoprofil und Zielen?)
Einstieg: (Konkrete Empfehlung mit Stückzahl und Betrag in {base}, z.B. „3 Anteile / ca. 270 {base}")

Wichtig: Prüfe Überschneidungen mit bestehenden ETFs, bevor du etwas empfiehlst. Nutze echte, handelbare Börsenticker im Yahoo-Format (z.B. IWDA.L für MSCI World in USD auf LSE, IWDA.AS für MSCI World auf Euronext Amsterdam, EMIM.L für Emerging Markets auf LSE, VUSA.L für S&P 500 auf LSE, XDWD.DE für MSCI World auf Xetra). Bevorzuge für langfristige Anleger kosteneffiziente, liquide ETFs mit niedrigen TERs."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=MAX_ANALYSIS_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        recommendations = _response_text(msg)
    except Exception as e:
        err = str(e)
        if "credit balance" in err or "billing" in err.lower():
            return jsonify({"error": "Kein API-Guthaben."}), 402
        return jsonify({"error": "Claude-Fehler: " + err[:300]}), 502

    return jsonify({"recommendations": recommendations, "truncated": msg.stop_reason == "max_tokens"})


# ── Portfolio CRUD ─────────────────────────────────────────────────────────────

@finance_bp.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM user_portfolio ORDER BY asset_type ASC, asset_name ASC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([_row_to_dict(r) for r in rows])


@finance_bp.route("/api/portfolio", methods=["POST"])
def create_portfolio_entry():
    data = request.json or {}
    asset_name = str(data.get("asset_name", "")).strip()
    ticker = str(data.get("ticker", "")).strip() or None
    asset_type = str(data.get("asset_type", "")).strip()
    notes = str(data.get("notes", "")).strip() or None

    if not asset_name:
        return jsonify({"error": "asset_name required"}), 400
    if not asset_type:
        return jsonify({"error": "asset_type required"}), 400
    try:
        quantity = float(data["quantity"])
        buy_price = float(data["buy_price"])
        assert quantity > 0 and buy_price >= 0
    except (KeyError, ValueError, TypeError, AssertionError):
        return jsonify({"error": "Valid quantity and buy_price required"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """INSERT INTO user_portfolio (asset_name, ticker, asset_type, quantity, buy_price, notes)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (asset_name, ticker, asset_type, quantity, buy_price, notes),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    cur.execute("SELECT * FROM user_portfolio WHERE id = %s", (new_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify(_row_to_dict(row)), 201


@finance_bp.route("/api/portfolio/<int:entry_id>", methods=["PUT"])
def update_portfolio_entry(entry_id):
    data = request.json or {}
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM user_portfolio WHERE id = %s", (entry_id,))
    entry = cur.fetchone()
    if not entry:
        cur.close()
        conn.close()
        return jsonify({"error": "Not found"}), 404

    asset_name = data.get("asset_name", entry["asset_name"])
    ticker = data.get("ticker", entry["ticker"])
    asset_type = data.get("asset_type", entry["asset_type"])
    notes = data.get("notes", entry["notes"])
    try:
        quantity = float(data.get("quantity", entry["quantity"]))
        buy_price = float(data.get("buy_price", entry["buy_price"]))
    except (ValueError, TypeError):
        cur.close()
        conn.close()
        return jsonify({"error": "Invalid quantity or buy_price"}), 400

    cur.execute(
        """UPDATE user_portfolio
           SET asset_name=%s, ticker=%s, asset_type=%s, quantity=%s, buy_price=%s, notes=%s
           WHERE id=%s""",
        (asset_name, ticker, asset_type, quantity, buy_price, notes, entry_id),
    )
    conn.commit()
    cur.execute("SELECT * FROM user_portfolio WHERE id = %s", (entry_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify(_row_to_dict(row))


@finance_bp.route("/api/portfolio/<int:entry_id>", methods=["DELETE"])
def delete_portfolio_entry(entry_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_portfolio WHERE id = %s", (entry_id,))
    conn.commit()
    cur.close()
    conn.close()
    return "", 204


# ── Financial Profile ─────────────────────────────────────────────────────────

@finance_bp.route("/api/financial-profile", methods=["GET"])
def get_financial_profile():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM user_financial_profile WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return jsonify({"id": 1, "risk_tolerance": None, "investment_goals": None, "monthly_budget": None})
    return jsonify(_row_to_dict(row))


@finance_bp.route("/api/financial-profile", methods=["PUT"])
def update_financial_profile():
    data = request.json or {}
    risk_tolerance = data.get("risk_tolerance")
    investment_goals = data.get("investment_goals")
    monthly_budget = data.get("monthly_budget")

    if monthly_budget is not None:
        try:
            monthly_budget = float(monthly_budget)
            assert monthly_budget >= 0
        except (ValueError, TypeError, AssertionError):
            return jsonify({"error": "Invalid monthly_budget"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """INSERT INTO user_financial_profile (id, risk_tolerance, investment_goals, monthly_budget)
           VALUES (1, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE
           SET risk_tolerance = EXCLUDED.risk_tolerance,
               investment_goals = EXCLUDED.investment_goals,
               monthly_budget = EXCLUDED.monthly_budget""",
        (risk_tolerance, investment_goals, monthly_budget),
    )
    conn.commit()
    cur.execute("SELECT * FROM user_financial_profile WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify(_row_to_dict(row))
