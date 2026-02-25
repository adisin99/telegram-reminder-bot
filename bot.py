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
TOKEN = "8235103406:AAFYJ2SNRW4A4AAEyz8t2h-5BeYk8rnzzwE"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1bHeyDgw9P-3iRLOp_6VpHGKSn9St6yjyqP-35hPg6Rs/edit?pli=1&gid=0#gid=0"

IST = pytz.timezone("Asia/Kolkata")
DIV = "━━━━━━━━━━━━━━━━━━━━"

# Defaults (overridden per-user via settings)
DEF_RETRIES = 3
DEF_RETRY_GAP = 10  # minutes
DEF_DIGEST_TIME = "07:00"

# Sheet1: user_id | message | date | time | repeat | status | retry_count
# Sheet2: user_id | emoji | message | time | repeat
# Sheet3: user_id | digest_on | digest_time | max_retries | retry_gap

EMOJIS = [
    ("💊", "Health"), ("💼", "Work"), ("🏋️", "Fitness"), ("📞", "Call"),
    ("🛒", "Shopping"), ("💰", "Finance"), ("📚", "Study"), ("🏠", "Home"),
    ("✈️", "Travel"), ("📝", "General"),
]

# =============== LOGGING =================
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ============= GOOGLE SHEET ==============
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.environ.get("GOOGLE_CREDS")
if not creds_json:
    raise Exception("GOOGLE_CREDS missing")
credentials = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
client = gspread.authorize(credentials)
workbook = client.open_by_url(SHEET_URL)

# Get or create sheets
def get_or_create_sheet(name, headers):
    try:
        ws = workbook.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = workbook.add_worksheet(title=name, rows=100, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
    return ws

sheet = get_or_create_sheet("Reminders", ["user_id", "message", "date", "time", "repeat", "status", "retry_count"])
tpl_sheet = get_or_create_sheet("Templates", ["user_id", "emoji", "message", "time", "repeat"])
cfg_sheet = get_or_create_sheet("Settings", ["user_id", "digest_on", "digest_time", "max_retries", "retry_gap"])


# ============= FORMATTERS ================

def hdr(title):
    return f"<b>{title}</b>\n{DIV}"

def detail(msg, ds, ts, rs=None):
    p = [fmt_date(ds), fmt_time(ts)]
    if rs:
        p.append(rs)
    return f"{msg}\n{' · '.join(p)}"

def fmt_date(ds):
    try:
        return datetime.strptime(norm_date(ds), "%Y-%m-%d").strftime("%-d %b")
    except Exception:
        return str(ds)

def fmt_time(ts):
    try:
        h, m = map(int, norm_time(ts).split(":"))
        return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
    except Exception:
        return str(ts)

def fmt_rep(r):
    return {"none": "Once", "daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}.get(str(r), str(r))

def fmt_snz(mins):
    return f"{mins} min" if mins < 60 else f"{mins // 60} hr{'s' if mins >= 120 else ''}"

def s_icon(s):
    return {"active": "○", "pending": "●", "missed": "✗", "snoozed": "◷"}.get(str(s), "?")

def s_label(s):
    return {"active": "Active", "pending": "Pending", "missed": "Missed", "snoozed": "Snoozed"}.get(str(s), str(s))


# ============= NORMALIZERS ================

def norm_date(val):
    s = str(val).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    for f in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        n = float(s)
        if 1 < n < 100000:
            return (datetime(1899, 12, 30) + timedelta(days=int(n))).strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        pass
    return s

def norm_time(val):
    s = str(val).strip()
    if ":" in s:
        try:
            p = s.split(":")
            h, m = int(p[0]), int(p[1].split()[0])
            u = s.upper()
            if "PM" in u and h != 12: h += 12
            elif "AM" in u and h == 12: h = 0
            return f"{h:02d}:{m:02d}"
        except (ValueError, IndexError):
            return s
    try:
        t = round(float(s) * 24 * 60)
        return f"{t // 60:02d}:{t % 60:02d}"
    except ValueError:
        return s


# ============= SETTINGS HELPERS ===========

def get_cfg(uid):
    """Get user settings, create defaults if missing."""
    uid_s = str(uid)
    try:
        rows = cfg_sheet.get_all_values()
    except Exception:
        try:
            client.login()
            rows = cfg_sheet.get_all_values()
        except Exception:
            return {"digest_on": True, "digest_time": DEF_DIGEST_TIME, "max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP}
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == uid_s:
            return {
                "digest_on": str(r[1]).lower() != "false",
                "digest_time": norm_time(r[2]) if r[2] else DEF_DIGEST_TIME,
                "max_retries": int(r[3]) if r[3] else DEF_RETRIES,
                "retry_gap": int(r[4]) if r[4] else DEF_RETRY_GAP,
                "_row": i,
            }
    # Create default
    cfg_sheet.append_row([uid_s, "true", DEF_DIGEST_TIME, DEF_RETRIES, DEF_RETRY_GAP], value_input_option="RAW")
    return {"digest_on": True, "digest_time": DEF_DIGEST_TIME, "max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP}

def get_cfg_row(uid):
    """Find the row number for this user in settings."""
    uid_s = str(uid)
    try:
        rows = cfg_sheet.get_all_values()
    except Exception:
        return None
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == uid_s:
            return i
    return None

def save_cfg(uid, field, value):
    """Update a single settings field."""
    row = get_cfg_row(uid)
    if not row:
        get_cfg(uid)  # creates default
        row = get_cfg_row(uid)
    if not row:
        return
    col_map = {"digest_on": 2, "digest_time": 3, "max_retries": 4, "retry_gap": 5}
    if field in col_map:
        cfg_sheet.update_cell(row, col_map[field], str(value))


# ============= HELPERS ====================

def get_detail(r):
    msg = str(r[1]).strip() if len(r) > 1 else ""
    ds = norm_date(r[2]) if len(r) > 2 else ""
    ts = norm_time(r[3]) if len(r) > 3 else ""
    rs = fmt_rep(r[4]) if len(r) > 4 else ""
    return msg, ds, ts, rs

def handled(r):
    return len(r) > 5 and r[5] != "pending"

def is_past(ds, ts):
    now = datetime.now(IST)
    try:
        d = datetime.strptime(norm_date(ds), "%Y-%m-%d").date()
        if d != now.date():
            return d < now.date()
        h, m = map(int, norm_time(ts).split(":"))
        return now > now.replace(hour=h, minute=m, second=0, microsecond=0)
    except Exception:
        return False

def past_msg(ts):
    return f"⚠ {fmt_time(ts)} has already passed today."

def advance_rep(row, r):
    rep = r[4] if len(r) > 4 else "none"
    if not rep or rep == "none":
        return False
    d = datetime.strptime(norm_date(r[2]), "%Y-%m-%d")
    if rep == "daily":
        nd = d + timedelta(days=1)
    elif rep == "weekly":
        nd = d + timedelta(days=7)
    elif rep == "monthly":
        mo, yr = d.month + 1, d.year
        if mo > 12: mo, yr = 1, yr + 1
        nd = d.replace(year=yr, month=mo)
    else:
        return False
    sheet.update_cell(row, 3, nd.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, "active")
    sheet.update_cell(row, 7, 0)
    return True

def kill_jobs(jq, row):
    for j in jq.get_jobs_by_name(f"retry-{row}"):
        j.schedule_removal()
    for j in jq.get_jobs_by_name(f"snooze-{row}"):
        j.schedule_removal()

def do_save(uid, ud, msg, date, time, rep):
    sheet.append_row([uid, msg, date, time, rep, "active", 0], value_input_option="RAW")
    ud.clear()


# ============= UI ========================

def home_text():
    return (f"{hdr('Smart Reminder Bot')}\nManage your reminders easily.\n\n"
            "Use <b>＋ New</b> or /add to create.\n"
            "Or just type naturally:\n<i>Buy milk tomorrow at 5pm</i>\n\n"
            "Use /list to view all.")

def home_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("＋ New", callback_data="add")]])

def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="cancel")]])

