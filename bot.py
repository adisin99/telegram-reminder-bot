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
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    ForceReply,
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
    try: ws = workbook.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = workbook.add_worksheet(title=name, rows=100, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
    return ws

sheet = get_or_create_sheet("Reminders", ["user_id", "message", "date", "time", "repeat", "status", "retry_count", "group_id", "task_id"])
cfg_sheet = get_or_create_sheet("Settings", ["user_id", "digest_on", "digest_time", "max_retries", "retry_gap", "timezone"])
grp_sheet = get_or_create_sheet("GroupMembers", ["group_id", "user_id", "first_name", "subscribed"])
task_sheet = get_or_create_sheet("TaskMembers", ["task_id", "user_id", "first_name", "status"])

# ============= FORMATTERS ================
def hdr(t): return f"<b>{t}</b>\n{DIV}"

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
ST_IC = {"active": "○", "pending": "●", "missed": "✗", "snoozed": "◷"}
ST_LB = {"active": "Active", "pending": "Pending", "missed": "Missed", "snoozed": "Snoozed"}
GT_IC = {"waiting": "⏳", "pending": "⏳", "done": "✅", "snoozed": "◷", "missed": "✗"}

def fmt_rep(r): return REP_MAP.get(str(r), str(r))
def fmt_snz(m): return f"{m} min" if m < 60 else f"{m // 60} hr{'s' if m >= 120 else ''}"

def tz_label(n):
    for tz, c, _, _ in TZ_DATA:
        if tz == n: return c
    return n.split("/")[-1].replace("_", " ")

def tz_short(n):
    for tz, c, o, _ in TZ_DATA:
        if tz == n: return f"{c} ({o})"
    return n.split("/")[-1].replace("_", " ")

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

# ============= HELPERS ====================
def get_tz(uid):
    try: return pytz.timezone(get_cfg(uid).get("timezone", DEF_TZ))
    except Exception: return pytz.timezone(DEF_TZ)

def safe_tz(n):
    try: return pytz.timezone(n)
    except Exception: return pytz.timezone(DEF_TZ)

def get_cfg(uid):
    uid_s = str(uid)
    try: rows = cfg_sheet.get_all_values()
    except Exception:
        try: client.login(); rows = cfg_sheet.get_all_values()
        except Exception: return {"digest_on": True, "digest_time": DEF_DIGEST_TIME, "max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP, "timezone": DEF_TZ}
    for r in rows[1:]:
        if str(r[0]) == uid_s:
            return {"digest_on": str(r[1]).lower() != "false", "digest_time": norm_time(r[2]) if r[2] else DEF_DIGEST_TIME,
                    "max_retries": int(r[3]) if r[3] else DEF_RETRIES, "retry_gap": int(r[4]) if r[4] else DEF_RETRY_GAP,
                    "timezone": str(r[5]) if len(r) > 5 and r[5] else DEF_TZ}
    cfg_sheet.append_row([uid_s, "true", DEF_DIGEST_TIME, DEF_RETRIES, DEF_RETRY_GAP, DEF_TZ], value_input_option="RAW")
    return {"digest_on": True, "digest_time": DEF_DIGEST_TIME, "max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP, "timezone": DEF_TZ}

def save_cfg(uid, field, value):
    uid_s = str(uid)
    col = {"digest_on": 2, "digest_time": 3, "max_retries": 4, "retry_gap": 5, "timezone": 6}.get(field)
    if not col: return
    try: rows = cfg_sheet.get_all_values()
    except Exception: return
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == uid_s: cfg_sheet.update_cell(i, col, str(value)); return
    get_cfg(uid)
    try: rows = cfg_sheet.get_all_values()
    except Exception: return
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == uid_s: cfg_sheet.update_cell(i, col, str(value)); return

def get_detail(r):
    return (str(r[1]).strip() if len(r) > 1 else "", norm_date(r[2]) if len(r) > 2 else "",
            norm_time(r[3]) if len(r) > 3 else "", fmt_rep(r[4]) if len(r) > 4 else "")

def row_detail(row):
    r = sheet.row_values(row)
    return (r, *get_detail(r))

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

def kill_jobs(jq, name_prefix):
    for n in ([f"retry-{name_prefix}", f"snooze-{name_prefix}"] if isinstance(name_prefix, int) else [name_prefix]):
        for j in jq.get_jobs_by_name(n): j.schedule_removal()

# ============= GROUP DATA =================
def gen_tid(): return f"t{int(time_module.time())}"

def grp_read(sheet_ref, filter_fn):
    try: rows = sheet_ref.get_all_values()
    except Exception: return []
    return [r for r in rows[1:] if filter_fn(r)]

def get_gsubs(gid):
    return [(r[1], r[2]) for r in grp_read(grp_sheet, lambda r: str(r[0]) == str(gid) and str(r[3]).lower() == "true")]

def set_gsub(gid, uid, name, sub=True):
    gid_s, uid_s = str(gid), str(uid)
    try: rows = grp_sheet.get_all_values()
    except Exception: rows = [["h"]]
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == gid_s and str(r[1]) == uid_s:
            grp_sheet.update_cell(i, 3, name); grp_sheet.update_cell(i, 4, str(sub).lower()); return
    grp_sheet.append_row([gid_s, uid_s, name, str(sub).lower()], value_input_option="RAW")

def get_tmembers(tid):
    return [(r[1], r[2], r[3]) for r in grp_read(task_sheet, lambda r: str(r[0]) == str(tid))]

def add_tmember(tid, uid, name, st="waiting"):
    uid_s = str(uid)
    try: rows = task_sheet.get_all_values()
    except Exception: rows = [["h"]]
    if any(str(r[0]) == str(tid) and str(r[1]) == uid_s for r in rows[1:]): return False
    task_sheet.append_row([str(tid), uid_s, name, st], value_input_option="RAW")
    return True

def set_tstatus(tid, uid, st):
    uid_s = str(uid)
    try: rows = task_sheet.get_all_values()
    except Exception: return
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == str(tid) and str(r[1]) == uid_s: task_sheet.update_cell(i, 4, st); return

def find_by_tid(tid):
    try: rows = sheet.get_all_values()
    except Exception: return None, None
    for i, r in enumerate(rows[1:], 2):
        if len(r) > 8 and str(r[8]) == str(tid): return i, r
    return None, None

def gstatus_text(tid, msg):
    ms = get_tmembers(tid)
    active = [(u, n, s) for u, n, s in ms if s != "skipped"]
    if not active: return f"⏰ {msg}\n\nNo subscribers"
    if all(s in ("done", "missed") for _, _, s in active):
        if all(s == "done" for _, _, s in active):
            return f"{msg}\n\n✅ All done · {', '.join(n for _, n, _ in active)}"
    default_icon = "⏳"
    prefix = "⏰ " if any(s not in ("done", "missed") for _, _, s in active) else ""
    parts = [f"{GT_IC.get(s, default_icon)} {n}" for _, n, s in active]
    return f"{prefix}{msg}\n\n{' · '.join(parts)}"

def gsub_text(tid):
    active = [(u, n) for u, n, s in get_tmembers(tid) if s != "skipped"]
    return f"{len(active)} subscribed: {', '.join(n for _, n in active)}" if active else "0 subscribed"

async def update_gstatus(ctx, tid, msg):
    info = ctx.bot_data.get(f"gs_{tid}")
    if not info: return
    try: await ctx.bot.edit_message_text(chat_id=info["c"], message_id=info["m"], text=gstatus_text(tid, msg), parse_mode="HTML")
    except Exception: pass

async def check_grp_resolved(ctx, tid, row, r):
    active = [(u, n, s) for u, n, s in get_tmembers(tid) if s != "skipped"]
    if not active or not all(s in ("done", "missed") for _, _, s in active): return
    for j in ctx.job_queue.get_jobs_by_name(f"gretry-{tid}"): j.schedule_removal()
    if not advance_rep_grp(row, r, tid): sheet.update_cell(row, 6, "done")
    sheet.update_cell(row, 7, 0)

def advance_rep_grp(row, r, tid):
    if not advance_rep(row, r): return False
    try: rows = task_sheet.get_all_values()
    except Exception: return True
    for i, tr in enumerate(rows[1:], 2):
        if str(tr[0]) == str(tid) and str(tr[3]) != "skipped": task_sheet.update_cell(i, 4, "waiting")
    return True

# ============= MENTION EXTRACTION =========
async def extract_mentions(message, bot):
    """Extract tagged users from both text_mention and @username mentions."""
    tagged = []
    seen_ids = set()
    if not message.entities:
        logger.info("[MENTION] No entities found in message")
        return tagged
    for entity in message.entities:
        logger.info(f"[MENTION] Entity: type={entity.type}, offset={entity.offset}, length={entity.length}, user={getattr(entity, 'user', None)}")
        # text_mention: user selected from autocomplete (has user object)
        if entity.type == "text_mention" and entity.user:
            uid = str(entity.user.id)
            if uid not in seen_ids:
                tagged.append((uid, entity.user.first_name or "User"))
                seen_ids.add(uid)
                logger.info(f"[MENTION] text_mention resolved: {uid} = {entity.user.first_name}")
        # mention: @username typed or selected from autocomplete
        elif entity.type == "mention":
            username = (message.text or "")[entity.offset:entity.offset + entity.length]
            logger.info(f"[MENTION] Trying to resolve @mention: {username}")
            try:
                chat = await bot.get_chat(username)
                if chat and chat.id:
                    uid = str(chat.id)
                    if uid not in seen_ids:
                        tagged.append((uid, chat.first_name or username.lstrip("@")))
                        seen_ids.add(uid)
                        logger.info(f"[MENTION] @mention resolved: {uid} = {chat.first_name}")
            except Exception as e:
                logger.warning(f"[MENTION] Could not resolve {username}: {e}")
    logger.info(f"[MENTION] Final tagged list: {tagged}")
    return tagged

def strip_mentions(text, message):
    if not message.entities: return text
    for entity in sorted(message.entities, key=lambda e: e.offset, reverse=True):
        if entity.type in ("mention", "text_mention"):
            mt = (message.text or "")[entity.offset:entity.offset + entity.length]
            text = text.replace(mt, "", 1)
    return re.sub(r'\s+', ' ', text).strip()

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

async def rm_home(ctx, ud):
    mid, cid = ud.pop("h_mid", None), ud.pop("h_cid", None)
    if mid and cid:
        try: await ctx.bot.edit_message_reply_markup(chat_id=cid, message_id=mid, reply_markup=None)
        except Exception: pass

def save_home(ud, msg): ud["h_mid"], ud["h_cid"] = msg.message_id, msg.chat.id

async def rm_gpm(ctx, tid, uid_s):
    old = ctx.bot_data.pop(f"gpm_{tid}_{uid_s}", None)
    if old:
        try: await ctx.bot.edit_message_reply_markup(chat_id=old["c"], message_id=old["m"], reply_markup=None)
        except Exception: pass

# ============= UI ========================
HOME_TEXT = (f"{hdr('Smart Reminder Bot')}\nType your reminder below:\n\n"
             "<i>Buy milk tomorrow at 5pm</i>\n"
             "<i>Gym at 6pm daily</i>\n"
             "<i>Meeting Monday 10am weekly</i>\n\n"
             "Or tap ＋ New for step-by-step.\n"
             "Use /list to view all.")

def home_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("＋ New", callback_data="add")]])
def cancel_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="cancel")]])
def gcancel_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="gcancel")]])
def act_kb(row): return InlineKeyboardMarkup([[InlineKeyboardButton("Snooze", callback_data=f"snzp_{row}"), InlineKeyboardButton("Done", callback_data=f"done_{row}")]])
def gact_kb(tid): return InlineKeyboardMarkup([[InlineKeyboardButton("Snooze", callback_data=f"gsnzp_{tid}"), InlineKeyboardButton("Done", callback_data=f"gdone_{tid}")]])

