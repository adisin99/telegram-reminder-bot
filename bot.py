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
TOKEN = "8608586255:AAGneh_XhBMD9hY39eamC15iCK6mGxzSOR0"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1bHeyDgw9P-3iRLOp_6VpHGKSn9St6yjyqP-35hPg6Rs/edit?pli=1&gid=0#gid=0"

IST = pytz.timezone("Asia/Kolkata")

DEFAULT_RETRY_INTERVAL = 600  # 10 minutes
DEFAULT_MAX_RETRIES = 3

# Sheet columns (1-indexed):
# 1: user_id
# 2: message
# 3: date
# 4: time
# 5: repeat
# 6: status
# 7: retry_count

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
        "Or just type naturally:\n"
        "<i>Buy milk tomorrow at 5pm</i>\n\n"
        "Use /list to view all."
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
        [InlineKeyboardButton("✕ Cancel", callback_data="nlcancel")],
    ])


def reminder_action_kb(row):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Snooze", callback_data=f"snzpick_{row}"),
            InlineKeyboardButton("Done", callback_data=f"done_{row}"),
        ],
    ])


def snooze_picker_kb(row):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("15m", callback_data=f"snzdo_{row}_15"),
            InlineKeyboardButton("30m", callback_data=f"snzdo_{row}_30"),
            InlineKeyboardButton("45m", callback_data=f"snzdo_{row}_45"),
        ],
        [
            InlineKeyboardButton("1h", callback_data=f"snzdo_{row}_60"),
            InlineKeyboardButton("2h", callback_data=f"snzdo_{row}_120"),
            InlineKeyboardButton("3h", callback_data=f"snzdo_{row}_180"),
        ],
        [
            InlineKeyboardButton("5h", callback_data=f"snzdo_{row}_300"),
            InlineKeyboardButton("8h", callback_data=f"snzdo_{row}_480"),
            InlineKeyboardButton("12h", callback_data=f"snzdo_{row}_720"),
        ],
        [InlineKeyboardButton("« Back", callback_data=f"snzbk_{row}")],
    ])


def format_snooze_label(mins):
    if mins < 60:
        return f"{mins} min"
    h = mins // 60
    return f"{h} hr" if h == 1 else f"{h} hrs"


# ============= CALENDAR PICKER ============