def repeat_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Once", callback_data="rep_none"),
         InlineKeyboardButton("Daily", callback_data="rep_daily")],
        [InlineKeyboardButton("Weekly", callback_data="rep_weekly"),
         InlineKeyboardButton("Monthly", callback_data="rep_monthly")],
        [InlineKeyboardButton("✕ Cancel", callback_data="cancel")]])

def act_kb(row):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Snooze", callback_data=f"snzp_{row}"),
        InlineKeyboardButton("Done", callback_data=f"done_{row}")]])

def snz_kb(row):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("15m", callback_data=f"snz_{row}_15"),
         InlineKeyboardButton("30m", callback_data=f"snz_{row}_30"),
         InlineKeyboardButton("45m", callback_data=f"snz_{row}_45")],
        [InlineKeyboardButton("1h", callback_data=f"snz_{row}_60"),
         InlineKeyboardButton("2h", callback_data=f"snz_{row}_120"),
         InlineKeyboardButton("3h", callback_data=f"snz_{row}_180")],
        [InlineKeyboardButton("5h", callback_data=f"snz_{row}_300"),
         InlineKeyboardButton("8h", callback_data=f"snz_{row}_480"),
         InlineKeyboardButton("12h", callback_data=f"snz_{row}_720")],
        [InlineKeyboardButton("« Back", callback_data=f"snzb_{row}")]])


# ============= CALENDAR ==================

def cal_kb(year, month, back_cb="cancel", back_txt="✕ Cancel"):
    now = datetime.now(IST)
    kb = [[InlineKeyboardButton(f"{cal_module.month_name[month]} {year}", callback_data="noop")]]
    kb.append([InlineKeyboardButton(d, callback_data="noop") for d in "Mo Tu We Th Fr Sa Su".split()])
    for week in cal_module.monthcalendar(year, month):
        if not any(d and datetime(year, month, d).date() >= now.date() for d in week if d):
            continue
        row = []
        for day in week:
            if day == 0 or datetime(year, month, day).date() < now.date():
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                ds = f"{year}-{month:02d}-{day:02d}"
                lbl = f"[{day}]" if datetime(year, month, day).date() == now.date() else str(day)
                row.append(InlineKeyboardButton(lbl, callback_data=f"day_{ds}"))
        kb.append(row)
    td = now.strftime("%Y-%m-%d")
    tm = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    kb.append([InlineKeyboardButton("Today", callback_data=f"day_{td}"),
               InlineKeyboardButton("Tomorrow", callback_data=f"day_{tm}")])
    nm, ny = (month % 12) + 1, year + (1 if month == 12 else 0)
    pm, py = ((month - 2) % 12) + 1, year - (1 if month == 1 else 0)
    nav = []
    if datetime(py, pm, 1) >= datetime(now.year, now.month, 1):
        nav.append(InlineKeyboardButton("‹", callback_data=f"cal_{py}_{pm:02d}"))
    else:
        nav.append(InlineKeyboardButton(" ", callback_data="noop"))
    nav.append(InlineKeyboardButton("›", callback_data=f"cal_{ny}_{nm:02d}"))
    kb.append(nav)
    kb.append([InlineKeyboardButton(back_txt, callback_data=back_cb)])
    return InlineKeyboardMarkup(kb)


# ============= TIME PARSER ===============

