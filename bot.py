import logging, os, json, re, math, calendar as cal_mod
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM, ForceReply, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, JobQueue
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ============= CONFIG =============
TOKEN = "8235103406:AAFYJ2SNRW4A4AAEyz8t2h-5BeYk8rnzzwE"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1bHeyDgw9P-3iRLOp_6VpHGKSn9St6yjyqP-35hPg6Rs/edit?pli=1&gid=0#gid=0"

DEF_TZ = "Asia/Kolkata"
DEF_RETRIES = 3
DEF_RETRY_GAP = 10
DEF_DIGEST_TIME = "07:00"

# ============= LOGGING =============
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ============= TIMEZONE DATA =============
TZ_DATA = {
    "Asia": [("India", "Asia/Kolkata"), ("UAE", "Asia/Dubai"), ("Pakistan", "Asia/Karachi"),
             ("Bangladesh", "Asia/Dhaka"), ("Thailand", "Asia/Bangkok"), ("Singapore", "Asia/Singapore"),
             ("China", "Asia/Shanghai"), ("Japan", "Asia/Tokyo"), ("Korea", "Asia/Seoul"),
             ("Indonesia", "Asia/Jakarta"), ("Saudi Arabia", "Asia/Riyadh"), ("Philippines", "Asia/Manila")],
    "Europe": [("UK", "Europe/London"), ("Germany", "Europe/Berlin"), ("France", "Europe/Paris"),
               ("Russia", "Europe/Moscow"), ("Turkey", "Europe/Istanbul")],
    "Americas": [("US East", "America/New_York"), ("US Central", "America/Chicago"),
                 ("US Mountain", "America/Denver"), ("US West", "America/Los_Angeles"),
                 ("Brazil", "America/Sao_Paulo"), ("Mexico", "America/Mexico_City")],
    "Africa": [("Nigeria", "Africa/Lagos"), ("Egypt", "Africa/Cairo"),
               ("Kenya", "Africa/Nairobi"), ("South Africa", "Africa/Johannesburg")],
    "Oceania": [("Australia", "Australia/Sydney"), ("New Zealand", "Pacific/Auckland")]
}
TZ_REGIONS = list(TZ_DATA.keys())
TZ_ICONS = {"Asia": "\U0001f30f", "Europe": "\U0001f30d", "Americas": "\U0001f30e",
            "Africa": "\U0001f30d", "Oceania": "\U0001f30f"}
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ST_IC = {"active": "\u25cb", "pending": "\u25cf", "snoozed": "\u25cf", "done": "\u2705", "missed": "\u2717", "cancelled": "\u2718"}
ST_LB = {"active": "Active", "pending": "Pending", "snoozed": "Snoozed", "done": "Done", "missed": "Missed", "cancelled": "Cancelled"}
GT_IC = {"waiting": "\u23f3", "pending": "\u23f3", "done": "\u2705", "missed": "\u2717", "snoozed": "\u23f3", "skipped": "\u23ed"}
SNOOZE_OPTIONS = [(15, "15m"), (30, "30m"), (45, "45m"), (60, "1h"), (120, "2h"), (180, "3h"), (300, "5h"), (480, "8h"), (720, "12h")]
FILLER = re.compile(r"^(remind me to|remind me|reminder|remember to|don't forget to|dont forget to|set reminder)\s+", re.I)
REP_MAP = {"none": "Once", "daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}

# ============= GOOGLE SHEETS =============
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
    except gspread.WorksheetNotFound:
        ws = workbook.add_worksheet(name, rows=1000, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
    return ws

sheet = get_or_create_sheet("Reminders", ["user_id","message","date","time","repeat","status","retry_count","group_id","task_id"])
cfg_sheet = get_or_create_sheet("Settings", ["user_id","digest_on","digest_time","max_retries","retry_gap","timezone","username","weekly_report"])
grp_sheet = get_or_create_sheet("GroupMembers", ["group_id","user_id","first_name","username","subscribed"])
tm_sheet = get_or_create_sheet("TaskMembers", ["task_id","user_id","first_name","status"])

# ============= HELPERS =============
def hdr(t):
    return f"<b>{t}</b>\n{'━'*20}"

def detail(m, d, t, r=None):
    ds = fmt_date(d) if d else ""
    ts = fmt_time(t) if t else ""
    parts = [p for p in [ds, ts, rep_label(r)] if p]
    return f"{m}\n{' \u00b7 '.join(parts)}" if parts else m

def rep_label(r):
    if not r:
        return ""
    if r.startswith("custom:"):
        days = r.replace("custom:", "").split(",")
        if set(days) == {"mon","tue","wed","thu","fri"}:
            return "Mon\u2013Fri"
        if set(days) == {"sat","sun"}:
            return "Weekends"
        if len(days) == 7:
            return "Daily"
        return ", ".join(d.capitalize() for d in days)
    return REP_MAP.get(r, r.capitalize())

def fmt_date(d):
    if not d:
        return ""
    try:
        dt = datetime.strptime(str(d).strip(), "%Y-%m-%d")
        return dt.strftime("%-d %b")
    except:
        return str(d)

def fmt_time(t):
    if not t:
        return ""
    try:
        parts = str(t).strip().split(":")
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        suffix = "AM" if h < 12 else "PM"
        dh = h if h <= 12 else h - 12
        if dh == 0:
            dh = 12
        return f"{dh}:{m:02d} {suffix}"
    except:
        return str(t)

def safe_tz(name):
    try:
        return pytz.timezone(name)
    except:
        return pytz.timezone(DEF_TZ)

def get_tz(uid):
    cfg = get_cfg(uid)
    return safe_tz(cfg.get("timezone", DEF_TZ))

def tz_short(name):
    for region in TZ_DATA.values():
        for label, tzn in region:
            if tzn == name:
                dt = datetime.now(pytz.timezone(tzn))
                off = dt.strftime("%z")
                oh, om = off[:3], off[3:]
                o = f"+{oh[1:]}:{om}" if off[0] == "+" else f"-{oh[1:]}:{om}"
                if om == "00":
                    o = off[:3]
                return f"{label} ({o})"
    return name

def norm_date(v):
    s = str(v).strip()
    try:
        f = float(s)
        if f > 50000 or f < 1:
            return s
        base = datetime(1899, 12, 30)
        return (base + timedelta(days=int(f))).strftime("%Y-%m-%d")
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except:
            pass
    return s

def norm_time(v):
    s = str(v).strip()
    try:
        f = float(s)
        h = int(f * 24) if f < 1 else int(f)
        m = int((f * 24 - h) * 60) if f < 1 else 0
        return f"{h:02d}:{m:02d}"
    except ValueError:
        pass
    return parse_time(s) or s

def parse_time(s):
    s = s.strip().upper().replace(".", ":")
    m_ap = re.match(r"^(\d{1,2})(?::(\d{1,2}))?\s*(AM|PM)$", s)
    if m_ap:
        h, mn, ap = int(m_ap.group(1)), int(m_ap.group(2) or 0), m_ap.group(3)
        if h == 12:
            h = 0 if ap == "AM" else 12
        elif ap == "PM":
            h += 12
        if 0 <= h < 24 and 0 <= mn < 60:
            return f"{h:02d}:{mn:02d}"
    m24 = re.match(r"^(\d{1,2}):(\d{1,2})$", s)
    if m24:
        h, mn = int(m24.group(1)), int(m24.group(2))
        if 0 <= h < 24 and 0 <= mn < 60:
            return f"{h:02d}:{mn:02d}"
    return None

def is_past(ds, ts, tz):
    try:
        dt = tz.localize(datetime.strptime(f"{ds} {ts}", "%Y-%m-%d %H:%M"))
        return dt < datetime.now(tz)
    except:
        return False

def past_msg(ts):
    return f"\u26a0 {fmt_time(ts)} has already passed today.\nEnter a future time:"

# ============= SETTINGS =============
def get_cfg(uid):
    uid_s = str(uid)
    rows = cfg_sheet.get_all_values()
    for r in rows[1:]:
        if r and r[0] == uid_s:
            return {"digest_on": r[1] if len(r) > 1 else "true",
                    "digest_time": r[2] if len(r) > 2 else DEF_DIGEST_TIME,
                    "max_retries": r[3] if len(r) > 3 else str(DEF_RETRIES),
                    "retry_gap": r[4] if len(r) > 4 else str(DEF_RETRY_GAP),
                    "timezone": r[5] if len(r) > 5 else DEF_TZ,
                    "username": r[6] if len(r) > 6 else "",
                    "weekly_report": r[7] if len(r) > 7 else "true"}
    cfg_sheet.append_row([uid_s, "true", DEF_DIGEST_TIME, str(DEF_RETRIES), str(DEF_RETRY_GAP), DEF_TZ, "", "true"], value_input_option="RAW")
    return {"digest_on": "true", "digest_time": DEF_DIGEST_TIME, "max_retries": str(DEF_RETRIES),
            "retry_gap": str(DEF_RETRY_GAP), "timezone": DEF_TZ, "username": "", "weekly_report": "true"}

def save_cfg(uid, key, val):
    uid_s = str(uid)
    rows = cfg_sheet.get_all_values()
    col_map = {"digest_on": 2, "digest_time": 3, "max_retries": 4, "retry_gap": 5, "timezone": 6, "username": 7, "weekly_report": 8}
    col = col_map.get(key)
    if not col:
        return
    for i, r in enumerate(rows):
        if r and r[0] == uid_s:
            cfg_sheet.update_cell(i + 1, col, str(val))
            return
    get_cfg(uid)
    save_cfg(uid, key, val)

def update_username(user):
    if not user:
        return
    uname = user.username or ""
    if uname:
        save_cfg(user.id, "username", uname)

def get_user_retries(uid):
    cfg = get_cfg(uid)
    try:
        return int(cfg.get("max_retries", DEF_RETRIES))
    except:
        return DEF_RETRIES

def get_user_gap(uid):
    cfg = get_cfg(uid)
    try:
        return int(cfg.get("retry_gap", DEF_RETRY_GAP)) * 60
    except:
        return DEF_RETRY_GAP * 60

# ============= GROUP HELPERS =============
def set_gsub(gid, uid, name, username="", sub=True):
    gid_s, uid_s = str(gid), str(uid)
    rows = grp_sheet.get_all_values()
    for i, r in enumerate(rows[1:], start=2):
        if len(r) >= 2 and r[0] == gid_s and r[1] == uid_s:
            grp_sheet.update_cell(i, 3, name)
            if username:
                grp_sheet.update_cell(i, 4, username)
            grp_sheet.update_cell(i, 5, "true" if sub else "false")
            return
    grp_sheet.append_row([gid_s, uid_s, name, username or "", "true" if sub else "false"], value_input_option="RAW")

def get_gsubs(gid):
    gid_s = str(gid)
    rows = grp_sheet.get_all_values()
    result = []
    for r in rows[1:]:
        if len(r) >= 5 and r[0] == gid_s and r[4].lower() == "true":
            uname = r[3] if len(r) > 3 else ""
            result.append((r[1], r[2], uname))
    return result

def add_tmember(tid, uid, name, status="waiting"):
    tm_sheet.append_row([tid, str(uid), name, status], value_input_option="RAW")

def get_tmembers(tid):
    rows = tm_sheet.get_all_values()
    return [(r[1], r[2], r[3]) for r in rows[1:] if r and r[0] == tid]

def set_tstatus(tid, uid, status):
    uid_s = str(uid)
    rows = tm_sheet.get_all_values()
    for i, r in enumerate(rows[1:], start=2):
        if r and r[0] == tid and r[1] == uid_s:
            tm_sheet.update_cell(i, 4, status)
            return

def reset_tmembers(tid):
    rows = tm_sheet.get_all_values()
    for i, r in enumerate(rows[1:], start=2):
        if r and r[0] == tid:
            tm_sheet.update_cell(i, 4, "waiting")

# ============= REMINDER HELPERS =============
def get_detail(r):
    msg = r[1] if len(r) > 1 else ""
    ds = norm_date(r[2]) if len(r) > 2 else ""
    ts = norm_time(r[3]) if len(r) > 3 else ""
    rep = r[4] if len(r) > 4 else "none"
    st = r[5] if len(r) > 5 else "active"
    return msg, ds, ts, rep, st

def advance_rep(row, rep, ds, ts):
    try:
        d = datetime.strptime(ds, "%Y-%m-%d")
    except:
        return
    if rep == "daily":
        nd = d + timedelta(days=1)
    elif rep == "weekly":
        nd = d + timedelta(days=7)
    elif rep == "monthly":
        m = d.month + 1
        y = d.year
        if m > 12:
            m, y = 1, y + 1
        try:
            nd = d.replace(year=y, month=m)
        except:
            nd = d.replace(year=y, month=m, day=28)
    elif rep.startswith("custom:"):
        days = rep.replace("custom:", "").split(",")
        for i in range(1, 8):
            nd = d + timedelta(days=i)
            if DAY_NAMES[nd.weekday()] in days:
                break
        else:
            nd = d + timedelta(days=1)
    else:
        return
    sheet.update_cell(row, 3, nd.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, "active")
    sheet.update_cell(row, 7, 0)

def is_custom_match(rep, dt):
    if not rep.startswith("custom:"):
        return False
    days = rep.replace("custom:", "").split(",")
    return DAY_NAMES[dt.weekday()] in days

# ============= UI HELPERS =============
def home_kb():
    return IKM([[IKB("\uff0b New", callback_data="add")]])

HOME_TEXT = "Type your reminder:\n<i>\"Buy milk tomorrow at 5pm\"</i>\n\nOr tap \uff0b New for step-by-step."

def cancel_kb():
    return IKM([[IKB("\u2715 Cancel", callback_data="cancel")]])

def repeat_kb(row=None):
    prefix = f"chrep_{row}_" if row else "rep_"
    return IKM([
        [IKB("Daily", callback_data=f"{prefix}daily"), IKB("Weekly", callback_data=f"{prefix}weekly")],
        [IKB("Monthly", callback_data=f"{prefix}monthly"), IKB("Customize", callback_data=f"{prefix}custom")],
        [IKB("\u00ab Back", callback_data=f"repbk_{row}" if row else "cancel")]
    ])

def custom_days_kb(selected, row=None):
    prefix = f"cday_{row}_" if row else "cday__"
    save_cb = f"csave_{row}" if row else "csave_"
    back_cb = f"chrep_{row}_back" if row else "rep_back"
    btns = []
    r1, r2 = [], []
    for i, d in enumerate(DAY_SHORT):
        dn = DAY_NAMES[i]
        label = f"[{d}]" if dn in selected else d
        cb = f"{prefix}{dn}"
        if i < 4:
            r1.append(IKB(label, callback_data=cb))
        else:
            r2.append(IKB(label, callback_data=cb))
    btns.append(r1)
    btns.append(r2)
    btns.append([IKB("Mon\u2013Fri", callback_data=f"{prefix}mf"),
                 IKB("All", callback_data=f"{prefix}all"),
                 IKB("Clear", callback_data=f"{prefix}clr")])
    if selected:
        btns.append([IKB("\u2713 Save", callback_data=save_cb)])
    btns.append([IKB("\u00ab Back", callback_data=back_cb)])
    return btns

def snz_kb(row):
    btns = []
    r = []
    for mins, label in SNOOZE_OPTIONS:
        r.append(IKB(label, callback_data=f"snz_{row}_{mins}"))
        if len(r) == 3:
            btns.append(r)
            r = []
    if r:
        btns.append(r)
    btns.append([IKB("\u00ab Back", callback_data=f"snzb_{row}")])
    return IKM(btns)

def reminder_kb(row):
    return IKM([[IKB("Snooze", callback_data=f"snzp_{row}"), IKB("\u2705 Done", callback_data=f"done_{row}")]])

async def safe_edit(msg, text, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except:
        pass

def store_prompt(ud, msg):
    ud["p_mid"] = msg.message_id
    ud["p_cid"] = msg.chat_id

async def rm_prompt(ud, bot):
    mid, cid = ud.pop("p_mid", None), ud.pop("p_cid", None)
    if mid and cid:
        try:
            await bot.edit_message_reply_markup(cid, mid, reply_markup=None)
        except:
            pass

async def del_prompt(ud, bot):
    mid, cid = ud.pop("p_mid", None), ud.pop("p_cid", None)
    if mid and cid:
        try:
            await bot.delete_message(cid, mid)
        except:
            pass

def rm_home(ud, bot):
    return rm_old_home(ud, bot)

async def rm_old_home(ud, bot):
    mid, cid = ud.pop("h_mid", None), ud.pop("h_cid", None)
    if mid and cid:
        try:
            await bot.edit_message_reply_markup(cid, mid, reply_markup=None)
        except:
            pass

# ============= CALENDAR =============
def cal_kb(year, month, tz, back_cb="cancel", back_txt="\u2715 Cancel"):
    now = datetime.now(tz)
    today = now.date()
    first_wd, ndays = cal_mod.monthrange(year, month)
    header = f"{cal_mod.month_name[month]} {year}"
    btns = [[IKB(header, callback_data="noop")]]
    btns.append([IKB(d, callback_data="noop") for d in DAY_SHORT])
    row = [IKB(" ", callback_data="noop")] * first_wd
    for d in range(1, ndays + 1):
        dt = datetime(year, month, d).date()
        if dt < today:
            row.append(IKB("\u00b7", callback_data="noop"))
        elif dt == today:
            row.append(IKB(f"[{d}]", callback_data=f"day_{dt.isoformat()}"))
        else:
            row.append(IKB(str(d), callback_data=f"day_{dt.isoformat()}"))
        if len(row) == 7:
            if not all(b.callback_data == "noop" or b.text == "\u00b7" for b in row):
                btns.append(row)
            row = []
    if row:
        row += [IKB(" ", callback_data="noop")] * (7 - len(row))
        if not all(b.callback_data == "noop" or b.text == "\u00b7" for b in row):
            btns.append(row)
    quick = []
    if today.month == month and today.year == year:
        quick.append(IKB("Today", callback_data=f"day_{today.isoformat()}"))
    tmrw = today + timedelta(days=1)
    if tmrw.month == month and tmrw.year == year:
        quick.append(IKB("Tomorrow", callback_data=f"day_{tmrw.isoformat()}"))
    if quick:
        btns.append(quick)
    pm = month - 1 or 12
    py = year - 1 if month == 1 else year
    nm = month % 12 + 1
    ny = year + 1 if month == 12 else year
    btns.append([IKB("\u2039", callback_data=f"cal_{py}_{pm}"), IKB("\u203a", callback_data=f"cal_{ny}_{nm}")])
    btns.append([IKB(back_txt, callback_data=back_cb)])
    return IKM(btns)

# ============= NL PARSER =============
def _find_time(text):
    patterns = [
        (r'(?:at|by)\s+(\d{1,2})[:.](\d{2})\s*(am|pm)', re.I),
        (r'(?:at|by)\s+(\d{1,2})\s*(am|pm)', re.I),
        (r'(\d{1,2})[:.](\d{2})\s*(am|pm)', re.I),
        (r'(\d{1,2})\s*(am|pm)', re.I),
        (r'(?:at|by)\s+(\d{1,2}):(\d{2})\b', re.I),
    ]
    for pat, flg in patterns:
        m = re.search(pat, text, flg)
        if m:
            gs = m.groups()
            if len(gs) == 3 and gs[2]:
                h, mn = int(gs[0]), int(gs[1]) if gs[1] else 0
                ap = gs[2].upper()
                if h == 12:
                    h = 0 if ap == "AM" else 12
                elif ap == "PM":
                    h += 12
            elif len(gs) == 2 and gs[1] and gs[1].upper() in ("AM", "PM"):
                h, mn = int(gs[0]), 0
                ap = gs[1].upper()
                if h == 12:
                    h = 0 if ap == "AM" else 12
                elif ap == "PM":
                    h += 12
            else:
                h, mn = int(gs[0]), int(gs[1]) if len(gs) > 1 and gs[1] else 0
            if 0 <= h < 24 and 0 <= mn < 60:
                return f"{h:02d}:{mn:02d}", m.start(), m.end()
    return None, -1, -1

def _find_date(text, tz):
    now = datetime.now(tz)
    today = now.date()
    lw = text.lower()
    patterns = [
        (r'\b(today|tonight)\b', 0),
        (r'\b(tomorrow|tmrw|tmr)\b', 1),
        (r'\bday after tomorrow\b', 2),
    ]
    for pat, delta in patterns:
        m = re.search(pat, lw)
        if m:
            d = today + timedelta(days=delta)
            return d.strftime("%Y-%m-%d"), m.start(), m.end()
    for i, dn in enumerate(DAY_NAMES):
        full = DAY_FULL[i].lower()
        pat = rf'\b(next\s+)?({full}|{dn})\b'
        m = re.search(pat, lw)
        if m:
            diff = (i - today.weekday()) % 7
            if diff == 0:
                diff = 7
            if m.group(1):
                diff = (i - today.weekday()) % 7
                if diff == 0:
                    diff = 7
            d = today + timedelta(days=diff)
            return d.strftime("%Y-%m-%d"), m.start(), m.end()
    m = re.search(r'\bon\s+(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*', lw)
    if m:
        day_n, mon_s = int(m.group(1)), m.group(2)
        months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
        mn = months.get(mon_s[:3], 0)
        if mn:
            y = today.year if datetime(today.year, mn, day_n).date() >= today else today.year + 1
            try:
                return datetime(y, mn, day_n).strftime("%Y-%m-%d"), m.start(), m.end()
            except:
                pass
    m = re.search(r'\bon\s+(\d{1,2})(?:st|nd|rd|th)?\b', lw)
    if m:
        day_n = int(m.group(1))
        if 1 <= day_n <= 31:
            try:
                d = today.replace(day=day_n)
                if d < today:
                    nm = today.month + 1
                    ny = today.year
                    if nm > 12:
                        nm, ny = 1, ny + 1
                    d = d.replace(year=ny, month=nm)
                return d.strftime("%Y-%m-%d"), m.start(), m.end()
            except:
                pass
    m = re.search(r'\bnext week\b', lw)
    if m:
        d = today + timedelta(days=7)
        return d.strftime("%Y-%m-%d"), m.start(), m.end()
    return None, -1, -1

def _find_repeat(text, tz):
    now = datetime.now(tz)
    today = now.date()
    lw = text.lower()
    m = re.search(r'\bevery\s+day\b', lw)
    if m:
        return "daily", m.start(), m.end(), None
    for i, dn in enumerate(DAY_NAMES):
        full = DAY_FULL[i].lower()
        pat = rf'\bevery\s+({full}|{dn}(?:s)?)\b'
        m = re.search(pat, lw)
        if m:
            diff = (i - today.weekday()) % 7
            if diff == 0:
                diff = 7
            nd = today + timedelta(days=diff)
            return "weekly", m.start(), m.end(), nd.strftime("%Y-%m-%d")
    for word, val in [("daily", "daily"), ("every day", "daily"), ("weekly", "weekly"),
                      ("every week", "weekly"), ("monthly", "monthly"), ("every month", "monthly")]:
        m = re.search(rf'\b{word}\b', lw)
        if m:
            return val, m.start(), m.end(), None
    return None, -1, -1, None

def _find_relative(text, tz):
    now = datetime.now(tz)
    lw = text.lower()
    m = re.match(r'.*?\b(?:in|after)\s+(\d+)\s*(min(?:ute)?s?|hr?s?|hours?|days?|weeks?)\b', lw)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("min"):
            dt = now + timedelta(minutes=n)
        elif unit.startswith("h"):
            dt = now + timedelta(hours=n)
        elif unit.startswith("d"):
            dt = now + timedelta(days=n)
        elif unit.startswith("w"):
            dt = now + timedelta(weeks=n)
        else:
            return None, None, -1, -1
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), m.start(0) + lw.index(m.group(0)[lw.index("in") if "in" in lw else lw.index("after"):].split()[0]), m.end()
    return None, None, -1, -1

def parse_nl_partial(text, tz):
    spans = []
    rd, rt = _find_relative(text, tz)
    if rd and rt:
        m = re.search(r'\b(?:in|after)\s+\d+\s*(?:min(?:ute)?s?|hr?s?|hours?|days?|weeks?)\b', text, re.I)
        if m:
            spans.append((m.start(), m.end()))
        rep, rs, re2, rep_date = _find_repeat(text, tz)
        if rep and rs >= 0:
            spans.append((rs, re2))
        msg = _clean_msg(text, spans)
        return {"message": msg, "date": rd, "time": rt, "repeat": rep} if msg else None
    rep, rs, re2, rep_date = _find_repeat(text, tz)
    if rep and rs >= 0:
        spans.append((rs, re2))
    t, ts, te = _find_time(text)
    if t and ts >= 0:
        spans.append((ts, te))
    d, ds2, de2 = _find_date(text, tz)
    if d and ds2 >= 0:
        spans.append((ds2, de2))
    if rep_date and not d:
        d = rep_date
    msg = _clean_msg(text, spans)
    if not msg:
        return None
    if not t and not d and not rep:
        prefix_m = FILLER.match(text)
        if not prefix_m:
            return None
    result = {"message": msg}
    if d:
        result["date"] = d
    if t:
        result["time"] = t
    if rep:
        result["repeat"] = rep
    return result

def _clean_msg(text, spans):
    if not spans:
        msg = text
    else:
        spans.sort()
        parts, last = [], 0
        for s, e in spans:
            if s > last:
                parts.append(text[last:s])
            last = max(last, e)
        if last < len(text):
            parts.append(text[last:])
        msg = " ".join(parts)
    msg = FILLER.sub("", msg).strip()
    msg = re.sub(r'\s+', ' ', msg).strip(" .,;:-")
    return msg

# ============= AUTO MINIMIZE =============
async def p_auto_minimize(ctx):
    d = ctx.job.data
    try:
        await ctx.bot.edit_message_text(
            chat_id=d["cid"], message_id=d["mid"],
            text=d["min_text"], parse_mode="HTML",
            reply_markup=IKM([[IKB("\U0001f4cb Show", callback_data=d["show_cb"])]])
        )
    except:
        pass

def schedule_minimize(ctx, cid, mid, min_text, show_cb, delay=60):
    ctx.job_queue.run_once(p_auto_minimize, delay, data={"cid": cid, "mid": mid, "min_text": min_text, "show_cb": show_cb})

# ============= GROUP AUTO MINIMIZE =============
async def g_auto_minimize(ctx):
    d = ctx.job.data
    try:
        await ctx.bot.edit_message_text(
            chat_id=d["cid"], message_id=d["mid"],
            text=d["min_text"], parse_mode="HTML",
            reply_markup=IKM([[IKB("\U0001f4cb Show", callback_data=d["show_cb"])]])
        )
    except:
        pass

# ============= COMMANDS =============
async def start(update, ctx):
    uid = update.effective_user.id
    update_username(update.effective_user)
    if update.effective_chat.type != "private":
        txt = (f"{hdr('Smart Reminder Bot')}\n\n"
               "<b>Commands</b>\n"
               "/remind \u2014 Group reminder\n"
               "/list \u2014 Active reminders\n\n"
               "<b>Examples</b>\n"
               "<code>/remind Buy milk at 5pm</code>\n"
               "<code>/remind Meeting tomorrow 10am daily</code>\n"
               "<code>/remind</code> \u2014 step-by-step\n\n"
               "<b>Tag members to assign:</b>\n"
               "<code>/remind @user Submit report at 5pm</code>")
        sent = await update.message.reply_text(txt, parse_mode="HTML",
            reply_markup=IKM([[IKB("\u2715 Close", callback_data="gclose")]]))
        gid = update.effective_chat.id
        ctx.bot_data[f"gmin_{sent.message_id}"] = {"text": txt, "min_text": "Smart Reminder Bot",
            "show_cb": f"gshow_start_{sent.message_id}"}
        ctx.job_queue.run_once(g_auto_minimize, 30, data={"cid": gid, "mid": sent.message_id,
            "min_text": "Smart Reminder Bot", "show_cb": f"gshow_start_{sent.message_id}"})
        return
    await rm_old_home(ctx.user_data, ctx.bot)
    sent = await update.message.reply_text(f"{hdr('Smart Reminder Bot')}\n\n{HOME_TEXT}", parse_mode="HTML", reply_markup=home_kb())
    ctx.user_data["h_mid"] = sent.message_id
    ctx.user_data["h_cid"] = sent.chat_id

async def add_cmd(update, ctx):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /remind here for group reminders.")
        return
    update_username(update.effective_user)
    ctx.user_data.clear()
    ctx.user_data["step"] = "message"
    sent = await update.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", parse_mode="HTML", reply_markup=cancel_kb())
    store_prompt(ctx.user_data, sent)

async def list_cmd(update, ctx):
    update_username(update.effective_user)
    if update.effective_chat.type != "private":
        await _group_list(update, ctx)
        return
    await _private_list(update, ctx, new=True)

async def _private_list(update_or_query, ctx, new=True):
    uid = update_or_query.effective_user.id if hasattr(update_or_query, "effective_user") else update_or_query.from_user.id
    rows = sheet.get_all_values()
    items = []
    uid_s = str(uid)
    for i, r in enumerate(rows[1:], start=2):
        if not r or r[0] != uid_s:
            continue
        if len(r) > 7 and r[7]:
            continue
        st = r[5] if len(r) > 5 else ""
        if st in ("active", "pending", "snoozed", "missed"):
            msg, ds, ts, rep, _ = get_detail(r)
            items.append((i, msg, ds, ts, rep, st))
    if not items:
        txt = f"{hdr('Reminders')}\n\nNo active reminders."
        if new:
            target = update_or_query.message if hasattr(update_or_query, "message") and hasattr(update_or_query.message, "reply_text") else update_or_query
            sent = await target.reply_text(txt, parse_mode="HTML", reply_markup=IKM([[IKB("\u2715 Close", callback_data="pclose_list")], [IKB("\uff0b New", callback_data="add")]]))
        else:
            sent = update_or_query.message if hasattr(update_or_query, "message") else update_or_query
            await safe_edit(sent, txt, IKM([[IKB("\u2715 Close", callback_data="pclose_list")], [IKB("\uff0b New", callback_data="add")]]))
        return
    lines = [hdr("Reminders"), ""]
    for idx, (row, msg, ds, ts, rep, st) in enumerate(items):
        ic = ST_IC.get(st, "\u25cb")
        short_msg = msg[:25] + "\u2026" if len(msg) > 25 else msg
        dts = fmt_date(ds)
        tts = fmt_time(ts)
        lines.append(f"{idx+1} {ic} {short_msg}\n   {dts} \u00b7 {tts}")
    txt = "\n".join(lines)
    num_btns = []
    row_btns = []
    for idx, (row, *_) in enumerate(items):
        row_btns.append(IKB(str(idx + 1), callback_data=f"view_{row}"))
        if len(row_btns) == 5:
            num_btns.append(row_btns)
            row_btns = []
    if row_btns:
        num_btns.append(row_btns)
    num_btns.append([IKB("\u2715 Close", callback_data="pclose_list")])
    num_btns.append([IKB("\uff0b New", callback_data="add")])
    kb = IKM(num_btns)
    if new:
        target = update_or_query.message if hasattr(update_or_query, "message") and not isinstance(update_or_query.message, type(None)) else update_or_query
        if hasattr(target, "reply_text"):
            sent = await target.reply_text(txt, parse_mode="HTML", reply_markup=kb)
            count = len(items)
            schedule_minimize(ctx, sent.chat_id, sent.message_id,
                f"\U0001f4cb Reminders ({count} active)", f"pshow_list_{sent.message_id}", 60)
        else:
            await safe_edit(target, txt, kb)
    else:
        target = update_or_query.message if hasattr(update_or_query, "message") else update_or_query
        await safe_edit(target, txt, kb)

async def _group_list(update, ctx):
    gid = update.effective_chat.id
    rows = sheet.get_all_values()
    gid_s = str(gid)
    items = []
    for i, r in enumerate(rows[1:], start=2):
        if len(r) <= 7 or r[7] != gid_s:
            continue
        st = r[5] if len(r) > 5 else ""
        if st in ("active", "pending", "snoozed"):
            msg, ds, ts, rep, _ = get_detail(r)
            items.append((i, msg, ds, ts, rep, st))
    if not items:
        txt = f"{hdr('Group Reminders')}\n\nNo active reminders."
        sent = await update.message.reply_text(txt, parse_mode="HTML",
            reply_markup=IKM([[IKB("\u2715 Close", callback_data="gclose")]]))
        ctx.bot_data[f"gmin_{sent.message_id}"] = {"text": txt, "min_text": "Group Reminders \u2014 No active",
            "show_cb": f"gshow_list_{gid}_{sent.message_id}"}
        ctx.job_queue.run_once(g_auto_minimize, 30, data={"cid": gid, "mid": sent.message_id,
            "min_text": "Group Reminders \u2014 No active", "show_cb": f"gshow_list_{gid}_{sent.message_id}"})
        return
    lines = [hdr("Group Reminders"), ""]
    for idx, (row, msg, ds, ts, rep, st) in enumerate(items):
        short = msg[:25] + "\u2026" if len(msg) > 25 else msg
        lines.append(f"{idx+1} {ST_IC.get(st, '\u25cb')} {short}\n   {fmt_date(ds)} \u00b7 {fmt_time(ts)}")
    txt = "\n".join(lines)
    btns = []
    rb = []
    for idx, (row, *_) in enumerate(items):
        rb.append(IKB(str(idx+1), callback_data=f"gview_{row}"))
        if len(rb) == 5:
            btns.append(rb)
            rb = []
    if rb:
        btns.append(rb)
    btns.append([IKB("\u2715 Close", callback_data="gclose")])
    sent = await update.message.reply_text(txt, parse_mode="HTML", reply_markup=IKM(btns))
    ctx.bot_data[f"gmin_{sent.message_id}"] = {"text": txt, "min_text": f"Group Reminders ({len(items)})",
        "show_cb": f"gshow_list_{gid}_{sent.message_id}", "kb": btns}
    ctx.job_queue.run_once(g_auto_minimize, 60, data={"cid": gid, "mid": sent.message_id,
        "min_text": f"Group Reminders ({len(items)})", "show_cb": f"gshow_list_{gid}_{sent.message_id}"})

async def info_cmd(update, ctx):
    update_username(update.effective_user)
    txt = (f"{hdr('About')}\n\n"
           "Smart Reminder Bot\n\n"
           "<b>Quick:</b> Type naturally\n"
           "<i>\"Buy milk tomorrow at 5pm\"</i>\n"
           "<i>\"Meeting in 30 min\"</i>\n"
           "<i>\"Gym at 6pm daily\"</i>\n"
           "<i>\"Standup every monday 10am\"</i>\n\n"
           "<b>Features:</b>\n"
           "\u2022 Smart snooze (15m\u201312h)\n"
           "\u2022 Auto-retry if missed\n"
           "\u2022 Daily digest\n"
           "\u2022 Weekly report\n"
           "\u2022 Custom days (Mon\u2013Fri)\n"
           "\u2022 Monthly schedule view\n"
           "\u2022 Group reminders\n"
           "\u2022 Per-user timezone\n\n"
           "Tip: Add \"daily\", \"weekly\" or \"monthly\" to set recurring.")
    sent = await update.message.reply_text(txt, parse_mode="HTML",
        reply_markup=IKM([[IKB("\u2715 Close", callback_data="pclose_info")], [IKB("\uff0b New", callback_data="add")]]))
    schedule_minimize(ctx, sent.chat_id, sent.message_id,
        "\u2139\ufe0f Info", f"pshow_info_{sent.message_id}", 60)
    ctx.bot_data[f"pinfo_{sent.message_id}"] = txt

async def settings_cmd(update, ctx):
    update_username(update.effective_user)
    if update.effective_chat.type != "private":
        return
    uid = update.effective_user.id
    cfg = get_cfg(uid)
    await _show_settings(update.message, cfg, new=True)

async def _show_settings(target, cfg, new=True):
    dig = "ON" if cfg.get("digest_on", "true") == "true" else "OFF"
    dt = fmt_time(cfg.get("digest_time", DEF_DIGEST_TIME))
    ret = cfg.get("max_retries", str(DEF_RETRIES))
    gap = cfg.get("retry_gap", str(DEF_RETRY_GAP))
    tz_name = cfg.get("timezone", DEF_TZ)
    tz_label = tz_short(tz_name)
    wr = "ON" if cfg.get("weekly_report", "true") == "true" else "OFF"
    txt = (f"{hdr('Settings')}\n\n"
           f"Daily Digest: {dig} \u00b7 {dt}\n"
           f"Max Retries: {ret}\u00d7\n"
           f"Retry Gap: {gap} min\n"
           f"Timezone: {tz_label}\n"
           f"Weekly Report: {wr}")
    btns = [
        [IKB(f"Digest: {dig}", callback_data="cfg_digest"), IKB(f"\u23f0 {dt}", callback_data="cfg_digtime")],
        [IKB(f"Retries: {ret}\u00d7", callback_data="cfg_retries"), IKB(f"Gap: {gap}m", callback_data="cfg_gap")],
        [IKB(f"\U0001f30d {tz_label}", callback_data="cfg_tz")],
        [IKB(f"Report: {wr}", callback_data="cfg_weekly")],
        [IKB("\u00ab Back", callback_data="back_home")]
    ]
    if new:
        await target.reply_text(txt, parse_mode="HTML", reply_markup=IKM(btns))
    else:
        await safe_edit(target, txt, IKM(btns))

async def month_cmd(update, ctx):
    update_username(update.effective_user)
    if update.effective_chat.type != "private":
        return
    uid = update.effective_user.id
    tz = get_tz(uid)
    now = datetime.now(tz)
    sent = await update.message.reply_text("Loading...", parse_mode="HTML")
    txt, kb = _build_month(uid, now.year, now.month, tz)
    await safe_edit(sent, txt, kb)
    schedule_minimize(ctx, sent.chat_id, sent.message_id,
        f"\U0001f4c5 {cal_mod.month_name[now.month]} {now.year}", f"pshow_month_{now.year}_{now.month}_{sent.message_id}", 60)

def _build_month(uid, year, month, tz):
    now = datetime.now(tz)
    today = now.date()
    first_day = datetime(year, month, 1).date()
    _, ndays = cal_mod.monthrange(year, month)
    last_day = datetime(year, month, ndays).date()
    rows_data = sheet.get_all_values()
    uid_s = str(uid)
    reminders = []
    for i, r in enumerate(rows_data[1:], start=2):
        if not r or r[0] != uid_s:
            continue
        if len(r) > 7 and r[7]:
            continue
        msg, ds, ts, rep, st = get_detail(r)
        if not ds:
            continue
        reminders.append((i, msg, ds, ts, rep, st))
    all_events = _expand_for_range(reminders, first_day, last_day, today)
    weeks = []
    d = first_day
    while d <= last_day:
        ws = d
        we = min(d + timedelta(days=6 - d.weekday()), last_day)
        week_events = [e for e in all_events if ws <= e[0] <= we]
        weeks.append((ws, we, week_events))
        d = we + timedelta(days=1)
    while len(weeks) < 4:
        weeks.append((last_day, last_day, []))
    if len(weeks) > 4:
        extra = []
        for ev in weeks[4:]:
            extra.extend(ev[2])
        ws3, _, evs3 = weeks[3]
        weeks[3] = (ws3, last_day, evs3 + extra)
        weeks = weeks[:4]
    total, done_c, missed_c, upcoming_c = 0, 0, 0, 0
    for ev_list in [w[2] for w in weeks]:
        for _, _, _, _, st in ev_list:
            total += 1
            if st == "done":
                done_c += 1
            elif st == "missed":
                missed_c += 1
            else:
                upcoming_c += 1
    mn = cal_mod.month_name[month]
    lines = [hdr(f"\U0001f4c5 {mn} {year}"), ""]
    current_week = -1
    for wi, (ws, we, evts) in enumerate(weeks):
        ws_str = ws.strftime("%-d %b")
        we_str = we.strftime("%-d %b")
        count = len(evts)
        marker = " \u25c2" if ws <= today <= we else ""
        lines.append(f"W{wi+1}: {ws_str}\u2013{we_str} \u00b7 {count} reminder{'s' if count != 1 else ''}{marker}")
        if ws <= today <= we:
            current_week = wi
    lines.append("")
    summary_parts = [f"Total: {total}"]
    if done_c:
        summary_parts.append(f"\u2705 {done_c} done")
    if missed_c:
        summary_parts.append(f"\u2717 {missed_c} missed")
    if upcoming_c:
        summary_parts.append(f"\u25cb {upcoming_c} upcoming")
    lines.append(" \u00b7 ".join(summary_parts))
    txt = "\n".join(lines)
    btns = []
    rb = []
    for wi in range(len(weeks)):
        rb.append(IKB(str(wi + 1), callback_data=f"mw_{year}_{month}_{wi}"))
        if len(rb) == 4:
            btns.append(rb)
            rb = []
    if rb:
        btns.append(rb)
    pm = month - 1 or 12
    py = year if month > 1 else year - 1
    nm = month % 12 + 1
    ny = year if month < 12 else year + 1
    btns.append([IKB(f"\u2039 {cal_mod.month_abbr[pm]}", callback_data=f"mn_{py}_{pm}"),
                 IKB(f"{cal_mod.month_abbr[nm]} \u203a", callback_data=f"mn_{ny}_{nm}")])
    btns.append([IKB("\u2715 Close", callback_data="pclose_month")])
    return txt, IKM(btns)

def _build_week(uid, year, month, week_idx, tz):
    now = datetime.now(tz)
    today = now.date()
    first_day = datetime(year, month, 1).date()
    _, ndays = cal_mod.monthrange(year, month)
    last_day = datetime(year, month, ndays).date()
    weeks = []
    d = first_day
    while d <= last_day:
        ws = d
        we = min(d + timedelta(days=6 - d.weekday()), last_day)
        weeks.append((ws, we))
        d = we + timedelta(days=1)
    while len(weeks) < 4:
        weeks.append((last_day, last_day))
    if len(weeks) > 4:
        ws3, _ = weeks[3]
        weeks[3] = (ws3, last_day)
        weeks = weeks[:4]
    if week_idx >= len(weeks):
        week_idx = len(weeks) - 1
    ws, we = weeks[week_idx]
    rows_data = sheet.get_all_values()
    uid_s = str(uid)
    reminders = []
    for i, r in enumerate(rows_data[1:], start=2):
        if not r or r[0] != uid_s:
            continue
        if len(r) > 7 and r[7]:
            continue
        msg, ds, ts, rep, st = get_detail(r)
        if not ds:
            continue
        reminders.append((i, msg, ds, ts, rep, st))
    events = _expand_for_range(reminders, ws, we, today)
    events.sort(key=lambda e: (e[0], e[3] or ""))
    mn = cal_mod.month_name[month]
    ws_str = ws.strftime("%-d %b")
    we_str = we.strftime("%-d %b")
    lines = [hdr(f"W{week_idx+1}: {ws_str}\u2013{we_str}"), ""]
    by_date = {}
    recurring = []
    for dt, msg, rep, ts, st in events:
        if rep and rep != "none":
            key = (msg, ts, rep)
            found = False
            for ri, (k, dates, s) in enumerate(recurring):
                if k == key:
                    recurring[ri] = (k, dates + [dt], s)
                    found = True
                    break
            if not found:
                recurring.append((key, [dt], st))
        else:
            by_date.setdefault(dt, []).append((msg, ts, st))
    d = ws
    while d <= we:
        if d in by_date:
            day_str = d.strftime("%-d %b, %a")
            if d == today:
                day_str = f"Today, {day_str}"
            lines.append(f"<b>{day_str}</b>")
            for msg, ts, st in by_date[d]:
                ic = ST_IC.get(st, "\u25cb")
                lines.append(f"  {ic} {msg} \u00b7 {fmt_time(ts)}")
            lines.append("")
        d += timedelta(days=1)
    for (msg, ts, rep), dates, st in recurring:
        if not dates:
            continue
        ic = ST_IC.get(st, "\u25cb")
        day_names = [DAY_SHORT[d.weekday()] for d in sorted(set(dates))]
        if len(day_names) == 5 and all(d in day_names for d in ["Mon","Tue","Wed","Thu","Fri"]):
            day_label = "Mon\u2013Fri"
        elif len(day_names) == 7:
            day_label = "Daily"
        else:
            day_label = ", ".join(day_names)
        lines.append(f"<b>{day_label}</b>")
        lines.append(f"  {ic} {msg} \u00b7 {fmt_time(ts)}")
        lines.append("")
    if len(lines) <= 3:
        lines.append("No reminders this week.")
    txt = "\n".join(lines)
    btns = []
    if week_idx < 3:
        btns.append([IKB(f"W{week_idx+2} \u203a", callback_data=f"mw_{year}_{month}_{week_idx+1}")])
    if week_idx > 0:
        btns.append([IKB(f"\u2039 W{week_idx}", callback_data=f"mw_{year}_{month}_{week_idx-1}")])
    btns.append([IKB(f"\u00ab {mn} {year}", callback_data=f"mn_{year}_{month}")])
    return txt, IKM(btns)

def _expand_for_range(reminders, start_date, end_date, today):
    events = []
    for row, msg, ds, ts, rep, st in reminders:
        try:
            rd = datetime.strptime(ds, "%Y-%m-%d").date()
        except:
            continue
        if rep in (None, "", "none"):
            if start_date <= rd <= end_date:
                events.append((rd, msg, rep, ts, st))
        elif rep == "daily":
            d = max(rd, start_date)
            while d <= end_date:
                est = st if d <= today else "active"
                events.append((d, msg, rep, ts, est))
                d += timedelta(days=1)
        elif rep == "weekly":
            d = rd
            while d <= end_date:
                if d >= start_date:
                    est = st if d <= today else "active"
                    events.append((d, msg, rep, ts, est))
                d += timedelta(days=7)
        elif rep == "monthly":
            for m_off in range(13):
                nm = rd.month + m_off
                ny = rd.year + (nm - 1) // 12
                nm = (nm - 1) % 12 + 1
                try:
                    d = rd.replace(year=ny, month=nm)
                except:
                    continue
                if d > end_date:
                    break
                if d >= start_date:
                    est = st if d <= today else "active"
                    events.append((d, msg, rep, ts, est))
        elif rep.startswith("custom:"):
            cdays = rep.replace("custom:", "").split(",")
            d = max(rd, start_date)
            while d <= end_date:
                if DAY_NAMES[d.weekday()] in cdays:
                    est = st if d <= today else "active"
                    events.append((d, msg, rep, ts, est))
                d += timedelta(days=1)
    return events

# ============= REMIND (GROUP) =============
def extract_tag_texts(msg):
    if not msg.entities:
        return []
    tags = []
    for e in msg.entities:
        if e.type == "mention":
            raw = msg.text[e.offset:e.offset + e.length]
            uname = raw.lstrip("@").lower()
            tags.append(uname)
        elif e.type == "text_mention" and e.user:
            tags.append(str(e.user.id))
            if e.user.username:
                tags.append(e.user.username.lower())
    return tags

async def remind_cmd(update, ctx):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Use /add in private or just type naturally.")
        return
    uid = update.effective_user.id
    gid = update.effective_chat.id
    name = update.effective_user.first_name or "User"
    uname = update.effective_user.username or ""
    set_gsub(gid, uid, name, uname, True)
    update_username(update.effective_user)
    text = (update.message.text or "").strip()
    cmd_part = text.split()[0] if text else ""
    body = text[len(cmd_part):].strip() if len(text) > len(cmd_part) else ""
    tags = extract_tag_texts(update.message)
    if body:
        tz = get_tz(uid)
        parsed = parse_nl_partial(body, tz)
        if parsed and parsed.get("message"):
            ctx.user_data["g_chat"] = gid
            ctx.user_data["g_creator"] = uid
            ctx.user_data["g_creator_name"] = name
            ctx.user_data["g_tags"] = tags
            msg_text = parsed["message"]
            date_val = parsed.get("date")
            time_val = parsed.get("time")
            rep_val = parsed.get("repeat")
            ctx.user_data["message"] = msg_text
            if date_val:
                ctx.user_data["date"] = date_val
            if time_val:
                ctx.user_data["time"] = time_val
            if rep_val:
                ctx.user_data["repeat"] = rep_val
            if date_val and time_val:
                if is_past(date_val, time_val, tz):
                    ctx.user_data["step"] = "g_date"
                    now = datetime.now(tz)
                    sent = await update.message.reply_text(
                        f"{hdr('Group Reminder')}\n{msg_text}\n\n{past_msg(time_val)}",
                        parse_mode="HTML",
                        reply_markup=cal_kb(now.year, now.month, tz, "gcancel"))
                    return
                await _finish_group(update.message, ctx)
                return
            if date_val and not time_val:
                ctx.user_data["step"] = "g_time"
                sent = await update.message.reply_text(
                    f"{hdr('Group Reminder')}\n{msg_text}\n{fmt_date(date_val)}\n\nEnter time:\ne.g. 9pm, 9:30 PM, 21:30",
                    parse_mode="HTML", reply_markup=ForceReply(selective=True))
                return
            if time_val and not date_val:
                tz2 = get_tz(uid)
                now2 = datetime.now(tz2)
                td = now2.strftime("%Y-%m-%d")
                if not is_past(td, time_val, tz2):
                    ctx.user_data["date"] = td
                    await _finish_group(update.message, ctx)
                    return
                ctx.user_data["step"] = "g_date"
                sent = await update.message.reply_text(
                    f"{hdr('Group Reminder')}\n{msg_text}\n{fmt_time(time_val)}\n\n{past_msg(time_val)}",
                    parse_mode="HTML",
                    reply_markup=cal_kb(now2.year, now2.month, tz2, "gcancel"))
                return
            ctx.user_data["step"] = "g_date"
            tz3 = get_tz(uid)
            now3 = datetime.now(tz3)
            sent = await update.message.reply_text(
                f"{hdr('Group Reminder')}\n{msg_text}\n\nPick a date:",
                parse_mode="HTML",
                reply_markup=cal_kb(now3.year, now3.month, tz3, "gcancel"))
            return
    ctx.user_data["g_chat"] = gid
    ctx.user_data["g_creator"] = uid
    ctx.user_data["g_creator_name"] = name
    ctx.user_data["g_tags"] = tags
    ctx.user_data["step"] = "g_message"
    await update.message.reply_text(
        f"{hdr('Group Reminder')}\nEnter message:",
        parse_mode="HTML",
        reply_markup=ForceReply(selective=True))

async def _finish_group(target, ctx):
    ud = ctx.user_data
    msg = ud.get("message", "")
    ds = ud.get("date", "")
    ts = ud.get("time", "")
    rep = ud.get("repeat", "none")
    gid = ud.get("g_chat")
    creator = ud.get("g_creator")
    creator_name = ud.get("g_creator_name", "")
    tags = ud.get("g_tags", [])
    tid = f"t_{int(datetime.now().timestamp())}"
    sheet.append_row([str(creator), msg, ds, ts, rep, "active", 0, str(gid), tid], value_input_option="RAW")
    subs = get_gsubs(gid)
    if tags:
        for sub_uid, sub_name, sub_uname in subs:
            matched = False
            for tag in tags:
                if tag == sub_uid or (sub_uname and tag == sub_uname.lower()) or tag == sub_name.lower():
                    matched = True
                    break
            if matched:
                add_tmember(tid, sub_uid, sub_name, "waiting")
            else:
                add_tmember(tid, sub_uid, sub_name, "skipped")
    else:
        for sub_uid, sub_name, _ in subs:
            add_tmember(tid, sub_uid, sub_name, "waiting")
    members = get_tmembers(tid)
    active = [(u, n, s) for u, n, s in members if s != "skipped"]
    if tags and active:
        names = ", ".join(n for _, n, _ in active)
        sub_line = f"For: {names}"
    else:
        sub_line = f"{len(active)} subscribed" + (f": {', '.join(n for _,n,_ in active)}" if active else "")
    txt = (f"{hdr('Group Reminder')}\n"
           f"{detail(msg, ds, ts, rep)}\n"
           f"By {creator_name}\n\n{sub_line}")
    btns = [[IKB("\uff0b Count Me In", callback_data=f"gjoin_{tid}"),
             IKB("\u2715 Skip", callback_data=f"gskip_{tid}")]]
    if rep == "none":
        btns.append([IKB("\U0001f501 Repeat", callback_data=f"grep_{tid}")])
    if hasattr(target, "reply_text"):
        await target.reply_text(txt, parse_mode="HTML", reply_markup=IKM(btns))
    else:
        await safe_edit(target, txt, IKM(btns))
    ud.clear()

# ============= BUTTON HANDLER =============
async def on_btn(update, ctx):
    q = update.callback_query
    await q.answer()
    d = q.data
    uid = q.from_user.id
    update_username(q.from_user)
    if d == "noop":
        return
    # HOME
    if d == "add":
        ctx.user_data.clear()
        ctx.user_data["step"] = "message"
        sent = await q.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", parse_mode="HTML", reply_markup=cancel_kb())
        store_prompt(ctx.user_data, sent)
        return
    if d == "back_home":
        await rm_old_home(ctx.user_data, ctx.bot)
        sent = await q.message.reply_text(f"{hdr('Smart Reminder Bot')}\n\n{HOME_TEXT}", parse_mode="HTML", reply_markup=home_kb())
        ctx.user_data["h_mid"] = sent.message_id
        ctx.user_data["h_cid"] = sent.chat_id
        return
    if d == "cancel":
        ctx.user_data.clear()
        await safe_edit(q.message, f"{hdr('Cancelled')}", home_kb())
        return
    if d == "gcancel":
        ctx.user_data.clear()
        try:
            await q.message.delete()
        except:
            pass
        return
    # PRIVATE CLOSE/SHOW
    if d == "pclose_list":
        await safe_edit(q.message, "\U0001f4cb Reminders", IKM([[IKB("\U0001f4cb Show", callback_data=f"pshow_list_{q.message.message_id}")]]))
        return
    if d == "pclose_info":
        await safe_edit(q.message, "\u2139\ufe0f Info", IKM([[IKB("\U0001f4cb Show", callback_data=f"pshow_info_{q.message.message_id}")]]))
        return
    if d == "pclose_month":
        tz = get_tz(uid)
        now = datetime.now(tz)
        mn = cal_mod.month_name[now.month]
        await safe_edit(q.message, f"\U0001f4c5 {mn} {now.year}",
            IKM([[IKB("\U0001f4cb Show", callback_data=f"pshow_month_{now.year}_{now.month}_{q.message.message_id}")]]))
        return
    if d.startswith("pshow_list_"):
        await _private_list(q, ctx, new=False)
        return
    if d.startswith("pshow_info_"):
        txt = ctx.bot_data.get(f"pinfo_{d.replace('pshow_info_', '')}", "")
        if not txt:
            txt = "Use /info to see details."
        await safe_edit(q.message, txt, IKM([[IKB("\u2715 Close", callback_data="pclose_info")], [IKB("\uff0b New", callback_data="add")]]))
        return
    if d.startswith("pshow_month_"):
        parts = d.replace("pshow_month_", "").split("_")
        if len(parts) >= 2:
            y, m = int(parts[0]), int(parts[1])
            tz = get_tz(uid)
            txt, kb = _build_month(uid, y, m, tz)
            await safe_edit(q.message, txt, kb)
        return
    # GROUP CLOSE/SHOW
    if d == "gclose":
        mid = q.message.message_id
        gdata = ctx.bot_data.get(f"gmin_{mid}")
        if gdata:
            await safe_edit(q.message, gdata.get("min_text", "Smart Reminder Bot"),
                IKM([[IKB("\U0001f4cb Show", callback_data=gdata.get("show_cb", "noop"))]]))
        else:
            await safe_edit(q.message, "Smart Reminder Bot",
                IKM([[IKB("\U0001f4cb Show", callback_data="noop")]]))
        return
    if d.startswith("gshow_start_"):
        mid = d.replace("gshow_start_", "")
        gdata = ctx.bot_data.get(f"gmin_{mid}")
        if gdata:
            await safe_edit(q.message, gdata["text"], IKM([[IKB("\u2715 Close", callback_data="gclose")]]))
        return
    if d.startswith("gshow_list_"):
        parts = d.replace("gshow_list_", "").split("_")
        if len(parts) >= 2:
            gid = int(parts[0])
            rows = sheet.get_all_values()
            gid_s = str(gid)
            items = []
            for i, r in enumerate(rows[1:], start=2):
                if len(r) <= 7 or r[7] != gid_s:
                    continue
                st = r[5] if len(r) > 5 else ""
                if st in ("active", "pending", "snoozed"):
                    msg, ds, ts, rep, _ = get_detail(r)
                    items.append((i, msg, ds, ts, rep, st))
            if not items:
                await safe_edit(q.message, f"{hdr('Group Reminders')}\n\nNo active reminders.",
                    IKM([[IKB("\u2715 Close", callback_data="gclose")]]))
                return
            lines = [hdr("Group Reminders"), ""]
            for idx, (row, msg, ds, ts, rep, st) in enumerate(items):
                short = msg[:25] + "\u2026" if len(msg) > 25 else msg
                lines.append(f"{idx+1} {ST_IC.get(st, chr(9675))} {short}\n   {fmt_date(ds)} \u00b7 {fmt_time(ts)}")
            btns = []
            rb = []
            for idx, (row, *_) in enumerate(items):
                rb.append(IKB(str(idx+1), callback_data=f"gview_{row}"))
                if len(rb) == 5:
                    btns.append(rb)
                    rb = []
            if rb:
                btns.append(rb)
            btns.append([IKB("\u2715 Close", callback_data="gclose")])
            await safe_edit(q.message, "\n".join(lines), IKM(btns))
        return
    # CALENDAR
    if d.startswith("cal_"):
        parts = d.replace("cal_", "").split("_")
        y, m = int(parts[0]), int(parts[1])
        tz = get_tz(uid)
        step = ctx.user_data.get("step", "")
        back_cb = "gcancel" if step.startswith("g_") else ("cancel" if step != "edit_date" else f"edit_{ctx.user_data.get('editing_row', '')}")
        back_txt = "\u2715 Cancel" if step != "edit_date" else "\u00ab Back"
        await safe_edit(q.message, q.message.text, cal_kb(y, m, tz, back_cb, back_txt))
        return
    if d.startswith("day_"):
        ds = d.replace("day_", "")
        step = ctx.user_data.get("step", "")
        tz = get_tz(uid)
        if step == "edit_date":
            row = ctx.user_data.get("editing_row")
            if row:
                ts_existing = ctx.user_data.get("edit_old_time", "")
                if is_past(ds, ts_existing, tz):
                    now = datetime.now(tz)
                    await safe_edit(q.message, f"{past_msg(ts_existing)}\nChange the time first or pick a future date.",
                        cal_kb(now.year, now.month, tz, f"edit_{row}", "\u00ab Back"))
                    return
                sheet.update_cell(row, 3, ds)
                r = sheet.row_values(row)
                msg, _, ts, rep, st = get_detail(r)
                await safe_edit(q.message, f"{hdr('Updated \u2713')}\n{detail(msg, ds, ts, rep)}", home_kb())
            ctx.user_data.clear()
            return
        if step in ("g_date", "date"):
            ctx.user_data["date"] = ds
            ts = ctx.user_data.get("time")
            if ts:
                if is_past(ds, ts, tz):
                    now = datetime.now(tz)
                    msg_text = ctx.user_data.get("message", "")
                    bcb = "gcancel" if step == "g_date" else "cancel"
                    await safe_edit(q.message, f"{msg_text}\n\n{past_msg(ts)}",
                        cal_kb(now.year, now.month, tz, bcb))
                    return
                if step == "g_date":
                    await _finish_group(q.message, ctx)
                else:
                    await _save_reminder(q.message, ctx)
                return
            ctx.user_data["step"] = "g_time" if step == "g_date" else "time"
            msg_text = ctx.user_data.get("message", "")
            if step == "g_date":
                await safe_edit(q.message, f"{hdr('Group Reminder')}\n{msg_text}\n{fmt_date(ds)}\n\nEnter time:\ne.g. 9pm, 9:30 PM, 21:30",
                    IKM([[IKB("\u2715 Cancel", callback_data="gcancel")]]))
            else:
                sent = await q.message.reply_text(f"{msg_text}\n{fmt_date(ds)}\n\nEnter time:\ne.g. 9pm, 9:30 PM, 21:30", parse_mode="HTML")
                store_prompt(ctx.user_data, sent)
            return
        return
    # REPEAT
    if d.startswith("rep_"):
        val = d.replace("rep_", "")
        if val == "back":
            return
        if val == "custom":
            ctx.user_data["custom_days"] = []
            await safe_edit(q.message, f"{hdr('Select Days')}\nPick days for reminder:",
                IKM(custom_days_kb([], None)))
            return
        ctx.user_data["repeat"] = val
        step = ctx.user_data.get("step", "")
        if step == "g_repeat":
            await _finish_group(q.message, ctx)
        else:
            await _save_reminder(q.message, ctx)
        return
    if d.startswith("cday__"):
        val = d.replace("cday__", "")
        sel = ctx.user_data.get("custom_days", [])
        if val == "mf":
            sel = ["mon", "tue", "wed", "thu", "fri"]
        elif val == "all":
            sel = list(DAY_NAMES)
        elif val == "clr":
            sel = []
        elif val in sel:
            sel.remove(val)
        else:
            sel.append(val)
        ctx.user_data["custom_days"] = sel
        await safe_edit(q.message, f"{hdr('Select Days')}\nPick days for reminder:",
            IKM(custom_days_kb(sel, None)))
        return
    if d == "csave_":
        sel = ctx.user_data.get("custom_days", [])
        if sel:
            ordered = [dn for dn in DAY_NAMES if dn in sel]
            ctx.user_data["repeat"] = "custom:" + ",".join(ordered)
            step = ctx.user_data.get("step", "")
            if step == "g_repeat":
                await _finish_group(q.message, ctx)
            else:
                await _save_reminder(q.message, ctx)
        return
    # CHANGE REPEAT (after save)
    if d.startswith("chrep_"):
        parts = d.replace("chrep_", "").split("_", 1)
        row = int(parts[0])
        val = parts[1] if len(parts) > 1 else ""
        if val == "back":
            ctx.user_data.pop("custom_days", None)
            r = sheet.row_values(row)
            msg, ds, ts, rep, st = get_detail(r)
            await safe_edit(q.message, f"{hdr('Saved \u2713')}\n{detail(msg, ds, ts, rep)}",
                IKM([[IKB("\U0001f501 Repeat", callback_data=f"grep_{row}"),
                      IKB("\u270e Edit", callback_data=f"edit_{row}"),
                      IKB("\uff0b New", callback_data="add")]]))
            return
        if val == "custom":
            ctx.user_data["custom_days"] = []
            ctx.user_data["chrep_row"] = row
            await safe_edit(q.message, f"{hdr('Select Days')}\nPick days for reminder:",
                IKM(custom_days_kb([], row)))
            return
        sheet.update_cell(row, 5, val)
        r = sheet.row_values(row)
        msg, ds, ts, rep, st = get_detail(r)
        await safe_edit(q.message, f"{hdr('Updated \u2713')}\n{detail(msg, ds, ts, rep)}", home_kb())
        return
    if d.startswith("cday_") and not d.startswith("cday__"):
        parts = d.replace("cday_", "").split("_", 1)
        row = int(parts[0]) if parts[0] else None
        val = parts[1] if len(parts) > 1 else ""
        sel = ctx.user_data.get("custom_days", [])
        if val == "mf":
            sel = ["mon", "tue", "wed", "thu", "fri"]
        elif val == "all":
            sel = list(DAY_NAMES)
        elif val == "clr":
            sel = []
        elif val in sel:
            sel.remove(val)
        else:
            sel.append(val)
        ctx.user_data["custom_days"] = sel
        await safe_edit(q.message, f"{hdr('Select Days')}\nPick days for reminder:",
            IKM(custom_days_kb(sel, row)))
        return
    if d.startswith("csave_") and d != "csave_":
        row = int(d.replace("csave_", ""))
        sel = ctx.user_data.get("custom_days", [])
        if sel:
            ordered = [dn for dn in DAY_NAMES if dn in sel]
            rep_val = "custom:" + ",".join(ordered)
            sheet.update_cell(row, 5, rep_val)
            r = sheet.row_values(row)
            msg, ds, ts, rep, st = get_detail(r)
            await safe_edit(q.message, f"{hdr('Updated \u2713')}\n{detail(msg, ds, ts, rep)}", home_kb())
        ctx.user_data.pop("custom_days", None)
        return
    if d.startswith("grep_"):
        val = d.replace("grep_", "")
        try:
            row = int(val)
            r = sheet.row_values(row)
            msg, ds, ts, rep, st = get_detail(r)
            await safe_edit(q.message, f"{detail(msg, ds, ts, None)}\n\nRepeat?", repeat_kb(row))
        except:
            pass
        return
    # SNOOZE
    if d.startswith("snzp_"):
        row = int(d.replace("snzp_", ""))
        r = sheet.row_values(row)
        if len(r) > 5 and r[5] not in ("pending", "snoozed"):
            await safe_edit(q.message, f"{hdr('Already handled')}", home_kb())
            return
        await safe_edit(q.message, q.message.text, snz_kb(row))
        return
    if d.startswith("snzb_"):
        row = int(d.replace("snzb_", ""))
        await safe_edit(q.message, q.message.text, reminder_kb(row))
        return
    if d.startswith("snz_"):
        parts = d.replace("snz_", "").split("_")
        row, mins = int(parts[0]), int(parts[1])
        r = sheet.row_values(row)
        if len(r) > 5 and r[5] not in ("pending", "snoozed"):
            await safe_edit(q.message, f"{hdr('Already handled')}", home_kb())
            return
        cancel_jobs(ctx, f"retry-{row}")
        tz = get_tz(uid)
        now = datetime.now(tz)
        snz_time = now + timedelta(minutes=mins)
        msg, ds, ts, rep, st = get_detail(r)
        gid = r[7] if len(r) > 7 else ""
        tid = r[8] if len(r) > 8 else ""
        if rep and rep != "none" and not gid:
            sheet.update_cell(row, 6, "snoozed")
            sheet.update_cell(row, 7, 0)
            ctx.job_queue.run_once(snooze_fire, mins * 60,
                data={"row": row, "chat": uid, "uid": uid}, name=f"snz-{row}")
        else:
            sheet.update_cell(row, 3, snz_time.strftime("%Y-%m-%d"))
            sheet.update_cell(row, 4, snz_time.strftime("%H:%M"))
            sheet.update_cell(row, 6, "active")
            sheet.update_cell(row, 7, 0)
        if tid and gid:
            set_tstatus(tid, uid, "snoozed")
        label = f"{mins}m" if mins < 60 else f"{mins//60}h"
        await safe_edit(q.message,
            f"{detail(msg, ds, ts, rep)}\n\n<b>Snoozed {label}</b> \u2192 {fmt_time(snz_time.strftime('%H:%M'))}",
            home_kb())
        return
    # DONE
    if d.startswith("done_"):
        row = int(d.replace("done_", ""))
        r = sheet.row_values(row)
        if len(r) > 5 and r[5] not in ("pending", "snoozed"):
            await safe_edit(q.message, f"{hdr('Already handled')}", home_kb())
            return
        cancel_jobs(ctx, f"retry-{row}")
        msg, ds, ts, rep, st = get_detail(r)
        gid = r[7] if len(r) > 7 else ""
        tid = r[8] if len(r) > 8 else ""
        if rep and rep != "none":
            advance_rep(row, rep, ds, ts)
        else:
            sheet.update_cell(row, 6, "done")
        sheet.update_cell(row, 7, 0)
        if tid and gid:
            set_tstatus(tid, uid, "done")
            await _update_group_status(ctx, gid, tid, r)
        await safe_edit(q.message, f"{detail(msg, ds, ts, rep)}\n\n<b>Done \u2713</b>", home_kb())
        return
    # UNDO CANCEL
    if d.startswith("undo_"):
        row = int(d.replace("undo_", ""))
        r = sheet.row_values(row)
        if r:
            sheet.update_cell(row, 6, "active")
            msg, ds, ts, rep, _ = get_detail(r)
            await safe_edit(q.message, f"{hdr('Restored \u2713')}\n{detail(msg, ds, ts, rep)}", home_kb())
        return
    # VIEW (from list)
    if d.startswith("view_"):
        row = int(d.replace("view_", ""))
        r = sheet.row_values(row)
        if not r:
            await safe_edit(q.message, "Not found.", home_kb())
            return
        msg, ds, ts, rep, st = get_detail(r)
        ic = ST_IC.get(st, "\u25cb")
        lb = ST_LB.get(st, st)
        txt = f"{hdr('Reminder')}\n{msg}\n\n{fmt_date(ds)} \u00b7 {fmt_time(ts)}\n{rep_label(rep)} \u00b7 {ic} {lb}"
        btns = []
        if st in ("active", "pending", "snoozed"):
            btns.append([IKB("\u270e Edit", callback_data=f"edit_{row}"), IKB("\u2715 Cancel", callback_data=f"crem_{row}")])
        elif st == "missed":
            btns.append([IKB("\u2715 Remove", callback_data=f"crem_{row}")])
        btns.append([IKB("\u00ab Back", callback_data="back_list")])
        await safe_edit(q.message, txt, IKM(btns))
        return
    if d == "back_list":
        await _private_list(q, ctx, new=False)
        return
    # EDIT
    if d.startswith("edit_"):
        row = int(d.replace("edit_", ""))
        r = sheet.row_values(row)
        if not r:
            return
        msg, ds, ts, rep, st = get_detail(r)
        txt = f"{hdr('Edit Reminder')}\n{detail(msg, ds, ts, rep)}\n\nWhat to change?"
        btns = [
            [IKB("Message", callback_data=f"emsg_{row}"), IKB("Date", callback_data=f"edate_{row}"),
             IKB("Time", callback_data=f"etime_{row}")],
            [IKB("\u00ab Back", callback_data=f"view_{row}")]
        ]
        await safe_edit(q.message, txt, IKM(btns))
        return
    if d.startswith("emsg_"):
        row = int(d.replace("emsg_", ""))
        r = sheet.row_values(row)
        msg, ds, ts, rep, st = get_detail(r)
        ctx.user_data["step"] = "edit_message"
        ctx.user_data["editing_row"] = row
        sent = await q.message.reply_text(
            f"{detail(msg, ds, ts, rep)}\n\nEnter new message:", parse_mode="HTML",
            reply_markup=IKM([[IKB("\u00ab Back", callback_data=f"edit_{row}")]]))
        store_prompt(ctx.user_data, sent)
        return
    if d.startswith("edate_"):
        row = int(d.replace("edate_", ""))
        r = sheet.row_values(row)
        msg, ds, ts, rep, st = get_detail(r)
        ctx.user_data["step"] = "edit_date"
        ctx.user_data["editing_row"] = row
        ctx.user_data["edit_old_time"] = ts
        tz = get_tz(uid)
        now = datetime.now(tz)
        await safe_edit(q.message, f"{detail(msg, ds, ts, rep)}\n\nPick new date:",
            cal_kb(now.year, now.month, tz, f"edit_{row}", "\u00ab Back"))
        return
    if d.startswith("etime_"):
        row = int(d.replace("etime_", ""))
        r = sheet.row_values(row)
        msg, ds, ts, rep, st = get_detail(r)
        ctx.user_data["step"] = "edit_time"
        ctx.user_data["editing_row"] = row
        ctx.user_data["edit_old_date"] = ds
        sent = await q.message.reply_text(
            f"{detail(msg, ds, ts, rep)}\n\nEnter new time:\ne.g. 9pm, 9:30 PM, 21:30",
            parse_mode="HTML", reply_markup=IKM([[IKB("\u00ab Back", callback_data=f"edit_{row}")]]))
        store_prompt(ctx.user_data, sent)
        return
    # CANCEL REMINDER
    if d.startswith("crem_"):
        row = int(d.replace("crem_", ""))
        cancel_jobs(ctx, f"retry-{row}")
        r = sheet.row_values(row)
        msg, ds, ts, rep, st = get_detail(r)
        sheet.update_cell(row, 6, "cancelled")
        sheet.update_cell(row, 7, 0)
        await safe_edit(q.message, f"{detail(msg, ds, ts, rep)}\n\n<b>Cancelled \u2718</b>",
            IKM([[IKB("\u21a9 Undo", callback_data=f"undo_{row}"), IKB("\uff0b New", callback_data="add")]]))
        return
    # GROUP BUTTONS
    if d.startswith("gjoin_"):
        tid = d.replace("gjoin_", "")
        name = q.from_user.first_name or "User"
        uname = q.from_user.username or ""
        gid = q.message.chat_id
        set_gsub(gid, uid, name, uname, True)
        members = get_tmembers(tid)
        uid_s = str(uid)
        found = False
        for mu, mn, ms in members:
            if mu == uid_s:
                found = True
                if ms == "skipped":
                    set_tstatus(tid, uid, "waiting")
                break
        if not found:
            add_tmember(tid, uid, name, "waiting")
        members = get_tmembers(tid)
        active = [(u, n, s) for u, n, s in members if s != "skipped"]
        rows_all = sheet.get_all_values()
        rem_row = None
        for i, r in enumerate(rows_all[1:], start=2):
            if len(r) > 8 and r[8] == tid:
                rem_row = r
                break
        if rem_row:
            msg, ds, ts, rep, st = get_detail(rem_row)
            creator_name = ""
            for mu, mn, ms in members:
                if mu == rem_row[0]:
                    creator_name = mn
                    break
            sub_line = f"{len(active)} subscribed" + (f": {', '.join(n for _,n,_ in active)}" if active else "")
            txt = (f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, rep)}\n"
                   f"By {creator_name}\n\n{sub_line}")
            btns = [[IKB("\uff0b Count Me In", callback_data=f"gjoin_{tid}"),
                     IKB("\u2715 Skip", callback_data=f"gskip_{tid}")]]
            await safe_edit(q.message, txt, IKM(btns))
        return
    if d.startswith("gskip_"):
        tid = d.replace("gskip_", "")
        uid_s = str(uid)
        uname = q.from_user.username or ""
        gid = q.message.chat_id
        if uname:
            set_gsub(gid, uid, q.from_user.first_name or "User", uname, True)
        members = get_tmembers(tid)
        found = False
        for mu, mn, ms in members:
            if mu == uid_s:
                found = True
                set_tstatus(tid, uid, "skipped")
                break
        members = get_tmembers(tid)
        active = [(u, n, s) for u, n, s in members if s != "skipped"]
        rows_all = sheet.get_all_values()
        rem_row = None
        for i, r in enumerate(rows_all[1:], start=2):
            if len(r) > 8 and r[8] == tid:
                rem_row = r
                break
        if rem_row:
            msg, ds, ts, rep, st = get_detail(rem_row)
            sub_line = f"{len(active)} subscribed" + (f": {', '.join(n for _,n,_ in active)}" if active else "")
            txt = (f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, rep)}\nBy ...\n\n{sub_line}")
            btns = [[IKB("\uff0b Count Me In", callback_data=f"gjoin_{tid}"),
                     IKB("\u2715 Skip", callback_data=f"gskip_{tid}")]]
            await safe_edit(q.message, txt, IKM(btns))
        return
    # MONTH
    if d.startswith("mw_"):
        parts = d.replace("mw_", "").split("_")
        y, m, wi = int(parts[0]), int(parts[1]), int(parts[2])
        tz = get_tz(uid)
        txt, kb = _build_week(uid, y, m, wi, tz)
        await safe_edit(q.message, txt, kb)
        return
    if d.startswith("mn_"):
        parts = d.replace("mn_", "").split("_")
        y, m = int(parts[0]), int(parts[1])
        tz = get_tz(uid)
        txt, kb = _build_month(uid, y, m, tz)
        await safe_edit(q.message, txt, kb)
        return
    # SETTINGS
    if d.startswith("cfg_"):
        await _btn_cfg(q, ctx, d)
        return

