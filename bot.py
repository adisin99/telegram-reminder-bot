import logging
import os
import json
import re
import calendar as cal_module
import time as time_module
from datetime import datetime, timedelta
from threading import Thread

import pytz
from flask import Flask

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
    BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, ForceReply,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, JobQueue,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ================= CONFIG =================
TOKEN = os.environ.get("BOT_TOKEN")
SHEET_URL = os.environ.get("SHEET_URL")
creds_json = os.environ.get("GOOGLE_CREDS")

DIV = "━━━━━━━━━━━━━━━━━━━━"
AUTO_MIN_SEC = 180

DEF_TZ = "Asia/Kolkata"
DEF_RETRIES = 3
DEF_RETRY_GAP = 10
DEF_DIGEST_TIME = "07:00"

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

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

IGNORE_WORDS = {
    'hi', 'hello', 'hey', 'yo', 'thanks', 'thank', 'thank you', 'ty', 'thx',
    'ok', 'okay', 'k', 'kk', 'okays', 'yes', 'yeah', 'yep', 'yup', 'y',
    'no', 'nah', 'nope', 'n', 'bye', 'goodbye', 'cya', 'see you',
    'good morning', 'good night', 'gm', 'gn', 'lol', 'haha', 'hehe',
    'what', 'why', 'how', 'when', 'where', 'help', '?',
}

TZ_REGIONS = list(dict.fromkeys(t[3] for t in TZ_DATA))
TZ_ICONS = {"Asia": "🌏", "Europe": "🌍", "Americas": "🌎", "Oceania": "🌏", "Africa": "🌍"}

# =============== LOGGING =================
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ============= GOOGLE SHEET ==============
if not all([TOKEN, SHEET_URL, creds_json]):
    raise Exception("Missing required environment variables")

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
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
cfg_sheet = get_or_create_sheet("Settings", ["user_id", "digest_on", "digest_time", "max_retries", "retry_gap", "timezone", "username"])
grp_sheet = get_or_create_sheet("GroupMembers", ["group_id", "user_id", "first_name", "username", "subscribed"])
task_sheet = get_or_create_sheet("TaskMembers", ["task_id", "user_id", "first_name", "status"])
digest_archive_sheet = get_or_create_sheet("DigestArchive", ["user_id", "date", "count", "digest_text"])
weekly_archive_sheet = get_or_create_sheet("WeeklyArchive", ["user_id", "week_start", "week_end", "done", "missed", "total", "report_text"])
monthly_archive_sheet = get_or_create_sheet("MonthlyArchive", ["user_id", "year", "month", "done", "missed", "snoozed", "total", "stats_json"])

# ============= FORMATTERS ================
def hdr(t):
    return f"<b>{t}</b>\n{DIV}"

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

ST_IC = {"active": "○", "pending": "●", "missed": "✗", "snoozed": "◷", "done": "✅", "cancelled": "✕"}
ST_LB = {"active": "Active", "pending": "Pending", "missed": "Missed", "snoozed": "Snoozed", "done": "Done", "cancelled": "Cancelled"}
GT_IC = {"waiting": "⏳", "pending": "⏳", "done": "✅", "snoozed": "◷", "missed": "✗"}

def fmt_rep(r):
    s = str(r)
    if s.startswith("custom:"):
        days = s.split(":")[1].split(",") if ":" in s else []
        if not days:
            return "Custom"
        if days == ["mon", "tue", "wed", "thu", "fri"]:
            return "Mon–Fri"
        if days == ["sat", "sun"]:
            return "Weekends"
        if days == DAY_KEYS:
            return "Every day"
        return ", ".join(d.capitalize() for d in days)
    return {"none": "Once", "daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}.get(s, s)

def fmt_snz(m):
    return f"{m} min" if m < 60 else f"{m // 60} hr{'s' if m >= 120 else ''}"

def tz_label(n):
    for tz, c, _, _ in TZ_DATA:
        if tz == n:
            return c
    return n.split("/")[-1].replace("_", " ")

def tz_short(n):
    for tz, c, o, _ in TZ_DATA:
        if tz == n:
            return f"{c} ({o})"
    return n.split("/")[-1].replace("_", " ")

# ============= NORMALIZERS ================
def norm_date(val):
    s = str(val).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    for f in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            pass
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
            if "PM" in u and h != 12:
                h += 12
            elif "AM" in u and h == 12:
                h = 0
            return f"{h:02d}:{m:02d}"
        except (ValueError, IndexError):
            return s
    try:
        t = round(float(s) * 24 * 60)
        return f"{t // 60:02d}:{t % 60:02d}"
    except ValueError:
        return s

# ============= HELPERS ====================
def get_tz(uid):
    try:
        return pytz.timezone(get_cfg(uid).get("timezone", DEF_TZ))
    except Exception:
        return pytz.timezone(DEF_TZ)

def safe_tz(n):
    try:
        return pytz.timezone(n)
    except Exception:
        return pytz.timezone(DEF_TZ)

def get_cfg(uid):
    uid_s = str(uid)
    try:
        rows = cfg_sheet.get_all_values()
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
    cfg_sheet.append_row([uid_s, "true", DEF_DIGEST_TIME, DEF_RETRIES, DEF_RETRY_GAP, DEF_TZ, ""], value_input_option="RAW")
    return {"digest_on": True, "digest_time": DEF_DIGEST_TIME, "max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP, "timezone": DEF_TZ}

def save_cfg(uid, field, value):
    uid_s = str(uid)
    col = {"digest_on": 2, "digest_time": 3, "max_retries": 4, "retry_gap": 5, "timezone": 6}.get(field)
    if not col:
        return
    try:
        rows = cfg_sheet.get_all_values()
        for i, r in enumerate(rows[1:], 2):
            if str(r[0]) == uid_s:
                cfg_sheet.update_cell(i, col, str(value))
                return
        get_cfg(uid)
        rows = cfg_sheet.get_all_values()
        for i, r in enumerate(rows[1:], 2):
            if str(r[0]) == uid_s:
                cfg_sheet.update_cell(i, col, str(value))
    except Exception:
        pass

def update_username(uid, username):
    if not username:
        return
    uid_s, uname = str(uid), username.lower().strip()
    try:
        rows = cfg_sheet.get_all_values()
        for i, r in enumerate(rows[1:], 2):
            if str(r[0]) == uid_s:
                if len(r) > 6 and r[6].strip().lower() == uname:
                    return
                cfg_sheet.update_cell(i, 7, uname)
                return
        get_cfg(uid)
        rows = cfg_sheet.get_all_values()
        for i, r in enumerate(rows[1:], 2):
            if str(r[0]) == uid_s:
                cfg_sheet.update_cell(i, 7, uname)
                return
    except Exception:
        pass

def row_detail(row):
    r = sheet.row_values(row)
    return (r, str(r[1]).strip() if len(r) > 1 else "", norm_date(r[2]) if len(r) > 2 else "",
            norm_time(r[3]) if len(r) > 3 else "", fmt_rep(r[4]) if len(r) > 4 else "")

def get_detail(r):
    return (str(r[1]).strip() if len(r) > 1 else "", norm_date(r[2]) if len(r) > 2 else "",
            norm_time(r[3]) if len(r) > 3 else "", fmt_rep(r[4]) if len(r) > 4 else "")

def is_past(ds, ts, tz=None):
    now = datetime.now(tz or safe_tz(DEF_TZ))
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

def is_custom_day_match(rep, now):
    if not str(rep).startswith("custom:"):
        return True
    days = str(rep).split(":")[1].split(",") if ":" in str(rep) else []
    return now.strftime("%a").lower()[:3] in days

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
        if mo > 12:
            mo, yr = 1, yr + 1
        nd = d.replace(year=yr, month=mo)
    elif rep.startswith("custom:"):
        days = rep.split(":")[1].split(",") if ":" in rep else []
        if not days:
            return False
        for offset in range(1, 8):
            candidate = d + timedelta(days=offset)
            if candidate.strftime("%a").lower()[:3] in days:
                nd = candidate
                break
        else:
            return False
    else:
        return False
    sheet.update_cell(row, 3, nd.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, "active")
    sheet.update_cell(row, 7, 0)
    return True

def kill_jobs(jq, name_prefix):
    for n in [f"retry-{name_prefix}", f"snooze-{name_prefix}"] if isinstance(name_prefix, int) else [name_prefix]:
        for j in jq.get_jobs_by_name(n):
            j.schedule_removal()

# ============= GROUP DATA =================
def gen_tid():
    return f"t{int(time_module.time())}"

def grp_read(sheet_ref, filter_fn):
    try:
        return [r for r in sheet_ref.get_all_values()[1:] if filter_fn(r)]
    except Exception:
        return []

def get_gsubs(gid):
    return [(r[1], r[2], r[3] if len(r) > 4 else "") for r in grp_read(grp_sheet, 
            lambda r: str(r[0]) == str(gid) and str(r[4] if len(r) > 4 else r[3]).lower() == "true")]

def set_gsub(gid, uid, name, username="", sub=True):
    gid_s, uid_s, uname = str(gid), str(uid), username.lower().strip() if username else ""
    try:
        rows = grp_sheet.get_all_values()
        for i, r in enumerate(rows[1:], 2):
            if str(r[0]) == gid_s and str(r[1]) == uid_s:
                grp_sheet.update_cell(i, 3, name)
                if uname:
                    grp_sheet.update_cell(i, 4, uname)
                grp_sheet.update_cell(i, 5, str(sub).lower())
                return
        grp_sheet.append_row([gid_s, uid_s, name, uname, str(sub).lower()], value_input_option="RAW")
    except Exception:
        pass

def get_tmembers(tid):
    return [(r[1], r[2], r[3]) for r in grp_read(task_sheet, lambda r: str(r[0]) == str(tid))]

def add_tmember(tid, uid, name, st="waiting"):
    try:
        rows = task_sheet.get_all_values()
        if any(str(r[0]) == str(tid) and str(r[1]) == str(uid) for r in rows[1:]):
            return False
        task_sheet.append_row([str(tid), str(uid), name, st], value_input_option="RAW")
        return True
    except Exception:
        return False

def set_tstatus(tid, uid, st):
    try:
        rows = task_sheet.get_all_values()
        for i, r in enumerate(rows[1:], 2):
            if str(r[0]) == str(tid) and str(r[1]) == str(uid):
                task_sheet.update_cell(i, 4, st)
                return
    except Exception:
        pass

def find_by_tid(tid):
    try:
        rows = sheet.get_all_values()
        for i, r in enumerate(rows[1:], 2):
            if len(r) > 8 and str(r[8]) == str(tid):
                return i, r
    except Exception:
        pass
    return None, None

def gstatus_text(tid, msg):
    ms = get_tmembers(tid)
    active = [(u, n, s) for u, n, s in ms if s != "skipped"]
    if not active:
        return f"⏰ {msg}\n\nNo subscribers"
    if all(s in ("done", "missed") for _, _, s in active):
        if all(s == "done" for _, _, s in active):
            return f"{msg}\n\n✅ All done · {', '.join(n for _, n, _ in active)}"
    prefix = "⏰ " if any(s not in ("done", "missed") for _, _, s in active) else ""
    parts = [f"{GT_IC.get(s, '⏳')} {n}" for _, n, s in active]
    return f"{prefix}{msg}\n\n{' · '.join(parts)}"

def gsub_text(tid):
    active = [(u, n) for u, n, s in get_tmembers(tid) if s != "skipped"]
    return f"{len(active)} subscribed: {', '.join(n for _, n in active)}" if active else "0 subscribed"

async def update_gstatus(ctx, tid, msg):
    info = ctx.bot_data.get(f"gs_{tid}")
    if info:
        try:
            await ctx.bot.edit_message_text(chat_id=info["c"], message_id=info["m"], text=gstatus_text(tid, msg), parse_mode="HTML")
        except Exception:
            pass

async def check_grp_resolved(ctx, tid, row, r):
    active = [(u, n, s) for u, n, s in get_tmembers(tid) if s != "skipped"]
    if not active or not all(s in ("done", "missed") for _, _, s in active):
        return
    kill_jobs(ctx.job_queue, f"gretry-{tid}")
    if not advance_rep_grp(row, r, tid):
        sheet.update_cell(row, 6, "done")
        sheet.update_cell(row, 7, 0)

def advance_rep_grp(row, r, tid):
    if not advance_rep(row, r):
        return False
    try:
        rows = task_sheet.get_all_values()
        for i, tr in enumerate(rows[1:], 2):
            if str(tr[0]) == str(tid) and str(tr[3]) != "skipped":
                task_sheet.update_cell(i, 4, "waiting")
    except Exception:
        pass
    return True

# ============= TAG EXTRACTION =========
def extract_tags(message):
    tags = []
    if not message.entities:
        return tags
    for entity in message.entities:
        if entity.type == "text_mention" and entity.user:
            if entity.user.first_name:
                tags.append(entity.user.first_name.lower().strip())
            if entity.user.username:
                tags.append(entity.user.username.lower().strip())
        elif entity.type == "mention":
            raw = (message.text or "")[entity.offset:entity.offset + entity.length]
            clean = raw.lstrip("@").lower().strip()
            if clean:
                tags.append(clean)
    return tags

def strip_mentions(text, message):
    if not message.entities:
        return text
    for entity in sorted(message.entities, key=lambda e: e.offset, reverse=True):
        if entity.type in ("mention", "text_mention"):
            mt = (message.text or "")[entity.offset:entity.offset + entity.length]
            text = text.replace(mt, "", 1)
    return re.sub(r'\s+', ' ', text).strip()

# ============= MESSAGE UTILS =============
async def safe_edit(msg, text, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

async def del_prompt(ctx, ud):
    mid, cid = ud.pop("p_mid", None), ud.pop("p_cid", None)
    if mid and cid:
        try:
            await ctx.bot.delete_message(chat_id=cid, message_id=mid)
        except Exception:
            pass

def save_p(ud, msg):
    ud["p_mid"], ud["p_cid"] = msg.message_id, msg.chat.id

async def rm_btns(ctx, row):
    prev = ctx.bot_data.pop(f"r_{row}", None)
    if prev:
        try:
            await ctx.bot.edit_message_reply_markup(chat_id=prev["c"], message_id=prev["m"], reply_markup=None)
        except Exception:
            pass

async def rm_home(ctx, ud):
    mid, cid = ud.pop("h_mid", None), ud.pop("h_cid", None)
    if mid and cid:
        try:
            await ctx.bot.edit_message_reply_markup(chat_id=cid, message_id=mid, reply_markup=None)
        except Exception:
            pass

def save_home(ud, msg):
    ud["h_mid"], ud["h_cid"] = msg.message_id, msg.chat.id

async def rm_gpm(ctx, tid, uid_s):
    old = ctx.bot_data.pop(f"gpm_{tid}_{uid_s}", None)
    if old:
        try:
            await ctx.bot.edit_message_reply_markup(chat_id=old["c"], message_id=old["m"], reply_markup=None)
        except Exception:
            pass

def get_username(user):
    return user.username.lower().strip() if user and getattr(user, 'username', None) else ""

async def auto_minimize(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    try:
        await ctx.bot.edit_message_text(
            chat_id=d["c"], message_id=d["m"], text=d["min_text"],
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Show", callback_data=d["show_cb"])]]),
            parse_mode="HTML")
    except Exception:
        pass

def schedule_minimize(ctx, sent, min_text, show_cb, timeout=AUTO_MIN_SEC):
    ctx.job_queue.run_once(auto_minimize, timeout, data={
        "c": sent.chat.id, "m": sent.message_id,
        "min_text": min_text, "show_cb": show_cb
    })

# ============= UI ========================
HOME_TEXT = (
    f"{hdr('RemindX')}\n"
    "Just type your reminder:\n\n"
    "<i>Buy milk tomorrow at 5pm</i>\n"
    "<i>Gym at 6pm daily</i>\n"
    "<i>Meeting Monday 10am weekly</i>\n"
    "<i>Call mom in 30 min</i>"
)

def home_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Schedule", callback_data="sched_today")]])

def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="cancel")]])