def parse_time(text):
    s = text.strip()
    m = re.match(r'^(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)?$', s, re.I)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if m.group(3):
            ap = m.group(3).lower()
            if ap == 'pm' and h != 12: h += 12
            elif ap == 'am' and h == 12: h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
    m = re.match(r'^(\d{1,2})\s*(am|pm)$', s, re.I)
    if m:
        h = int(m.group(1))
        ap = m.group(2).lower()
        if ap == 'pm' and h != 12: h += 12
        elif ap == 'am' and h == 12: h = 0
        if 0 <= h <= 23:
            return f"{h:02d}:00"
    m = re.match(r'^(\d{1,2}):(\d{1,2})$', s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
    return None


# ============= NATURAL LANGUAGE ===========

def _to24(h, mi, ap):
    ap = ap.lower()
    if ap == 'pm' and h != 12: h += 12
    elif ap == 'am' and h == 12: h = 0
    return f"{h:02d}:{mi:02d}" if 0 <= h <= 23 and 0 <= mi <= 59 else None

def _find_time(text):
    for pat, mode in [
        (r'(?:at|by)\s+(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)', 'hma'),
        (r'(?:at|by)\s+(\d{1,2})\s*(am|pm)', 'ha'),
        (r'(?:at|by)\s+(\d{1,2}):(\d{2})\b', '24'),
        (r'(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)', 'hma'),
        (r'(\d{1,2})\s*(am|pm)', 'ha'),
    ]:
        m = re.search(pat, text, re.I)
        if m:
            if mode == 'hma': t = _to24(int(m.group(1)), int(m.group(2)), m.group(3))
            elif mode == 'ha': t = _to24(int(m.group(1)), 0, m.group(2))
            else:
                h, mi = int(m.group(1)), int(m.group(2))
                t = f"{h:02d}:{mi:02d}" if 0 <= h <= 23 and 0 <= mi <= 59 else None
            if t: return t, m.start(), m.end()
    return None

def _find_date(text):
    now = datetime.now(IST)
    low = text.lower()
    for pat, delta in [
        (r'\bday\s+after\s+tomorrow\b', 2), (r'\b(today|tonight)\b', 0),
        (r'\b(tomorrow|tmrw|tmr)\b', 1), (r'\bnext\s+week\b', 7),
    ]:
        m = re.search(pat, low)
        if m: return (now + timedelta(days=delta)).strftime("%Y-%m-%d"), m.start(), m.end()
    days = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
    abbrs = ['mon','tue','wed','thu','fri','sat','sun']
    for i, (full, abr) in enumerate(zip(days, abbrs)):
        m = re.search(rf'\b(?:on\s+)?({full}|{abr})\b', low)
        if m:
            d = (i - now.weekday()) % 7 or 7
            return (now + timedelta(days=d)).strftime("%Y-%m-%d"), m.start(), m.end()
    m = re.search(r'(?:on\s+)?(\d{1,2})\s*(?:st|nd|rd|th)\b', low)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                d = now.replace(day=day)
                if d.date() < now.date():
                    mo, yr = now.month + 1, now.year
                    if mo > 12: mo, yr = 1, yr + 1
                    d = d.replace(year=yr, month=mo)
                return d.strftime("%Y-%m-%d"), m.start(), m.end()
            except ValueError: pass
    months = ['january','february','march','april','may','june','july','august','september','october','november','december']
    mabbr = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec']
    for mi, (mf, ma) in enumerate(zip(months, mabbr), 1):
        for pt in [rf'\b(?:on\s+)?({mf}|{ma})\s+(\d{{1,2}})\b', rf'\b(?:on\s+)?(\d{{1,2}})\s+({mf}|{ma})\b']:
            m = re.search(pt, low)
            if m:
                g1, g2 = m.group(1), m.group(2)
                day = int(g2) if g1.isalpha() else int(g1)
                try:
                    d = datetime(now.year, mi, day)
                    if d.date() < now.date(): d = datetime(now.year + 1, mi, day)
                    return d.strftime("%Y-%m-%d"), m.start(), m.end()
                except ValueError: pass
    return None

def _find_repeat(text):
    low = text.lower()
    for pat, val in [
        (r'\b(?:every\s*day|daily)\b', 'daily'), (r'\b(?:every\s*week|weekly)\b', 'weekly'),
        (r'\b(?:every\s*month|monthly)\b', 'monthly'), (r'\b(?:once|one[\s-]?time|no\s*repeat)\b', 'none'),
    ]:
        m = re.search(pat, low)
        if m: return val, m.start(), m.end()
    return None

def _clean(text, spans):
    for s, e in sorted([x for x in spans if x], key=lambda x: x[0], reverse=True):
        text = text[:s] + text[e:]
    for f in [r'^\s*remind\s+me\s+to\s+', r'^\s*reminder\s+to\s+', r'^\s*reminder\s+',
              r'^\s*remind\s+me\s+', r'^\s*remember\s+to\s+', r"^\s*don'?t\s+forget\s+to\s+",
              r'^\s*set\s+(?:a\s+)?reminder\s+(?:to\s+|for\s+)?']:
        text = re.sub(f, '', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip().strip('.,;:!? ')
    text = re.sub(r'^\s*on\s+', '', text, flags=re.I).strip()
    text = re.sub(r'\s+on\s*$', '', text, flags=re.I).strip()
    return text[0].upper() + text[1:] if text else text

def parse_nl(text):
    tr, dr, rr = _find_time(text), _find_date(text), _find_repeat(text)
    ts = tr[0] if tr else None
    ds = dr[0] if dr else None
    rep = rr[0] if rr else None
    msg = _clean(text, [(tr[1],tr[2]) if tr else None, (dr[1],dr[2]) if dr else None, (rr[1],rr[2]) if rr else None])
    if not msg or not ts: return None
    return {'message': msg, 'date': ds, 'time': ts, 'repeat': rep}


# ============= MESSAGE UTILS =============

async def safe_edit(msg, text, kb=None):
    try: await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception: pass

async def rm_prompt(ctx, ud):
    mid, cid = ud.pop("p_mid", None), ud.pop("p_cid", None)
    if mid and cid:
        try: await ctx.bot.edit_message_reply_markup(chat_id=cid, message_id=mid, reply_markup=None)
        except Exception: pass

async def del_prompt(ctx, ud):
    mid, cid = ud.pop("p_mid", None), ud.pop("p_cid", None)
    if mid and cid:
        try: await ctx.bot.delete_message(chat_id=cid, message_id=mid)
        except Exception: pass

def save_p(ud, msg):
    ud["p_mid"], ud["p_cid"] = msg.message_id, msg.chat.id

async def rm_btns(ctx, row):
    prev = ctx.bot_data.pop(f"r_{row}", None)
    if prev:
        try: await ctx.bot.edit_message_reply_markup(chat_id=prev["c"], message_id=prev["m"], reply_markup=None)
        except Exception: pass

def save_rm(ctx, row, cid, mid):
    ctx.bot_data[f"r_{row}"] = {"c": cid, "m": mid}

async def rm_home(ctx, ud):
    mid, cid = ud.pop("h_mid", None), ud.pop("h_cid", None)
    if mid and cid:
        try: await ctx.bot.edit_message_reply_markup(chat_id=cid, message_id=mid, reply_markup=None)
        except Exception: pass

def save_home(ud, msg):
    ud["h_mid"], ud["h_cid"] = msg.message_id, msg.chat.id


# ============= POST INIT =================

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("add", "New reminder"),
        BotCommand("list", "All reminders"),
        BotCommand("templates", "Saved templates"),
        BotCommand("settings", "Bot settings"),
        BotCommand("info", "About this bot")])


# ============= COMMANDS ===================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    # Ensure user has settings row
    get_cfg(update.effective_user.id)
    sent = await update.message.reply_text(home_text(), reply_markup=home_kb(), parse_mode="HTML")
    save_home(ctx.user_data, sent)

async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    ctx.user_data["step"] = "message"
    sent = await update.message.reply_text(
        f"{hdr('New Reminder')}\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
    save_p(ctx.user_data, sent)

async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    await show_list(update.message, update.effective_user.id, ctx.user_data, new=True)

async def info_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"{hdr('Smart Reminder Bot')}\n\n"
        "Set reminders and get notified on time.\n\n"
        "<b>Features</b>\n• One-time & recurring reminders\n• Calendar date picker\n"
        "• Flexible time input\n• Snooze (15m to 12h)\n"
        "• Auto-retry if missed\n• Edit or cancel anytime\n"
        "• Reusable templates\n• Daily morning digest\n• Customisable settings\n\n"
        "<b>Smart Input</b>\nJust type naturally:\n"
        "<code>Buy milk tomorrow at 5pm</code>\n"
        "<code>Call mom at 3:30pm</code>\n"
        "<code>Meeting on Monday at 10am weekly</code>\n\n"
        "<b>Commands</b>\n/add — New reminder\n/list — All reminders\n"
        "/templates — Saved templates\n/settings — Bot settings\n/info — This page\n\n"
        "<b>Time Formats</b>\n"
        "<code>9pm</code>  <code>9:30 PM</code>  <code>21:30</code>  <code>7:05pm</code>",
        parse_mode="HTML")


# ============= SETTINGS ===================

async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    await show_settings(update.message, update.effective_user.id, new=True)

async def show_settings(target, uid, new=False):
    cfg = get_cfg(uid)
    d_status = "ON" if cfg["digest_on"] else "OFF"
    d_time = fmt_time(cfg["digest_time"]) if cfg["digest_on"] else "—"
    txt = (f"{hdr('Settings')}\n\n"
           f"<b>Daily Digest</b>: {d_status}"
           + (f" · {d_time}" if cfg["digest_on"] else "") +
           f"\n<b>Max Retries</b>: {cfg['max_retries']}×"
           f"\n<b>Retry Gap</b>: {cfg['retry_gap']} min")
    btns = [
        [InlineKeyboardButton(f"Digest: {d_status}", callback_data="cfg_digest_toggle"),
         InlineKeyboardButton(f"⏰ {d_time}" if cfg["digest_on"] else "—", callback_data="cfg_digest_time" if cfg["digest_on"] else "noop")],
        [InlineKeyboardButton(f"Retries: {cfg['max_retries']}×", callback_data="cfg_retries"),
         InlineKeyboardButton(f"Gap: {cfg['retry_gap']}m", callback_data="cfg_gap")],
        [InlineKeyboardButton("« Back", callback_data="home")]]
    if new:
        await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
    else:
        await safe_edit(target, txt, InlineKeyboardMarkup(btns))