def saved_kb(row, rep):
    if rep == "none":
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Repeat", callback_data=f"chrep_{row}"), InlineKeyboardButton("＋ New", callback_data="add")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("＋ New", callback_data="add")]])

def gjoin_kb(tid, show_rep=False):
    btns = [[InlineKeyboardButton("＋ Count Me In", callback_data=f"gjoin_{tid}"), InlineKeyboardButton("✕ Skip", callback_data=f"gskip_{tid}")]]
    if show_rep: btns.append([InlineKeyboardButton("🔁 Repeat", callback_data=f"gchrep_{tid}")])
    return InlineKeyboardMarkup(btns)

def rep_picker_kb(prefix):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Once", callback_data=f"{prefix}_none"), InlineKeyboardButton("Daily", callback_data=f"{prefix}_daily")],
        [InlineKeyboardButton("Weekly", callback_data=f"{prefix}_weekly"), InlineKeyboardButton("Monthly", callback_data=f"{prefix}_monthly")]])

def snz_kb(key, pfx="snz"):
    opts = [(15,"15m"),(30,"30m"),(45,"45m"),(60,"1h"),(120,"2h"),(180,"3h"),(300,"5h"),(480,"8h"),(720,"12h")]
    kb = [[InlineKeyboardButton(l, callback_data=f"{pfx}_{key}_{m}") for m, l in opts[i:i+3]] for i in range(0, 9, 3)]
    kb.append([InlineKeyboardButton("« Back", callback_data=f"{pfx}b_{key}")])
    return InlineKeyboardMarkup(kb)

def cfg_picker_kb(values, fmt_fn, cur, cb_prefix):
    btns, row = [], []
    for v in values:
        row.append(InlineKeyboardButton(f"[{fmt_fn(v)}]" if v == cur else fmt_fn(v), callback_data=f"{cb_prefix}{v}"))
        if len(row) == 3: btns.append(row); row = []
    if row: btns.append(row)
    btns.append([InlineKeyboardButton("« Back", callback_data="cfg_back")])
    return InlineKeyboardMarkup(btns)

def gmin_kb(show_cb):
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Show", callback_data=show_cb)]])

def gclose_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✕ Close", callback_data="gclose")]])

# ============= CALENDAR ==================
def cal_kb(year, month, back_cb="cancel", back_txt="✕ Cancel", tz=None):
    now = datetime.now(tz or safe_tz(DEF_TZ))
    kb = [[InlineKeyboardButton(f"{cal_module.month_name[month]} {year}", callback_data="noop")]]
    kb.append([InlineKeyboardButton(d, callback_data="noop") for d in "Mo Tu We Th Fr Sa Su".split()])
    for week in cal_module.monthcalendar(year, month):
        if not any(d and datetime(year, month, d).date() >= now.date() for d in week if d): continue
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
    kb.append([InlineKeyboardButton("Today", callback_data=f"day_{td}"), InlineKeyboardButton("Tomorrow", callback_data=f"day_{tm}")])
    nm, ny = (month % 12) + 1, year + (1 if month == 12 else 0)
    pm, py = ((month - 2) % 12) + 1, year - (1 if month == 1 else 0)
    nav = [InlineKeyboardButton("‹", callback_data=f"cal_{py}_{pm:02d}") if datetime(py, pm, 1) >= datetime(now.year, now.month, 1) else InlineKeyboardButton(" ", callback_data="noop")]
    nav.append(InlineKeyboardButton("›", callback_data=f"cal_{ny}_{nm:02d}"))
    kb.append(nav)
    kb.append([InlineKeyboardButton(back_txt, callback_data=back_cb)])
    return InlineKeyboardMarkup(kb)

# ============= PARSERS ====================
def parse_time(text):
    s = text.strip()
    for pat, mode in [(r'^(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)$', 'hma'), (r'^(\d{1,2})\s*(am|pm)$', 'ha'), (r'^(\d{1,2})[:.]\s*(\d{1,2})$', '24')]:
        m = re.match(pat, s, re.I)
        if not m: continue
        if mode == 'hma': h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        elif mode == 'ha': h, mi, ap = int(m.group(1)), 0, m.group(2).lower()
        else: h, mi, ap = int(m.group(1)), int(m.group(2)), None
        if ap:
            if ap == 'pm' and h != 12: h += 12
            elif ap == 'am' and h == 12: h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59: return f"{h:02d}:{mi:02d}"
    return None

def _to24(h, mi, ap):
    if ap.lower() == 'pm' and h != 12: h += 12
    elif ap.lower() == 'am' and h == 12: h = 0
    return f"{h:02d}:{mi:02d}" if 0 <= h <= 23 and 0 <= mi <= 59 else None

def _find_time(text):
    for pat, mode in [(r'(?:at|by)\s+(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)', 'hma'), (r'(?:at|by)\s+(\d{1,2})\s*(am|pm)', 'ha'),
                      (r'(?:at|by)\s+(\d{1,2}):(\d{2})\b', '24'), (r'(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)', 'hma'), (r'(\d{1,2})\s*(am|pm)', 'ha')]:
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
    for pat, delta in [(r'\bday\s+after\s+tomorrow\b', 2), (r'\b(today|tonight)\b', 0), (r'\b(tomorrow|tmrw|tmr)\b', 1), (r'\bnext\s+week\b', 7)]:
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
    for s, e in sorted([x for x in spans if x], key=lambda x: x[0], reverse=True): text = text[:s] + text[e:]
    for f in [r'^\s*remind\s+me\s+to\s+', r'^\s*reminder\s+to\s+', r'^\s*reminder\s+', r'^\s*remind\s+me\s+',
              r'^\s*remember\s+to\s+', r"^\s*don'?t\s+forget\s+to\s+", r'^\s*set\s+(?:a\s+)?reminder\s+(?:to\s+|for\s+)?']:
        text = re.sub(f, '', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip().strip('.,;:!? ')
    text = re.sub(r'^\s*on\s+', '', text, flags=re.I).strip()
    text = re.sub(r'\s+on\s*$', '', text, flags=re.I).strip()
    return text[0].upper() + text[1:] if text else text

def parse_nl_partial(text, tz=None):
    tr, dr, rr = _find_time(text), _find_date(text, tz), _find_repeat(text)
    ts = tr[0] if tr else None
    ds = dr[0] if dr else None
    rep = rr[0] if rr else None
    msg = _clean(text, [(tr[1],tr[2]) if tr else None, (dr[1],dr[2]) if dr else None, (rr[1],rr[2]) if rr else None])
    if not msg: return None
    return {'message': msg, 'date': ds, 'time': ts, 'repeat': rep}

# ============= COMMANDS ===================
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("add", "New reminder"),
        BotCommand("list", "All reminders"),
        BotCommand("settings", "Bot settings"),
        BotCommand("info", "About this bot"),
    ], scope=BotCommandScopeAllPrivateChats())
    await app.bot.set_my_commands([
        BotCommand("start", "Bot info & commands"),
        BotCommand("remind", "Group reminder"),
        BotCommand("list", "Active reminders"),
    ], scope=BotCommandScopeAllGroupChats())
    await app.bot.set_my_commands([])

