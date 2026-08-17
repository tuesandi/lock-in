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
    """Current price + day-change % via yfinance."""
    try:
        t = yf.Ticker(ticker)
        fi = t.fast_info
        current = fi.last_price
        prev = fi.previous_close
        change_pct = round((current - prev) / prev * 100, 2) if current and prev else None
        return {
            "price": round(float(current), 4) if current else None,
            "change_pct": change_pct,
            "currency": getattr(fi, "currency", "USD"),
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


def fetch_news(ticker: str, asset_type: str) -> list[str]:
    """Latest 3 headlines via Finnhub (skipped if no API key)."""
    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not api_key or not ticker:
        return []
    try:
        if "crypto" in asset_type.lower():
            url = f"https://finnhub.io/api/v1/news?category=crypto&token={api_key}"
        else:
            from_date = (date.today() - timedelta(days=30)).isoformat()
            to_date = date.today().isoformat()
            url = (
                f"https://finnhub.io/api/v1/company-news"
                f"?symbol={ticker}&from={from_date}&to={to_date}&token={api_key}"
            )
        articles = requests.get(url, timeout=8).json()
        if isinstance(articles, list):
            return [a["headline"] for a in articles[:3] if a.get("headline")]
    except Exception:
        pass
    return []


def fetch_market_data(position: dict) -> dict:
    """Merge price, change %, gain/loss and news for one portfolio position."""
    ticker = (position.get("ticker") or "").strip()
    asset_type = (position.get("asset_type") or "").lower()

    if any(x in asset_type for x in ("crypto", "krypto", "Krypto", "Crypto")):
        market = fetch_crypto_data(ticker)
    else:
        market = fetch_stock_data(ticker)

    news = fetch_news(ticker, asset_type)

    current_price = market["price"]
    buy_price = _to_float(position.get("buy_price") or 0)
    quantity = _to_float(position.get("quantity") or 0)

    total_value = round(current_price * quantity, 2) if current_price else None
    gain_loss_pct = (
        round((current_price - buy_price) / buy_price * 100, 2)
        if current_price and buy_price
        else None
    )

    return {
        **{k: _to_float(v) for k, v in position.items()},
        "current_price": current_price,
        "change_pct_today": market["change_pct"],
        "currency": market["currency"],
        "total_value": total_value,
        "gain_loss_pct": gain_loss_pct,
        "news": news,
    }


# ── AI Analysis Endpoint ───────────────────────────────────────────────────────

@finance_bp.route("/api/finance/analyze", methods=["POST"])
def analyze_portfolio():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM user_portfolio ORDER BY asset_type ASC, asset_name ASC")
    raw_positions = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM user_financial_profile WHERE id = 1")
    profile_row = cur.fetchone()
    cur.close()
    conn.close()

    if not raw_positions:
        return jsonify({"error": "Portfolio ist leer – bitte zuerst Positionen hinzufügen."}), 400

    profile = _row_to_dict(profile_row) if profile_row else {}

    # Fetch live market data for each position
    enriched = [fetch_market_data(p) for p in raw_positions]

    # Calculate portfolio totals
    total_value = sum(p["total_value"] for p in enriched if p["total_value"] is not None)
    portfolio_value_str = f"${total_value:,.2f}" if total_value else "unbekannt"

    # Build position blocks for the prompt
    position_blocks = []
    for p in enriched:
        buy = p.get("buy_price")
        curr = p.get("current_price")
        qty = p.get("quantity")
        change = p.get("change_pct_today")
        gl = p.get("gain_loss_pct")
        tv = p.get("total_value")
        news_lines = "\n  ".join(p["news"]) if p["news"] else "keine Schlagzeilen verfügbar"
        currency = p.get("currency") or "USD"

        position_blocks.append(
            f"**{p['asset_name']} ({p.get('ticker', '–')})** | {p['asset_type']}\n"
            f"  Menge: {qty} × Kaufkurs {buy} {currency}\n"
            f"  Aktueller Kurs: {curr} {currency}"
            + (f" ({change:+.2f}% heute)" if change is not None else "")
            + f"\n  Gewinn/Verlust seit Kauf: " + (f"{gl:+.2f}%" if gl is not None else "n/a")
            + f"\n  Positionswert: " + (f"${tv:,.2f}" if tv else "n/a")
            + (f"\n  Neueste Schlagzeilen:\n  {news_lines}" if p["news"] else "")
        )

    positions_text = "\n\n".join(position_blocks)

    risk = profile.get("risk_tolerance") or "nicht angegeben"
    goals = profile.get("investment_goals") or "nicht angegeben"
    budget = profile.get("monthly_budget")
    budget_str = f"${budget:,.2f}/Monat" if budget else "nicht angegeben"

    prompt = f"""Du bist ein hochdisziplinierter, risikobewusster Finanzanalyst mit jahrzehntelanger Erfahrung in Aktien, ETFs und Kryptowährungen. Dein Stil ist präzise, direkt und handlungsorientiert – keine Floskeln, keine übertriebene Vorsicht.

## Investorenprofil
- Risikotoleranz: {risk}
- Investitionsziele: {goals}
- Monatliches Investitionsbudget: {budget_str}

## Portfolio (Gesamtwert: ~{portfolio_value_str})

{positions_text}

---

Erstelle jetzt eine strukturierte Analyse auf Deutsch mit genau diesen drei Abschnitten:

### 1. Portfolio-Gesamtlage
Bewerte Gesamtwert, Diversifikation nach Anlageklasse und identifiziere konkrete Klumpenrisiken (welche Position oder Klasse dominiert zu stark?).

### 2. Positions-Analyse
Gib für jede Position eine klare Empfehlung in diesem Format:
**[TICKER] → HALTEN / KAUFEN/AUFSTOCKEN / VERKAUFEN**
Begründung: (max. 2 Sätze – ausschließlich basierend auf Kursverlauf, Gewinn/Verlust und aktuellen Schlagzeilen)

### 3. Impuls-Check
Eine kurze, direkte Erinnerung daran, warum die langfristige Strategie – basierend auf Risikoprofil und Zielen – Vorrang vor kurzfristigen Impulsen haben sollte. Max. 3 Sätze."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        analysis = msg.content[0].text.strip()
    except Exception as e:
        err = str(e)
        if "credit balance" in err or "billing" in err.lower():
            return jsonify({"error": "Kein API-Guthaben. Bitte unter console.anthropic.com aufladen."}), 402
        return jsonify({"error": "Claude-Fehler: " + err[:300]}), 502

    return jsonify({
        "analysis": analysis,
        "portfolio_value": total_value,
        "positions": enriched,
    })


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
