from flask import Flask, request, jsonify, render_template
import psycopg2
import psycopg2.extras
from datetime import date, datetime
import os
import re
import atexit
import anthropic
import requests
import feedparser
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

app = Flask(__name__)


# ── Pushover ──────────────────────────────────────────────────────────────────

def send_push_notification(title, message):
    user_key = os.environ.get("PUSHOVER_USER_KEY", "").strip()
    api_token = os.environ.get("PUSHOVER_API_TOKEN", "").strip()
    if not user_key or not api_token:
        return
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": api_token, "user": user_key, "title": title, "message": message},
            timeout=5,
        )
    except Exception:
        pass


def daily_reminder():
    today = date.today().isoformat()
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT title, time FROM termine WHERE date = %s ORDER BY time IS NULL, time ASC",
            (today,),
        )
        termine = cur.fetchall()
        cur.execute(
            "SELECT title FROM todos WHERE completed = 0 AND (due_date IS NULL OR due_date <= %s) ORDER BY due_date ASC",
            (today,),
        )
        todos = cur.fetchall()
        cur.close()
        conn.close()
    except Exception:
        return

    parts = []
    if termine:
        parts.append("Termine: " + ", ".join(
            f"{r['time'] or '?'} Uhr – {r['title']}" for r in termine
        ))
    if todos:
        parts.append("To-Dos: " + ", ".join(r["title"] for r in todos[:5]))

    if parts:
        send_push_notification("Tagesübersicht", " | ".join(parts))
    else:
        send_push_notification("Guten Morgen, Andreas!", "Heute keine Termine oder offenen To-Dos.")


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(os.environ.get("DATABASE_URL", ""))


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS todos (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            due_date TEXT,
            completed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS calorie_goal (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            goal INTEGER NOT NULL DEFAULT 2500
        )
    """)
    cur.execute("""
        INSERT INTO calorie_goal (id, goal) VALUES (1, 2500)
        ON CONFLICT DO NOTHING
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS calorie_entries (
            id SERIAL PRIMARY KEY,
            date TEXT NOT NULL DEFAULT to_char(CURRENT_DATE, 'YYYY-MM-DD'),
            description TEXT NOT NULL,
            calories INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS termine (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            date TEXT NOT NULL DEFAULT to_char(CURRENT_DATE, 'YYYY-MM-DD'),
            time TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/scan")
@app.route("/food-upload")
def scan():
    return render_template("scan.html")


# ── Todos ─────────────────────────────────────────────────────────────────────

@app.route("/api/todos", methods=["GET"])
def get_todos():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM todos ORDER BY completed ASC, due_date ASC NULLS LAST, created_at DESC"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/todos", methods=["POST"])
def create_todo():
    data = request.json or {}
    title = str(data.get("title", "")).strip()
    due_date = data.get("due_date") or None
    if not title:
        return jsonify({"error": "Title required"}), 400
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "INSERT INTO todos (title, due_date) VALUES (%s, %s) RETURNING id",
        (title, due_date),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    cur.execute("SELECT * FROM todos WHERE id = %s", (new_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    send_push_notification(
        "Neues To-Do",
        f"{title}" + (f" (fällig: {due_date})" if due_date else ""),
    )
    return jsonify(dict(row)), 201


@app.route("/api/todos/<int:todo_id>", methods=["PUT"])
def update_todo(todo_id):
    data = request.json or {}
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM todos WHERE id = %s", (todo_id,))
    todo = cur.fetchone()
    if not todo:
        cur.close()
        conn.close()
        return jsonify({"error": "Not found"}), 404
    title = data.get("title", todo["title"])
    due_date = data.get("due_date", todo["due_date"])
    completed = data.get("completed", todo["completed"])
    cur.execute(
        "UPDATE todos SET title=%s, due_date=%s, completed=%s WHERE id=%s",
        (title, due_date, int(completed), todo_id),
    )
    conn.commit()
    cur.execute("SELECT * FROM todos WHERE id = %s", (todo_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify(dict(row))


@app.route("/api/todos/<int:todo_id>", methods=["DELETE"])
def delete_todo(todo_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM todos WHERE id = %s", (todo_id,))
    conn.commit()
    cur.close()
    conn.close()
    return "", 204


# ── Calories ──────────────────────────────────────────────────────────────────

@app.route("/api/calories/goal", methods=["GET"])
def get_goal():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT goal FROM calorie_goal WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify({"goal": row["goal"]})


@app.route("/api/calories/goal", methods=["PUT"])
def update_goal():
    data = request.json or {}
    try:
        goal = int(data["goal"])
        assert goal >= 0
    except (KeyError, ValueError, AssertionError):
        return jsonify({"error": "Invalid goal"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE calorie_goal SET goal = %s WHERE id = 1", (goal,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"goal": goal})


@app.route("/api/calories/entries", methods=["GET"])
def get_entries():
    selected_date = request.args.get("date", date.today().isoformat())
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM calorie_entries WHERE date = %s ORDER BY created_at ASC",
        (selected_date,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/calories/entries", methods=["POST"])
def add_entry():
    data = request.json or {}
    description = str(data.get("description", "")).strip()
    entry_date = data.get("date", date.today().isoformat())
    try:
        calories = int(data["calories"])
        assert calories > 0
    except (KeyError, ValueError, AssertionError):
        return jsonify({"error": "Invalid calories"}), 400
    if not description:
        return jsonify({"error": "Description required"}), 400
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "INSERT INTO calorie_entries (date, description, calories) VALUES (%s, %s, %s) RETURNING id",
        (entry_date, description, calories),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    cur.execute("SELECT * FROM calorie_entries WHERE id = %s", (new_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify(dict(row)), 201


@app.route("/api/calories/entries/<int:entry_id>", methods=["DELETE"])
def delete_entry(entry_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM calorie_entries WHERE id = %s", (entry_id,))
    conn.commit()
    cur.close()
    conn.close()
    return "", 204


# ── Termine ───────────────────────────────────────────────────────────────────

@app.route("/api/termine", methods=["GET"])
def get_termine():
    selected_date = request.args.get("date", date.today().isoformat())
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM termine WHERE date = %s ORDER BY time IS NULL, time ASC, created_at ASC",
        (selected_date,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/termine", methods=["POST"])
def create_termin():
    data = request.json or {}
    title = str(data.get("title", "")).strip()
    termin_date = data.get("date", date.today().isoformat())
    termin_time = data.get("time") or None
    if not title:
        return jsonify({"error": "Title required"}), 400
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "INSERT INTO termine (title, date, time) VALUES (%s, %s, %s) RETURNING id",
        (title, termin_date, termin_time),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    cur.execute("SELECT * FROM termine WHERE id = %s", (new_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify(dict(row)), 201


@app.route("/api/termine/<int:termin_id>", methods=["DELETE"])
def delete_termin(termin_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM termine WHERE id = %s", (termin_id,))
    conn.commit()
    cur.close()
    conn.close()
    return "", 204


@app.route("/api/termine/range", methods=["GET"])
def get_termine_range():
    start = request.args.get("start", date.today().isoformat())
    end   = request.args.get("end",   date.today().isoformat())
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM termine WHERE date >= %s AND date < %s ORDER BY date ASC, time IS NULL, time ASC",
        (start, end),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Food analysis ─────────────────────────────────────────────────────────────

ALLOWED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}

@app.route("/api/analyze-food", methods=["POST"])
def analyze_food():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    data = request.get_json(silent=True, force=True) or {}

    image_b64 = (data.get("image") or "").strip()
    if not image_b64:
        return jsonify({
            "error": "Kein Bild übermittelt (Feld: 'image', Base64-String)",
            "received_keys": list(data.keys()),
        }), 400

    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]

    media_type = (data.get("media_type") or "image/jpeg").strip()
    if media_type not in ALLOWED_MIME:
        media_type = "image/jpeg"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=16,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Analysiere dieses Essensfoto. Schätze die Kalorien und antworte NUR mit einer Zahl.",
                        },
                    ],
                }
            ],
        )
    except Exception as e:
        msg = str(e)
        if "credit balance" in msg or "billing" in msg.lower():
            return jsonify({"error": "Kein API-Guthaben. Bitte unter console.anthropic.com aufladen."}), 402
        if "invalid api key" in msg.lower() or "authentication" in msg.lower():
            return jsonify({"error": "Ungültiger API-Key."}), 401
        return jsonify({"error": "API-Fehler: " + msg[:300]}), 502

    raw = message.content[0].text.strip()
    match = re.search(r"\d+", raw)
    if not match:
        return jsonify({"error": "Konnte keine Zahl aus Antwort lesen", "raw": raw}), 500

    return jsonify({"calories": int(match.group())})


# ── Morning Briefing ──────────────────────────────────────────────────────────

USER_NAME = "Andreas"

WMO_CODES = {
    0: "Klarer Himmel", 1: "Überwiegend klar", 2: "Teilweise bewölkt", 3: "Bewölkt",
    45: "Neblig", 48: "Neblig (gefrierend)",
    51: "Leichter Nieselregen", 53: "Mäßiger Nieselregen", 55: "Starker Nieselregen",
    61: "Leichter Regen", 63: "Mäßiger Regen", 65: "Starker Regen",
    71: "Leichter Schneefall", 73: "Mäßiger Schneefall", 75: "Starker Schneefall",
    80: "Leichte Regenschauer", 81: "Mäßige Regenschauer", 82: "Starke Regenschauer",
    95: "Gewitter", 96: "Gewitter mit Hagel", 99: "Gewitter mit schwerem Hagel",
}


def _greeting():
    h = datetime.now().hour
    if h < 12:
        return "Guten Morgen"
    if h < 18:
        return "Guten Tag"
    return "Guten Abend"


def _weather():
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=47.483&longitude=7.550"
            "&current=temperature_2m,weathercode,windspeed_10m"
            "&timezone=Europe%2FZurich"
        )
        data = requests.get(url, timeout=6).json()["current"]
        desc = WMO_CODES.get(data["weathercode"], "unbekannt")
        return f"{desc}, {data['temperature_2m']}°C, Wind {data['windspeed_10m']} km/h"
    except Exception:
        return "Wetter nicht verfügbar"


def _rss_titles(feed_url, max_items=3):
    try:
        feed = feedparser.parse(feed_url)
        return [e.title for e in feed.entries[:max_items]]
    except Exception:
        return []


@app.route("/api/morning-briefing", methods=["GET"])
def morning_briefing():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    today_str = date.today().isoformat()
    today_fmt = date.today().strftime("%A, %d. %B %Y")

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT title, time FROM termine WHERE date = %s ORDER BY time IS NULL, time ASC",
        (today_str,),
    )
    termine_rows = cur.fetchall()
    cur.execute(
        "SELECT title FROM todos WHERE completed = 0 ORDER BY due_date ASC",
    )
    todo_rows = cur.fetchall()
    cur.close()
    conn.close()

    termine_text = (
        ", ".join(
            f"{(r['time'] or '?')} Uhr – {r['title']}" for r in termine_rows
        )
        or "Keine Termine"
    )
    todos_text = (
        ", ".join(r["title"] for r in todo_rows[:5]) or "Keine offenen To-Dos"
    )

    srf = _rss_titles("https://www.srf.ch/news/bnf/rss/1646")
    welt = _rss_titles("https://www.tagesschau.de/xml/rss2")
    news_text = ""
    if srf:
        news_text += "Schweiz: " + " | ".join(srf) + ". "
    if welt:
        news_text += "Welt: " + " | ".join(welt) + "."
    if not news_text:
        news_text = "Keine Nachrichten verfügbar."

    prompt = f"""Daten für das Morgen-Briefing:
- Begrüßung: {_greeting()}, {USER_NAME}!
- Datum: {today_fmt}
- Wetter (Ettingen/Basel): {_weather()}
- Heutige Termine: {termine_text}
- Offene To-Dos: {todos_text}
- Aktuelle Nachrichten: {news_text}

Erstelle aus diesen Daten ein kurzes, flüssig geschriebenes, motivierendes Audio-Briefing zum Vorlesen (maximal 150 Wörter). Keine Markdown-Sterne oder Formatierungen, nur reiner Text."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        briefing = msg.content[0].text.strip().replace("\n", " ")
    except Exception as e:
        err = str(e)
        if "credit balance" in err or "billing" in err.lower():
            return "Kein API-Guthaben.", 402, {"Content-Type": "text/plain; charset=utf-8"}
        return "Claude-Fehler: " + err[:300], 502, {"Content-Type": "text/plain; charset=utf-8"}

    return briefing, 200, {"Content-Type": "text/plain; charset=utf-8"}


# ── Cron Daily Summary ────────────────────────────────────────────────────────

@app.route("/api/cron-daily-summary", methods=["GET", "POST"])
def cron_daily_summary():
    today = date.today().isoformat()
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT title, time FROM termine WHERE date = %s ORDER BY time IS NULL, time ASC",
            (today,),
        )
        termine = cur.fetchall()
        cur.execute(
            "SELECT title FROM todos WHERE completed = 0 AND (due_date IS NULL OR due_date <= %s) ORDER BY due_date ASC",
            (today,),
        )
        todos = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not termine and not todos:
        send_push_notification(
            "Deine Mittags-Übersicht 🕒",
            "Alles erledigt – keine offenen To-Dos oder Termine für heute. 🎉",
        )
        return jsonify({"sent": True, "termine": 0, "todos": 0})

    lines = []
    if termine:
        lines.append("📅 Termine heute:")
        for r in termine:
            t = r["time"] or "?"
            lines.append(f"  • {t} Uhr – {r['title']}")
    if todos:
        lines.append("✅ Offene To-Dos:")
        for r in todos[:10]:
            lines.append(f"  • {r['title']}")

    message = "\n".join(lines)
    send_push_notification("Deine Mittags-Übersicht 🕒", message)
    return jsonify({"sent": True, "termine": len(termine), "todos": len(todos), "message": message})


# ── Startup ───────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone="Europe/Zurich")
scheduler.add_job(daily_reminder, "cron", hour=8, minute=0)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=5000)
