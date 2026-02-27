import logging
import os
import json
import re
import calendar as cal_module
import time as time_module
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

DIV = "━━━━━━━━━━━━━━━━━━━━"

DEF_TZ = "Asia/Kolkata"
DEF_RETRIES = 3
DEF_RETRY_GAP = 10
DEF_DIGEST_TIME = "07:00"

TZ_DATA = [
    ("Asia/Kolkata", "India", "+5:30", "Asia"),
    ("Asia/Dubai", "UAE", "+4", "Asia"),
    ("Asia/Karachi", "Pakistan", "+5", "Asia"),
    ("Asia/Dhaka", "Bangladesh", "+6", "Asia"),
    ("Asia/Bangkok", "Thailand", "+7", "Asia"),
    ("Asia/Singapore", "Singapore", "+8", "Asia"),
    ("Asia/Shanghai", "China", "+8", "Asia"),
    ("Asia/Tokyo", "Japan", "+9", "Asia"),
    ("Asia/Seoul", "Korea", "+9", "Asia"),
    ("Asia/Jakarta", "Indonesia", "+7", "Asia"),
    ("Asia/Riyadh", "Saudi Arabia", "+3", "Asia"),
    ("Asia/Manila", "Philippines", "+8", "Asia"),
    ("Europe/London", "UK", "0/+1", "Europe"),
    ("Europe/Berlin", "Germany", "+1/+2", "Europe"),
    ("Europe/Paris", "France", "+1/+2", "Europe"),
    ("Europe/Moscow", "Russia", "+3", "Europe"),
    ("Europe/Istanbul", "Turkey", "+3", "Europe"),
    ("America/New_York", "US East", "-5/-4", "Americas"),
    ("America/Chicago", "US Central", "-6/-5", "Americas"),
    ("America/Denver", "US Mountain", "-7/-6", "Americas"),
    ("America/Los_Angeles", "US West", "-8/-7", "Americas"),
    ("America/Sao_Paulo", "Brazil", "-3", "Americas"),
    ("America/Mexico_City", "Mexico", "-6/-5", "Americas"),
    ("Australia/Sydney", "Australia", "+10/+11", "Oceania"),
    ("Pacific/Auckland", "New Zealand", "+12/+13", "Oceania"),
    ("Africa/Lagos", "Nigeria", "+1", "Africa"),
    ("Africa/Cairo", "Egypt", "+2", "Africa"),
    ("Africa/Nairobi", "Kenya", "+3", "Africa"),
    ("Africa/Johannesburg", "S. Africa", "+2", "Africa"),
]

TZ_REGIONS = list(dict.fromkeys(t[3] for t in TZ_DATA))
TZ_ICONS = {"Asia": "🌏", "Europe": "🌍", "Americas": "🌎", "Oceania": "🌏", "Africa": "🌍"}

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