# ============= TEMPLATES ==================

async def templates_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    await show_templates(update.message, update.effective_user.id, ctx.user_data, new=True)

async def show_templates(target, uid, ud, new=False):
    uid_s = str(uid)
    try:
        rows = tpl_sheet.get_all_values()
    except Exception:
        try:
            client.login()
            rows = tpl_sheet.get_all_values()
        except Exception:
            rows = []
    items = [(i, r) for i, r in enumerate(rows[1:], 2) if str(r[0]) == uid_s]

    if not items:
        txt = f"{hdr('Templates')}\nNo templates yet.\nSave frequent reminders as templates!"
        btns = [[InlineKeyboardButton("＋ Create", callback_data="tpl_add")],
                [InlineKeyboardButton("« Back", callback_data="home")]]
        if new:
            await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        else:
            await safe_edit(target, txt, InlineKeyboardMarkup(btns))
        return

    lines = [hdr("Templates")]
    num_btns = []
    for idx, (ri, r) in enumerate(items, 1):
        emoji = r[1] if len(r) > 1 else "📝"
        msg = str(r[2]) if len(r) > 2 else ""
        ts = fmt_time(norm_time(r[3])) if len(r) > 3 else ""
        rep = fmt_rep(r[4]) if len(r) > 4 else ""
        short = msg[:25] + "…" if len(msg) > 25 else msg
        lines.append(f"\n<b>{idx}</b> {emoji} {short}\n   {ts} · {rep}")
        num_btns.append(InlineKeyboardButton(str(idx), callback_data=f"tplv_{ri}"))

    btn_rows = [num_btns[i:i+5] for i in range(0, len(num_btns), 5)]
    btn_rows.append([InlineKeyboardButton("＋ Create", callback_data="tpl_add")])
    btn_rows.append([InlineKeyboardButton("« Back", callback_data="home")])

    txt = "\n".join(lines)
    if new:
        await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(btn_rows), parse_mode="HTML")
    else:
        await safe_edit(target, txt, InlineKeyboardMarkup(btn_rows))


# ============= SHOW LIST =================