async def _btn_cfg(q, ctx, d):
    uid = q.from_user.id
    cfg = get_cfg(uid)
    if d == "cfg_digest":
        cur = cfg.get("digest_on", "true")
        new_val = "false" if cur == "true" else "true"
        save_cfg(uid, "digest_on", new_val)
        cfg["digest_on"] = new_val
        await _show_settings(q.message, cfg, new=False)
    elif d == "cfg_digtime":
        ctx.user_data["step"] = "cfg_digtime"
        await q.message.reply_text("Enter digest time:\ne.g. 7am, 8:30 PM, 06:00", parse_mode="HTML")
    elif d == "cfg_retries":
        btns = []
        rb = []
        for v in [1, 2, 3, 5, 7, 10]:
            rb.append(IKB(f"{v}\u00d7", callback_data=f"cfg_rval_{v}"))
            if len(rb) == 3:
                btns.append(rb)
                rb = []
        if rb:
            btns.append(rb)
        btns.append([IKB("\u00ab Back", callback_data="cfg_back")])
        await safe_edit(q.message, f"{hdr('Max Retries')}\nHow many retry notifications?", IKM(btns))
    elif d.startswith("cfg_rval_"):
        val = d.replace("cfg_rval_", "")
        save_cfg(uid, "max_retries", val)
        cfg["max_retries"] = val
        await _show_settings(q.message, cfg, new=False)
    elif d == "cfg_gap":
        btns = []
        rb = []
        for v in [5, 10, 15, 20, 30, 60]:
            rb.append(IKB(f"{v}m", callback_data=f"cfg_gval_{v}"))
            if len(rb) == 3:
                btns.append(rb)
                rb = []
        if rb:
            btns.append(rb)
        btns.append([IKB("\u00ab Back", callback_data="cfg_back")])
        await safe_edit(q.message, f"{hdr('Retry Gap')}\nMinutes between retries?", IKM(btns))
    elif d.startswith("cfg_gval_"):
        val = d.replace("cfg_gval_", "")
        save_cfg(uid, "retry_gap", val)
        cfg["retry_gap"] = val
        await _show_settings(q.message, cfg, new=False)
    elif d == "cfg_tz":
        btns = [[IKB(f"{TZ_ICONS.get(r, '')} {r}", callback_data=f"cfg_tzr_{r}")] for r in TZ_REGIONS]
        btns.append([IKB("\u00ab Back", callback_data="cfg_back")])
        await safe_edit(q.message, f"{hdr('Timezone')}\nPick your region:", IKM(btns))
    elif d.startswith("cfg_tzr_"):
        region = d.replace("cfg_tzr_", "")
        tzs = TZ_DATA.get(region, [])
        cur_tz = cfg.get("timezone", DEF_TZ)
        btns = []
        for label, tzn in tzs:
            dt = datetime.now(pytz.timezone(tzn))
            off = dt.strftime("%z")
            o = f"{off[:3]}:{off[3:]}"
            mark = " \u2713" if tzn == cur_tz else ""
            btns.append([IKB(f"{label} {o}{mark}", callback_data=f"cfg_tzs_{tzn}")])
        btns.append([IKB("\u00ab Regions", callback_data="cfg_tz")])
        await safe_edit(q.message, f"{hdr('Timezone')}\n{region}:", IKM(btns))
    elif d.startswith("cfg_tzs_"):
        tzn = d.replace("cfg_tzs_", "")
        save_cfg(uid, "timezone", tzn)
        cfg["timezone"] = tzn
        await _show_settings(q.message, cfg, new=False)
    elif d == "cfg_weekly":
        cur = cfg.get("weekly_report", "true")
        new_val = "false" if cur == "true" else "true"
        save_cfg(uid, "weekly_report", new_val)
        cfg["weekly_report"] = new_val
        await _show_settings(q.message, cfg, new=False)
    elif d == "cfg_back":
        await _show_settings(q.message, cfg, new=False)