GRP_START = (f"{hdr('Smart Reminder Bot')}\n\n"
    "<b>Commands</b>\n"
    "/remind — Group reminder\n"
    "/list — Active reminders\n\n"
    "<b>Examples</b>\n"
    "<code>/remind Buy milk at 5pm</code>\n"
    "<code>/remind Meeting tomorrow 10am daily</code>\n"
    "<code>/remind</code> — step-by-step\n\n"
    "<i>Tag members to assign:</i>\n"
    "<code>/remind @John Submit report at 5pm</code>")

GRP_START_MIN = "<b>Smart Reminder Bot</b>"

def build_grp_list_text(gid):
    try: rows = sheet.get_all_values()
    except Exception: rows = []
    items = [(i, r) for i, r in enumerate(rows[1:], 2)
             if len(r) > 7 and str(r[7]).strip() == str(gid) and str(r[5]).strip() in ("active", "pending", "snoozed")]
    if not items: return None, []
    lines = [hdr("Group Reminders")]
    for idx, (ri, r) in enumerate(items, 1):
        st, msg = str(r[5]).strip(), str(r[1]).strip()
        short = msg[:30] + "…" if len(msg) > 30 else msg
        lines.append(f"\n<b>{idx}</b> {ST_IC.get(st,'?')} {short}\n   {fmt_date(norm_date(r[2]))} · {fmt_time(norm_time(r[3]))}")
    return "\n".join(lines), items

GRP_LIST_MIN = "<b>Group Reminders</b>"

async def auto_minimize(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    try:
        await ctx.bot.edit_message_text(
            chat_id=d["c"], message_id=d["m"],
            text=d["min_text"],
            reply_markup=gmin_kb(d["show_cb"]),
            parse_mode="HTML")
    except Exception: pass

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        gid = str(update.effective_chat.id)
        sent = await update.message.reply_text(GRP_START, reply_markup=gclose_kb(), parse_mode="HTML")
        show_cb = f"gshow_start_{sent.message_id}"
        ctx.bot_data[f"gstart_{sent.message_id}"] = {"c": gid, "text": GRP_START}
        ctx.bot_data[f"gmin_{sent.message_id}"] = {"min_text": GRP_START_MIN, "show_cb": show_cb}
        ctx.job_queue.run_once(auto_minimize, 30, data={
            "c": sent.chat.id, "m": sent.message_id,
            "min_text": GRP_START_MIN, "show_cb": show_cb
        })
        return
    await rm_home(ctx, ctx.user_data); ctx.user_data.clear()
    get_cfg(update.effective_user.id)
    sent = await update.message.reply_text(HOME_TEXT, reply_markup=home_kb(), parse_mode="HTML")
    save_home(ctx.user_data, sent)

async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /remind here for group reminders."); return
    await rm_home(ctx, ctx.user_data); ctx.user_data.clear()
    ctx.user_data["step"] = "message"
    sent = await update.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
    save_p(ctx.user_data, sent)

async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        gid = str(update.effective_chat.id)
        list_text, items = build_grp_list_text(gid)
        if not list_text:
            sent = await update.message.reply_text(f"{hdr('Group Reminders')}\nNo active reminders.", reply_markup=gclose_kb(), parse_mode="HTML")
            show_cb = f"gshow_list_{gid}_{sent.message_id}"
            ctx.bot_data[f"gmin_{sent.message_id}"] = {"min_text": "<b>Group Reminders</b> — No active", "show_cb": show_cb}
            ctx.job_queue.run_once(auto_minimize, 30, data={
                "c": sent.chat.id, "m": sent.message_id,
                "min_text": "<b>Group Reminders</b> — No active", "show_cb": show_cb
            })
            return
        sent = await update.message.reply_text(list_text, reply_markup=gclose_kb(), parse_mode="HTML")
        show_cb = f"gshow_list_{gid}_{sent.message_id}"
        ctx.bot_data[f"glist_{sent.message_id}"] = {"c": gid, "text": list_text}
        ctx.bot_data[f"gmin_{sent.message_id}"] = {"min_text": GRP_LIST_MIN, "show_cb": show_cb}
        ctx.job_queue.run_once(auto_minimize, 60, data={
            "c": sent.chat.id, "m": sent.message_id,
            "min_text": GRP_LIST_MIN, "show_cb": show_cb
        })
        return
    await rm_home(ctx, ctx.user_data); ctx.user_data.clear()
    await show_list(update.message, update.effective_user.id, ctx.user_data, new=True)

async def info_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /info in private chat."); return
    cfg = get_cfg(update.effective_user.id)
    await update.message.reply_text(
        f"{hdr('Smart Reminder Bot')}\n\nSet reminders and get notified on time.\n\n"
        "<b>Smart Input</b>\nJust type naturally:\n"
        "<code>Buy milk tomorrow at 5pm</code>\n"
        "<code>Meeting Monday 10am weekly</code>\n"
        "<code>Call mom at 3pm</code>\n\n"
        "<i>Add \"daily\", \"weekly\" or \"monthly\"\nto set recurring reminders.</i>\n\n"
        "<b>Features</b>\n• One-time & recurring reminders\n• Calendar date picker\n• Flexible time input\n"
        "• Snooze (15m to 12h)\n• Auto-retry if missed\n• Edit or cancel anytime\n• Daily morning digest\n"
        f"• Per-user timezone ({tz_short(cfg['timezone'])})\n\n"
        "<b>Group Reminders</b>\n• Use /remind in groups\n"
        "• Tag members to assign specific people\n• Members opt in per reminder\n"
        "• Track who's done / pending / missed\n\n"
        "<b>Commands</b>\n/add — New reminder\n/list — All reminders\n/remind — Group reminder\n"
        "/settings — Bot settings\n/info — This page", parse_mode="HTML")

async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Use /remind in groups.\nJust type naturally for personal reminders."); return

    ud = ctx.user_data; ud.clear()
    uid = update.effective_user.id
    utz = get_tz(uid)
    gid = str(update.effective_chat.id)
    name = update.effective_user.first_name or "User"
    ud["g_chat"], ud["g_name"] = gid, name

    # Extract tagged members (both @username and text_mention)
    tagged = await extract_mentions(update.message, ctx.bot)
    logger.info(f"[REMIND] Tagged result: {tagged}")
    if tagged: ud["g_tagged"] = tagged

    raw = update.message.text or ""
    text = re.sub(r'^/remind(@\w+)?\s*', '', raw.strip(), flags=re.I).strip()
    text = strip_mentions(text, update.message)

    if not text:
        # Step-by-step: use ForceReply so bot receives text in privacy mode
        ud["step"] = "g_message"
        sent = await update.message.reply_text(
            f"{hdr('Group Reminder')}\nType your reminder message:\n<i>↩️ Reply to this message</i>",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Type your reminder..."),
            parse_mode="HTML")
        save_p(ud, sent)
        return

    result = parse_nl_partial(text, tz=utz)
    if result and result.get('message'):
        msg = result['message']
        ud["message"] = msg
        if result.get('time'): ud["time"] = result['time']
        if result.get('repeat'): ud["repeat"] = result['repeat']
        await handle_nl_result(update.message, ctx, uid, ud, msg, result.get('time'), result.get('date'), utz, is_group=True)
    else:
        ud["message"] = text
        ud["step"] = "g_date"
        now = datetime.now(utz)
        await update.message.reply_text(f"{hdr('Group Reminder')}\n{text}\n\nPick a date:",
            reply_markup=cal_kb(now.year, now.month, "gcancel", "✕ Cancel", tz=utz), parse_mode="HTML")

async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    await rm_home(ctx, ctx.user_data); ctx.user_data.clear()
    await show_settings(update.message, update.effective_user.id, new=True)

def get_user_groups(uid):
    uid_s = str(uid)
    gids = []
    for r in grp_read(grp_sheet, lambda r: str(r[1]) == uid_s and str(r[3]).lower() == "true"):
        if r[0] not in gids: gids.append(r[0])
    return gids

async def show_settings(target, uid, new=False):
    cfg = get_cfg(uid)
    d_on = cfg["digest_on"]
    d_time = fmt_time(cfg["digest_time"]) if d_on else "—"
    tz_disp = tz_label(cfg.get("timezone", DEF_TZ))
    grps = get_user_groups(uid)
    txt = (f"{hdr('Settings')}\n\n<b>Digest</b>: {'ON' if d_on else 'OFF'}" + (f" · {d_time}" if d_on else "") +
           f"\n<b>Retries</b>: {cfg['max_retries']}×\n<b>Gap</b>: {cfg['retry_gap']} min\n<b>Timezone</b>: {tz_disp}")
    if grps: txt += f"\n<b>Groups</b>: {len(grps)} subscribed"
    btns = [
        [InlineKeyboardButton(f"Digest: {'ON' if d_on else 'OFF'}", callback_data="cfg_digest_toggle"),
         InlineKeyboardButton(f"⏰ {d_time}" if d_on else "—", callback_data="cfg_digest_time" if d_on else "noop")],
        [InlineKeyboardButton(f"Retries: {cfg['max_retries']}×", callback_data="cfg_retries"),
         InlineKeyboardButton(f"Gap: {cfg['retry_gap']}m", callback_data="cfg_gap")],
        [InlineKeyboardButton(f"🌍 {tz_disp}", callback_data="cfg_tz")]]
    if grps: btns.append([InlineKeyboardButton(f"👥 Groups ({len(grps)})", callback_data="cfg_groups")])
    btns.append([InlineKeyboardButton("« Back", callback_data="home")])
    if new: await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
    else: await safe_edit(target, txt, InlineKeyboardMarkup(btns))

# ============= SAVE FUNCTIONS =============
async def save_reminder(target, uid, ud, msg, date, time, edit_msg=False):
    rep = ud.get("repeat", "none")
    sheet.append_row([uid, msg, date, time, rep, "active", 0, "", ""], value_input_option="RAW")
    try: row = len(sheet.get_all_values())
    except Exception: row = 0
    ud.clear()
    txt = f"{hdr('Saved ✓')}\n{detail(msg, date, time, fmt_rep(rep))}"
    kb = saved_kb(row, rep) if row > 0 else home_kb()
    if edit_msg:
        await safe_edit(target, txt, kb); save_home(ud, target)
    else:
        sent = await target.reply_text(txt, reply_markup=kb, parse_mode="HTML"); save_home(ud, sent)

async def finish_group_remind(target, ctx, uid, ud, rep, edit_msg=False):
    msg, ds, ts = ud.get("message", ""), ud.get("date", ""), ud.get("time", "")
    gid = ud.get("g_chat", "")
    name = ud.get("g_name", "User")
    tagged = ud.get("g_tagged")
    tid = gen_tid()

    sheet.append_row([uid, msg, ds, ts, rep, "active", 0, gid, tid], value_input_option="RAW")

    logger.info(f"[FINISH_GRP] tagged={tagged}, gid={gid}, tid={tid}")
    if tagged:
        logger.info(f"[FINISH_GRP] TAGGED BRANCH: adding only {len(tagged)} tagged users")
        for t_uid, t_name in tagged: add_tmember(tid, t_uid, t_name)
    else:
        subs = get_gsubs(gid)
        logger.info(f"[FINISH_GRP] SUBSCRIBER BRANCH: adding {len(subs)} subscribers")
        for sub_uid, sub_name in subs: add_tmember(tid, sub_uid, sub_name)

    sub_info = f"For: {', '.join(n for _, n in tagged)}" if tagged else gsub_text(tid)
    txt = f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, fmt_rep(rep))}\nBy {name}\n\n{sub_info}"
    show_rep = (rep == "none")
    kb = gjoin_kb(tid, show_rep)
    ud.clear()

    if edit_msg:
        await safe_edit(target, txt, kb)
        ctx.bot_data[f"gm_{tid}"] = {"c": str(target.chat.id), "m": target.message_id}
    else:
        sent = await target.reply_text(txt, reply_markup=kb, parse_mode="HTML")
        ctx.bot_data[f"gm_{tid}"] = {"c": str(target.chat.id), "m": sent.message_id}