def build_calendar_kb(year, month, for_edit=None, for_nl=False):
    now = datetime.now(IST)
    kb = []

    month_name = cal_module.month_name[month]
    kb.append([InlineKeyboardButton(f"{month_name} {year}", callback_data="noop")])

    kb.append([
        InlineKeyboardButton(d, callback_data="noop")
        for d in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    ])

    weeks = cal_module.monthcalendar(year, month)

    if for_edit:
        day_prefix = f"eday_{for_edit}_"
        nav_prefix = f"ecal_{for_edit}_"
    elif for_nl:
        day_prefix = "nlday_"
        nav_prefix = "nlcal_"
    else:
        day_prefix = "day_"
        nav_prefix = "cal_"

    for week in weeks:
        has_future = False
        for day in week:
            if day != 0:
                day_date = datetime(year, month, day)
                if day_date.date() >= now.date():
                    has_future = True
                    break
        if not has_future:
            continue

        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                day_date = datetime(year, month, day)
                date_str = f"{year}-{month:02d}-{day:02d}"
                if day_date.date() < now.date():
                    row.append(InlineKeyboardButton(" ", callback_data="noop"))
                else:
                    label = f"[{day}]" if day_date.date() == now.date() else str(day)
                    row.append(InlineKeyboardButton(label, callback_data=f"{day_prefix}{date_str}"))
        kb.append(row)

    today_str = now.strftime("%Y-%m-%d")
    tmrw_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    kb.append([
        InlineKeyboardButton("Today", callback_data=f"{day_prefix}{today_str}"),
        InlineKeyboardButton("Tomorrow", callback_data=f"{day_prefix}{tmrw_str}"),
    ])

    next_m = month + 1
    next_y = year
    if next_m > 12:
        next_m = 1
        next_y += 1

    prev_m = month - 1
    prev_y = year
    if prev_m < 1:
        prev_m = 12
        prev_y -= 1

    nav_row = []
    if datetime(prev_y, prev_m, 1) >= datetime(now.year, now.month, 1):
        nav_row.append(InlineKeyboardButton("‹", callback_data=f"{nav_prefix}{prev_y}_{prev_m:02d}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))
    nav_row.append(InlineKeyboardButton("›", callback_data=f"{nav_prefix}{next_y}_{next_m:02d}"))
    kb.append(nav_row)

    if for_edit:
        kb.append([InlineKeyboardButton("« Back", callback_data=f"edit_{for_edit}")])
    elif for_nl:
        kb.append([InlineKeyboardButton("✕ Cancel", callback_data="nlcancel")])
    else:
        kb.append([InlineKeyboardButton("✕ Cancel", callback_data="cancel_add")])

    return InlineKeyboardMarkup(kb)


# ============= TIME PARSER ===============

def parse_time_input(text):
    s = text.strip()

    m = re.match(r'^(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm|AM|PM)?$', s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        ampm = m.group(3)
        if ampm:
            ampm = ampm.lower()
            if ampm == 'pm' and h != 12:
                h += 12
            elif ampm == 'am' and h == 12:
                h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
        return None

    m = re.match(r'^(\d{1,2})\s*(am|pm|AM|PM)$', s)
    if m:
        h = int(m.group(1))
        ampm = m.group(2).lower()
        if ampm == 'pm' and h != 12:
            h += 12
        elif ampm == 'am' and h == 12:
            h = 0
        if 0 <= h <= 23:
            return f"{h:02d}:00"
        return None

    m = re.match(r'^(\d{1,2}):(\d{1,2})$', s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
        return None

    return None


# ============= NATURAL LANGUAGE ===========

def _convert_to_24h(h, mi, ampm):
    ampm = ampm.lower()
    if ampm == 'pm' and h != 12:
        h += 12
    elif ampm == 'am' and h == 12:
        h = 0
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return f"{h:02d}:{mi:02d}"
    return None


def extract_time_from_text(text):
    """Find time pattern in text. Returns (HH:MM, start, end) or None."""

    # "at/by 9:30 pm" or "at/by 9.30pm"
    m = re.search(r'(?:at|by)\s+(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)', text, re.I)
    if m:
        t = _convert_to_24h(int(m.group(1)), int(m.group(2)), m.group(3))
        if t:
            return (t, m.start(), m.end())

    # "at/by 9 pm" or "at/by 9pm"
    m = re.search(r'(?:at|by)\s+(\d{1,2})\s*(am|pm)', text, re.I)
    if m:
        t = _convert_to_24h(int(m.group(1)), 0, m.group(2))
        if t:
            return (t, m.start(), m.end())

    # "at/by 21:30" (24h with at/by)
    m = re.search(r'(?:at|by)\s+(\d{1,2}):(\d{2})\b', text, re.I)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return (f"{h:02d}:{mi:02d}", m.start(), m.end())

    # "9:30 pm" or "9.30pm" (without at/by)
    m = re.search(r'(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)', text, re.I)
    if m:
        t = _convert_to_24h(int(m.group(1)), int(m.group(2)), m.group(3))
        if t:
            return (t, m.start(), m.end())

    # "9pm" or "9 pm" (without at/by)
    m = re.search(r'(\d{1,2})\s*(am|pm)', text, re.I)
    if m:
        t = _convert_to_24h(int(m.group(1)), 0, m.group(2))
        if t:
            return (t, m.start(), m.end())

    return None


def extract_date_from_text(text):
    """Find date keyword/pattern in text. Returns (YYYY-MM-DD, start, end) or None."""
    now = datetime.now(IST)
    lower = text.lower()

    # "day after tomorrow"
    m = re.search(r'\bday\s+after\s+tomorrow\b', lower)
    if m:
        d = now + timedelta(days=2)
        return (d.strftime("%Y-%m-%d"), m.start(), m.end())

    # "today" or "tonight"
    m = re.search(r'\b(today|tonight)\b', lower)
    if m:
        return (now.strftime("%Y-%m-%d"), m.start(), m.end())

    # "tomorrow" / "tmrw" / "tmr"
    m = re.search(r'\b(tomorrow|tmrw|tmr)\b', lower)
    if m:
        d = now + timedelta(days=1)
        return (d.strftime("%Y-%m-%d"), m.start(), m.end())

    # "next week"
    m = re.search(r'\bnext\s+week\b', lower)
    if m:
        d = now + timedelta(days=7)
        return (d.strftime("%Y-%m-%d"), m.start(), m.end())

    # Day names: "monday", "on monday", "on mon"
    days_full = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    days_abbr = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    for i, (full, abbr) in enumerate(zip(days_full, days_abbr)):
        m = re.search(rf'\b(?:on\s+)?({full}|{abbr})\b', lower)
        if m:
            current_day = now.weekday()
            target_day = i
            delta = (target_day - current_day) % 7
            if delta == 0:
                delta = 7
            d = now + timedelta(days=delta)
            return (d.strftime("%Y-%m-%d"), m.start(), m.end())

    # "on 25th" / "on 25" / "25th"
    m = re.search(r'(?:on\s+)?(\d{1,2})\s*(?:st|nd|rd|th)\b', lower)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                d = now.replace(day=day)
                if d.date() < now.date():
                    month = now.month + 1
                    year = now.year
                    if month > 12:
                        month = 1
                        year += 1
                    d = d.replace(year=year, month=month)
                return (d.strftime("%Y-%m-%d"), m.start(), m.end())
            except ValueError:
                pass

    # Month names: "on June 25", "on 25 June", "June 25", "25 June"
    months = ['january', 'february', 'march', 'april', 'may', 'june',
              'july', 'august', 'september', 'october', 'november', 'december']
    months_abbr = ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
                   'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

    for mi_idx, (mfull, mabbr) in enumerate(zip(months, months_abbr), 1):
        # "June 25" or "Jun 25"
        m = re.search(rf'\b(?:on\s+)?({mfull}|{mabbr})\s+(\d{{1,2}})\b', lower)
        if m:
            day = int(m.group(2))
            try:
                year = now.year
                d = datetime(year, mi_idx, day)
                if d.date() < now.date():
                    d = datetime(year + 1, mi_idx, day)
                return (d.strftime("%Y-%m-%d"), m.start(), m.end())
            except ValueError:
                pass

        # "25 June" or "25 Jun"
        m = re.search(rf'\b(?:on\s+)?(\d{{1,2}})\s+({mfull}|{mabbr})\b', lower)
        if m:
            day = int(m.group(1))
            try:
                year = now.year
                d = datetime(year, mi_idx, day)
                if d.date() < now.date():
                    d = datetime(year + 1, mi_idx, day)
                return (d.strftime("%Y-%m-%d"), m.start(), m.end())
            except ValueError:
                pass

    return None


def clean_message(text, time_span, date_span):
    """Remove matched time/date spans and filler prefixes."""
    spans = []
    if time_span:
        spans.append(time_span)
    if date_span:
        spans.append(date_span)
    spans.sort(key=lambda x: x[0], reverse=True)

    result = text
    for start, end in spans:
        result = result[:start] + result[end:]

    # Remove filler prefixes
    fillers = [
        r'^\s*remind\s+me\s+to\s+',
        r'^\s*reminder\s+to\s+',
        r'^\s*reminder\s+',
        r'^\s*remind\s+me\s+',
        r'^\s*remember\s+to\s+',
        r'^\s*don\'?t\s+forget\s+to\s+',
        r'^\s*set\s+reminder\s+(?:to\s+|for\s+)?',
        r'^\s*set\s+(?:a\s+)?reminder\s+(?:to\s+|for\s+)?',
    ]
    for filler in fillers:
        result = re.sub(filler, '', result, flags=re.I)

    # Remove trailing/leading filler words
    result = re.sub(r'\s+', ' ', result).strip()
    result = result.strip('.,;:!? ')

    # Remove leftover "on" at start/end
    result = re.sub(r'^\s*on\s+', '', result, flags=re.I).strip()
    result = re.sub(r'\s+on\s*$', '', result, flags=re.I).strip()

    # Capitalize first letter
    if result:
        result = result[0].upper() + result[1:]

    return result


def parse_natural_text(text):
    """Extract message, date, time from natural text.
    Returns dict with 'message', 'date', 'time' (any can be None) or None."""

    time_result = extract_time_from_text(text)
    date_result = extract_date_from_text(text)

    time_str = time_result[0] if time_result else None
    time_span = (time_result[1], time_result[2]) if time_result else None

    date_str = date_result[0] if date_result else None
    date_span = (date_result[1], date_result[2]) if date_result else None

    message = clean_message(text, time_span, date_span)

    if not message:
        return None

    # Need at least message + one of (date, time)
    if not date_str and not time_str:
        return None

    return {
        'message': message,
        'date': date_str,
        'time': time_str,
    }


# ============= TIME VALIDATION ============

def is_past_time(date_str, time_str):
    now = datetime.now(IST)
    try:
        d = datetime.strptime(normalize_date(date_str), "%Y-%m-%d").date()
        if d > now.date():
            return False
        if d < now.date():
            return True
        t = normalize_time(time_str)
        h, m = map(int, t.split(":"))
        reminder_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return now > reminder_time
    except Exception:
        return False


# ============= HELPERS ====================

def get_reminder_msg(r):
    msg = r[1] if len(r) > 1 else ""
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
    try:
        serial = float(s)
        if 1 < serial < 100000:
            d = datetime(1899, 12, 30) + timedelta(days=int(serial))
            return d.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        pass
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
    repeat = r[4] if len(r) > 4 else "none"
    if not repeat or repeat == "none":
        return False
    d = datetime.strptime(normalize_date(r[2]), "%Y-%m-%d")
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
    sheet.update_cell(row, 3, nd.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, "active")
    sheet.update_cell(row, 7, 0)
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
    msg = get_reminder_msg(r)
    date_str = normalize_date(r[2]) if len(r) > 2 else ""
    time_str = normalize_time(r[3]) if len(r) > 3 else ""
    repeat_str = format_repeat(r[4]) if len(r) > 4 else ""
    return msg, date_str, time_str, repeat_str


# ============= SAFE EDIT ==================
async def safe_edit(message, text, reply_markup=None, parse_mode="HTML"):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        pass


# ============= REMOVE PROMPT KEYBOARD =====
async def remove_prompt_kb(context, user_data):
    msg_id = user_data.get("prompt_msg_id")
    chat_id = user_data.get("prompt_chat_id")
    if msg_id and chat_id:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=None,
            )
        except Exception:
            pass
    user_data.pop("prompt_msg_id", None)
    user_data.pop("prompt_chat_id", None)


# ============= DELETE PROMPT ==============
async def delete_prompt(context, user_data):
    msg_id = user_data.get("prompt_msg_id")
    chat_id = user_data.get("prompt_chat_id")
    if msg_id and chat_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    user_data.pop("prompt_msg_id", None)
    user_data.pop("prompt_chat_id", None)


# ============= STORE PROMPT ==============
def store_prompt(user_data, message):
    user_data["prompt_msg_id"] = message.message_id
    user_data["prompt_chat_id"] = message.chat.id


# ============= REMOVE OLD REMINDER BUTTONS =
async def remove_old_reminder_buttons(context, row):
    key = f"rem_msg_{row}"
    prev = context.bot_data.get(key)
    if prev:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=prev["chat_id"],
                message_id=prev["msg_id"],
                reply_markup=None,
            )
        except Exception:
            pass
    context.bot_data.pop(key, None)


def store_reminder_msg(context, row, chat_id, msg_id):
    context.bot_data[f"rem_msg_{row}"] = {"chat_id": chat_id, "msg_id": msg_id}


# ============= POST INIT =================
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
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
    sent = await update.message.reply_text(
        "<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\nEnter message:",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    store_prompt(context.user_data, sent)


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
        "• Calendar date picker\n"
        "• Flexible time input\n"
        "• Snooze (15m to 12h)\n"
        "• Auto-retry 3× every 10 min if missed\n"
        "• Edit or cancel anytime\n\n"
        "<b>Smart Input</b>\n"
        "Just type naturally:\n"
        "<code>Buy milk tomorrow at 5pm</code>\n"
        "<code>Call mom today at 3:30pm</code>\n"
        "<code>Meeting on Monday at 10am</code>\n"
        "<code>Pay rent on Friday at 9am</code>\n\n"
        "<b>Commands</b>\n"
        "/add — New reminder\n"
        "/list — All reminders\n"
        "/info — This page\n\n"
        "<b>Time Formats</b>\n"
        "You can enter time in any format:\n"
        "<code>9pm</code>  <code>9:30 PM</code>  <code>21:30</code>\n"
        "<code>7:05pm</code>  <code>07:05 AM</code>  <code>14:00</code>"
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
        sent = await query.message.reply_text(
            "<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\nEnter message:",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
        store_prompt(context.user_data, sent)

    # ---- CANCEL ADD ----
    elif data == "cancel_add":
        context.user_data.clear()
        await safe_edit(query.message, home_text(), home_kb())

    # ---- NL CANCEL ----
    elif data == "nlcancel":
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
        store_prompt(context.user_data, query.message)

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
                f"{msg}\n{format_date_short(date_str)}\n\n"
                f"Enter time:\n"
                f"<i>e.g. 9pm, 9:30 PM, 21:30</i>"
            ),
            cancel_kb(),
        )
        store_prompt(context.user_data, query.message)

    # ---- NL CALENDAR NAV ----
    elif data.startswith("nlcal_"):
        parts = data.replace("nlcal_", "").split("_")
        year, month = int(parts[0]), int(parts[1])
        msg = context.user_data.get("nl_message", "")
        time_str = context.user_data.get("nl_time", "")
        time_display = f"\n{format_time_12h(time_str)}" if time_str else ""
        await safe_edit(
            query.message,
            (
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}{time_display}\n\nPick a date:"
            ),
            build_calendar_kb(year, month, for_nl=True),
        )

    # ---- NL DAY SELECTED ----
    elif data.startswith("nlday_"):
        date_str = data.replace("nlday_", "")
        msg = context.user_data.get("nl_message", "")
        time_str = context.user_data.get("nl_time", "")

        if time_str:
            # Have both date and time → validate and ask repeat
            if is_past_time(date_str, time_str):
                now = datetime.now(IST)
                await safe_edit(
                    query.message,
                    (
                        f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                        f"{msg}\n{format_time_12h(time_str)}\n\n"
                        f"⚠ {format_time_12h(time_str)} has already passed today.\n"
                        f"Pick a future date:"
                    ),
                    build_calendar_kb(now.year, now.month, for_nl=True),
                )
                return

            context.user_data["nl_date"] = date_str
            await safe_edit(
                query.message,
                (
                    f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"{msg}\n{format_date_short(date_str)} · {format_time_12h(time_str)}\n\nRepeat?"
                ),
                nl_repeat_kb(),
            )
        else:
            # Have date but no time → ask for time
            context.user_data["nl_date"] = date_str
            context.user_data["step"] = "nl_time"
            await safe_edit(
                query.message,
                (
                    f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"{msg}\n{format_date_short(date_str)}\n\n"
                    f"Enter time:\n"
                    f"<i>e.g. 9pm, 9:30 PM, 21:30</i>"
                ),
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("✕ Cancel", callback_data="nlcancel")],
                ]),
            )
            store_prompt(context.user_data, query.message)

    # ---- NL REPEAT SAVE ----
    elif data.startswith("nlrep_"):
        repeat = data.replace("nlrep_", "")
        msg = context.user_data.get("nl_message", "")
        date = context.user_data.get("nl_date", "")
        time = context.user_data.get("nl_time", "")
        uid = query.from_user.id

        row = [uid, msg, date, time, repeat, "active", 0]
        sheet.append_row(row, value_input_option="RAW")
        context.user_data.clear()

        await safe_edit(
            query.message,
            (
                f"<b>Saved ✓</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n"
                f"{format_date_short(date)} · {format_time_12h(time)} · {format_repeat(repeat)}"
            ),
            home_kb(),
        )

    # ---- SAVE (repeat choice — step-by-step flow) ----
    elif data.startswith("rep_"):
        repeat = data.replace("rep_", "")
        message = context.user_data.get("message", "")
        date = context.user_data.get("date", "")
        time = context.user_data.get("time", "")
        row = [query.from_user.id, message, date, time, repeat, "active", 0]
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

    # ---- SNOOZE PICKER ----
    elif data.startswith("snzpick_"):
        row = int(data.replace("snzpick_", ""))
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)

        if len(r) > 5 and r[5] != "pending":
            await safe_edit(
                query.message,
                (
                    f"{msg}\n"
                    f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}\n\n"
                    f"<i>Already handled</i>"
                ),
            )
            return

        await safe_edit(
            query.message,
            (
                f"{msg}\n"
                f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}\n\n"
                f"Snooze for:"
            ),
            snooze_picker_kb(row),
        )

    # ---- SNOOZE BACK ----
    elif data.startswith("snzbk_"):
        row = int(data.replace("snzbk_", ""))
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)

        if len(r) > 5 and r[5] != "pending":
            await safe_edit(
                query.message,
                (
                    f"{msg}\n"
                    f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}\n\n"
                    f"<i>Already handled</i>"
                ),
            )
            return

        await safe_edit(
            query.message,
            f"{msg}\n\n<b>⏰ Reminder</b>",
            reminder_action_kb(row),
        )

    # ---- SNOOZE DO ----
    elif data.startswith("snzdo_"):
        parts = data.replace("snzdo_", "").split("_")
        row = int(parts[0])
        mins = int(parts[1])

        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)

        if len(r) > 5 and r[5] != "pending":
            await safe_edit(
                query.message,
                (
                    f"{msg}\n"
                    f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}\n\n"
                    f"<i>Already handled</i>"
                ),
            )
            return

        cancel_retry_jobs(context.job_queue, row)
        await remove_old_reminder_buttons(context, row)

        now = datetime.now(IST)
        new_time = now + timedelta(minutes=mins)
        sheet.update_cell(row, 3, new_time.strftime("%Y-%m-%d"))
        sheet.update_cell(row, 4, new_time.strftime("%H:%M"))
        sheet.update_cell(row, 6, "active")
        sheet.update_cell(row, 7, 0)

        context.bot_data.pop(f"rem_msg_{row}", None)

        await safe_edit(
            query.message,
            (
                f"{msg}\n"
                f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}\n\n"
                f"<b>Snoozed {format_snooze_label(mins)}</b> → {format_time_12h(new_time.strftime('%H:%M'))}"
            ),
        )

    # ---- DONE ----
    elif data.startswith("done_"):
        row = int(data.replace("done_", ""))
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)

        if len(r) > 5 and r[5] != "pending":
            await safe_edit(
                query.message,
                (
                    f"{msg}\n"
                    f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}\n\n"
                    f"<i>Already handled</i>"
                ),
            )
            return

        cancel_retry_jobs(context.job_queue, row)
        await remove_old_reminder_buttons(context, row)

        is_repeat = advance_repeat(row, r)
        if not is_repeat:
            sheet.update_cell(row, 6, "done")
            sheet.update_cell(row, 7, 0)

        context.bot_data.pop(f"rem_msg_{row}", None)

        await safe_edit(
            query.message,
            (
                f"{msg}\n"
                f"{format_date_short(date_str)} · {format_time_12h(time_str)} · {repeat_str}\n\n"
                f"<b>Done</b> ✅"
            ),
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
        store_prompt(context.user_data, query.message)

    # ---- EDIT DATE ----
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

        if is_past_time(new_date, time_str):
            now = datetime.now(IST)
            await safe_edit(
                query.message,
                (
                    f"<b>Edit Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"{msg}\n"
                    f"Current: <i>{format_date_short(old_date)} · {format_time_12h(time_str)}</i>\n\n"
                    f"⚠ {format_time_12h(time_str)} has already passed today.\n"
                    f"Pick a future date or change the time first."
                ),
                build_calendar_kb(now.year, now.month, for_edit=row),
            )
            return

        sheet.update_cell(row, 3, new_date)
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

    # ---- EDIT TIME ----
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
                f"Enter new time:\n"
                f"<i>e.g. 9pm, 9:30 PM, 21:30</i>"
            ),
            InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data=f"edit_{row}")],
            ]),
        )
        store_prompt(context.user_data, query.message)

    # ---- CANCEL REMINDER ----
    elif data.startswith("cancelrem_"):
        row = int(data.replace("cancelrem_", ""))
        cancel_retry_jobs(context.job_queue, row)
        r = sheet.row_values(row)
        msg, date_str, time_str, repeat_str = reminder_detail(r)
        sheet.update_cell(row, 6, "cancelled")
        sheet.update_cell(row, 7, 0)

        await remove_old_reminder_buttons(context, row)
        context.bot_data.pop(f"rem_msg_{row}", None)

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

    if step:
        await _handle_step(update, context, step, text)
    else:
        # No active step → try natural language
        await _try_natural(update, context, text)


