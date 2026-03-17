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

DIV = "\u2501" * 20

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

TZ_REGIONS = list(dict.fromkeys(t[3] for t in TZ_DATA))
TZ_ICONS = {"Asia": "\U0001f30f", "Europe": "\U0001f30d", "Americas": "\U0001f30e", "Oceania": "\U0001f30f", "Africa": "\U0001f30d"}

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
cfg_sheet = get_or_create_sheet("Settings", ["user_id", "digest_on", "digest_time", "max_retries", "retry_gap", "timezone", "username"])
grp_sheet = get_or_create_sheet("GroupMembers", ["group_id", "user_id", "first_name", "username", "subscribed"])
task_sheet = get_or_create_sheet("TaskMembers", ["task_id", "user_id", "first_name", "status"])

# ============= FORMATTERS ================
def hdr(t):
    return "<b>" + t + "</b>\n" + DIV

def detail(msg, ds, ts, rs=None):
    p = [fmt_date(ds), fmt_time(ts)]
    if rs:
        p.append(rs)
    return msg + "\n" + " \u00b7 ".join(p)

def fmt_date(ds):
    try:
        return datetime.strptime(norm_date(ds), "%Y-%m-%d").strftime("%-d %b")
    except Exception:
        return str(ds)

def fmt_time(ts):
    try:
        h, m = map(int, norm_time(ts).split(":"))
        ap = "AM" if h < 12 else "PM"
        hd = h % 12 or 12
        return str(hd) + ":" + f"{m:02d}" + " " + ap
    except Exception:
        return str(ts)

ST_IC = {"active": "\u25cb", "pending": "\u25cf", "missed": "\u2717", "snoozed": "\u25f7"}
ST_LB = {"active": "Active", "pending": "Pending", "missed": "Missed", "snoozed": "Snoozed"}
GT_IC = {"waiting": "\u23f3", "pending": "\u23f3", "done": "\u2705", "snoozed": "\u25f7", "missed": "\u2717"}

def fmt_rep(r):
    s = str(r)
    if s.startswith("custom:"):
        days = s.split(":")[1].split(",") if ":" in s else []
        if not days:
            return "Custom"
        if days == ["mon", "tue", "wed", "thu", "fri"]:
            return "Mon\u2013Fri"
        if days == ["sat", "sun"]:
            return "Weekends"
        if days == DAY_KEYS:
            return "Every day"
        return ", ".join(d.capitalize() for d in days)
    mp = {"none": "Once", "daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "hourly": "Hourly"}
    return mp.get(s, s)

def fmt_snz(m):
    if m < 60:
        return str(m) + " min"
    hrs = m // 60
    return str(hrs) + " hr" + ("s" if hrs >= 2 else "")

def tz_label(n):
    for tz, c, _, _ in TZ_DATA:
        if tz == n:
            return c
    return n.split("/")[-1].replace("_", " ")

def tz_short(n):
    for tz, c, o, _ in TZ_DATA:
        if tz == n:
            return c + " (" + o + ")"
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
        try:
            client.login()
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
    except Exception:
        return
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == uid_s:
            cfg_sheet.update_cell(i, col, str(value))
            return
    get_cfg(uid)
    try:
        rows = cfg_sheet.get_all_values()
    except Exception:
        return
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == uid_s:
            cfg_sheet.update_cell(i, col, str(value))
            return

def update_username(uid, username):
    if not username:
        return
    uid_s = str(uid)
    uname = username.lower().strip()
    try:
        rows = cfg_sheet.get_all_values()
    except Exception:
        return
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == uid_s:
            current = r[6].strip().lower() if len(r) > 6 else ""
            if current != uname:
                if len(r) < 7:
                    cfg_sheet.update_cell(i, 7, uname)
                else:
                    cfg_sheet.update_cell(i, 7, uname)
            return
    get_cfg(uid)
    try:
        rows = cfg_sheet.get_all_values()
    except Exception:
        return
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == uid_s:
            cfg_sheet.update_cell(i, 7, uname)
            return

def get_detail(r):
    msg = str(r[1]).strip() if len(r) > 1 else ""
    ds = norm_date(r[2]) if len(r) > 2 else ""
    ts = norm_time(r[3]) if len(r) > 3 else ""
    rs = fmt_rep(r[4]) if len(r) > 4 else ""
    return (msg, ds, ts, rs)

def row_detail(row):
    r = sheet.row_values(row)
    return (r,) + get_detail(r)

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
    return "\u26a0 " + fmt_time(ts) + " has already passed today."

def is_custom_day_match(rep, now):
    if not str(rep).startswith("custom:"):
        return True
    days = str(rep).split(":")[1].split(",") if ":" in str(rep) else []
    today_abbr = now.strftime("%a").lower()[:3]
    return today_abbr in days

def advance_rep(row, r):
    rep = r[4] if len(r) > 4 else "none"
    if not rep or rep == "none":
        return False
    d = datetime.strptime(norm_date(r[2]), "%Y-%m-%d")
    nd = None
    if rep == "daily":
        nd = d + timedelta(days=1)
    elif rep == "weekly":
        nd = d + timedelta(days=7)
    elif rep == "monthly":
        mo, yr = d.month + 1, d.year
        if mo > 12:
            mo, yr = 1, yr + 1
        try:
            nd = d.replace(year=yr, month=mo)
        except ValueError:
            return False
    elif rep == "hourly":
        h, mi = map(int, norm_time(r[3]).split(":"))
        h += 1
        if h >= 24:
            h = 0
            nd = d + timedelta(days=1)
        else:
            nd = d
        sheet.update_cell(row, 4, f"{h:02d}:{mi:02d}")
        sheet.update_cell(row, 3, nd.strftime("%Y-%m-%d"))
        sheet.update_cell(row, 6, "active")
        sheet.update_cell(row, 7, 0)
        return True
    elif rep.startswith("custom:"):
        days = rep.split(":")[1].split(",") if ":" in rep else []
        if not days:
            return False
        for offset in range(1, 8):
            candidate = d + timedelta(days=offset)
            if candidate.strftime("%a").lower()[:3] in days:
                nd = candidate
                break
        if not nd:
            return False
    else:
        return False
    sheet.update_cell(row, 3, nd.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, "active")
    sheet.update_cell(row, 7, 0)
    return True

def kill_jobs(jq, name_prefix):
    names = [f"retry-{name_prefix}", f"snooze-{name_prefix}"] if isinstance(name_prefix, int) else [name_prefix]
    for n in names:
        for j in jq.get_jobs_by_name(n):
            j.schedule_removal()

# ============= GROUP DATA =================
def gen_tid():
    return "t" + str(int(time_module.time()))

def grp_read(sheet_ref, filter_fn):
    try:
        rows = sheet_ref.get_all_values()
    except Exception:
        return []
    return [r for r in rows[1:] if filter_fn(r)]

def get_gsubs(gid):
    return [(r[1], r[2], r[3] if len(r) > 4 else "") for r in grp_read(grp_sheet, lambda r: str(r[0]) == str(gid) and str(r[4] if len(r) > 4 else r[3]).lower() == "true")]

def set_gsub(gid, uid, name, username="", sub=True):
    gid_s, uid_s = str(gid), str(uid)
    uname = username.lower().strip() if username else ""
    try:
        rows = grp_sheet.get_all_values()
    except Exception:
        rows = [["h"]]
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == gid_s and str(r[1]) == uid_s:
            grp_sheet.update_cell(i, 3, name)
            if uname:
                grp_sheet.update_cell(i, 4, uname)
            grp_sheet.update_cell(i, 5, str(sub).lower())
            return
    grp_sheet.append_row([gid_s, uid_s, name, uname, str(sub).lower()], value_input_option="RAW")

def get_tmembers(tid):
    return [(r[1], r[2], r[3]) for r in grp_read(task_sheet, lambda r: str(r[0]) == str(tid))]

def add_tmember(tid, uid, name, st="waiting"):
    uid_s = str(uid)
    try:
        rows = task_sheet.get_all_values()
    except Exception:
        rows = [["h"]]
    if any(str(r[0]) == str(tid) and str(r[1]) == uid_s for r in rows[1:]):
        return False
    task_sheet.append_row([str(tid), uid_s, name, st], value_input_option="RAW")
    return True

def set_tstatus(tid, uid, st):
    uid_s = str(uid)
    try:
        rows = task_sheet.get_all_values()
    except Exception:
        return
    for i, r in enumerate(rows[1:], 2):
        if str(r[0]) == str(tid) and str(r[1]) == uid_s:
            task_sheet.update_cell(i, 4, st)
            return

def find_by_tid(tid):
    try:
        rows = sheet.get_all_values()
    except Exception:
        return None, None
    for i, r in enumerate(rows[1:], 2):
        if len(r) > 8 and str(r[8]) == str(tid):
            return i, r
    return None, None

def gstatus_text(tid, msg):
    ms = get_tmembers(tid)
    active = [(u, n, s) for u, n, s in ms if s != "skipped"]
    if not active:
        return "\u23f0 " + msg + "\n\nNo subscribers"
    all_resolved = all(s in ("done", "missed") for _, _, s in active)
    all_done = all(s == "done" for _, _, s in active)
    if all_resolved and all_done:
        names = ", ".join(n for _, n, _ in active)
        return msg + "\n\n\u2705 All done \u00b7 " + names
    default_ic = "\u23f3"
    prefix = "\u23f0 " if not all_resolved else ""
    parts = []
    for _, n, s in active:
        ic = GT_IC.get(s, default_ic)
        parts.append(ic + " " + n)
    return prefix + msg + "\n\n" + " \u00b7 ".join(parts)

def gsub_text(tid):
    active = [(u, n) for u, n, s in get_tmembers(tid) if s != "skipped"]
    if active:
        return str(len(active)) + " subscribed: " + ", ".join(n for _, n in active)
    return "0 subscribed"

async def update_gstatus(ctx, tid, msg):
    info = ctx.bot_data.get("gs_" + tid)
    if not info:
        return
    try:
        await ctx.bot.edit_message_text(chat_id=info["c"], message_id=info["m"], text=gstatus_text(tid, msg), parse_mode="HTML")
    except Exception:
        pass

async def check_grp_resolved(ctx, tid, row, r):
    active = [(u, n, s) for u, n, s in get_tmembers(tid) if s != "skipped"]
    if not active or not all(s in ("done", "missed") for _, _, s in active):
        return
    for j in ctx.job_queue.get_jobs_by_name("gretry-" + tid):
        j.schedule_removal()
    if not advance_rep_grp(row, r, tid):
        sheet.update_cell(row, 6, "done")
    sheet.update_cell(row, 7, 0)

def advance_rep_grp(row, r, tid):
    if not advance_rep(row, r):
        return False
    try:
        rows = task_sheet.get_all_values()
    except Exception:
        return True
    for i, tr in enumerate(rows[1:], 2):
        if str(tr[0]) == str(tid) and str(tr[3]) != "skipped":
            task_sheet.update_cell(i, 4, "waiting")
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

async def rm_prompt(ctx, ud):
    mid, cid = ud.pop("p_mid", None), ud.pop("p_cid", None)
    if mid and cid:
        try:
            await ctx.bot.edit_message_reply_markup(chat_id=cid, message_id=mid, reply_markup=None)
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
    ud["p_mid"] = msg.message_id
    ud["p_cid"] = msg.chat.id

async def rm_btns(ctx, row):
    prev = ctx.bot_data.pop("r_" + str(row), None)
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
    ud["h_mid"] = msg.message_id
    ud["h_cid"] = msg.chat.id

async def rm_gpm(ctx, tid, uid_s):
    key = "gpm_" + tid + "_" + uid_s
    old = ctx.bot_data.pop(key, None)
    if old:
        try:
            await ctx.bot.edit_message_reply_markup(chat_id=old["c"], message_id=old["m"], reply_markup=None)
        except Exception:
            pass

def get_username(user):
    if user and getattr(user, 'username', None):
        return user.username.lower().strip()
    return ""

async def auto_minimize(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    mid_key = "modified_" + str(d["m"])
    if ctx.bot_data.get(mid_key):
        return
    show_kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f4cb Show", callback_data=d["show_cb"])]])
    try:
        await ctx.bot.edit_message_text(
            chat_id=d["c"], message_id=d["m"],
            text=d["min_text"], reply_markup=show_kb, parse_mode="HTML")
    except Exception:
        pass

def cancel_amin(ctx, mid):
    for j in ctx.job_queue.get_jobs_by_name("amin_" + str(mid)):
        j.schedule_removal()

# ============= UI ========================
HOME_TEXT = (
    "<b>Smart Reminder Bot</b>\n" + DIV + "\n"
    "Type your reminder below:\n\n"
    "<i>Buy milk tomorrow at 5pm</i>\n"
    "<i>Gym at 6pm daily</i>\n"
    "<i>Meeting Monday 10am weekly</i>\n"
    "<i>Call mom in 30 min</i>\n\n"
    "Or tap \uff0b New for step-by-step.\n"
    "Use /list to view all."
)

def home_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("\uff0b New", callback_data="add")]])

def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("\u2715 Cancel", callback_data="cancel")]])