# ============= NL HANDLER =================
async def handle_nl_result(target, ctx, uid, ud, msg, ts, ds, utz, is_group=False):
    if ts:
        if not ds: ds = datetime.now(utz).strftime("%Y-%m-%d")
        if is_past(ds, ts, utz):
            ud["step"] = "g_date" if is_group else "date"
            back_cb = "gcancel" if is_group else "cancel"
            title = "Group Reminder" if is_group else "New Reminder"
            now = datetime.now(utz)
            sent = await target.reply_text(f"{hdr(title)}\n{msg}\n\n{past_msg(ts)}\nPick a future date:",
                reply_markup=cal_kb(now.year, now.month, back_cb, "✕ Cancel", tz=utz), parse_mode="HTML")
            save_p(ud, sent)
        else:
            ud["date"] = ds
            if is_group:
                await finish_group_remind(target, ctx, uid, ud, ud.get("repeat", "none"))
            else:
                await save_reminder(target, uid, ud, msg, ds, ts)
    elif ds:
        ud["date"] = ds
        ud["step"] = "g_time" if is_group else "time"
        title = "Group Reminder" if is_group else "New Reminder"
        if is_group:
            sent = await target.reply_text(
                f"{hdr(title)}\n{msg}\n{fmt_date(ds)}\n\nEnter time:\n<i>↩️ Reply to this message</i>\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
                reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 9pm, 9:30 PM"),
                parse_mode="HTML")
        else:
            cancel = cancel_kb()
            sent = await target.reply_text(f"{hdr(title)}\n{msg}\n{fmt_date(ds)}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
                reply_markup=cancel, parse_mode="HTML")
        save_p(ud, sent)
    else:
        ud["step"] = "g_date" if is_group else "date"
        back_cb = "gcancel" if is_group else "cancel"
        title = "Group Reminder" if is_group else "New Reminder"
        now = datetime.now(utz)
        sent = await target.reply_text(f"{hdr(title)}\n{msg}\n\nPick a date:",
            reply_markup=cal_kb(now.year, now.month, back_cb, "✕ Cancel", tz=utz), parse_mode="HTML")
        save_p(ud, sent)

# ============= SHOW LIST =================
async def show_list(target, uid, ud, new=False):
    try: rows = sheet.get_all_records()
    except Exception:
        try: client.login(); rows = sheet.get_all_records()
        except Exception: rows = []
    items = [(i, r) for i, r in enumerate(rows, 2)
             if str(r.get("user_id", "")) == str(uid) and str(r.get("status", "")).strip() in ("active", "pending", "missed", "snoozed")
             and not str(r.get("group_id", "")).strip()]
    if not items:
        t, kb = f"{hdr('Reminders')}\nNo reminders found.", home_kb()
        if new: sent = await target.reply_text(t, reply_markup=kb, parse_mode="HTML"); save_home(ud, sent)
        else: await safe_edit(target, t, kb)
        return
    lines = [hdr("Reminders")]
    for idx, (ri, r) in enumerate(items, 1):
        st, msg = str(r.get("status", "")), str(r.get("message", ""))
        short = msg[:30] + "…" if len(msg) > 30 else msg
        lines.append(f"\n<b>{idx}</b> {ST_IC.get(st,'?')} {short}\n   {fmt_date(norm_date(r.get('date', '')))} · {fmt_time(norm_time(r.get('time', '')))}")
    btns, num_row = [], []
    for idx, (ri, _) in enumerate(items, 1):
        num_row.append(InlineKeyboardButton(str(idx), callback_data=f"view_{ri}"))
        if len(num_row) == 5: btns.append(num_row); num_row = []
    if num_row: btns.append(num_row)
    btns.append([InlineKeyboardButton("« Back", callback_data="home")])
    if new: await target.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
    else: await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup(btns))

# ============= BUTTON HANDLERS ===========
async def on_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data, ud, uid = q.data, ctx.user_data, q.from_user.id
    if data == "noop": return

    if data in ("home", "cancel"):
        ud.clear(); await safe_edit(q.message, HOME_TEXT, home_kb()); save_home(ud, q.message); return
    if data == "gcancel":
        ud.clear(); await safe_edit(q.message, f"{hdr('Group Reminder')}\n\nCancelled."); return

    # Group close → MINIMIZE (not delete)
    if data == "gclose":
        mid = str(q.message.message_id)
        stored = ctx.bot_data.get(f"gmin_{mid}")
        if stored:
            await safe_edit(q.message, stored["min_text"], gmin_kb(stored["show_cb"]))
        else:
            # Fallback: generic minimize
            await safe_edit(q.message, "<b>ℹ️</b>", gmin_kb(f"gshow_generic_{mid}"))
        return

    # Group show (expand from minimized)
    if data.startswith("gshow_start_"):
        mid = data[12:]
        stored = ctx.bot_data.get(f"gstart_{mid}")
        if stored:
            await safe_edit(q.message, stored["text"], gclose_kb())
        else:
            await safe_edit(q.message, GRP_START, gclose_kb())
        return
    if data.startswith("gshow_list_"):
        parts = data[11:]
        sep = parts.rfind("_")
        if sep > 0:
            gid = parts[:sep]
        else:
            gid = str(q.message.chat.id)
        # Re-fetch fresh list data
        list_text, items = build_grp_list_text(gid)
        if list_text:
            await safe_edit(q.message, list_text, gclose_kb())
        else:
            await safe_edit(q.message, f"{hdr('Group Reminders')}\nNo active reminders.", gclose_kb())
        return
    if data.startswith("gshow_generic_"):
        # Generic fallback — can't recover original text, just show bot name
        await safe_edit(q.message, GRP_START, gclose_kb())
        return

    if data == "add":
        await rm_home(ctx, ud); ud.clear(); ud["step"] = "message"
        sent = await q.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
        save_p(ud, sent); return
    if data == "list_refresh": ud.clear(); await show_list(q.message, uid, ud); return

    # Repeat change (private)
    if data.startswith("chrep_"):
        row = int(data[6:]); r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message, f"{hdr('Saved ✓')}\n{detail(msg, ds, ts)}\n\nRepeat?", rep_picker_kb(f"chrepv_{row}")); return
    if data.startswith("chrepv_"):
        parts = data.split("_"); row, rep = int(parts[1]), parts[2]
        r, msg, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 5, rep)
        await safe_edit(q.message, f"{hdr('Updated ✓')}\n{detail(msg, ds, ts, fmt_rep(rep))}", home_kb())
        save_home(ud, q.message); return

    # Repeat change (group)
    if data.startswith("gchrep_"):
        tid = data[7:]; row, r = find_by_tid(tid)
        if not r: return
        msg, ds, ts, rs = get_detail(r)
        await safe_edit(q.message, f"{detail(msg, ds, ts)}\n\nRepeat?", rep_picker_kb(f"gchrepv_{tid}")); return
    if data.startswith("gchrepv_"):
        parts = data.split("_"); tid, rep = parts[1], parts[2]
        row, r = find_by_tid(tid)
        if not r: return
        sheet.update_cell(row, 5, rep)
        msg, ds, ts, _ = get_detail(r)
        sub_info = gsub_text(tid)
        await safe_edit(q.message, f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, fmt_rep(rep))}\n\n{sub_info}", gjoin_kb(tid))
        ctx.bot_data[f"gm_{tid}"] = {"c": str(q.message.chat.id), "m": q.message.message_id}; return

    # Group callbacks
    if data.startswith(("gjoin_", "gskip_", "gdone_", "gsnzp_", "gsnzb_", "gsnz_")):
        await _btn_group(q, ctx, uid, data); return
    if data.startswith(("cal_", "day_")): await _btn_cal(q, ctx, ud, uid, data); return
    if data.startswith(("view_", "snzp_", "snzb_", "snz_", "done_", "crem_")): await _btn_rem(q, ctx, ud, uid, data); return
    if data.startswith(("edit_", "emsg_", "edate_", "etime_")): await _btn_edit(q, ud, uid, data); return
    if data.startswith(("cfg_", "cfgr_", "cfgg_", "tzr_", "tzs_", "gunsub_")): await _btn_cfg(q, ctx, ud, uid, data); return

