import os
import sqlite3
import threading
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, request, redirect, url_for, session, render_template_string, flash
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "")
DB_PATH = os.getenv("DB_PATH", "/var/data/memeraja.db")
MINING_REWARD = int(os.getenv("MINING_REWARD", "50"))
REFERRAL_REWARD = int(os.getenv("REFERRAL_REWARD", "100"))
MIN_WITHDRAWAL = int(os.getenv("MIN_WITHDRAWAL", "500"))
MINING_COOLDOWN = timedelta(hours=24)

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in Render Environment Variables.")
if ADMIN_ID <= 0:
    raise RuntimeError("Set ADMIN_ID to your numeric Telegram user ID.")
if not ADMIN_PASSWORD or len(ADMIN_PASSWORD) < 12:
    raise RuntimeError("Set a strong ADMIN_PASSWORD (at least 12 characters).")
if not FLASK_SECRET_KEY or len(FLASK_SECRET_KEY) < 32:
    raise RuntimeError("Set FLASK_SECRET_KEY to a random string of at least 32 characters.")

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            points INTEGER NOT NULL DEFAULT 0,
            last_mining TEXT,
            referred_by INTEGER,
            referral_paid INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            link TEXT DEFAULT '',
            reward INTEGER NOT NULL CHECK(reward > 0),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            submitted_at TEXT NOT NULL,
            reviewed_at TEXT,
            UNIQUE(task_id, user_id),
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL CHECK(amount > 0),
            network TEXT NOT NULL,
            wallet_address TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            admin_note TEXT DEFAULT '',
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            kind TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('min_withdrawal',?)", (str(MIN_WITHDRAWAL),))

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def get_user(user_id):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def ensure_user(user_id, username, referred_by=None):
    with db() as con:
        existing = con.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            con.execute("UPDATE users SET username=? WHERE user_id=?", (username or "", user_id))
        else:
            valid_ref = None
            if referred_by and referred_by != user_id:
                ref = con.execute("SELECT user_id FROM users WHERE user_id=?", (referred_by,)).fetchone()
                if ref:
                    valid_ref = referred_by
            con.execute("INSERT INTO users(user_id,username,referred_by) VALUES(?,?,?)",
                        (user_id, username or "", valid_ref))