def act_kb(row):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Snooze", callback_data="snzp_" + str(row)), InlineKeyboardButton("Done", callback_data="done_" + str(row))]])

def gact_kb(tid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Snooze", callback_data="gsnzp_" + tid), InlineKeyboardButton("Done", callback_data="gdone_" + tid)]])

def close_show_kb(show_cb):
    return InlineKeyboardMarkup([[InlineKeyboardButton("\u2715 Close", callback_data="pclose_" + show_cb), InlineKeyboardButton("\uff0b New", callback_data="add")]])

def show_only_kb(show_cb):
    return InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f4cb Show", callback_data=show_cb)]])

def saved_kb(row, rep):
    r = str(rep)
    row1 = [InlineKeyboardButton("\u270e Edit", callback_data="edit_" + str(row))]
    if r == "none" or not r:
        row1.append(InlineKeyboardButton("\U0001f501 Repeat", callback_data="chrep_" + str(row)))
    return InlineKeyboardMarkup([row1, [InlineKeyboardButton("\uff0b New", callback_data="add")]])

def gjoin_kb(tid, show_rep=False):
    btns = [[InlineKeyboardButton("\uff0b Count Me In", callback_data="gjoin_" + tid), InlineKeyboardButton("\u2715 Skip", callback_data="gskip_" + tid)]]
    if show_rep:
        btns.append([InlineKeyboardButton("\U0001f501 Repeat", callback_data="gchrep_" + tid)])
    return InlineKeyboardMarkup(btns)

def rep_picker_kb(prefix):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Daily", callback_data=prefix + "_daily"),
         InlineKeyboardButton("Weekly", callback_data=prefix + "_weekly")],
        [InlineKeyboardButton("Monthly", callback_data=prefix + "_monthly"),
         InlineKeyboardButton("Hourly", callback_data=prefix + "_hourly")],
        [InlineKeyboardButton("Customize", callback_data=prefix + "_custom")]])

def custom_days_kb(selected, prefix):
    row1, row2 = [], []
    for i, (name, key) in enumerate(zip(DAY_NAMES, DAY_KEYS)):
        lbl = "[" + name + "]" if key in selected else name
        btn = InlineKeyboardButton(lbl, callback_data="cday_" + prefix + "_" + key)
        if i < 4:
            row1.append(btn)
        else:
            row2.append(btn)
    btns = [row1, row2]
    btns.append([
        InlineKeyboardButton("Mon\u2013Fri", callback_data="cday_" + prefix + "_weekdays"),
        InlineKeyboardButton("All", callback_data="cday_" + prefix + "_all"),
        InlineKeyboardButton("Clear", callback_data="cday_" + prefix + "_clear"),
    ])
    if selected:
        btns.append([InlineKeyboardButton("\u2713 Save", callback_data="cdaysave_" + prefix)])
    btns.append([InlineKeyboardButton("\u00ab Back", callback_data="cdayback_" + prefix)])
    return InlineKeyboardMarkup(btns)

def snz_kb(key, pfx="snz"):
    opts = [(15, "15m"), (30, "30m"), (45, "45m"), (60, "1h"), (120, "2h"), (180, "3h"), (300, "5h"), (480, "8h"), (720, "12h")]
    kb = []
    for i in range(0, 9, 3):
        row = []
        for m, l in opts[i:i+3]:
            row.append(InlineKeyboardButton(l, callback_data=pfx + "_" + str(key) + "_" + str(m)))
        kb.append(row)
    kb.append([InlineKeyboardButton("\u00ab Back", callback_data=pfx + "b_" + str(key))])
    return InlineKeyboardMarkup(kb)

def cfg_picker_kb(values, fmt_fn, cur, cb_prefix):
    btns, row = [], []
    for v in values:
        lbl = "[" + fmt_fn(v) + "]" if v == cur else fmt_fn(v)
        row.append(InlineKeyboardButton(lbl, callback_data=cb_prefix + str(v)))
        if len(row) == 3:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([InlineKeyboardButton("\u00ab Back", callback_data="cfg_back")])
    return InlineKeyboardMarkup(btns)

def gmin_kb(show_cb):
    return InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f4cb Show", callback_data=show_cb)]])

def gclose_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("\u2715 Close", callback_data="gclose")]])

# ============= CALENDAR ==================
def cal_kb(year, month, back_cb="cancel", back_txt="\u2715 Cancel", tz=None):
    now = datetime.now(tz or safe_tz(DEF_TZ))
    mn = cal_module.month_name[month]
    kb = [[InlineKeyboardButton(mn + " " + str(year), callback_data="noop")]]
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
                lbl = "[" + str(day) + "]" if datetime(year, month, day).date() == now.date() else str(day)
                row.append(InlineKeyboardButton(lbl, callback_data="day_" + ds))
        kb.append(row)
    td = now.strftime("%Y-%m-%d")
    tm = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    kb.append([InlineKeyboardButton("Today", callback_data="day_" + td), InlineKeyboardButton("Tomorrow", callback_data="day_" + tm)])
    nm, ny = (month % 12) + 1, year + (1 if month == 12 else 0)
    pm_m, py = ((month - 2) % 12) + 1, year - (1 if month == 1 else 0)
    nav = []
    if datetime(py, pm_m, 1) >= datetime(now.year, now.month, 1):
        nav.append(InlineKeyboardButton("\u2039", callback_data="cal_" + str(py) + "_" + f"{pm_m:02d}"))
    else:
        nav.append(InlineKeyboardButton(" ", callback_data="noop"))
    nav.append(InlineKeyboardButton("\u203a", callback_data="cal_" + str(ny) + "_" + f"{nm:02d}"))
    kb.append(nav)
    kb.append([InlineKeyboardButton(back_txt, callback_data=back_cb)])
    return InlineKeyboardMarkup(kb)

# ============= PARSERS ====================
def parse_time(text):
    s = text.strip()
    pats = [
        (r'^(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)$', 'hma'),
        (r'^(\d{1,2})\s*(am|pm)$', 'ha'),
        (r'^(\d{1,2})[:.]\s*(\d{1,2})$', '24'),
    ]
    for pat, mode in pats:
        m = re.match(pat, s, re.I)
        if not m:
            continue
        if mode == 'hma':
            h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        elif mode == 'ha':
            h, mi, ap = int(m.group(1)), 0, m.group(2).lower()
        else:
            h, mi, ap = int(m.group(1)), int(m.group(2)), None
        if ap:
            if ap == 'pm' and h != 12:
                h += 12
            elif ap == 'am' and h == 12:
                h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
    return None

def _to24(h, mi, ap):
    if ap.lower() == 'pm' and h != 12:
        h += 12
    elif ap.lower() == 'am' and h == 12:
        h = 0
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return f"{h:02d}:{mi:02d}"
    return None

def _find_time(text):
    pats = [
        (r'(?:at|by)\s+(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)', 'hma'),
        (r'(?:at|by)\s+(\d{1,2})\s*(am|pm)', 'ha'),
        (r'(?:at|by)\s+(\d{1,2}):(\d{2})\b', '24'),
        (r'(\d{1,2})[:.]\s*(\d{1,2})\s*(am|pm)', 'hma'),
        (r'(\d{1,2})\s*(am|pm)', 'ha'),
    ]
    for pat, mode in pats:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        if mode == 'hma':
            t = _to24(int(m.group(1)), int(m.group(2)), m.group(3))
        elif mode == 'ha':
            t = _to24(int(m.group(1)), 0, m.group(2))
        else:
            h, mi = int(m.group(1)), int(m.group(2))
            t = f"{h:02d}:{mi:02d}" if 0 <= h <= 23 and 0 <= mi <= 59 else None
        if t:
            return t, m.start(), m.end()
    return None

def _find_relative(text, tz=None):
    now = datetime.now(tz or safe_tz(DEF_TZ))
    low = text.lower()
    m = re.search(r'\b(?:in|after)\s+(\d+)\s*(?:min(?:ute)?s?|m)\b', low)
    if m:
        mins = int(m.group(1))
        if mins > 0:
            target = now + timedelta(minutes=mins)
            return target.strftime("%Y-%m-%d"), target.strftime("%H:%M"), m.start(), m.end()
    m = re.search(r'\b(?:in|after)\s+(\d+)\s*(?:hour|hr|h)s?\b', low)
    if m:
        hrs = int(m.group(1))
        if hrs > 0:
            target = now + timedelta(hours=hrs)
            return target.strftime("%Y-%m-%d"), target.strftime("%H:%M"), m.start(), m.end()
    m = re.search(r'\b(?:in|after)\s+(\d+)\s*days?\b', low)
    if m:
        days = int(m.group(1))
        if days > 0:
            target = now + timedelta(days=days)
            return target.strftime("%Y-%m-%d"), now.strftime("%H:%M"), m.start(), m.end()
    m = re.search(r'\b(?:in|after)\s+(\d+)\s*weeks?\b', low)
    if m:
        weeks = int(m.group(1))
        if weeks > 0:
            target = now + timedelta(weeks=weeks)
            return target.strftime("%Y-%m-%d"), now.strftime("%H:%M"), m.start(), m.end()
    return None

def _find_date(text, tz=None):
    now = datetime.now(tz or safe_tz(DEF_TZ))
    low = text.lower()
    for pat, delta in [(r'\bday\s+after\s+tomorrow\b', 2), (r'\b(today|tonight)\b', 0), (r'\b(tomorrow|tmrw|tmr)\b', 1), (r'\bnext\s+week\b', 7)]:
        m = re.search(pat, low)
        if m:
            return (now + timedelta(days=delta)).strftime("%Y-%m-%d"), m.start(), m.end()
    days_full = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    days_abr = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    for i, (full, abr) in enumerate(zip(days_full, days_abr)):
        m = re.search(r'\b(?:on\s+)?(' + full + '|' + abr + r')\b', low)
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
                    if mo > 12:
                        mo, yr = 1, yr + 1
                    d = d.replace(year=yr, month=mo)
                return d.strftime("%Y-%m-%d"), m.start(), m.end()
            except ValueError:
                pass
    months_full = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december']
    months_abr = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
    for mi, (mf, ma) in enumerate(zip(months_full, months_abr), 1):
        for pt in [r'\b(?:on\s+)?(' + mf + '|' + ma + r')\s+(\d{1,2})\b', r'\b(?:on\s+)?(\d{1,2})\s+(' + mf + '|' + ma + r')\b']:
            m = re.search(pt, low)
            if m:
                g1, g2 = m.group(1), m.group(2)
                day = int(g2) if g1.isalpha() else int(g1)
                try:
                    d = datetime(now.year, mi, day)
                    if d.date() < now.date():
                        d = datetime(now.year + 1, mi, day)
                    return d.strftime("%Y-%m-%d"), m.start(), m.end()
                except ValueError:
                    pass
    return None

def _find_repeat(text, tz=None):
    low = text.lower()
    now = datetime.now(tz or safe_tz(DEF_TZ))
    days_full = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    days_abr = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    for i, (full, abr) in enumerate(zip(days_full, days_abr)):
        m = re.search(r'\bevery\s+(' + full + '|' + abr + r')\b', low)
        if m:
            d = (i - now.weekday()) % 7
            if d == 0:
                d = 0
            target = (now + timedelta(days=d)).strftime("%Y-%m-%d")
            return 'weekly', m.start(), m.end(), target
    m = re.search(r'\b(?:every\s*day|daily)\b', low)
    if m:
        return 'daily', m.start(), m.end(), None
    m = re.search(r'\b(?:every\s*week|weekly)\b', low)
    if m:
        return 'weekly', m.start(), m.end(), None
    m = re.search(r'\b(?:every\s*month|monthly)\b', low)
    if m:
        return 'monthly', m.start(), m.end(), None
    m = re.search(r'\b(?:every\s*hour|hourly)\b', low)
    if m:
        return 'hourly', m.start(), m.end(), None
    m = re.search(r'\b(?:once|one[\s-]?time|no\s*repeat)\b', low)
    if m:
        return 'none', m.start(), m.end(), None
    return None

