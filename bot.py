import logging
import os
import json
import re
import calendar as cal_module
from datetime import datetime, timedelta

import pytz

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    JobQueue,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ================= CONFIG =================
TOKEN = "8464632180:AAGh_semPGrVtKBcMFVDy5EvIAl9bzTwcVs"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1bHeyDgw9P-3iRLOp_6VpHGKSn9St6yjyqP-35hPg6Rs/edit?pli=1&gid=0#gid=0"

IST = pytz.timezone("Asia/Kolkata")

DEFAULT_RETRY_INTERVAL = 600  # 10 minutes
DEFAULT_MAX_RETRIES = 3

# Sheet columns (1-indexed):
# 1: id (empty)
# 2: user_id
# 3: title (legacy, now empty)
# 4: message
# 5: date
# 6: time
# 7: repeat
# 8: status
# 9: retries

# =============== LOGGING =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============= GOOGLE SHEET ==============
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

creds_json = os.environ.get("GOOGLE_CREDS")
if not creds_json:
    raise Exception("GOOGLE_CREDS missing")

creds = json.loads(creds_json)
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds, scope)
client = gspread.authorize(credentials)
sheet = client.open_by_url(SHEET_URL).sheet1


# ============= UI ========================

def home_text():
    return (
        "<b>Smart Reminder Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Manage your reminders easily.\n\n"
        "Use <b>＋ New</b> or /add to create.\n"
        "Use /list to view all.\n\n"
        "<i>Or just type naturally:</i>\n"
        "<code>Study tomorrow at 9pm</code>"
    )


def home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("＋ New", callback_data="add")],
    ])


def cancel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✕ Cancel", callback_data="cancel_add")],
    ])


def repeat_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Once", callback_data="rep_none"),
            InlineKeyboardButton("Daily", callback_data="rep_daily"),
        ],
        [
            InlineKeyboardButton("Weekly", callback_data="rep_weekly"),
            InlineKeyboardButton("Monthly", callback_data="rep_monthly"),
        ],
        [InlineKeyboardButton("✕ Cancel", callback_data="cancel_add")],
    ])


def nl_repeat_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Once", callback_data="nlrep_none"),
            InlineKeyboardButton("Daily", callback_data="nlrep_daily"),
        ],
        [
            InlineKeyboardButton("Weekly", callback_data="nlrep_weekly"),
            InlineKeyboardButton("Monthly", callback_data="nlrep_monthly"),
        ],
        [InlineKeyboardButton("✕ Cancel", callback_data="cancel_add")],
    ])


def reminder_action_kb(row):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1h", callback_data=f"snooze_{row}"),
            InlineKeyboardButton("Done", callback_data=f"done_{row}"),
        ],
    ])


# ============= CALENDAR PICKER ============