def close_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔴", callback_data="cfg_close")]])

def act_kb(row):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Snooze", callback_data=f"snzp_{row}"), 
                                  InlineKeyboardButton("Done", callback_data=f"done_{row}")]])

def gact_kb(tid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Snooze", callback_data=f"gsnzp_{tid}"), 
                                  InlineKeyboardButton("Done", callback_data=f"gdone_{tid}")]])

def saved_kb(row, rep):
    btns = []
    if str(rep) in ("none", ""):
        btns.append(InlineKeyboardButton("🔁 Repeat", callback_data=f"chrep_{row}"))
    btns.append(InlineKeyboardButton("✎ Edit", callback_data=f"edit_{row}"))
    return InlineKeyboardMarkup([btns])

def gjoin_kb(tid, show_rep=False):
    btns = [[InlineKeyboardButton("＋ Count Me In", callback_data=f"gjoin_{tid}"), 
             InlineKeyboardButton("✕ Skip", callback_data=f"gskip_{tid}")]]
    if show_rep:
        btns.append([InlineKeyboardButton("🔁 Repeat", callback_data=f"gchrep_{tid}")])
    return InlineKeyboardMarkup(btns)

def rep_picker_kb(prefix):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Daily", callback_data=f"{prefix}_daily"), 
         InlineKeyboardButton("Weekly", callback_data=f"{prefix}_weekly"),
         InlineKeyboardButton("Monthly", callback_data=f"{prefix}_monthly")],
        [InlineKeyboardButton("Customize", callback_data=f"{prefix}_custom")],
    ])

def custom_days_kb(selected, prefix):
    row1 = [InlineKeyboardButton(f"[{n}]" if DAY_KEYS[i] in selected else n, callback_data=f"cday_{prefix}_{DAY_KEYS[i]}") 
            for i, n in enumerate(DAY_NAMES[:4])]
    row2 = [InlineKeyboardButton(f"[{n}]" if DAY_KEYS[i] in selected else n, callback_data=f"cday_{prefix}_{DAY_KEYS[i]}") 
            for i, n in enumerate(DAY_NAMES[4:], 4)]
    btns = [row1, row2, [
        InlineKeyboardButton("Mon–Fri", callback_data=f"cday_{prefix}_weekdays"),
        InlineKeyboardButton("All", callback_data=f"cday_{prefix}_all"),
        InlineKeyboardButton("Clear", callback_data=f"cday_{prefix}_clear"),
    ]]
    if selected:
        btns.append([InlineKeyboardButton("✓ Save", callback_data=f"cdaysave_{prefix}")])
    return InlineKeyboardMarkup(btns)

def snz_kb(key, pfx="snz"):
    opts = [(15, "15m"), (30, "30m"), (45, "45m"), (60, "1h"), (120, "2h"), (180, "3h")]
    kb = [[InlineKeyboardButton(l, callback_data=f"{pfx}_{key}_{m}") for m, l in opts[i:i+3]] for i in range(0, 6, 3)]
    return InlineKeyboardMarkup(kb)

def gmin_kb(show_cb):
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Show", callback_data=show_cb)]])

def gclose_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔴", callback_data="gclose")]])

# ============= CALENDAR ==================
def cal_kb(year, month, back_cb="cancel", tz=None):
    now = datetime.now(tz or safe_tz(DEF_TZ))
    kb = [[InlineKeyboardButton(f"{cal_module.month_name[month]} {year}", callback_data="noop")]]
    kb.append([InlineKeyboardButton(d, callback_data="noop") for d in "Mo Tu We Th Fr Sa Su".split()])
    for week in cal_module.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0 or datetime(year, month, day).date() < now.date():
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                ds = f"{year}-{month:02d}-{day:02d}"
                lbl = f"[{day}]" if datetime(year, month, day).date() == now.date() else str(day)
                row.append(InlineKeyboardButton(lbl, callback_data=f"day_{ds}"))
        kb.append(row)
    td, tm = now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")
    kb.append([InlineKeyboardButton("Today", callback_data=f"day_{td}"), 
               InlineKeyboardButton("Tomorrow", callback_data=f"day_{tm}")])
    nm, ny = (month % 12) + 1, year + (1 if month == 12 else 0)
    pm, py = ((month - 2) % 12) + 1, year - (1 if month == 1 else 0)
    nav = [InlineKeyboardButton("‹", callback_data=f"cal_{py}_{pm:02d}"), 
           InlineKeyboardButton("›", callback_data=f"cal_{ny}_{nm:02d}")]
    kb.append(nav)
    kb.append([InlineKeyboardButton("✕ Cancel", callback_data=back_cb)])
    return InlineKeyboardMarkup(kb)

# ============= PARSERS ====================
def parse_time(text):
    for pat, mode in [(r'^(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)$', 'hma'), 
                      (r'^(\d{1,2})\s*(am|pm)$', 'ha'), 
                      (r'^(\d{1,2})[:.]\s*(\d{1,2})$', '24')]:
        m = re.match(pat, text.strip(), re.I)
        if m:
            h = int(m.group(1))
            mi = int(m.group(2)) if mode != 'ha' else 0
            if mode != '24':
                ap = m.group(3 if mode == 'hma' else 2).lower()
                if ap == 'pm' and h != 12:
                    h += 12
                elif ap == 'am' and h == 12:
                    h = 0
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return f"{h:02d}:{mi:02d}"
    return None

def to24(h, mi, ap):
    if ap == 'pm' and h != 12:
        h += 12
    elif ap == 'am' and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if 0 <= h <= 23 and 0 <= mi <= 59 else None

def find_time(text):
    for pat, mode in [(r'(?:at|by)\s+(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)', 'hma'),
                      (r'(?:at|by)\s+(\d{1,2})\s*(am|pm)', 'ha'),
                      (r'(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)', 'hma'),
                      (r'(\d{1,2})\s*(am|pm)', 'ha')]:
        m = re.search(pat, text, re.I)
        if m:
            if mode == 'hma':
                t = to24(int(m.group(1)), int(m.group(2)), m.group(3))
            else:
                t = to24(int(m.group(1)), 0, m.group(2))
            if t:
                return t, m.start(), m.end()
    return None

def find_relative(text, tz=None):
    now = datetime.now(tz or safe_tz(DEF_TZ))
    for pat, unit in [(r'\b(?:in|after)\s+(\d+)\s*(?:min(?:ute)?s?|m)\b', 'min'),
                      (r'\b(?:in|after)\s+(\d+)\s*(?:hour|hr|h)s?\b', 'hour'),
                      (r'\b(?:in|after)\s+(\d+)\s*days?\b', 'day'),
                      (r'\b(?:in|after)\s+(\d+)\s*weeks?\b', 'week')]:
        m = re.search(pat, text.lower())
        if m:
            val = int(m.group(1))
            if val > 0:
                if unit == 'min':
                    target = now + timedelta(minutes=val)
                elif unit == 'hour':
                    target = now + timedelta(hours=val)
                elif unit == 'day':
                    target = now + timedelta(days=val)
                else:
                    target = now + timedelta(weeks=val)
                return target.strftime("%Y-%m-%d"), target.strftime("%H:%M"), m.start(), m.end()
    return None

def find_date(text, tz=None):
    now = datetime.now(tz or safe_tz(DEF_TZ))
    low = text.lower()
    for pat, delta in [(r'\bday\s+after\s+tomorrow\b', 2), (r'\b(today|tonight)\b', 0), 
                       (r'\b(tomorrow|tmrw|tmr)\b', 1)]:
        m = re.search(pat, low)
        if m:
            return (now + timedelta(days=delta)).strftime("%Y-%m-%d"), m.start(), m.end()
    days_full = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    days_abbr = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    for i, (full, abr) in enumerate(zip(days_full, days_abbr)):
        m = re.search(rf'\b(?:on\s+)?({full}|{abr})\b', low)
        if m:
            d = (i - now.weekday()) % 7 or 7
            return (now + timedelta(days=d)).strftime("%Y-%m-%d"), m.start(), m.end()
    return None