def _clean(text, spans):
    for s, e in sorted([x for x in spans if x], key=lambda x: x[0], reverse=True):
        text = text[:s] + text[e:]
    prefixes = [
        r'^\s*remind\s+me\s+to\s+',
        r'^\s*remind\s+to\s+',
        r'^\s*reminder\s+to\s+',
        r'^\s*reminder\s+',
        r'^\s*remind\s+me\s+',
        r'^\s*remember\s+to\s+',
        r"^\s*don'?t\s+forget\s+to\s+",
        r'^\s*set\s+(?:a\s+)?reminder\s+(?:to\s+|for\s+)?',
    ]
    for f in prefixes:
        text = re.sub(f, '', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip().strip('.,;:!? ')
    text = re.sub(r'^\s*on\s+', '', text, flags=re.I).strip()
    text = re.sub(r'\s+on\s*$', '', text, flags=re.I).strip()
    if text:
        text = text[0].upper() + text[1:]
    return text

def parse_nl_partial(text, tz=None):
    rel = _find_relative(text, tz)
    if rel:
        ds_rel, ts_rel, rs, re_ = rel
        rr = _find_repeat(text, tz)
        rep = rr[0] if rr else None
        spans = [(rs, re_)]
        if rr:
            spans.append((rr[1], rr[2]))
        msg = _clean(text, spans)
        if not msg:
            return None
        return {'message': msg, 'date': ds_rel, 'time': ts_rel, 'repeat': rep}

    tr = _find_time(text)
    dr = _find_date(text, tz)
    rr = _find_repeat(text, tz)
    ts = tr[0] if tr else None
    ds = dr[0] if dr else None
    rep = rr[0] if rr else None
    if rr and len(rr) > 3 and rr[3]:
        ds = rr[3]
    spans = []
    if tr:
        spans.append((tr[1], tr[2]))
    if dr:
        spans.append((dr[1], dr[2]))
    if rr:
        spans.append((rr[1], rr[2]))
    msg = _clean(text, spans)
    if not msg:
        return None
    return {'message': msg, 'date': ds, 'time': ts, 'repeat': rep}

# ============= COMMANDS ===================
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("add", "New reminder"),
        BotCommand("list", "All reminders"),
        BotCommand("month", "Monthly schedule"),
        BotCommand("settings", "Bot settings"),
        BotCommand("info", "About this bot"),
    ], scope=BotCommandScopeAllPrivateChats())
    await app.bot.set_my_commands([
        BotCommand("start", "Bot info & commands"),
        BotCommand("remind", "Group reminder"),
        BotCommand("list", "Active reminders"),
    ], scope=BotCommandScopeAllGroupChats())
    await app.bot.set_my_commands([])

GRP_START = (
    "<b>Smart Reminder Bot</b>\n" + DIV + "\n\n"
    "<b>Commands</b>\n"
    "/remind \u2014 Group reminder\n"
    "/list \u2014 Active reminders\n\n"
    "<b>Examples</b>\n"
    "<code>/remind Buy milk at 5pm</code>\n"
    "<code>/remind Meeting tomorrow 10am daily</code>\n"
    "<code>/remind Call mom in 30 min</code>\n"
    "<code>/remind</code> \u2014 step-by-step\n\n"
    "<i>Tag members to assign:</i>\n"
    "<code>/remind @John Submit report at 5pm</code>"
)

def build_grp_list_text(gid):
    try:
        rows = sheet.get_all_values()
    except Exception:
        rows = []
    items = [(i, r) for i, r in enumerate(rows[1:], 2)
             if len(r) > 7 and str(r[7]).strip() == str(gid) and str(r[5]).strip() in ("active", "pending", "snoozed")]
    if not items:
        return None, []
    lines = [hdr("Group Reminders")]
    for idx, (ri, r) in enumerate(items, 1):
        st = str(r[5]).strip()
        msg = str(r[1]).strip()
        short = msg[:30] + "\u2026" if len(msg) > 30 else msg
        lines.append("\n<b>" + str(idx) + "</b> " + ST_IC.get(st, '?') + " " + short + "\n   " + fmt_date(norm_date(r[2])) + " \u00b7 " + fmt_time(norm_time(r[3])))
    return "\n".join(lines), items

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        gid = str(update.effective_chat.id)
        user = update.effective_user
        set_gsub(gid, user.id, user.first_name or "User", get_username(user), True)
        sent = await update.message.reply_text(GRP_START, reply_markup=gclose_kb(), parse_mode="HTML")
        show_cb = "gshow_start_" + str(sent.message_id)
        ctx.bot_data["gmin_" + str(sent.message_id)] = {"min_text": "<b>Smart Reminder Bot</b>", "show_cb": show_cb, "full_text": GRP_START}
        ctx.job_queue.run_once(auto_minimize, 30, data={
            "c": sent.chat.id, "m": sent.message_id,
            "min_text": "<b>Smart Reminder Bot</b>", "show_cb": show_cb
        }, name="amin_" + str(sent.message_id))
        return
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    user = update.effective_user
    get_cfg(user.id)
    update_username(user.id, get_username(user))
    sent = await update.message.reply_text(HOME_TEXT, reply_markup=home_kb(), parse_mode="HTML")
    save_home(ctx.user_data, sent)