# ============= SAVE REMINDER =============
async def _save_reminder(target, ctx):
    ud = ctx.user_data
    msg = ud.get("message", "")
    ds = ud.get("date", "")
    ts = ud.get("time", "")
    rep = ud.get("repeat", "none")
    uid = ud.get("uid", "")
    if not uid and hasattr(target, "chat"):
        uid = target.chat.id
    rows = sheet.get_all_values()
    row_num = len(rows) + 1
    sheet.append_row([str(uid), msg, ds, ts, rep, "active", 0, "", ""], value_input_option="RAW")
    txt = f"{hdr('Saved \u2713')}\n{detail(msg, ds, ts, rep)}"
    btns = []
    if rep == "none":
        btns.append([IKB("\U0001f501 Repeat", callback_data=f"grep_{row_num}"),
                     IKB("\u270e Edit", callback_data=f"edit_{row_num}"),
                     IKB("\uff0b New", callback_data="add")])
    else:
        btns.append([IKB("\u270e Edit", callback_data=f"edit_{row_num}"),
                     IKB("\uff0b New", callback_data="add")])
    if hasattr(target, "reply_text") and callable(target.reply_text):
        await target.reply_text(txt, parse_mode="HTML", reply_markup=IKM(btns))
    else:
        await safe_edit(target, txt, IKM(btns))
    ud.clear()