def find_repeat(text, tz=None):
    low = text.lower()
    for pat, rep in [(r'\b(?:every\s*day|daily)\b', 'daily'),
                     (r'\b(?:every\s*week|weekly)\b', 'weekly'),
                     (r'\b(?:every\s*month|monthly)\b', 'monthly')]:
        m = re.search(pat, low)
        if m:
            return rep, m.start(), m.end(), None
    return None

def clean_text(text, spans):
    for s, e in sorted([x for x in spans if x], key=lambda x: x[0], reverse=True):
        text = text[:s] + text[e:]
    for f in [r'^\s*remind\s+(?:me\s+)?to\s+', r'^\s*reminder\s+']:
        text = re.sub(f, '', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip().strip('.,;:!? ')
    return text[0].upper() + text[1:] if text else text

def parse_nl_partial(text, tz=None):
    rel = find_relative(text, tz)
    if rel:
        ds_rel, ts_rel, rs, re_ = rel
        rr = find_repeat(text, tz)
        spans = [(rs, re_)]
        if rr:
            spans.append((rr[1], rr[2]))
        msg = clean_text(text, spans)
        return {'message': msg, 'date': ds_rel, 'time': ts_rel, 'repeat': rr[0] if rr else None} if msg else None

    tr, dr, rr = find_time(text), find_date(text, tz), find_repeat(text, tz)
    spans = []
    if tr:
        spans.append((tr[1], tr[2]))
    if dr:
        spans.append((dr[1], dr[2]))
    if rr:
        spans.append((rr[1], rr[2]))
    msg = clean_text(text, spans)
    return {'message': msg, 'date': dr[0] if dr else None, 'time': tr[0] if tr else None, 
            'repeat': rr[0] if rr else None} if msg else None

# ============= ARCHIVE FUNCTIONS ===========
def save_digest_archive(uid, date_str, count, text):
    try:
        digest_archive_sheet.append_row([str(uid), date_str, count, text], value_input_option="RAW")
    except Exception as e:
        logger.error(f"[ARCHIVE] Digest save failed: {e}")

def save_weekly_archive(uid, week_start, week_end, done, missed, total, text):
    try:
        weekly_archive_sheet.append_row([str(uid), week_start, week_end, done, missed, total, text], value_input_option="RAW")
    except Exception as e:
        logger.error(f"[ARCHIVE] Weekly save failed: {e}")

def get_digest_archive(uid, limit=30):
    try:
        rows = digest_archive_sheet.get_all_values()
        return sorted([(r[1], r[2], r[3]) for r in rows[1:] if r and str(r[0]) == str(uid)], 
                     key=lambda x: x[0], reverse=True)[:limit]
    except Exception:
        return []

def get_weekly_archive(uid, limit=12):
    try:
        rows = weekly_archive_sheet.get_all_values()
        return sorted([(r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows[1:] if r and str(r[0]) == str(uid)], 
                     key=lambda x: x[0], reverse=True)[:limit]
    except Exception:
        return []

# ============= SCHEDULE VIEWS =============
def get_user_reminders(uid):
    try:
        rows = sheet.get_all_values()
        return [r for r in rows[1:] if len(r) >= 6 and str(r[0]) == str(uid) and not (len(r) > 7 and str(r[7]).strip())]
    except Exception:
        return []

def expand_recur(reminders, start_date, end_date):
    expanded = []
    for r in reminders:
        try:
            rem_date = datetime.strptime(norm_date(r[2]), "%Y-%m-%d").date()
        except Exception:
            continue
        msg, ts = str(r[1]).strip(), norm_time(r[3])
        rep = str(r[4]).strip() if len(r) > 4 else "none"
        st = str(r[5]).strip() if len(r) > 5 else "active"
        
        if rep == "none" or not rep:
            if start_date <= rem_date <= end_date:
                expanded.append({"date": rem_date, "msg": msg, "time": ts, "rep": rep, "status": st})
        elif rep == "daily":
            d = max(rem_date, start_date)
            while d <= end_date:
                expanded.append({"date": d, "msg": msg, "time": ts, "rep": rep, "status": st if d == rem_date else "active"})
                d += timedelta(days=1)
        elif rep == "weekly":
            d = rem_date
            while d <= end_date:
                if d >= start_date:
                    expanded.append({"date": d, "msg": msg, "time": ts, "rep": rep, "status": st if d == rem_date else "active"})
                d += timedelta(days=7)
        elif rep.startswith("custom:"):
            cdays = rep.split(":")[1].split(",") if ":" in rep else []
            d = max(rem_date, start_date)
            while d <= end_date:
                if d.strftime("%a").lower()[:3] in cdays:
                    expanded.append({"date": d, "msg": msg, "time": ts, "rep": rep, "status": st if d == rem_date else "active"})
                d += timedelta(days=1)
    return expanded

async def show_today_view(target, uid, ctx, new=False):
    utz = get_tz(uid)
    now = datetime.now(utz)
    today = now.date()
    
    reminders = get_user_reminders(uid)
    expanded = sorted(expand_recur(reminders, today, today), key=lambda x: x["time"])
    
    lines = [f"📅 <b>Today — {now.strftime('%-d %b')}</b>\n{DIV}\n"]
    if expanded:
        for item in expanded:
            ic = ST_IC.get(item["status"], "○")
            rep_suffix = f" · {fmt_rep(item['rep'])}" if item["rep"] != "none" else ""
            short = item["msg"][:30] + "…" if len(item["msg"]) > 30 else item["msg"]
            lines.append(f"{ic} {fmt_time(item['time'])} · {short}{rep_suffix}")
        lines.append(f"\n{len(expanded)} reminder{'s' if len(expanded) != 1 else ''} today")
    else:
        lines.append("No reminders today. Enjoy!")
    
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    
    btns = [
        [InlineKeyboardButton("◂ Yesterday", callback_data=f"sched_day_{yesterday}"), 
         InlineKeyboardButton("Tomorrow ▸", callback_data=f"sched_day_{tomorrow}")],
        [InlineKeyboardButton("📆 Week", callback_data="sched_week"),
         InlineKeyboardButton("📊 Month", callback_data="sched_month"),
         InlineKeyboardButton("📋 All", callback_data="sched_all")]
    ]
    
    txt = "\n".join(lines)
    if new:
        sent = await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        schedule_minimize(ctx, sent, f"<b>📅 Today</b> ({len(expanded)})", "sched_today")
    else:
        await safe_edit(target, txt, InlineKeyboardMarkup(btns))

async def show_all_view(target, uid, ctx, new=False):
    try:
        rows = sheet.get_all_records()
        items = [(i, r) for i, r in enumerate(rows, 2)
                 if str(r.get("user_id", "")) == str(uid) 
                 and str(r.get("status", "")).strip() in ("active", "pending", "snoozed")
                 and not str(r.get("group_id", "")).strip()]
    except Exception:
        items = []
    
    if not items:
        txt = f"{hdr('All Reminders')}\nNo reminders found."
        btns = [[InlineKeyboardButton("📅 Today", callback_data="sched_today")]]
    else:
        lines = [hdr("All Reminders")]
        for idx, (ri, r) in enumerate(items, 1):
            st, msg = str(r.get("status", "")), str(r.get("message", ""))
            short = msg[:30] + "…" if len(msg) > 30 else msg
            lines.append(f"\n<b>{idx}</b> {ST_IC.get(st, '?')} {short}\n   {fmt_date(norm_date(r.get('date', '')))} · {fmt_time(norm_time(r.get('time', '')))}")
        txt = "\n".join(lines)
        
        num_btns = [InlineKeyboardButton(str(idx), callback_data=f"view_{ri}") 
                    for idx, (ri, _) in enumerate(items, 1)]
        btns = [num_btns[i:i+5] for i in range(0, len(num_btns), 5)]
        btns.append([InlineKeyboardButton("📅 Today", callback_data="sched_today"),
                     InlineKeyboardButton("📆 Week", callback_data="sched_week"),
                     InlineKeyboardButton("📊 Month", callback_data="sched_month")])
    
    if new:
        sent = await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        schedule_minimize(ctx, sent, f"<b>📋 All</b> ({len(items)})", "sched_all")
    else:
        await safe_edit(target, txt, InlineKeyboardMarkup(btns))

# ============= STATS VIEWS =================
async def show_history_hub(target, uid, ctx, new=False):
    utz = get_tz(uid)
    now = datetime.now(utz)
    
    try:
        rem_rows = sheet.get_all_values()
        uid_s = str(uid)
        
        month_start = now.replace(day=1).strftime("%Y-%m-%d")
        month_end = now.strftime("%Y-%m-%d")
        month_done = sum(1 for r in rem_rows[1:] if len(r) >= 6 and str(r[0]) == uid_s 
                        and not (len(r) > 7 and str(r[7]).strip())
                        and month_start <= norm_date(r[2]) <= month_end and str(r[5]).strip() == "done")
        month_total = sum(1 for r in rem_rows[1:] if len(r) >= 6 and str(r[0]) == uid_s 
                         and not (len(r) > 7 and str(r[7]).strip())
                         and month_start <= norm_date(r[2]) <= month_end and str(r[5]).strip() in ("done", "missed"))
        
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        week_done = sum(1 for r in rem_rows[1:] if len(r) >= 6 and str(r[0]) == uid_s 
                       and not (len(r) > 7 and str(r[7]).strip())
                       and week_start <= norm_date(r[2]) <= month_end and str(r[5]).strip() == "done")
        week_total = sum(1 for r in rem_rows[1:] if len(r) >= 6 and str(r[0]) == uid_s 
                        and not (len(r) > 7 and str(r[7]).strip())
                        and week_start <= norm_date(r[2]) <= month_end and str(r[5]).strip() in ("done", "missed"))
        
        today = now.strftime("%Y-%m-%d")
        today_pending = sum(1 for r in rem_rows[1:] if len(r) >= 6 and str(r[0]) == uid_s 
                           and not (len(r) > 7 and str(r[7]).strip())
                           and norm_date(r[2]) == today and str(r[5]).strip() in ("active", "pending", "snoozed"))
        
        month_pct = f"{month_done}/{month_total} ({round(month_done/month_total*100) if month_total else 0}%)"
        week_pct = f"{week_done}/{week_total} ({round(week_done/week_total*100) if week_total else 0}%)"
    except Exception:
        month_pct = week_pct = "—"
        today_pending = 0
    
    txt = (
        f"{hdr('Stats & Reports')}\n\n"
        f"📅 Daily Digests ▸\n"
        f"📊 Weekly Reports ▸\n\n"
        f"{DIV}\n"
        f"Current Performance:\n"
        f"This Month: {month_pct}\n"
        f"This Week: {week_pct}\n"
        f"Today: {today_pending} pending"
    )
    
    btns = [
        [InlineKeyboardButton("📅 Daily Digests", callback_data="hist_digest")],
        [InlineKeyboardButton("📊 Weekly Reports", callback_data="hist_weekly")],
        [InlineKeyboardButton("🔴", callback_data="stats_close")]
    ]
    
    if new:
        sent = await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        schedule_minimize(ctx, sent, "<b>📈 Stats</b>", "stats_home")
    else:
        await safe_edit(target, txt, InlineKeyboardMarkup(btns))

async def show_digest_archive(target, uid, ctx):
    digests = get_digest_archive(uid, limit=30)
    
    if not digests:
        txt = f"{hdr('Daily Digests')}\n\nDigest archive will appear after daily digests are sent.\n\nEnable digest in /settings!"
        btns = [[InlineKeyboardButton("« Back", callback_data="stats_home")]]
        await safe_edit(target, txt, InlineKeyboardMarkup(btns))
        return
    
    utz = get_tz(uid)
    now = datetime.now(utz)
    
    lines = [hdr("Daily Digests")]
    btns = []
    for date_str, count, _ in digests[:10]:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            label = "Today" if d.date() == now.date() else d.strftime("%a %-d %b")
            lines.append(f"{label} · {count} reminders")
            btns.append([InlineKeyboardButton(label, callback_data=f"hist_digest_{date_str}")])
        except Exception:
            pass
    
    btns.append([InlineKeyboardButton("« Back", callback_data="stats_home")])
    await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup(btns))

async def show_weekly_archive(target, uid, ctx):
    weeks = get_weekly_archive(uid, limit=12)
    
    if not weeks:
        # Generate current week on-demand
        utz = get_tz(uid)
        now = datetime.now(utz)
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        week_end = now.strftime("%Y-%m-%d")
        
        try:
            rem_rows = sheet.get_all_values()
            uid_s = str(uid)
            done = sum(1 for r in rem_rows[1:] if len(r) >= 6 and str(r[0]) == uid_s 
                      and not (len(r) > 7 and str(r[7]).strip())
                      and week_start <= norm_date(r[2]) <= week_end and str(r[5]).strip() == "done")
            missed = sum(1 for r in rem_rows[1:] if len(r) >= 6 and str(r[0]) == uid_s 
                        and not (len(r) > 7 and str(r[7]).strip())
                        and week_start <= norm_date(r[2]) <= week_end and str(r[5]).strip() == "missed")
            total = done + missed
            
            if total > 0:
                weeks = [(week_start, week_end, str(done), str(missed), str(total), "")]
            else:
                txt = f"{hdr('Weekly Reports')}\n\nNo weekly data yet.\nComplete reminders to see reports!"
                await safe_edit(target, txt, InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="stats_home")]]))
                return
        except Exception:
            txt = f"{hdr('Weekly Reports')}\n\nNo data available."
            await safe_edit(target, txt, InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="stats_home")]]))
            return
    
    utz = get_tz(uid)
    now = datetime.now(utz)
    current_week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    
    lines = [hdr("Weekly Reports")]
    btns = []
    for idx, (week_start, week_end, done, missed, total, _) in enumerate(weeks[:12], 1):
        try:
            ws = datetime.strptime(week_start, "%Y-%m-%d")
            we = datetime.strptime(week_end, "%Y-%m-%d")
            label = f"W{idx} ({ws.strftime('%-d %b')}–{we.strftime('%-d %b')})"
            marker = " ◂" if week_start == current_week_start else ""
            lines.append(f"{label}{marker}")
            btns.append([InlineKeyboardButton(f"{label}{marker}", callback_data=f"hist_weekly_{week_start}")])
        except Exception:
            pass
    
    btns.append([InlineKeyboardButton("« Back", callback_data="stats_home")])
    await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup(btns))