async def show_list(target, uid, ud, new=False):
    try:
        rows = sheet.get_all_records()
    except Exception:
        try:
            client.login()
            rows = sheet.get_all_records()
        except Exception:
            rows = []
    items = [(i, r) for i, r in enumerate(rows, 2)
             if str(r.get("user_id", "")) == str(uid)
             and str(r.get("status", "")).strip() in ("active", "pending", "missed", "snoozed")]

    if not items:
        t = f"{hdr('Reminders')}\nNo reminders found."
        kb = home_kb()
        if new:
            sent = await target.reply_text(t, reply_markup=kb, parse_mode="HTML")
            save_home(ud, sent)
        else:
            await safe_edit(target, t, kb)
        return

    lines = [hdr("Reminders")]
    btns = []
    for idx, (ri, r) in enumerate(items, 1):
        st = str(r.get("status", ""))
        msg = str(r.get("message", ""))
        short = msg[:30] + "…" if len(msg) > 30 else msg
        lines.append(
            f"\n<b>{idx}</b> {s_icon(st)} {short}\n"
            f"   {fmt_date(norm_date(r.get('date', '')))} · "
            f"{fmt_time(norm_time(r.get('time', '')))}")

    num_row = []
    for idx, (ri, r) in enumerate(items, 1):
        num_row.append(InlineKeyboardButton(str(idx), callback_data=f"view_{ri}"))
        if len(num_row) == 5:
            btns.append(num_row)
            num_row = []
    if num_row:
        btns.append(num_row)
    btns.append([InlineKeyboardButton("« Back", callback_data="home")])
    t = "\n".join(lines)
    if new:
        await target.reply_text(t, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
    else:
        await safe_edit(target, t, InlineKeyboardMarkup(btns))


# ============= SAVE OR ASK REPEAT ========

async def finish_or_repeat(target, uid, ud, msg, date, time, edit_msg=False):
    rep = ud.get("repeat")
    if rep:
        do_save(uid, ud, msg, date, time, rep)
        txt = f"{hdr('Saved ✓')}\n{detail(msg, date, time, fmt_rep(rep))}"
        if edit_msg:
            await safe_edit(target, txt, home_kb())
        else:
            sent = await target.reply_text(txt, reply_markup=home_kb(), parse_mode="HTML")
            save_home(ud, sent)
    else:
        ud["step"] = "repeat"
        txt = f"{hdr('New Reminder')}\n{detail(msg, date, time)}\n\nRepeat?"
        if edit_msg:
            await safe_edit(target, txt, repeat_kb())
        else:
            sent = await target.reply_text(txt, reply_markup=repeat_kb(), parse_mode="HTML")
            save_p(ud, sent)


# ============= BUTTON HANDLER ============

async def on_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data, ud = q.data, ctx.user_data

    if data == "noop":
        return

    # ---- HOME / CANCEL ----
    if data in ("home", "cancel"):
        ud.clear()
        await safe_edit(q.message, home_text(), home_kb())
        save_home(ud, q.message)

    elif data == "add":
        await rm_home(ctx, ud)
        ud.clear()
        ud["step"] = "message"
        sent = await q.message.reply_text(
            f"{hdr('New Reminder')}\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
        save_p(ud, sent)

    # ---- CALENDAR NAV ----
    elif data.startswith("cal_"):
        parts = data[4:].split("_")
        yr, mo = int(parts[0]), int(parts[1])
        step = ud.get("step")
        if step == "edit_date":
            row = ud.get("editing_row")
            r = sheet.row_values(row)
            msg, ds, ts, rs = get_detail(r)
            await safe_edit(q.message,
                f"{hdr('Edit Reminder')}\n{msg}\nCurrent: <i>{fmt_date(ds)} · {fmt_time(ts)}</i>\n\nPick new date:",
                cal_kb(yr, mo, f"edit_{row}", "« Back"))
        else:
            msg = ud.get("message", "")
            ts = ud.get("time", "")
            td = f"\n{fmt_time(ts)}" if ts else ""
            await safe_edit(q.message,
                f"{hdr('New Reminder')}\n{msg}{td}\n\nPick a date:", cal_kb(yr, mo))

    # ---- DAY SELECTED ----
    elif data.startswith("day_"):
        date_str = data[4:]
        step = ud.get("step")

        if step == "edit_date":
            row = ud.get("editing_row")
            r = sheet.row_values(row)
            msg, old_d, ts, rs = get_detail(r)
            if is_past(date_str, ts):
                now = datetime.now(IST)
                await safe_edit(q.message,
                    f"{hdr('Edit Reminder')}\n{msg}\nCurrent: <i>{fmt_date(old_d)} · {fmt_time(ts)}</i>\n\n"
                    f"{past_msg(ts)}\nPick a future date or change the time first.",
                    cal_kb(now.year, now.month, f"edit_{row}", "« Back"))
                return
            sheet.update_cell(row, 3, date_str)
            ud.clear()
            await safe_edit(q.message,
                f"{hdr('Updated ✓')}\n{msg}\nDate: {fmt_date(old_d)} → <b>{fmt_date(date_str)}</b>\n"
                f"Time: {fmt_time(ts)} · {rs}", home_kb())
            save_home(ud, q.message)
        else:
            ud["date"] = date_str
            msg = ud.get("message", "")
            ts = ud.get("time")
            if ts:
                if is_past(date_str, ts):
                    now = datetime.now(IST)
                    await safe_edit(q.message,
                        f"{hdr('New Reminder')}\n{msg}\n{fmt_time(ts)}\n\n"
                        f"{past_msg(ts)}\nPick a future date:", cal_kb(now.year, now.month))
                    return
                await finish_or_repeat(q.message, q.from_user.id, ud, msg, date_str, ts, edit_msg=True)
            else:
                ud["step"] = "time"
                await safe_edit(q.message,
                    f"{hdr('New Reminder')}\n{msg}\n{fmt_date(date_str)}\n\n"
                    f"Enter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", cancel_kb())
                save_p(ud, q.message)

    # ---- REPEAT → SAVE ----
    elif data.startswith("rep_"):
        rep = data[4:]
        msg, date, time = ud.get("message", ""), ud.get("date", ""), ud.get("time", "")
        do_save(q.from_user.id, ud, msg, date, time, rep)
        await safe_edit(q.message,
            f"{hdr('Saved ✓')}\n{detail(msg, date, time, fmt_rep(rep))}", home_kb())
        save_home(ud, q.message)

    # ---- VIEW REMINDER ----
    elif data.startswith("view_"):
        row = int(data[5:])
        r = sheet.row_values(row)
        msg, ds, ts, rs = get_detail(r)
        st = r[5] if len(r) > 5 else "active"
        btns = []
        if st != "missed":
            btns.append([
                InlineKeyboardButton("✎ Edit", callback_data=f"edit_{row}"),
                InlineKeyboardButton("✕ Cancel", callback_data=f"crem_{row}")])
        else:
            btns.append([InlineKeyboardButton("✕ Remove", callback_data=f"crem_{row}")])
        btns.append([InlineKeyboardButton("« Back", callback_data="list_refresh")])
        await safe_edit(q.message,
            f"{hdr('Reminder')}\n{msg}\n\n"
            f"{fmt_date(ds)} · {fmt_time(ts)}\n"
            f"{rs} · {s_icon(st)} <i>{s_label(st)}</i>",
            InlineKeyboardMarkup(btns))

    # ---- SNOOZE PICKER ----
    elif data.startswith("snzp_"):
        row = int(data[5:])
        r = sheet.row_values(row)
        msg, ds, ts, rs = get_detail(r)
        if handled(r):
            await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\n<i>Already handled</i>")
            return
        await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\nSnooze for:", snz_kb(row))

    elif data.startswith("snzb_"):
        row = int(data[5:])
        r = sheet.row_values(row)
        msg, ds, ts, rs = get_detail(r)
        if handled(r):
            await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\n<i>Already handled</i>")
            return
        await safe_edit(q.message, f"{msg}\n\n<b>⏰ Reminder</b>", act_kb(row))

    elif data.startswith("snz_"):
        parts = data[4:].split("_")
        row, mins = int(parts[0]), int(parts[1])
        r = sheet.row_values(row)
        msg, ds, ts, rs = get_detail(r)
        if handled(r):
            await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\n<i>Already handled</i>")
            return
        kill_jobs(ctx.job_queue, row)
        await rm_btns(ctx, row)
        nt = datetime.now(IST) + timedelta(minutes=mins)
        rep = r[4] if len(r) > 4 else "none"
        if rep and rep != "none":
            sheet.update_cell(row, 6, "snoozed")
            sheet.update_cell(row, 7, 0)
            ctx.job_queue.run_once(snooze_fire, mins * 60,
                data={"row": row, "chat": q.from_user.id}, name=f"snooze-{row}")
        else:
            sheet.update_cell(row, 3, nt.strftime("%Y-%m-%d"))
            sheet.update_cell(row, 4, nt.strftime("%H:%M"))
            sheet.update_cell(row, 6, "active")
            sheet.update_cell(row, 7, 0)
        ctx.bot_data.pop(f"r_{row}", None)
        await safe_edit(q.message,
            f"{detail(msg, ds, ts, rs)}\n\n<b>Snoozed {fmt_snz(mins)}</b> → {fmt_time(nt.strftime('%H:%M'))}")

    # ---- DONE ----
    elif data.startswith("done_"):
        row = int(data[5:])
        r = sheet.row_values(row)
        msg, ds, ts, rs = get_detail(r)
        if handled(r):
            await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\n<i>Already handled</i>")
            return
        kill_jobs(ctx.job_queue, row)
        await rm_btns(ctx, row)
        if not advance_rep(row, r):
            sheet.update_cell(row, 6, "done")
            sheet.update_cell(row, 7, 0)
        ctx.bot_data.pop(f"r_{row}", None)
        await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\n<b>Done</b> ✅")

    # ---- EDIT ----
    elif data.startswith("edit_") and not data.startswith(("emsg_", "edate_", "etime_")):
        row = int(data[5:])
        ud.clear()
        r = sheet.row_values(row)
        msg, ds, ts, rs = get_detail(r)
        await safe_edit(q.message,
            f"{hdr('Edit Reminder')}\n{detail(msg, ds, ts, rs)}\n\nWhat to change?",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Message", callback_data=f"emsg_{row}"),
                 InlineKeyboardButton("Date", callback_data=f"edate_{row}"),
                 InlineKeyboardButton("Time", callback_data=f"etime_{row}")],
                [InlineKeyboardButton("« Back", callback_data=f"view_{row}")]]))

    elif data.startswith("emsg_"):
        row = int(data[5:])
        ud["editing_row"], ud["step"] = row, "edit_message"
        r = sheet.row_values(row)
        msg, ds, ts, rs = get_detail(r)
        await safe_edit(q.message,
            f"{hdr('Edit Reminder')}\nCurrent: <i>{msg}</i>\n"
            f"{fmt_date(ds)} · {fmt_time(ts)} · {rs}\n\nEnter new message:",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"edit_{row}")]]))
        save_p(ud, q.message)

    elif data.startswith("edate_"):
        row = int(data[6:])
        ud["editing_row"], ud["step"] = row, "edit_date"
        r = sheet.row_values(row)
        msg, ds, ts, rs = get_detail(r)
        now = datetime.now(IST)
        await safe_edit(q.message,
            f"{hdr('Edit Reminder')}\n{msg}\nCurrent: <i>{fmt_date(ds)} · {fmt_time(ts)}</i>\n\nPick new date:",
            cal_kb(now.year, now.month, f"edit_{row}", "« Back"))

    elif data.startswith("etime_"):
        row = int(data[6:])
        ud["editing_row"], ud["step"] = row, "edit_time"
        r = sheet.row_values(row)
        msg, ds, ts, rs = get_detail(r)
        await safe_edit(q.message,
            f"{hdr('Edit Reminder')}\n{msg}\nCurrent: <i>{fmt_date(ds)} · {fmt_time(ts)}</i>\n\n"
            f"Enter new time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"edit_{row}")]]))
        save_p(ud, q.message)

    # ---- CANCEL REMINDER ----
    elif data.startswith("crem_"):
        row = int(data[5:])
        kill_jobs(ctx.job_queue, row)
        r = sheet.row_values(row)
        msg, ds, ts, rs = get_detail(r)
        sheet.update_cell(row, 6, "cancelled")
        sheet.update_cell(row, 7, 0)
        await rm_btns(ctx, row)
        ctx.bot_data.pop(f"r_{row}", None)
        await safe_edit(q.message, f"{detail(msg, ds, ts)}\n\n<b>Cancelled</b> ✕", home_kb())
        save_home(ud, q.message)

    elif data == "list_refresh":
        ud.clear()
        await show_list(q.message, q.from_user.id, ud)

    # ======= SETTINGS CALLBACKS =======

    elif data == "cfg_digest_toggle":
        uid = q.from_user.id
        cfg = get_cfg(uid)
        new_val = not cfg["digest_on"]
        save_cfg(uid, "digest_on", str(new_val).lower())
        await show_settings(q.message, uid)

    elif data == "cfg_digest_time":
        ud.clear()
        uid = q.from_user.id
        cfg = get_cfg(uid)
        ud["step"] = "set_digest_time"
        await safe_edit(q.message,
            f"{hdr('Settings')}\nCurrent digest time: <b>{fmt_time(cfg['digest_time'])}</b>\n\n"
            f"Enter new time:\n<i>e.g. 7am, 8:30 AM, 06:00</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="cfg_back")]]))
        save_p(ud, q.message)

    elif data == "cfg_retries":
        uid = q.from_user.id
        cfg = get_cfg(uid)
        cur = cfg["max_retries"]
        btns = []
        row_btns = []
        for v in [1, 2, 3, 5, 7, 10]:
            lbl = f"[{v}]" if v == cur else str(v)
            row_btns.append(InlineKeyboardButton(lbl, callback_data=f"cfgr_{v}"))
            if len(row_btns) == 3:
                btns.append(row_btns)
                row_btns = []
        if row_btns:
            btns.append(row_btns)
        btns.append([InlineKeyboardButton("« Back", callback_data="cfg_back")])
        await safe_edit(q.message,
            f"{hdr('Settings')}\nMax retries: <b>{cur}×</b>\n\nHow many times to retry if missed?",
            InlineKeyboardMarkup(btns))

    elif data.startswith("cfgr_"):
        val = int(data[5:])
        uid = q.from_user.id
        save_cfg(uid, "max_retries", val)
        await show_settings(q.message, uid)

    elif data == "cfg_gap":
        uid = q.from_user.id
        cfg = get_cfg(uid)
        cur = cfg["retry_gap"]
        btns = []
        row_btns = []
        for v in [5, 10, 15, 20, 30, 60]:
            lbl = f"[{v}m]" if v == cur else f"{v}m"
            row_btns.append(InlineKeyboardButton(lbl, callback_data=f"cfgg_{v}"))
            if len(row_btns) == 3:
                btns.append(row_btns)
                row_btns = []
        if row_btns:
            btns.append(row_btns)
        btns.append([InlineKeyboardButton("« Back", callback_data="cfg_back")])
        await safe_edit(q.message,
            f"{hdr('Settings')}\nRetry gap: <b>{cur} min</b>\n\nTime between retries?",
            InlineKeyboardMarkup(btns))

    elif data.startswith("cfgg_"):
        val = int(data[5:])
        uid = q.from_user.id
        save_cfg(uid, "retry_gap", val)
        await show_settings(q.message, uid)

    elif data == "cfg_back":
        ud.clear()
        await show_settings(q.message, q.from_user.id)

    # ======= TEMPLATE CALLBACKS =======

    elif data == "tpl_add":
        ud.clear()
        ud["step"] = "tpl_emoji"
        emoji_btns = []
        row_btns = []
        for emoji, label in EMOJIS:
            row_btns.append(InlineKeyboardButton(f"{emoji}", callback_data=f"tple_{emoji}"))
            if len(row_btns) == 5:
                emoji_btns.append(row_btns)
                row_btns = []
        if row_btns:
            emoji_btns.append(row_btns)
        emoji_btns.append([InlineKeyboardButton("« Back", callback_data="tpl_back")])
        await safe_edit(q.message,
            f"{hdr('New Template')}\nPick a category:\n\n"
            + "  ".join(f"{e} {l}" for e, l in EMOJIS),
            InlineKeyboardMarkup(emoji_btns))

    elif data.startswith("tple_"):
        emoji = data[5:]
        ud["tpl_emoji"] = emoji
        ud["step"] = "tpl_message"
        await safe_edit(q.message,
            f"{hdr('New Template')}\n{emoji}\n\nEnter message:",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="tpl_add")]]))
        save_p(ud, q.message)

    elif data.startswith("tplv_"):
        tpl_row = int(data[5:])
        try:
            r = tpl_sheet.row_values(tpl_row)
        except Exception:
            await safe_edit(q.message, "Template not found.", home_kb())
            return
        emoji = r[1] if len(r) > 1 else "📝"
        msg = str(r[2]) if len(r) > 2 else ""
        ts = fmt_time(norm_time(r[3])) if len(r) > 3 else ""
        rep = fmt_rep(r[4]) if len(r) > 4 else ""
        await safe_edit(q.message,
            f"{hdr('Template')}\n{emoji} {msg}\n\n{ts} · {rep}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("▶ Activate", callback_data=f"tpla_{tpl_row}"),
                 InlineKeyboardButton("✕ Delete", callback_data=f"tpld_{tpl_row}")],
                [InlineKeyboardButton("« Back", callback_data="tpl_back")]]))

    elif data.startswith("tpla_"):
        tpl_row = int(data[5:])
        try:
            r = tpl_sheet.row_values(tpl_row)
        except Exception:
            await safe_edit(q.message, "Template not found.", home_kb())
            return
        emoji = r[1] if len(r) > 1 else "📝"
        msg = f"{emoji} {r[2]}" if len(r) > 2 else ""
        ts = norm_time(r[3]) if len(r) > 3 else ""
        rep = str(r[4]) if len(r) > 4 else "none"
        date = datetime.now(IST).strftime("%Y-%m-%d")

        if is_past(date, ts):
            date = (datetime.now(IST) + timedelta(days=1)).strftime("%Y-%m-%d")

        uid = q.from_user.id
        sheet.append_row([uid, msg, date, ts, rep, "active", 0], value_input_option="RAW")
        await safe_edit(q.message,
            f"{hdr('Saved ✓')}\n{detail(msg, date, ts, fmt_rep(rep))}", home_kb())
        save_home(ud, q.message)

    elif data.startswith("tpld_"):
        tpl_row = int(data[5:])
        try:
            r = tpl_sheet.row_values(tpl_row)
            emoji = r[1] if len(r) > 1 else "📝"
            msg = str(r[2]) if len(r) > 2 else ""
            tpl_sheet.delete_rows(tpl_row)
        except Exception:
            msg, emoji = "Template", "📝"
        await safe_edit(q.message,
            f"{emoji} {msg}\n\n<b>Deleted</b> ✕",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Templates", callback_data="tpl_back")]]))

    elif data == "tpl_back":
        ud.clear()
        await show_templates(q.message, q.from_user.id, ud)

    # ---- TEMPLATE REPEAT SAVE ----
    elif data.startswith("tplrep_"):
        rep = data[7:]
        emoji = ud.get("tpl_emoji", "📝")
        msg = ud.get("tpl_message", "")
        ts = ud.get("tpl_time", "")
        uid = q.from_user.id
        tpl_sheet.append_row([uid, emoji, msg, ts, rep], value_input_option="RAW")
        ud.clear()
        await safe_edit(q.message,
            f"{hdr('Template Saved ✓')}\n{emoji} {msg}\n{fmt_time(ts)} · {fmt_rep(rep)}",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Templates", callback_data="tpl_back")]]))


# ============= TEXT HANDLER ===============

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    step = ctx.user_data.get("step")
    text = update.message.text.strip()
    if step:
        await _do_step(update, ctx, step, text)
    else:
        await _try_nl(update, ctx, text)

async def _try_nl(update, ctx, text):
    result = parse_nl(text)
    if not result:
        return
    msg, time, date, rep = result['message'], result['time'], result['date'], result.get('repeat')
    if not msg:
        return
    ud = ctx.user_data
    await rm_home(ctx, ud)
    ud.clear()
    ud["message"], ud["time"] = msg, time
    if rep:
        ud["repeat"] = rep
    if not date:
        date = datetime.now(IST).strftime("%Y-%m-%d")
    if is_past(date, time):
        ud["step"] = "date"
        now = datetime.now(IST)
        sent = await update.message.reply_text(
            f"{hdr('New Reminder')}\n{msg}\n\n{past_msg(time)}\nPick a future date:",
            reply_markup=cal_kb(now.year, now.month), parse_mode="HTML")
        save_p(ud, sent)
    else:
        ud["date"] = date
        await finish_or_repeat(update.message, update.effective_user.id, ud, msg, date, time)

async def _do_step(update, ctx, step, text):
    ud = ctx.user_data

    if step == "message":
        await rm_prompt(ctx, ud)
        ud["message"], ud["step"] = text, "date"
        now = datetime.now(IST)
        sent = await update.message.reply_text(
            f"{hdr('New Reminder')}\n{text}\n\nPick a date:",
            reply_markup=cal_kb(now.year, now.month), parse_mode="HTML")
        save_p(ud, sent)

    elif step == "time":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text(
                "Invalid time. Try again:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML")
            return
        ds = ud.get("date", "")
        if is_past(ds, parsed):
            await update.message.reply_text(f"{past_msg(parsed)}\nEnter a future time:", parse_mode="HTML")
            return
        await del_prompt(ctx, ud)
        ud["time"] = parsed
        msg = ud.get("message", "")
        await finish_or_repeat(update.message, update.effective_user.id, ud, msg, ds, parsed)

    elif step == "edit_message":
        row = ud.get("editing_row")
        if not row: return
        await rm_prompt(ctx, ud)
        r = sheet.row_values(row)
        old, ds, ts, rs = get_detail(r)
        sheet.update_cell(row, 2, text)
        ud.clear()
        sent = await update.message.reply_text(
            f"{hdr('Updated ✓')}\nMessage: {old} → <b>{text}</b>\n"
            f"{fmt_date(ds)} · {fmt_time(ts)} · {rs}",
            reply_markup=home_kb(), parse_mode="HTML")
        save_home(ud, sent)

    elif step == "edit_time":
        row = ud.get("editing_row")
        if not row: return
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text(
                "Invalid time. Try again:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML")
            return
        r = sheet.row_values(row)
        msg, ds, old_t, rs = get_detail(r)
        if is_past(ds, parsed):
            await update.message.reply_text(f"{past_msg(parsed)}\nEnter a future time:", parse_mode="HTML")
            return
        await del_prompt(ctx, ud)
        sheet.update_cell(row, 4, parsed)
        ud.clear()
        sent = await update.message.reply_text(
            f"{hdr('Updated ✓')}\n{msg}\nDate: {fmt_date(ds)}\n"
            f"Time: {fmt_time(old_t)} → <b>{fmt_time(parsed)}</b> · {rs}",
            reply_markup=home_kb(), parse_mode="HTML")
        save_home(ud, sent)

    elif step == "set_digest_time":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text(
                "Invalid time. Try again:\n<i>e.g. 7am, 8:30 AM, 06:00</i>", parse_mode="HTML")
            return
        await del_prompt(ctx, ud)
        uid = update.effective_user.id
        save_cfg(uid, "digest_time", parsed)
        ud.clear()
        sent = await update.message.reply_text(
            f"{hdr('Settings')}\nDigest time updated → <b>{fmt_time(parsed)}</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Settings", callback_data="cfg_back")]]),
            parse_mode="HTML")

    # ---- TEMPLATE STEPS ----
    elif step == "tpl_message":
        await rm_prompt(ctx, ud)
        ud["tpl_message"] = text
        ud["step"] = "tpl_time"
        sent = await update.message.reply_text(
            f"{hdr('New Template')}\n{ud.get('tpl_emoji', '📝')} {text}\n\n"
            f"Enter default time:\n<i>e.g. 9pm, 9:30 AM, 21:30</i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="tpl_add")]]),
            parse_mode="HTML")
        save_p(ud, sent)

    elif step == "tpl_time":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text(
                "Invalid time. Try again:\n<i>e.g. 9pm, 9:30 AM, 21:30</i>", parse_mode="HTML")
            return
        await del_prompt(ctx, ud)
        ud["tpl_time"] = parsed
        ud["step"] = "tpl_repeat"
        emoji = ud.get("tpl_emoji", "📝")
        msg = ud.get("tpl_message", "")
        sent = await update.message.reply_text(
            f"{hdr('New Template')}\n{emoji} {msg}\n{fmt_time(parsed)}\n\nRepeat?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Once", callback_data="tplrep_none"),
                 InlineKeyboardButton("Daily", callback_data="tplrep_daily")],
                [InlineKeyboardButton("Weekly", callback_data="tplrep_weekly"),
                 InlineKeyboardButton("Monthly", callback_data="tplrep_monthly")],
                [InlineKeyboardButton("✕ Cancel", callback_data="tpl_back")]]),
            parse_mode="HTML")
        save_p(ud, sent)