# ============= TEXT HANDLER =============
async def on_text(update, ctx):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    uid = update.effective_user.id
    update_username(update.effective_user)
    chat_type = update.effective_chat.type
    step = ctx.user_data.get("step", "")
    # Group text steps
    if chat_type != "private":
        if step == "g_message":
            g_chat = ctx.user_data.get("g_chat")
            if g_chat and g_chat == update.effective_chat.id:
                tz = get_tz(uid)
                parsed = parse_nl_partial(text, tz)
                if parsed and parsed.get("message"):
                    ctx.user_data["message"] = parsed["message"]
                    if parsed.get("date"):
                        ctx.user_data["date"] = parsed["date"]
                    if parsed.get("time"):
                        ctx.user_data["time"] = parsed["time"]
                    if parsed.get("repeat"):
                        ctx.user_data["repeat"] = parsed["repeat"]
                    d_val = ctx.user_data.get("date")
                    t_val = ctx.user_data.get("time")
                    if d_val and t_val:
                        if is_past(d_val, t_val, tz):
                            now = datetime.now(tz)
                            ctx.user_data["step"] = "g_date"
                            await update.message.reply_text(
                                f"{parsed['message']}\n\n{past_msg(t_val)}",
                                parse_mode="HTML",
                                reply_markup=cal_kb(now.year, now.month, tz, "gcancel"))
                            return
                        await _finish_group(update.message, ctx)
                        return
                    if t_val and not d_val:
                        now2 = datetime.now(tz)
                        td = now2.strftime("%Y-%m-%d")
                        if not is_past(td, t_val, tz):
                            ctx.user_data["date"] = td
                            await _finish_group(update.message, ctx)
                            return
                        ctx.user_data["step"] = "g_date"
                        await update.message.reply_text(
                            f"{parsed['message']}\n{fmt_time(t_val)}\n\n{past_msg(t_val)}",
                            parse_mode="HTML",
                            reply_markup=cal_kb(now2.year, now2.month, tz, "gcancel"))
                        return
                    if d_val:
                        ctx.user_data["step"] = "g_time"
                        await update.message.reply_text(
                            f"{hdr('Group Reminder')}\n{parsed['message']}\n{fmt_date(d_val)}\n\nEnter time:",
                            parse_mode="HTML", reply_markup=ForceReply(selective=True))
                        return
                    ctx.user_data["message"] = parsed["message"]
                else:
                    ctx.user_data["message"] = text
                ctx.user_data["step"] = "g_date"
                now3 = datetime.now(tz)
                await update.message.reply_text(
                    f"{hdr('Group Reminder')}\n{ctx.user_data['message']}\n\nPick a date:",
                    parse_mode="HTML",
                    reply_markup=cal_kb(now3.year, now3.month, tz, "gcancel"))
            return
        if step == "g_time":
            g_chat = ctx.user_data.get("g_chat")
            if g_chat and g_chat == update.effective_chat.id:
                t = parse_time(text)
                if not t:
                    await update.message.reply_text("Invalid time. Try: 9pm, 9:30 PM, 21:30",
                        reply_markup=ForceReply(selective=True))
                    return
                tz = get_tz(uid)
                ds = ctx.user_data.get("date", "")
                if ds and is_past(ds, t, tz):
                    await update.message.reply_text(past_msg(t), parse_mode="HTML",
                        reply_markup=ForceReply(selective=True))
                    return
                ctx.user_data["time"] = t
                await _finish_group(update.message, ctx)
            return
        return
    # Private steps
    if step == "message":
        await rm_prompt(ctx.user_data, ctx.bot)
        tz = get_tz(uid)
        ctx.user_data["uid"] = uid
        parsed = parse_nl_partial(text, tz)
        if parsed and parsed.get("message"):
            ctx.user_data["message"] = parsed["message"]
            if parsed.get("date"):
                ctx.user_data["date"] = parsed["date"]
            if parsed.get("time"):
                ctx.user_data["time"] = parsed["time"]
            if parsed.get("repeat"):
                ctx.user_data["repeat"] = parsed["repeat"]
            d_val = ctx.user_data.get("date")
            t_val = ctx.user_data.get("time")
            if d_val and t_val:
                if is_past(d_val, t_val, tz):
                    now = datetime.now(tz)
                    ctx.user_data["step"] = "date"
                    sent = await update.message.reply_text(
                        f"{parsed['message']}\n\n{past_msg(t_val)}",
                        parse_mode="HTML",
                        reply_markup=cal_kb(now.year, now.month, tz))
                    return
                await _save_reminder(update.message, ctx)
                return
            if t_val and not d_val:
                now2 = datetime.now(tz)
                td = now2.strftime("%Y-%m-%d")
                if not is_past(td, t_val, tz):
                    ctx.user_data["date"] = td
                    await _save_reminder(update.message, ctx)
                    return
                ctx.user_data["step"] = "date"
                sent = await update.message.reply_text(
                    f"{parsed['message']}\n{fmt_time(t_val)}\n\n{past_msg(t_val)}",
                    parse_mode="HTML",
                    reply_markup=cal_kb(now2.year, now2.month, tz))
                return
            if d_val:
                ctx.user_data["step"] = "time"
                sent = await update.message.reply_text(
                    f"{parsed['message']}\n{fmt_date(d_val)}\n\nEnter time:\ne.g. 9pm, 9:30 PM, 21:30",
                    parse_mode="HTML")
                store_prompt(ctx.user_data, sent)
                return
            ctx.user_data["message"] = parsed["message"]
        else:
            ctx.user_data["message"] = text
        ctx.user_data["step"] = "date"
        now3 = datetime.now(tz)
        sent = await update.message.reply_text(
            f"{ctx.user_data['message']}\n\nPick a date:",
            parse_mode="HTML",
            reply_markup=cal_kb(now3.year, now3.month, tz))
        return
    if step == "time":
        await del_prompt(ctx.user_data, ctx.bot)
        t = parse_time(text)
        if not t:
            sent = await update.message.reply_text("Invalid time. Try: 9pm, 9:30 PM, 21:30", parse_mode="HTML")
            store_prompt(ctx.user_data, sent)
            return
        tz = get_tz(uid)
        ds = ctx.user_data.get("date", "")
        if ds and is_past(ds, t, tz):
            sent = await update.message.reply_text(past_msg(t), parse_mode="HTML")
            store_prompt(ctx.user_data, sent)
            return
        ctx.user_data["time"] = t
        ctx.user_data["uid"] = uid
        await _save_reminder(update.message, ctx)
        return
    if step == "edit_message":
        await rm_prompt(ctx.user_data, ctx.bot)
        row = ctx.user_data.get("editing_row")
        if row:
            sheet.update_cell(row, 2, text)
            r = sheet.row_values(row)
            msg, ds, ts, rep, st = get_detail(r)
            await update.message.reply_text(f"{hdr('Updated \u2713')}\n{detail(msg, ds, ts, rep)}", parse_mode="HTML", reply_markup=home_kb())
        ctx.user_data.clear()
        return
    if step == "edit_time":
        await rm_prompt(ctx.user_data, ctx.bot)
        t = parse_time(text)
        if not t:
            sent = await update.message.reply_text("Invalid time. Try: 9pm, 9:30 PM, 21:30", parse_mode="HTML")
            store_prompt(ctx.user_data, sent)
            return
        row = ctx.user_data.get("editing_row")
        ds = ctx.user_data.get("edit_old_date", "")
        tz = get_tz(uid)
        if ds and is_past(ds, t, tz):
            sent = await update.message.reply_text(past_msg(t), parse_mode="HTML")
            store_prompt(ctx.user_data, sent)
            return
        if row:
            sheet.update_cell(row, 4, t)
            r = sheet.row_values(row)
            msg, _, ts, rep, st = get_detail(r)
            await update.message.reply_text(f"{hdr('Updated \u2713')}\n{detail(msg, ds, t, rep)}", parse_mode="HTML", reply_markup=home_kb())
        ctx.user_data.clear()
        return
    if step == "cfg_digtime":
        t = parse_time(text)
        if not t:
            await update.message.reply_text("Invalid time. Try: 7am, 8:30 PM, 06:00")
            return
        save_cfg(uid, "digest_time", t)
        cfg = get_cfg(uid)
        ctx.user_data.clear()
        await update.message.reply_text(f"Digest time set to {fmt_time(t)}", parse_mode="HTML")
        return
    # NL FALLBACK (no active step)
    if not step:
        tz = get_tz(uid)
        parsed = parse_nl_partial(text, tz)
        if not parsed or not parsed.get("message"):
            return
        ctx.user_data["uid"] = uid
        ctx.user_data["message"] = parsed["message"]
        if parsed.get("repeat"):
            ctx.user_data["repeat"] = parsed["repeat"]
        d_val = parsed.get("date")
        t_val = parsed.get("time")
        if d_val and t_val:
            ctx.user_data["date"] = d_val
            ctx.user_data["time"] = t_val
            if is_past(d_val, t_val, tz):
                now = datetime.now(tz)
                ctx.user_data["step"] = "date"
                sent = await update.message.reply_text(
                    f"{parsed['message']}\n\n{past_msg(t_val)}",
                    parse_mode="HTML",
                    reply_markup=cal_kb(now.year, now.month, tz))
                return
            await _save_reminder(update.message, ctx)
            return
        if t_val:
            ctx.user_data["time"] = t_val
            now2 = datetime.now(tz)
            td = now2.strftime("%Y-%m-%d")
            if not is_past(td, t_val, tz):
                ctx.user_data["date"] = td
                await _save_reminder(update.message, ctx)
                return
            ctx.user_data["step"] = "date"
            sent = await update.message.reply_text(
                f"{parsed['message']}\n{fmt_time(t_val)}\n\n{past_msg(t_val)}",
                parse_mode="HTML",
                reply_markup=cal_kb(now2.year, now2.month, tz))
            return
        if d_val:
            ctx.user_data["date"] = d_val
            ctx.user_data["step"] = "time"
            sent = await update.message.reply_text(
                f"{parsed['message']}\n{fmt_date(d_val)}\n\nEnter time:\ne.g. 9pm, 9:30 PM, 21:30",
                parse_mode="HTML")
            store_prompt(ctx.user_data, sent)
            return
        ctx.user_data["step"] = "date"
        now3 = datetime.now(tz)
        sent = await update.message.reply_text(
            f"{parsed['message']}\n\nPick a date:",
            parse_mode="HTML",
            reply_markup=cal_kb(now3.year, now3.month, tz))
        return