async def show_weekly_detail(target, uid, ctx, week_start):
    weeks = get_weekly_archive(uid, limit=12)
    week_data = next((w for w in weeks if w[0] == week_start), None)
    
    if not week_data:
        txt = f"{hdr('Weekly Report')}\n\nReport not found."
        await safe_edit(target, txt, InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="hist_weekly")]]))
        return
    
    _, week_end, done, missed, total, report_text = week_data
    
    if not report_text:
        try:
            pct = round(int(done) / int(total) * 100) if int(total) else 0
            ws_date = datetime.strptime(week_start, "%Y-%m-%d")
            we_date = datetime.strptime(week_end, "%Y-%m-%d")
            mot = "Outstanding! 🏆" if pct >= 90 else ("Keep it up! 💪" if pct >= 70 else "Room to improve 📈")
            txt = (
                f"📊 <b>Weekly Report</b>\n{DIV}\n{ws_date.strftime('%-d %b')} — {we_date.strftime('%-d %b')}\n\n"
                f"✅ Completed: {done}/{total} ({pct}%)\n"
                f"❌ Missed: {missed}\n\n{mot}"
            )
        except Exception:
            txt = f"{hdr('Weekly Report')}\n{week_start} — {week_end}\n\nNo data."
    else:
        txt = report_text
    
    await safe_edit(target, txt, InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="hist_weekly")]]))

# ============= SAVE FUNCTIONS =============
async def save_reminder(target, uid, ud, msg, date, time_str, edit_msg=False):
    rep = ud.get("repeat", "none")
    sheet.append_row([uid, msg, date, time_str, rep, "active", 0, "", ""], value_input_option="RAW")
    try:
        row = len(sheet.get_all_values())
    except Exception:
        row = 0
    ud.clear()
    txt = f"{hdr('✓ Saved')}\n{detail(msg, date, time_str, fmt_rep(rep))}"
    kb = saved_kb(row, rep) if row > 0 else home_kb()
    if edit_msg:
        await safe_edit(target, txt, kb)
        save_home(ud, target)
    else:
        sent = await target.reply_text(txt, reply_markup=kb, parse_mode="HTML")
        save_home(ud, sent)

async def finish_group_remind(target, ctx, uid, ud, rep, edit_msg=False):
    msg, ds, ts = ud.get("message", ""), ud.get("date", ""), ud.get("time", "")
    gid, name, tags, tid = ud.get("g_chat", ""), ud.get("g_name", "User"), ud.get("g_tags"), gen_tid()

    sheet.append_row([uid, msg, ds, ts, rep, "active", 0, gid, tid], value_input_option="RAW")
    subs = get_gsubs(gid)

    if tags:
        tagged_names = []
        for sub_uid, sub_name, sub_uname in subs:
            matched = any(tag == sub_uname.lower() and sub_uname for tag in tags) or any(tag == sub_name.lower() for tag in tags)
            if matched:
                add_tmember(tid, sub_uid, sub_name, "waiting")
                tagged_names.append(sub_name)
            else:
                add_tmember(tid, sub_uid, sub_name, "skipped")
        sub_info = f"For: {', '.join(tagged_names)}" if tagged_names else "No matching subscribers"
    else:
        for sub_uid, sub_name, _ in subs:
            add_tmember(tid, sub_uid, sub_name)
        sub_info = gsub_text(tid)

    txt = f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, fmt_rep(rep))}\nBy {name}\n\n{sub_info}"
    kb = gjoin_kb(tid, rep == "none")
    ud.clear()

    if edit_msg:
        await safe_edit(target, txt, kb)
        ctx.bot_data[f"gm_{tid}"] = {"c": str(target.chat.id), "m": target.message_id}
    else:
        sent = await target.reply_text(txt, reply_markup=kb, parse_mode="HTML")
        ctx.bot_data[f"gm_{tid}"] = {"c": str(target.chat.id), "m": sent.message_id}

async def handle_nl_result(target, ctx, uid, ud, msg, ts, ds, utz, is_group=False):
    if ts:
        if not ds:
            ds = datetime.now(utz).strftime("%Y-%m-%d")
        if is_past(ds, ts, utz):
            ud["step"] = "g_date" if is_group else "date"
            back_cb = "gcancel" if is_group else "cancel"
            now = datetime.now(utz)
            sent = await target.reply_text(
                f"{hdr('Group Reminder' if is_group else 'New Reminder')}\n{msg}\n\n{past_msg(ts)}\nPick a future date:",
                reply_markup=cal_kb(now.year, now.month, back_cb, tz=utz), parse_mode="HTML")
            save_p(ud, sent)
        else:
            ud["date"] = ds
            if is_group:
                await finish_group_remind(target, ctx, uid, ud, ud.get("repeat", "none"))
            else:
                await save_reminder(target, uid, ud, msg, ds, ts)
    elif ds:
        ud["date"], ud["step"] = ds, "g_time" if is_group else "time"
        sent = await target.reply_text(
            f"{hdr('Group Reminder' if is_group else 'New Reminder')}\n{msg}\n{fmt_date(ds)}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM</i>",
            reply_markup=ForceReply(selective=True) if is_group else cancel_kb(), parse_mode="HTML")
        save_p(ud, sent)
    else:
        ud["step"] = "g_date" if is_group else "date"
        now = datetime.now(utz)
        sent = await target.reply_text(
            f"{hdr('Group Reminder' if is_group else 'New Reminder')}\n{msg}\n\nPick a date:",
            reply_markup=cal_kb(now.year, now.month, "gcancel" if is_group else "cancel", tz=utz), parse_mode="HTML")
        save_p(ud, sent)

# ============= COMMANDS ===================
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("add", "New reminder"),
        BotCommand("schedule", "View reminders"),
        BotCommand("stats", "Reports & history"),
        BotCommand("settings", "Bot settings"),
    ], scope=BotCommandScopeAllPrivateChats())
    await app.bot.set_my_commands([
        BotCommand("start", "Bot info"),
        BotCommand("remind", "Group reminder"),
        BotCommand("list", "Active reminders"),
    ], scope=BotCommandScopeAllGroupChats())

GRP_START = (
    f"{hdr('RemindX')}\n\n"
    "<b>Commands</b>\n/remind — Group reminder\n/list — Active reminders\n\n"
    "<b>Examples</b>\n<code>/remind Buy milk at 5pm</code>\n"
    "<code>/remind Meeting tomorrow 10am daily</code>\n\n"
    "<i>Tag members:</i> <code>/remind @user Task at 5pm</code>"
)

def build_grp_list_text(gid):
    try:
        rows = sheet.get_all_values()
        items = [(i, r) for i, r in enumerate(rows[1:], 2)
                 if len(r) > 7 and str(r[7]).strip() == str(gid) and str(r[5]).strip() in ("active", "pending", "snoozed")]
        if not items:
            return None
        lines = [hdr("Group Reminders")]
        for idx, (ri, r) in enumerate(items, 1):
            short = str(r[1]).strip()[:30] + "…" if len(str(r[1]).strip()) > 30 else str(r[1]).strip()
            lines.append(f"\n<b>{idx}</b> {ST_IC.get(str(r[5]).strip(), '?')} {short}\n   {fmt_date(norm_date(r[2]))} · {fmt_time(norm_time(r[3]))}")
        return "\n".join(lines)
    except Exception:
        return None

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        gid, user = str(update.effective_chat.id), update.effective_user
        set_gsub(gid, user.id, user.first_name or "User", get_username(user), True)
        sent = await update.message.reply_text(GRP_START, reply_markup=gclose_kb(), parse_mode="HTML")
        ctx.bot_data[f"gmin_{sent.message_id}"] = {"min_text": "<b>RemindX</b>", 
                                                     "show_cb": f"gshow_start_{sent.message_id}", "full_text": GRP_START}
        schedule_minimize(ctx, sent, "<b>RemindX</b>", f"gshow_start_{sent.message_id}")
        return
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    get_cfg(update.effective_user.id)
    update_username(update.effective_user.id, get_username(update.effective_user))
    sent = await update.message.reply_text(HOME_TEXT, reply_markup=home_kb(), parse_mode="HTML")
    save_home(ctx.user_data, sent)

async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /remind for group reminders.")
        return
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    update_username(update.effective_user.id, get_username(update.effective_user))
    ctx.user_data["step"] = "message"
    sent = await update.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
    save_p(ctx.user_data, sent)

async def schedule_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    update_username(update.effective_user.id, get_username(update.effective_user))
    await show_today_view(update.message, update.effective_user.id, ctx, new=True)

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    update_username(update.effective_user.id, get_username(update.effective_user))
    await show_history_hub(update.message, update.effective_user.id, ctx, new=True)