# ============= SNOOZE FIRE ================

async def snooze_fire(ctx: ContextTypes.DEFAULT_TYPE):
    row, chat = ctx.job.data["row"], ctx.job.data["chat"]
    try:
        r = sheet.row_values(row)
    except Exception as e:
        logger.error(f"snooze_fire row {row}: {e}")
        return
    if not r or len(r) <= 5 or r[5] != "snoozed":
        return
    msg = str(r[1]).strip()
    await rm_btns(ctx, row)
    try:
        sent = await ctx.bot.send_message(chat_id=chat,
            text=f"{msg}\n\n<b>⏰ Reminder</b>",
            reply_markup=act_kb(row), parse_mode="HTML")
        save_rm(ctx, row, chat, sent.message_id)
    except Exception as e:
        logger.error(f"snooze_fire send {chat}: {e}")
        return
    sheet.update_cell(row, 6, "pending")
    sheet.update_cell(row, 7, 0)

    # Use user settings for retry
    try:
        uid = int(r[0])
    except (ValueError, TypeError):
        uid = r[0]
    cfg = get_cfg(uid)
    ctx.job_queue.run_once(auto_retry, cfg["retry_gap"] * 60,
        data={"row": row, "chat": chat}, name=f"retry-{row}")


# ============= AUTO RETRY ================