async def _btn_group(q, ctx, uid, data):
    uid_s = str(uid)
    if data.startswith("gjoin_"):
        tid = data[6:]
        row, r = find_by_tid(tid)
        if not r or str(r[5]) != "active": await q.answer("Already fired.", show_alert=True); return
        if not add_tmember(tid, uid_s, q.from_user.first_name or "User"): await q.answer("Already joined!", show_alert=True); return
        set_gsub(str(q.message.chat.id), uid_s, q.from_user.first_name or "User", True)
        msg, ds, ts, rs = get_detail(r)
        rep = r[4] if len(r) > 4 else "none"
        await safe_edit(q.message, f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, rs)}\n\n{gsub_text(tid)}", gjoin_kb(tid, rep == "none"))
        await q.answer("Joined ✓")
    elif data.startswith("gskip_"):
        tid = data[6:]
        row, r = find_by_tid(tid)
        if not r or str(r[5]) != "active": await q.answer("Already fired.", show_alert=True); return
        ms = get_tmembers(tid)
        if any(str(u) == uid_s for u, _, _ in ms): set_tstatus(tid, uid_s, "skipped")
        else: add_tmember(tid, uid_s, q.from_user.first_name or "User", "skipped")
        msg, ds, ts, rs = get_detail(r)
        rep = r[4] if len(r) > 4 else "none"
        await safe_edit(q.message, f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, rs)}\n\n{gsub_text(tid)}", gjoin_kb(tid, rep == "none"))
        await q.answer("Skipped")
    elif data.startswith("gdone_"):
        tid = data[6:]
        ms = get_tmembers(tid)
        st = next((s for u, _, s in ms if str(u) == uid_s), None)
        if st and st != "pending": await safe_edit(q.message, f"<i>Already handled</i>"); return
        set_tstatus(tid, uid_s, "done"); await rm_gpm(ctx, tid, uid_s)
        row, r = find_by_tid(tid)
        msg = str(r[1]).strip() if r else ""
        await safe_edit(q.message, f"{msg}\n\n<b>Done</b> ✓")
        await update_gstatus(ctx, tid, msg)
        if row and r: await check_grp_resolved(ctx, tid, row, r); await update_gstatus(ctx, tid, msg)
    elif data.startswith("gsnzp_"):
        tid = data[6:]
        st = next((s for u, _, s in get_tmembers(tid) if str(u) == uid_s), None)
        if st and st != "pending": await safe_edit(q.message, f"<i>Already handled</i>"); return
        row, r = find_by_tid(tid)
        await safe_edit(q.message, f"{str(r[1]).strip() if r else ''}\n\nSnooze for:", snz_kb(tid, "gsnz"))
    elif data.startswith("gsnzb_"):
        tid = data[6:]
        row, r = find_by_tid(tid)
        await safe_edit(q.message, f"{str(r[1]).strip() if r else ''}\n\n<b>⏰ Group Reminder</b>", gact_kb(tid))
    elif data.startswith("gsnz_"):
        rest = data[5:]
        last_us = rest.rfind("_")
        if last_us < 0: return
        tid, mins = rest[:last_us], int(rest[last_us+1:])
        st = next((s for u, _, s in get_tmembers(tid) if str(u) == uid_s), None)
        if st and st != "pending": await safe_edit(q.message, f"<i>Already handled</i>"); return
        set_tstatus(tid, uid_s, "snoozed"); await rm_gpm(ctx, tid, uid_s)
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
        if step == "edit_date":
            row = ud["editing_row"]; r, msg, ds, ts, rs = row_detail(row)
            await safe_edit(q.message, f"{hdr('Edit Reminder')}\n{msg}\nCurrent: <i>{fmt_date(ds)} · {fmt_time(ts)}</i>\n\nPick new date:",
                cal_kb(yr, mo, f"edit_{row}", "« Back", tz=utz))
        elif step == "g_date":
            msg = ud.get("message", "")
            ts = ud.get("time")
            await safe_edit(q.message, f"{hdr('Group Reminder')}\n{msg}{chr(10)+fmt_time(ts) if ts else ''}\n\nPick a date:",
                cal_kb(yr, mo, "gcancel", "✕ Cancel", tz=utz))
        else:
            msg, ts = ud.get("message", ""), ud.get("time")
            await safe_edit(q.message, f"{hdr('New Reminder')}\n{msg}{chr(10)+fmt_time(ts) if ts else ''}\n\nPick a date:", cal_kb(yr, mo, tz=utz))

    elif data.startswith("day_"):
        ds = data[4:]
        if step == "edit_date":
            row = ud["editing_row"]; r, msg, old_d, ts, rs = row_detail(row)
            if is_past(ds, ts, utz):
                now = datetime.now(utz)
                await safe_edit(q.message, f"{hdr('Edit Reminder')}\n{msg}\n\n{past_msg(ts)}\nPick a future date or change time first.",
                    cal_kb(now.year, now.month, f"edit_{row}", "« Back", tz=utz))
            else:
                sheet.update_cell(row, 3, ds); ud.clear()
                await safe_edit(q.message, f"{hdr('Updated ✓')}\n{msg}\nDate: {fmt_date(old_d)} → <b>{fmt_date(ds)}</b>\nTime: {fmt_time(ts)} · {rs}", home_kb())
                save_home(ud, q.message)

        elif step == "g_date":
            ud["date"] = ds
            msg, ts = ud.get("message", ""), ud.get("time")
            if ts:
                if is_past(ds, ts, utz):
                    now = datetime.now(utz)
                    await safe_edit(q.message, f"{hdr('Group Reminder')}\n{msg}\n{fmt_time(ts)}\n\n{past_msg(ts)}\nPick a future date:",
                        cal_kb(now.year, now.month, "gcancel", "✕ Cancel", tz=utz))
                else:
                    rep = ud.get("repeat", "none")
                    await finish_group_remind(q.message, ctx, uid, ud, rep, edit_msg=True)
            else:
                ud["step"] = "g_time"
                # Use ForceReply for group time input
                try: await q.message.delete()
                except Exception: pass
                sent = await ctx.bot.send_message(
                    chat_id=q.message.chat.id,
                    text=f"{hdr('Group Reminder')}\n{msg}\n{fmt_date(ds)}\n\nEnter time:\n<i>↩️ Reply to this message</i>\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
                    reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 9pm, 9:30 PM"),
                    parse_mode="HTML")
                save_p(ud, sent)

        else:
            ud["date"] = ds
            msg, ts = ud.get("message", ""), ud.get("time")
            if ts:
                if is_past(ds, ts, utz):
                    now = datetime.now(utz)
                    await safe_edit(q.message, f"{hdr('New Reminder')}\n{msg}\n{fmt_time(ts)}\n\n{past_msg(ts)}\nPick a future date:", cal_kb(now.year, now.month, tz=utz))
                else:
                    await save_reminder(q.message, uid, ud, msg, ds, ts, edit_msg=True)
            else:
                ud["step"] = "time"
                await safe_edit(q.message, f"{hdr('New Reminder')}\n{msg}\n{fmt_date(ds)}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", cancel_kb())
                save_p(ud, q.message)

