import os
import io
import re
import sqlite3
import logging
import threading
import asyncio
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import matplotlib
matplotlib.use("Agg")  # headless, required on Render
import matplotlib.pyplot as plt

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN env var is not set.")

# Use /data/finance.db if you mount a Render Disk at /data.
# Otherwise it falls back to the working dir (wiped on redeploy).
DB_PATH = os.environ.get("DB_PATH", "/data/finance.db")
try:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
except Exception:
    DB_PATH = "finance.db"  # fallback for local testing

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# CATEGORY RULES (auto-detect)
# -------------------------------------------------------------------
CATEGORY_RULES = [
    ("food",      r"\b(coffee|lunch|dinner|breakfast|snack|restaurant|pizza|burger|groceries|food|eat|cafe)\b"),
    ("transport", r"\b(gas|fuel|uber|lyft|taxi|bus|train|metro|parking|toll|transport)\b"),
    ("shopping",  r"\b(clothes|shoes|amazon|store|shopping|gift|mall|target|walmart)\b"),
    ("bills",     r"\b(rent|electric|water|internet|phone|bill|subscription|netflix|spotify)\b"),
    ("health",    r"\b(doctor|pharmacy|medicine|gym|health|dentist|hospital)\b"),
    ("fun",       r"\b(movie|game|concert|bar|beer|wine|fun|entertainment)\b"),
]

def detect_category(text: str) -> str:
    t = text.lower()
    for cat, pattern in CATEGORY_RULES:
        if re.search(pattern, t):
            return cat
    return "other"

# -------------------------------------------------------------------
# DB
# -------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def add_expense(user_id: int, amount: float, category: str, note: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO expenses (user_id, amount, category, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, amount, category, note, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def get_week_total(user_id: int) -> float:
    since = (datetime.utcnow() - timedelta(days=7)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE user_id = ? AND created_at >= ?",
        (user_id, since),
    )
    total = cur.fetchone()[0]
    conn.close()
    return total

def get_month_total(user_id: int) -> float:
    since = (datetime.utcnow() - timedelta(days=30)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE user_id = ? AND created_at >= ?",
        (user_id, since),
    )
    total = cur.fetchone()[0]
    conn.close()
    return total

def get_recent(user_id: int, limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT amount, category, note, created_at FROM expenses "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def get_category_totals(user_id: int, days: int = 30):
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT category, SUM(amount) FROM expenses "
        "WHERE user_id = ? AND created_at >= ? GROUP BY category",
        (user_id, since),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

# -------------------------------------------------------------------
# CHART
# -------------------------------------------------------------------
def make_chart(user_id: int) -> io.BytesIO | None:
    rows = get_category_totals(user_id, days=30)
    if not rows:
        return None
    labels = [r[0] for r in rows]
    values = [r[1] for r in rows]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.pie(values, labels=labels, autopct="%1.0f%%", startangle=90)
    ax.set_title("Spending by category (last 30 days)")
    buf = io.BytesIO()
    fig.savefig(buf, format="PNG", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    buf.name = "chart.png"
    return buf

# -------------------------------------------------------------------
# HEALTH SERVER
# -------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Finance bot alive")

    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

# -------------------------------------------------------------------
# HANDLERS
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💰 *Personal Finance Tracker*\n\n"
        "*Log an expense:*\n"
        "`/spent 12 coffee`\n"
        "`/spent 40 gas`\n"
        "`/spent 25.50 movie tickets`\n\n"
        "*Reports:*\n"
        "`/report` — this week's total\n"
        "`/month` — last 30 days total\n"
        "`/chart` — pie chart by category\n"
        "`/recent` — last 10 expenses\n\n"
        "Categories are auto-detected (food, transport, shopping, bills, health, fun, other)."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def spent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    raw = " ".join(context.args) if context.args else ""

    # Also support replying-to-message style: /spent 12 coffee
    if not raw and msg.reply_to_message:
        raw = msg.reply_to_message.text or ""

    m = re.match(r"^\s*([0-9]+(?:\.[0-9]{1,2})?)\s+(.+)$", raw)
    if not m:
        await msg.reply_text(
            "Usage: `/spent 12 coffee`\n"
            "First number is the amount, then a short note.",
            parse_mode="Markdown",
        )
        return

    amount = float(m.group(1))
    note = m.group(2).strip()
    if amount <= 0 or amount > 1_000_000:
        await msg.reply_text("Amount must be between 0 and 1,000,000.")
        return

    category = detect_category(note)
    add_expense(msg.from_user.id, amount, category, note)

    week = get_week_total(msg.from_user.id)
    await msg.reply_text(
        f"✅ Logged *${amount:.2f}* — {category}\n"
        f"📝 {note}\n"
        f"📅 This week: *${week:.2f}*",
        parse_mode="Markdown",
    )


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = get_week_total(update.message.from_user.id)
    await update.message.reply_text(f"📅 *This week:* ${total:.2f}", parse_mode="Markdown")


async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = get_month_total(update.message.from_user.id)
    await update.message.reply_text(f"🗓️ *Last 30 days:* ${total:.2f}", parse_mode="Markdown")


async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_recent(update.message.from_user.id, limit=10)
    if not rows:
        await update.message.reply_text("No expenses logged yet. Try `/spent 12 coffee`.", parse_mode="Markdown")
        return
    lines = ["🧾 *Last 10 expenses:*\n"]
    for amount, cat, note, ts in rows:
        date = ts[:10]
        lines.append(f"• {date} — ${amount:.2f} ({cat}) — {note}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buf = await asyncio.to_thread(make_chart, update.message.from_user.id)
    if not buf:
        await update.message.reply_text("No expenses in the last 30 days to chart.")
        return
    await update.message.reply_photo(photo=buf, caption="📊 Spending by category (last 30 days)")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "I only understand /start, /spent, /report, /month, /chart, /recent."
    )

# -------------------------------------------------------------------
# MAIN (Python 3.14 event-loop fix)
# -------------------------------------------------------------------
def main():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    init_db()
    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("spent", spent))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("chart", chart))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    print("💰 Finance bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()