def build_calendar_kb(year, month, for_edit=None):
    now = datetime.now(IST)
    kb = []

    month_name = cal_module.month_name[month]
    kb.append([InlineKeyboardButton(f"{month_name} {year}", callback_data="noop")])

    kb.append([
        InlineKeyboardButton(d, callback_data="noop")
        for d in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    ])

    weeks = cal_module.monthcalendar(year, month)
    prefix = f"eday_{for_edit}_" if for_edit else "day_"

    for week in weeks:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                day_date = datetime(year, month, day)
                date_str = f"{year}-{month:02d}-{day:02d}"
                if day_date.date() < now.date():
                    row.append(InlineKeyboardButton("·", callback_data="noop"))
                else:
                    label = f"[{day}]" if day_date.date() == now.date() else str(day)
                    row.append(InlineKeyboardButton(label, callback_data=f"{prefix}{date_str}"))
        kb.append(row)

    # Quick buttons
    today_str = now.strftime("%Y-%m-%d")
    tmrw_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    quick_prefix = f"eday_{for_edit}_" if for_edit else "day_"
    kb.append([
        InlineKeyboardButton("Today", callback_data=f"{quick_prefix}{today_str}"),
        InlineKeyboardButton("Tomorrow", callback_data=f"{quick_prefix}{tmrw_str}"),
    ])

    # Navigation
    prev_m = month - 1
    prev_y = year
    if prev_m < 1:
        prev_m = 12
        prev_y -= 1
    next_m = month + 1
    next_y = year
    if next_m > 12:
        next_m = 1
        next_y += 1

    nav_prefix = f"ecal_{for_edit}_" if for_edit else "cal_"
    nav_row = []
    if datetime(prev_y, prev_m, 1) >= datetime(now.year, now.month, 1):
        nav_row.append(InlineKeyboardButton("‹", callback_data=f"{nav_prefix}{prev_y}_{prev_m:02d}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))
    nav_row.append(InlineKeyboardButton("·", callback_data="noop"))
    nav_row.append(InlineKeyboardButton("›", callback_data=f"{nav_prefix}{next_y}_{next_m:02d}"))
    kb.append(nav_row)

    if for_edit:
        kb.append([InlineKeyboardButton("« Back", callback_data=f"edit_{for_edit}")])
    else:
        kb.append([InlineKeyboardButton("✕ Cancel", callback_data="cancel_add")])

    return InlineKeyboardMarkup(kb)


# ============= TIME PICKER ===============

def build_time_hour_kb(for_edit=None):
    kb = []
    kb.append([InlineKeyboardButton("Select Hour", callback_data="noop")])

    prefix = f"ehr_{for_edit}_" if for_edit else "hr_"

    # AM
    kb.append([InlineKeyboardButton("— AM —", callback_data="noop")])
    am_row1 = []
    am_row2 = []
    for h in range(12):
        label = "12" if h == 0 else str(h)
        btn = InlineKeyboardButton(label, callback_data=f"{prefix}{h:02d}")
        if h < 6:
            am_row1.append(btn)
        else:
            am_row2.append(btn)
    kb.append(am_row1)
    kb.append(am_row2)

    # PM
    kb.append([InlineKeyboardButton("— PM —", callback_data="noop")])
    pm_row1 = []
    pm_row2 = []
    for h in range(12, 24):
        label = "12" if h == 12 else str(h - 12)
        btn = InlineKeyboardButton(label, callback_data=f"{prefix}{h:02d}")
        if h < 18:
            pm_row1.append(btn)
        else:
            pm_row2.append(btn)
    kb.append(pm_row1)
    kb.append(pm_row2)

    if for_edit:
        kb.append([InlineKeyboardButton("« Back", callback_data=f"edit_{for_edit}")])
    else:
        kb.append([InlineKeyboardButton("✕ Cancel", callback_data="cancel_add")])

    return InlineKeyboardMarkup(kb)


def build_time_minute_kb(hour, for_edit=None):
    kb = []
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    ampm = "AM" if hour < 12 else "PM"
    kb.append([InlineKeyboardButton(f"{h12} {ampm} — Minutes", callback_data="noop")])

    prefix = f"emn_{for_edit}_" if for_edit else "mn_"
    row = []
    for m in [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]:
        time_str = f"{hour:02d}:{m:02d}"
        row.append(InlineKeyboardButton(f":{m:02d}", callback_data=f"{prefix}{time_str}"))
        if len(row) == 4:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    back_cb = f"edittime_{for_edit}" if for_edit else "time_back"
    kb.append([InlineKeyboardButton("« Hour", callback_data=back_cb)])

    return InlineKeyboardMarkup(kb)


# ============= NATURAL LANGUAGE ===========

def parse_natural_language(text):
    """
    Parse: "Study tomorrow at 9pm" → ("Study", "2025-07-01", "21:00")
    Returns (message, date_str, time_str) or None.
    """
    now = datetime.now(IST)
    s = text.strip()

    # Strip "remind me to" / "reminder to" prefix
    s_clean = re.sub(r'^remind(?:er)?\s+(?:me\s+)?(?:to\s+)?', '', s, flags=re.IGNORECASE)
    if s_clean == s:
        # Also try without prefix change
        s_clean = s
    working = s_clean

    date_str = None
    time_str = None
    date_span = None
    time_span = None

    low = working.lower()

    # --- TIME ---
    # HH:MM am/pm
    m = re.search(r'(?:at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)', low)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if m.group(3) == 'pm' and h != 12:
            h += 12
        elif m.group(3) == 'am' and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            time_str = f"{h:02d}:{mi:02d}"
            time_span = m.span()

    # HH:MM 24h
    if not time_str:
        m = re.search(r'(?:at\s+)?(\d{1,2}):(\d{2})\b', low)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                time_str = f"{h:02d}:{mi:02d}"
                time_span = m.span()

    # Hpm / Ham
    if not time_str:
        m = re.search(r'(?:at\s+)?(\d{1,2})\s*(am|pm)', low)
        if m:
            h = int(m.group(1))
            if m.group(2) == 'pm' and h != 12:
                h += 12
            elif m.group(2) == 'am' and h == 12:
                h = 0
            if 0 <= h <= 23:
                time_str = f"{h:02d}:00"
                time_span = m.span()

    # --- DATE ---
    # day after tomorrow
    m = re.search(r'day\s+after\s+tomorrow', low)
    if m:
        date_str = (now + timedelta(days=2)).strftime("%Y-%m-%d")
        date_span = m.span()

    if not date_str:
        m = re.search(r'\btomorrow\b', low)
        if m:
            date_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            date_span = m.span()

    if not date_str:
        m = re.search(r'\btoday\b', low)
        if m:
            date_str = now.strftime("%Y-%m-%d")
            date_span = m.span()

    # Weekday
    if not date_str:
        day_map = {
            'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
            'friday': 4, 'saturday': 5, 'sunday': 6,
            'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6,
        }
        pattern = '|'.join(day_map.keys())
        m = re.search(r'(?:next\s+|on\s+)?(' + pattern + r')\b', low)
        if m:
            target = day_map[m.group(1)]
            diff = (target - now.weekday()) % 7
            if diff == 0:
                diff = 7
            date_str = (now + timedelta(days=diff)).strftime("%Y-%m-%d")
            date_span = m.span()

    # YYYY-MM-DD
    if not date_str:
        m = re.search(r'(\d{4}-\d{2}-\d{2})', low)
        if m:
            date_str = m.group(1)
            date_span = m.span()

    if not date_str or not time_str:
        return None

    # --- MESSAGE: remove date & time parts ---
    spans = sorted([s for s in [date_span, time_span] if s], key=lambda x: x[0], reverse=True)
    msg = low
    for start, end in spans:
        msg = msg[:start] + msg[end:]

    msg = re.sub(r'\b(at|on|by)\b', '', msg)
    msg = re.sub(r'\s+', ' ', msg).strip().strip('., ')

    if not msg:
        return None

    msg = msg[0].upper() + msg[1:]

    return (msg, date_str, time_str)


# ============= HELPERS ====================

def get_reminder_msg(r):
    msg = r[3] if len(r) > 3 else ""
    if not msg or not str(msg).strip():
        msg = r[2] if len(r) > 2 else ""
    return str(msg).strip()


def normalize_date(val):
    s = str(val).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def normalize_time(val):
    s = str(val).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            h = int(parts[0])
            m = int(parts[1].split()[0])
            upper = s.upper()
            if "PM" in upper and h != 12:
                h += 12
            elif "AM" in upper and h == 12:
                h = 0
            return f"{h:02d}:{m:02d}"
        except (ValueError, IndexError):
            return s
    try:
        f = float(s)
        total_minutes = round(f * 24 * 60)
        h = total_minutes // 60
        m = total_minutes % 60
        return f"{h:02d}:{m:02d}"
    except ValueError:
        return s


def advance_repeat(row, r):
    repeat = r[6] if len(r) > 6 else "none"
    if not repeat or repeat == "none":
        return False
    d = datetime.strptime(normalize_date(r[4]), "%Y-%m-%d")
    if repeat == "daily":
        nd = d + timedelta(days=1)
    elif repeat == "weekly":
        nd = d + timedelta(days=7)
    elif repeat == "monthly":
        m = d.month + 1
        y = d.year
        if m > 12:
            m = 1
            y += 1
        nd = d.replace(year=y, month=m)
    else:
        return False
    sheet.update_cell(row, 5, nd.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 8, "active")
    sheet.update_cell(row, 9, 0)
    return True


def cancel_retry_jobs(job_queue, row):
    jobs = job_queue.get_jobs_by_name(f"retry-{row}")
    for job in jobs:
        job.schedule_removal()
    logger.info(f"Cancelled {len(jobs)} retry job(s) for row {row}")


def format_date_short(date_str):
    try:
        d = datetime.strptime(normalize_date(date_str), "%Y-%m-%d")
        return d.strftime("%-d %b")
    except Exception:
        return str(date_str)


def format_time_12h(time_str):
    try:
        t = normalize_time(time_str)
        h, m = map(int, t.split(":"))
        ampm = "AM" if h < 12 else "PM"
        h12 = h % 12
        if h12 == 0:
            h12 = 12
        return f"{h12}:{m:02d} {ampm}"
    except Exception:
        return str(time_str)


def format_repeat(repeat):
    mapping = {"none": "Once", "daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
    return mapping.get(str(repeat), str(repeat))


def status_icon(status):
    mapping = {"active": "○", "pending": "●", "missed": "✗"}
    return mapping.get(str(status), "?")


def status_label(status):
    mapping = {"active": "Active", "pending": "Pending", "missed": "Missed"}
    return mapping.get(str(status), str(status))


def reminder_detail(r):
    """Return formatted detail line for a reminder row."""
    msg = get_reminder_msg(r)
    date_str = normalize_date(r[4]) if len(r) > 4 else ""
    time_str = normalize_time(r[5]) if len(r) > 5 else ""
    repeat_str = format_repeat(r[6]) if len(r) > 6 else ""
    return msg, date_str, time_str, repeat_str


# ============= SAFE EDIT ==================
async def safe_edit(message, text, reply_markup=None, parse_mode="HTML"):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


# ============= POST INIT =================
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("add", "New reminder"),
        BotCommand("list", "All reminders"),
        BotCommand("info", "About this bot"),
    ])
    logger.info("Bot commands registered")


# ============= START =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(home_text(), reply_markup=home_kb(), parse_mode="HTML")


# ============= /add =======================
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["step"] = "message"
    await update.message.reply_text(
        "<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\nEnter message:",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


# ============= /list ======================
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    uid = update.effective_user.id
    await show_list(update.message, uid, is_new_message=True)


# ============= /info ======================
async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Smart Reminder Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Set reminders and get notified on time.\n\n"
        "<b>Features</b>\n"
        "• One-time & recurring reminders\n"
        "• Date & time pickers\n"
        "• Snooze for 1 hour\n"
        "• Auto-retry 3× every 10 min if missed\n"
        "• Edit or cancel anytime\n"
        "• Natural language input\n\n"
        "<b>Commands</b>\n"
        "/add — New reminder (step by step)\n"
        "/list — All reminders\n"
        "/info — This page\n\n"
        "<b>Quick Add</b>\n"
        "Just type naturally:\n"
        "<code>Study tomorrow at 9pm</code>\n"
        "<code>Call mom today at 5:30pm</code>\n"
        "<code>Meeting on Monday at 10am</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ============= SHOW LIST =================
async def show_list(target, uid, is_new_message=False):
    rows = sheet.get_all_records()
    items = []
    for i, r in enumerate(rows, start=2):
        if str(r.get("user_id", "")) != str(uid):
            continue
        status = str(r.get("status", "")).strip()
        if status in ("active", "pending", "missed"):
            items.append((i, r))

    if not items:
        text = "<b>Reminders</b>\n━━━━━━━━━━━━━━━━━━━━\nNo reminders found."
        kb = home_kb()
        if is_new_message:
            await target.reply_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await safe_edit(target, text, kb)
        return

    lines = ["<b>Reminders</b>\n━━━━━━━━━━━━━━━━━━━━"]
    buttons = []

    for idx, (row_idx, r) in enumerate(items):
        status = str(r.get("status", ""))
        icon = status_icon(status)
        date_str = normalize_date(r.get("date", ""))
        time_str = normalize_time(r.get("time", ""))
        repeat_str = format_repeat(r.get("repeat", "none"))
        msg = str(r.get("message", ""))
        if not msg.strip():
            msg = str(r.get("title", ""))
        msg_short = msg[:40] + "…" if len(msg) > 40 else msg
        btn_label = msg[:12] if len(msg) > 12 else msg

        lines.append(
            f"\n{icon} {msg_short}\n"
            f"   {format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str} · <i>{status_label(status)}</i>"
        )

        if status == "missed":
            buttons.append([
                InlineKeyboardButton(f"✕ {btn_label}", callback_data=f"cancelrem_{row_idx}"),
            ])
        else:
            buttons.append([
                InlineKeyboardButton(f"✎ {btn_label}", callback_data=f"edit_{row_idx}"),
                InlineKeyboardButton(f"✕ {btn_label}", callback_data=f"cancelrem_{row_idx}"),
            ])

    buttons.append([InlineKeyboardButton("« Back", callback_data="home")])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(buttons)

    if is_new_message:
        await target.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await safe_edit(target, text, kb)


# ============= BUTTON HANDLER ============
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "noop":
        return

    # ---- HOME ----
    if data == "home":
        context.user_data.clear()
        await safe_edit(query.message, home_text(), home_kb())

    # ---- ADD ----
    elif data == "add":
        context.user_data.clear()
        context.user_data["step"] = "message"
        await safe_edit(
            query.message,
            "<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\nEnter message:",
            cancel_kb(),
        )

    # ---- CANCEL ADD ----
    elif data == "cancel_add":
        context.user_data.clear()
        await safe_edit(query.message, home_text(), home_kb())

    # ---- CALENDAR NAV (new) ----
    elif data.startswith("cal_"):
        parts = data.replace("cal_", "").split("_")
        year, month = int(parts[0]), int(parts[1])
        msg = context.user_data.get("message", "")
        await safe_edit(
            query.message,
            f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n{msg}\n\nPick a date:",
            build_calendar_kb(year, month),
        )

    # ---- DAY SELECTED (new) ----
    elif data.startswith("day_"):
        date_str = data.replace("day_", "")
        context.user_data["date"] = date_str
        context.user_data["step"] = "time"
        msg = context.user_data.get("message", "")
        await safe_edit(
            query.message,
            (
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n{format_date_short(date_str)}\n\nPick time:"
            ),
            build_time_hour_kb(),
        )

    # ---- HOUR SELECTED (new) ----
    elif data.startswith("hr_"):
        hour = int(data.replace("hr_", ""))
        context.user_data["selected_hour"] = hour
        msg = context.user_data.get("message", "")
        date = context.user_data.get("date", "")
        await safe_edit(
            query.message,
            (
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n{format_date_short(date)}\n\nPick minutes:"
            ),
            build_time_minute_kb(hour),
        )

    # ---- BACK TO HOURS (new) ----
    elif data == "time_back":
        msg = context.user_data.get("message", "")
        date = context.user_data.get("date", "")
        await safe_edit(
            query.message,
            (
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n{format_date_short(date)}\n\nPick time:"
            ),
            build_time_hour_kb(),
        )

    # ---- MINUTE SELECTED (new) → repeat ----
    elif data.startswith("mn_"):
        time_str = data.replace("mn_", "")
        context.user_data["time"] = time_str
        context.user_data["step"] = "repeat"
        msg = context.user_data.get("message", "")
        date = context.user_data.get("date", "")
        await safe_edit(
            query.message,
            (
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n{format_date_short(date)} · {format_time_12h(time_str)}\n\nRepeat?"
            ),
            repeat_kb(),
        )

    # ---- SAVE (repeat choice) ----
    elif data.startswith("rep_"):
        repeat = data.replace("rep_", "")
        message = context.user_data.get("message", "")
        date = context.user_data.get("date", "")
        time = context.user_data.get("time", "")
        row = ["", query.from_user.id, "", message, date, time, repeat, "active", 0]
        sheet.append_row(row, value_input_option="RAW")
        context.user_data.clear()
        await safe_edit(
            query.message,
            (
                f"<b>Saved ✓</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{message}\n"
                f"{format_date_short(date)} · {format_time_12h(time)} · {format_repeat(repeat)}"
            ),
            home_kb(),
        )

    # ---- NL SAVE (natural language repeat) ----
    elif data.startswith("nlrep_"):
        repeat = data.replace("nlrep_", "")
        message = context.user_data.get("nl_message", "")
        date = context.user_data.get("nl_date", "")
        time = context.user_data.get("nl_time", "")
        row = ["", query.from_user.id, "", message, date, time, repeat, "active", 0]
        sheet.append_row(row, value_input_option="RAW")
        context.user_data.clear()
        await safe_edit(
            query.message,
            (
                f"<b>Saved ✓</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{message}\n"
                f"{format_date_short(date)} · {format_time_12h(time)} · {format_repeat(repeat)}"
            ),
            home_kb(),
        )

    # ---- SNOOZE ----
    elif data.startswith("snooze_"):
        row = int(data.replace("snooze_", ""))
        cancel_retry_jobs(context.job_queue, row)
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)

        now = datetime.now(IST)
        new_time = now + timedelta(hours=1)
        sheet.update_cell(row, 5, new_time.strftime("%Y-%m-%d"))
        sheet.update_cell(row, 6, new_time.strftime("%H:%M"))
        sheet.update_cell(row, 8, "active")
        sheet.update_cell(row, 9, 0)

        await safe_edit(
            query.message,
            (
                f"{msg}\n"
                f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}\n\n"
                f"<b>Snoozed</b> → {format_time_12h(new_time.strftime('%H:%M'))}"
            ),
            home_kb(),
        )

    # ---- DONE ----
    elif data.startswith("done_"):
        row = int(data.replace("done_", ""))
        cancel_retry_jobs(context.job_queue, row)
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)
        is_repeat = advance_repeat(row, r)
        if not is_repeat:
            sheet.update_cell(row, 8, "done")
            sheet.update_cell(row, 9, 0)

        await safe_edit(
            query.message,
            (
                f"{msg}\n"
                f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}\n\n"
                f"<b>Done</b> ✓"
            ),
            home_kb(),
        )

    # ---- EDIT (from list) ----
    elif data.startswith("edit_") and not any(
        data.startswith(p) for p in ("editmsg_", "editdate_", "edittime_")
    ):
        row = int(data.replace("edit_", ""))
        context.user_data.clear()
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)

        await safe_edit(
            query.message,
            (
                f"<b>Edit Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n"
                f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}\n\n"
                f"What to change?"
            ),
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Message", callback_data=f"editmsg_{row}"),
                    InlineKeyboardButton("Date", callback_data=f"editdate_{row}"),
                    InlineKeyboardButton("Time", callback_data=f"edittime_{row}"),
                ],
                [InlineKeyboardButton("« Back", callback_data="list_refresh")],
            ]),
        )

    # ---- EDIT MESSAGE ----
    elif data.startswith("editmsg_"):
        row = int(data.replace("editmsg_", ""))
        context.user_data["editing_row"] = row
        context.user_data["step"] = "edit_message"
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)

        await safe_edit(
            query.message,
            (
                f"<b>Edit Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"Current: <i>{msg}</i>\n"
                f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}\n\n"
                f"Enter new message:"
            ),
            InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data=f"edit_{row}")],
            ]),
        )

    # ---- EDIT DATE (show calendar) ----
    elif data.startswith("editdate_"):
        row = int(data.replace("editdate_", ""))
        context.user_data["editing_row"] = row
        context.user_data["step"] = "edit_date"
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)
        now = datetime.now(IST)

        await safe_edit(
            query.message,
            (
                f"<b>Edit Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n"
                f"Current: <i>{format_date_short(date_str)} · {format_time_12h(time_str)}</i>\n\n"
                f"Pick new date:"
            ),
            build_calendar_kb(now.year, now.month, for_edit=row),
        )

    # ---- EDIT CALENDAR NAV ----
    elif data.startswith("ecal_"):
        parts = data.replace("ecal_", "").split("_")
        row = int(parts[0])
        year, month = int(parts[1]), int(parts[2])
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)

        await safe_edit(
            query.message,
            (
                f"<b>Edit Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n"
                f"Current: <i>{format_date_short(date_str)} · {format_time_12h(time_str)}</i>\n\n"
                f"Pick new date:"
            ),
            build_calendar_kb(year, month, for_edit=row),
        )

    # ---- EDIT DAY SELECTED ----
    elif data.startswith("eday_"):
        parts = data.replace("eday_", "").split("_", 1)
        row = int(parts[0])
        new_date = parts[1]
        r = sheet.row_values(row)
        msg, old_date, time_str, repeat_str = reminder_detail(r)

        sheet.update_cell(row, 5, new_date)
        context.user_data.clear()

        await safe_edit(
            query.message,
            (
                f"<b>Updated ✓</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n"
                f"Date: {format_date_short(old_date)} → <b>{format_date_short(new_date)}</b>\n"
                f"Time: {format_time_12h(time_str)} · {repeat_str}"
            ),
            home_kb(),
        )

    # ---- EDIT TIME (show hour picker) ----
    elif data.startswith("edittime_"):
        row = int(data.replace("edittime_", ""))
        context.user_data["editing_row"] = row
        context.user_data["step"] = "edit_time"
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)

        await safe_edit(
            query.message,
            (
                f"<b>Edit Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n"
                f"Current: <i>{format_date_short(date_str)} · {format_time_12h(time_str)}</i>\n\n"
                f"Pick new time:"
            ),
            build_time_hour_kb(for_edit=row),
        )

    # ---- EDIT HOUR SELECTED ----
    elif data.startswith("ehr_"):
        parts = data.replace("ehr_", "").split("_")
        row = int(parts[0])
        hour = int(parts[1])
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)

        await safe_edit(
            query.message,
            (
                f"<b>Edit Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n"
                f"Current: <i>{format_date_short(date_str)} · {format_time_12h(time_str)}</i>\n\n"
                f"Pick minutes:"
            ),
            build_time_minute_kb(hour, for_edit=row),
        )

    # ---- EDIT MINUTE SELECTED ----
    elif data.startswith("emn_"):
        parts = data.replace("emn_", "").split("_", 1)
        row = int(parts[0])
        new_time = parts[1]
        r = sheet.row_values(row)
        msg, date_str, old_time, repeat_str = reminder_detail(r)

        sheet.update_cell(row, 6, new_time)
        context.user_data.clear()

        await safe_edit(
            query.message,
            (
                f"<b>Updated ✓</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n"
                f"Date: {format_date_short(date_str)}\n"
                f"Time: {format_time_12h(old_time)} → <b>{format_time_12h(new_time)}</b> · {repeat_str}"
            ),
            home_kb(),
        )

    # ---- CANCEL REMINDER ----
    elif data.startswith("cancelrem_"):
        row = int(data.replace("cancelrem_", ""))
        cancel_retry_jobs(context.job_queue, row)
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)
        sheet.update_cell(row, 8, "cancelled")
        sheet.update_cell(row, 9, 0)

        await safe_edit(
            query.message,
            (
                f"{msg}\n"
                f"{format_date_short(date_str)} · {format_time_12h(time_str)}\n\n"
                f"<b>Cancelled</b> ✕"
            ),
            home_kb(),
        )

    # ---- LIST REFRESH ----
    elif data == "list_refresh":
        context.user_data.clear()
        uid = query.from_user.id
        await show_list(query.message, uid)