def get_or_create_sheet(name, headers):
    try:
        ws = workbook.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = workbook.add_worksheet(title=name, rows=100, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
    return ws

sheet = get_or_create_sheet("Reminders", ["user_id", "message", "date", "time", "repeat", "status", "retry_count", "group_id", "task_id"])
cfg_sheet = get_or_create_sheet("Settings", ["user_id", "digest_on", "digest_time", "max_retries", "retry_gap", "timezone"])
grp_sheet = get_or_create_sheet("GroupMembers", ["group_id", "user_id", "first_name", "subscribed"])
task_sheet = get_or_create_sheet("TaskMembers", ["task_id", "user_id", "first_name", "status"])

# ============= FORMATTERS ================

def hdr(title): return f"<b>{title}</b>\n{DIV}"

def detail(msg, ds, ts, rs=None):
    p = [fmt_date(ds), fmt_time(ts)]
    if rs: p.append(rs)
    return f"{msg}\n{' · '.join(p)}"

def fmt_date(ds):
    try: return datetime.strptime(norm_date(ds), "%Y-%m-%d").strftime("%-d %b")
    except Exception: return str(ds)

def fmt_time(ts):
    try:
        h, m = map(int, norm_time(ts).split(":"))
        return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
    except Exception: return str(ts)

REP_MAP = {"none": "Once", "daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
STATUS_ICON = {"active": "○", "pending": "●", "missed": "✗", "snoozed": "◷"}
STATUS_LABEL = {"active": "Active", "pending": "Pending", "missed": "Missed", "snoozed": "Snoozed"}

def fmt_rep(r): return REP_MAP.get(str(r), str(r))
def fmt_snz(m): return f"{m} min" if m < 60 else f"{m // 60} hr{'s' if m >= 120 else ''}"
def s_icon(s): return STATUS_ICON.get(str(s), "?")
def s_label(s): return STATUS_LABEL.get(str(s), str(s))

def tz_label(tz_name):
    for tz, country, _, _ in TZ_DATA:
        if tz == tz_name: return country
    return tz_name.split("/")[-1].replace("_", " ")

def tz_short(tz_name):
    for tz, country, offset, _ in TZ_DATA:
        if tz == tz_name: return f"{country} ({offset})"
    return tz_name.split("/")[-1].replace("_", " ")

# ============= NORMALIZERS ================

def norm_date(val):
    s = str(val).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-": return s
    for f in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try: return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError: pass
    try:
        n = float(s)
        if 1 < n < 100000: return (datetime(1899, 12, 30) + timedelta(days=int(n))).strftime("%Y-%m-%d")
    except (ValueError, OverflowError): pass
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
        except (ValueError, IndexError): return s
    try:
        t = round(float(s) * 24 * 60)
        return f"{t // 60:02d}:{t % 60:02d}"
    except ValueError: return s

# ============= TIMEZONE HELPERS ===========

def get_tz(uid):
    try: return pytz.timezone(get_cfg(uid).get("timezone", DEF_TZ))
    except Exception: return pytz.timezone(DEF_TZ)

def safe_tz(name):
    try: return pytz.timezone(name)
    except Exception: return pytz.timezone(DEF_TZ)

# ============= SETTINGS HELPERS ===========

def get_cfg(uid):
    uid_s = str(uid)
    try: rows = cfg_sheet.get_all_values()
    except Exception:
        try: client.login(); rows = cfg_sheet.get_all_values()
        except Exception:
            return {"digest_on": True, "digest_time": DEF_DIGEST_TIME, "max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP, "timezone": DEF_TZ}
    for r in rows[1:]:
        if str(r[0]) == uid_s:
            return {
                "digest_on": str(r[1]).lower() != "false",
                "digest_time": norm_time(r[2]) if r[2] else DEF_DIGEST_TIME,
                "max_retries": int(r[3]) if r[3] else DEF_RETRIES,
                "retry_gap": int(r[4]) if r[4] else DEF_RETRY_GAP,
                "timezone": str(r[5]) if len(r) > 5 and r[5] else DEF_TZ,
            }
    cfg_sheet.append_row([uid_s, "true", DEF_DIGEST_TIME, DEF_RETRIES, DEF_RETRY_GAP, DEF_TZ], value_input_option="RAW")
    return {"digest_on": True, "digest_time": DEF_DIGEST_TIME, "max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP, "timezone": DEF_TZ}

def save_cfg(uid, field, value):
    uid_s, col_map = str(uid), {"digest_on": 2, "digest_time": 3, "max_retries": 4, "retry_gap": 5, "timezone": 6}
    if field not in col_map: return
    try: rows = cfg_sheet.get_all_values()
    except Exception: return
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == uid_s:
            cfg_sheet.update_cell(i, col_map[field], str(value)); return
    get_cfg(uid)
    try: rows = cfg_sheet.get_all_values()
    except Exception: return
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == uid_s:
            cfg_sheet.update_cell(i, col_map[field], str(value)); return

# ============= CORE HELPERS ===============

def get_detail(r):
    return (str(r[1]).strip() if len(r) > 1 else "",
            norm_date(r[2]) if len(r) > 2 else "",
            norm_time(r[3]) if len(r) > 3 else "",
            fmt_rep(r[4]) if len(r) > 4 else "")

def row_detail(row):
    r = sheet.row_values(row)
    return (r, *get_detail(r))

def handled(r): return len(r) > 5 and r[5] != "pending"

async def guard(q, r):
    if handled(r):
        msg, ds, ts, rs = get_detail(r)
        await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\n<i>Already handled</i>")
        return True
    return False

def is_past(ds, ts, tz=None):
    now = datetime.now(tz or safe_tz(DEF_TZ))
    try:
        d = datetime.strptime(norm_date(ds), "%Y-%m-%d").date()
        if d != now.date(): return d < now.date()
        h, m = map(int, norm_time(ts).split(":"))
        return now > now.replace(hour=h, minute=m, second=0, microsecond=0)
    except Exception: return False

def past_msg(ts): return f"⚠ {fmt_time(ts)} has already passed today."

def advance_rep(row, r):
    rep = r[4] if len(r) > 4 else "none"
    if not rep or rep == "none": return False
    d = datetime.strptime(norm_date(r[2]), "%Y-%m-%d")
    if rep == "daily": nd = d + timedelta(days=1)
    elif rep == "weekly": nd = d + timedelta(days=7)
    elif rep == "monthly":
        mo, yr = d.month + 1, d.year
        if mo > 12: mo, yr = 1, yr + 1
        nd = d.replace(year=yr, month=mo)
    else: return False
    sheet.update_cell(row, 3, nd.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, "active")
    sheet.update_cell(row, 7, 0)
    return True

def kill_jobs(jq, row):
    for n in (f"retry-{row}", f"snooze-{row}"):
        for j in jq.get_jobs_by_name(n): j.schedule_removal()

def do_save(uid, ud, msg, date, time, rep, gid="", tid=""):
    sheet.append_row([uid, msg, date, time, rep, "active", 0, gid, tid], value_input_option="RAW")
    ud.clear()

# ============= GROUP HELPERS ==============

def gen_tid(): return f"t{int(time_module.time())}"

def get_gsubs(gid):
    """Get subscribed members of a group."""
    try: rows = grp_sheet.get_all_values()
    except Exception: return []
    return [(r[1], r[2]) for r in rows[1:] if str(r[0]) == str(gid) and str(r[3]).lower() == "true"]

def set_gsub(gid, uid, name, sub=True):
    """Subscribe/unsubscribe a user to a group."""
    gid_s, uid_s = str(gid), str(uid)
    try: rows = grp_sheet.get_all_values()
    except Exception: rows = [["h"]]
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == gid_s and str(r[1]) == uid_s:
            grp_sheet.update_cell(i, 3, name)
            grp_sheet.update_cell(i, 4, str(sub).lower()); return
    grp_sheet.append_row([gid_s, uid_s, name, str(sub).lower()], value_input_option="RAW")

def get_tmembers(tid):
    """Get all task members: [(uid, name, status), ...]"""
    try: rows = task_sheet.get_all_values()
    except Exception: return []
    return [(r[1], r[2], r[3]) for r in rows[1:] if str(r[0]) == str(tid)]

def add_tmember(tid, uid, name, st="waiting"):
    """Add a member to a task. Returns False if already exists."""
    uid_s = str(uid)
    try: rows = task_sheet.get_all_values()
    except Exception: rows = [["h"]]
    for r in rows[1:]:
        if str(r[0]) == str(tid) and str(r[1]) == uid_s: return False
    task_sheet.append_row([str(tid), uid_s, name, st], value_input_option="RAW")
    return True

def set_tstatus(tid, uid, st):
    """Update a task member's status."""
    uid_s = str(uid)
    try: rows = task_sheet.get_all_values()
    except Exception: return
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == str(tid) and str(r[1]) == uid_s:
            task_sheet.update_cell(i, 4, st); return

def find_by_tid(tid):
    """Find reminder row by task_id. Returns (row_index, row_values) or (None, None)."""
    try: rows = sheet.get_all_values()
    except Exception: return None, None
    for i, r in enumerate(rows[1:], 2):
        if len(r) > 8 and str(r[8]) == str(tid): return i, r
    return None, None

def gstatus_text(tid, msg):
    """Build group status message text."""
    ms = get_tmembers(tid)
    active = [(u, n, s) for u, n, s in ms if s != "skipped"]
    if not active: return f"⏰ {msg}\n\nNo subscribers"
    ic = {"waiting": "⏳", "pending": "⏳", "done": "✅", "snoozed": "◷", "missed": "✗"}
    if all(s in ("done", "missed") for _, _, s in active):
        if all(s == "done" for _, _, s in active):
            return f"{msg}\n\n✅ All done · {', '.join(n for _, n, _ in active)}"
        parts = [f"{ic[s]} {n}" for _, n, s in active]
        return f"{msg}\n\n{' · '.join(parts)}"
    parts = [f"{ic.get(s, '⏳')} {n}" for _, n, s in active]
    return f"⏰ {msg}\n\n{' · '.join(parts)}"

async def update_gstatus(ctx, tid, msg):
    """Edit the group status message in-place."""
    info = ctx.bot_data.get(f"gs_{tid}")
    if not info: return
    try: await ctx.bot.edit_message_text(chat_id=info["c"], message_id=info["m"], text=gstatus_text(tid, msg), parse_mode="HTML")
    except Exception: pass

def advance_rep_grp(row, r, tid):
    """Advance repeat for group reminder and reset task members."""
    if not advance_rep(row, r): return False
    try: rows = task_sheet.get_all_values()
    except Exception: return True
    for i, tr in enumerate(rows[1:], 2):
        if str(tr[0]) == str(tid) and str(tr[3]) != "skipped":
            task_sheet.update_cell(i, 4, "waiting")
    return True

async def check_all_resolved(ctx, tid, row, r):
    """Check if all group members are done/missed. If so, advance or mark done."""
    members = get_tmembers(tid)
    active = [(u, n, s) for u, n, s in members if s != "skipped"]
    if not active: return
    if not all(s in ("done", "missed") for _, _, s in active): return
    for j in ctx.job_queue.get_jobs_by_name(f"gretry-{tid}"): j.schedule_removal()
    if not advance_rep_grp(row, r, tid):
        sheet.update_cell(row, 6, "done")
    sheet.update_cell(row, 7, 0)

def gsub_text(tid):
    """Build subscriber count text for setup message."""
    members = get_tmembers(tid)
    active = [(u, n) for u, n, s in members if s != "skipped"]
    if not active: return "0 subscribed"
    return f"{len(active)} subscribed: {', '.join(n for _, n in active)}"

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

def save_p(ud, msg): ud["p_mid"], ud["p_cid"] = msg.message_id, msg.chat.id

async def rm_btns(ctx, row):
    prev = ctx.bot_data.pop(f"r_{row}", None)
    if prev:
        try: await ctx.bot.edit_message_reply_markup(chat_id=prev["c"], message_id=prev["m"], reply_markup=None)
        except Exception: pass

def save_rm(ctx, row, cid, mid): ctx.bot_data[f"r_{row}"] = {"c": cid, "m": mid}

async def rm_home(ctx, ud):
    mid, cid = ud.pop("h_mid", None), ud.pop("h_cid", None)
    if mid and cid:
        try: await ctx.bot.edit_message_reply_markup(chat_id=cid, message_id=mid, reply_markup=None)
        except Exception: pass

def save_home(ud, msg): ud["h_mid"], ud["h_cid"] = msg.message_id, msg.chat.id

async def rm_gpm(ctx, tid, uid_s):
    """Remove old private group reminder buttons for a member."""
    old = ctx.bot_data.pop(f"gpm_{tid}_{uid_s}", None)
    if old:
        try: await ctx.bot.edit_message_reply_markup(chat_id=old["c"], message_id=old["m"], reply_markup=None)
        except Exception: pass

# ============= UI ========================

HOME_TEXT = (f"{hdr('Smart Reminder Bot')}\nManage your reminders easily.\n\n"
             "Use <b>＋ New</b> or /add to create.\n"
             "Or just type naturally:\n<i>Buy milk tomorrow at 5pm</i>\n\n"
             "Use /list to view all.")

def home_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("＋ New", callback_data="add")]])
def cancel_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="cancel")]])

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