async def auto_retry(ctx: ContextTypes.DEFAULT_TYPE):
    row, chat = ctx.job.data["row"], ctx.job.data["chat"]
    try:
        r = sheet.row_values(row)
    except Exception as e:
        logger.error(f"retry row {row}: {e}")
        return
    if not r or len(r) <= 5 or r[5] != "pending":
        return

    # Get user settings
    try:
        uid = int(r[0])
    except (ValueError, TypeError):
        uid = r[0]
    cfg = get_cfg(uid)
    max_retries = cfg["max_retries"]
    retry_gap = cfg["retry_gap"]

    try:
        count = int(r[6])
    except (IndexError, ValueError):
        count = 0

    if count >= max_retries:
        if not advance_rep(row, r):
            sheet.update_cell(row, 6, "missed")
            sheet.update_cell(row, 7, 0)
        return

    await rm_btns(ctx, row)
    nc = count + 1
    sent = await ctx.bot.send_message(chat_id=chat,
        text=f"{str(r[1]).strip()}\n\n<b>Reminder</b> ({nc}/{max_retries})",
        reply_markup=act_kb(row), parse_mode="HTML")
    save_rm(ctx, row, chat, sent.message_id)
    sheet.update_cell(row, 7, nc)

    if nc >= max_retries:
        if not advance_rep(row, r):
            sheet.update_cell(row, 6, "missed")
            sheet.update_cell(row, 7, 0)
    else:
        ctx.job_queue.run_once(auto_retry, retry_gap * 60,
            data={"row": row, "chat": chat}, name=f"retry-{row}")