# ============= CANCEL JOBS =============
def cancel_jobs(ctx, name):
    jobs = ctx.job_queue.get_jobs_by_name(name)
    for j in jobs:
        j.schedule_removal()

# ============= SNOOZE FIRE =============
async def snooze_fire(ctx):
    d = ctx.job.data
    row, chat, uid_val = d["row"], d["chat"], d.get("uid", d["chat"])
    r = sheet.row_values(row)
    if not r or (len(r) > 5 and r[5] not in ("snoozed", "active")):
        return
    msg, ds, ts, rep, st = get_detail(r)
    sheet.update_cell(row, 6, "pending")
    sheet.update_cell(row, 7, 0)
    txt = f"\u23f0 {msg}"
    try:
        sent = await ctx.bot.send_message(chat_id=chat, text=txt, reply_markup=reminder_kb(row), parse_mode="HTML")
        ctx.bot_data[f"rmsg_{row}"] = {"mid": sent.message_id, "cid": chat}
    except:
        pass
    gap = get_user_gap(uid_val)
    ctx.job_queue.run_once(auto_retry, gap, data={"row": row, "chat": chat, "uid": uid_val, "count": 0}, name=f"retry-{row}")

# ============= AUTO RETRY =============
async def auto_retry(ctx):
    d = ctx.job.data
    row, chat, uid_val = d["row"], d["chat"], d.get("uid", d["chat"])
    count = d.get("count", 0)
    r = sheet.row_values(row)
    if not r:
        return
    st = r[5] if len(r) > 5 else ""
    if st not in ("pending",):
        return
    max_ret = get_user_retries(uid_val)
    if count >= max_ret:
        rep = r[4] if len(r) > 4 else "none"
        ds = norm_date(r[2]) if len(r) > 2 else ""
        ts = norm_time(r[3]) if len(r) > 3 else ""
        if rep and rep != "none":
            advance_rep(row, rep, ds, ts)
        else:
            sheet.update_cell(row, 6, "missed")
        sheet.update_cell(row, 7, 0)
        gid = r[7] if len(r) > 7 else ""
        tid = r[8] if len(r) > 8 else ""
        if tid and gid:
            set_tstatus(tid, str(uid_val), "missed")
            await _update_group_status(ctx, gid, tid, r)
        return
    msg = r[1] if len(r) > 1 else ""
    old = ctx.bot_data.get(f"rmsg_{row}")
    if old:
        try:
            await ctx.bot.edit_message_reply_markup(old["cid"], old["mid"], reply_markup=None)
        except:
            pass
    txt = f"\U0001f514 {msg}\n<i>Retry {count+1}/{get_user_retries(uid_val)}</i>"
    try:
        sent = await ctx.bot.send_message(chat_id=chat, text=txt, reply_markup=reminder_kb(row), parse_mode="HTML")
        ctx.bot_data[f"rmsg_{row}"] = {"mid": sent.message_id, "cid": chat}
    except:
        pass
    sheet.update_cell(row, 7, count + 1)
    gap = get_user_gap(uid_val)
    ctx.job_queue.run_once(auto_retry, gap, data={"row": row, "chat": chat, "uid": uid_val, "count": count + 1}, name=f"retry-{row}")