# ============= TEXT HANDLER ==============
async def save_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")
    text = update.message.text.strip()

    # ---- ACTIVE FLOW ----
    if step:
        if step == "message":
            context.user_data["message"] = text
            context.user_data["step"] = "date"
            now = datetime.now(IST)
            await update.message.reply_text(
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n{text}\n\nPick a date:",
                reply_markup=build_calendar_kb(now.year, now.month),
                parse_mode="HTML",
            )

        elif step == "edit_message":
            row = context.user_data.get("editing_row")
            if row:
                r = sheet.row_values(row)
                old_msg, date_str, time_str, repeat_str = reminder_detail(r)
                sheet.update_cell(row, 4, text)
                context.user_data.clear()
                await update.message.reply_text(
                    (
                        f"<b>Updated ✓</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                        f"Message: {old_msg} → <b>{text}</b>\n"
                        f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}"
                    ),
                    reply_markup=home_kb(),
                    parse_mode="HTML",
                )
        return

    # ---- NO ACTIVE FLOW → TRY NATURAL LANGUAGE ----
    parsed = parse_natural_language(text)
    if parsed:
        message, date_str, time_str = parsed
        context.user_data["nl_message"] = message
        context.user_data["nl_date"] = date_str
        context.user_data["nl_time"] = time_str

        await update.message.reply_text(
            (
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{message}\n"
                f"{format_date_short(date_str)} · {format_time_12h(time_str)}\n\n"
                f"Repeat?"
            ),
            reply_markup=nl_repeat_kb(),
            parse_mode="HTML",
        )