async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        gid, user = str(update.effective_chat.id), update.effective_user
        set_gsub(gid, user.id, user.first_name or "User", get_username(user), True)
        list_text = build_grp_list_text(gid)
        if not list_text:
            sent = await update.message.reply_text(f"{hdr('Group Reminders')}\nNo active reminders.", 
                                                   reply_markup=gclose_kb(), parse_mode="HTML")
            ctx.bot_data[f"gmin_{sent.message_id}"] = {"min_text": "<b>Group Reminders</b>", 
                                                        "show_cb": f"gshow_list_{gid}_{sent.message_id}"}
            schedule_minimize(ctx, sent, "<b>Group Reminders</b>", f"gshow_list_{gid}_{sent.message_id}")
        else:
            sent = await update.message.reply_text(list_text, reply_markup=gclose_kb(), parse_mode="HTML")
            ctx.bot_data[f"gmin_{sent.message_id}"] = {"min_text": "<b>Group Reminders</b>", 
                                                        "show_cb": f"gshow_list_{gid}_{sent.message_id}"}
            schedule_minimize(ctx, sent, "<b>Group Reminders</b>", f"gshow_list_{gid}_{sent.message_id}")
        return
    await show_all_view(update.message, update.effective_user.id, ctx, new=True)

async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Use /remind in groups.\nJust type naturally for personal reminders.")
        return
    ud = ctx.user_data
    ud.clear()
    uid, utz = update.effective_user.id, get_tz(update.effective_user.id)
    user = update.effective_user
    gid, name, uname = str(update.effective_chat.id), user.first_name or "User", get_username(user)
    ud["g_chat"], ud["g_name"] = gid, name
    set_gsub(gid, uid, name, uname, True)

    tags = extract_tags(update.message)
    if tags:
        ud["g_tags"] = tags

    text = re.sub(r'^/remind(@\w+)?\s*', '', (update.message.text or "").strip(), flags=re.I).strip()
    text = strip_mentions(text, update.message)

    if not text:
        ud["step"] = "g_message"
        sent = await update.message.reply_text(
            f"{hdr('Group Reminder')}\nType your reminder:\n<i>↩️ Reply to this message</i>",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Type your reminder..."),
            parse_mode="HTML")
        save_p(ud, sent)
        return

    result = parse_nl_partial(text, tz=utz)
    if result and result.get('message'):
        msg = result['message']
        ud["message"] = msg
        if result.get('time'):
            ud["time"] = result['time']
        if result.get('repeat'):
            ud["repeat"] = result['repeat']
        await handle_nl_result(update.message, ctx, uid, ud, msg, result.get('time'), result.get('date'), utz, is_group=True)
    else:
        ud["message"], ud["step"] = text, "g_date"
        now = datetime.now(utz)
        await update.message.reply_text(
            f"{hdr('Group Reminder')}\n{text}\n\nPick a date:",
            reply_markup=cal_kb(now.year, now.month, "gcancel", tz=utz), parse_mode="HTML")

async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    update_username(update.effective_user.id, get_username(update.effective_user))
    
    cfg = get_cfg(update.effective_user.id)
    d_on, d_time = cfg["digest_on"], fmt_time(cfg["digest_time"]) if cfg["digest_on"] else "—"
    tz_disp = tz_label(cfg.get("timezone", DEF_TZ))
    
    txt = (
        f"{hdr('Settings')}\n\n"
        f"<b>Digest</b>: {'ON' if d_on else 'OFF'}" + (f" · {d_time}" if d_on else "") +
        f"\n<b>Retries</b>: {cfg['max_retries']}×"
        f"\n<b>Gap</b>: {cfg['retry_gap']} min"
        f"\n<b>Timezone</b>: {tz_disp}"
    )
    
    btns = [
        [InlineKeyboardButton(f"Digest: {'ON' if d_on else 'OFF'}", callback_data="cfg_digest_toggle")],
        [InlineKeyboardButton(f"🌍 {tz_disp}", callback_data="cfg_tz")],
        [InlineKeyboardButton("🔴", callback_data="cfg_close")],
    ]
    
    sent = await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
    schedule_minimize(ctx, sent, "<b>⚙️ Settings</b>", "cfg_close")

# ============= TEXT HANDLER ===============
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    step = ctx.user_data.get("step", "")
    text = update.message.text.strip()

    if update.effective_chat.type == "private":
        update_username(update.effective_user.id, get_username(update.effective_user))

    if update.effective_chat.type != "private":
        if step.startswith("g_") and str(update.effective_chat.id) == str(ctx.user_data.get("g_chat", "")):
            await _do_step(update, ctx, step, text)
        return

    if step:
        await _do_step(update, ctx, step, text)
    else:
        await _try_nl(update, ctx, text)

async def _try_nl(update, ctx, text):
    uid, utz = update.effective_user.id, get_tz(update.effective_user.id)
    result = parse_nl_partial(text, tz=utz)
    
    msg = result['message'] if result and result.get('message') else text.strip()
    
    if not msg or msg.lower() in IGNORE_WORDS:
        return

    ts, ds, rep = (result.get('time'), result.get('date'), result.get('repeat')) if result else (None, None, None)
    has_prefix = bool(re.search(r'(?:remind|reminder|remember|don.?t\s+forget)', text, re.I))
    
    if ts or ds or has_prefix:
        ud = ctx.user_data
        await rm_home(ctx, ud)
        ud.clear()
        ud["message"] = msg
        if ts:
            ud["time"] = ts
        if rep:
            ud["repeat"] = rep
        await handle_nl_result(update.message, ctx, uid, ud, msg, ts, ds, utz)

async def _do_step(update, ctx, step, text):
    ud, uid, utz = ctx.user_data, update.effective_user.id, get_tz(update.effective_user.id)

    if step == "message":
        await del_prompt(ctx, ud)
        result = parse_nl_partial(text, tz=utz)
        if result and (result.get('time') or result.get('date')):
            msg = result['message']
            ud["message"] = msg
            if result.get('time'):
                ud["time"] = result['time']
            if result.get('repeat'):
                ud["repeat"] = result['repeat']
            await handle_nl_result(update.message, ctx, uid, ud, msg, result.get('time'), result.get('date'), utz)
        else:
            msg = result['message'] if result else text
            ud["message"], ud["step"] = msg, "date"
            now = datetime.now(utz)
            sent = await update.message.reply_text(
                f"{hdr('New Reminder')}\n{msg}\n\nPick a date:",
                reply_markup=cal_kb(now.year, now.month, tz=utz), parse_mode="HTML")
            save_p(ud, sent)
    elif step == "time":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text("Invalid time.\n<i>e.g. 9pm, 9:30 PM</i>", parse_mode="HTML")
            return
        ds = ud.get("date", "")
        if is_past(ds, parsed, utz):
            await update.message.reply_text(f"{past_msg(parsed)}\nEnter a future time:", parse_mode="HTML")
            return
        await del_prompt(ctx, ud)
        await save_reminder(update.message, uid, ud, ud.get("message", ""), ds, parsed)
    elif step == "g_message":
        await del_prompt(ctx, ud)
        result = parse_nl_partial(text, tz=utz)
        if result and (result.get('time') or result.get('date')):
            msg = result['message']
            ud["message"] = msg
            if result.get('time'):
                ud["time"] = result['time']
            if result.get('repeat'):
                ud["repeat"] = result['repeat']
            await handle_nl_result(update.message, ctx, uid, ud, msg, result.get('time'), result.get('date'), utz, is_group=True)
        else:
            msg = result['message'] if result else text
            ud["message"], ud["step"] = msg, "g_date"
            now = datetime.now(utz)
            sent = await update.message.reply_text(
                f"{hdr('Group Reminder')}\n{msg}\n\nPick a date:",
                reply_markup=cal_kb(now.year, now.month, "gcancel", tz=utz), parse_mode="HTML")
            save_p(ud, sent)
    elif step == "g_time":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text("Invalid time.\n<i>e.g. 9pm, 9:30 PM</i>", parse_mode="HTML")
            return
        ds = ud.get("date", "")
        if is_past(ds, parsed, utz):
            await update.message.reply_text(f"{past_msg(parsed)}\nEnter a future time:", parse_mode="HTML")
            return
        await del_prompt(ctx, ud)
        ud["time"] = parsed
        await finish_group_remind(update.message, ctx, uid, ud, ud.get("repeat", "none"))


# ============= BUTTON HANDLERS ===========
async def on_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data, ud, uid = q.data, ctx.user_data, q.from_user.id
    
    if data == "noop":
        return

    if update.effective_chat.type == "private":
        update_username(uid, get_username(q.from_user))

    # Stats navigation
    if data == "stats_home":
        await show_history_hub(q.message, uid, ctx)
        return
    if data == "stats_close":
        await safe_edit(q.message, "<b>📈 Stats</b>", InlineKeyboardMarkup([[InlineKeyboardButton("📋 Show", callback_data="stats_home")]]))
        return

    # Schedule navigation
    if data == "sched_today":
        await show_today_view(q.message, uid, ctx)
        return
    if data == "sched_week":
        utz = get_tz(uid)
        now = datetime.now(utz)
        week_start = (now - timedelta(days=now.weekday())).date()
        await show_week_view(q.message, uid, ctx, week_start)
        return
    if data.startswith("sched_day_"):
        date_str = data[10:]
        await show_day_detail(q.message, uid, ctx, date_str)
        return
    if data == "sched_month":
        utz = get_tz(uid)
        now = datetime.now(utz)
        await show_month_view(q.message, uid, ctx, now.year, now.month)
        return
    if data == "sched_all":
        await show_all_view(q.message, uid, ctx)
        return

    # History navigation
    if data == "hist_digest":
        await show_digest_archive(q.message, uid, ctx)
        return
    if data.startswith("hist_digest_"):
        date_str = data[12:]
        digests = get_digest_archive(uid, limit=30)
        digest_data = next((d for d in digests if d[0] == date_str), None)
        if digest_data:
            txt = f"☀️ <b>Daily Digest</b>\n{digest_data[0]}\n{DIV}\n\n{digest_data[2]}"
            await safe_edit(q.message, txt, InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="hist_digest")]]))
        return
    if data == "hist_weekly":
        await show_weekly_archive(q.message, uid, ctx)
        return
    if data.startswith("hist_weekly_"):
        week_start = data[12:]
        await show_weekly_detail(q.message, uid, ctx, week_start)
        return

    # Basic navigation
    if data in ("home", "cancel"):
        ud.clear()
        sent = await q.message.reply_text(HOME_TEXT, reply_markup=home_kb(), parse_mode="HTML")
        save_home(ud, sent)
        return
    if data == "gcancel":
        ud.clear()
        await safe_edit(q.message, f"{hdr('Group Reminder')}\nCancelled.")
        return
    if data == "gclose":
        mid = str(q.message.message_id)
        stored = ctx.bot_data.get(f"gmin_{mid}")
        if stored:
            await safe_edit(q.message, stored["min_text"], gmin_kb(stored["show_cb"]))
        return
    if data.startswith("gshow_"):
        if data.startswith("gshow_start_"):
            await safe_edit(q.message, GRP_START, gclose_kb())
        elif data.startswith("gshow_list_"):
            parts = data[11:].split("_")
            gid = parts[0] if parts else str(q.message.chat.id)
            list_text = build_grp_list_text(gid)
            if list_text:
                await safe_edit(q.message, list_text, gclose_kb())
            else:
                await safe_edit(q.message, f"{hdr('Group Reminders')}\nNo active reminders.", gclose_kb())
        return

    # Repeat change
    if data.startswith("chrep_"):
        row = int(data[6:])
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message, f"{hdr('✓ Saved')}\n{detail(msg, ds, ts)}\n\nRepeat?", rep_picker_kb(f"chrepv_{row}"))
        return
    if data.startswith("chrepv_"):
        parts = data.split("_")
        row, rep = int(parts[1]), parts[2]
        if rep == "custom":
            ud["custom_days_for"] = ("chrep", row)
            ud["custom_days_selected"] = []
            r, msg, ds, ts, rs = row_detail(row)
            await safe_edit(q.message, f"{detail(msg, ds, ts)}\n\nSelect days:", custom_days_kb([], f"chrep_{row}"))
            return
        r, msg, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 5, rep)
        await safe_edit(q.message, f"{hdr('Updated ✓')}\n{detail(msg, ds, ts, fmt_rep(rep))}", home_kb())
        save_home(ud, q.message)
        return

    # Custom days
    if data.startswith("cday_"):
        rest = data[5:]
        last_us = rest.rfind("_")
        if last_us < 0:
            return
        prefix, day_key = rest[:last_us], rest[last_us + 1:]
        selected = ud.get("custom_days_selected", [])
        
        if day_key == "weekdays":
            selected = ["mon", "tue", "wed", "thu", "fri"]
        elif day_key == "all":
            selected = list(DAY_KEYS)
        elif day_key == "clear":
            selected = []
        elif day_key in DAY_KEYS:
            if day_key in selected:
                selected.remove(day_key)
            else:
                selected.append(day_key)
            selected = [d for d in DAY_KEYS if d in selected]
        
        ud["custom_days_selected"] = selected
        info = ud.get("custom_days_for")
        if info:
            kind, key = info
            if kind == "chrep":
                r, msg, ds, ts, rs = row_detail(int(key))
                await safe_edit(q.message, f"{detail(msg, ds, ts)}\n\nSelect days:", custom_days_kb(selected, f"chrep_{key}"))
            elif kind == "gchrep":
                row, r = find_by_tid(key)
                if r:
                    msg, ds, ts, _ = get_detail(r)
                    await safe_edit(q.message, f"{detail(msg, ds, ts)}\n\nSelect days:", custom_days_kb(selected, f"gchrep_{key}"))
        return

    if data.startswith("cdaysave_"):
        selected = ud.get("custom_days_selected", [])
        if not selected:
            return
        rep_value = f"custom:{','.join(selected)}"
        info = ud.get("custom_days_for")
        if not info:
            return
        kind, key = info
        ud.pop("custom_days_for", None)
        ud.pop("custom_days_selected", None)
        
        if kind == "chrep":
            row = int(key)
            r, msg, ds, ts, rs = row_detail(row)
            sheet.update_cell(row, 5, rep_value)
            await safe_edit(q.message, f"{hdr('Updated ✓')}\n{detail(msg, ds, ts, fmt_rep(rep_value))}", home_kb())
            save_home(ud, q.message)
        elif kind == "gchrep":
            row, r = find_by_tid(key)
            if r:
                sheet.update_cell(row, 5, rep_value)
                msg, ds, ts, _ = get_detail(r)
                await safe_edit(q.message, f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, fmt_rep(rep_value))}\n\n{gsub_text(key)}", gjoin_kb(key))
        return

    # Group actions
    if data.startswith(("gjoin_", "gskip_", "gdone_", "gsnzp_", "gsnz_")):
        await _btn_group(q, ctx, uid, data)
        return

    # Calendar
    if data.startswith(("cal_", "day_")):
        await _btn_cal(q, ctx, ud, uid, data)
        return

    # Reminder actions
    if data.startswith(("view_", "snzp_", "snz_", "done_", "crem_", "undo_")):
        await _btn_rem(q, ctx, ud, uid, data)
        return

    # Edit
    if data.startswith("edit_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message, f"{hdr('Edit Reminder')}\n{detail(msg, ds, ts, rs)}\n\nWhat to change?",
                        InlineKeyboardMarkup([
                            [InlineKeyboardButton("Message", callback_data=f"emsg_{row}"), 
                             InlineKeyboardButton("Date", callback_data=f"edate_{row}"),
                             InlineKeyboardButton("Time", callback_data=f"etime_{row}")],
                            [InlineKeyboardButton("« Back", callback_data=f"view_{row}")],
                        ]))
        return

    # Settings
    if data.startswith("cfg_"):
        await _btn_cfg(q, ctx, ud, uid, data)
        return