# ============= DAILY DIGEST ==============

async def check_digest(ctx: ContextTypes.DEFAULT_TYPE):
    """Runs every minute, checks if any user's digest time matches now."""
    now = datetime.now(IST)
    nt = now.strftime("%H:%M")

    try:
        cfg_rows = cfg_sheet.get_all_values()
    except Exception:
        try:
            client.login()
            cfg_rows = cfg_sheet.get_all_values()
        except Exception:
            return

    for r in cfg_rows[1:]:
        if len(r) < 3:
            continue
        if str(r[1]).lower() != "true":
            continue
        if norm_time(r[2]) != nt:
            continue

        uid = r[0]
        try:
            uid_int = int(uid)
        except (ValueError, TypeError):
            continue

        # Get today's reminders
        try:
            rem_rows = sheet.get_all_values()
        except Exception:
            continue

        today = now.strftime("%Y-%m-%d")
        items = []
        for v in rem_rows[1:]:
            if len(v) < 6:
                continue
            if str(v[0]) != str(uid):
                continue
            if str(v[5]).strip().lower() not in ("active", "snoozed"):
                continue
            if norm_date(str(v[2]).strip()) != today:
                continue
            items.append(v)

        # Sort by time
        items.sort(key=lambda x: norm_time(str(x[3]).strip()))

        if items:
            lines = [f"☀️ <b>Good morning!</b>\n{DIV}\n\nToday — {now.strftime('%-d %b')}\n"]
            for v in items:
                msg = str(v[1]).strip()
                ts = fmt_time(norm_time(str(v[3]).strip()))
                short = msg[:30] + "…" if len(msg) > 30 else msg
                lines.append(f"  {ts} · {short}")
            lines.append(f"\n{len(items)} reminder{'s' if len(items) != 1 else ''} today")
        else:
            lines = [f"☀️ <b>Good morning!</b>\n{DIV}\n\nToday — {now.strftime('%-d %b')}\n",
                     "No reminders today. Enjoy your day!"]

        txt = "\n".join(lines)
        try:
            await ctx.bot.send_message(chat_id=uid_int, text=txt,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("＋ New", callback_data="add")]]),
                parse_mode="HTML")
        except Exception as e:
            logger.error(f"[DIGEST] Send {uid}: {e}")


# ============= SCHEDULER =================

async def check_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(IST)
    nd, nt = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    logger.info(f"[CRON] {nd} {nt}")

    try:
        vals = sheet.get_all_values()
    except Exception:
        try:
            client.login()
            vals = sheet.get_all_values()
        except Exception as e:
            logger.error(f"[CRON] {e}")
            return

    for idx, v in enumerate(vals[1:], 2):
        if len(v) < 7 or str(v[5]).strip().lower() != "active":
            continue
        if norm_date(str(v[2]).strip()) != nd or norm_time(str(v[3]).strip()) != nt:
            continue

        uid = v[0]
        try:
            uid = int(uid)
        except (ValueError, TypeError):
            pass

        msg = str(v[1]).strip()
        logger.info(f"[CRON] FIRE row {idx}: '{msg[:30]}' → {uid}")
        kill_jobs(ctx.job_queue, idx)
        await rm_btns(ctx, idx)

        try:
            sent = await ctx.bot.send_message(chat_id=uid,
                text=f"{msg}\n\n<b>⏰ Reminder</b>",
                reply_markup=act_kb(idx), parse_mode="HTML")
            save_rm(ctx, idx, uid, sent.message_id)
        except Exception as e:
            logger.error(f"[CRON] Send {uid}: {e}")
            continue

        sheet.update_cell(idx, 6, "pending")
        sheet.update_cell(idx, 7, 0)

        # Use user settings for retry gap
        cfg = get_cfg(uid)
        ctx.job_queue.run_once(auto_retry, cfg["retry_gap"] * 60,
            data={"row": idx, "chat": uid}, name=f"retry-{idx}")


# ============= MAIN ======================

def main():
    app = Application.builder().token(TOKEN).job_queue(JobQueue()).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("templates", templates_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(check_reminders, interval=60, first=0)
    app.job_queue.run_repeating(check_digest, interval=60, first=10)
    print("Smart Reminder Bot Running")
    app.run_polling()


if __name__ == "__main__":
    main()