# ============= AUTO RETRY ================
async def auto_retry(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    row = data["row"]
    chat = data["chat"]

    try:
        r = sheet.row_values(row)
    except Exception as e:
        logger.error(f"auto_retry: failed to read row {row}: {e}")
        return

    if not r:
        return

    if r[7] != "pending":
        logger.info(f"auto_retry: row {row} status '{r[7]}', skipping.")
        return

    try:
        count = int(r[8])
    except (IndexError, ValueError):
        count = 0

    if count >= DEFAULT_MAX_RETRIES:
        if not advance_repeat(row, r):
            sheet.update_cell(row, 8, "missed")
            sheet.update_cell(row, 9, 0)
        return

    msg = get_reminder_msg(r)
    text = f"{msg}\n\n<b>Reminder</b> ({count + 1}/{DEFAULT_MAX_RETRIES})"

    await context.bot.send_message(
        chat_id=chat, text=text,
        reply_markup=reminder_action_kb(row), parse_mode="HTML",
    )

    new_count = count + 1
    sheet.update_cell(row, 9, new_count)

    if new_count >= DEFAULT_MAX_RETRIES:
        if not advance_repeat(row, r):
            sheet.update_cell(row, 8, "missed")
            sheet.update_cell(row, 9, 0)
    else:
        context.job_queue.run_once(
            auto_retry, DEFAULT_RETRY_INTERVAL,
            data={"row": row, "chat": chat}, name=f"retry-{row}",
        )


# ============= SCHEDULER =================
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(IST)
    now_str = now.strftime("%Y-%m-%d %H:%M")

    try:
        rows = sheet.get_all_records()
    except Exception as e:
        logger.error(f"check_reminders: {e}")
        return

    for i, r in enumerate(rows, start=2):
        status = str(r.get("status", "")).strip()
        if status != "active":
            continue

        date_val = normalize_date(r.get("date", ""))
        time_val = normalize_time(r.get("time", ""))
        rem_str = f"{date_val} {time_val}"

        if rem_str != now_str:
            continue

        uid = r.get("user_id")
        msg = str(r.get("message", ""))
        if not msg.strip():
            msg = str(r.get("title", ""))
        logger.info(f"Firing row {i}: '{msg[:30]}' for {uid}")

        cancel_retry_jobs(context.job_queue, i)

        text = f"{msg}\n\n<b>⏰ Reminder</b>"

        try:
            await context.bot.send_message(
                chat_id=uid, text=text,
                reply_markup=reminder_action_kb(i), parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"check_reminders: send to {uid} failed: {e}")
            continue

        sheet.update_cell(i, 8, "pending")
        sheet.update_cell(i, 9, 0)

        context.job_queue.run_once(
            auto_retry, DEFAULT_RETRY_INTERVAL,
            data={"row": i, "chat": uid}, name=f"retry-{i}",
        )


# ============= MAIN ======================
def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .job_queue(JobQueue())
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_text))

    app.job_queue.run_repeating(check_reminders, interval=60, first=0)

    print("Smart Reminder Bot Running")
    app.run_polling()


if __name__ == "__main__":
    main()