async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /remind here for group reminders.")
        return
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    update_username(update.effective_user.id, get_username(update.effective_user))
    ctx.user_data["step"] = "message"
    sent = await update.message.reply_text(hdr("New Reminder") + "\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
    save_p(ctx.user_data, sent)

async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        gid = str(update.effective_chat.id)
        user = update.effective_user
        set_gsub(gid, user.id, user.first_name or "User", get_username(user), True)
        list_text, items = build_grp_list_text(gid)
        if not list_text:
            sent = await update.message.reply_text(hdr("Group Reminders") + "\nNo active reminders.", reply_markup=gclose_kb(), parse_mode="HTML")
            show_cb = "gshow_list_" + gid + "_" + str(sent.message_id)
            ctx.bot_data["gmin_" + str(sent.message_id)] = {"min_text": "<b>Group Reminders</b> \u2014 No active", "show_cb": show_cb}
            ctx.job_queue.run_once(auto_minimize, 30, data={
                "c": sent.chat.id, "m": sent.message_id,
                "min_text": "<b>Group Reminders</b> \u2014 No active", "show_cb": show_cb
            }, name="amin_" + str(sent.message_id))
            return
        sent = await update.message.reply_text(list_text, reply_markup=gclose_kb(), parse_mode="HTML")
        show_cb = "gshow_list_" + gid + "_" + str(sent.message_id)
        ctx.bot_data["gmin_" + str(sent.message_id)] = {"min_text": "<b>Group Reminders</b>", "show_cb": show_cb, "gid": gid}
        ctx.job_queue.run_once(auto_minimize, 60, data={
            "c": sent.chat.id, "m": sent.message_id,
            "min_text": "<b>Group Reminders</b>", "show_cb": show_cb
        }, name="amin_" + str(sent.message_id))
        return
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    update_username(update.effective_user.id, get_username(update.effective_user))
    await show_list(update.message, update.effective_user.id, ctx, new=True)

async def info_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /info in private chat.")
        return
    update_username(update.effective_user.id, get_username(update.effective_user))
    cfg = get_cfg(update.effective_user.id)
    info_text = (
        hdr("Smart Reminder Bot") + "\n\nSet reminders and get notified on time.\n\n"
        "<b>Smart Input</b>\nJust type naturally:\n"
        "<code>Buy milk tomorrow at 5pm</code>\n"
        "<code>Meeting Monday 10am weekly</code>\n"
        "<code>Call mom in 30 min</code>\n"
        "<code>Gym every monday at 6pm</code>\n"
        "<code>Remind me to pay rent at 9am</code>\n\n"
        "<i>Add \"daily\", \"weekly\", \"monthly\"\nor \"hourly\" to set recurring.</i>\n\n"
        "<b>Features</b>\n\u2022 One-time & recurring reminders\n\u2022 Custom day selection (Mon\u2013Fri)\n\u2022 Calendar date picker\n"
        "\u2022 Flexible time input\n"
        "\u2022 Relative time (in 5 min, in 2 hours)\n"
        "\u2022 Snooze (15m to 12h)\n\u2022 Auto-retry if missed\n\u2022 Edit or cancel anytime\n\u2022 Daily morning digest\n"
        "\u2022 Weekly report\n"
        "\u2022 Monthly schedule (/month)\n"
        "\u2022 Per-user timezone (" + tz_short(cfg['timezone']) + ")\n\n"
        "<b>Group Reminders</b>\n\u2022 Use /remind in groups\n"
        "\u2022 Tag members to assign specific people\n\u2022 Members opt in per reminder\n"
        "\u2022 Track who's done / pending / missed\n\n"
        "<b>Commands</b>\n/add \u2014 New reminder\n/list \u2014 All reminders\n/month \u2014 Monthly schedule\n/remind \u2014 Group reminder\n"
        "/settings \u2014 Bot settings\n/info \u2014 This page"
    )
    show_cb = "pshow_info"
    sent = await update.message.reply_text(info_text, reply_markup=close_show_kb(show_cb), parse_mode="HTML")
    ctx.bot_data["pinfo_" + str(sent.message_id)] = {"text": info_text, "uid": update.effective_user.id}
    ctx.bot_data["pinfo_latest"] = sent.message_id
    ctx.job_queue.run_once(auto_minimize, 60, data={
        "c": sent.chat.id, "m": sent.message_id,
        "min_text": "<b>\u2139\ufe0f Info</b>", "show_cb": show_cb
    }, name="amin_" + str(sent.message_id))

async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Use /remind in groups.\nJust type naturally for personal reminders.")
        return
    ud = ctx.user_data
    ud.clear()
    uid = update.effective_user.id
    utz = get_tz(uid)
    gid = str(update.effective_chat.id)
    user = update.effective_user
    name = user.first_name or "User"
    uname = get_username(user)
    ud["g_chat"] = gid
    ud["g_name"] = name
    set_gsub(gid, uid, name, uname, True)

    tags = extract_tags(update.message)
    logger.info("[REMIND] Tags extracted: %s", tags)
    if tags:
        ud["g_tags"] = tags

    raw = update.message.text or ""
    text = re.sub(r'^/remind(@\w+)?\s*', '', raw.strip(), flags=re.I).strip()
    text = strip_mentions(text, update.message)

    if not text:
        ud["step"] = "g_message"
        sent = await update.message.reply_text(
            hdr("Group Reminder") + "\nType your reminder message:\n<i>\u21a9\ufe0f Reply to this message</i>",
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
        ud["message"] = text
        ud["step"] = "g_date"
        now = datetime.now(utz)
        await update.message.reply_text(
            hdr("Group Reminder") + "\n" + text + "\n\nPick a date:",
            reply_markup=cal_kb(now.year, now.month, "gcancel", "\u2715 Cancel", tz=utz), parse_mode="HTML")

async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    update_username(update.effective_user.id, get_username(update.effective_user))
    await show_settings(update.message, update.effective_user.id, new=True)

def get_user_groups(uid):
    uid_s = str(uid)
    gids = []
    for r in grp_read(grp_sheet, lambda r: str(r[1]) == uid_s and len(r) > 4 and str(r[4]).lower() == "true"):
        if r[0] not in gids:
            gids.append(r[0])
    return gids

async def show_settings(target, uid, new=False):
    cfg = get_cfg(uid)
    d_on = cfg["digest_on"]
    d_time = fmt_time(cfg["digest_time"]) if d_on else "\u2014"
    tz_disp = tz_label(cfg.get("timezone", DEF_TZ))
    grps = get_user_groups(uid)
    txt = hdr("Settings") + "\n\n<b>Digest</b>: " + ("ON" if d_on else "OFF")
    if d_on:
        txt += " \u00b7 " + d_time
    txt += "\n<b>Retries</b>: " + str(cfg['max_retries']) + "\u00d7"
    txt += "\n<b>Gap</b>: " + str(cfg['retry_gap']) + " min"
    txt += "\n<b>Timezone</b>: " + tz_disp
    if grps:
        txt += "\n<b>Groups</b>: " + str(len(grps)) + " subscribed"
    btns = [
        [InlineKeyboardButton("Digest: " + ("ON" if d_on else "OFF"), callback_data="cfg_digest_toggle"),
         InlineKeyboardButton("\u23f0 " + d_time if d_on else "\u2014", callback_data="cfg_digest_time" if d_on else "noop")],
        [InlineKeyboardButton("Retries: " + str(cfg['max_retries']) + "\u00d7", callback_data="cfg_retries"),
         InlineKeyboardButton("Gap: " + str(cfg['retry_gap']) + "m", callback_data="cfg_gap")],
        [InlineKeyboardButton("\U0001f30d " + tz_disp, callback_data="cfg_tz")],
    ]
    if grps:
        btns.append([InlineKeyboardButton("\U0001f465 Groups (" + str(len(grps)) + ")", callback_data="cfg_groups")])
    btns.append([InlineKeyboardButton("\u00ab Back", callback_data="home")])
    kb = InlineKeyboardMarkup(btns)
    if new:
        await target.reply_text(txt, reply_markup=kb, parse_mode="HTML")
    else:
        await safe_edit(target, txt, kb)

# ============= MONTH VIEW =================
async def month_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /month in private chat.")
        return
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    update_username(update.effective_user.id, get_username(update.effective_user))
    uid = update.effective_user.id
    utz = get_tz(uid)
    now = datetime.now(utz)
    txt, kb = build_month_view(uid, now.year, now.month, utz)
    show_cb = "pshow_month"
    sent = await update.message.reply_text(txt, reply_markup=kb, parse_mode="HTML")
    save_home(ctx.user_data, sent)
    ctx.job_queue.run_once(auto_minimize, 60, data={
        "c": sent.chat.id, "m": sent.message_id,
        "min_text": "<b>\U0001f4c5 Monthly Schedule</b>", "show_cb": show_cb
    }, name="amin_" + str(sent.message_id))

def get_user_reminders(uid):
    try:
        rows = sheet.get_all_values()
    except Exception:
        return []
    uid_s = str(uid)
    return [r for r in rows[1:] if len(r) >= 6 and str(r[0]) == uid_s and not (len(r) > 7 and str(r[7]).strip())]

def expand_recur(reminders, start_date, end_date):
    expanded = []
    for r in reminders:
        msg = str(r[1]).strip()
        ds = norm_date(r[2])
        ts = norm_time(r[3])
        rep = str(r[4]).strip() if len(r) > 4 else "none"
        st = str(r[5]).strip() if len(r) > 5 else "active"
        try:
            rem_date = datetime.strptime(ds, "%Y-%m-%d").date()
        except Exception:
            continue
        if rep == "none" or not rep:
            if start_date <= rem_date <= end_date:
                expanded.append({"date": rem_date, "msg": msg, "time": ts, "rep": rep, "status": st})
        elif rep == "daily":
            d = max(rem_date, start_date)
            while d <= end_date:
                expanded.append({"date": d, "msg": msg, "time": ts, "rep": rep, "status": st if d == rem_date else "active"})
                d += timedelta(days=1)
        elif rep == "hourly":
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
        elif rep == "monthly":
            d = rem_date
            for _ in range(12):
                if start_date <= d <= end_date:
                    expanded.append({"date": d, "msg": msg, "time": ts, "rep": rep, "status": st if d == rem_date else "active"})
                mo, yr = d.month + 1, d.year
                if mo > 12:
                    mo, yr = 1, yr + 1
                try:
                    d = d.replace(year=yr, month=mo)
                except ValueError:
                    break
        elif rep.startswith("custom:"):
            cdays = rep.split(":")[1].split(",") if ":" in rep else []
            if not cdays:
                continue
            d = max(rem_date, start_date)
            while d <= end_date:
                if d.strftime("%a").lower()[:3] in cdays:
                    expanded.append({"date": d, "msg": msg, "time": ts, "rep": rep, "status": st if d == rem_date else "active"})
                d += timedelta(days=1)
    return expanded

def group_recur(items):
    grouped = {}
    singles = []
    for item in items:
        key = item['msg'] + "_" + item['time'] + "_" + item['rep']
        if item["rep"] and item["rep"] != "none":
            grouped.setdefault(key, {"msg": item["msg"], "time": item["time"], "rep": item["rep"], "dates": []})
            grouped[key]["dates"].append(item["date"])
        else:
            singles.append(item)
    return singles, list(grouped.values())

def build_month_view(uid, year, month, utz):
    now = datetime.now(utz)
    first_day = datetime(year, month, 1).date()
    last_day_num = cal_module.monthrange(year, month)[1]
    last_day = datetime(year, month, last_day_num).date()

    reminders = get_user_reminders(uid)
    expanded = expand_recur(reminders, first_day, last_day)

    # Always 4 weeks
    week_ranges = [(1, 7), (8, 14), (15, 21), (22, last_day_num)]
    weeks = []
    for s, e in week_ranges:
        ws = datetime(year, month, s).date()
        we = datetime(year, month, min(e, last_day_num)).date()
        week_items = [x for x in expanded if ws <= x["date"] <= we]
        weeks.append({"start": ws, "end": we, "count": len(week_items)})

    total = len(expanded)
    done_count = sum(1 for x in expanded if x["status"] == "done")
    missed_count = sum(1 for x in expanded if x["status"] == "missed")
    upcoming = total - done_count - missed_count

    mn = cal_module.month_name[month]
    lines = ["\U0001f4c5 <b>" + mn + " " + str(year) + "</b>\n" + DIV + "\n"]
    for i, w in enumerate(weeks):
        ws_str = w["start"].strftime("%-d %b")
        we_str = w["end"].strftime("%-d %b")
        current = " \u25c2" if w["start"] <= now.date() <= w["end"] else ""
        cnt = w['count']
        lbl = str(cnt) + " reminder" + ("s" if cnt != 1 else "")
        lines.append("<b>W" + str(i + 1) + "</b>: " + ws_str + "\u2013" + we_str + current + " \u00b7 " + lbl)

    if total:
        lines.append("\nTotal: " + str(total))
        parts = []
        if done_count:
            parts.append("\u2705 " + str(done_count) + " done")
        if missed_count:
            parts.append("\u2717 " + str(missed_count) + " missed")
        if upcoming:
            parts.append("\u25cb " + str(upcoming) + " upcoming")
        if parts:
            lines.append(" \u00b7 ".join(parts))

    btns = []
    num_row = []
    for i in range(4):
        num_row.append(InlineKeyboardButton(str(i + 1), callback_data="mw_" + str(year) + "_" + f"{month:02d}" + "_" + str(i)))
    btns.append(num_row)

    pm_m = ((month - 2) % 12) + 1
    py = year - (1 if month == 1 else 0)
    nm = (month % 12) + 1
    ny = year + (1 if month == 12 else 0)
    nav = [InlineKeyboardButton("\u2039", callback_data="mn_" + str(py) + "_" + f"{pm_m:02d}"),
           InlineKeyboardButton("\u203a", callback_data="mn_" + str(ny) + "_" + f"{nm:02d}")]
    btns.append(nav)
    btns.append([InlineKeyboardButton("\u00ab Back", callback_data="home")])

    return "\n".join(lines), InlineKeyboardMarkup(btns)

def build_week_view(uid, year, month, week_idx, utz):
    now = datetime.now(utz)
    last_day_num = cal_module.monthrange(year, month)[1]

    week_ranges = [(1, 7), (8, 14), (15, 21), (22, last_day_num)]
    if week_idx >= len(week_ranges):
        back_cb = "mn_" + str(year) + "_" + f"{month:02d}"
        return hdr("Week") + "\nInvalid week.", InlineKeyboardMarkup([[InlineKeyboardButton("\u00ab Back", callback_data=back_cb)]])

    s, e = week_ranges[week_idx]
    ws = datetime(year, month, s).date()
    we = datetime(year, month, min(e, last_day_num)).date()

    reminders = get_user_reminders(uid)
    expanded = expand_recur(reminders, ws, we)

    by_date = {}
    for item in expanded:
        by_date.setdefault(item["date"], []).append(item)

    singles, recur_groups = group_recur(expanded)

    ws_str = ws.strftime("%-d %b")
    we_str = we.strftime("%-d %b")
    lines = ["<b>Week " + str(week_idx + 1) + "</b>: " + ws_str + "\u2013" + we_str + "\n" + DIV + "\n"]

    d = ws
    while d <= we:
        day_items = by_date.get(d, [])
        day_singles = [x for x in day_items if x["rep"] == "none" or not x["rep"]]
        if day_singles:
            if d == now.date():
                day_label = "Today, " + d.strftime("%-d %b")
            else:
                day_label = d.strftime("%-d %b, %a")
            lines.append("<b>" + day_label + "</b>")
            for item in sorted(day_singles, key=lambda x: x["time"]):
                ic = ST_IC.get(item["status"], "\u25cb")
                lines.append("  " + ic + " " + fmt_time(item['time']) + " \u00b7 " + item['msg'])
            lines.append("")
        d += timedelta(days=1)

    if recur_groups:
        for grp in recur_groups:
            dates = sorted(grp["dates"])
            rep = grp["rep"]
            if rep == "daily" or rep == "hourly":
                weekdays = [d for d in dates if d.weekday() < 5]
                weekends = [d for d in dates if d.weekday() >= 5]
                if len(dates) >= 5 and weekdays and not weekends:
                    label = "Mon\u2013Fri"
                elif weekends and not weekdays:
                    label = "Weekends"
                else:
                    label = "Daily" if rep == "daily" else "Hourly"
            elif rep.startswith("custom:"):
                label = fmt_rep(rep)
            elif rep == "weekly":
                if dates:
                    label = dates[0].strftime("%A") + "s"
                else:
                    label = "Weekly"
            else:
                label = fmt_rep(rep)
            lines.append("<i>" + label + "</i>\n  \u25cb " + fmt_time(grp['time']) + " \u00b7 " + grp['msg'])

    if not expanded:
        lines.append("No reminders this week.")

    btns = []
    nav_row = []
    if week_idx > 0:
        nav_row.append(InlineKeyboardButton("\u2039 W" + str(week_idx), callback_data="mw_" + str(year) + "_" + f"{month:02d}" + "_" + str(week_idx - 1)))
    if week_idx < 3:
        nav_row.append(InlineKeyboardButton("W" + str(week_idx + 2) + " \u203a", callback_data="mw_" + str(year) + "_" + f"{month:02d}" + "_" + str(week_idx + 1)))
    if nav_row:
        btns.append(nav_row)
    mn = cal_module.month_name[month]
    btns.append([InlineKeyboardButton("\u00ab " + mn + " " + str(year), callback_data="mn_" + str(year) + "_" + f"{month:02d}")])

    return "\n".join(lines), InlineKeyboardMarkup(btns)

# ============= SAVE FUNCTIONS =============
async def save_reminder(target, uid, ud, msg, date, time_str, edit_msg=False):
    rep = ud.get("repeat", "none")
    sheet.append_row([uid, msg, date, time_str, rep, "active", 0, "", ""], value_input_option="RAW")
    try:
        row = len(sheet.get_all_values())
    except Exception:
        row = 0
    ud.clear()
    txt = hdr("Saved \u2713") + "\n" + detail(msg, date, time_str, fmt_rep(rep))
    kb = saved_kb(row, rep) if row > 0 else home_kb()
    if edit_msg:
        await safe_edit(target, txt, kb)
        save_home(ud, target)
    else:
        sent = await target.reply_text(txt, reply_markup=kb, parse_mode="HTML")
        save_home(ud, sent)

async def finish_group_remind(target, ctx, uid, ud, rep, edit_msg=False):
    msg = ud.get("message", "")
    ds = ud.get("date", "")
    ts = ud.get("time", "")
    gid = ud.get("g_chat", "")
    name = ud.get("g_name", "User")
    tags = ud.get("g_tags")
    tid = gen_tid()

    sheet.append_row([uid, msg, ds, ts, rep, "active", 0, gid, tid], value_input_option="RAW")

    subs = get_gsubs(gid)
    logger.info("[FINISH_GRP] tags=%s, gid=%s, tid=%s, subs_count=%d", tags, gid, tid, len(subs))

    if tags:
        tagged_names = []
        for sub_uid, sub_name, sub_uname in subs:
            sub_name_l = sub_name.lower().strip()
            sub_uname_l = sub_uname.lower().strip() if sub_uname else ""
            matched = any(tag == sub_uname_l and sub_uname_l for tag in tags) or any(tag == sub_name_l for tag in tags)
            if matched:
                add_tmember(tid, sub_uid, sub_name, "waiting")
                tagged_names.append(sub_name)
                logger.info("[FINISH_GRP] MATCHED: %s (username=%s)", sub_name, sub_uname)
            else:
                add_tmember(tid, sub_uid, sub_name, "skipped")
                logger.info("[FINISH_GRP] SKIPPED: %s (username=%s)", sub_name, sub_uname)
        sub_info = "For: " + ", ".join(tagged_names) if tagged_names else "No matching subscribers"
    else:
        for sub_uid, sub_name, sub_uname in subs:
            add_tmember(tid, sub_uid, sub_name)
        sub_info = gsub_text(tid)

    txt = hdr("Group Reminder") + "\n" + detail(msg, ds, ts, fmt_rep(rep)) + "\nBy " + name + "\n\n" + sub_info
    show_rep = (rep == "none")
    kb = gjoin_kb(tid, show_rep)
    ud.clear()

    if edit_msg:
        await safe_edit(target, txt, kb)
        ctx.bot_data["gm_" + tid] = {"c": str(target.chat.id), "m": target.message_id}
    else:
        sent = await target.reply_text(txt, reply_markup=kb, parse_mode="HTML")
        ctx.bot_data["gm_" + tid] = {"c": str(sent.chat.id), "m": sent.message_id}

# ============= NL HANDLER =================
async def handle_nl_result(target, ctx, uid, ud, msg, ts, ds, utz, is_group=False):
    if ts:
        if not ds:
            ds = datetime.now(utz).strftime("%Y-%m-%d")
        if is_past(ds, ts, utz):
            ud["step"] = "g_date" if is_group else "date"
            back_cb = "gcancel" if is_group else "cancel"
            title = "Group Reminder" if is_group else "New Reminder"
            now = datetime.now(utz)
            sent = await target.reply_text(
                hdr(title) + "\n" + msg + "\n\n" + past_msg(ts) + "\nPick a future date:",
                reply_markup=cal_kb(now.year, now.month, back_cb, "\u2715 Cancel", tz=utz), parse_mode="HTML")
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
                hdr(title) + "\n" + msg + "\n" + fmt_date(ds) + "\n\nEnter time:\n<i>\u21a9\ufe0f Reply to this message</i>\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
                reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 9pm, 9:30 PM"),
                parse_mode="HTML")
        else:
            sent = await target.reply_text(
                hdr(title) + "\n" + msg + "\n" + fmt_date(ds) + "\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
                reply_markup=cancel_kb(), parse_mode="HTML")
        save_p(ud, sent)
    else:
        ud["step"] = "g_date" if is_group else "date"
        back_cb = "gcancel" if is_group else "cancel"
        title = "Group Reminder" if is_group else "New Reminder"
        now = datetime.now(utz)
        sent = await target.reply_text(
            hdr(title) + "\n" + msg + "\n\nPick a date:",
            reply_markup=cal_kb(now.year, now.month, back_cb, "\u2715 Cancel", tz=utz), parse_mode="HTML")
        save_p(ud, sent)

# ============= SHOW LIST =================
async def show_list(target, uid, ctx, new=False):
    ud = ctx.user_data
    try:
        rows = sheet.get_all_records()
    except Exception:
        try:
            client.login()
            rows = sheet.get_all_records()
        except Exception:
            rows = []
    items = [(i, r) for i, r in enumerate(rows, 2)
             if str(r.get("user_id", "")) == str(uid) and str(r.get("status", "")).strip() in ("active", "pending", "missed", "snoozed")
             and not str(r.get("group_id", "")).strip()]
    if not items:
        t = hdr("Reminders") + "\nNo reminders found."
        kb = home_kb()
        if new:
            sent = await target.reply_text(t, reply_markup=kb, parse_mode="HTML")
            save_home(ud, sent)
        else:
            await safe_edit(target, t, kb)
        return
    lines = [hdr("Reminders")]
    for idx, (ri, r) in enumerate(items, 1):
        st = str(r.get("status", ""))
        msg = str(r.get("message", ""))
        short = msg[:30] + "\u2026" if len(msg) > 30 else msg
        lines.append("\n<b>" + str(idx) + "</b> " + ST_IC.get(st, '?') + " " + short + "\n   " + fmt_date(norm_date(r.get('date', ''))) + " \u00b7 " + fmt_time(norm_time(r.get('time', ''))))
    btns = []
    num_row = []
    for idx, (ri, _) in enumerate(items, 1):
        num_row.append(InlineKeyboardButton(str(idx), callback_data="view_" + str(ri)))
        if len(num_row) == 5:
            btns.append(num_row)
            num_row = []
    if num_row:
        btns.append(num_row)
    btns.append([InlineKeyboardButton("\u00ab Back", callback_data="home")])
    txt = "\n".join(lines)
    kb = InlineKeyboardMarkup(btns)
    if new:
        sent = await target.reply_text(txt, reply_markup=kb, parse_mode="HTML")
        save_home(ud, sent)
        show_cb = "pshow_list"
        ctx.job_queue.run_once(auto_minimize, 60, data={
            "c": sent.chat.id, "m": sent.message_id,
            "min_text": "<b>\U0001f4cb Reminders</b> (" + str(len(items)) + " active)",
            "show_cb": show_cb
        }, name="amin_" + str(sent.message_id))
    else:
        await safe_edit(target, txt, kb)

# ============= BUTTON HANDLERS ===========
async def on_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    ud = ctx.user_data
    uid = q.from_user.id

    if data == "noop":
        return

    if update.effective_chat.type == "private":
        update_username(uid, get_username(q.from_user))

    # Cancel auto-minimize for this message
    cancel_amin(ctx, q.message.message_id)
    # Mark message as modified so auto-minimize won't fire
    ctx.bot_data["modified_" + str(q.message.message_id)] = True

    if data in ("home", "cancel"):
        ud.clear()
        await safe_edit(q.message, HOME_TEXT, home_kb())
        save_home(ud, q.message)
        return
    if data == "gcancel":
        ud.clear()
        await safe_edit(q.message, hdr("Group Reminder") + "\n\nCancelled.")
        return

    # Group close -> MINIMIZE
    if data == "gclose":
        mid = str(q.message.message_id)
        stored = ctx.bot_data.get("gmin_" + mid)
        if stored:
            await safe_edit(q.message, stored["min_text"], gmin_kb(stored["show_cb"]))
        else:
            await safe_edit(q.message, "<b>\u2139\ufe0f</b>", gmin_kb("gshow_generic_" + mid))
        return

    # Private close -> MINIMIZE
    if data.startswith("pclose_"):
        show_cb = data[7:]
        if show_cb == "pshow_list":
            count = "?"
            try:
                uid_s = str(uid)
                r = sheet.get_all_records()
                count = sum(1 for row in r if str(row.get("user_id", "")) == uid_s and str(row.get("status", "")).strip() in ("active", "pending", "missed", "snoozed") and not str(row.get("group_id", "")).strip())
            except Exception:
                pass
            await safe_edit(q.message, "<b>\U0001f4cb Reminders</b> (" + str(count) + " active)", show_only_kb(show_cb))
        elif show_cb == "pshow_info":
            await safe_edit(q.message, "<b>\u2139\ufe0f Info</b>", show_only_kb(show_cb))
        elif show_cb == "pshow_month":
            await safe_edit(q.message, "<b>\U0001f4c5 Monthly Schedule</b>", show_only_kb(show_cb))
        else:
            await safe_edit(q.message, "<b>\u2139\ufe0f</b>", show_only_kb(show_cb))
        return

    # Private show
    if data == "pshow_info":
        uid_val = update.effective_user.id
        cfg = get_cfg(uid_val)
        info_text = (
            hdr("Smart Reminder Bot") + "\n\nSet reminders and get notified on time.\n\n"
            "<b>Smart Input</b>\nJust type naturally:\n"
            "<code>Buy milk tomorrow at 5pm</code>\n"
            "<code>Meeting Monday 10am weekly</code>\n"
            "<code>Call mom in 30 min</code>\n\n"
            "<b>Commands</b>\n/add /list /month /settings /info"
        )
        await safe_edit(q.message, info_text, close_show_kb("pshow_info"))
        return
    if data == "pshow_list":
        await show_list(q.message, uid, ctx)
        return
    if data == "pshow_month":
        utz = get_tz(uid)
        now = datetime.now(utz)
        txt, kb = build_month_view(uid, now.year, now.month, utz)
        await safe_edit(q.message, txt, kb)
        return

    # Group show
    if data.startswith("gshow_start_"):
        mid = data[12:]
        stored = ctx.bot_data.get("gmin_" + mid)
        full = stored.get("full_text", GRP_START) if stored else GRP_START
        await safe_edit(q.message, full, gclose_kb())
        return
    if data.startswith("gshow_list_"):
        parts = data[11:]
        sep = parts.rfind("_")
        gid = parts[:sep] if sep > 0 else str(q.message.chat.id)
        list_text, items = build_grp_list_text(gid)
        if list_text:
            await safe_edit(q.message, list_text, gclose_kb())
        else:
            await safe_edit(q.message, hdr("Group Reminders") + "\nNo active reminders.", gclose_kb())
        return
    if data.startswith("gshow_generic_"):
        await safe_edit(q.message, GRP_START, gclose_kb())
        return

    if data == "add":
        await rm_home(ctx, ud)
        ud.clear()
        ud["step"] = "message"
        sent = await q.message.reply_text(hdr("New Reminder") + "\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
        save_p(ud, sent)
        return
    if data == "list_refresh":
        ud.clear()
        await show_list(q.message, uid, ctx)
        return

    # Undo cancelled
    if data.startswith("undo_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 6, "active")
        sheet.update_cell(row, 7, 0)
        await safe_edit(q.message, hdr("Restored \u2713") + "\n" + detail(msg, ds, ts, rs), home_kb())
        save_home(ud, q.message)
        return

    # Repeat change (private)
    if data.startswith("chrep_"):
        row = int(data[6:])
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message, hdr("Saved \u2713") + "\n" + detail(msg, ds, ts) + "\n\nRepeat?", rep_picker_kb("chrepv_" + str(row)))
        return
    if data.startswith("chrepv_"):
        parts = data.split("_")
        row_s = parts[1]
        rep = parts[2]
        row = int(row_s)
        if rep == "custom":
            ud["custom_days_for"] = ("chrep", row)
            ud["custom_days_selected"] = []
            r, msg, ds, ts, rs = row_detail(row)
            await safe_edit(q.message, detail(msg, ds, ts) + "\n\nSelect days:", custom_days_kb([], "chrep_" + str(row)))
            return
        r, msg, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 5, rep)
        await safe_edit(q.message, hdr("Updated \u2713") + "\n" + detail(msg, ds, ts, fmt_rep(rep)), home_kb())
        save_home(ud, q.message)
        return

    # Repeat change (group)
    if data.startswith("gchrep_"):
        tid = data[7:]
        row, r = find_by_tid(tid)
        if not r:
            return
        msg, ds, ts, rs = get_detail(r)
        await safe_edit(q.message, detail(msg, ds, ts) + "\n\nRepeat?", rep_picker_kb("gchrepv_" + tid))
        return
    if data.startswith("gchrepv_"):
        parts = data.split("_")
        tid = parts[1]
        rep = parts[2]
        if rep == "custom":
            ud["custom_days_for"] = ("gchrep", tid)
            ud["custom_days_selected"] = []
            row, r = find_by_tid(tid)
            if not r:
                return
            msg, ds, ts, _ = get_detail(r)
            await safe_edit(q.message, detail(msg, ds, ts) + "\n\nSelect days:", custom_days_kb([], "gchrep_" + tid))
            return
        row, r = find_by_tid(tid)
        if not r:
            return
        sheet.update_cell(row, 5, rep)
        msg, ds, ts, _ = get_detail(r)
        sub_info = gsub_text(tid)
        await safe_edit(q.message, hdr("Group Reminder") + "\n" + detail(msg, ds, ts, fmt_rep(rep)) + "\n\n" + sub_info, gjoin_kb(tid))
        ctx.bot_data["gm_" + tid] = {"c": str(q.message.chat.id), "m": q.message.message_id}
        return

    # Custom days toggle
    if data.startswith("cday_"):
        rest = data[5:]
        last_us = rest.rfind("_")
        if last_us < 0:
            return
        prefix = rest[:last_us]
        day_key = rest[last_us + 1:]
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
                await safe_edit(q.message, detail(msg, ds, ts) + "\n\nSelect days:", custom_days_kb(selected, "chrep_" + str(key)))
            elif kind == "gchrep":
                row, r = find_by_tid(key)
                if r:
                    msg, ds, ts, _ = get_detail(r)
                    await safe_edit(q.message, detail(msg, ds, ts) + "\n\nSelect days:", custom_days_kb(selected, "gchrep_" + key))
        return

    # Custom days save
    if data.startswith("cdaysave_"):
        selected = ud.get("custom_days_selected", [])
        if not selected:
            return
        rep_value = "custom:" + ",".join(selected)
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
            await safe_edit(q.message, hdr("Updated \u2713") + "\n" + detail(msg, ds, ts, fmt_rep(rep_value)), home_kb())
            save_home(ud, q.message)
        elif kind == "gchrep":
            row, r = find_by_tid(key)
            if r:
                sheet.update_cell(row, 5, rep_value)
                msg, ds, ts, _ = get_detail(r)
                sub_info = gsub_text(key)
                await safe_edit(q.message, hdr("Group Reminder") + "\n" + detail(msg, ds, ts, fmt_rep(rep_value)) + "\n\n" + sub_info, gjoin_kb(key))
                ctx.bot_data["gm_" + key] = {"c": str(q.message.chat.id), "m": q.message.message_id}
        return

    # Custom days back
    if data.startswith("cdayback_"):
        info = ud.get("custom_days_for")
        ud.pop("custom_days_for", None)
        ud.pop("custom_days_selected", None)
        if info:
            kind, key = info
            if kind == "chrep":
                row = int(key)
                r, msg, ds, ts, rs = row_detail(row)
                await safe_edit(q.message, hdr("Saved \u2713") + "\n" + detail(msg, ds, ts) + "\n\nRepeat?", rep_picker_kb("chrepv_" + str(row)))
            elif kind == "gchrep":
                row, r = find_by_tid(key)
                if r:
                    msg, ds, ts, _ = get_detail(r)
                    await safe_edit(q.message, detail(msg, ds, ts) + "\n\nRepeat?", rep_picker_kb("gchrepv_" + key))
        return

    # Weekly report detail
    if data.startswith("wrdetail_"):
        mid = data[9:]
        wr = ctx.bot_data.get("wr_" + mid)
        if not wr:
            await q.answer("Report expired", show_alert=True)
            return
        uid_s = wr["uid"]
        ws = wr["ws"]
        we = wr["we"]
        try:
            rem_rows = sheet.get_all_values()
        except Exception:
            rem_rows = []
        done_list = []
        missed_list = []
        for v in rem_rows[1:]:
            if len(v) < 6 or str(v[0]) != uid_s:
                continue
            if len(v) > 7 and str(v[7]).strip():
                continue
            ds = norm_date(str(v[2]).strip())
            st = str(v[5]).strip().lower()
            if ws <= ds <= we:
                m = str(v[1]).strip()
                short = m[:30] + "\u2026" if len(m) > 30 else m
                ts_val = norm_time(str(v[3]).strip())
                if st == "done":
                    done_list.append("  \u2705 " + fmt_time(ts_val) + " \u00b7 " + short)
                elif st == "missed":
                    missed_list.append("  \u2717 " + fmt_time(ts_val) + " \u00b7 " + short)
        lines = [hdr("Weekly Detail")]
        if done_list:
            lines.append("\n<b>Completed</b>")
            lines.extend(done_list)
        if missed_list:
            lines.append("\n<b>Missed</b>")
            lines.extend(missed_list)
        if not done_list and not missed_list:
            lines.append("\nNo data available.")
        btns = [[InlineKeyboardButton("\u00ab Back", callback_data="wrback_" + mid)]]
        await safe_edit(q.message, "\n".join(lines), InlineKeyboardMarkup(btns))
        return

    # Weekly report back
    if data.startswith("wrback_"):
        mid = data[7:]
        wr = ctx.bot_data.get("wr_" + mid)
        if not wr:
            return
        btns = [[InlineKeyboardButton("\U0001f4cb Detail", callback_data="wrdetail_" + mid), InlineKeyboardButton("\uff0b New", callback_data="add")]]
        await safe_edit(q.message, wr["text"], InlineKeyboardMarkup(btns))
        return

    # Month view
    if data.startswith("mw_"):
        parts = data[3:].split("_")
        yr, mo, wi = int(parts[0]), int(parts[1]), int(parts[2])
        utz = get_tz(uid)
        txt, kb = build_week_view(uid, yr, mo, wi, utz)
        await safe_edit(q.message, txt, kb)
        return
    if data.startswith("mn_"):
        parts = data[3:].split("_")
        yr, mo = int(parts[0]), int(parts[1])
        utz = get_tz(uid)
        txt, kb = build_month_view(uid, yr, mo, utz)
        await safe_edit(q.message, txt, kb)
        return

    # Group callbacks
    if data.startswith(("gjoin_", "gskip_", "gdone_", "gsnzp_", "gsnzb_", "gsnz_")):
        await _btn_group(q, ctx, uid, data)
        return
    if data.startswith(("cal_", "day_")):
        await _btn_cal(q, ctx, ud, uid, data)
        return
    if data.startswith(("view_", "snzp_", "snzb_", "snz_", "done_", "crem_")):
        await _btn_rem(q, ctx, ud, uid, data)
        return
    if data.startswith(("edit_", "emsg_", "edate_", "etime_")):
        await _btn_edit(q, ud, uid, data)
        return
    if data.startswith(("cfg_", "cfgr_", "cfgg_", "tzr_", "tzs_", "gunsub_")):
        await _btn_cfg(q, ctx, ud, uid, data)
        return

async def _btn_group(q, ctx, uid, data):
    uid_s = str(uid)
    user = q.from_user
    uname = get_username(user)

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
        rep = r[4] if len(r) > 4 else "none"
        await safe_edit(q.message, hdr("Group Reminder") + "\n" + detail(msg, ds, ts, rs) + "\n\n" + gsub_text(tid), gjoin_kb(tid, rep == "none"))
        await q.answer("Joined \u2713")
    elif data.startswith("gskip_"):
        tid = data[6:]
        row, r = find_by_tid(tid)
        if not r or str(r[5]) != "active":
            await q.answer("Already fired.", show_alert=True)
            return
        set_gsub(str(q.message.chat.id), uid_s, user.first_name or "User", uname, True)
        ms = get_tmembers(tid)
        if any(str(u) == uid_s for u, _, _ in ms):
            set_tstatus(tid, uid_s, "skipped")
        else:
            add_tmember(tid, uid_s, user.first_name or "User", "skipped")
        msg, ds, ts, rs = get_detail(r)
        rep = r[4] if len(r) > 4 else "none"
        await safe_edit(q.message, hdr("Group Reminder") + "\n" + detail(msg, ds, ts, rs) + "\n\n" + gsub_text(tid), gjoin_kb(tid, rep == "none"))
        await q.answer("Skipped")
    elif data.startswith("gdone_"):
        tid = data[6:]
        ms = get_tmembers(tid)
        st = next((s for u, _, s in ms if str(u) == uid_s), None)
        if st and st != "pending":
            await safe_edit(q.message, "<i>Already handled</i>")
            return
        set_tstatus(tid, uid_s, "done")
        await rm_gpm(ctx, tid, uid_s)
        row, r = find_by_tid(tid)
        msg = str(r[1]).strip() if r else ""
        await safe_edit(q.message, msg + "\n\n<b>Done</b> \u2713")
        await update_gstatus(ctx, tid, msg)
        if row and r:
            await check_grp_resolved(ctx, tid, row, r)
            await update_gstatus(ctx, tid, msg)
    elif data.startswith("gsnzp_"):
        tid = data[6:]
        st = next((s for u, _, s in get_tmembers(tid) if str(u) == uid_s), None)
        if st and st != "pending":
            await safe_edit(q.message, "<i>Already handled</i>")
            return
        row, r = find_by_tid(tid)
        msg = str(r[1]).strip() if r else ""
        await safe_edit(q.message, msg + "\n\nSnooze for:", snz_kb(tid, "gsnz"))
    elif data.startswith("gsnzb_"):
        tid = data[6:]
        row, r = find_by_tid(tid)
        msg = str(r[1]).strip() if r else ""
        await safe_edit(q.message, msg + "\n\n<b>\u23f0 Group Reminder</b>", gact_kb(tid))
    elif data.startswith("gsnz_"):
        rest = data[5:]
        last_us = rest.rfind("_")
        if last_us < 0:
            return
        tid = rest[:last_us]
        mins = int(rest[last_us + 1:])
        st = next((s for u, _, s in get_tmembers(tid) if str(u) == uid_s), None)
        if st and st != "pending":
            await safe_edit(q.message, "<i>Already handled</i>")
            return
        set_tstatus(tid, uid_s, "snoozed")
        await rm_gpm(ctx, tid, uid_s)
        row, r = find_by_tid(tid)
        msg = str(r[1]).strip() if r else ""
        nt = datetime.now(get_tz(uid)) + timedelta(minutes=mins)
        await safe_edit(q.message, msg + "\n\n<b>Snoozed " + fmt_snz(mins) + "</b> \u2192 " + fmt_time(nt.strftime('%H:%M')))
        await update_gstatus(ctx, tid, msg)
        ctx.job_queue.run_once(grp_snooze_cb, mins * 60, data={"tid": tid, "uid": uid, "uid_s": uid_s}, name="gsnz-" + tid + "-" + uid_s)

async def _btn_cal(q, ctx, ud, uid, data):
    utz = get_tz(uid)
    step = ud.get("step", "")

    if data.startswith("cal_"):
        parts = data[4:].split("_")
        yr, mo = int(parts[0]), int(parts[1])
        if step == "edit_date":
            row = ud["editing_row"]
            r, msg, ds, ts, rs = row_detail(row)
            await safe_edit(q.message, hdr("Edit Reminder") + "\n" + msg + "\nCurrent: <i>" + fmt_date(ds) + " \u00b7 " + fmt_time(ts) + "</i>\n\nPick new date:",
                cal_kb(yr, mo, "edit_" + str(row), "\u00ab Back", tz=utz))
        elif step == "g_date":
            msg = ud.get("message", "")
            ts = ud.get("time")
            extra = "\n" + fmt_time(ts) if ts else ""
            await safe_edit(q.message, hdr("Group Reminder") + "\n" + msg + extra + "\n\nPick a date:",
                cal_kb(yr, mo, "gcancel", "\u2715 Cancel", tz=utz))
        else:
            msg = ud.get("message", "")
            ts = ud.get("time")
            extra = "\n" + fmt_time(ts) if ts else ""
            await safe_edit(q.message, hdr("New Reminder") + "\n" + msg + extra + "\n\nPick a date:", cal_kb(yr, mo, tz=utz))

    elif data.startswith("day_"):
        ds = data[4:]
        if step == "edit_date":
            row = ud["editing_row"]
            r, msg, old_d, ts, rs = row_detail(row)
            if is_past(ds, ts, utz):
                now = datetime.now(utz)
                await safe_edit(q.message, hdr("Edit Reminder") + "\n" + msg + "\n\n" + past_msg(ts) + "\nPick a future date or change time first.",
                    cal_kb(now.year, now.month, "edit_" + str(row), "\u00ab Back", tz=utz))
            else:
                sheet.update_cell(row, 3, ds)
                ud.clear()
                await safe_edit(q.message, hdr("Updated \u2713") + "\n" + msg + "\nDate: " + fmt_date(old_d) + " \u2192 <b>" + fmt_date(ds) + "</b>\nTime: " + fmt_time(ts) + " \u00b7 " + rs, home_kb())
                save_home(ud, q.message)
        elif step == "g_date":
            ud["date"] = ds
            msg = ud.get("message", "")
            ts = ud.get("time")
            if ts:
                if is_past(ds, ts, utz):
                    now = datetime.now(utz)
                    await safe_edit(q.message, hdr("Group Reminder") + "\n" + msg + "\n" + fmt_time(ts) + "\n\n" + past_msg(ts) + "\nPick a future date:",
                        cal_kb(now.year, now.month, "gcancel", "\u2715 Cancel", tz=utz))
                else:
                    rep = ud.get("repeat", "none")
                    await finish_group_remind(q.message, ctx, uid, ud, rep, edit_msg=True)
            else:
                ud["step"] = "g_time"
                try:
                    await q.message.delete()
                except Exception:
                    pass
                sent = await ctx.bot.send_message(
                    chat_id=q.message.chat.id,
                    text=hdr("Group Reminder") + "\n" + msg + "\n" + fmt_date(ds) + "\n\nEnter time:\n<i>\u21a9\ufe0f Reply to this message</i>\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
                    reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 9pm, 9:30 PM"),
                    parse_mode="HTML")
                save_p(ud, sent)
        else:
            ud["date"] = ds
            msg = ud.get("message", "")
            ts = ud.get("time")
            if ts:
                if is_past(ds, ts, utz):
                    now = datetime.now(utz)
                    await safe_edit(q.message, hdr("New Reminder") + "\n" + msg + "\n" + fmt_time(ts) + "\n\n" + past_msg(ts) + "\nPick a future date:", cal_kb(now.year, now.month, tz=utz))
                else:
                    await save_reminder(q.message, uid, ud, msg, ds, ts, edit_msg=True)
            else:
                ud["step"] = "time"
                await safe_edit(q.message, hdr("New Reminder") + "\n" + msg + "\n" + fmt_date(ds) + "\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", cancel_kb())
                save_p(ud, q.message)

async def _btn_rem(q, ctx, ud, uid, data):
    if data.startswith("view_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        st = r[5] if len(r) > 5 else "active"
        if st != "missed":
            btns = [[InlineKeyboardButton("\u270e Edit", callback_data="edit_" + str(row)), InlineKeyboardButton("\u2715 Cancel", callback_data="crem_" + str(row))]]
        else:
            btns = [[InlineKeyboardButton("\u2715 Remove", callback_data="crem_" + str(row))]]
        btns.append([InlineKeyboardButton("\u00ab Back", callback_data="list_refresh")])
        await safe_edit(q.message, hdr("Reminder") + "\n" + msg + "\n\n" + fmt_date(ds) + " \u00b7 " + fmt_time(ts) + "\n" + rs + " \u00b7 " + ST_IC.get(st, '?') + " <i>" + ST_LB.get(st, st) + "</i>", InlineKeyboardMarkup(btns))
    elif data.startswith("snzp_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        if len(r) > 5 and r[5] != "pending":
            await safe_edit(q.message, detail(msg, ds, ts, rs) + "\n\n<i>Already handled</i>")
            return
        await safe_edit(q.message, detail(msg, ds, ts, rs) + "\n\nSnooze for:", snz_kb(row))
    elif data.startswith("snzb_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        if len(r) > 5 and r[5] != "pending":
            await safe_edit(q.message, detail(msg, ds, ts, rs) + "\n\n<i>Already handled</i>")
            return
        await safe_edit(q.message, msg + "\n\n<b>\u23f0 Reminder</b>", act_kb(row))
    elif data.startswith("snz_"):
        parts = data[4:].split("_")
        row = int(parts[0])
        mins = int(parts[1])
        r, msg, ds, ts, rs = row_detail(row)
        if len(r) > 5 and r[5] != "pending":
            await safe_edit(q.message, detail(msg, ds, ts, rs) + "\n\n<i>Already handled</i>")
            return
        kill_jobs(ctx.job_queue, row)
        await rm_btns(ctx, row)
        utz = get_tz(uid)
        nt = datetime.now(utz) + timedelta(minutes=mins)
        rep = r[4] if len(r) > 4 else "none"
        if rep and rep != "none":
            sheet.update_cell(row, 6, "snoozed")
            sheet.update_cell(row, 7, 0)
            ctx.job_queue.run_once(snooze_cb, mins * 60, data={"row": row, "chat": uid}, name="snooze-" + str(row))
        else:
            sheet.update_cell(row, 3, nt.strftime("%Y-%m-%d"))
            sheet.update_cell(row, 4, nt.strftime("%H:%M"))
            sheet.update_cell(row, 6, "active")
            sheet.update_cell(row, 7, 0)
        ctx.bot_data.pop("r_" + str(row), None)
        await safe_edit(q.message, detail(msg, ds, ts, rs) + "\n\n<b>Snoozed " + fmt_snz(mins) + "</b> \u2192 " + fmt_time(nt.strftime('%H:%M')))
    elif data.startswith("done_"):
        row = int(data[5:])
        r, msg, ds, ts, rs = row_detail(row)
        if len(r) > 5 and r[5] != "pending":
            await safe_edit(q.message, detail(msg, ds, ts, rs) + "\n\n<i>Already handled</i>")
            return
        kill_jobs(ctx.job_queue, row)
        await rm_btns(ctx, row)
        if not advance_rep(row, r):
            sheet.update_cell(row, 6, "done")
            sheet.update_cell(row, 7, 0)
        ctx.bot_data.pop("r_" + str(row), None)
        await safe_edit(q.message, detail(msg, ds, ts, rs) + "\n\n<b>Done</b> \u2713")
    elif data.startswith("crem_"):
        row = int(data[5:])
        kill_jobs(ctx.job_queue, row)
        r, msg, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 6, "cancelled")
        sheet.update_cell(row, 7, 0)
        await rm_btns(ctx, row)
        ctx.bot_data.pop("r_" + str(row), None)
        undo_kb = InlineKeyboardMarkup([[InlineKeyboardButton("\u21a9 Undo", callback_data="undo_" + str(row)), InlineKeyboardButton("\uff0b New", callback_data="add")]])
        await safe_edit(q.message, detail(msg, ds, ts) + "\n\n<b>Cancelled</b> \u2715", undo_kb)

async def _btn_edit(q, ud, uid, data):
    if data.startswith("emsg_"):
        row = int(data[5:])
        ud["editing_row"] = row
        ud["step"] = "edit_message"
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message, hdr("Edit Reminder") + "\nCurrent: <i>" + msg + "</i>\n" + fmt_date(ds) + " \u00b7 " + fmt_time(ts) + " \u00b7 " + rs + "\n\nEnter new message:",
            InlineKeyboardMarkup([[InlineKeyboardButton("\u00ab Back", callback_data="edit_" + str(row))]]))
        save_p(ud, q.message)
    elif data.startswith("edate_"):
        row = int(data[6:])
        ud["editing_row"] = row
        ud["step"] = "edit_date"
        r, msg, ds, ts, rs = row_detail(row)
        utz = get_tz(uid)
        now = datetime.now(utz)
        await safe_edit(q.message, hdr("Edit Reminder") + "\n" + msg + "\nCurrent: <i>" + fmt_date(ds) + " \u00b7 " + fmt_time(ts) + "</i>\n\nPick new date:",
            cal_kb(now.year, now.month, "edit_" + str(row), "\u00ab Back", tz=utz))
    elif data.startswith("etime_"):
        row = int(data[6:])
        ud["editing_row"] = row
        ud["step"] = "edit_time"
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message, hdr("Edit Reminder") + "\n" + msg + "\nCurrent: <i>" + fmt_date(ds) + " \u00b7 " + fmt_time(ts) + "</i>\n\nEnter new time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("\u00ab Back", callback_data="edit_" + str(row))]]))
        save_p(ud, q.message)
    elif data.startswith("edit_"):
        row = int(data[5:])
        ud.clear()
        r, msg, ds, ts, rs = row_detail(row)
        await safe_edit(q.message, hdr("Edit Reminder") + "\n" + detail(msg, ds, ts, rs) + "\n\nWhat to change?",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Message", callback_data="emsg_" + str(row)),
                 InlineKeyboardButton("Date", callback_data="edate_" + str(row)),
                 InlineKeyboardButton("Time", callback_data="etime_" + str(row))],
                [InlineKeyboardButton("\u00ab Back", callback_data="view_" + str(row))]
            ]))