async def _btn_rem(q, ctx, ud, uid, data):
    if data.startswith("view_"):
        row = int(data[5:]); r, msg, ds, ts, rs = row_detail(row)
        st = r[5] if len(r) > 5 else "active"
        btns = [[InlineKeyboardButton("✎ Edit", callback_data=f"edit_{row}"), InlineKeyboardButton("✕ Cancel", callback_data=f"crem_{row}")] if st != "missed"
                else [InlineKeyboardButton("✕ Remove", callback_data=f"crem_{row}")]]
        btns.append([InlineKeyboardButton("« Back", callback_data="list_refresh")])
        await safe_edit(q.message, f"{hdr('Reminder')}\n{msg}\n\n{fmt_date(ds)} · {fmt_time(ts)}\n{rs} · {ST_IC.get(st,'?')} <i>{ST_LB.get(st,st)}</i>", InlineKeyboardMarkup(btns))
    elif data.startswith("snzp_"):
        row = int(data[5:]); r, msg, ds, ts, rs = row_detail(row)
        if len(r) > 5 and r[5] != "pending": await safe_edit(q.message, f"{detail(msg,ds,ts,rs)}\n\n<i>Already handled</i>"); return
        await safe_edit(q.message, f"{detail(msg,ds,ts,rs)}\n\nSnooze for:", snz_kb(row))
    elif data.startswith("snzb_"):
        row = int(data[5:]); r, msg, ds, ts, rs = row_detail(row)
        if len(r) > 5 and r[5] != "pending": await safe_edit(q.message, f"{detail(msg,ds,ts,rs)}\n\n<i>Already handled</i>"); return
        await safe_edit(q.message, f"{msg}\n\n<b>⏰ Reminder</b>", act_kb(row))
    elif data.startswith("snz_"):
        parts = data[4:].split("_"); row, mins = int(parts[0]), int(parts[1])
        r, msg, ds, ts, rs = row_detail(row)
        if len(r) > 5 and r[5] != "pending": await safe_edit(q.message, f"{detail(msg,ds,ts,rs)}\n\n<i>Already handled</i>"); return
        kill_jobs(ctx.job_queue, row); await rm_btns(ctx, row)
        utz = get_tz(uid); nt = datetime.now(utz) + timedelta(minutes=mins)
        rep = r[4] if len(r) > 4 else "none"
        if rep and rep != "none":
            sheet.update_cell(row, 6, "snoozed"); sheet.update_cell(row, 7, 0)
            ctx.job_queue.run_once(snooze_cb, mins * 60, data={"row": row, "chat": uid}, name=f"snooze-{row}")
        else:
            sheet.update_cell(row, 3, nt.strftime("%Y-%m-%d")); sheet.update_cell(row, 4, nt.strftime("%H:%M"))
            sheet.update_cell(row, 6, "active"); sheet.update_cell(row, 7, 0)
        ctx.bot_data.pop(f"r_{row}", None)
        await safe_edit(q.message, f"{detail(msg,ds,ts,rs)}\n\n<b>Snoozed {fmt_snz(mins)}</b> → {fmt_time(nt.strftime('%H:%M'))}")
    elif data.startswith("done_"):
        row = int(data[5:]); r, msg, ds, ts, rs = row_detail(row)
        if len(r) > 5 and r[5] != "pending": await safe_edit(q.message, f"{detail(msg,ds,ts,rs)}\n\n<i>Already handled</i>"); return
        kill_jobs(ctx.job_queue, row); await rm_btns(ctx, row)
        if not advance_rep(row, r): sheet.update_cell(row, 6, "done"); sheet.update_cell(row, 7, 0)
        ctx.bot_data.pop(f"r_{row}", None)
        await safe_edit(q.message, f"{detail(msg,ds,ts,rs)}\n\n<b>Done</b> ✓")
    elif data.startswith("crem_"):
        row = int(data[5:]); kill_jobs(ctx.job_queue, row)
        r, msg, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 6, "cancelled"); sheet.update_cell(row, 7, 0)
        await rm_btns(ctx, row); ctx.bot_data.pop(f"r_{row}", None)
        await safe_edit(q.message, f"{detail(msg,ds,ts)}\n\n<b>Cancelled</b> ✕", home_kb()); save_home(ud, q.message)

async def _btn_edit(q, ud, uid, data):
    if data.startswith("emsg_"):
        row = int(data[5:]); ud["editing_row"], ud["step"] = row, "edit_message"
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message, f"{hdr('Edit Reminder')}\nCurrent: <i>{msg}</i>\n{fmt_date(ds)} · {fmt_time(ts)} · {rs}\n\nEnter new message:",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"edit_{row}")]]))
        save_p(ud, q.message)
    elif data.startswith("edate_"):
        row = int(data[6:]); ud["editing_row"], ud["step"] = row, "edit_date"
        r, msg, ds, ts, rs = row_detail(row)
        utz, now = get_tz(uid), datetime.now(get_tz(uid))
        await safe_edit(q.message, f"{hdr('Edit Reminder')}\n{msg}\nCurrent: <i>{fmt_date(ds)} · {fmt_time(ts)}</i>\n\nPick new date:",
            cal_kb(now.year, now.month, f"edit_{row}", "« Back", tz=utz))
    elif data.startswith("etime_"):
        row = int(data[6:]); ud["editing_row"], ud["step"] = row, "edit_time"
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message, f"{hdr('Edit Reminder')}\n{msg}\nCurrent: <i>{fmt_date(ds)} · {fmt_time(ts)}</i>\n\nEnter new time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"edit_{row}")]]))
        save_p(ud, q.message)
    elif data.startswith("edit_"):
        row = int(data[5:]); ud.clear()
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message, f"{hdr('Edit Reminder')}\n{detail(msg, ds, ts, rs)}\n\nWhat to change?",
            InlineKeyboardMarkup([[InlineKeyboardButton("Message", callback_data=f"emsg_{row}"), InlineKeyboardButton("Date", callback_data=f"edate_{row}"),
                                   InlineKeyboardButton("Time", callback_data=f"etime_{row}")],
                                  [InlineKeyboardButton("« Back", callback_data=f"view_{row}")]]))

async def _btn_cfg(q, ctx, ud, uid, data):
    if data == "cfg_digest_toggle":
        cfg = get_cfg(uid); save_cfg(uid, "digest_on", str(not cfg["digest_on"]).lower())
        await show_settings(q.message, uid)
    elif data == "cfg_digest_time":
        ud.clear(); ud["step"] = "set_digest_time"; cfg = get_cfg(uid)
        await safe_edit(q.message, f"{hdr('Settings')}\nDigest time: <b>{fmt_time(cfg['digest_time'])}</b>\n\nEnter new time:\n<i>e.g. 7am, 8:30 AM</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="cfg_back")]]))
        save_p(ud, q.message)
    elif data == "cfg_retries":
        cfg = get_cfg(uid)
        await safe_edit(q.message, f"{hdr('Settings')}\nRetries: <b>{cfg['max_retries']}×</b>\n\nHow many retries?",
            cfg_picker_kb([1,2,3,5,7,10], str, cfg["max_retries"], "cfgr_"))
    elif data.startswith("cfgr_"):
        save_cfg(uid, "max_retries", int(data[5:])); await show_settings(q.message, uid)
    elif data == "cfg_gap":
        cfg = get_cfg(uid)
        await safe_edit(q.message, f"{hdr('Settings')}\nGap: <b>{cfg['retry_gap']} min</b>\n\nTime between retries?",
            cfg_picker_kb([5,10,15,20,30,60], lambda v: f"{v}m", cfg["retry_gap"], "cfgg_"))
    elif data.startswith("cfgg_"):
        save_cfg(uid, "retry_gap", int(data[5:])); await show_settings(q.message, uid)
    elif data == "cfg_tz":
        cfg = get_cfg(uid)
        btns, row = [], []
        for region in TZ_REGIONS:
            row.append(InlineKeyboardButton(f"{TZ_ICONS.get(region, '🌐')} {region}", callback_data=f"tzr_{region}"))
            if len(row) == 2: btns.append(row); row = []
        if row: btns.append(row)
        btns.append([InlineKeyboardButton("« Back", callback_data="cfg_back")])
        await safe_edit(q.message, f"{hdr('Timezone')}\n\nCurrent: <b>{tz_short(cfg.get('timezone', DEF_TZ))}</b>\n\nPick a region:", InlineKeyboardMarkup(btns))
    elif data.startswith("tzr_"):
        cfg = get_cfg(uid); region = data[4:]
        btns, row = [], []
        for idx, (tz, country, offset, reg) in enumerate(TZ_DATA):
            if reg != region: continue
            lbl = f"[{country}]" if tz == cfg.get("timezone", DEF_TZ) else country
            row.append(InlineKeyboardButton(f"{lbl} {offset}", callback_data=f"tzs_{idx}"))
            if len(row) == 2: btns.append(row); row = []
        if row: btns.append(row)
        btns.append([InlineKeyboardButton("« Regions", callback_data="cfg_tz")])
        await safe_edit(q.message, f"{hdr('Timezone')}\n\n{TZ_ICONS.get(region,'🌐')} <b>{region}</b>\n\nPick:", InlineKeyboardMarkup(btns))
    elif data.startswith("tzs_"):
        idx = int(data[4:])
        if 0 <= idx < len(TZ_DATA): save_cfg(uid, "timezone", TZ_DATA[idx][0]); await show_settings(q.message, uid)
    elif data == "cfg_groups":
        grps = get_user_groups(uid)
        if not grps:
            await safe_edit(q.message, f"{hdr('Groups')}\n\nNo group subscriptions.",
                InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="cfg_back")]]))
            return
        btns = []
        for gid in grps:
            try:
                chat = await ctx.bot.get_chat(int(gid))
                name = chat.title or f"Group {gid}"
            except Exception: name = f"Group {gid}"
            btns.append([InlineKeyboardButton(f"✕ {name}", callback_data=f"gunsub_{gid}")])
        btns.append([InlineKeyboardButton("« Back", callback_data="cfg_back")])
        await safe_edit(q.message, f"{hdr('Group Subscriptions')}\n\nTap to unsubscribe:", InlineKeyboardMarkup(btns))
    elif data.startswith("gunsub_"):
        gid_s, uid_s = data[7:], str(uid)
        try:
            rows = grp_sheet.get_all_values()
            for i, r in enumerate(rows[1:], 2):
                if str(r[0]) == gid_s and str(r[1]) == uid_s:
                    grp_sheet.update_cell(i, 4, "false"); break
        except Exception: pass
        grps = get_user_groups(uid)
        if not grps:
            await safe_edit(q.message, f"{hdr('Groups')}\n\nUnsubscribed ✓\nNo more group subscriptions.",
                InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="cfg_back")]]))
        else:
            btns = []
            for gid in grps:
                try:
                    chat = await ctx.bot.get_chat(int(gid))
                    name = chat.title or f"Group {gid}"
                except Exception: name = f"Group {gid}"
                btns.append([InlineKeyboardButton(f"✕ {name}", callback_data=f"gunsub_{gid}")])
            btns.append([InlineKeyboardButton("« Back", callback_data="cfg_back")])
            await safe_edit(q.message, f"{hdr('Group Subscriptions')}\n\nUnsubscribed ✓\n\nTap to unsubscribe:", InlineKeyboardMarkup(btns))
    elif data == "cfg_back":
        ud.clear(); await show_settings(q.message, uid)

