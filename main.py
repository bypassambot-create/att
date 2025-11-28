"""
attendance_bot_interactive_with_counts.py
Attendance bot for group chats using telebot (pyTelegramBotAPI) + sqlite.

Enhancement: interactive /attendance output with pagination, filtering, sorting,
and counts (Active / Inactive / Total) shown in the header.
"""

import time
import sqlite3
import threading
from datetime import datetime, timedelta
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable not set")
DB_PATH = "attendance.db"
INACTIVE_THRESHOLD = timedelta(hours=24)   # no activity for this => become inactive
INACTIVE_PERIOD = timedelta(days=1)        # how long inactive lasts when marked
MINUTES_REDUCED_PER_MESSAGE = 1
MESSAGES_TO_CLEAR_INACTIVE = 15
SCAN_INTERVAL_SECONDS = 10 * 60
PAGE_SIZE = 10
# ----------------------------

bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')

# ---------- DATABASE (same as before) ----------
def init_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        last_active TIMESTAMP,
        inactive_until TIMESTAMP,
        messages_since_inactive INTEGER DEFAULT 0,
        inactive_marked_at TIMESTAMP
    )
    ''')
    conn.commit()
    conn.close()

def db_conn():
    return sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES)

def upsert_user(user_id, username=None, first_name=None, last_name=None):
    now = datetime.utcnow()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cur.fetchone():
        cur.execute("""
            UPDATE users SET username = COALESCE(?, username),
                             first_name = COALESCE(?, first_name),
                             last_name = COALESCE(?, last_name)
            WHERE user_id = ?
        """, (username, first_name, last_name, user_id))
    else:
        cur.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, last_active)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, username, first_name, last_name, now))
    conn.commit()
    conn.close()

def mark_active(user_id):
    now = datetime.utcnow()
    conn = db_conn()
    cur = conn.cursor()
    # reset messages_since_inactive if not currently inactive
    cur.execute("""
        UPDATE users
        SET last_active = ?,
            messages_since_inactive = CASE WHEN inactive_until IS NOT NULL AND inactive_until > ? THEN messages_since_inactive ELSE 0 END
        WHERE user_id = ?
    """, (now, now, user_id))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, first_name, last_name, last_active, inactive_until, messages_since_inactive, inactive_marked_at FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def set_inactive(user_id):
    now = datetime.utcnow()
    until = now + INACTIVE_PERIOD
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users SET inactive_until = ?, inactive_marked_at = ?, messages_since_inactive = 0 WHERE user_id = ?
    """, (until, now, user_id))
    conn.commit()
    conn.close()

def clear_inactive(user_id):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users SET inactive_until = NULL, messages_since_inactive = 0, inactive_marked_at = NULL WHERE user_id = ?
    """, (user_id,))
    conn.commit()
    conn.close()

def reduce_inactive_by_minutes(user_id, minutes=1):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT inactive_until, messages_since_inactive FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return
    inactive_until, messages_since_inactive = row
    if inactive_until is None:
        conn.close()
        return
    # normalize to datetime
    inactive_dt = datetime.fromisoformat(inactive_until) if isinstance(inactive_until, str) else inactive_until
    new_until = inactive_dt - timedelta(minutes=minutes)
    messages_since_inactive = (messages_since_inactive or 0) + 1
    if messages_since_inactive >= MESSAGES_TO_CLEAR_INACTIVE or new_until <= datetime.utcnow():
        cur.execute("UPDATE users SET inactive_until = NULL, messages_since_inactive = 0, inactive_marked_at = NULL WHERE user_id = ?", (user_id,))
    else:
        cur.execute("UPDATE users SET inactive_until = ?, messages_since_inactive = ? WHERE user_id = ?", (new_until, messages_since_inactive, user_id))
    conn.commit()
    conn.close()

def all_tracked_users():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, first_name, last_name, last_active, inactive_until FROM users")
    rows = cur.fetchall()
    conn.close()
    return rows

# ---------- SCANNER ----------
def scan_and_mark_inactive():
    now = datetime.utcnow()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, last_active, inactive_until FROM users")
    rows = cur.fetchall()
    for user_id, last_active, inactive_until in rows:
        if last_active is None:
            continue
        last_active_dt = datetime.fromisoformat(last_active) if isinstance(last_active, str) else last_active
        if inactive_until:
            continue
        if now - last_active_dt >= INACTIVE_THRESHOLD:
            until = now + INACTIVE_PERIOD
            cur.execute("UPDATE users SET inactive_until = ?, inactive_marked_at = ?, messages_since_inactive = 0 WHERE user_id = ?", (until, now, user_id))
    conn.commit()
    conn.close()
    threading.Timer(SCAN_INTERVAL_SECONDS, scan_and_mark_inactive).start()

# ---------- UTILS for attendance display ----------
def format_user_line(row):
    """
    row: (user_id, username, first_name, last_name, last_active, inactive_until)
    """
    user_id, username, first_name, last_name, last_active, inactive_until = row
    if username:
        display = f"@{username}"
    else:
        display = (first_name or "") + ((" " + last_name) if last_name else "")
        display = display.strip() or f"user_{user_id}"
    status = "Active"
    if inactive_until:
        until_dt = datetime.fromisoformat(inactive_until) if isinstance(inactive_until, str) else inactive_until
        if until_dt > datetime.utcnow():
            delta = until_dt - datetime.utcnow()
            days = delta.days
            hours, remainder = divmod(delta.seconds, 3600)
            minutes = remainder // 60
            status = f"Inactive {days}d {hours}h {minutes}m"
        else:
            status = "Active"
    return f"{display} | {status}"

def compute_counts(rows):
    """
    Given rows from all_tracked_users(), compute (active_count, inactive_count, total).
    A user is considered inactive if inactive_until > now.
    """
    now = datetime.utcnow()
    active = 0
    inactive = 0
    total = 0
    for r in rows:
        total += 1
        inactive_until = r[5]
        if inactive_until:
            until_dt = datetime.fromisoformat(inactive_until) if isinstance(inactive_until, str) else inactive_until
            if until_dt > now:
                inactive += 1
            else:
                active += 1
        else:
            active += 1
    return active, inactive, total

def filter_and_sort_users(rows, filter_mode='all', sort_mode='name'):
    """
    filter_mode: 'all', 'active', 'inactive'
    sort_mode: 'name', 'last'
    """
    processed = []
    for r in rows:
        user_id, username, first_name, last_name, last_active, inactive_until = r
        is_inactive = False
        if inactive_until:
            until_dt = datetime.fromisoformat(inactive_until) if isinstance(inactive_until, str) else inactive_until
            is_inactive = until_dt > datetime.utcnow()
        if filter_mode == 'active' and is_inactive:
            continue
        if filter_mode == 'inactive' and not is_inactive:
            continue
        # compute sort keys
        name_key = (username or (first_name or "")).lower() if (username or first_name) else str(user_id)
        last_key = datetime.fromisoformat(last_active) if last_active and isinstance(last_active, str) else last_active or datetime.fromtimestamp(0)
        processed.append((r, name_key, last_key))
    if sort_mode == 'name':
        processed.sort(key=lambda t: t[1])
    else:
        # newest last_active first
        processed.sort(key=lambda t: t[2], reverse=True)
    return [t[0] for t in processed]

def build_attendance_text(paged_rows, active_count, inactive_count, total_count, page, page_size, filter_mode, sort_mode):
    total_pages = (total_count + page_size - 1)//page_size if total_count else 1
    header = (
        f"<b>Attendance</b> — <i>{filter_mode}</i> — Sorted by <i>{'name' if sort_mode=='name' else 'last active'}</i>\n"
        f"Active: <b>{active_count}</b>  |  Inactive: <b>{inactive_count}</b>  |  Total: <b>{total_count}</b>\n"
        f"Page {page+1} / {total_pages}\n\n"
    )
    lines = [format_user_line(r) for r in paged_rows]
    if not lines:
        body = "(no users to show on this page)"
    else:
        body = "\n".join(lines)
    return header + body

def build_inline_keyboard(page, total_pages, filter_mode, sort_mode):
    kb = InlineKeyboardMarkup()
    # navigation row
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⟨ Prev", callback_data=f"ATT|{page-1}|{filter_mode}|{sort_mode}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ⟩", callback_data=f"ATT|{page+1}|{filter_mode}|{sort_mode}"))
    if nav_buttons:
        kb.row(*nav_buttons)
    # filter row
    filter_buttons = [
        InlineKeyboardButton("All", callback_data=f"ATT|{page}|all|{sort_mode}"),
        InlineKeyboardButton("Active", callback_data=f"ATT|{page}|active|{sort_mode}"),
        InlineKeyboardButton("Inactive", callback_data=f"ATT|{page}|inactive|{sort_mode}"),
    ]
    kb.row(*filter_buttons)
    # sort row
    sort_buttons = [
        InlineKeyboardButton("Sort: Name", callback_data=f"ATT|{page}|{filter_mode}|name"),
        InlineKeyboardButton("Sort: Last active", callback_data=f"ATT|{page}|{filter_mode}|last"),
    ]
    kb.row(*sort_buttons)
    return kb

# ---------- TELEBOT HANDLERS ----------
@bot.message_handler(commands=['start'])
def handle_start(message):
    bot.reply_to(message, "Attendance bot is running. Add me to a group and I will track activity. Use /attendance in group to see statuses.")

@bot.message_handler(commands=['attendance'])
def handle_attendance(message):
    # only allow in groups
    if message.chat.type not in ['group', 'supergroup']:
        bot.reply_to(message, "Please use /attendance inside the group chat.")
        return

    # small synchronous scan to refresh statuses
    scan_and_mark_inactive_once()

    rows = all_tracked_users()
    active_count, inactive_count, total_count = compute_counts(rows)

    filter_mode = 'all'
    sort_mode = 'name'
    filtered = filter_and_sort_users(rows, filter_mode=filter_mode, sort_mode=sort_mode)
    total_filtered = len(filtered)
    total_pages = (total_filtered + PAGE_SIZE - 1) // PAGE_SIZE if total_filtered else 1
    page = 0
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    paged = filtered[start:end]

    text = build_attendance_text(paged, active_count, inactive_count, total_count, page, PAGE_SIZE, filter_mode, sort_mode)
    kb = build_inline_keyboard(page, total_pages, filter_mode, sort_mode)
    bot.reply_to(message, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("ATT|"))
def handle_attendance_callback(cq):
    # parse data: ATT|<page>|<filter>|<sort>
    try:
        parts = cq.data.split("|")
        _, page_s, filter_mode, sort_mode = parts
        page = int(page_s)
    except Exception:
        bot.answer_callback_query(cq.id, text="Invalid data.")
        return

    # refresh data
    scan_and_mark_inactive_once()
    rows = all_tracked_users()
    active_count, inactive_count, total_count = compute_counts(rows)

    filtered = filter_and_sort_users(rows, filter_mode=filter_mode, sort_mode=sort_mode)
    total_filtered = len(filtered)
    total_pages = (total_filtered + PAGE_SIZE - 1) // PAGE_SIZE if total_filtered else 1
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    paged = filtered[start:end]
    text = build_attendance_text(paged, active_count, inactive_count, total_count, page, PAGE_SIZE, filter_mode, sort_mode)
    kb = build_inline_keyboard(page, total_pages, filter_mode, sort_mode)

    try:
        bot.edit_message_text(text, chat_id=cq.message.chat.id, message_id=cq.message.message_id, reply_markup=kb)
    except telebot.apihelper.ApiException:
        # fallback: send new message
        bot.send_message(cq.message.chat.id, text, reply_markup=kb)
    bot.answer_callback_query(cq.id)

@bot.message_handler(func=lambda m: True, content_types=['text', 'audio', 'document', 'photo', 'video', 'sticker', 'voice'])
def handle_all_messages(message):
    # Only handle messages in group/supergroup chats
    if message.chat.type not in ['group', 'supergroup']:
        return

    if message.from_user is None:
        return

    u = message.from_user
    user_id = u.id
    username = u.username
    first_name = u.first_name
    last_name = getattr(u, 'last_name', None)

    # ensure user exists in DB
    upsert_user(user_id, username=username, first_name=first_name, last_name=last_name)

    # mark active (updates last_active)
    mark_active(user_id)

    # If user currently inactive, reduce remaining time by 1 minute and potentially clear
    user = get_user(user_id)
    if user and user[5]:  # inactive_until is index 5
        reduce_inactive_by_minutes(user_id, MINUTES_REDUCED_PER_MESSAGE)

@bot.message_handler(content_types=['new_chat_members'])
def handle_new_members(message):
    for member in message.new_chat_members:
        upsert_user(member.id, username=member.username, first_name=member.first_name, last_name=getattr(member, 'last_name', None))
        mark_active(member.id)

@bot.message_handler(content_types=['left_chat_member'])
def handle_left_member(message):
    left = message.left_chat_member
    if left:
        upsert_user(left.id, username=left.username, first_name=left.first_name, last_name=getattr(left, 'last_name', None))
        # keep history

# ---------- small synchronous scan for freshness ----------
def scan_and_mark_inactive_once():
    now = datetime.utcnow()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, last_active, inactive_until FROM users")
    rows = cur.fetchall()
    for user_id, last_active, inactive_until in rows:
        if last_active is None:
            continue
        last_active_dt = datetime.fromisoformat(last_active) if isinstance(last_active, str) else last_active
        if inactive_until:
            continue
        if now - last_active_dt >= INACTIVE_THRESHOLD:
            until = now + INACTIVE_PERIOD
            cur.execute("UPDATE users SET inactive_until = ?, inactive_marked_at = ?, messages_since_inactive = 0 WHERE user_id = ?", (until, now, user_id))
    conn.commit()
    conn.close()

# ---------- STARTUP ----------
if __name__ == '__main__':
    print("Initializing database...")
    init_db()
    print("Starting background scanner...")
    threading.Timer(SCAN_INTERVAL_SECONDS, scan_and_mark_inactive).start()
    print("Bot polling started...")
    bot.infinity_polling()