async def _btn_cfg(q, ctx, ud, uid, data):
    if data == "cfg_digest_toggle":
        cfg = get_cfg(uid)
        save_cfg(uid, "digest_on", str(not cfg["digest_on"]).lower())
        await show_settings(q.message, uid)
    elif data == "cfg_digest_time":
        ud.clear()
        ud["step"] = "set_digest_time"
        cfg = get_cfg(uid)
        await safe_edit(q.message, hdr("Settings") + "\nDigest time: <b>" + fmt_time(cfg['digest_time']) + "</b>\n\nEnter new time:\n<i>e.g. 7am, 8:30 AM</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("\u00ab Back", callback_data="cfg_back")]]))
        save_p(ud, q.message)
    elif data == "cfg_retries":
        cfg = get_cfg(uid)
        await safe_edit(q.message, hdr("Settings") + "\nRetries: <b>" + str(cfg['max_retries']) + "\u00d7</b>\n\nHow many retries?",
            cfg_picker_kb([1, 2, 3, 5, 7, 10], str, cfg["max_retries"], "cfgr_"))
    elif data.startswith("cfgr_"):
        save_cfg(uid, "max_retries", int(data[5:]))
        await show_settings(q.message, uid)
    elif data == "cfg_gap":
        cfg = get_cfg(uid)
        await safe_edit(q.message, hdr("Settings") + "\nGap: <b>" + str(cfg['retry_gap']) + " min</b>\n\nTime between retries?",
            cfg_picker_kb([5, 10, 15, 20, 30, 60], lambda v: str(v) + "m", cfg["retry_gap"], "cfgg_"))
    elif data.startswith("cfgg_"):
        save_cfg(uid, "retry_gap", int(data[5:]))
        await show_settings(q.message, uid)
    elif data == "cfg_tz":
        cfg = get_cfg(uid)
        btns = []
        row = []
        for region in TZ_REGIONS:
            ic = TZ_ICONS.get(region, "\U0001f310")
            row.append(InlineKeyboardButton(ic + " " + region, callback_data="tzr_" + region))
            if len(row) == 2:
                btns.append(row)
                row = []
        if row:
            btns.append(row)
        btns.append([InlineKeyboardButton("\u00ab Back", callback_data="cfg_back")])
        await safe_edit(q.message, hdr("Timezone") + "\n\nCurrent: <b>" + tz_short(cfg.get('timezone', DEF_TZ)) + "</b>\n\nPick a region:", InlineKeyboardMarkup(btns))
    elif data.startswith("tzr_"):
        cfg = get_cfg(uid)
        region = data[4:]
        btns = []
        row = []
        for idx, (tz, country, offset, reg) in enumerate(TZ_DATA):
            if reg != region:
                continue
            lbl = "[" + country + "]" if tz == cfg.get("timezone", DEF_TZ) else country
            row.append(InlineKeyboardButton(lbl + " " + offset, callback_data="tzs_" + str(idx)))
            if len(row) == 2:
                btns.append(row)
                row = []
        if row:
            btns.append(row)
        ic = TZ_ICONS.get(region, "\U0001f310")
        btns.append([InlineKeyboardButton("\u00ab Regions", callback_data="cfg_tz")])
        await safe_edit(q.message, hdr("Timezone") + "\n\n" + ic + " <b>" + region + "</b>\n\nPick:", InlineKeyboardMarkup(btns))
    elif data.startswith("tzs_"):
        idx = int(data[4:])
        if 0 <= idx < len(TZ_DATA):
            save_cfg(uid, "timezone", TZ_DATA[idx][0])
            await show_settings(q.message, uid)
    elif data == "cfg_groups":
        grps = get_user_groups(uid)
        if not grps:
            await safe_edit(q.message, hdr("Groups") + "\n\nNo group subscriptions.",
                InlineKeyboardMarkup([[InlineKeyboardButton("\u00ab Back", callback_data="cfg_back")]]))
            return
        btns = []
        for gid in grps:
            try:
                chat = await ctx.bot.get_chat(int(gid))
                name = chat.title or "Group " + gid
            except Exception:
                name = "Group " + gid
            btns.append([InlineKeyboardButton("\u2715 " + name, callback_data="gunsub_" + gid)])
        btns.append([InlineKeyboardButton("\u00ab Back", callback_data="cfg_back")])
        await safe_edit(q.message, hdr("Group Subscriptions") + "\n\nTap to unsubscribe:", InlineKeyboardMarkup(btns))
    elif data.startswith("gunsub_"):
        gid_s = data[7:]
        uid_s = str(uid)
        try:
            rows = grp_sheet.get_all_values()
            for i, r in enumerate(rows[1:], 2):
                if str(r[0]) == gid_s and str(r[1]) == uid_s:
                    grp_sheet.update_cell(i, 5, "false")
                    break
        except Exception:
            pass
        grps = get_user_groups(uid)
        if not grps:
            await safe_edit(q.message, hdr("Groups") + "\n\nUnsubscribed \u2713\nNo more group subscriptions.",
                InlineKeyboardMarkup([[InlineKeyboardButton("\u00ab Back", callback_data="cfg_back")]]))
        else:
            btns = []
            for gid in grps:
                try:
                    chat = await ctx.bot.get_chat(int(gid))
                    name = chat.title or "Group " + gid
                except Exception:
                    name = "Group " + gid
                btns.append([InlineKeyboardButton("\u2715 " + name, callback_data="gunsub_" + gid)])
            btns.append([InlineKeyboardButton("\u00ab Back", callback_data="cfg_back")])
            await safe_edit(q.message, hdr("Group Subscriptions") + "\n\nUnsubscribed \u2713\n\nTap to unsubscribe:", InlineKeyboardMarkup(btns))
    elif data == "cfg_back":
        ud.clear()
        await show_settings(q.message, uid)

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
    uid = update.effective_user.id
    utz = get_tz(uid)
    result = parse_nl_partial(text, tz=utz)
    if not result or not result['message']:
        return

    msg = result['message']
    ts = result.get('time')
    ds = result.get('date')
    rep = result.get('repeat')

    has_prefix = bool(re.search(r'(?:remind|reminder|remember|don.?t\s+forget|set\s+reminder)', text, re.I))
    if not ts and not ds and not has_prefix:
        return

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
    ud = ctx.user_data
    uid = update.effective_user.id
    utz = get_tz(uid)

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
            ud["message"] = msg
            ud["step"] = "date"
            now = datetime.now(utz)
            sent = await update.message.reply_text(
                hdr("New Reminder") + "\n" + msg + "\n\nPick a date:",
                reply_markup=cal_kb(now.year, now.month, tz=utz), parse_mode="HTML")
            save_p(ud, sent)

    elif step == "time":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text("Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML")
            return
        ds = ud.get("date", "")
        if is_past(ds, parsed, utz):
            await update.message.reply_text(past_msg(parsed) + "\nEnter a future time:", parse_mode="HTML")
            return
        await del_prompt(ctx, ud)
        ud["time"] = parsed
        await save_reminder(update.message, uid, ud, ud.get("message", ""), ds, parsed)

    elif step == "edit_message":
        row = ud.get("editing_row")
        if not row:
            return
        await rm_prompt(ctx, ud)
        r, old, ds, ts, rs = row_detail(row)
        sheet.update_cell(row, 2, text)
        ud.clear()
        sent = await update.message.reply_text(
            hdr("Updated \u2713") + "\nMessage: " + old + " \u2192 <b>" + text + "</b>\n" + fmt_date(ds) + " \u00b7 " + fmt_time(ts) + " \u00b7 " + rs,
            reply_markup=home_kb(), parse_mode="HTML")
        save_home(ud, sent)

    elif step == "edit_time":
        row = ud.get("editing_row")
        if not row:
            return
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text("Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML")
            return
        r, msg, ds, old_t, rs = row_detail(row)
        if is_past(ds, parsed, utz):
            await update.message.reply_text(past_msg(parsed) + "\nEnter a future time:", parse_mode="HTML")
            return
        await del_prompt(ctx, ud)
        sheet.update_cell(row, 4, parsed)
        ud.clear()
        sent = await update.message.reply_text(
            hdr("Updated \u2713") + "\n" + msg + "\nTime: " + fmt_time(old_t) + " \u2192 <b>" + fmt_time(parsed) + "</b> \u00b7 " + fmt_date(ds) + " \u00b7 " + rs,
            reply_markup=home_kb(), parse_mode="HTML")
        save_home(ud, sent)

    elif step == "set_digest_time":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text("Invalid time.\n<i>e.g. 7am, 8:30 AM</i>", parse_mode="HTML")
            return
        await del_prompt(ctx, ud)
        save_cfg(uid, "digest_time", parsed)
        ud.clear()
        await update.message.reply_text(
            hdr("Settings") + "\nDigest time \u2192 <b>" + fmt_time(parsed) + "</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u00ab Settings", callback_data="cfg_back")]]),
            parse_mode="HTML")

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
            ud["message"] = msg
            ud["step"] = "g_date"
            now = datetime.now(utz)
            sent = await update.message.reply_text(
                hdr("Group Reminder") + "\n" + msg + "\n\nPick a date:",
                reply_markup=cal_kb(now.year, now.month, "gcancel", "\u2715 Cancel", tz=utz), parse_mode="HTML")
            save_p(ud, sent)

    elif step == "g_time":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text("Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML")
            return
        ds = ud.get("date", "")
        if is_past(ds, parsed, utz):
            await update.message.reply_text(past_msg(parsed) + "\nEnter a future time:", parse_mode="HTML")
            return
        await del_prompt(ctx, ud)
        ud["time"] = parsed
        rep = ud.get("repeat", "none")
        await finish_group_remind(update.message, ctx, uid, ud, rep)