def gact_kb(tid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Snooze", callback_data=f"gsnzp_{tid}"),
        InlineKeyboardButton("Done", callback_data=f"gdone_{tid}")]])

def snz_kb(key, pfx="snz"):
    opts = [(15,"15m"),(30,"30m"),(45,"45m"),(60,"1h"),(120,"2h"),(180,"3h"),(300,"5h"),(480,"8h"),(720,"12h")]
    rs = [opts[i:i+3] for i in range(0, len(opts), 3)]
    kb = [[InlineKeyboardButton(lbl, callback_data=f"{pfx}_{key}_{m}") for m, lbl in r] for r in rs]
    kb.append([InlineKeyboardButton("« Back", callback_data=f"{pfx}b_{key}")])
    return InlineKeyboardMarkup(kb)

def gjoin_kb(tid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("＋ Count Me In", callback_data=f"gjoin_{tid}"),
        InlineKeyboardButton("✕ Skip", callback_data=f"gskip_{tid}")]])

def cfg_picker_kb(values, fmt_fn, cur, cb_prefix, back_cb="cfg_back"):
    btns, row = [], []
    for v in values:
        lbl = f"[{fmt_fn(v)}]" if v == cur else fmt_fn(v)
        row.append(InlineKeyboardButton(lbl, callback_data=f"{cb_prefix}{v}"))
        if len(row) == 3: btns.append(row); row = []
    if row: btns.append(row)
    btns.append([InlineKeyboardButton("« Back", callback_data=back_cb)])
    return InlineKeyboardMarkup(btns)

# ============= CALENDAR ==================

def cal_kb(year, month, back_cb="cancel", back_txt="✕ Cancel", tz=None):
    now = datetime.now(tz or safe_tz(DEF_TZ))
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
    for pat, mode in [
        (r'^(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)$', 'hma'),
        (r'^(\d{1,2})\s*(am|pm)$', 'ha'),
        (r'^(\d{1,2})[:.]\s*(\d{1,2})$', '24'),
    ]:
        m = re.match(pat, s, re.I)
        if not m: continue
        if mode == 'hma':
            h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        elif mode == 'ha':
            h, mi, ap = int(m.group(1)), 0, m.group(2).lower()
        else:
            h, mi, ap = int(m.group(1)), int(m.group(2)), None
        if ap:
            if ap == 'pm' and h != 12: h += 12
            elif ap == 'am' and h == 12: h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
    return None

# ============= NATURAL LANGUAGE ===========

def _to24(h, mi, ap):
    if ap.lower() == 'pm' and h != 12: h += 12
    elif ap.lower() == 'am' and h == 12: h = 0
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
        if not m: continue
        if mode == 'hma': t = _to24(int(m.group(1)), int(m.group(2)), m.group(3))
        elif mode == 'ha': t = _to24(int(m.group(1)), 0, m.group(2))
        else:
            h, mi = int(m.group(1)), int(m.group(2))
            t = f"{h:02d}:{mi:02d}" if 0 <= h <= 23 and 0 <= mi <= 59 else None
        if t: return t, m.start(), m.end()
    return None

def _find_date(text, tz=None):
    now = datetime.now(tz or safe_tz(DEF_TZ))
    low = text.lower()
    for pat, delta in [(r'\bday\s+after\s+tomorrow\b', 2), (r'\b(today|tonight)\b', 0),
                        (r'\b(tomorrow|tmrw|tmr)\b', 1), (r'\bnext\s+week\b', 7)]:
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
    for pat, val in [(r'\b(?:every\s*day|daily)\b', 'daily'), (r'\b(?:every\s*week|weekly)\b', 'weekly'),
                      (r'\b(?:every\s*month|monthly)\b', 'monthly'), (r'\b(?:once|one[\s-]?time|no\s*repeat)\b', 'none')]:
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

def parse_nl(text, tz=None):
    tr, dr, rr = _find_time(text), _find_date(text, tz), _find_repeat(text)
    ts = tr[0] if tr else None
    ds = dr[0] if dr else None
    rep = rr[0] if rr else None
    msg = _clean(text, [(tr[1],tr[2]) if tr else None, (dr[1],dr[2]) if dr else None, (rr[1],rr[2]) if rr else None])
    if not msg or not ts: return None
    return {'message': msg, 'date': ds, 'time': ts, 'repeat': rep}

# ============= POST INIT =================

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("add", "New reminder"), BotCommand("list", "All reminders"),
        BotCommand("remind", "Group reminder"),
        BotCommand("settings", "Bot settings"), BotCommand("info", "About this bot")])

# ============= COMMANDS ===================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    await rm_home(ctx, ctx.user_data); ctx.user_data.clear()
    get_cfg(update.effective_user.id)
    sent = await update.message.reply_text(HOME_TEXT, reply_markup=home_kb(), parse_mode="HTML")
    save_home(ctx.user_data, sent)

async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /add in private chat.\nUse /remind here for group reminders."); return
    await rm_home(ctx, ctx.user_data); ctx.user_data.clear()
    ctx.user_data["step"] = "message"
    sent = await update.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
    save_p(ctx.user_data, sent)

async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /list in private chat."); return
    await rm_home(ctx, ctx.user_data); ctx.user_data.clear()
    await show_list(update.message, update.effective_user.id, ctx.user_data, new=True)

async def info_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = get_cfg(update.effective_user.id)
    await update.message.reply_text(
        f"{hdr('Smart Reminder Bot')}\n\n"
        "Set reminders and get notified on time.\n\n"
        "<b>Features</b>\n• One-time & recurring reminders\n• Calendar date picker\n"
        "• Flexible time input\n• Snooze (15m to 12h)\n• Auto-retry if missed\n"
        "• Edit or cancel anytime\n• Daily morning digest\n• Customisable settings\n"
        f"• Per-user timezone ({tz_short(cfg['timezone'])})\n\n"
        "<b>Group Reminders</b>\n• Use /remind in groups\n• Members opt in per reminder\n"
        "• Track who's done / pending / missed\n• Auto-subscribe for future reminders\n\n"
        "<b>Smart Input</b>\nJust type naturally:\n"
        "<code>Buy milk tomorrow at 5pm</code>\n<code>Call mom at 3:30pm</code>\n"
        "<code>Meeting on Monday at 10am weekly</code>\n\n"
        "<b>Commands</b>\n/add — New reminder (private)\n/list — All reminders (private)\n"
        "/remind — Group reminder\n/settings — Bot settings\n/info — This page\n\n"
        "<b>Time Formats</b>\n<code>9pm</code>  <code>9:30 PM</code>  <code>21:30</code>  <code>7:05pm</code>",
        parse_mode="HTML")

# ============= GROUP REMIND COMMAND =======

async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Use /remind in groups.\nUse /add for personal reminders.", parse_mode="HTML"); return

    text = re.sub(r'^/remind(@\w+)?\s*', '', update.message.text.strip(), flags=re.I).strip()
    if not text:
        await update.message.reply_text(
            f"{hdr('Group Reminder')}\n\nUsage:\n"
            "<code>/remind Buy milk at 5pm</code>\n"
            "<code>/remind Meeting tomorrow at 10am</code>\n"
            "<code>/remind Standup at 9am daily</code>",
            parse_mode="HTML"); return

    uid = update.effective_user.id
    utz = get_tz(uid)
    result = parse_nl(text, tz=utz)

    if not result or not result.get('time'):
        await update.message.reply_text(
            "⚠ Include a time.\n<code>/remind Buy milk at 5pm</code>", parse_mode="HTML"); return

    msg, ts = result['message'], result['time']
    ds = result.get('date') or datetime.now(utz).strftime("%Y-%m-%d")
    rep = result.get('repeat') or 'none'

    if is_past(ds, ts, utz):
        ds = (datetime.now(utz) + timedelta(days=1)).strftime("%Y-%m-%d")

    gid = str(update.effective_chat.id)
    tid = gen_tid()
    name = update.effective_user.first_name or "User"

    # Save reminder
    sheet.append_row([uid, msg, ds, ts, rep, "active", 0, gid, tid], value_input_option="RAW")

    # Auto-add subscribed group members
    subs = get_gsubs(gid)
    for sub_uid, sub_name in subs:
        add_tmember(tid, sub_uid, sub_name)

    txt = (f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, fmt_rep(rep))}\n"
           f"By {name}\n\n{gsub_text(tid)}")

    sent = await update.message.reply_text(txt, reply_markup=gjoin_kb(tid), parse_mode="HTML")
    ctx.bot_data[f"gm_{tid}"] = {"c": gid, "m": sent.message_id}