# ============= TEXT HANDLER ===============
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    step = ctx.user_data.get("step", "")
    text = update.message.text.strip()

    if update.effective_chat.type != "private":
        # In groups: handle g_ steps if in correct group
        if step.startswith("g_") and str(update.effective_chat.id) == str(ctx.user_data.get("g_chat", "")):
            await _do_step(update, ctx, step, text)
        return

    if step: await _do_step(update, ctx, step, text)
    else: await _try_nl(update, ctx, text)

async def _try_nl(update, ctx, text):
    uid, utz = update.effective_user.id, get_tz(update.effective_user.id)
    result = parse_nl_partial(text, tz=utz)
    if not result or not result['message']: return

    msg, ts, ds, rep = result['message'], result.get('time'), result.get('date'), result.get('repeat')

    # Need at least time, date, or a recognized prefix to trigger
    has_prefix = bool(re.search(r'(?:remind|reminder|remember|don.?t\s+forget|set\s+reminder)', text, re.I))
    if not ts and not ds and not has_prefix: return

    ud = ctx.user_data; await rm_home(ctx, ud); ud.clear()
    ud["message"] = msg
    if ts: ud["time"] = ts
    if rep: ud["repeat"] = rep

    await handle_nl_result(update.message, ctx, uid, ud, msg, ts, ds, utz)

async def _do_step(update, ctx, step, text):
    ud, uid = ctx.user_data, update.effective_user.id
    utz = get_tz(uid)

    # === PRIVATE MESSAGE STEP (with NL) ===
    if step == "message":
        await del_prompt(ctx, ud)
        result = parse_nl_partial(text, tz=utz)
        if result and (result.get('time') or result.get('date')):
            msg = result['message']
            ud["message"] = msg
            if result.get('time'): ud["time"] = result['time']
            if result.get('repeat'): ud["repeat"] = result['repeat']
            await handle_nl_result(update.message, ctx, uid, ud, msg, result.get('time'), result.get('date'), utz)
        else:
            msg = result['message'] if result else text
            ud["message"] = msg; ud["step"] = "date"
            now = datetime.now(utz)
            sent = await update.message.reply_text(f"{hdr('New Reminder')}\n{msg}\n\nPick a date:",
                reply_markup=cal_kb(now.year, now.month, tz=utz), parse_mode="HTML")
            save_p(ud, sent)

    # === PRIVATE TIME STEP ===
    elif step == "time":
        parsed = parse_time(text)
        if not parsed: await update.message.reply_text("Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML"); return
        ds = ud.get("date", "")
        if is_past(ds, parsed, utz): await update.message.reply_text(f"{past_msg(parsed)}\nEnter a future time:", parse_mode="HTML"); return
        await del_prompt(ctx, ud); ud["time"] = parsed
        await save_reminder(update.message, uid, ud, ud.get("message", ""), ds, parsed)

    # === EDIT STEPS ===
    elif step == "edit_message":
        row = ud.get("editing_row")
        if not row: return
        await rm_prompt(ctx, ud); r, old, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 2, text); ud.clear()
        sent = await update.message.reply_text(f"{hdr('Updated ✓')}\nMessage: {old} → <b>{text}</b>\n{fmt_date(ds)} · {fmt_time(ts)} · {rs}",
            reply_markup=home_kb(), parse_mode="HTML")
        save_home(ud, sent)
    elif step == "edit_time":
        row = ud.get("editing_row")
        if not row: return
        parsed = parse_time(text)
        if not parsed: await update.message.reply_text("Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML"); return
        r, msg, ds, old_t, rs = row_detail(row)
        if is_past(ds, parsed, utz): await update.message.reply_text(f"{past_msg(parsed)}\nEnter a future time:", parse_mode="HTML"); return
        await del_prompt(ctx, ud); sheet.update_cell(row, 4, parsed); ud.clear()
        sent = await update.message.reply_text(f"{hdr('Updated ✓')}\n{msg}\nTime: {fmt_time(old_t)} → <b>{fmt_time(parsed)}</b> · {fmt_date(ds)} · {rs}",
            reply_markup=home_kb(), parse_mode="HTML")
        save_home(ud, sent)
    elif step == "set_digest_time":
        parsed = parse_time(text)
        if not parsed: await update.message.reply_text("Invalid time.\n<i>e.g. 7am, 8:30 AM</i>", parse_mode="HTML"); return
        await del_prompt(ctx, ud); save_cfg(uid, "digest_time", parsed); ud.clear()
        await update.message.reply_text(f"{hdr('Settings')}\nDigest time → <b>{fmt_time(parsed)}</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Settings", callback_data="cfg_back")]]), parse_mode="HTML")

    # === GROUP MESSAGE STEP (with NL) ===
    elif step == "g_message":
        await del_prompt(ctx, ud)
        result = parse_nl_partial(text, tz=utz)
        if result and (result.get('time') or result.get('date')):
            msg = result['message']
            ud["message"] = msg
            if result.get('time'): ud["time"] = result['time']
            if result.get('repeat'): ud["repeat"] = result['repeat']
            await handle_nl_result(update.message, ctx, uid, ud, msg, result.get('time'), result.get('date'), utz, is_group=True)
        else:
            msg = result['message'] if result else text
            ud["message"] = msg; ud["step"] = "g_date"
            now = datetime.now(utz)
            sent = await update.message.reply_text(f"{hdr('Group Reminder')}\n{msg}\n\nPick a date:",
                reply_markup=cal_kb(now.year, now.month, "gcancel", "✕ Cancel", tz=utz), parse_mode="HTML")
            save_p(ud, sent)

    # === GROUP TIME STEP ===
    elif step == "g_time":
        parsed = parse_time(text)
        if not parsed: await update.message.reply_text("Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML"); return
        ds = ud.get("date", "")
        if is_past(ds, parsed, utz): await update.message.reply_text(f"{past_msg(parsed)}\nEnter a future time:", parse_mode="HTML"); return
        await del_prompt(ctx, ud); ud["time"] = parsed
        rep = ud.get("repeat", "none")
        await finish_group_remind(update.message, ctx, uid, ud, rep)

# ============= FIRE & RETRY ==============
async def send_and_track(ctx, chat_id, text, kb, track_key, track_cid):
    try:
        sent = await ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")
        ctx.bot_data[track_key] = {"c": track_cid, "m": sent.message_id}
        return True
    except Exception as e:
        logger.error(f"Send {chat_id}: {e}"); return False

async def snooze_cb(ctx: ContextTypes.DEFAULT_TYPE):
    row, chat = ctx.job.data["row"], ctx.job.data["chat"]
    try: r = sheet.row_values(row)
    except Exception as e: logger.error(f"snooze row {row}: {e}"); return
    if not r or len(r) <= 5 or r[5] != "snoozed": return
    await rm_btns(ctx, row)
    if await send_and_track(ctx, chat, f"{str(r[1]).strip()}\n\n<b>⏰ Reminder</b>", act_kb(row), f"r_{row}", chat):
        sheet.update_cell(row, 6, "pending"); sheet.update_cell(row, 7, 0)
        cfg = get_cfg(int(r[0]) if r[0].isdigit() else r[0])
        ctx.job_queue.run_once(auto_retry, cfg["retry_gap"] * 60, data={"row": row, "chat": chat}, name=f"retry-{row}")