# ============= FIRE & RETRY ==============
async def send_and_track(ctx, chat_id, text, kb, track_key, track_cid):
    try:
        sent = await ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")
        ctx.bot_data[track_key] = {"c": track_cid, "m": sent.message_id}
        return True
    except Exception as e:
        logger.error("Send %s: %s", chat_id, e)
        return False

async def snooze_cb(ctx: ContextTypes.DEFAULT_TYPE):
    row = ctx.job.data["row"]
    chat = ctx.job.data["chat"]
    try:
        r = sheet.row_values(row)
    except Exception as e:
        logger.error("snooze row %s: %s", row, e)
        return
    if not r or len(r) <= 5 or r[5] != "snoozed":
        return
    await rm_btns(ctx, row)
    msg = str(r[1]).strip()
    if await send_and_track(ctx, chat, msg + "\n\n<b>\u23f0 Reminder</b>", act_kb(row), "r_" + str(row), chat):
        sheet.update_cell(row, 6, "pending")
        sheet.update_cell(row, 7, 0)
        uid_val = int(r[0]) if r[0].isdigit() else r[0]
        cfg = get_cfg(uid_val)
        ctx.job_queue.run_once(auto_retry, cfg["retry_gap"] * 60, data={"row": row, "chat": chat}, name="retry-" + str(row))