# ============= SETTINGS ===================

async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    await rm_home(ctx, ctx.user_data); ctx.user_data.clear()
    await show_settings(update.message, update.effective_user.id, new=True)

async def show_settings(target, uid, new=False):
    cfg = get_cfg(uid)
    d_on, d_time = cfg["digest_on"], fmt_time(cfg["digest_time"]) if cfg["digest_on"] else "—"
    tz_disp = tz_label(cfg.get("timezone", DEF_TZ))
    txt = (f"{hdr('Settings')}\n\n<b>Daily Digest</b>: {'ON' if d_on else 'OFF'}"
           + (f" · {d_time}" if d_on else "") +
           f"\n<b>Max Retries</b>: {cfg['max_retries']}×"
           f"\n<b>Retry Gap</b>: {cfg['retry_gap']} min"
           f"\n<b>Timezone</b>: {tz_disp}")
    btns = [
        [InlineKeyboardButton(f"Digest: {'ON' if d_on else 'OFF'}", callback_data="cfg_digest_toggle"),
         InlineKeyboardButton(f"⏰ {d_time}" if d_on else "—", callback_data="cfg_digest_time" if d_on else "noop")],
        [InlineKeyboardButton(f"Retries: {cfg['max_retries']}×", callback_data="cfg_retries"),
         InlineKeyboardButton(f"Gap: {cfg['retry_gap']}m", callback_data="cfg_gap")],
        [InlineKeyboardButton(f"🌍 {tz_disp}", callback_data="cfg_tz")],
        [InlineKeyboardButton("« Back", callback_data="home")]]
    if new: await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
    else: await safe_edit(target, txt, InlineKeyboardMarkup(btns))

# ============= TIMEZONE PICKER ============

def tz_region_kb():
    btns, row = [], []
    for region in TZ_REGIONS:
        row.append(InlineKeyboardButton(f"{TZ_ICONS.get(region, '🌐')} {region}", callback_data=f"tzr_{region}"))
        if len(row) == 2: btns.append(row); row = []
    if row: btns.append(row)
    btns.append([InlineKeyboardButton("« Back", callback_data="cfg_back")])
    return InlineKeyboardMarkup(btns)

def tz_list_kb(region, cur_tz):
    btns, row = [], []
    for idx, (tz, country, offset, reg) in enumerate(TZ_DATA):
        if reg != region: continue
        lbl = f"[{country}]" if tz == cur_tz else country
        row.append(InlineKeyboardButton(f"{lbl} {offset}", callback_data=f"tzs_{idx}"))
        if len(row) == 2: btns.append(row); row = []
    if row: btns.append(row)
    btns.append([InlineKeyboardButton("« Regions", callback_data="cfg_tz")])
    return InlineKeyboardMarkup(btns)

# ============= SHOW LIST =================

async def show_list(target, uid, ud, new=False):
    try: rows = sheet.get_all_records()
    except Exception:
        try: client.login(); rows = sheet.get_all_records()
        except Exception: rows = []

    items = [(i, r) for i, r in enumerate(rows, 2)
             if str(r.get("user_id", "")) == str(uid)
             and str(r.get("status", "")).strip() in ("active", "pending", "missed", "snoozed")
             and not str(r.get("group_id", "")).strip()]  # personal only

    if not items:
        t, kb = f"{hdr('Reminders')}\nNo reminders found.", home_kb()
        if new: sent = await target.reply_text(t, reply_markup=kb, parse_mode="HTML"); save_home(ud, sent)
        else: await safe_edit(target, t, kb)
        return

    lines = [hdr("Reminders")]
    for idx, (ri, r) in enumerate(items, 1):
        st, msg = str(r.get("status", "")), str(r.get("message", ""))
        short = msg[:30] + "…" if len(msg) > 30 else msg
        lines.append(f"\n<b>{idx}</b> {s_icon(st)} {short}\n"
                     f"   {fmt_date(norm_date(r.get('date', '')))} · {fmt_time(norm_time(r.get('time', '')))}")

    btns, num_row = [], []
    for idx, (ri, _) in enumerate(items, 1):
        num_row.append(InlineKeyboardButton(str(idx), callback_data=f"view_{ri}"))
        if len(num_row) == 5: btns.append(num_row); num_row = []
    if num_row: btns.append(num_row)
    btns.append([InlineKeyboardButton("« Back", callback_data="home")])
    if new: await target.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
    else: await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup(btns))

# ============= FINISH / REPEAT ===========

async def finish_or_repeat(target, uid, ud, msg, date, time, edit_msg=False):
    rep = ud.get("repeat")
    if rep:
        do_save(uid, ud, msg, date, time, rep)
        txt = f"{hdr('Saved ✓')}\n{detail(msg, date, time, fmt_rep(rep))}"
    else:
        ud["step"] = "repeat"
        txt = f"{hdr('New Reminder')}\n{detail(msg, date, time)}\n\nRepeat?"
    if rep:
        if edit_msg: await safe_edit(target, txt, home_kb())
        else:
            sent = await target.reply_text(txt, reply_markup=home_kb(), parse_mode="HTML")
            save_home(ud, sent)
    else:
        if edit_msg: await safe_edit(target, txt, repeat_kb())
        else:
            sent = await target.reply_text(txt, reply_markup=repeat_kb(), parse_mode="HTML")
            save_p(ud, sent)

# ============= BUTTON HANDLERS ===========

