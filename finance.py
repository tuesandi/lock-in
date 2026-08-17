from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
import os

finance_bp = Blueprint("finance", __name__)


def get_db():
    return psycopg2.connect(os.environ.get("DATABASE_URL", ""))


# ── Portfolio ─────────────────────────────────────────────────────────────────

@finance_bp.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM user_portfolio ORDER BY asset_type ASC, asset_name ASC"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


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
    return jsonify(dict(row)), 201


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
    return jsonify(dict(row))


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
    return jsonify(dict(row))


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
    return jsonify(dict(row))