async def auto_retry(ctx: ContextTypes.DEFAULT_TYPE):
    row = ctx.job.data["row"]
    chat = ctx.job.data["chat"]
    try:
        r = sheet.row_values(row)
    except Exception as e:
        logger.error("retry %s: %s", row, e)
        return
    if not r or len(r) <= 5 or r[5] != "pending":
        return
    uid_val = int(r[0]) if r[0].isdigit() else r[0]
    cfg = get_cfg(uid_val)
    max_r = cfg["max_retries"]
    gap = cfg["retry_gap"]
    count = int(r[6]) if len(r) > 6 and r[6].isdigit() else 0
    if count >= max_r:
        if not advance_rep(row, r):
            sheet.update_cell(row, 6, "missed")
            sheet.update_cell(row, 7, 0)
        return
    await rm_btns(ctx, row)
    nc = count + 1
    msg = str(r[1]).strip()
    await send_and_track(ctx, chat, msg + "\n\n<b>Reminder</b> (" + str(nc) + "/" + str(max_r) + ")", act_kb(row), "r_" + str(row), chat)
    sheet.update_cell(row, 7, nc)
    if nc >= max_r:
        if not advance_rep(row, r):
            sheet.update_cell(row, 6, "missed")
            sheet.update_cell(row, 7, 0)
    else:
        ctx.job_queue.run_once(auto_retry, gap * 60, data={"row": row, "chat": chat}, name="retry-" + str(row))