async def _btn_group(q, ctx, ud, uid, data):
    """Handle gjoin_, gskip_, gdone_, gsnzp_, gsnzb_, gsnz_ callbacks."""

    if data.startswith("gjoin_"):
        tid = data[6:]
        name = q.from_user.first_name or "User"
        gid = str(q.message.chat.id)

        # Check if reminder is still active
        row, r = find_by_tid(tid)
        if not r or str(r[5]) != "active":
            await q.answer("This reminder has already fired.", show_alert=True); return True

        added = add_tmember(tid, str(uid), name)
        if not added:
            await q.answer("Already joined!", show_alert=True); return True

        # Also subscribe to group for future reminders
        set_gsub(gid, str(uid), name, True)

        # Update setup message
        msg, ds, ts, rs = get_detail(r)
        txt = f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, rs)}\n\n{gsub_text(tid)}"
        await safe_edit(q.message, txt, gjoin_kb(tid))
        await q.answer("Joined ✓"); return True

    if data.startswith("gskip_"):
        tid = data[6:]
        uid_s = str(uid)

        row, r = find_by_tid(tid)
        if not r or str(r[5]) != "active":
            await q.answer("This reminder has already fired.", show_alert=True); return True

        # Mark as skipped in task members (or add then skip)
        members = get_tmembers(tid)
        found = any(str(u) == uid_s for u, _, _ in members)
        if found:
            set_tstatus(tid, uid_s, "skipped")
        else:
            add_tmember(tid, uid_s, q.from_user.first_name or "User", "skipped")

        msg, ds, ts, rs = get_detail(r)
        txt = f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, rs)}\n\n{gsub_text(tid)}"
        await safe_edit(q.message, txt, gjoin_kb(tid))
        await q.answer("Skipped"); return True

    if data.startswith("gdone_"):
        tid = data[6:]
        uid_s = str(uid)

        # Check if already handled
        members = get_tmembers(tid)
        cur_st = None
        for u, n, s in members:
            if str(u) == uid_s: cur_st = s; break
        if cur_st and cur_st != "pending":
            await safe_edit(q.message, f"{q.message.text}\n\n<i>Already handled</i>"); return True

        set_tstatus(tid, uid_s, "done")
        await rm_gpm(ctx, tid, uid_s)

        row, r = find_by_tid(tid)
        msg = str(r[1]).strip() if r else ""
        await safe_edit(q.message, f"{msg}\n\n<b>Done</b> ✓")
        await update_gstatus(ctx, tid, msg)

        if row and r:
            await check_all_resolved(ctx, tid, row, r)
            # Update group status again after potential advance
            await update_gstatus(ctx, tid, msg)
        return True

    if data.startswith("gsnzp_"):
        tid = data[6:]
        uid_s = str(uid)
        members = get_tmembers(tid)
        for u, n, s in members:
            if str(u) == uid_s and s != "pending":
                await safe_edit(q.message, f"<i>Already handled</i>"); return True

        row, r = find_by_tid(tid)
        msg = str(r[1]).strip() if r else ""
        await safe_edit(q.message, f"{msg}\n\nSnooze for:", snz_kb(tid, "gsnz"))
        return True

    if data.startswith("gsnzb_"):
        tid = data[6:]
        row, r = find_by_tid(tid)
        msg = str(r[1]).strip() if r else ""
        await safe_edit(q.message, f"{msg}\n\n<b>⏰ Group Reminder</b>", gact_kb(tid))
        return True

    if data.startswith("gsnz_"):
        rest = data[5:]
        last_us = rest.rfind("_")
        if last_us < 0: return False
        tid = rest[:last_us]
        try: mins = int(rest[last_us+1:])
        except ValueError: return False

        uid_s = str(uid)
        members = get_tmembers(tid)
        for u, n, s in members:
            if str(u) == uid_s and s != "pending":
                await safe_edit(q.message, f"<i>Already handled</i>"); return True

        set_tstatus(tid, uid_s, "snoozed")
        await rm_gpm(ctx, tid, uid_s)

        row, r = find_by_tid(tid)
        msg = str(r[1]).strip() if r else ""
        utz = get_tz(uid)
        nt = datetime.now(utz) + timedelta(minutes=mins)

        await safe_edit(q.message, f"{msg}\n\n<b>Snoozed {fmt_snz(mins)}</b> → {fmt_time(nt.strftime('%H:%M'))}")
        await update_gstatus(ctx, tid, msg)

        # Schedule personal snooze
        ctx.job_queue.run_once(group_snooze_fire, mins * 60,
            data={"tid": tid, "uid": uid, "uid_s": uid_s}, name=f"gsnz-{tid}-{uid_s}")
        return True

    return False

async def _btn_calendar(q, ud, uid, data):
    """Handle cal_ navigation and day_ selection."""
    utz = get_tz(uid)

    if data.startswith("cal_"):
        parts = data[4:].split("_")
        yr, mo = int(parts[0]), int(parts[1])
        if ud.get("step") == "edit_date":
            row = ud["editing_row"]
            r, msg, ds, ts, rs = row_detail(row)
            await safe_edit(q.message,
                f"{hdr('Edit Reminder')}\n{msg}\nCurrent: <i>{fmt_date(ds)} · {fmt_time(ts)}</i>\n\nPick new date:",
                cal_kb(yr, mo, f"edit_{row}", "« Back", tz=utz))
        else:
            msg, ts = ud.get("message", ""), ud.get("time", "")
            td = f"\n{fmt_time(ts)}" if ts else ""
            await safe_edit(q.message, f"{hdr('New Reminder')}\n{msg}{td}\n\nPick a date:", cal_kb(yr, mo, tz=utz))
        return True

    if data.startswith("day_"):
        date_str = data[4:]
        if ud.get("step") == "edit_date":
            row = ud["editing_row"]
            r, msg, old_d, ts, rs = row_detail(row)
            if is_past(date_str, ts, utz):
                now = datetime.now(utz)
                await safe_edit(q.message,
                    f"{hdr('Edit Reminder')}\n{msg}\nCurrent: <i>{fmt_date(old_d)} · {fmt_time(ts)}</i>\n\n"
                    f"{past_msg(ts)}\nPick a future date or change the time first.",
                    cal_kb(now.year, now.month, f"edit_{row}", "« Back", tz=utz))
            else:
                sheet.update_cell(row, 3, date_str)
                ud.clear()
                await safe_edit(q.message,
                    f"{hdr('Updated ✓')}\n{msg}\nDate: {fmt_date(old_d)} → <b>{fmt_date(date_str)}</b>\n"
                    f"Time: {fmt_time(ts)} · {rs}", home_kb())
                save_home(ud, q.message)
        else:
            ud["date"] = date_str
            msg, ts = ud.get("message", ""), ud.get("time")
            if ts:
                if is_past(date_str, ts, utz):
                    now = datetime.now(utz)
                    await safe_edit(q.message,
                        f"{hdr('New Reminder')}\n{msg}\n{fmt_time(ts)}\n\n"
                        f"{past_msg(ts)}\nPick a future date:", cal_kb(now.year, now.month, tz=utz))
                else:
                    await finish_or_repeat(q.message, uid, ud, msg, date_str, ts, edit_msg=True)
            else:
                ud["step"] = "time"
                await safe_edit(q.message,
                    f"{hdr('New Reminder')}\n{msg}\n{fmt_date(date_str)}\n\n"
                    f"Enter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", cancel_kb())
                save_p(ud, q.message)
        return True
    return False