def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛏️ Mine", callback_data="mine"),
         InlineKeyboardButton("💰 Wallet", callback_data="wallet")],
        [InlineKeyboardButton("🎯 Tasks", callback_data="tasks"),
         InlineKeyboardButton("👥 Refer", callback_data="refer")],
        [InlineKeyboardButton("💸 Withdraw PPC", callback_data="withdraw"),
         InlineKeyboardButton("📜 History", callback_data="history")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referred_by = None
    if context.args:
        try:
            ref_id = int(context.args[0])
            if ref_id != user.id and get_user(ref_id):
                referred_by = ref_id
        except ValueError:
            pass
    ensure_user(user.id, user.username or "", referred_by)
    await update.message.reply_text(
        "👑 Welcome to MemeRaja Mining!\\n\\n"
        f"⛏️ Daily Mining: +{MINING_REWARD} PPC\\n"
        f"👥 Referral reward: +{REFERRAL_REWARD} PPC (after eligible activity)\\n"
        f"💸 Minimum withdrawal: {MIN_WITHDRAWAL} PPC\\n\\n"
        "PPC are in-app points. Withdrawal requests require admin review; approval does not itself send crypto.",
        reply_markup=menu()
    )

async def do_mine(user_id):
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            return "Please use /start first."
        if user["last_mining"]:
            last = datetime.fromisoformat(user["last_mining"])
            now = datetime.now(timezone.utc)
            if now - last < MINING_COOLDOWN:
                remaining = MINING_COOLDOWN - (now - last)
                sec = max(0, int(remaining.total_seconds()))
                return f"⏳ Mining already claimed. Come back in {sec//3600}h {(sec%3600)//60}m."
        now = now_iso()
        con.execute("UPDATE users SET points=points+?, last_mining=? WHERE user_id=?",
                    (MINING_REWARD, now, user_id))
        con.execute("INSERT INTO transactions(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)",
                    (user_id, MINING_REWARD, "credit", "Daily mining", now))
    return f"⛏️ Mining successful!\\n+{MINING_REWARD} PPC added."

async def show_tasks(user_id):
    with db() as con:
        tasks = con.execute("""
            SELECT t.*,
            (SELECT status FROM task_submissions s WHERE s.task_id=t.id AND s.user_id=?) AS submission_status
            FROM tasks t WHERE t.active=1 ORDER BY t.id DESC
        """, (user_id,)).fetchall()
    if not tasks:
        return "🎯 No active tasks at the moment."
    buttons = []
    lines = ["🎯 Available Tasks\\n"]
    for t in tasks:
        status = t["submission_status"]
        lines.append(f"#{t['id']} — {t['title']} (+{t['reward']} PPC)")
        if t["description"]:
            lines.append(t["description"])
        if status == "approved":
            lines.append("Status: ✅ Approved")
        elif status == "pending":
            lines.append("Status: 🕒 Waiting for admin review")
        else:
            lines.append("Status: Not submitted")
            buttons.append([InlineKeyboardButton(f"Submit #{t['id']}", callback_data=f"task_submit:{t['id']}")])
        if t["link"]:
            buttons.append([InlineKeyboardButton(f"Open task #{t['id']}", url=t["link"])])
        lines.append("")
    buttons.append([InlineKeyboardButton("⬅️ Main menu", callback_data="menu")])
    return "\\n".join(lines), InlineKeyboardMarkup(buttons)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    ensure_user(uid, q.from_user.username or "")
    action = q.data

    if action == "mine":
        message = await do_mine(uid)
        await q.edit_message_text(message, reply_markup=menu())
    elif action in ("wallet", "balance"):
        user = get_user(uid)
        await q.edit_message_text(f"💰 PPC Wallet\\n\\nBalance: {user['points']} PPC\\nMinimum withdrawal: {MIN_WITHDRAWAL} PPC",
                                  reply_markup=menu())
    elif action == "tasks":
        result = await show_tasks(uid)
        if isinstance(result, tuple):
            await q.edit_message_text(result[0], reply_markup=result[1], disable_web_page_preview=True)
        else:
            await q.edit_message_text(result, reply_markup=menu())
    elif action.startswith("task_submit:"):
        task_id = int(action.split(":", 1)[1])
        with db() as con:
            task = con.execute("SELECT id FROM tasks WHERE id=? AND active=1", (task_id,)).fetchone()
            if not task:
                msg = "This task is no longer available."
            else:
                try:
                    con.execute("INSERT INTO task_submissions(task_id,user_id,status,submitted_at) VALUES(?,?,?,?)",
                                (task_id, uid, "pending", now_iso()))
                    msg = "✅ Task submitted for admin review. PPC will be credited only after approval."
                except sqlite3.IntegrityError:
                    row = con.execute("SELECT status FROM task_submissions WHERE task_id=? AND user_id=?",
                                      (task_id, uid)).fetchone()
                    msg = f"Your task status: {row['status']}."
        await q.edit_message_text(msg, reply_markup=menu())
    elif action == "refer":
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start={uid}"
        await q.edit_message_text(f"👥 Refer & Earn\\n\\nYour referral link:\\n{link}\\nReward: +{REFERRAL_REWARD} PPC after eligible activity.",
                                  reply_markup=menu())
    elif action == "withdraw":
        context.user_data["awaiting_withdraw_amount"] = True
        await q.edit_message_text(
            f"💸 Withdrawal request\\nMinimum: {MIN_WITHDRAWAL} PPC\\n\\nSend the amount as a number (example: 500).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel_withdraw")]])
        )
    elif action == "cancel_withdraw":
        context.user_data.pop("awaiting_withdraw_amount", None)
        context.user_data.pop("withdraw_amount", None)
        context.user_data.pop("awaiting_withdraw_address", None)
        await q.edit_message_text("Withdrawal cancelled.", reply_markup=menu())
    elif action == "history":
        with db() as con:
            rows = con.execute("SELECT amount,kind,note,created_at FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
            withdrawals = con.execute("SELECT amount,network,status,created_at FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
        lines = ["📜 Recent transactions"]
        if rows:
            for r in rows:
                sign = "+" if r["kind"] == "credit" else "-"
                lines.append(f"{sign}{r['amount']} PPC — {r['note'] or r['kind']}")
        else:
            lines.append("No transactions yet.")
        if withdrawals:
            lines.append("\\n💸 Withdrawal requests")
            for w in withdrawals:
                lines.append(f"{w['amount']} PPC · {w['network']} · {w['status']}")
        await q.edit_message_text("\\n".join(lines), reply_markup=menu())
    elif action == "leaderboard":
        with db() as con:
            rows = con.execute("SELECT username,points FROM users ORDER BY points DESC LIMIT 10").fetchall()
        lines = ["🏆 Top Miners\\n"]
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. @{r['username']}" if r["username"] else f"{i}. User")
            lines[-1] += f" — {r['points']} PPC"
        await q.edit_message_text("\\n".join(lines), reply_markup=menu())
    elif action == "menu":
        await q.edit_message_text("MemeRaja Mining menu:", reply_markup=menu())
    else:
        await q.edit_message_text("Unknown action.", reply_markup=menu())

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    if context.user_data.get("awaiting_withdraw_amount"):
        try:
            amount = int(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send a whole number of PPC.")
            return
        with db() as con:
            user = con.execute("SELECT points FROM users WHERE user_id=?", (uid,)).fetchone()
            min_row = con.execute("SELECT value FROM settings WHERE key='min_withdrawal'").fetchone()
            minimum = int(min_row["value"]) if min_row else MIN_WITHDRAWAL
            if not user or amount < minimum or amount > user["points"]:
                await update.message.reply_text(f"Invalid amount. Minimum is {minimum} PPC and your balance must cover it.")
                return
        context.user_data["withdraw_amount"] = amount
        context.user_data.pop("awaiting_withdraw_amount", None)
        context.user_data["awaiting_withdraw_address"] = True
        await update.message.reply_text("Now send your crypto wallet address. Do not send your seed phrase or private key.")
        return
    if context.user_data.get("awaiting_withdraw_address"):
        amount = int(context.user_data.get("withdraw_amount", 0))
        if len(text) < 15 or len(text) > 200 or " " in text:
            await update.message.reply_text("That address format looks invalid. Send the public wallet address only, or /cancel.")
            return
        # Reserve PPC at request creation so it cannot be withdrawn twice.
        with db() as con:
            user = con.execute("SELECT points FROM users WHERE user_id=?", (uid,)).fetchone()
            if not user or user["points"] < amount:
                context.user_data.clear()
                await update.message.reply_text("Insufficient balance. Please start again.", reply_markup=menu())
                return
            con.execute("UPDATE users SET points=points-? WHERE user_id=?", (amount, uid))
            cur = con.execute("INSERT INTO withdrawals(user_id,amount,network,wallet_address,status,created_at) VALUES(?,?,?,?,?,?)",
                              (uid, amount, "Crypto network to confirm", text, "pending", now_iso()))
            wid = cur.lastrowid
            con.execute("INSERT INTO transactions(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)",
                        (uid, amount, "debit", f"Withdrawal request #{wid} (reserved)", now_iso()))
        context.user_data.pop("awaiting_withdraw_address", None)
        context.user_data.pop("withdraw_amount", None)
        await update.message.reply_text(f"✅ Withdrawal request #{wid} submitted for review. PPC has been reserved while pending.", reply_markup=menu())
        return
    await update.message.reply_text("Use the menu buttons or /start.", reply_markup=menu())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.", reply_markup=menu())

# Minimal admin panel. Access requires ADMIN_PASSWORD; Telegram ADMIN_ID is used for bot-side admin notices.
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

LOGIN_HTML = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MemeRaja Admin Login</title><style>body{font-family:Arial;background:#101827;color:#fff;max-width:420px;margin:50px auto;padding:20px}input,button{width:100%;padding:12px;margin:8px 0;border-radius:8px;border:0}button{background:#36c98f;font-weight:bold}</style>
<h2>👑 MemeRaja Admin</h2><form method="post"><label>Admin password</label><input type="password" name="password" required><button>Login</button></form><p>{{ message }}</p>"""

ADMIN_HTML = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MemeRaja Admin Panel</title><style>
body{font-family:Arial;background:#101827;color:#e9eef8;margin:0;padding:16px}h1,h2{color:#6ee7b7}
.card{background:#1b2638;padding:16px;border-radius:12px;margin:12px 0;overflow:auto}
input,textarea,button,select{padding:10px;margin:5px 0;border-radius:7px;border:1px solid #45546a;max-width:100%;box-sizing:border-box}
input,textarea{background:#0e1726;color:#fff;width:100%}button{background:#6ee7b7;color:#07131b;font-weight:bold;cursor:pointer}
table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #344256;text-align:left;vertical-align:top;word-break:break-word}
a{color:#6ee7b7}.small{font-size:12px;color:#b8c4d6}
</style>
<h1>👑 MemeRaja Admin Panel</h1><p><a href="/admin/logout">Logout</a></p>
<div class="card"><h2>Overview</h2><p>Users: {{ users_count }} · Active tasks: {{ task_count }} · Pending submissions: {{ pending_submissions }} · Pending withdrawals: {{ pending_withdrawals }}</p>
<form method="post" action="/admin/minimum"><label>Minimum withdrawal (PPC)</label><input type="number" min="1" name="minimum" value="{{ minimum }}" required><button>Save minimum</button></form></div>
<div class="card"><h2>Add Task</h2><form method="post" action="/admin/tasks/add">
<input name="title" placeholder="Task title" required maxlength="120">
<textarea name="description" placeholder="Instructions" maxlength="1000"></textarea>
<input name="link" placeholder="https://... (optional)">
<input type="number" name="reward" min="1" placeholder="PPC reward" required>
<button>Add task</button></form></div>
<div class="card"><h2>Tasks</h2><table><tr><th>ID / Task</th><th>Reward</th><th>Status</th><th>Action</th></tr>
{% for t in tasks %}<tr><td>#{{t.id}} — {{t.title}}<br><span class="small">{{t.description}}</span><br>{% if t.link %}<a href="{{t.link}}" target="_blank">Open link</a>{% endif %}</td><td>{{t.reward}} PPC</td><td>{{'Active' if t.active else 'Paused'}}</td><td><form method="post" action="/admin/tasks/toggle"><input type="hidden" name="task_id" value="{{t.id}}"><button>{{'Pause' if t.active else 'Activate'}}</button></form></td></tr>{% endfor %}</table></div>
<div class="card"><h2>Task submissions (review before crediting)</h2><table><tr><th>Submission</th><th>Task</th><th>User</th><th>Action</th></tr>
{% for s in submissions %}<tr><td>#{{s.id}}<br>{{s.status}}</td><td>{{s.title}} (+{{s.reward}} PPC)</td><td>{{s.user_id}} / @{{s.username}}</td><td>{% if s.status=='pending' %}<form method="post" action="/admin/submissions/review"><input type="hidden" name="submission_id" value="{{s.id}}"><button name="decision" value="approve">Approve + credit</button><button name="decision" value="reject">Reject</button></form>{% else %}Reviewed{% endif %}</td></tr>{% endfor %}</table></div>
<div class="card"><h2>Crypto withdrawal requests</h2><p class="small">Approval here records admin approval only. Send the crypto separately, then mark it paid. If rejected, reserved PPC is refunded.</p>
<table><tr><th>Request</th><th>User</th><th>Amount</th><th>Network / Address</th><th>Status / Action</th></tr>
{% for w in withdrawals %}<tr><td>#{{w.id}}<br>{{w.created_at}}</td><td>{{w.user_id}} / @{{w.username}}</td><td>{{w.amount}} PPC</td><td>{{w.network}}<br>{{w.wallet_address}}</td><td>{{w.status}}<br>{% if w.status=='pending' %}<form method="post" action="/admin/withdrawals/review"><input type="hidden" name="withdrawal_id" value="{{w.id}}"><button name="decision" value="approve">Approve</button><button name="decision" value="reject">Reject + refund</button></form>{% elif w.status=='approved' %}<form method="post" action="/admin/withdrawals/paid"><input type="hidden" name="withdrawal_id" value="{{w.id}}"><input name="txid" placeholder="Transaction hash (optional)"><button>Mark paid</button></form>{% endif %}</td></tr>{% endfor %}</table></div>
<div class="card"><h2>Users</h2><table><tr><th>User ID</th><th>Username</th><th>Balance</th><th>Manual PPC credit</th></tr>
{% for u in users %}<tr><td>{{u.user_id}}</td><td>@{{u.username}}</td><td>{{u.points}} PPC</td><td><form method="post" action="/admin/users/credit"><input type="hidden" name="user_id" value="{{u.user_id}}"><input type="number" min="1" name="amount" placeholder="Amount" required><input name="note" placeholder="Reason" required><button>Credit</button></form></td></tr>{% endfor %}</table></div>
<p class="small">Keep this panel private. Never share the admin password. Do not store crypto seed phrases or private keys here.</p>"""

def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("admin_ok"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapped

@app.route("/")
def home():
    return "MemeRaja Mining service is running."

@app.route("/admin", methods=["GET", "POST"])
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_ok"):
        return redirect(url_for("admin_dashboard"))
    message = ""
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session.clear()
            session["admin_ok"] = True
            session["csrf"] = secrets.token_urlsafe(24)
            return redirect(url_for("admin_dashboard"))
        message = "Incorrect password."
    return render_template_string(LOGIN_HTML, message=message)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    with db() as con:
        users = con.execute("SELECT user_id,username,points FROM users ORDER BY points DESC LIMIT 300").fetchall()
        tasks = con.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
        submissions = con.execute("""SELECT s.*,t.title,t.reward,u.username FROM task_submissions s
            JOIN tasks t ON t.id=s.task_id JOIN users u ON u.user_id=s.user_id
            ORDER BY CASE s.status WHEN 'pending' THEN 0 ELSE 1 END, s.id DESC LIMIT 200""").fetchall()
        withdrawals = con.execute("""SELECT w.*,u.username FROM withdrawals w JOIN users u ON u.user_id=w.user_id
            ORDER BY CASE w.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, w.id DESC LIMIT 200""").fetchall()
        users_count = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        task_count = con.execute("SELECT COUNT(*) n FROM tasks WHERE active=1").fetchone()["n"]
        pending_submissions = con.execute("SELECT COUNT(*) n FROM task_submissions WHERE status='pending'").fetchone()["n"]
        pending_withdrawals = con.execute("SELECT COUNT(*) n FROM withdrawals WHERE status='pending'").fetchone()["n"]
        minimum = con.execute("SELECT value FROM settings WHERE key='min_withdrawal'").fetchone()["value"]
    return render_template_string(ADMIN_HTML, users=users, tasks=tasks, submissions=submissions,
        withdrawals=withdrawals, users_count=users_count, task_count=task_count,
        pending_submissions=pending_submissions, pending_withdrawals=pending_withdrawals, minimum=minimum)

@app.route("/admin/minimum", methods=["POST"])
@admin_required
def admin_minimum():
    try:
        minimum = int(request.form.get("minimum", "0"))
        if minimum < 1: raise ValueError
        with db() as con:
            con.execute("INSERT INTO settings(key,value) VALUES('min_withdrawal',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(minimum),))
        flash("Minimum updated.")
    except ValueError:
        pass
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/tasks/add", methods=["POST"])
@admin_required
def admin_add_task():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    link = request.form.get("link", "").strip()
    try:
        reward = int(request.form.get("reward", "0"))
    except ValueError:
        reward = 0
    if title and reward > 0 and (not link or link.startswith("https://") or link.startswith("http://")):
        with db() as con:
            con.execute("INSERT INTO tasks(title,description,link,reward,active,created_at) VALUES(?,?,?,?,1,?)",
                        (title, description, link, reward, now_iso()))
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/tasks/toggle", methods=["POST"])
@admin_required
def admin_toggle_task():
    task_id = int(request.form.get("task_id", "0"))
    with db() as con:
        con.execute("UPDATE tasks SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (task_id,))
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/submissions/review", methods=["POST"])
@admin_required
def admin_review_submission():
    sid = int(request.form.get("submission_id", "0"))
    decision = request.form.get("decision")
    with db() as con:
        s = con.execute("""SELECT s.*,t.reward FROM task_submissions s JOIN tasks t ON t.id=s.task_id WHERE s.id=?""", (sid,)).fetchone()
        if s and s["status"] == "pending" and decision in ("approve", "reject"):
            status = "approved" if decision == "approve" else "rejected"
            con.execute("UPDATE task_submissions SET status=?,reviewed_at=? WHERE id=?", (status, now_iso(), sid))
            if decision == "approve":
                con.execute("UPDATE users SET points=points+? WHERE user_id=?", (s["reward"], s["user_id"]))
                con.execute("INSERT INTO transactions(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)",
                            (s["user_id"], s["reward"], "credit", f"Task #{s['task_id']} approved", now_iso()))
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/withdrawals/review", methods=["POST"])
@admin_required
def admin_review_withdrawal():
    wid = int(request.form.get("withdrawal_id", "0"))
    decision = request.form.get("decision")
    with db() as con:
        w = con.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if w and w["status"] == "pending" and decision in ("approve", "reject"):
            if decision == "approve":
                con.execute("UPDATE withdrawals SET status='approved',reviewed_at=? WHERE id=?", (now_iso(), wid))
            else:
                con.execute("UPDATE withdrawals SET status='rejected',reviewed_at=?,admin_note='Rejected; PPC refunded' WHERE id=?", (now_iso(), wid))
                con.execute("UPDATE users SET points=points+? WHERE user_id=?", (w["amount"], w["user_id"]))
                con.execute("INSERT INTO transactions(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)",
                            (w["user_id"], w["amount"], "credit", f"Refund for rejected withdrawal #{wid}", now_iso()))
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/withdrawals/paid", methods=["POST"])
@admin_required
def admin_mark_paid():
    wid = int(request.form.get("withdrawal_id", "0"))
    txid = request.form.get("txid", "").strip()[:200]
    with db() as con:
        w = con.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if w and w["status"] == "approved":
            con.execute("UPDATE withdrawals SET status='paid',reviewed_at=?,admin_note=? WHERE id=?",
                        (now_iso(), f"Paid. TXID: {txid}", wid))
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/users/credit", methods=["POST"])
@admin_required
def admin_credit_user():
    try:
        uid = int(request.form.get("user_id", "0"))
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        return redirect(url_for("admin_dashboard"))
    note = request.form.get("note", "").strip()[:200]
    if uid > 0 and amount > 0 and note:
        with db() as con:
            user = con.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
            if user:
                con.execute("UPDATE users SET points=points+? WHERE user_id=?", (amount, uid))
                con.execute("INSERT INTO transactions(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)",
                            (uid, amount, "credit", f"Admin credit: {note}", now_iso()))
    return redirect(url_for("admin_dashboard"))

def run_bot():
    async def post_init(application):
        # Best-effort: announce startup only in logs, never reveal secrets.
        print("Telegram bot application initialized.")
    bot_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("cancel", cancel))
    bot_app.add_handler(CallbackQueryHandler(button))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    bot_app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "10000"))
    # Flask runs in a background thread; Telegram polling stays on the main thread.
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True
    ).start()
    print(f"Admin panel listening on port {port}")
    run_bot()