# ============= GROUP STATUS UPDATE =============
async def _update_group_status(ctx, gid, tid, rem_row):
    members = get_tmembers(tid)
    active = [(u, n, s) for u, n, s in members if s != "skipped"]
    if not active:
        return
    all_done = all(s in ("done", "missed") for _, _, s in active)
    msg = rem_row[1] if len(rem_row) > 1 else ""
    if all_done:
        all_really_done = all(s == "done" for _, _, s in active)
        if all_really_done:
            txt = f"{msg} \u00b7 \u2705 All done\n{', '.join(n for _, n, _ in active)}"
        else:
            parts = []
            for u, n, s in active:
                parts.append(f"{GT_IC.get(s, chr(9203))} {n}")
            txt = f"{msg}\n\n{' \u00b7 '.join(parts)}"
    else:
        parts = []
        for u, n, s in active:
            parts.append(f"{GT_IC.get(s, chr(9203))} {n}")
        txt = f"\u23f0 {msg}\n\n{' \u00b7 '.join(parts)}"
    gmsg = ctx.bot_data.get(f"gmsg_{tid}")
    if gmsg:
        try:
            await ctx.bot.edit_message_text(chat_id=gmsg["cid"], message_id=gmsg["mid"], text=txt, parse_mode="HTML")
        except:
            pass