async def _btn_group(q, ctx, uid, data):
    uid_s, user, uname = str(uid), q.from_user, get_username(q.from_user)

    if data.startswith("gjoin_"):
        tid = data[6:]
        row, r = find_by_tid(tid)
        if not r or str(r[5]) != "active":
            await q.answer("Already fired.", show_alert=True)
            return
        if not add_tmember(tid, uid_s, user.first_name or "User"):
            await q.answer("Already joined!", show_alert=True)
            return
        set_gsub(str(q.message.chat.id), uid_s, user.first_name or "User", uname, True)
        msg, ds, ts, rs = get_detail(r)
        await safe_edit(q.message, f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, rs)}\n\n{gsub_text(tid)}", gjoin_kb(tid))
        await q.answer("Joined ✓")
    elif data.startswith("gskip_"):
        tid = data[6:]
        set_gsub(str(q.message.chat.id), uid_s, user.first_name or "User", uname, True)
        ms = get_tmembers(tid)
        if any(str(u) == uid_s for u, _, _ in ms):
            set_tstatus(tid, uid_s, "skipped")
        else:
            add_tmember(tid, uid_s, user.first_name or "User", "skipped")
        row, r = find_by_tid(tid)
        if r:
            msg, ds, ts, rs = get_detail(r)
            await safe_edit(q.message, f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, rs)}\n\n{gsub_text(tid)}", gjoin_kb(tid))
        await q.answer("Skipped")
    elif data.startswith("gdone_"):
        tid = data[6:]
        ms = get_tmembers(tid)
        st = next((s for u, _, s in ms if str(u) == uid_s), None)
        if st and st != "pending":
            await safe_edit(q.message, f"<i>Already handled</i>")
            return
        set_tstatus(tid, uid_s, "done")
        await rm_gpm(ctx, tid, uid_s)
        row, r = find_by_tid(tid)
        msg = str(r[1]).strip() if r else ""
        await safe_edit(q.message, f"{msg}\n\n<b>Done</b> ✓")
        await update_gstatus(ctx, tid, msg)
        if row and r:
            await check_grp_resolved(ctx, tid, row, r)
            await update_gstatus(ctx, tid, msg)
    elif data.startswith("gsnzp_"):
        tid = data[6:]
        row, r = find_by_tid(tid)
        await safe_edit(q.message, f"{str(r[1]).strip() if r else ''}\n\nSnooze for:", snz_kb(tid, "gsnz"))
    elif data.startswith("gsnz_"):
        rest = data[5:]
        last_us = rest.rfind("_")
        if last_us < 0:
            return
        tid, mins = rest[:last_us], int(rest[last_us + 1:])
        set_tstatus(tid, uid_s, "snoozed")
        await rm_gpm(ctx, tid, uid_s)
        row, r = find_by_tid(tid)
        msg = str(r[1]).strip() if r else ""
        nt = datetime.now(get_tz(uid)) + timedelta(minutes=mins)
        await safe_edit(q.message, f"{msg}\n\n<b>Snoozed {fmt_snz(mins)}</b> → {fmt_time(nt.strftime('%H:%M'))}")
        await update_gstatus(ctx, tid, msg)
        ctx.job_queue.run_once(grp_snooze_cb, mins * 60, data={"tid": tid, "uid": uid, "uid_s": uid_s}, name=f"gsnz-{tid}-{uid_s}")

async def _btn_cal(q, ctx, ud, uid, data):
    utz = get_tz(uid)
    step = ud.get("step", "")

    if data.startswith("cal_"):
        parts = data[4:].split("_")
        yr, mo = int(parts[0]), int(parts[1])
        msg, ts = ud.get("message", ""), ud.get("time")
        if step == "g_date":
            await safe_edit(q.message, f"{hdr('Group Reminder')}\n{msg}\n\nPick a date:",
                            cal_kb(yr, mo, "gcancel", tz=utz))
        else:
            await safe_edit(q.message, f"{hdr('New Reminder')}\n{msg}\n\nPick a date:", cal_kb(yr, mo, tz=utz))

    elif data.startswith("day_"):
        ds = data[4:]
        if step == "g_date":
            ud["date"] = ds
            msg, ts = ud.get("message", ""), ud.get("time")
            if ts:
                if is_past(ds, ts, utz):
                    now = datetime.now(utz)
                    await safe_edit(q.message, f"{hdr('Group Reminder')}\n{msg}\n\n{past_msg(ts)}\nPick a future date:",
                                    cal_kb(now.year, now.month, "gcancel", tz=utz))
                else:
                    await finish_group_remind(q.message, ctx, uid, ud, ud.get("repeat", "none"), edit_msg=True)
            else:
                ud["step"] = "g_time"
                try:
                    await q.message.delete()
                except Exception:
                    pass
                sent = await ctx.bot.send_message(
                    chat_id=q.message.chat.id,
                    text=f"{hdr('Group Reminder')}\n{msg}\n{fmt_date(ds)}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM</i>",
                    reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 9pm"),
                    parse_mode="HTML")
                save_p(ud, sent)
        else:
            ud["date"] = ds
            msg, ts = ud.get("message", ""), ud.get("time")
            if ts:
                if is_past(ds, ts, utz):
                    now = datetime.now(utz)
                    await safe_edit(q.message, f"{hdr('New Reminder')}\n{msg}\n\n{past_msg(ts)}\nPick a future date:", cal_kb(now.year, now.month, tz=utz))
                else:
                    await save_reminder(q.message, uid, ud, msg, ds, ts, edit_msg=True)
            else:
                ud["step"] = "time"
                await safe_edit(q.message, f"{hdr('New Reminder')}\n{msg}\n{fmt_date(ds)}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM</i>", cancel_kb())
                save_p(ud, q.message)