async def _btn_reminder(q, ctx, ud, uid, data):
    """Handle view_, snzp_, snzb_, snz_, done_, crem_."""
    if data.startswith("view_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        st = r[5] if len(r) > 5 else "active"
        btns = []
        if st != "missed":
            btns.append([InlineKeyboardButton("✎ Edit", callback_data=f"edit_{row}"),
                         InlineKeyboardButton("✕ Cancel", callback_data=f"crem_{row}")])
        else:
            btns.append([InlineKeyboardButton("✕ Remove", callback_data=f"crem_{row}")])
        btns.append([InlineKeyboardButton("« Back", callback_data="list_refresh")])
        await safe_edit(q.message,
            f"{hdr('Reminder')}\n{msg}\n\n{fmt_date(ds)} · {fmt_time(ts)}\n{rs} · {s_icon(st)} <i>{s_label(st)}</i>",
            InlineKeyboardMarkup(btns))
        return True

    if data.startswith("snzp_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        if await guard(q, r): return True
        await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\nSnooze for:", snz_kb(row))
        return True

    if data.startswith("snzb_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        if await guard(q, r): return True
        await safe_edit(q.message, f"{msg}\n\n<b>⏰ Reminder</b>", act_kb(row))
        return True

    if data.startswith("snz_"):
        parts = data[4:].split("_")
        row, mins = int(parts[0]), int(parts[1])
        r, msg, ds, ts, rs = row_detail(row)
        if await guard(q, r): return True
        kill_jobs(ctx.job_queue, row); await rm_btns(ctx, row)
        utz = get_tz(uid)
        nt = datetime.now(utz) + timedelta(minutes=mins)
        rep = r[4] if len(r) > 4 else "none"
        if rep and rep != "none":
            sheet.update_cell(row, 6, "snoozed"); sheet.update_cell(row, 7, 0)
            ctx.job_queue.run_once(snooze_fire, mins * 60, data={"row": row, "chat": uid}, name=f"snooze-{row}")
        else:
            sheet.update_cell(row, 3, nt.strftime("%Y-%m-%d")); sheet.update_cell(row, 4, nt.strftime("%H:%M"))
            sheet.update_cell(row, 6, "active"); sheet.update_cell(row, 7, 0)
        ctx.bot_data.pop(f"r_{row}", None)
        await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\n<b>Snoozed {fmt_snz(mins)}</b> → {fmt_time(nt.strftime('%H:%M'))}")
        return True

    if data.startswith("done_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        if await guard(q, r): return True
        kill_jobs(ctx.job_queue, row); await rm_btns(ctx, row)
        if not advance_rep(row, r):
            sheet.update_cell(row, 6, "done"); sheet.update_cell(row, 7, 0)
        ctx.bot_data.pop(f"r_{row}", None)
        await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\n<b>Done</b> ✓")
        return True

    if data.startswith("crem_"):
        row = int(data[5:])
        kill_jobs(ctx.job_queue, row)
        r, msg, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 6, "cancelled"); sheet.update_cell(row, 7, 0)
        await rm_btns(ctx, row); ctx.bot_data.pop(f"r_{row}", None)
        await safe_edit(q.message, f"{detail(msg, ds, ts)}\n\n<b>Cancelled</b> ✕", home_kb())
        save_home(ud, q.message)
        return True

    return False

async def _btn_edit(q, ud, uid, data):
    """Handle edit_, emsg_, edate_, etime_."""
    if data.startswith("emsg_"):
        row = int(data[5:])
        ud["editing_row"], ud["step"] = row, "edit_message"
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message,
            f"{hdr('Edit Reminder')}\nCurrent: <i>{msg}</i>\n{fmt_date(ds)} · {fmt_time(ts)} · {rs}\n\nEnter new message:",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"edit_{row}")]]))
        save_p(ud, q.message)
        return True

    if data.startswith("edate_"):
        row = int(data[6:])
        ud["editing_row"], ud["step"] = row, "edit_date"
        r, msg, ds, ts, rs = row_detail(row)
        utz, now = get_tz(uid), datetime.now(get_tz(uid))
        await safe_edit(q.message,
            f"{hdr('Edit Reminder')}\n{msg}\nCurrent: <i>{fmt_date(ds)} · {fmt_time(ts)}</i>\n\nPick new date:",
            cal_kb(now.year, now.month, f"edit_{row}", "« Back", tz=utz))
        return True

    if data.startswith("etime_"):
        row = int(data[6:])
        ud["editing_row"], ud["step"] = row, "edit_time"
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message,
            f"{hdr('Edit Reminder')}\n{msg}\nCurrent: <i>{fmt_date(ds)} · {fmt_time(ts)}</i>\n\n"
            f"Enter new time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"edit_{row}")]]))
        save_p(ud, q.message)
        return True

    if data.startswith("edit_"):
        row = int(data[5:])
        ud.clear()
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message,
            f"{hdr('Edit Reminder')}\n{detail(msg, ds, ts, rs)}\n\nWhat to change?",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Message", callback_data=f"emsg_{row}"),
                 InlineKeyboardButton("Date", callback_data=f"edate_{row}"),
                 InlineKeyboardButton("Time", callback_data=f"etime_{row}")],
                [InlineKeyboardButton("« Back", callback_data=f"view_{row}")]]))
        return True

    return False

async def _btn_settings(q, ctx, ud, uid, data):
    """Handle all cfg_*, tzr_, tzs_ callbacks."""
    if data == "cfg_digest_toggle":
        cfg = get_cfg(uid)
        save_cfg(uid, "digest_on", str(not cfg["digest_on"]).lower())
        await show_settings(q.message, uid); return True

    if data == "cfg_digest_time":
        ud.clear(); ud["step"] = "set_digest_time"
        cfg = get_cfg(uid)
        await safe_edit(q.message,
            f"{hdr('Settings')}\nCurrent digest time: <b>{fmt_time(cfg['digest_time'])}</b>\n\n"
            f"Enter new time:\n<i>e.g. 7am, 8:30 AM, 06:00</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="cfg_back")]]))
        save_p(ud, q.message); return True

    if data == "cfg_retries":
        cfg = get_cfg(uid)
        await safe_edit(q.message,
            f"{hdr('Settings')}\nMax retries: <b>{cfg['max_retries']}×</b>\n\nHow many times to retry if missed?",
            cfg_picker_kb([1, 2, 3, 5, 7, 10], str, cfg["max_retries"], "cfgr_"))
        return True

    if data.startswith("cfgr_"):
        save_cfg(uid, "max_retries", int(data[5:])); await show_settings(q.message, uid); return True

    if data == "cfg_gap":
        cfg = get_cfg(uid)
        await safe_edit(q.message,
            f"{hdr('Settings')}\nRetry gap: <b>{cfg['retry_gap']} min</b>\n\nTime between retries?",
            cfg_picker_kb([5, 10, 15, 20, 30, 60], lambda v: f"{v}m", cfg["retry_gap"], "cfgg_"))
        return True

    if data.startswith("cfgg_"):
        save_cfg(uid, "retry_gap", int(data[5:])); await show_settings(q.message, uid); return True

    if data == "cfg_tz":
        cfg = get_cfg(uid)
        await safe_edit(q.message,
            f"{hdr('Timezone')}\n\nCurrent: <b>{tz_short(cfg.get('timezone', DEF_TZ))}</b>\n\nPick a region:",
            tz_region_kb())
        return True

    if data.startswith("tzr_"):
        cfg = get_cfg(uid)
        await safe_edit(q.message,
            f"{hdr('Timezone')}\n\n{TZ_ICONS.get(data[4:], '🌐')} <b>{data[4:]}</b>\n\nPick your timezone:",
            tz_list_kb(data[4:], cfg.get("timezone", DEF_TZ)))
        return True

    if data.startswith("tzs_"):
        idx = int(data[4:])
        if 0 <= idx < len(TZ_DATA):
            save_cfg(uid, "timezone", TZ_DATA[idx][0])
            await show_settings(q.message, uid)
        return True

    if data == "cfg_back":
        ud.clear(); await show_settings(q.message, uid); return True

    return False

# ============= MAIN BUTTON DISPATCHER ====

async def on_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data, ud, uid = q.data, ctx.user_data, q.from_user.id

    if data == "noop": return

    # Navigation (private only)
    if data in ("home", "cancel"):
        ud.clear()
        await safe_edit(q.message, HOME_TEXT, home_kb())
        save_home(ud, q.message); return

    if data == "add":
        await rm_home(ctx, ud); ud.clear(); ud["step"] = "message"
        sent = await q.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
        save_p(ud, sent); return

    if data.startswith("rep_"):
        rep, msg, date, time = data[4:], ud.get("message", ""), ud.get("date", ""), ud.get("time", "")
        do_save(uid, ud, msg, date, time, rep)
        await safe_edit(q.message, f"{hdr('Saved ✓')}\n{detail(msg, date, time, fmt_rep(rep))}", home_kb())
        save_home(ud, q.message); return

    if data == "list_refresh":
        ud.clear(); await show_list(q.message, uid, ud); return

    # Delegate to sub-handlers
    if await _btn_group(q, ctx, ud, uid, data): return
    if await _btn_calendar(q, ud, uid, data): return
    if await _btn_edit(q, ud, uid, data): return
    if await _btn_reminder(q, ctx, ud, uid, data): return
    if await _btn_settings(q, ctx, ud, uid, data): return

# ============= TEXT HANDLER ===============

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return  # ignore group text
    step, text = ctx.user_data.get("step"), update.message.text.strip()
    if step: await _do_step(update, ctx, step, text)
    else: await _try_nl(update, ctx, text)

async def _try_nl(update, ctx, text):
    uid, utz = update.effective_user.id, get_tz(update.effective_user.id)
    result = parse_nl(text, tz=utz)
    if not result or not result['message']: return
    msg, time, date, rep = result['message'], result['time'], result['date'], result.get('repeat')
    ud = ctx.user_data
    await rm_home(ctx, ud); ud.clear()
    ud["message"], ud["time"] = msg, time
    if rep: ud["repeat"] = rep
    if not date: date = datetime.now(utz).strftime("%Y-%m-%d")
    if is_past(date, time, utz):
        ud["step"] = "date"
        now = datetime.now(utz)
        sent = await update.message.reply_text(
            f"{hdr('New Reminder')}\n{msg}\n\n{past_msg(time)}\nPick a future date:",
            reply_markup=cal_kb(now.year, now.month, tz=utz), parse_mode="HTML")
        save_p(ud, sent)
    else:
        ud["date"] = date
        await finish_or_repeat(update.message, uid, ud, msg, date, time)

async def _do_step(update, ctx, step, text):
    ud, uid = ctx.user_data, update.effective_user.id

    if step == "message":
        await rm_prompt(ctx, ud)
        ud["message"], ud["step"] = text, "date"
        utz, now = get_tz(uid), datetime.now(get_tz(uid))
        sent = await update.message.reply_text(
            f"{hdr('New Reminder')}\n{text}\n\nPick a date:",
            reply_markup=cal_kb(now.year, now.month, tz=utz), parse_mode="HTML")
        save_p(ud, sent)

    elif step == "time":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text("Invalid time. Try again:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML"); return
        ds, utz = ud.get("date", ""), get_tz(uid)
        if is_past(ds, parsed, utz):
            await update.message.reply_text(f"{past_msg(parsed)}\nEnter a future time:", parse_mode="HTML"); return
        await del_prompt(ctx, ud)
        ud["time"] = parsed
        await finish_or_repeat(update.message, uid, ud, ud.get("message", ""), ds, parsed)

    elif step == "edit_message":
        row = ud.get("editing_row")
        if not row: return
        await rm_prompt(ctx, ud)
        r, old, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 2, text); ud.clear()
        sent = await update.message.reply_text(
            f"{hdr('Updated ✓')}\nMessage: {old} → <b>{text}</b>\n{fmt_date(ds)} · {fmt_time(ts)} · {rs}",
            reply_markup=home_kb(), parse_mode="HTML")
        save_home(ud, sent)

    elif step == "edit_time":
        row = ud.get("editing_row")
        if not row: return
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text("Invalid time. Try again:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML"); return
        r, msg, ds, old_t, rs = row_detail(row)
        if is_past(ds, parsed, get_tz(uid)):
            await update.message.reply_text(f"{past_msg(parsed)}\nEnter a future time:", parse_mode="HTML"); return
        await del_prompt(ctx, ud)
        sheet.update_cell(row, 4, parsed); ud.clear()
        sent = await update.message.reply_text(
            f"{hdr('Updated ✓')}\n{msg}\nDate: {fmt_date(ds)}\nTime: {fmt_time(old_t)} → <b>{fmt_time(parsed)}</b> · {rs}",
            reply_markup=home_kb(), parse_mode="HTML")
        save_home(ud, sent)

    elif step == "set_digest_time":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text("Invalid time. Try again:\n<i>e.g. 7am, 8:30 AM, 06:00</i>", parse_mode="HTML"); return
        await del_prompt(ctx, ud)
        save_cfg(uid, "digest_time", parsed); ud.clear()
        await update.message.reply_text(
            f"{hdr('Settings')}\nDigest time updated → <b>{fmt_time(parsed)}</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Settings", callback_data="cfg_back")]]),
            parse_mode="HTML")