async def grp_snooze_cb(ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.job.data["tid"]
    uid = ctx.job.data["uid"]
    uid_s = ctx.job.data["uid_s"]
    st = next((s for u, _, s in get_tmembers(tid) if str(u) == uid_s), None)
    if st != "snoozed":
        return
    set_tstatus(tid, uid_s, "pending")
    row, r = find_by_tid(tid)
    if not r:
        return
    msg = str(r[1]).strip()
    await rm_gpm(ctx, tid, uid_s)
    if not await send_and_track(ctx, uid, msg + "\n\n<b>\u23f0 Group Reminder</b>", gact_kb(tid), "gpm_" + tid + "_" + uid_s, uid):
        set_tstatus(tid, uid_s, "missed")
    await update_gstatus(ctx, tid, msg)

async def fire_group(ctx, row, v, uid, msg, gid, tid, cfg):
    active = [(u, n) for u, n, s in get_tmembers(tid) if s == "waiting"]
    if not active:
        sheet.update_cell(row, 6, "done")
        return
    for u, n in active:
        set_tstatus(tid, u, "pending")
    setup = ctx.bot_data.pop("gm_" + tid, None)
    if setup:
        try:
            await ctx.bot.edit_message_reply_markup(chat_id=int(setup["c"]), message_id=setup["m"], reply_markup=None)
        except Exception:
            pass
    try:
        status = await ctx.bot.send_message(chat_id=int(gid), text=gstatus_text(tid, msg), parse_mode="HTML")
        ctx.bot_data["gs_" + tid] = {"c": int(gid), "m": status.message_id}
    except Exception as e:
        logger.error("[FIRE] Group %s: %s", gid, e)
    for u, n in active:
        if not await send_and_track(ctx, int(u), msg + "\n\n<b>\u23f0 Group Reminder</b>", gact_kb(tid), "gpm_" + tid + "_" + u, int(u)):
            set_tstatus(tid, u, "missed")
    sheet.update_cell(row, 6, "pending")
    sheet.update_cell(row, 7, 0)
    gap = cfg.get("retry_gap", DEF_RETRY_GAP)
    ctx.job_queue.run_once(grp_retry, gap * 60, data={"tid": tid, "row": row, "gid": gid}, name="gretry-" + tid)

async def grp_retry(ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.job.data["tid"]
    row = ctx.job.data["row"]
    gid = ctx.job.data["gid"]
    try:
        r = sheet.row_values(row)
    except Exception as e:
        logger.error("[GRETRY] %s: %s", row, e)
        return
    if not r or len(r) <= 5 or r[5] != "pending":
        return
    creator = int(r[0]) if r[0].isdigit() else r[0]
    cfg = get_cfg(creator)
    max_r = cfg["max_retries"]
    gap = cfg["retry_gap"]
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
        if not await send_and_track(ctx, int(u), msg + "\n\n<b>Group Reminder</b> (" + str(nc) + "/" + str(max_r) + ")", gact_kb(tid), "gpm_" + tid + "_" + u, int(u)):
            set_tstatus(tid, u, "missed")
    sheet.update_cell(row, 7, nc)
    await update_gstatus(ctx, tid, msg)
    if nc >= max_r:
        for u, n in pending:
            set_tstatus(tid, u, "missed")
        await update_gstatus(ctx, tid, msg)
        await check_grp_resolved(ctx, tid, row, r)
    else:
        ctx.job_queue.run_once(grp_retry, gap * 60, data={"tid": tid, "row": row, "gid": gid}, name="gretry-" + tid)

# ============= DAILY DIGEST ==============
async def check_digest(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        cfg_rows = cfg_sheet.get_all_values()
    except Exception:
        try:
            client.login()
            cfg_rows = cfg_sheet.get_all_values()
        except Exception:
            return
    for r in cfg_rows[1:]:
        if len(r) < 3 or str(r[1]).lower() != "true":
            continue
        tz_name = str(r[5]) if len(r) > 5 and r[5] else DEF_TZ
        user_tz = safe_tz(tz_name)
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
            lines = ["\u2600\ufe0f <b>Good morning!</b>\n" + DIV + "\n\nToday \u2014 " + today_str + "\n"]
            for v in items:
                msg_text = str(v[1]).strip()
                short = msg_text[:30] + "\u2026" if len(msg_text) > 30 else msg_text
                lines.append("  " + fmt_time(norm_time(str(v[3]).strip())) + " \u00b7 " + short)
            cnt = len(items)
            lines.append("\n" + str(cnt) + " reminder" + ("s" if cnt != 1 else "") + " today")
        else:
            lines = ["\u2600\ufe0f <b>Good morning!</b>\n" + DIV + "\n\nToday \u2014 " + today_str + "\n", "No reminders today. Enjoy your day!"]
        try:
            await ctx.bot.send_message(chat_id=uid_int, text="\n".join(lines), reply_markup=home_kb(), parse_mode="HTML")
        except Exception as e:
            logger.error("[DIGEST] %s: %s", r[0], e)

# ============= WEEKLY REPORT =============
async def check_weekly_report(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        cfg_rows = cfg_sheet.get_all_values()
    except Exception:
        try:
            client.login()
            cfg_rows = cfg_sheet.get_all_values()
        except Exception:
            return
    for r in cfg_rows[1:]:
        tz_name = str(r[5]) if len(r) > 5 and r[5] else DEF_TZ
        user_tz = safe_tz(tz_name)
        now = datetime.now(user_tz)
        if now.weekday() != 6:
            continue
        if now.strftime("%H:%M") != "09:00":
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
        done_c, missed_c, snoozed_c = 0, 0, 0
        day_done = {}
        day_missed = {}
        for v in rem_rows[1:]:
            if len(v) < 6 or str(v[0]) != uid_s:
                continue
            if len(v) > 7 and str(v[7]).strip():
                continue
            ds = norm_date(str(v[2]).strip())
            st = str(v[5]).strip().lower()
            if week_start <= ds <= week_end:
                if st == "done":
                    done_c += 1
                elif st == "missed":
                    missed_c += 1
                try:
                    wd = datetime.strptime(ds, "%Y-%m-%d").strftime("%A")
                    if st == "done":
                        day_done[wd] = day_done.get(wd, 0) + 1
                    elif st == "missed":
                        day_missed[wd] = day_missed.get(wd, 0) + 1
                except Exception:
                    pass
        total = done_c + missed_c
        if total == 0:
            continue
        pct = round(done_c / total * 100)
        best_day = max(day_done, key=day_done.get) if day_done else "\u2014"
        worst_day = max(day_missed, key=day_missed.get) if day_missed else "\u2014"
        if pct >= 90:
            mot = "Outstanding! \U0001f3c6"
        elif pct >= 70:
            mot = "Keep it up! \U0001f4aa"
        elif pct >= 50:
            mot = "Room to improve \U0001f4c8"
        else:
            mot = "Let's do better next week \U0001f3af"
        ws_d = (now - timedelta(days=7)).strftime("%-d %b")
        we_d = now.strftime("%-d %b")
        txt = (
            "\U0001f4ca <b>Weekly Report</b>\n" + DIV + "\n" + ws_d + " \u2014 " + we_d + "\n\n"
            "\u2705 Completed: " + str(done_c) + "/" + str(total) + " (" + str(pct) + "%)\n"
            "\u274c Missed: " + str(missed_c) + "\n\n"
            "\U0001f4c5 Most Productive: " + best_day + "\n"
            "\U0001f4c9 Most Missed: " + worst_day + "\n\n"
            + mot
        )
        try:
            detail_kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f4cb Detail", callback_data="wrdetail_placeholder"), InlineKeyboardButton("\uff0b New", callback_data="add")]])
            sent = await ctx.bot.send_message(chat_id=uid_int, text=txt, reply_markup=detail_kb, parse_mode="HTML")
            # Update callback with actual message id and store data
            real_cb = "wrdetail_" + str(sent.message_id)
            ctx.bot_data["wr_" + str(sent.message_id)] = {"uid": uid_s, "ws": week_start, "we": week_end, "text": txt}
            real_kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f4cb Detail", callback_data=real_cb), InlineKeyboardButton("\uff0b New", callback_data="add")]])
            await ctx.bot.edit_message_reply_markup(chat_id=uid_int, message_id=sent.message_id, reply_markup=real_kb)
        except Exception as e:
            logger.error("[WEEKLY] %s: %s", uid_s, e)

# ============= SCHEDULER =================
async def check_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        cfg_vals = cfg_sheet.get_all_values()
    except Exception:
        try:
            client.login()
            cfg_vals = cfg_sheet.get_all_values()
        except Exception:
            cfg_vals = []
    tz_map = {}
    cfg_map = {}
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
    except Exception:
        try:
            client.login()
            vals = sheet.get_all_values()
        except Exception as e:
            logger.error("[CRON] %s", e)
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
        uid_val = int(v[0]) if v[0].isdigit() else v[0]
        msg = str(v[1]).strip()
        gid = str(v[7]).strip() if len(v) > 7 else ""
        tid = str(v[8]).strip() if len(v) > 8 else ""
        logger.info("[CRON] FIRE %d: '%s' uid=%s gid=%s", idx, msg[:30], uid_val, gid)
        if gid and tid:
            await fire_group(ctx, idx, v, uid_val, msg, gid, tid, cfg_map.get(uid_s, {}))
        else:
            kill_jobs(ctx.job_queue, idx)
            await rm_btns(ctx, idx)
            if await send_and_track(ctx, uid_val, msg + "\n\n<b>\u23f0 Reminder</b>", act_kb(idx), "r_" + str(idx), uid_val):
                sheet.update_cell(idx, 6, "pending")
                sheet.update_cell(idx, 7, 0)
                gap = cfg_map.get(uid_s, {"retry_gap": DEF_RETRY_GAP})["retry_gap"]
                ctx.job_queue.run_once(auto_retry, gap * 60, data={"row": idx, "chat": uid_val}, name="retry-" + str(idx))

# ============= MAIN ======================
def main():
    app = Application.builder().token(TOKEN).job_queue(JobQueue()).post_init(post_init).build()
    for cmd, fn in [("start", start), ("add", add_cmd), ("list", list_cmd), ("month", month_cmd), ("remind", remind_cmd), ("settings", settings_cmd), ("info", info_cmd)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(check_reminders, interval=60, first=0)
    app.job_queue.run_repeating(check_digest, interval=60, first=10)
    app.job_queue.run_repeating(check_weekly_report, interval=60, first=20)
    print("Smart Reminder Bot Running")
    app.run_polling()

if __name__ == "__main__":
    main()