# ============= SCHEDULER =============
async def check_reminders(ctx):
    try:
        client.login()
    except:
        pass
    now_utc = datetime.now(pytz.utc)
    try:
        cfg_rows = cfg_sheet.get_all_values()
    except:
        try:
            client.login()
            cfg_rows = cfg_sheet.get_all_values()
        except:
            return
    tz_map = {}
    cfg_map = {}
    for r in cfg_rows[1:]:
        if not r:
            continue
        u = r[0]
        tzn = r[5] if len(r) > 5 else DEF_TZ
        tz_map[u] = safe_tz(tzn)
        cfg_map[u] = {"max_retries": int(r[3]) if len(r) > 3 and r[3].isdigit() else DEF_RETRIES,
                       "retry_gap": int(r[4]) if len(r) > 4 and r[4].isdigit() else DEF_RETRY_GAP}
    try:
        rows = sheet.get_all_values()
    except:
        return
    for i, r in enumerate(rows[1:], start=2):
        if not r or len(r) < 6:
            continue
        st = r[5]
        if st != "active":
            continue
        uid_s = r[0]
        ds = norm_date(r[2])
        ts = norm_time(r[3])
        rep = r[4]
        gid = r[7] if len(r) > 7 else ""
        tid = r[8] if len(r) > 8 else ""
        user_tz = tz_map.get(uid_s, safe_tz(DEF_TZ))
        now_local = now_utc.astimezone(user_tz)
        now_str = now_local.strftime("%Y-%m-%d %H:%M")
        rem_str = f"{ds} {ts}"
        if rem_str != now_str:
            continue
        if rep and rep.startswith("custom:") and not is_custom_match(rep, now_local):
            continue
        cancel_jobs(ctx, f"retry-{i}")
        sheet.update_cell(i, 6, "pending")
        sheet.update_cell(i, 7, 0)
        msg = r[1]
        if gid and tid:
            members = get_tmembers(tid)
            active = [(u, n, s) for u, n, s in members if s in ("waiting",)]
            for mu, mn, ms in active:
                set_tstatus(tid, mu, "pending")
            status_parts = []
            for u, n, s in get_tmembers(tid):
                if s != "skipped":
                    status_parts.append(f"{GT_IC.get('pending', chr(9203))} {n}")
            status_txt = f"\u23f0 {msg}\n\n{' \u00b7 '.join(status_parts)}"
            try:
                sent = await ctx.bot.send_message(chat_id=int(gid), text=status_txt, parse_mode="HTML")
                ctx.bot_data[f"gmsg_{tid}"] = {"mid": sent.message_id, "cid": int(gid)}
            except:
                pass
            for mu, mn, ms in active:
                try:
                    s = await ctx.bot.send_message(chat_id=int(mu), text=f"\u23f0 {msg}\nFrom group",
                        reply_markup=reminder_kb(i), parse_mode="HTML")
                    ctx.bot_data[f"rmsg_{i}_{mu}"] = {"mid": s.message_id, "cid": int(mu)}
                except:
                    pass
            c = cfg_map.get(uid_s, {"retry_gap": DEF_RETRY_GAP})
            gap = c["retry_gap"] * 60
            ctx.job_queue.run_once(grp_retry, gap, data={"row": i, "tid": tid, "gid": gid, "uid": uid_s, "count": 0}, name=f"retry-{i}")
        else:
            try:
                sent = await ctx.bot.send_message(chat_id=int(uid_s), text=f"\u23f0 {msg}",
                    reply_markup=reminder_kb(i), parse_mode="HTML")
                ctx.bot_data[f"rmsg_{i}"] = {"mid": sent.message_id, "cid": int(uid_s)}
            except:
                pass
            c = cfg_map.get(uid_s, {"retry_gap": DEF_RETRY_GAP})
            gap = c["retry_gap"] * 60
            ctx.job_queue.run_once(auto_retry, gap, data={"row": i, "chat": int(uid_s), "uid": int(uid_s), "count": 0}, name=f"retry-{i}")