# ============= SNOOZE FIRE (personal) =====

async def snooze_fire(ctx: ContextTypes.DEFAULT_TYPE):
    row, chat = ctx.job.data["row"], ctx.job.data["chat"]
    try: r = sheet.row_values(row)
    except Exception as e: logger.error(f"snooze_fire row {row}: {e}"); return
    if not r or len(r) <= 5 or r[5] != "snoozed": return
    await rm_btns(ctx, row)
    try:
        sent = await ctx.bot.send_message(chat_id=chat, text=f"{str(r[1]).strip()}\n\n<b>⏰ Reminder</b>",
            reply_markup=act_kb(row), parse_mode="HTML")
        save_rm(ctx, row, chat, sent.message_id)
    except Exception as e: logger.error(f"snooze_fire send {chat}: {e}"); return
    sheet.update_cell(row, 6, "pending"); sheet.update_cell(row, 7, 0)
    cfg = get_cfg(int(r[0]) if r[0].isdigit() else r[0])
    ctx.job_queue.run_once(auto_retry, cfg["retry_gap"] * 60, data={"row": row, "chat": chat}, name=f"retry-{row}")

# ============= AUTO RETRY (personal) ======

async def auto_retry(ctx: ContextTypes.DEFAULT_TYPE):
    row, chat = ctx.job.data["row"], ctx.job.data["chat"]
    try: r = sheet.row_values(row)
    except Exception as e: logger.error(f"retry row {row}: {e}"); return
    if not r or len(r) <= 5 or r[5] != "pending": return

    uid_val = int(r[0]) if r[0].isdigit() else r[0]
    cfg = get_cfg(uid_val)
    max_r, gap = cfg["max_retries"], cfg["retry_gap"]
    count = int(r[6]) if len(r) > 6 and r[6].isdigit() else 0

    if count >= max_r:
        if not advance_rep(row, r): sheet.update_cell(row, 6, "missed"); sheet.update_cell(row, 7, 0)
        return

    await rm_btns(ctx, row)
    nc = count + 1
    sent = await ctx.bot.send_message(chat_id=chat,
        text=f"{str(r[1]).strip()}\n\n<b>Reminder</b> ({nc}/{max_r})",
        reply_markup=act_kb(row), parse_mode="HTML")
    save_rm(ctx, row, chat, sent.message_id)
    sheet.update_cell(row, 7, nc)

    if nc >= max_r:
        if not advance_rep(row, r): sheet.update_cell(row, 6, "missed"); sheet.update_cell(row, 7, 0)
    else:
        ctx.job_queue.run_once(auto_retry, gap * 60, data={"row": row, "chat": chat}, name=f"retry-{row}")

# ============= GROUP FIRE =================

async def fire_group(ctx, row, v, creator_uid, msg, gid, tid, cfg):
    """Fire a group reminder: notify all members privately + post status in group."""
    members = get_tmembers(tid)
    active = [(u, n) for u, n, s in members if s == "waiting"]

    if not active:
        sheet.update_cell(row, 6, "done"); return

    # Mark all active members as pending
    for u, n in active:
        set_tstatus(tid, u, "pending")

    # Archive setup message (remove buttons)
    setup = ctx.bot_data.pop(f"gm_{tid}", None)
    if setup:
        try: await ctx.bot.edit_message_reply_markup(chat_id=int(setup["c"]), message_id=setup["m"], reply_markup=None)
        except Exception: pass

    # Post NEW status message in group
    try:
        status = await ctx.bot.send_message(chat_id=int(gid), text=gstatus_text(tid, msg), parse_mode="HTML")
        ctx.bot_data[f"gs_{tid}"] = {"c": int(gid), "m": status.message_id}
    except Exception as e:
        logger.error(f"[CRON] Group msg {gid}: {e}")

    # DM each member privately
    for u, n in active:
        try:
            u_int = int(u)
            sent = await ctx.bot.send_message(chat_id=u_int,
                text=f"{msg}\n\n<b>⏰ Group Reminder</b>",
                reply_markup=gact_kb(tid), parse_mode="HTML")
            ctx.bot_data[f"gpm_{tid}_{u}"] = {"c": u_int, "m": sent.message_id}
        except Exception as e:
            logger.error(f"[CRON] DM {u}: {e}")
            set_tstatus(tid, u, "missed")

    sheet.update_cell(row, 6, "pending"); sheet.update_cell(row, 7, 0)

    gap = cfg.get("retry_gap", DEF_RETRY_GAP)
    ctx.job_queue.run_once(group_retry, gap * 60,
        data={"tid": tid, "row": row, "gid": gid}, name=f"gretry-{tid}")

# ============= GROUP RETRY ================