async def auto_retry(ctx: ContextTypes.DEFAULT_TYPE):
    row, chat = ctx.job.data["row"], ctx.job.data["chat"]
    try: r = sheet.row_values(row)
    except Exception as e: logger.error(f"retry {row}: {e}"); return
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
    await send_and_track(ctx, chat, f"{str(r[1]).strip()}\n\n<b>Reminder</b> ({nc}/{max_r})", act_kb(row), f"r_{row}", chat)
    sheet.update_cell(row, 7, nc)
    if nc >= max_r:
        if not advance_rep(row, r): sheet.update_cell(row, 6, "missed"); sheet.update_cell(row, 7, 0)
    else:
        ctx.job_queue.run_once(auto_retry, gap * 60, data={"row": row, "chat": chat}, name=f"retry-{row}")

async def grp_snooze_cb(ctx: ContextTypes.DEFAULT_TYPE):
    tid, uid, uid_s = ctx.job.data["tid"], ctx.job.data["uid"], ctx.job.data["uid_s"]
    st = next((s for u, _, s in get_tmembers(tid) if str(u) == uid_s), None)
    if st != "snoozed": return
    set_tstatus(tid, uid_s, "pending")
    row, r = find_by_tid(tid)
    if not r: return
    msg = str(r[1]).strip()
    await rm_gpm(ctx, tid, uid_s)
    if not await send_and_track(ctx, uid, f"{msg}\n\n<b>⏰ Group Reminder</b>", gact_kb(tid), f"gpm_{tid}_{uid_s}", uid):
        set_tstatus(tid, uid_s, "missed")
    await update_gstatus(ctx, tid, msg)

async def fire_group(ctx, row, v, uid, msg, gid, tid, cfg):
    active = [(u, n) for u, n, s in get_tmembers(tid) if s == "waiting"]
    if not active: sheet.update_cell(row, 6, "done"); return
    for u, n in active: set_tstatus(tid, u, "pending")
    setup = ctx.bot_data.pop(f"gm_{tid}", None)
    if setup:
        try: await ctx.bot.edit_message_reply_markup(chat_id=int(setup["c"]), message_id=setup["m"], reply_markup=None)
        except Exception: pass
    try:
        status = await ctx.bot.send_message(chat_id=int(gid), text=gstatus_text(tid, msg), parse_mode="HTML")
        ctx.bot_data[f"gs_{tid}"] = {"c": int(gid), "m": status.message_id}
    except Exception as e: logger.error(f"[FIRE] Group {gid}: {e}")
    for u, n in active:
        if not await send_and_track(ctx, int(u), f"{msg}\n\n<b>⏰ Group Reminder</b>", gact_kb(tid), f"gpm_{tid}_{u}", int(u)):
            set_tstatus(tid, u, "missed")
    sheet.update_cell(row, 6, "pending"); sheet.update_cell(row, 7, 0)
    ctx.job_queue.run_once(grp_retry, cfg.get("retry_gap", DEF_RETRY_GAP) * 60,
        data={"tid": tid, "row": row, "gid": gid}, name=f"gretry-{tid}")

async def grp_retry(ctx: ContextTypes.DEFAULT_TYPE):
    tid, row, gid = ctx.job.data["tid"], ctx.job.data["row"], ctx.job.data["gid"]
    try: r = sheet.row_values(row)
    except Exception as e: logger.error(f"[GRETRY] {row}: {e}"); return
    if not r or len(r) <= 5 or r[5] != "pending": return
    creator = int(r[0]) if r[0].isdigit() else r[0]
    cfg = get_cfg(creator)
    max_r, gap = cfg["max_retries"], cfg["retry_gap"]
    count = int(r[6]) if len(r) > 6 and r[6].isdigit() else 0
    pending = [(u, n) for u, n, s in get_tmembers(tid) if s == "pending"]
    msg = str(r[1]).strip()
    if not pending or count >= max_r:
        for u, n in pending: set_tstatus(tid, u, "missed")
        await update_gstatus(ctx, tid, msg); await check_grp_resolved(ctx, tid, row, r); return
    nc = count + 1
    for u, n in pending:
        await rm_gpm(ctx, tid, u)
        if not await send_and_track(ctx, int(u), f"{msg}\n\n<b>Group Reminder</b> ({nc}/{max_r})", gact_kb(tid), f"gpm_{tid}_{u}", int(u)):
            set_tstatus(tid, u, "missed")
    sheet.update_cell(row, 7, nc); await update_gstatus(ctx, tid, msg)
    if nc >= max_r:
        for u, n in pending: set_tstatus(tid, u, "missed")
        await update_gstatus(ctx, tid, msg); await check_grp_resolved(ctx, tid, row, r)
    else:
        ctx.job_queue.run_once(grp_retry, gap * 60, data={"tid": tid, "row": row, "gid": gid}, name=f"gretry-{tid}")

# ============= DAILY DIGEST ==============
async def check_digest(ctx: ContextTypes.DEFAULT_TYPE):
    try: cfg_rows = cfg_sheet.get_all_values()
    except Exception:
        try: client.login(); cfg_rows = cfg_sheet.get_all_values()
        except Exception: return
    for r in cfg_rows[1:]:
        if len(r) < 3 or str(r[1]).lower() != "true": continue
        tz_name = str(r[5]) if len(r) > 5 and r[5] else DEF_TZ
        user_tz = safe_tz(tz_name); now = datetime.now(user_tz)
        if norm_time(r[2]) != now.strftime("%H:%M"): continue
        try: uid_int = int(r[0])
        except (ValueError, TypeError): continue
        try: rem_rows = sheet.get_all_values()
        except Exception: continue
        today = now.strftime("%Y-%m-%d")
        items = [v for v in rem_rows[1:] if len(v) >= 6 and str(v[0]) == str(r[0])
                 and str(v[5]).strip().lower() in ("active", "snoozed") and norm_date(str(v[2]).strip()) == today
                 and not (len(v) > 7 and str(v[7]).strip())]
        items.sort(key=lambda x: norm_time(str(x[3]).strip()))
        if items:
            lines = [f"☀️ <b>Good morning!</b>\n{DIV}\n\nToday — {now.strftime('%-d %b')}\n"]
            for v in items:
                msg = str(v[1]).strip()
                lines.append(f"  {fmt_time(norm_time(str(v[3]).strip()))} · {msg[:30] + '…' if len(msg) > 30 else msg}")
            lines.append(f"\n{len(items)} reminder{'s' if len(items) != 1 else ''} today")
        else:
            lines = [f"☀️ <b>Good morning!</b>\n{DIV}\n\nToday — {now.strftime('%-d %b')}\n", "No reminders today. Enjoy your day!"]
        try: await ctx.bot.send_message(chat_id=uid_int, text="\n".join(lines), reply_markup=home_kb(), parse_mode="HTML")
        except Exception as e: logger.error(f"[DIGEST] {r[0]}: {e}")

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
        cfg_map[uid_s] = {"retry_gap": int(r[4]) if len(r) > 4 and r[4] else DEF_RETRY_GAP, "max_retries": int(r[3]) if len(r) > 3 and r[3] else DEF_RETRIES}
    try: vals = sheet.get_all_values()
    except Exception:
        try: client.login(); vals = sheet.get_all_values()
        except Exception as e: logger.error(f"[CRON] {e}"); return
    for idx, v in enumerate(vals[1:], 2):
        if len(v) < 7 or str(v[5]).strip().lower() != "active": continue
        uid_s = str(v[0])
        user_tz = safe_tz(tz_map.get(uid_s, DEF_TZ)); now = datetime.now(user_tz)
        if norm_date(str(v[2]).strip()) != now.strftime("%Y-%m-%d"): continue
        if norm_time(str(v[3]).strip()) != now.strftime("%H:%M"): continue
        uid = int(v[0]) if v[0].isdigit() else v[0]
        msg = str(v[1]).strip()
        gid = str(v[7]).strip() if len(v) > 7 else ""
        tid = str(v[8]).strip() if len(v) > 8 else ""
        logger.info(f"[CRON] FIRE {idx}: '{msg[:30]}' uid={uid} gid={gid}")
        if gid and tid:
            await fire_group(ctx, idx, v, uid, msg, gid, tid, cfg_map.get(uid_s, {}))
        else:
            kill_jobs(ctx.job_queue, idx); await rm_btns(ctx, idx)
            if await send_and_track(ctx, uid, f"{msg}\n\n<b>⏰ Reminder</b>", act_kb(idx), f"r_{idx}", uid):
                sheet.update_cell(idx, 6, "pending"); sheet.update_cell(idx, 7, 0)
                gap = cfg_map.get(uid_s, {"retry_gap": DEF_RETRY_GAP})["retry_gap"]
                ctx.job_queue.run_once(auto_retry, gap * 60, data={"row": idx, "chat": uid}, name=f"retry-{idx}")

# ============= MAIN ======================
def main():
    app = Application.builder().token(TOKEN).job_queue(JobQueue()).post_init(post_init).build()
    for cmd, fn in [("start", start), ("add", add_cmd), ("list", list_cmd), ("remind", remind_cmd), ("settings", settings_cmd), ("info", info_cmd)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(check_reminders, interval=60, first=0)
    app.job_queue.run_repeating(check_digest, interval=60, first=10)
    print("Smart Reminder Bot Running")
    app.run_polling()

if __name__ == "__main__":
    main()