async def _try_natural(update, context, text):
    """Try to parse natural language input and create a reminder."""
    result = parse_natural_text(text)

    if not result:
        return  # silently ignore

    msg = result['message']
    date = result['date']
    time = result['time']

    if not msg:
        return

    context.user_data.clear()

    # All three found → validate and ask repeat
    if msg and date and time:
        if is_past_time(date, time):
            # Past time today → show calendar to pick different date
            context.user_data["nl_message"] = msg
            context.user_data["nl_time"] = time
            now = datetime.now(IST)
            sent = await update.message.reply_text(
                (
                    f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"{msg}\n\n"
                    f"⚠ {format_time_12h(time)} has already passed today.\n"
                    f"Pick a future date:"
                ),
                reply_markup=build_calendar_kb(now.year, now.month, for_nl=True),
                parse_mode="HTML",
            )
            store_prompt(context.user_data, sent)
            return

        context.user_data["nl_message"] = msg
        context.user_data["nl_date"] = date
        context.user_data["nl_time"] = time
        sent = await update.message.reply_text(
            (
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n"
                f"{format_date_short(date)} · {format_time_12h(time)}\n\nRepeat?"
            ),
            reply_markup=nl_repeat_kb(),
            parse_mode="HTML",
        )
        store_prompt(context.user_data, sent)

    # Message + time, no date → show calendar
    elif msg and time and not date:
        context.user_data["nl_message"] = msg
        context.user_data["nl_time"] = time
        now = datetime.now(IST)
        sent = await update.message.reply_text(
            (
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n{format_time_12h(time)}\n\n"
                f"Pick a date:"
            ),
            reply_markup=build_calendar_kb(now.year, now.month, for_nl=True),
            parse_mode="HTML",
        )
        store_prompt(context.user_data, sent)

    # Message + date, no time → ask for time
    elif msg and date and not time:
        context.user_data["nl_message"] = msg
        context.user_data["nl_date"] = date
        context.user_data["step"] = "nl_time"
        sent = await update.message.reply_text(
            (
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n{format_date_short(date)}\n\n"
                f"Enter time:\n"
                f"<i>e.g. 9pm, 9:30 PM, 21:30</i>"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✕ Cancel", callback_data="nlcancel")],
            ]),
            parse_mode="HTML",
        )
        store_prompt(context.user_data, sent)