async def grp_retry(ctx):
    d = ctx.job.data
    row, tid, gid, uid_s = d["row"], d["tid"], d["gid"], d["uid"]
    count = d.get("count", 0)
    r = sheet.row_values(row)
    if not r or (len(r) > 5 and r[5] != "pending"):
        return
    max_ret = get_user_retries(int(uid_s))
    if count >= max_ret:
        members = get_tmembers(tid)
        for mu, mn, ms in members:
            if ms == "pending":
                set_tstatus(tid, mu, "missed")
        rep = r[4] if len(r) > 4 else "none"
        ds = norm_date(r[2]) if len(r) > 2 else ""
        ts = norm_time(r[3]) if len(r) > 3 else ""
        if rep and rep != "none":
            advance_rep(row, rep, ds, ts)
            reset_tmembers(tid)
        else:
            sheet.update_cell(row, 6, "missed")
        sheet.update_cell(row, 7, 0)
        await _update_group_status(ctx, gid, tid, r)
        return
    msg = r[1] if len(r) > 1 else ""
    members = get_tmembers(tid)
    pending = [(u, n) for u, n, s in members if s == "pending"]
    for mu, mn in pending:
        old = ctx.bot_data.get(f"rmsg_{row}_{mu}")
        if old:
            try:
                await ctx.bot.edit_message_reply_markup(old["cid"], old["mid"], reply_markup=None)
            except:
                pass
        try:
            s = await ctx.bot.send_message(chat_id=int(mu),
                text=f"\U0001f514 {msg}\n<i>Retry {count+1}/{max_ret}</i>",
                reply_markup=reminder_kb(row), parse_mode="HTML")
            ctx.bot_data[f"rmsg_{row}_{mu}"] = {"mid": s.message_id, "cid": int(mu)}
        except:
            pass
    sheet.update_cell(row, 7, count + 1)
    c_cfg = cfg_map_for(uid_s)
    gap = c_cfg * 60
    ctx.job_queue.run_once(grp_retry, gap, data={"row": row, "tid": tid, "gid": gid, "uid": uid_s, "count": count + 1}, name=f"retry-{row}")

def cfg_map_for(uid_s):
    try:
        rows = cfg_sheet.get_all_values()
        for r in rows[1:]:
            if r and r[0] == uid_s:
                return int(r[4]) if len(r) > 4 and r[4].isdigit() else DEF_RETRY_GAP
    except:
        pass
    return DEF_RETRY_GAP

# ============= DAILY DIGEST =============
async def check_digest(ctx):
    try:
        cfg_rows = cfg_sheet.get_all_values()
    except:
        return
    for r in cfg_rows[1:]:
        if not r or len(r) < 3:
            continue
        uid_s = r[0]
        dig_on = r[1] if len(r) > 1 else "true"
        dig_time = r[2] if len(r) > 2 else DEF_DIGEST_TIME
        if dig_on != "true":
            continue
        tzn = r[5] if len(r) > 5 else DEF_TZ
        tz = safe_tz(tzn)
        now = datetime.now(tz)
        now_hm = now.strftime("%H:%M")
        dt = norm_time(dig_time)
        if now_hm != dt:
            continue
        today_s = now.strftime("%Y-%m-%d")
        try:
            rem_rows = sheet.get_all_values()
        except:
            continue
        items = []
        for rr in rem_rows[1:]:
            if not rr or rr[0] != uid_s:
                continue
            if len(rr) > 7 and rr[7]:
                continue
            st = rr[5] if len(rr) > 5 else ""
            if st not in ("active", "snoozed", "pending"):
                continue
            ds = norm_date(rr[2]) if len(rr) > 2 else ""
            if ds != today_s:
                rep = rr[4] if len(rr) > 4 else ""
                if rep == "daily":
                    pass
                elif rep.startswith("custom:") and is_custom_match(rep, now):
                    pass
                else:
                    continue
            ts = norm_time(rr[3]) if len(rr) > 3 else ""
            msg = rr[1] if len(rr) > 1 else ""
            items.append((ts, msg))
        if not items:
            continue
        items.sort()
        today_fmt = now.strftime("%-d %b")
        lines = [f"\u2600\ufe0f Good morning!\n{hdr('Today \u2014 ' + today_fmt)}", ""]
        for ts, msg in items:
            lines.append(f"  {fmt_time(ts)} \u00b7 {msg}")
        lines.append(f"\n{len(items)} reminder{'s' if len(items) != 1 else ''} today")
        txt = "\n".join(lines)
        try:
            await ctx.bot.send_message(chat_id=int(uid_s), text=txt, parse_mode="HTML", reply_markup=home_kb())
        except:
            pass

# ============= WEEKLY REPORT =============
async def check_weekly_report(ctx):
    try:
        cfg_rows = cfg_sheet.get_all_values()
    except:
        return
    for r in cfg_rows[1:]:
        if not r:
            continue
        uid_s = r[0]
        wr = r[7] if len(r) > 7 else "true"
        if wr != "true":
            continue
        tzn = r[5] if len(r) > 5 else DEF_TZ
        tz = safe_tz(tzn)
        now = datetime.now(tz)
        if now.weekday() != 6:
            continue
        if now.strftime("%H:%M") != "09:00":
            continue
        today = now.date()
        week_start = today - timedelta(days=6)
        try:
            rem_rows = sheet.get_all_values()
        except:
            continue
        done_c, missed_c, snoozed_c = 0, 0, 0
        day_done = {}
        day_missed = {}
        done_list = []
        missed_list = []
        for rr in rem_rows[1:]:
            if not rr or rr[0] != uid_s:
                continue
            if len(rr) > 7 and rr[7]:
                continue
            ds = norm_date(rr[2]) if len(rr) > 2 else ""
            st = rr[5] if len(rr) > 5 else ""
            msg = rr[1] if len(rr) > 1 else ""
            try:
                rd = datetime.strptime(ds, "%Y-%m-%d").date()
            except:
                continue
            if not (week_start <= rd <= today):
                continue
            dn = DAY_SHORT[rd.weekday()]
            if st == "done":
                done_c += 1
                day_done[dn] = day_done.get(dn, 0) + 1
                done_list.append(f"\u2705 {msg} \u00b7 {fmt_date(ds)}")
            elif st == "missed":
                missed_c += 1
                day_missed[dn] = day_missed.get(dn, 0) + 1
                missed_list.append(f"\u2717 {msg} \u00b7 {fmt_date(ds)}")
        total = done_c + missed_c
        if total == 0:
            continue
        pct = int(done_c / total * 100) if total else 0
        best_day = max(day_done, key=day_done.get) if day_done else "\u2014"
        worst_day = max(day_missed, key=day_missed.get) if day_missed else "\u2014"
        streak = 0
        for i in range(7):
            d = today - timedelta(days=i)
            dn = DAY_SHORT[d.weekday()]
            if day_missed.get(dn, 0) == 0 and day_done.get(dn, 0) > 0:
                streak += 1
            else:
                break
        if pct >= 90:
            mood = "Outstanding! \U0001f3c6"
        elif pct >= 70:
            mood = "Keep it up! \U0001f4aa"
        elif pct >= 50:
            mood = "Room to improve \U0001f4c8"
        else:
            mood = "Let's do better next week \U0001f3af"
        ws = week_start.strftime("%-d %b")
        te = today.strftime("%-d %b")
        lines = [f"\U0001f4ca Weekly Report\n{hdr(f'{ws} \u2014 {te}')}", "",
                 f"\u2705 Completed: {done_c}/{total} ({pct}%)",
                 f"\u274c Missed: {missed_c}",
                 f"\u23ed Snoozed: {snoozed_c} times", "",
                 f"\U0001f4c5 Most Productive: {best_day}",
                 f"\U0001f4c9 Most Missed: {worst_day}", "",
                 f"\U0001f525 Streak: {streak} day{'s' if streak != 1 else ''} without missing!", "",
                 mood]
        txt = "\n".join(lines)
        row_num = None
        for idx, rr in enumerate(rem_rows[1:], start=2):
            if rr and rr[0] == uid_s:
                row_num = idx
                break
        btns = [[IKB("\U0001f4cb Details", callback_data=f"wrdet_{uid_s}")]]
        ctx.bot_data[f"wr_done_{uid_s}"] = done_list[-10:]
        ctx.bot_data[f"wr_miss_{uid_s}"] = missed_list[-10:]
        try:
            await ctx.bot.send_message(chat_id=int(uid_s), text=txt, parse_mode="HTML", reply_markup=IKM(btns))
        except:
            pass

# ============= MAIN =============
async def post_init(app):
    private_cmds = [
        BotCommand("add", "New reminder"),
        BotCommand("list", "All reminders"),
        BotCommand("month", "Monthly schedule"),
        BotCommand("settings", "Bot settings"),
        BotCommand("info", "About this bot")
    ]
    group_cmds = [
        BotCommand("start", "Bot info & commands"),
        BotCommand("remind", "Group reminder"),
        BotCommand("list", "Active reminders")
    ]
    await app.bot.set_my_commands(private_cmds, scope={"type": "all_private_chats"})
    await app.bot.set_my_commands(group_cmds, scope={"type": "all_group_chats"})

def main():
    app = Application.builder().token(TOKEN).job_queue(JobQueue()).post_init(post_init).build()
    for cmd, fn in [("start", start), ("add", add_cmd), ("list", list_cmd),
                    ("info", info_cmd), ("settings", settings_cmd), ("month", month_cmd), ("remind", remind_cmd)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(check_reminders, interval=60, first=0)
    app.job_queue.run_repeating(check_digest, interval=60, first=30)
    app.job_queue.run_repeating(check_weekly_report, interval=60, first=45)
    log.info("\U0001f680 Smart Reminder Bot Running")
    app.run_polling()

if __name__ == "__main__":
    main()