async def group_retry(ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.job.data["tid"]
    row = ctx.job.data["row"]
    gid = ctx.job.data["gid"]

    try: r = sheet.row_values(row)
    except Exception as e: logger.error(f"[GRETRY] row {row}: {e}"); return
    if not r or len(r) <= 5 or r[5] != "pending": return

    creator_uid = int(r[0]) if r[0].isdigit() else r[0]
    cfg = get_cfg(creator_uid)
    max_r, gap = cfg["max_retries"], cfg["retry_gap"]
    count = int(r[6]) if len(r) > 6 and r[6].isdigit() else 0

    members = get_tmembers(tid)
    pending = [(u, n) for u, n, s in members if s == "pending"]

    if not pending or count >= max_r:
        # Mark remaining pending as missed
        for u, n in pending:
            set_tstatus(tid, u, "missed")
        msg = str(r[1]).strip()
        await update_gstatus(ctx, tid, msg)
        # Check all resolved
        await check_all_resolved(ctx, tid, row, r)
        return

    nc = count + 1
    msg = str(r[1]).strip()

    # Send retry to each pending member
    for u, n in pending:
        await rm_gpm(ctx, tid, u)
        try:
            u_int = int(u)
            sent = await ctx.bot.send_message(chat_id=u_int,
                text=f"{msg}\n\n<b>Group Reminder</b> ({nc}/{max_r})",
                reply_markup=gact_kb(tid), parse_mode="HTML")
            ctx.bot_data[f"gpm_{tid}_{u}"] = {"c": u_int, "m": sent.message_id}
        except Exception as e:
            logger.error(f"[GRETRY] DM {u}: {e}")
            set_tstatus(tid, u, "missed")

    sheet.update_cell(row, 7, nc)
    await update_gstatus(ctx, tid, msg)

    if nc >= max_r:
        for u, n in pending:
            set_tstatus(tid, u, "missed")
        await update_gstatus(ctx, tid, msg)
        await check_all_resolved(ctx, tid, row, r)
    else:
        ctx.job_queue.run_once(group_retry, gap * 60,
            data={"tid": tid, "row": row, "gid": gid}, name=f"gretry-{tid}")

# ============= GROUP SNOOZE FIRE ==========

async def group_snooze_fire(ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.job.data["tid"]
    uid = ctx.job.data["uid"]
    uid_s = ctx.job.data["uid_s"]

    members = get_tmembers(tid)
    for u, n, s in members:
        if str(u) == uid_s and s != "snoozed": return

    set_tstatus(tid, uid_s, "pending")

    row, r = find_by_tid(tid)
    if not r: return
    msg = str(r[1]).strip()

    await rm_gpm(ctx, tid, uid_s)
    try:
        sent = await ctx.bot.send_message(chat_id=uid,
            text=f"{msg}\n\n<b>⏰ Group Reminder</b>",
            reply_markup=gact_kb(tid), parse_mode="HTML")
        ctx.bot_data[f"gpm_{tid}_{uid_s}"] = {"c": uid, "m": sent.message_id}
    except Exception as e:
        logger.error(f"[GSNZ] DM {uid}: {e}")
        set_tstatus(tid, uid_s, "missed")

    await update_gstatus(ctx, tid, msg)

# ============= DAILY DIGEST ==============

async def check_digest(ctx: ContextTypes.DEFAULT_TYPE):
    try: cfg_rows = cfg_sheet.get_all_values()
    except Exception:
        try: client.login(); cfg_rows = cfg_sheet.get_all_values()
        except Exception: return

    for r in cfg_rows[1:]:
        if len(r) < 3 or str(r[1]).lower() != "true": continue
        tz_name = str(r[5]) if len(r) > 5 and r[5] else DEF_TZ
        user_tz = safe_tz(tz_name)
        now = datetime.now(user_tz)
        if norm_time(r[2]) != now.strftime("%H:%M"): continue

        try: uid_int = int(r[0])
        except (ValueError, TypeError): continue

        try: rem_rows = sheet.get_all_values()
        except Exception: continue

        today = now.strftime("%Y-%m-%d")
        # Personal reminders for today
        items = [v for v in rem_rows[1:] if len(v) >= 6 and str(v[0]) == str(r[0])
                 and str(v[5]).strip().lower() in ("active", "snoozed")
                 and norm_date(str(v[2]).strip()) == today
                 and not (len(v) > 7 and str(v[7]).strip())]  # exclude group reminders
        items.sort(key=lambda x: norm_time(str(x[3]).strip()))

        if items:
            lines = [f"☀️ <b>Good morning!</b>\n{DIV}\n\nToday — {now.strftime('%-d %b')}\n"]
            for v in items:
                msg = str(v[1]).strip()
                short = msg[:30] + "…" if len(msg) > 30 else msg
                lines.append(f"  {fmt_time(norm_time(str(v[3]).strip()))} · {short}")
            lines.append(f"\n{len(items)} reminder{'s' if len(items) != 1 else ''} today")
        else:
            lines = [f"☀️ <b>Good morning!</b>\n{DIV}\n\nToday — {now.strftime('%-d %b')}\n",
                     "No reminders today. Enjoy your day!"]
        try:
            await ctx.bot.send_message(chat_id=uid_int, text="\n".join(lines),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("＋ New", callback_data="add")]]),
                parse_mode="HTML")
        except Exception as e: logger.error(f"[DIGEST] Send {r[0]}: {e}")

# ============= SCHEDULER =================

async def check_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    try: cfg_vals = cfg_sheet.get_all_values()
    except Exception:
        try: client.login(); cfg_vals = cfg_sheet.get_all_values()
        except Exception: cfg_vals = []

    tz_map, cfg_map = {}, {}
    for r in cfg_vals[1:]:
        if not r: continue
        uid_s = str(r[0])
        tz_map[uid_s] = str(r[5]) if len(r) > 5 and r[5] else DEF_TZ
        cfg_map[uid_s] = {
            "retry_gap": int(r[4]) if len(r) > 4 and r[4] else DEF_RETRY_GAP,
            "max_retries": int(r[3]) if len(r) > 3 and r[3] else DEF_RETRIES,
        }

    try: vals = sheet.get_all_values()
    except Exception:
        try: client.login(); vals = sheet.get_all_values()
        except Exception as e: logger.error(f"[CRON] {e}"); return

    for idx, v in enumerate(vals[1:], 2):
        if len(v) < 7 or str(v[5]).strip().lower() != "active": continue
        uid_s = str(v[0])
        user_tz = safe_tz(tz_map.get(uid_s, DEF_TZ))
        now = datetime.now(user_tz)
        if norm_date(str(v[2]).strip()) != now.strftime("%Y-%m-%d"): continue
        if norm_time(str(v[3]).strip()) != now.strftime("%H:%M"): continue

        uid = int(v[0]) if v[0].isdigit() else v[0]
        msg = str(v[1]).strip()
        gid = str(v[7]).strip() if len(v) > 7 else ""
        tid = str(v[8]).strip() if len(v) > 8 else ""

        logger.info(f"[CRON] FIRE row {idx}: '{msg[:30]}' → uid={uid} gid={gid}")

        if gid and tid:
            # GROUP REMINDER
            await fire_group(ctx, idx, v, uid, msg, gid, tid, cfg_map.get(uid_s, {}))
        else:
            # PERSONAL REMINDER
            kill_jobs(ctx.job_queue, idx); await rm_btns(ctx, idx)
            try:
                sent = await ctx.bot.send_message(chat_id=uid, text=f"{msg}\n\n<b>⏰ Reminder</b>",
                    reply_markup=act_kb(idx), parse_mode="HTML")
                save_rm(ctx, idx, uid, sent.message_id)
            except Exception as e: logger.error(f"[CRON] Send {uid}: {e}"); continue

            sheet.update_cell(idx, 6, "pending"); sheet.update_cell(idx, 7, 0)
            gap = cfg_map.get(uid_s, {"retry_gap": DEF_RETRY_GAP})["retry_gap"]
            ctx.job_queue.run_once(auto_retry, gap * 60, data={"row": idx, "chat": uid}, name=f"retry-{idx}")

# ============= MAIN ======================

def main():
    app = Application.builder().token(TOKEN).job_queue(JobQueue()).post_init(post_init).build()
    for cmd, fn in [("start", start), ("add", add_cmd), ("list", list_cmd),
                    ("remind", remind_cmd), ("settings", settings_cmd), ("info", info_cmd)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(check_reminders, interval=60, first=0)
    app.job_queue.run_repeating(check_digest, interval=60, first=10)
    print("Smart Reminder Bot Running")
    app.run_polling()

if __name__ == "__main__":
    main()
