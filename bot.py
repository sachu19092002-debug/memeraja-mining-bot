import os
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.getenv("PORT", "10000"))
APP_URL = os.environ["APP_URL"].rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram-webhook")
DB = "memeraja.db"

MINING_REWARD = 50
REFERRAL_REWARD = 100
TASK_REWARD = 20
MINING_COOLDOWN = timedelta(hours=24)


def db():
    return sqlite3.connect(DB)


def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER DEFAULT 0,
            last_mining TEXT,
            referred_by INTEGER,
            referral_paid INTEGER DEFAULT 0
        )
    """)
    con.commit()
    con.close()


def get_user(user_id):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    return row


def create_user(user_id, username, referred_by=None):
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users "
        "(user_id, username, referred_by) VALUES (?, ?, ?)",
        (user_id, username, referred_by),
    )
    con.commit()
    con.close()


def add_points(user_id, amount):
    con = db()
    cur = con.cursor()
    cur.execute(
        "UPDATE users SET points = points + ? WHERE user_id=?",
        (amount, user_id),
    )
    con.commit()
    con.close()


def menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⛏️ Mine", callback_data="mine"),
            InlineKeyboardButton("💰 Balance", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("👥 Refer", callback_data="refer"),
            InlineKeyboardButton("🎯 Tasks", callback_data="tasks"),
        ],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    existing = get_user(user.id)
    referred_by = None

    if not existing and context.args:
        try:
            ref_id = int(context.args[0])
            if ref_id != user.id and get_user(ref_id):
                referred_by = ref_id
        except ValueError:
            pass

    create_user(user.id, user.username or "", referred_by)

    await update.message.reply_text(
        "👑 Welcome to MemeRaja Mining!\n\n"
        f"⛏️ Daily Mining: +{MINING_REWARD}\n"
        f"👥 Referral: +{REFERRAL_REWARD}\n"
        f"🎯 Task: +{TASK_REWARD}\n\n"
        "These are community points, not cryptocurrency or guaranteed monetary value.",
        reply_markup=menu(),
    )


async def mine(user_id):
    user = get_user(user_id)
    if not user:
        return "Please use /start first."

    last_mining = user[3]
    if last_mining:
        last = datetime.fromisoformat(last_mining)
        now = datetime.now(timezone.utc)
        if now - last < MINING_COOLDOWN:
            remaining = MINING_COOLDOWN - (now - last)
            hours = remaining.seconds // 3600
            minutes = (remaining.seconds % 3600) // 60
            return f"⏳ Mining already claimed.\nCome back in {hours}h {minutes}m."

    con = db()
    cur = con.cursor()
    cur.execute(
        "UPDATE users SET points=points+?, last_mining=? WHERE user_id=?",
        (MINING_REWARD, datetime.now(timezone.utc).isoformat(), user_id),
    )
    con.commit()
    con.close()
    return f"⛏️ Mining successful!\n\n+{MINING_REWARD} points added."


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    action = query.data

    if action == "mine":
        message = await mine(user_id)

    elif action == "balance":
        user = get_user(user_id)
        message = f"💰 Your Balance\n\n⭐ {user[2] if user else 0} points"

    elif action == "refer":
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start={user_id}"
        message = (
            "👥 Refer & Earn\n\n"
            f"{link}\n\n"
            f"Reward: +{REFERRAL_REWARD} points\n"
            "Referral rewards are credited only after the required activity."
        )

    elif action == "tasks":
        message = (
            "🎯 Tasks\n\n"
            f"Approved task reward: +{TASK_REWARD} points.\n"
            "Tasks will be enabled by the admin."
        )

    elif action == "leaderboard":
        con = db()
        cur = con.cursor()
        cur.execute(
            "SELECT username, points FROM users ORDER BY points DESC LIMIT 10"
        )
        rows = cur.fetchall()
        con.close()

        message = "🏆 Top Miners\n\n"
        if not rows:
            message = "🏆 No users yet."
        else:
            for i, (username, points) in enumerate(rows, 1):
                name = f"@{username}" if username else "User"
                message += f"{i}. {name} — ⭐ {points}\n"
    else:
        message = "Unknown action."

    await query.edit_message_text(message, reply_markup=menu())


async def post_init(application: Application):
    webhook_url = f"{APP_URL}/{WEBHOOK_PATH}"
    await application.bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True,
    )
    print(f"Webhook set: {webhook_url}")


def main():
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{APP_URL}/{WEBHOOK_PATH}",
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
    