async def _btn_rem(q, ctx, ud, uid, data):
    if data.startswith("view_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        st = r[5] if len(r) > 5 else "active"
        btns = [[InlineKeyboardButton("✎ Edit", callback_data=f"edit_{row}"), 
                 InlineKeyboardButton("✕ Cancel", callback_data=f"crem_{row}")],
                [InlineKeyboardButton("« Back", callback_data="sched_all")]]
        await safe_edit(q.message, f"{hdr('Reminder')}\n{msg}\n\n{fmt_date(ds)} · {fmt_time(ts)}\n{rs} · {ST_IC.get(st, '?')} {ST_LB.get(st, st)}", 
                       InlineKeyboardMarkup(btns))
    elif data.startswith("snzp_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\nSnooze for:", snz_kb(row))
    elif data.startswith("snz_"):
        parts = data[4:].split("_")
        row, mins = int(parts[0]), int(parts[1])
        r, msg, ds, ts, rs = row_detail(row)
        kill_jobs(ctx.job_queue, row)
        await rm_btns(ctx, row)
        utz = get_tz(uid)
        nt = datetime.now(utz) + timedelta(minutes=mins)
        rep = r[4] if len(r) > 4 else "none"
        if rep and rep != "none":
            sheet.update_cell(row, 6, "snoozed")
            sheet.update_cell(row, 7, 0)
            ctx.job_queue.run_once(snooze_cb, mins * 60, data={"row": row, "chat": uid}, name=f"snooze-{row}")
        else:
            sheet.update_cell(row, 3, nt.strftime("%Y-%m-%d"))
            sheet.update_cell(row, 4, nt.strftime("%H:%M"))
            sheet.update_cell(row, 6, "active")
            sheet.update_cell(row, 7, 0)
        await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\n<b>Snoozed {fmt_snz(mins)}</b> → {fmt_time(nt.strftime('%H:%M'))}")
    elif data.startswith("done_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        kill_jobs(ctx.job_queue, row)
        await rm_btns(ctx, row)
        if not advance_rep(row, r):
            sheet.update_cell(row, 6, "done")
            sheet.update_cell(row, 7, 0)
        await safe_edit(q.message, f"{detail(msg, ds, ts, rs)}\n\n<b>Done</b> ✓")
    elif data.startswith("crem_"):
        row = int(data[5:])
        kill_jobs(ctx.job_queue, row)
        r, msg, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 6, "cancelled")
        await rm_btns(ctx, row)
        await safe_edit(q.message, f"{detail(msg, ds, ts)}\n\n<b>Cancelled</b> ✕",
                        InlineKeyboardMarkup([[InlineKeyboardButton("↩ Undo", callback_data=f"undo_{row}")]]))
    elif data.startswith("undo_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 6, "active")
        await safe_edit(q.message, f"{hdr('Restored ✓')}\n{detail(msg, ds, ts, rs)}", home_kb())
        save_home(ud, q.message)

async def _btn_cfg(q, ctx, ud, uid, data):
    if data == "cfg_digest_toggle":
        cfg = get_cfg(uid)
        save_cfg(uid, "digest_on", str(not cfg["digest_on"]).lower())
        await safe_edit(q.message, f"{hdr('Settings')}\n\nDigest → <b>{'ON' if not cfg['digest_on'] else 'OFF'}</b>", home_kb())
    elif data == "cfg_tz":
        cfg = get_cfg(uid)
        btns = []
        row = []
        for region in TZ_REGIONS:
            row.append(InlineKeyboardButton(f"{TZ_ICONS.get(region, '🌐')} {region}", callback_data=f"tzr_{region}"))
            if len(row) == 2:
                btns.append(row)
                row = []
        if row:
            btns.append(row)
        btns.append([InlineKeyboardButton("🔴", callback_data="cfg_close")])
        await safe_edit(q.message, f"{hdr('Timezone')}\n\nCurrent: <b>{tz_short(cfg.get('timezone', DEF_TZ))}</b>\n\nSelect region:", 
                       InlineKeyboardMarkup(btns))
    elif data.startswith("tzr_"):
        cfg = get_cfg(uid)
        region = data[4:]
        btns, row = [], []
        for idx, (tz, country, offset, reg) in enumerate(TZ_DATA):
            if reg != region:
                continue
            lbl = f"[{country}]" if tz == cfg.get("timezone", DEF_TZ) else country
            row.append(InlineKeyboardButton(f"{lbl} {offset}", callback_data=f"tzs_{idx}"))
            if len(row) == 2:
                btns.append(row)
                row = []
        if row:
            btns.append(row)
        btns.append([InlineKeyboardButton("« Regions", callback_data="cfg_tz")])
        await safe_edit(q.message, f"{hdr('Timezone')}\n\n{TZ_ICONS.get(region, '🌐')} <b>{region}</b>", InlineKeyboardMarkup(btns))
    elif data.startswith("tzs_"):
        idx = int(data[4:])
        if 0 <= idx < len(TZ_DATA):
            tz_name, country, _, _ = TZ_DATA[idx]
            save_cfg(uid, "timezone", tz_name)
            await safe_edit(q.message, f"{hdr('Settings')}\n\nTimezone → <b>{country}</b>", home_kb())
    elif data == "cfg_close":
        await safe_edit(q.message, "<b>⚙️ Settings</b>", InlineKeyboardMarkup([[InlineKeyboardButton("📋 Show", callback_data="cfg_close")]]))

# ============= WEEK & DAY VIEWS =============
async def show_week_view(target, uid, ctx, week_start, new=False):
    utz = get_tz(uid)
    now = datetime.now(utz)
    week_end = week_start + timedelta(days=6)
    
    reminders = get_user_reminders(uid)
    expanded = expand_recur(reminders, week_start, week_end)
    
    by_date = {}
    for item in expanded:
        by_date.setdefault(item["date"], []).append(item)
    
    lines = [f"📆 <b>Week — {week_start.strftime('%-d %b')}–{week_end.strftime('%-d %b')}</b>\n{DIV}\n"]
    
    done_count = sum(1 for x in expanded if x["status"] == "done")
    missed_count = sum(1 for x in expanded if x["status"] == "missed")
    upcoming = len(expanded) - done_count - missed_count
    
    for i in range(7):
        current_day = week_start + timedelta(days=i)
        day_items = by_date.get(current_day, [])
        marker = " ◂" if current_day == now.date() else ""
        lines.append(f"{current_day.strftime('%a %-d')}{marker} · {len(day_items)} reminder{'s' if len(day_items) != 1 else ''}")
    
    if expanded:
        lines.append(f"\nTotal: {len(expanded)}")
        parts = []
        if done_count:
            parts.append(f"✅ {done_count}")
        if missed_count:
            parts.append(f"✗ {missed_count}")
        if upcoming:
            parts.append(f"○ {upcoming}")
        if parts:
            lines.append(" · ".join(parts))
    
    day_btns = [InlineKeyboardButton(DAY_NAMES[i], callback_data=f"sched_day_{(week_start + timedelta(days=i)).strftime('%Y-%m-%d')}") 
                for i in range(7)]
    
    btns = [day_btns[i:i+4] for i in range(0, 7, 4)]
    btns.append([InlineKeyboardButton("◂ Last", callback_data=f"sched_week_{(week_start - timedelta(days=7)).strftime('%Y-%m-%d')}"),
                 InlineKeyboardButton("Next ▸", callback_data=f"sched_week_{(week_start + timedelta(days=7)).strftime('%Y-%m-%d')}")])
    btns.append([InlineKeyboardButton("📅 Today", callback_data="sched_today"),
                 InlineKeyboardButton("📊 Month", callback_data="sched_month"),
                 InlineKeyboardButton("📋 All", callback_data="sched_all")])
    
    txt = "\n".join(lines)
    if new:
        sent = await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        schedule_minimize(ctx, sent, f"<b>📆 Week</b> ({len(expanded)})", f"sched_week_{week_start.strftime('%Y-%m-%d')}")
    else:
        await safe_edit(target, txt, InlineKeyboardMarkup(btns))

async def show_day_detail(target, uid, ctx, date_str):
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return
    
    utz = get_tz(uid)
    now = datetime.now(utz)
    reminders = get_user_reminders(uid)
    expanded = sorted(expand_recur(reminders, day, day), key=lambda x: x["time"])
    
    day_name = "Today" if day == now.date() else day.strftime("%A, %-d %b")
    lines = [f"<b>{day_name}</b>\n{DIV}\n"]
    
    if expanded:
        for idx, item in enumerate(expanded, 1):
            ic = ST_IC.get(item["status"], "○")
            rep_suffix = f" · {fmt_rep(item['rep'])}" if item["rep"] != "none" else ""
            lines.append(f"{idx}. {ic} {fmt_time(item['time'])} · {item['msg']}{rep_suffix}")
    else:
        lines.append("No reminders on this day.")
    
    btns = [[InlineKeyboardButton("« Week", callback_data=f"sched_week_{(day - timedelta(days=day.weekday())).strftime('%Y-%m-%d')}")]]
    await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup(btns))

async def show_month_view(target, uid, ctx, year, month, new=False):
    utz = get_tz(uid)
    now = datetime.now(utz)
    first_day = datetime(year, month, 1).date()
    last_day = datetime(year, month, cal_module.monthrange(year, month)[1]).date()
    
    reminders = get_user_reminders(uid)
    expanded = expand_recur(reminders, first_day, last_day)
    
    weeks = []
    d = first_day
    while d <= last_day:
        week_start = d
        week_end = min(d + timedelta(days=6 - d.weekday()), last_day)
        week_items = [x for x in expanded if week_start <= x["date"] <= week_end]
        weeks.append({"start": week_start, "end": week_end, "count": len(week_items)})
        d = week_end + timedelta(days=1)
    
    while len(weeks) > 4:
        w1, w2 = weeks[-2], weeks[-1]
        weeks[-2] = {"start": w1["start"], "end": w2["end"], "count": w1["count"] + w2["count"]}
        weeks.pop()
    
    month_name = cal_module.month_name[month]
    lines = [f"📊 <b>{month_name} {year}</b>\n{DIV}\n"]
    
    for i, w in enumerate(weeks):
        current = " ◂" if w["start"] <= now.date() <= w["end"] else ""
        lines.append(f"W{i + 1}: {w['start'].strftime('%-d %b')}–{w['end'].strftime('%-d %b')}{current} · {w['count']}")
    
    total = len(expanded)
    if total:
        done = sum(1 for x in expanded if x["status"] == "done")
        missed = sum(1 for x in expanded if x["status"] == "missed")
        lines.append(f"\nTotal: {total} · ✅ {done} · ✗ {missed}")
    
    week_btns = [InlineKeyboardButton(f"W{i + 1}", callback_data=f"sched_week_{w['start'].strftime('%Y-%m-%d')}") 
                 for i, w in enumerate(weeks)]
    btns = [week_btns]
    
    pm, py = ((month - 2) % 12) + 1, year - (1 if month == 1 else 0)
    nm, ny = (month % 12) + 1, year + (1 if month == 12 else 0)
    btns.append([InlineKeyboardButton("‹", callback_data=f"sched_month_{py}_{pm}"),
                 InlineKeyboardButton("›", callback_data=f"sched_month_{ny}_{nm}")])
    btns.append([InlineKeyboardButton("📅 Today", callback_data="sched_today"),
                 InlineKeyboardButton("📆 Week", callback_data="sched_week"),
                 InlineKeyboardButton("📋 All", callback_data="sched_all")])
    
    txt = "\n".join(lines)
    if new:
        sent = await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        schedule_minimize(ctx, sent, f"<b>📊 {month_name}</b> ({total})", f"sched_month_{year}_{month}")
    else:
        await safe_edit(target, txt, InlineKeyboardMarkup(btns))

# Handle month navigation callbacks
async def handle_sched_month(q, ctx, uid, data):
    parts = data[12:].split("_")
    yr, mo = int(parts[0]), int(parts[1])
    utz = get_tz(uid)
    await show_month_view(q.message, uid, ctx, yr, mo)

async def handle_sched_week(q, ctx, uid, data):
    date_str = data[11:]
    try:
        week_start = datetime.strptime(date_str, "%Y-%m-%d").date()
        await show_week_view(q.message, uid, ctx, week_start)
    except Exception:
        pass

# Add these to on_btn after sched_all
# if data.startswith("sched_month_"):
#     await handle_sched_month(q, ctx, uid, data)
#     return
# if data.startswith("sched_week_") and len(data) > 11:
#     await handle_sched_week(q, ctx, uid, data)
#     return

# ============= FIRE & RETRY ==============
async def send_and_track(ctx, chat_id, text, kb, track_key, track_cid):
    try:
        sent = await ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")
        ctx.bot_data[track_key] = {"c": track_cid, "m": sent.message_id}
        return True
    except Exception as e:
        logger.error(f"Send failed {chat_id}: {e}")
        return False

async def snooze_cb(ctx: ContextTypes.DEFAULT_TYPE):
    row, chat = ctx.job.data["row"], ctx.job.data["chat"]
    try:
        r = sheet.row_values(row)
        if not r or len(r) <= 5 or r[5] != "snoozed":
            return
        await rm_btns(ctx, row)
        if await send_and_track(ctx, chat, f"{str(r[1]).strip()}\n\n<b>⏰ Reminder</b>", act_kb(row), f"r_{row}", chat):
            sheet.update_cell(row, 6, "pending")
            sheet.update_cell(row, 7, 0)
            cfg = get_cfg(int(r[0]) if r[0].isdigit() else r[0])
            ctx.job_queue.run_once(auto_retry, cfg["retry_gap"] * 60, data={"row": row, "chat": chat}, name=f"retry-{row}")
    except Exception as e:
        logger.error(f"Snooze callback error: {e}")

async def auto_retry(ctx: ContextTypes.DEFAULT_TYPE):
    row, chat = ctx.job.data["row"], ctx.job.data["chat"]
    try:
        r = sheet.row_values(row)
        if not r or len(r) <= 5 or r[5] != "pending":
            return
        cfg = get_cfg(int(r[0]) if r[0].isdigit() else r[0])
        max_r, gap = cfg["max_retries"], cfg["retry_gap"]
        count = int(r[6]) if len(r) > 6 and r[6].isdigit() else 0
        
        if count >= max_r:
            if not advance_rep(row, r):
                sheet.update_cell(row, 6, "missed")
            return
        
        await rm_btns(ctx, row)
        nc = count + 1
        await send_and_track(ctx, chat, f"{str(r[1]).strip()}\n\n<b>Reminder</b> ({nc}/{max_r})", act_kb(row), f"r_{row}", chat)
        sheet.update_cell(row, 7, nc)
        
        if nc >= max_r:
            if not advance_rep(row, r):
                sheet.update_cell(row, 6, "missed")
        else:
            ctx.job_queue.run_once(auto_retry, gap * 60, data={"row": row, "chat": chat}, name=f"retry-{row}")
    except Exception as e:
        logger.error(f"Auto retry error: {e}")

async def grp_snooze_cb(ctx: ContextTypes.DEFAULT_TYPE):
    tid, uid, uid_s = ctx.job.data["tid"], ctx.job.data["uid"], ctx.job.data["uid_s"]
    st = next((s for u, _, s in get_tmembers(tid) if str(u) == uid_s), None)
    if st != "snoozed":
        return
    set_tstatus(tid, uid_s, "pending")
    row, r = find_by_tid(tid)
    if not r:
        return
    msg = str(r[1]).strip()
    await rm_gpm(ctx, tid, uid_s)
    if not await send_and_track(ctx, uid, f"{msg}\n\n<b>⏰ Group Reminder</b>", gact_kb(tid), f"gpm_{tid}_{uid_s}", uid):
        set_tstatus(tid, uid_s, "missed")
    await update_gstatus(ctx, tid, msg)

async def fire_group(ctx, row, v, uid, msg, gid, tid, cfg):
    active = [(u, n) for u, n, s in get_tmembers(tid) if s == "waiting"]
    if not active:
        sheet.update_cell(row, 6, "done")
        return
    
    for u, n in active:
        set_tstatus(tid, u, "pending")
    
    setup = ctx.bot_data.pop(f"gm_{tid}", None)
    if setup:
        try:
            await ctx.bot.edit_message_reply_markup(chat_id=int(setup["c"]), message_id=setup["m"], reply_markup=None)
        except Exception:
            pass
    
    try:
        status = await ctx.bot.send_message(chat_id=int(gid), text=gstatus_text(tid, msg), parse_mode="HTML")
        ctx.bot_data[f"gs_{tid}"] = {"c": int(gid), "m": status.message_id}
    except Exception as e:
        logger.error(f"Fire group status error: {e}")
    
    for u, n in active:
        if not await send_and_track(ctx, int(u), f"{msg}\n\n<b>⏰ Group Reminder</b>", gact_kb(tid), f"gpm_{tid}_{u}", int(u)):
            set_tstatus(tid, u, "missed")
    
    sheet.update_cell(row, 6, "pending")
    sheet.update_cell(row, 7, 0)
    ctx.job_queue.run_once(grp_retry, cfg.get("retry_gap", DEF_RETRY_GAP) * 60,
                           data={"tid": tid, "row": row, "gid": gid}, name=f"gretry-{tid}")

async def grp_retry(ctx: ContextTypes.DEFAULT_TYPE):
    tid, row, gid = ctx.job.data["tid"], ctx.job.data["row"], ctx.job.data["gid"]
    try:
        r = sheet.row_values(row)
        if not r or len(r) <= 5 or r[5] != "pending":
            return
        
        cfg = get_cfg(int(r[0]) if r[0].isdigit() else r[0])
        max_r, gap = cfg["max_retries"], cfg["retry_gap"]
        count = int(r[6]) if len(r) > 6 and r[6].isdigit() else 0
        pending = [(u, n) for u, n, s in get_tmembers(tid) if s == "pending"]
        msg = str(r[1]).strip()
        
        if not pending or count >= max_r:
            for u, n in pending:
                set_tstatus(tid, u, "missed")
            await update_gstatus(ctx, tid, msg)
            await check_grp_resolved(ctx, tid, row, r)
            return
        
        nc = count + 1
        for u, n in pending:
            await rm_gpm(ctx, tid, u)
            if not await send_and_track(ctx, int(u), f"{msg}\n\n<b>Reminder</b> ({nc}/{max_r})", gact_kb(tid), f"gpm_{tid}_{u}", int(u)):
                set_tstatus(tid, u, "missed")
        
        sheet.update_cell(row, 7, nc)
        await update_gstatus(ctx, tid, msg)
        
        if nc >= max_r:
            for u, n in pending:
                set_tstatus(tid, u, "missed")
            await check_grp_resolved(ctx, tid, row, r)
        else:
            ctx.job_queue.run_once(grp_retry, gap * 60, data={"tid": tid, "row": row, "gid": gid}, name=f"gretry-{tid}")
    except Exception as e:
        logger.error(f"Group retry error: {e}")

# ============= SCHEDULERS =================
async def check_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        cfg_vals = cfg_sheet.get_all_values()
    except Exception:
        cfg_vals = []
    
    tz_map, cfg_map = {}, {}
    for r in cfg_vals[1:]:
        if not r:
            continue
        uid_s = str(r[0])
        tz_map[uid_s] = str(r[5]) if len(r) > 5 and r[5] else DEF_TZ
        cfg_map[uid_s] = {
            "retry_gap": int(r[4]) if len(r) > 4 and r[4] else DEF_RETRY_GAP,
            "max_retries": int(r[3]) if len(r) > 3 and r[3] else DEF_RETRIES,
        }
    
    try:
        vals = sheet.get_all_values()
    except Exception as e:
        logger.error(f"Check reminders error: {e}")
        return
    
    for idx, v in enumerate(vals[1:], 2):
        if len(v) < 7 or str(v[5]).strip().lower() != "active":
            continue
        
        uid_s = str(v[0])
        user_tz = safe_tz(tz_map.get(uid_s, DEF_TZ))
        now = datetime.now(user_tz)
        
        if norm_date(str(v[2]).strip()) != now.strftime("%Y-%m-%d"):
            continue
        if norm_time(str(v[3]).strip()) != now.strftime("%H:%M"):
            continue
        
        rep = str(v[4]).strip() if len(v) > 4 else "none"
        if not is_custom_day_match(rep, now):
            continue
        
        uid = int(v[0]) if v[0].isdigit() else v[0]
        msg = str(v[1]).strip()
        gid = str(v[7]).strip() if len(v) > 7 else ""
        tid = str(v[8]).strip() if len(v) > 8 else ""
        
        logger.info(f"[FIRE] Row {idx}: {msg[:30]} uid={uid} gid={gid}")
        
        if gid and tid:
            await fire_group(ctx, idx, v, uid, msg, gid, tid, cfg_map.get(uid_s, {}))
        else:
            kill_jobs(ctx.job_queue, idx)
            await rm_btns(ctx, idx)
            
            if await send_and_track(ctx, uid, f"{msg}\n\n⏰ Reminder", act_kb(idx), f"r_{idx}", uid):
                sheet.update_cell(idx, 6, "pending")
                sheet.update_cell(idx, 7, 0)
                gap = cfg_map.get(uid_s, {"retry_gap": DEF_RETRY_GAP})["retry_gap"]
                ctx.job_queue.run_once(auto_retry, gap * 60, data={"row": idx, "chat": uid}, name=f"retry-{idx}")

async def check_digest(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        cfg_rows = cfg_sheet.get_all_values()
    except Exception:
        return
    
    for r in cfg_rows[1:]:
        if len(r) < 3 or str(r[1]).lower() != "true":
            continue
        
        user_tz = safe_tz(str(r[5]) if len(r) > 5 and r[5] else DEF_TZ)
        now = datetime.now(user_tz)
        
        if norm_time(r[2]) != now.strftime("%H:%M"):
            continue
        
        try:
            uid_int = int(r[0])
        except (ValueError, TypeError):
            continue
        
        try:
            rem_rows = sheet.get_all_values()
        except Exception:
            continue
        
        today = now.strftime("%Y-%m-%d")
        items = [v for v in rem_rows[1:] if len(v) >= 6 and str(v[0]) == str(r[0])
                 and str(v[5]).strip().lower() in ("active", "snoozed") and norm_date(str(v[2]).strip()) == today
                 and not (len(v) > 7 and str(v[7]).strip())]
        items.sort(key=lambda x: norm_time(str(x[3]).strip()))
        
        today_str = now.strftime("%-d %b")
        if items:
            lines = [f"☀️ <b>Good morning!</b>\n{DIV}\n\nToday — {today_str}\n"]
            for v in items:
                msg = str(v[1]).strip()
                lines.append(f"  {fmt_time(norm_time(str(v[3]).strip()))} · {msg[:30] + '…' if len(msg) > 30 else msg}")
            lines.append(f"\n{len(items)} reminder{'s' if len(items) != 1 else ''} today")
        else:
            lines = [f"☀️ <b>Good morning!</b>\n{DIV}\n\nToday — {today_str}\n", "No reminders today. Enjoy!"]
        
        digest_text = "\n".join(lines)
        try:
            await ctx.bot.send_message(chat_id=uid_int, text=digest_text, reply_markup=home_kb(), parse_mode="HTML")
            save_digest_archive(uid_int, today, len(items), digest_text)
        except Exception as e:
            logger.error(f"Digest send error {r[0]}: {e}")

async def check_weekly_report(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        cfg_rows = cfg_sheet.get_all_values()
    except Exception:
        return
    
    for r in cfg_rows[1:]:
        user_tz = safe_tz(str(r[5]) if len(r) > 5 and r[5] else DEF_TZ)
        now = datetime.now(user_tz)
        
        if now.weekday() != 6 or now.strftime("%H:%M") != "09:00":
            continue
        
        try:
            uid_int = int(r[0])
        except (ValueError, TypeError):
            continue
        
        try:
            rem_rows = sheet.get_all_values()
        except Exception:
            continue
        
        week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        week_end = now.strftime("%Y-%m-%d")
        uid_s = str(r[0])
        
        done = sum(1 for v in rem_rows[1:] if len(v) >= 6 and str(v[0]) == uid_s
                   and not (len(v) > 7 and str(v[7]).strip())
                   and week_start <= norm_date(str(v[2]).strip()) <= week_end 
                   and str(v[5]).strip().lower() == "done")
        missed = sum(1 for v in rem_rows[1:] if len(v) >= 6 and str(v[0]) == uid_s
                     and not (len(v) > 7 and str(v[7]).strip())
                     and week_start <= norm_date(str(v[2]).strip()) <= week_end 
                     and str(v[5]).strip().lower() == "missed")
        total = done + missed
        
        if total == 0:
            continue
        
        pct = round(done / total * 100)
        mot = ("Outstanding! 🏆" if pct >= 90 else 
               ("Keep it up! 💪" if pct >= 70 else 
                ("Room to improve 📈" if pct >= 50 else "Let's do better! 🎯")))
        
        ws_d = (now - timedelta(days=7)).strftime("%-d %b")
        we_d = now.strftime("%-d %b")
        
        txt = (
            f"📊 <b>Weekly Report</b>\n{DIV}\n{ws_d} — {we_d}\n\n"
            f"✅ Completed: {done}/{total} ({pct}%)\n"
            f"❌ Missed: {missed}\n\n{mot}"
        )
        
        try:
            await ctx.bot.send_message(chat_id=uid_int, text=txt, reply_markup=home_kb(), parse_mode="HTML")
            save_weekly_archive(uid_int, week_start, week_end, done, missed, total, txt)
        except Exception as e:
            logger.error(f"Weekly report error {uid_s}: {e}")

# ============= FLASK SERVER ===============
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "RemindX Bot is alive!"

def run_web():
    app_flask.run(host="0.0.0.0", port=10000)

# ============= MAIN ======================
def main():
    app = Application.builder().token(TOKEN).job_queue(JobQueue()).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    
    app.job_queue.run_repeating(check_reminders, interval=60, first=0)
    app.job_queue.run_repeating(check_digest, interval=60, first=10)
    app.job_queue.run_repeating(check_weekly_report, interval=60, first=20)
    
    print("🚀 RemindX Bot Running - Optimized & Enhanced")
    Thread(target=run_web, daemon=True).start()
    app.run_polling()

if __name__ == "__main__":
    main()