async def _handle_step(update, context, step, text):

    # ---- MESSAGE STEP ----
    if step == "message":
        await remove_prompt_kb(context, context.user_data)

        context.user_data["message"] = text
        context.user_data["step"] = "date"
        now = datetime.now(IST)
        sent = await update.message.reply_text(
            f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n{text}\n\nPick a date:",
            reply_markup=build_calendar_kb(now.year, now.month),
            parse_mode="HTML",
        )
        store_prompt(context.user_data, sent)

    # ---- TIME STEP (new reminder) ----
    elif step == "time":
        parsed = parse_time_input(text)
        if not parsed:
            await update.message.reply_text(
                (
                    "Invalid time format. Try again:\n"
                    "<i>e.g. 9pm, 9:30 PM, 21:30, 7:05pm</i>"
                ),
                parse_mode="HTML",
            )
            return

        date_str = context.user_data.get("date", "")

        if is_past_time(date_str, parsed):
            await update.message.reply_text(
                (
                    f"⚠ {format_time_12h(parsed)} has already passed today.\n"
                    f"Enter a future time:"
                ),
                parse_mode="HTML",
            )
            return

        await delete_prompt(context, context.user_data)

        context.user_data["time"] = parsed
        context.user_data["step"] = "repeat"
        msg = context.user_data.get("message", "")
        sent = await update.message.reply_text(
            (
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n{format_date_short(date_str)} · {format_time_12h(parsed)}\n\nRepeat?"
            ),
            reply_markup=repeat_kb(),
            parse_mode="HTML",
        )
        store_prompt(context.user_data, sent)

    # ---- NL TIME STEP (natural language — date known, need time) ----
    elif step == "nl_time":
        parsed = parse_time_input(text)
        if not parsed:
            await update.message.reply_text(
                (
                    "Invalid time format. Try again:\n"
                    "<i>e.g. 9pm, 9:30 PM, 21:30, 7:05pm</i>"
                ),
                parse_mode="HTML",
            )
            return

        date_str = context.user_data.get("nl_date", "")

        if is_past_time(date_str, parsed):
            await update.message.reply_text(
                (
                    f"⚠ {format_time_12h(parsed)} has already passed today.\n"
                    f"Enter a future time:"
                ),
                parse_mode="HTML",
            )
            return

        await delete_prompt(context, context.user_data)

        context.user_data["nl_time"] = parsed
        msg = context.user_data.get("nl_message", "")
        sent = await update.message.reply_text(
            (
                f"<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n{format_date_short(date_str)} · {format_time_12h(parsed)}\n\nRepeat?"
            ),
            reply_markup=nl_repeat_kb(),
            parse_mode="HTML",
        )
        store_prompt(context.user_data, sent)

    # ---- EDIT MESSAGE ----
    elif step == "edit_message":
        row = context.user_data.get("editing_row")
        if row:
            await remove_prompt_kb(context, context.user_data)

            r = sheet.row_values(row)
            old_msg, date_str, time_str, repeat_str = reminder_detail(r)
            sheet.update_cell(row, 2, text)
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

    # ---- EDIT TIME ----
    elif step == "edit_time":
        row = context.user_data.get("editing_row")
        if row:
            parsed = parse_time_input(text)
            if not parsed:
                await update.message.reply_text(
                    (
                        "Invalid time format. Try again:\n"
                        "<i>e.g. 9pm, 9:30 PM, 21:30, 7:05pm</i>"
                    ),
                    parse_mode="HTML",
                )
                return

            r = sheet.row_values(row)
            msg, date_str, old_time, repeat_str = reminder_detail(r)

            if is_past_time(date_str, parsed):
                await update.message.reply_text(
                    (
                        f"⚠ {format_time_12h(parsed)} has already passed today.\n"
                        f"Enter a future time:"
                    ),
                    parse_mode="HTML",
                )
                return

            await delete_prompt(context, context.user_data)

            sheet.update_cell(row, 4, parsed)
            context.user_data.clear()
            await update.message.reply_text(
                (
                    f"<b>Updated ✓</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"{msg}\n"
                    f"Date: {format_date_short(date_str)}\n"
                    f"Time: {format_time_12h(old_time)} → <b>{format_time_12h(parsed)}</b> · {repeat_str}"
                ),
                reply_markup=home_kb(),
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

    if len(r) <= 5 or r[5] != "pending":
        logger.info(f"auto_retry: row {row} status '{r[5] if len(r) > 5 else '?'}', skipping.")
        return

    try:
        count = int(r[6])
    except (IndexError, ValueError):
        count = 0

    if count >= DEFAULT_MAX_RETRIES:
        if not advance_repeat(row, r):
            sheet.update_cell(row, 6, "missed")
            sheet.update_cell(row, 7, 0)
        return

    await remove_old_reminder_buttons(context, row)

    msg = get_reminder_msg(r)
    new_count = count + 1
    text = f"{msg}\n\n<b>⏰Reminder</b> ({new_count}/{DEFAULT_MAX_RETRIES})"

    sent_msg = await context.bot.send_message(
        chat_id=chat, text=text,
        reply_markup=reminder_action_kb(row), parse_mode="HTML",
    )

    store_reminder_msg(context, row, chat, sent_msg.message_id)

    sheet.update_cell(row, 7, new_count)

    if new_count >= DEFAULT_MAX_RETRIES:
        if not advance_repeat(row, r):
            sheet.update_cell(row, 6, "missed")
            sheet.update_cell(row, 7, 0)
    else:
        context.job_queue.run_once(
            auto_retry, DEFAULT_RETRY_INTERVAL,
            data={"row": row, "chat": chat}, name=f"retry-{row}",
        )


# ============= SCHEDULER =================
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(IST)
    now_date = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M")

    logger.info(f"[CRON] Tick {now_date} {now_time}")

    try:
        all_vals = sheet.get_all_values()
    except Exception as e:
        logger.error(f"[CRON] Sheet read failed: {e}, re-authorizing...")
        try:
            client.login()
            all_vals = sheet.get_all_values()
            logger.info("[CRON] Re-auth OK")
        except Exception as e2:
            logger.error(f"[CRON] Re-auth failed: {e2}")
            return

    if len(all_vals) < 2:
        return

    for idx, vals in enumerate(all_vals[1:], start=2):
        if len(vals) < 7:
            continue

        status = str(vals[5]).strip().lower()
        if status != "active":
            continue

        raw_date = str(vals[2]).strip()
        raw_time = str(vals[3]).strip()
        date_val = normalize_date(raw_date)
        time_val = normalize_time(raw_time)

        logger.info(
            f"[CRON] Row {idx}: raw=({raw_date!r},{raw_time!r}) "
            f"norm=({date_val},{time_val}) vs now=({now_date},{now_time})"
        )

        if date_val != now_date or time_val != now_time:
            continue

        uid = vals[0]
        try:
            uid = int(uid)
        except (ValueError, TypeError):
            pass

        msg = str(vals[1]).strip()
        logger.info(f"[CRON] FIRING row {idx}: '{msg[:30]}' for {uid}")

        cancel_retry_jobs(context.job_queue, idx)
        await remove_old_reminder_buttons(context, idx)

        text = f"{msg}\n\n<b>⏰ Reminder</b>"

        try:
            sent_msg = await context.bot.send_message(
                chat_id=uid, text=text,
                reply_markup=reminder_action_kb(idx), parse_mode="HTML",
            )
            store_reminder_msg(context, idx, uid, sent_msg.message_id)
        except Exception as e:
            logger.error(f"[CRON] Send to {uid} failed: {e}")
            continue

        sheet.update_cell(idx, 6, "pending")
        sheet.update_cell(idx, 7, 0)

        context.job_queue.run_once(
            auto_retry, DEFAULT_RETRY_INTERVAL,
            data={"row": idx, "chat": uid}, name=f"retry-{idx}",
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
