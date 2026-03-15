import logging, os, json, re, time as _time
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM, BotCommand, ForceReply
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
IST = pytz.timezone(DEF_TZ)

# ============= LOGGING =============
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ============= SHEETS =============
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
        ws = workbook.add_worksheet(title=name, rows=1000, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
    return ws

sheet = get_or_create_sheet("Reminders", ["user_id","message","date","time","repeat","status","retry_count","group_id","task_id"])
cfg_sheet = get_or_create_sheet("Settings", ["user_id","digest_on","digest_time","max_retries","retry_gap","timezone","username","weekly_report"])
gm_sheet = get_or_create_sheet("GroupMembers", ["group_id","user_id","first_name","username","subscribed"])
tm_sheet = get_or_create_sheet("TaskMembers", ["task_id","user_id","first_name","status"])

# ============= CONSTANTS =============
DAYS = ["mon","tue","wed","thu","fri","sat","sun"]
DAY_NAMES = {"mon":"Monday","tue":"Tuesday","wed":"Wednesday","thu":"Thursday","fri":"Friday","sat":"Saturday","sun":"Sunday"}
DAY_SHORT = {"mon":"Mon","tue":"Tue","wed":"Wed","thu":"Thu","fri":"Fri","sat":"Sat","sun":"Sun"}
ST_IC = {"active":"○","pending":"●","snoozed":"◐","done":"✅","missed":"✗","cancelled":"—"}
GT_IC = {"waiting":"⏳","pending":"⏳","done":"✅","snoozed":"◐","missed":"✗","skipped":"⏭"}
REP_MAP = {"none":"Once","daily":"Daily","weekly":"Weekly","monthly":"Monthly"}

TZ_DATA = {
    "Asia":[("Asia/Kolkata","India","+5:30"),("Asia/Dubai","UAE","+4"),("Asia/Karachi","Pakistan","+5"),("Asia/Dhaka","Bangladesh","+6"),("Asia/Bangkok","Thailand","+7"),("Asia/Singapore","Singapore","+8"),("Asia/Shanghai","China","+8"),("Asia/Tokyo","Japan","+9"),("Asia/Seoul","Korea","+9"),("Asia/Jakarta","Indonesia","+7"),("Asia/Riyadh","Saudi","+3"),("Asia/Manila","Philippines","+8")],
    "Europe":[("Europe/London","UK","+0"),("Europe/Berlin","Germany","+1"),("Europe/Paris","France","+1"),("Europe/Moscow","Russia","+3"),("Europe/Istanbul","Turkey","+3")],
    "Americas":[("America/New_York","US East","-5"),("America/Chicago","US Central","-6"),("America/Denver","US Mountain","-7"),("America/Los_Angeles","US West","-8"),("America/Sao_Paulo","Brazil","-3"),("America/Mexico_City","Mexico","-6")],
    "Africa":[("Africa/Lagos","Nigeria","+1"),("Africa/Cairo","Egypt","+2"),("Africa/Nairobi","Kenya","+3"),("Africa/Johannesburg","S.Africa","+2")],
    "Oceania":[("Australia/Sydney","Australia","+10"),("Pacific/Auckland","New Zealand","+12")]
}
TZ_ICONS = {"Asia":"🌏","Europe":"🌍","Americas":"🌎","Africa":"🌍","Oceania":"🌏"}

# ============= HELPERS =============
def hdr(t): return f"<b>{t}</b>\n━━━━━━━━━━━━━━━━━━━━"
def detail(m, d, t, r=None):
    ds = fmt_date(d); ts = fmt_time(t)
    rp = fmt_repeat(r) if r else ""
    return f"{m}\n{ds} · {ts}" + (f" · {rp}" if rp else "")

def fmt_date(d):
    try:
        dt = datetime.strptime(str(d), "%Y-%m-%d")
        return dt.strftime("%-d %b")
    except: return str(d)

def fmt_time(t):
    try:
        dt = datetime.strptime(str(t), "%H:%M")
        return dt.strftime("%-I:%M %p")
    except: return str(t)

def fmt_repeat(r):
    if not r: return ""
    if r.startswith("custom:"):
        ds = r.replace("custom:","").split(",")
        if sorted(ds) == ["fri","mon","thu","tue","wed"]: return "Mon–Fri"
        if sorted(ds) == ["sat","sun"]: return "Weekends"
        if sorted(ds) == sorted(DAYS): return "Daily"
        return ", ".join(DAY_SHORT.get(d,d) for d in ds)
    return REP_MAP.get(r, r)

def safe_tz(n):
    try: return pytz.timezone(n)
    except: return pytz.timezone(DEF_TZ)

def tz_label(n):
    for rg in TZ_DATA.values():
        for tz_id, lbl, _ in rg:
            if tz_id == n: return lbl
    return n.split("/")[-1]

def tz_short(n):
    for rg in TZ_DATA.values():
        for tz_id, lbl, off in rg:
            if tz_id == n: return f"{lbl} ({off})"
    return n

def home_kb(): return IKM([[IKB("＋ New", callback_data="add")]])
def home_text(): return "Type a reminder:\n<i>\"Buy milk tomorrow at 5pm\"</i>"

# ============= SETTINGS =============
def get_cfg(uid):
    uid_s = str(uid)
    rows = cfg_sheet.get_all_values()
    for i, r in enumerate(rows):
        if i == 0: continue
        if str(r[0]) == uid_s:
            return {
                "row": i+1,
                "digest_on": r[1] if len(r)>1 else "true",
                "digest_time": r[2] if len(r)>2 else DEF_DIGEST_TIME,
                "max_retries": int(r[3]) if len(r)>3 and r[3] else DEF_RETRIES,
                "retry_gap": int(r[4]) if len(r)>4 and r[4] else DEF_RETRY_GAP,
                "timezone": r[5] if len(r)>5 and r[5] else DEF_TZ,
                "username": r[6] if len(r)>6 else "",
                "weekly_report": r[7] if len(r)>7 else "true"
            }
    cfg_sheet.append_row([uid_s, "true", DEF_DIGEST_TIME, DEF_RETRIES, DEF_RETRY_GAP, DEF_TZ, "", "true"], value_input_option="RAW")
    return {"row": len(rows)+1, "digest_on":"true", "digest_time":DEF_DIGEST_TIME, "max_retries":DEF_RETRIES, "retry_gap":DEF_RETRY_GAP, "timezone":DEF_TZ, "username":"", "weekly_report":"true"}

def save_cfg(uid, key, val):
    cfg = get_cfg(uid)
    cols = {"digest_on":2,"digest_time":3,"max_retries":4,"retry_gap":5,"timezone":6,"username":7,"weekly_report":8}
    if key in cols:
        cfg_sheet.update_cell(cfg["row"], cols[key], val)

def get_tz(uid): return safe_tz(get_cfg(uid)["timezone"])

def update_username(user):
    if not user or not user.username: return
    uid_s = str(user.id)
    uname = user.username.lower()
    rows = cfg_sheet.get_all_values()
    for i, r in enumerate(rows):
        if i == 0: continue
        if str(r[0]) == uid_s:
            cur = r[6] if len(r) > 6 else ""
            if cur != uname:
                if len(r) < 7:
                    while len(r) < 7: r.append("")
                    cfg_sheet.update(f"A{i+1}", [r])
                cfg_sheet.update_cell(i+1, 7, uname)
            return
    cfg_sheet.append_row([uid_s, "true", DEF_DIGEST_TIME, DEF_RETRIES, DEF_RETRY_GAP, DEF_TZ, uname, "true"], value_input_option="RAW")

# ============= GROUP MEMBERS =============
def set_gsub(gid, uid, name, username="", sub=True):
    gid_s, uid_s = str(gid), str(uid)
    uname = (username or "").lower()
    rows = gm_sheet.get_all_values()
    for i, r in enumerate(rows):
        if i == 0: continue
        if str(r[0]) == gid_s and str(r[1]) == uid_s:
            gm_sheet.update_cell(i+1, 3, name)
            if uname: gm_sheet.update_cell(i+1, 4, uname)
            gm_sheet.update_cell(i+1, 5, "true" if sub else "false")
            return
    gm_sheet.append_row([gid_s, uid_s, name, uname, "true" if sub else "false"], value_input_option="RAW")

def get_gsubs(gid):
    gid_s = str(gid)
    result = []
    rows = gm_sheet.get_all_values()
    for i, r in enumerate(rows):
        if i == 0: continue
        if str(r[0]) == gid_s and len(r) > 4 and r[4] == "true":
            uname = r[3] if len(r) > 3 else ""
            result.append((r[1], r[2], uname))
    return result

# ============= TASK MEMBERS =============
def add_tmember(tid, uid, name, status="waiting"):
    tm_sheet.append_row([tid, str(uid), name, status], value_input_option="RAW")

def get_tmembers(tid):
    result = []
    rows = tm_sheet.get_all_values()
    for i, r in enumerate(rows):
        if i == 0: continue
        if r[0] == tid and len(r) > 3:
            result.append({"row":i+1, "uid":r[1], "name":r[2], "status":r[3]})
    return result

def set_tstatus(tid, uid, status):
    uid_s = str(uid)
    rows = tm_sheet.get_all_values()
    for i, r in enumerate(rows):
        if i == 0: continue
        if r[0] == tid and str(r[1]) == uid_s:
            tm_sheet.update_cell(i+1, 4, status)
            return

# ============= REMINDER HELPERS =============
def get_detail(r):
    msg = r[1] if len(r)>1 else ""
    d = norm_date(r[2]) if len(r)>2 else ""
    t = norm_time(r[3]) if len(r)>3 else ""
    rep = r[4] if len(r)>4 else "none"
    st = r[5] if len(r)>5 else "active"
    return msg, d, t, rep, st

def norm_date(v):
    v = str(v).strip()
    if not v: return ""
    try:
        f = float(v)
        if f > 40000:
            dt = datetime(1899,12,30) + timedelta(days=int(f))
            return dt.strftime("%Y-%m-%d")
    except: pass
    for fmt in ["%Y-%m-%d","%d-%m-%Y","%d/%m/%Y","%m/%d/%Y"]:
        try: return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except: pass
    return v

def norm_time(v):
    v = str(v).strip()
    if not v: return ""
    try:
        f = float(v)
        h = int(f * 24)
        m = int((f * 24 - h) * 60)
        return f"{h:02d}:{m:02d}"
    except: pass
    v2 = re.sub(r'\s+', '', v.lower())
    m2 = re.match(r'^(\d{1,2})[.:](\d{2})\s*(am|pm)$', v2)
    if m2:
        h, mn, ap = int(m2.group(1)), int(m2.group(2)), m2.group(3)
        if ap == 'pm' and h != 12: h += 12
        if ap == 'am' and h == 12: h = 0
        return f"{h:02d}:{mn:02d}"
    m3 = re.match(r'^(\d{1,2})\s*(am|pm)$', v2)
    if m3:
        h, ap = int(m3.group(1)), m3.group(2)
        if ap == 'pm' and h != 12: h += 12
        if ap == 'am' and h == 12: h = 0
        return f"{h:02d}:00"
    if re.match(r'^\d{1,2}:\d{2}$', v): return v.zfill(5)
    return v

def parse_time_input(text):
    text = text.strip()
    t = re.sub(r'\s+', '', text.lower())
    m = re.match(r'^(\d{1,2})[.:](\d{1,2})\s*(am|pm)$', t)
    if m:
        h, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        if h < 1 or h > 12 or mn > 59: return None
        if ap == 'pm' and h != 12: h += 12
        if ap == 'am' and h == 12: h = 0
        return f"{h:02d}:{mn:02d}"
    m = re.match(r'^(\d{1,2})\s*(am|pm)$', t)
    if m:
        h, ap = int(m.group(1)), m.group(2)
        if h < 1 or h > 12: return None
        if ap == 'pm' and h != 12: h += 12
        if ap == 'am' and h == 12: h = 0
        return f"{h:02d}:00"
    m = re.match(r'^(\d{1,2}):(\d{2})$', text.strip())
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if h > 23 or mn > 59: return None
        return f"{h:02d}:{mn:02d}"
    return None

def is_past(ds, ts, tz):
    try:
        now = datetime.now(tz)
        dt = datetime.strptime(f"{ds} {ts}", "%Y-%m-%d %H:%M")
        dt = tz.localize(dt)
        return dt < now
    except: return False

def past_msg(ts): return f"⚠ {fmt_time(ts)} has already passed today.\nEnter a future time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>"

def advance_rep(row, r):
    rep = r[4] if len(r)>4 else "none"
    if rep == "none":
        sheet.update_cell(row, 6, "done")
        return
    d = datetime.strptime(norm_date(r[2]), "%Y-%m-%d")
    if rep == "daily":
        nd = d + timedelta(days=1)
    elif rep == "weekly":
        nd = d + timedelta(days=7)
    elif rep == "monthly":
        m = d.month + 1; y = d.year
        if m > 12: m = 1; y += 1
        try: nd = d.replace(year=y, month=m)
        except: nd = d.replace(year=y, month=m, day=28)
    elif rep.startswith("custom:"):
        cdays = rep.replace("custom:","").split(",")
        nd = d
        for _ in range(7):
            nd = nd + timedelta(days=1)
            wd = DAYS[nd.weekday()]
            if wd in cdays: break
        else: nd = d + timedelta(days=1)
    else:
        sheet.update_cell(row, 6, "done"); return
    sheet.update_cell(row, 3, nd.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, "active")
    sheet.update_cell(row, 7, 0)

# ============= CALENDAR =============
def cal_kb(y, m, tz, back_cb="cancel"):
    import calendar
    now = datetime.now(tz); today = now.date()
    rows = []
    mn = datetime(y, m, 1).strftime("%B %Y")
    rows.append([IKB(mn, callback_data="noop")])
    rows.append([IKB(d, callback_data="noop") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]])
    cal = calendar.monthcalendar(y, m)
    for week in cal:
        all_past = all((d == 0 or (datetime(y,m,d).date() < today)) for d in week)
        if all_past and not any(d != 0 and datetime(y,m,d).date() == today for d in week): continue
        row = []
        for d in week:
            if d == 0: row.append(IKB(" ", callback_data="noop"))
            elif datetime(y,m,d).date() < today: row.append(IKB("·", callback_data="noop"))
            elif datetime(y,m,d).date() == today: row.append(IKB(f"[{d}]", callback_data=f"day_{y}-{m:02d}-{d:02d}"))
            else: row.append(IKB(str(d), callback_data=f"day_{y}-{m:02d}-{d:02d}"))
        rows.append(row)
    rows.append([IKB("Today", callback_data=f"day_{today.strftime('%Y-%m-%d')}"), IKB("Tomorrow", callback_data=f"day_{(today+timedelta(days=1)).strftime('%Y-%m-%d')}")])
    pm = m - 1; py = y
    if pm < 1: pm = 12; py -= 1
    nm = m + 1; ny = y
    if nm > 12: nm = 1; ny += 1
    rows.append([IKB("‹", callback_data=f"cal_{py}-{pm:02d}"), IKB("›", callback_data=f"cal_{ny}-{nm:02d}")])
    rows.append([IKB("✕ Cancel", callback_data=back_cb)])
    return IKM(rows)

# ============= NL PARSER =============
def _find_time(text):
    patterns = [
        r'(?:at|by)\s+(\d{1,2})[.:](\d{2})\s*(am|pm)',
        r'(?:at|by)\s+(\d{1,2})\s*(am|pm)',
        r'(\d{1,2})[.:](\d{2})\s*(am|pm)',
        r'(\d{1,2})\s*(am|pm)',
        r'(?:at|by)\s+(\d{1,2}):(\d{2})',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            gs = m.groups()
            if len(gs) == 3:
                h, mn, ap = int(gs[0]), int(gs[1]), gs[2].lower()
                if ap == 'pm' and h != 12: h += 12
                if ap == 'am' and h == 12: h = 0
                return f"{h:02d}:{mn:02d}", m.start(), m.end()
            elif len(gs) == 2:
                if gs[1].lower() in ('am','pm'):
                    h, ap = int(gs[0]), gs[1].lower()
                    if ap == 'pm' and h != 12: h += 12
                    if ap == 'am' and h == 12: h = 0
                    return f"{h:02d}:00", m.start(), m.end()
                else:
                    h, mn = int(gs[0]), int(gs[1])
                    if h > 23 or mn > 59: continue
                    return f"{h:02d}:{mn:02d}", m.start(), m.end()
    return None, -1, -1

def _find_date(text, tz):
    now = datetime.now(tz); today = now.date()
    low = text.lower()
    patterns = [
        (r'\b(today|tonight)\b', 0),
        (r'\b(tomorrow|tmrw|tmr)\b', 1),
        (r'\bday\s+after\s+tomorrow\b', 2),
        (r'\bnext\s+week\b', 7),
    ]
    for p, delta in patterns:
        m = re.search(p, low)
        if m:
            d = today + timedelta(days=delta)
            return d.strftime("%Y-%m-%d"), m.start(), m.end()
    day_map = {"monday":0,"mon":0,"tuesday":1,"tue":1,"wednesday":2,"wed":2,"thursday":3,"thu":3,"friday":4,"fri":4,"saturday":5,"sat":5,"sunday":6,"sun":6}
    for name, wd in day_map.items():
        m = re.search(r'\b' + name + r'\b', low)
        if m:
            diff = (wd - today.weekday()) % 7
            if diff == 0: diff = 7
            d = today + timedelta(days=diff)
            return d.strftime("%Y-%m-%d"), m.start(), m.end()
    m = re.search(r'\bon\s+(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*', low)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)[:3]} {today.year}", "%d %b %Y").date()
            if d < today: d = d.replace(year=d.year+1)
            return d.strftime("%Y-%m-%d"), m.start(), m.end()
        except: pass
    m = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*', low)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)[:3]} {today.year}", "%d %b %Y").date()
            if d < today: d = d.replace(year=d.year+1)
            return d.strftime("%Y-%m-%d"), m.start(), m.end()
        except: pass
    m = re.search(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{1,2})(?:st|nd|rd|th)?', low)
    if m:
        try:
            d = datetime.strptime(f"{m.group(2)} {m.group(1)[:3]} {today.year}", "%d %b %Y").date()
            if d < today: d = d.replace(year=d.year+1)
            return d.strftime("%Y-%m-%d"), m.start(), m.end()
        except: pass
    return None, -1, -1

def _find_repeat(text, tz):
    low = text.lower()
    now = datetime.now(tz); today = now.date()
    day_map = {"monday":0,"mon":0,"tuesday":1,"tue":1,"wednesday":2,"wed":2,"thursday":3,"thu":3,"friday":4,"fri":4,"saturday":5,"sat":5,"sunday":6,"sun":6}
    for name, wd in day_map.items():
        m = re.search(r'\bevery\s+' + name + r'\b', low)
        if m:
            diff = (wd - today.weekday()) % 7
            if diff == 0: diff = 7
            d = today + timedelta(days=diff)
            return "weekly", m.start(), m.end(), d.strftime("%Y-%m-%d")
    if re.search(r'\bevery\s*day\b', low):
        m = re.search(r'\bevery\s*day\b', low)
        return "daily", m.start(), m.end(), today.strftime("%Y-%m-%d")
    simple = [(r'\bdaily\b', "daily"), (r'\bweekly\b', "weekly"), (r'\bmonthly\b', "monthly"), (r'\bevery\s+week\b', "weekly"), (r'\bevery\s+month\b', "monthly"), (r'\bevery\s+day\b', "daily")]
    for p, rep in simple:
        m = re.search(p, low)
        if m: return rep, m.start(), m.end(), None
    return None, -1, -1, None

def _find_relative(text, tz):
    now = datetime.now(tz)
    m = re.search(r'\b(?:in|after)\s+(\d+)\s*(min(?:ute)?s?|hrs?|hours?|days?|weeks?)\b', text, re.IGNORECASE)
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
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), m.start(), m.end()
    return None, None, -1, -1

def _clean_msg(text, spans):
    spans = sorted(spans, key=lambda x: x[0], reverse=True)
    for s, e in spans:
        if s >= 0: text = text[:s] + text[e:]
    prefixes = [r'^remind\s+me\s+to\s+', r'^remind\s+me\s+', r'^reminder\s+', r'^remember\s+to\s+', r"^don'?t\s+forget\s+to\s+", r'^set\s+reminder\s+']
    for p in prefixes:
        text = re.sub(p, '', text.strip(), flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', text).strip()

def parse_nl_partial(text, tz):
    result = {"message": None, "date": None, "time": None, "repeat": None}
    spans = []
    rd, rt, rs, re2 = _find_relative(text, tz)
    if rd:
        result["date"] = rd; result["time"] = rt; spans.append((rs, re2))
    rep, rps, rpe, rep_date = _find_repeat(text, tz)
    if rep:
        result["repeat"] = rep; spans.append((rps, rpe))
        if rep_date and not result["date"]: result["date"] = rep_date
    if not result["time"]:
        t, ts, te = _find_time(text)
        if t: result["time"] = t; spans.append((ts, te))
    if not result["date"]:
        d, ds, de = _find_date(text, tz)
        if d: result["date"] = d; spans.append((ds, de))
    msg = _clean_msg(text, spans)
    if msg: result["message"] = msg
    has_trigger = result["time"] or result["date"] or result["repeat"]
    if not has_trigger:
        for p in [r'remind\s+me', r'reminder', r'remember\s+to', r"don'?t\s+forget", r'set\s+reminder']:
            if re.search(p, text, re.IGNORECASE):
                has_trigger = True; break
    if not has_trigger or not result["message"]: return None
    return result

# ============= SAFE EDIT =============
async def safe_edit(msg, text, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except:
        pass

# ============= PROMPT UTILS =============
def store_prompt(ud, msg):
    ud["p_mid"] = msg.message_id; ud["p_cid"] = msg.chat_id

async def rm_prompt(ud, bot):
    mid = ud.pop("p_mid", None); cid = ud.pop("p_cid", None)
    if mid and cid:
        try: await bot.edit_message_reply_markup(cid, mid, reply_markup=None)
        except: pass

async def del_prompt(ud, bot):
    mid = ud.pop("p_mid", None); cid = ud.pop("p_cid", None)
    if mid and cid:
        try: await bot.delete_message(cid, mid)
        except: pass

def store_home(ud, msg):
    ud["h_mid"] = msg.message_id; ud["h_cid"] = msg.chat_id

async def rm_home(ud, bot):
    mid = ud.pop("h_mid", None); cid = ud.pop("h_cid", None)
    if mid and cid:
        try: await bot.edit_message_reply_markup(cid, mid, reply_markup=None)
        except: pass

# ============= AUTO MINIMIZE =============
async def p_auto_minimize(ctx):
    d = ctx.job.data
    mid = d["mid"]; cid = d["cid"]; min_text = d["min_text"]
    show_cb = d["show_cb"]
    kb = IKM([[IKB("📋 Show", callback_data=show_cb)]])
    try:
        await ctx.bot.edit_message_text(min_text, chat_id=cid, message_id=mid, reply_markup=kb, parse_mode="HTML")
    except: pass

def schedule_minimize(ctx, msg, min_text, show_cb, delay=60):
    ctx.job_queue.run_once(
        p_auto_minimize, delay,
        data={"mid": msg.message_id, "cid": msg.chat_id, "min_text": min_text, "show_cb": show_cb},
        name=f"pmin_{msg.chat_id}_{msg.message_id}"
    )

def cancel_minimize(ctx, chat_id, msg_id):
    jobs = ctx.job_queue.get_jobs_by_name(f"pmin_{chat_id}_{msg_id}")
    for j in jobs: j.schedule_removal()

# ============= STORE REMINDER MSG =============
def store_rem_msg(bd, row, mid, cid):
    bd[f"rem_{row}"] = {"mid": mid, "cid": cid}

async def rm_old_rem_btns(bd, bot, row):
    k = f"rem_{row}"
    d = bd.pop(k, None)
    if d:
        try: await bot.edit_message_reply_markup(d["cid"], d["mid"], reply_markup=None)
        except: pass

# ============= SAVE REMINDER =============
async def save_reminder(msg, ud, bot, uid, tz, gid="", tid=""):
    m = ud.get("message",""); d = ud.get("date",""); t = ud.get("time","")
    rep = ud.get("repeat","none")
    sheet.append_row([str(uid), m, d, t, rep, "active", 0, gid, tid], value_input_option="RAW")
    rep_btn = []
    if rep == "none":
        row_count = len(sheet.get_all_values())
        rep_btn = [IKB("🔁 Repeat", callback_data=f"chrep_{row_count}")]
    kb_row = rep_btn + [IKB("＋ New", callback_data="add")]
    text = f"{hdr('Saved ✓')}\n{detail(m, d, t, rep)}"
    sent = await msg.reply_text(text, reply_markup=IKM([kb_row]), parse_mode="HTML")
    store_home(ud, sent)
    ud.pop("step", None); ud.pop("message", None); ud.pop("date", None); ud.pop("time", None); ud.pop("repeat", None)

# ============= GROUP SAVE =============
async def save_group_reminder(msg, ud, bot, uid, tz):
    gid = ud.get("g_chat", "")
    m = ud.get("message",""); d = ud.get("date",""); t = ud.get("time","")
    rep = ud.get("repeat","none")
    tid = f"t_{int(_time.time())}"
    sheet.append_row([str(uid), m, d, t, rep, "active", 0, str(gid), tid], value_input_option="RAW")
    tagged = ud.get("g_tagged", [])
    subs = get_gsubs(gid)
    if tagged:
        tag_names = set()
        for tg in tagged:
            for n in tg.get("names", set()): tag_names.add(n.lower())
        for sub_uid, sub_name, sub_uname in subs:
            matched = False
            if sub_uname and sub_uname.lower() in tag_names: matched = True
            if sub_name and sub_name.lower() in tag_names: matched = True
            if matched:
                add_tmember(tid, sub_uid, sub_name, "waiting")
            else:
                add_tmember(tid, sub_uid, sub_name, "skipped")
        active = [(tm["uid"], tm["name"]) for tm in get_tmembers(tid) if tm["status"] == "waiting"]
        names_str = ", ".join(n for _, n in active) if active else "None"
        sub_line = f"For: {names_str}"
    else:
        for sub_uid, sub_name, _ in subs:
            add_tmember(tid, sub_uid, sub_name, "waiting")
        sub_line = f"{len(subs)} subscribed"
        if subs:
            names_str = ", ".join(n for _, n, _ in subs)
            sub_line += f": {names_str}"
    rep_btn = []
    if rep == "none":
        row_count = len(sheet.get_all_values())
        rep_btn = [[IKB("🔁 Repeat", callback_data=f"chrep_{row_count}")]]
    text = f"{hdr('Group Reminder')}\n{detail(m, d, t, rep)}\nBy {msg.from_user.first_name}\n\n{sub_line}"
    kb = [[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")]] + rep_btn
    await msg.reply_text(text, reply_markup=IKM(kb), parse_mode="HTML")
    for k in ["step","message","date","time","repeat","g_chat","g_tagged"]: ud.pop(k, None)

# ============= EXTRACT TAGS =============
def extract_tag_texts(msg):
    if not msg or not msg.entities: return []
    tags = []
    for ent in msg.entities:
        if ent.type == "text_mention" and ent.user:
            names = {ent.user.first_name.lower()} if ent.user.first_name else set()
            if ent.user.username: names.add(ent.user.username.lower())
            tags.append({"uid": str(ent.user.id), "names": names})
        elif ent.type == "mention":
            uname = msg.text[ent.offset+1:ent.offset+ent.length].lower()
            tags.append({"uid": None, "names": {uname}})
    return tags

# ============= CUSTOM DAY KB =============
def custom_day_kb(selected):
    rows = []
    r1 = [IKB(f"{'✓ ' if d in selected else ''}{DAY_SHORT[d]}", callback_data=f"cday_{d}") for d in DAYS[:4]]
    r2 = [IKB(f"{'✓ ' if d in selected else ''}{DAY_SHORT[d]}", callback_data=f"cday_{d}") for d in DAYS[4:]]
    rows.append(r1); rows.append(r2)
    rows.append([IKB("Mon–Fri", callback_data="cday_wkday"), IKB("All", callback_data="cday_all"), IKB("Clear", callback_data="cday_clear")])
    if selected:
        rows.append([IKB("✓ Save", callback_data="cday_save")])
    rows.append([IKB("« Back", callback_data="cday_back")])
    return IKM(rows)

# ============= REPEAT KB =============
def repeat_kb(row=None):
    prefix = f"chrep_do_{row}_" if row else "rep_"
    return IKM([
        [IKB("Daily", callback_data=f"{prefix}daily"), IKB("Weekly", callback_data=f"{prefix}weekly")],
        [IKB("Monthly", callback_data=f"{prefix}monthly"), IKB("Customize", callback_data=f"cust_{row}" if row else "cust_new")]
    ])

# ============= SNOOZE KB =============
def snz_kb(row):
    opts = [("15m",15),("30m",30),("45m",45),("1h",60),("2h",120),("3h",180),("5h",300),("8h",480),("12h",720)]
    rows = []
    for i in range(0, len(opts), 3):
        rows.append([IKB(o[0], callback_data=f"snz_{row}_{o[1]}") for o in opts[i:i+3]])
    rows.append([IKB("« Back", callback_data=f"snzb_{row}")])
    return IKM(rows)

def rem_kb(row): return IKM([[IKB("Snooze", callback_data=f"snzp_{row}"), IKB("Done", callback_data=f"done_{row}")]])

# ============= COMMANDS =============
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if user: update_username(user)
    if chat.type != "private":
        text = f"{hdr('Smart Reminder Bot')}\n\n<b>Commands</b>\n/remind — Group reminder\n/list — Active reminders\n\n<b>Examples</b>\n<code>/remind Buy milk at 5pm</code>\n<code>/remind Meeting tomorrow 10am daily</code>\n<code>/remind</code> — step-by-step\n\nTag members to assign:\n<code>/remind @John Submit report at 5pm</code>"
        sent = await chat.send_message(text, parse_mode="HTML", reply_markup=IKM([[IKB("✕ Close", callback_data="gclose")]]))
        min_text = "<b>Smart Reminder Bot</b>"
        show_cb = f"gshow_start_{sent.message_id}"
        ctx.bot_data[f"gmin_{sent.message_id}"] = {"text": text, "show_cb": show_cb, "min_text": min_text, "cid": chat.id}
        ctx.job_queue.run_once(p_auto_minimize, 60, data={"mid": sent.message_id, "cid": chat.id, "min_text": min_text, "show_cb": show_cb})
        return
    await rm_home(ctx.user_data, ctx.bot)
    text = f"{hdr('Smart Reminder Bot')}\n\n{home_text()}"
    sent = await update.message.reply_text(text, reply_markup=home_kb(), parse_mode="HTML")
    store_home(ctx.user_data, sent)

async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user: update_username(user)
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /remind here for group reminders.")
        return
    await rm_home(ctx.user_data, ctx.bot)
    ctx.user_data["step"] = "message"
    sent = await update.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", reply_markup=IKM([[IKB("✕ Cancel", callback_data="cancel")]]), parse_mode="HTML")
    store_prompt(ctx.user_data, sent)

async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user: update_username(user)
    chat = update.effective_chat
    if chat.type != "private":
        gid_s = str(chat.id)
        if user and user.username:
            set_gsub(chat.id, user.id, user.first_name, user.username)
        rows = sheet.get_all_values()
        items = []
        for i, r in enumerate(rows):
            if i == 0: continue
            if len(r) < 8: continue
            if str(r[7]) != gid_s: continue
            st = r[5] if len(r)>5 else ""
            if st not in ("active","pending","snoozed"): continue
            msg, d, t, rep, _ = get_detail(r)
            items.append((i+1, msg, d, t, rep, st))
        if not items:
            sent = await chat.send_message(f"{hdr('Group Reminders')}\n\nNo active reminders.", parse_mode="HTML", reply_markup=IKM([[IKB("✕ Close", callback_data="gclose")]]))
            min_text = "<b>Group Reminders — No active</b>"
            show_cb = f"gshow_list_{gid_s}_{sent.message_id}"
            ctx.bot_data[f"gmin_{sent.message_id}"] = {"text": f"{hdr('Group Reminders')}\n\nNo active reminders.", "show_cb": show_cb, "min_text": min_text, "cid": chat.id}
            ctx.job_queue.run_once(p_auto_minimize, 60, data={"mid": sent.message_id, "cid": chat.id, "min_text": min_text, "show_cb": show_cb})
            return
        lines = [hdr("Group Reminders"), ""]
        for row, msg, d, t, rep, st in items:
            ic = ST_IC.get(st, "○")
            short_msg = msg[:30] + "…" if len(msg) > 30 else msg
            lines.append(f"{items.index((row,msg,d,t,rep,st))+1} {ic} {short_msg}\n   {fmt_date(d)} · {fmt_time(t)}")
        text = "\n".join(lines)
        sent = await chat.send_message(text, parse_mode="HTML", reply_markup=IKM([[IKB("✕ Close", callback_data="gclose")]]))
        min_text = f"<b>Group Reminders ({len(items)} active)</b>"
        show_cb = f"gshow_list_{gid_s}_{sent.message_id}"
        ctx.bot_data[f"gmin_{sent.message_id}"] = {"text": text, "show_cb": show_cb, "min_text": min_text, "cid": chat.id}
        ctx.job_queue.run_once(p_auto_minimize, 60, data={"mid": sent.message_id, "cid": chat.id, "min_text": min_text, "show_cb": show_cb})
        return
    await show_list(update.message, ctx, update.effective_user.id, new=True)

async def show_list(target, ctx, uid, new=True):
    uid_s = str(uid)
    rows = sheet.get_all_values()
    items = []
    for i, r in enumerate(rows):
        if i == 0: continue
        if str(r[0]) != uid_s: continue
        st = r[5] if len(r)>5 else ""
        if st not in ("active","pending","snoozed","missed"): continue
        gid = r[7] if len(r)>7 else ""
        if gid: continue
        msg, d, t, rep, _ = get_detail(r)
        items.append((i+1, msg, d, t, rep, st))
    if not items:
        text = f"{hdr('Reminders')}\n\nNo active reminders."
        if new:
            sent = await target.reply_text(text, reply_markup=IKM([[IKB("« Back", callback_data="back_home")]]), parse_mode="HTML")
        else:
            await safe_edit(target, text, IKM([[IKB("« Back", callback_data="back_home")]]))
        return
    lines = [hdr("Reminders"), ""]
    for idx, (row, msg, d, t, rep, st) in enumerate(items):
        ic = ST_IC.get(st, "○")
        short_msg = msg[:30] + "…" if len(msg) > 30 else msg
        lines.append(f"{idx+1} {ic} {short_msg}\n   {fmt_date(d)} · {fmt_time(t)}")
    text = "\n".join(lines)
    btn_row = [IKB(str(idx+1), callback_data=f"view_{items[idx][0]}") for idx in range(len(items))]
    btn_rows = [btn_row[i:i+5] for i in range(0, len(btn_row), 5)]
    btn_rows.append([IKB("« Back", callback_data="back_home")])
    kb = IKM(btn_rows)
    if new:
        sent = await target.reply_text(text, reply_markup=kb, parse_mode="HTML")
        tz = get_tz(uid)
        min_text = f"<b>📋 Reminders ({len(items)} active)</b>"
        schedule_minimize(ctx, sent, min_text, f"pshow_list_{uid}")
    else:
        await safe_edit(target, text, kb)

async def info_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user: update_username(user)
    text = f"""{hdr('Smart Reminder Bot')}

<b>Just type a reminder:</b>
<i>Buy milk tomorrow at 5pm
Gym at 6pm daily
Meeting in 30 min
Call mom every monday at 10am</i>

<b>Or tap ＋ New</b> for step-by-step.

<b>Features:</b>
• Smart snooze (15m–12h)
• Auto-retry if missed
• Daily digest
• Weekly report
• Monthly schedule (/month)
• Recurring: daily, weekly, monthly, custom days
• Group reminders (/remind in groups)
• Per-user timezone

<b>Commands:</b>
/add — New reminder
/list — All reminders
/month — Monthly schedule
/settings — Preferences
/info — This page"""
    sent = await update.message.reply_text(text, reply_markup=IKM([[IKB("✕ Close", callback_data="pclose_info")], [IKB("＋ New", callback_data="add")]]), parse_mode="HTML")
    min_text = "<b>ℹ️ Info</b>"
    ctx.bot_data[f"pinfo_{sent.chat_id}"] = text
    schedule_minimize(ctx, sent, min_text, f"pshow_info_{sent.chat_id}")

async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user: update_username(user)
    await show_settings(update.message, ctx, user.id, new=True)

async def show_settings(target, ctx, uid, new=True):
    cfg = get_cfg(uid)
    d_on = "ON" if cfg["digest_on"] == "true" else "OFF"
    d_time = fmt_time(cfg["digest_time"])
    wr_on = "ON" if cfg.get("weekly_report","true") == "true" else "OFF"
    tz = tz_label(cfg["timezone"])
    text = f"""{hdr('Settings')}

Daily Digest: {d_on} · {d_time}
Max Retries: {cfg['max_retries']}×
Retry Gap: {cfg['retry_gap']} min
Weekly Report: {wr_on}
Timezone: {tz}"""
    gap_str = f"{cfg['retry_gap']}m"
    kb = IKM([
        [IKB(f"Digest: {d_on}", callback_data="cfg_digest"), IKB(f"⏰ {d_time}", callback_data="cfg_dtime")],
        [IKB(f"Retries: {cfg['max_retries']}×", callback_data="cfg_retries"), IKB(f"Gap: {gap_str}", callback_data="cfg_gap")],
        [IKB(f"Report: {wr_on}", callback_data="cfg_report")],
        [IKB(f"🌍 {tz}", callback_data="cfg_tz")],
        [IKB("« Back", callback_data="back_home")]
    ])
    if new:
        await target.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await safe_edit(target, text, kb)

async def month_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user: update_username(user)
    if update.effective_chat.type != "private": return
    now = datetime.now(get_tz(user.id))
    await show_month(update.message, ctx, user.id, now.year, now.month, new=True)

async def show_month(target, ctx, uid, year, month, new=True):
    uid_s = str(uid)
    tz = get_tz(uid)
    now = datetime.now(tz); today = now.date()
    first_of_month = datetime(year, month, 1).date()
    if month == 12:
        last_of_month = datetime(year+1, 1, 1).date() - timedelta(days=1)
    else:
        last_of_month = datetime(year, month+1, 1).date() - timedelta(days=1)
    rows = sheet.get_all_values()
    all_reminders = []
    for i, r in enumerate(rows):
        if i == 0: continue
        if str(r[0]) != uid_s: continue
        gid = r[7] if len(r)>7 else ""
        if gid: continue
        msg, d, t, rep, st = get_detail(r)
        if not d or not t: continue
        all_reminders.append((msg, d, t, rep, st))
    events = {}
    for msg, d, t, rep, st in all_reminders:
        try:
            rd = datetime.strptime(d, "%Y-%m-%d").date()
        except: continue
        dates_in_month = []
        if rep == "none":
            if first_of_month <= rd <= last_of_month:
                dates_in_month.append(rd)
        elif rep == "daily":
            start = max(rd, first_of_month)
            cur = start
            while cur <= last_of_month:
                dates_in_month.append(cur)
                cur += timedelta(days=1)
        elif rep == "weekly":
            start = rd
            cur = start
            while cur <= last_of_month:
                if cur >= first_of_month:
                    dates_in_month.append(cur)
                cur += timedelta(days=7)
        elif rep == "monthly":
            try:
                md = rd.replace(year=year, month=month)
                if first_of_month <= md <= last_of_month:
                    dates_in_month.append(md)
            except: pass
        elif rep.startswith("custom:"):
            cdays = rep.replace("custom:","").split(",")
            start = max(rd, first_of_month)
            cur = start
            while cur <= last_of_month:
                wd = DAYS[cur.weekday()]
                if wd in cdays:
                    dates_in_month.append(cur)
                cur += timedelta(days=1)
        for ed in dates_in_month:
            if ed not in events: events[ed] = []
            events[ed].append((msg, t, rep, st if ed <= today else "active"))
    weeks = []
    cur = first_of_month
    while cur.weekday() != 0: cur -= timedelta(days=1)
    while cur <= last_of_month:
        ws = max(cur, first_of_month)
        we = min(cur + timedelta(days=6), last_of_month)
        count = sum(len(events.get(ws + timedelta(days=j), [])) for j in range((we-ws).days+1))
        weeks.append((ws, we, count))
        cur += timedelta(days=7)
    if len(weeks) > 4:
        merged = weeks[:3]
        ws4 = weeks[3][0]; we4 = weeks[-1][1]
        c4 = sum(w[2] for w in weeks[3:])
        merged.append((ws4, we4, c4))
        weeks = merged
    month_name = datetime(year, month, 1).strftime("%B %Y")
    lines = [hdr(f"📅 {month_name}"), ""]
    total = done = missed = upcoming = 0
    for ed, evs in events.items():
        for _, _, _, s in evs:
            total += 1
            if s == "done": done += 1
            elif s == "missed": missed += 1
            else: upcoming += 1
    for wi, (ws, we, count) in enumerate(weeks):
        ws_str = ws.strftime("%-d %b")
        we_str = we.strftime("%-d %b")
        marker = " ◂" if ws <= today <= we else ""
        lines.append(f"W{wi+1}: {ws_str}–{we_str} · {count} reminder{'s' if count!=1 else ''}{marker}")
    lines.append("")
    summary_parts = [f"Total: {total}"]
    if done: summary_parts.append(f"✅ {done} done")
    if missed: summary_parts.append(f"✗ {missed} missed")
    if upcoming: summary_parts.append(f"○ {upcoming} upcoming")
    lines.append(" · ".join(summary_parts))
    text = "\n".join(lines)
    btn_row = [IKB(str(wi+1), callback_data=f"mw_{year}_{month}_{wi}") for wi in range(len(weeks))]
    pm = month - 1; py = year
    if pm < 1: pm = 12; py -= 1
    nm = month + 1; ny = year
    if nm > 12: nm = 1; ny += 1
    kb = IKM([btn_row, [IKB(f"‹ {datetime(py,pm,1).strftime('%b')}", callback_data=f"mn_{py}_{pm}"), IKB(f"{datetime(ny,nm,1).strftime('%b')} ›", callback_data=f"mn_{ny}_{nm}")], [IKB("« Back", callback_data="back_home")]])
    if new:
        sent = await target.reply_text(text, reply_markup=kb, parse_mode="HTML")
        min_text = f"<b>📅 {month_name}</b>"
        schedule_minimize(ctx, sent, min_text, f"pshow_month_{uid}_{year}_{month}")
    else:
        await safe_edit(target, text, kb)

async def show_week(target, ctx, uid, year, month, week_idx):
    uid_s = str(uid)
    tz = get_tz(uid)
    now = datetime.now(tz); today = now.date()
    first_of_month = datetime(year, month, 1).date()
    if month == 12:
        last_of_month = datetime(year+1, 1, 1).date() - timedelta(days=1)
    else:
        last_of_month = datetime(year, month+1, 1).date() - timedelta(days=1)
    cur = first_of_month
    while cur.weekday() != 0: cur -= timedelta(days=1)
    weeks = []
    while cur <= last_of_month:
        ws = max(cur, first_of_month)
        we = min(cur + timedelta(days=6), last_of_month)
        weeks.append((ws, we))
        cur += timedelta(days=7)
    if len(weeks) > 4:
        merged = weeks[:3]
        ws4 = weeks[3][0]; we4 = weeks[-1][1]
        merged.append((ws4, we4))
        weeks = merged
    if week_idx >= len(weeks): return
    ws, we = weeks[week_idx]
    rows = sheet.get_all_values()
    all_rems = []
    for i, r in enumerate(rows):
        if i == 0: continue
        if str(r[0]) != uid_s: continue
        gid = r[7] if len(r)>7 else ""
        if gid: continue
        msg, d, t, rep, st = get_detail(r)
        if not d or not t: continue
        all_rems.append((msg, d, t, rep, st))
    events = {}
    for msg, d, t, rep, st in all_rems:
        try: rd = datetime.strptime(d, "%Y-%m-%d").date()
        except: continue
        cur_d = ws
        while cur_d <= we:
            match = False
            if rep == "none" and rd == cur_d: match = True
            elif rep == "daily" and cur_d >= rd: match = True
            elif rep == "weekly" and cur_d >= rd and (cur_d - rd).days % 7 == 0: match = True
            elif rep == "monthly":
                try:
                    if rd.day == cur_d.day and cur_d >= rd: match = True
                except: pass
            elif rep.startswith("custom:"):
                cdays = rep.replace("custom:","").split(",")
                if DAYS[cur_d.weekday()] in cdays and cur_d >= rd: match = True
            if match:
                if cur_d not in events: events[cur_d] = []
                use_st = st if cur_d <= today else "active"
                events[cur_d].append((msg, t, rep, use_st))
            cur_d += timedelta(days=1)
    ws_str = ws.strftime("%-d %b")
    we_str = we.strftime("%-d %b")
    lines = [hdr(f"Week {week_idx+1}: {ws_str}–{we_str}"), ""]
    cur_d = ws
    while cur_d <= we:
        if cur_d in events:
            day_name = DAY_SHORT[DAYS[cur_d.weekday()]]
            date_str = cur_d.strftime("%-d %b")
            prefix = "Today, " if cur_d == today else ""
            lines.append(f"<b>{prefix}{date_str}, {day_name}</b>")
            sorted_evs = sorted(events[cur_d], key=lambda x: x[1])
            for msg, t, rep, s in sorted_evs:
                ic = ST_IC.get(s, "○")
                lines.append(f"  {ic} {msg} · {fmt_time(t)}")
            lines.append("")
        cur_d += timedelta(days=1)
    if not events:
        lines.append("No reminders this week.")
    text = "\n".join(lines)
    month_name = datetime(year, month, 1).strftime("%B %Y")
    nav_btns = []
    if week_idx + 1 < len(weeks):
        nav_btns.append(IKB(f"W{week_idx+2} ›", callback_data=f"mw_{year}_{month}_{week_idx+1}"))
    kb_rows = []
    if nav_btns: kb_rows.append(nav_btns)
    kb_rows.append([IKB(f"« {month_name}", callback_data=f"mn_{year}_{month}")])
    await safe_edit(target, text, IKM(kb_rows))

# ============= REMIND CMD (GROUP) =============
async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Use /add in private chat.\n/remind works in groups only.")
        return
    if user:
        update_username(user)
        uname = user.username or ""
        set_gsub(chat.id, user.id, user.first_name, uname)
    ud = ctx.user_data
    text_after = (update.message.text or "").replace("/remind", "", 1).strip()
    tags = extract_tag_texts(update.message)
    if tags: ud["g_tagged"] = tags
    else: ud.pop("g_tagged", None)
    ud["g_chat"] = chat.id
    if text_after:
        for tg in tags:
            for n in tg.get("names", set()):
                text_after = re.sub(r'@?' + re.escape(n), '', text_after, flags=re.IGNORECASE)
        text_after = re.sub(r'\s+', ' ', text_after).strip()
    if text_after:
        tz = get_tz(user.id)
        parsed = parse_nl_partial(text_after, tz)
        if parsed:
            ud["message"] = parsed["message"] or text_after
            if parsed["date"]: ud["date"] = parsed["date"]
            if parsed["time"]: ud["time"] = parsed["time"]
            if parsed["repeat"]: ud["repeat"] = parsed["repeat"]
            if ud.get("date") and ud.get("time"):
                if is_past(ud["date"], ud["time"], tz):
                    ud["date"] = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
                await save_group_reminder(update.message, ud, ctx.bot, user.id, tz)
                return
            if ud.get("time") and not ud.get("date"):
                if is_past(datetime.now(tz).strftime("%Y-%m-%d"), ud["time"], tz):
                    ud["step"] = "g_date"
                    now = datetime.now(tz)
                    sent = await update.message.reply_text(
                        f"{hdr('Group Reminder')}\n{ud['message']}\n{fmt_time(ud['time'])}\n\n⚠ Time passed. Pick a date:",
                        reply_markup=cal_kb(now.year, now.month, tz, "gcancel"), parse_mode="HTML"
                    )
                    return
                ud["date"] = datetime.now(tz).strftime("%Y-%m-%d")
                await save_group_reminder(update.message, ud, ctx.bot, user.id, tz)
                return
            if ud.get("date") and not ud.get("time"):
                ud["step"] = "g_time"
                sent = await update.message.reply_text(
                    f"{hdr('Group Reminder')}\n{ud['message']}\n{fmt_date(ud['date'])}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
                    parse_mode="HTML", reply_markup=ForceReply(selective=True)
                )
                return
            ud["step"] = "g_date"
            now = datetime.now(tz)
            sent = await update.message.reply_text(
                f"{hdr('Group Reminder')}\n{ud['message']}\n\nPick a date:",
                reply_markup=cal_kb(now.year, now.month, tz, "gcancel"), parse_mode="HTML"
            )
            return
    ud["step"] = "g_message"
    await update.message.reply_text(
        f"{hdr('Group Reminder')}\nEnter message:",
        parse_mode="HTML", reply_markup=ForceReply(selective=True)
    )

# ============= WEEKLY REPORT =============
async def check_weekly_report(ctx: ContextTypes.DEFAULT_TYPE):
    now_utc = datetime.now(pytz.utc)
    cfg_rows = cfg_sheet.get_all_values()
    for i, cr in enumerate(cfg_rows):
        if i == 0: continue
        if len(cr) < 6: continue
        uid = cr[0]
        wr_on = cr[7] if len(cr) > 7 else "true"
        if wr_on != "true": continue
        tz = safe_tz(cr[5] if len(cr) > 5 and cr[5] else DEF_TZ)
        now = now_utc.astimezone(tz)
        if now.weekday() != 6: continue
        if now.strftime("%H:%M") != "09:00": continue
        week_end = now.date()
        week_start = week_end - timedelta(days=6)
        rows = sheet.get_all_values()
        done_count = 0; missed_count = 0; snoozed = 0
        day_done = {}; day_missed = {}
        done_list = []; missed_list = []
        for j, r in enumerate(rows):
            if j == 0: continue
            if str(r[0]) != str(uid): continue
            msg, d, t, rep, st = get_detail(r)
            if not d: continue
            try: rd = datetime.strptime(d, "%Y-%m-%d").date()
            except: continue
            if not (week_start <= rd <= week_end): continue
            if st == "done":
                done_count += 1
                wd = DAYS[rd.weekday()]
                day_done[wd] = day_done.get(wd, 0) + 1
                done_list.append(f"  ✅ {msg} · {fmt_date(d)}")
            elif st == "missed":
                missed_count += 1
                wd = DAYS[rd.weekday()]
                day_missed[wd] = day_missed.get(wd, 0) + 1
                missed_list.append(f"  ✗ {msg} · {fmt_date(d)}")
        total = done_count + missed_count
        if total == 0: continue
        pct = int(done_count / total * 100) if total else 0
        best_day = max(day_done, key=day_done.get) if day_done else None
        worst_day = max(day_missed, key=day_missed.get) if day_missed else None
        if pct >= 90: mood = "Outstanding! 🏆"
        elif pct >= 70: mood = "Keep it up! 💪"
        elif pct >= 50: mood = "Room to improve 📈"
        else: mood = "Let's do better next week 🎯"
        ws_str = week_start.strftime("%-d %b")
        we_str = week_end.strftime("%-d %b")
        lines = [hdr("📊 Weekly Report"), f"{ws_str} — {we_str}", ""]
        lines.append(f"✅ Completed: {done_count}/{total} ({pct}%)")
        lines.append(f"❌ Missed: {missed_count}")
        if best_day: lines.append(f"\n📅 Most Productive: {DAY_NAMES.get(best_day, best_day)}")
        if worst_day: lines.append(f"📉 Most Missed: {DAY_NAMES.get(worst_day, worst_day)}")
        lines.append(f"\n{mood}")
        text = "\n".join(lines)
        detail_data = {"done": done_list, "missed": missed_list, "summary": text}
        bd_key = f"wr_{uid}_{week_start.isoformat()}"
        ctx.bot_data[bd_key] = detail_data
        kb = IKM([[IKB("📋 Details", callback_data=f"wrdet_{uid}_{week_start.isoformat()}")]])
        try: await ctx.bot.send_message(chat_id=int(uid), text=text, reply_markup=kb, parse_mode="HTML")
        except: pass

# ============= DIGEST =============
async def check_digest(ctx: ContextTypes.DEFAULT_TYPE):
    now_utc = datetime.now(pytz.utc)
    cfg_rows = cfg_sheet.get_all_values()
    for i, cr in enumerate(cfg_rows):
        if i == 0: continue
        if len(cr) < 3: continue
        uid = cr[0]
        if cr[1] != "true": continue
        d_time = cr[2] if cr[2] else DEF_DIGEST_TIME
        tz = safe_tz(cr[5] if len(cr)>5 and cr[5] else DEF_TZ)
        now = now_utc.astimezone(tz)
        if now.strftime("%H:%M") != d_time: continue
        today_str = now.strftime("%Y-%m-%d")
        rows = sheet.get_all_values()
        items = []
        for j, r in enumerate(rows):
            if j == 0: continue
            if str(r[0]) != str(uid): continue
            msg, d, t, rep, st = get_detail(r)
            if st not in ("active","snoozed"): continue
            if not d or not t: continue
            show = False
            if d == today_str: show = True
            elif rep == "daily" and d <= today_str: show = True
            elif rep == "weekly":
                try:
                    rd = datetime.strptime(d, "%Y-%m-%d").date()
                    td = datetime.strptime(today_str, "%Y-%m-%d").date()
                    if td >= rd and (td - rd).days % 7 == 0: show = True
                except: pass
            elif rep == "monthly":
                try:
                    rd = datetime.strptime(d, "%Y-%m-%d").date()
                    td = datetime.strptime(today_str, "%Y-%m-%d").date()
                    if td >= rd and td.day == rd.day: show = True
                except: pass
            elif rep.startswith("custom:"):
                cdays = rep.replace("custom:","").split(",")
                wd = DAYS[now.weekday()]
                if wd in cdays and d <= today_str: show = True
            if show: items.append((t, msg))
        if not items: continue
        items.sort()
        date_str = now.strftime("%-d %b")
        lines = [f"☀️ Good morning!\n{hdr('Today — ' + date_str)}", ""]
        for t, msg in items:
            lines.append(f"  {fmt_time(t)} · {msg}")
        lines.append(f"\n{len(items)} reminder{'s' if len(items)!=1 else ''} today")
        text = "\n".join(lines)
        try: await ctx.bot.send_message(chat_id=int(uid), text=text, reply_markup=home_kb(), parse_mode="HTML")
        except: pass

# ============= SCHEDULER =============
async def check_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    now_utc = datetime.now(pytz.utc)
    try:
        all_rows = sheet.get_all_values()
    except:
        try:
            client.login()
            all_rows = sheet.get_all_values()
        except: return
    cfg_rows = cfg_sheet.get_all_values()
    tz_map = {}; cfg_map = {}
    for i, cr in enumerate(cfg_rows):
        if i == 0: continue
        if len(cr) < 6: continue
        tz_map[cr[0]] = safe_tz(cr[5] if cr[5] else DEF_TZ)
        cfg_map[cr[0]] = {
            "max_retries": int(cr[3]) if len(cr)>3 and cr[3] else DEF_RETRIES,
            "retry_gap": int(cr[4]) if len(cr)>4 and cr[4] else DEF_RETRY_GAP
        }
    for i, r in enumerate(all_rows):
        if i == 0: continue
        if len(r) < 7: continue
        st = r[5]
        if st != "active": continue
        uid_s = r[0]
        tz = tz_map.get(uid_s, IST)
        now = now_utc.astimezone(tz)
        now_str = now.strftime("%Y-%m-%d %H:%M")
        d = norm_date(r[2]); t = norm_time(r[3])
        rem_str = f"{d} {t}"
        if rem_str != now_str: continue
        try:
            rem_dt = tz.localize(datetime.strptime(rem_str, "%Y-%m-%d %H:%M"))
            if abs((now - rem_dt).total_seconds()) > 30: continue
        except: continue
        row = i + 1
        gid = r[7] if len(r)>7 else ""
        tid = r[8] if len(r)>8 else ""
        msg = r[1]; rep = r[4]
        cfg = cfg_map.get(uid_s, {"max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP})
        retry_gap = cfg["retry_gap"] * 60
        sheet.update_cell(row, 6, "pending")
        sheet.update_cell(row, 7, 0)
        jobs = ctx.job_queue.get_jobs_by_name(f"retry-{row}")
        for j in jobs: j.schedule_removal()
        if gid and tid:
            members = [m for m in get_tmembers(tid) if m["status"] in ("waiting","pending")]
            for m in members:
                set_tstatus(tid, m["uid"], "pending")
            group_status_text = f"⏰ {msg}\n\n"
            all_members = get_tmembers(tid)
            parts = []
            for m in all_members:
                if m["status"] == "skipped": continue
                ic = GT_IC.get("pending", "⏳")
                parts.append(f"{ic} {m['name']}")
            group_status_text += " · ".join(parts)
            try:
                g_sent = await ctx.bot.send_message(chat_id=int(gid), text=group_status_text, parse_mode="HTML")
                ctx.bot_data[f"gstatus_{tid}"] = {"mid": g_sent.message_id, "cid": int(gid)}
            except: pass
            for m in members:
                text = f"{hdr('⏰ Reminder')}\n{msg}\nFrom group"
                try:
                    sent = await ctx.bot.send_message(chat_id=int(m["uid"]), text=text, reply_markup=rem_kb(row), parse_mode="HTML")
                    store_rem_msg(ctx.bot_data, f"{row}_{m['uid']}", sent.message_id, int(m["uid"]))
                except: pass
            ctx.job_queue.run_once(grp_retry, retry_gap, data={"row": row, "tid": tid, "gid": gid, "uid": uid_s}, name=f"retry-{row}")
        else:
            uid = int(uid_s)
            text = f"{hdr('⏰ Reminder')}\n{msg}"
            try:
                sent = await ctx.bot.send_message(chat_id=uid, text=text, reply_markup=rem_kb(row), parse_mode="HTML")
                store_rem_msg(ctx.bot_data, row, sent.message_id, uid)
            except: pass
            ctx.job_queue.run_once(auto_retry, retry_gap, data={"row": row, "chat": uid}, name=f"retry-{row}")

# ============= AUTO RETRY =============
async def auto_retry(ctx: ContextTypes.DEFAULT_TYPE):
    data = ctx.job.data
    row = data["row"]; chat = data["chat"]
    r = sheet.row_values(row)
    if not r or len(r) < 7: return
    if r[5] != "pending": return
    uid_s = r[0]
    cfg = get_cfg(int(uid_s)) if uid_s else {"max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP}
    max_r = cfg.get("max_retries", DEF_RETRIES)
    gap = cfg.get("retry_gap", DEF_RETRY_GAP)
    try: count = int(r[6])
    except: count = 0
    if count >= max_r:
        sheet.update_cell(row, 6, "missed")
        return
    msg = r[1]
    await rm_old_rem_btns(ctx.bot_data, ctx.bot, row)
    text = f"{hdr('🔔 Reminder')}\n{msg}\n\n<i>Retry {count+1}/{max_r}</i>"
    try:
        sent = await ctx.bot.send_message(chat_id=chat, text=text, reply_markup=rem_kb(row), parse_mode="HTML")
        store_rem_msg(ctx.bot_data, row, sent.message_id, chat)
    except: pass
    sheet.update_cell(row, 7, count + 1)
    if count + 1 < max_r:
        ctx.job_queue.run_once(auto_retry, gap * 60, data={"row": row, "chat": chat}, name=f"retry-{row}")

# ============= GROUP RETRY =============
async def grp_retry(ctx: ContextTypes.DEFAULT_TYPE):
    data = ctx.job.data
    row = data["row"]; tid = data["tid"]; gid = data["gid"]; uid_s = data["uid"]
    r = sheet.row_values(row)
    if not r or len(r) < 7: return
    if r[5] != "pending": return
    cfg = get_cfg(int(uid_s)) if uid_s else {"max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP}
    max_r = cfg.get("max_retries", DEF_RETRIES)
    gap = cfg.get("retry_gap", DEF_RETRY_GAP)
    try: count = int(r[6])
    except: count = 0
    if count >= max_r:
        pending = [m for m in get_tmembers(tid) if m["status"] == "pending"]
        for m in pending: set_tstatus(tid, m["uid"], "missed")
        sheet.update_cell(row, 6, "missed")
        await update_gstatus(ctx, tid, r[1])
        return
    msg = r[1]
    pending = [m for m in get_tmembers(tid) if m["status"] == "pending"]
    for m in pending:
        await rm_old_rem_btns(ctx.bot_data, ctx.bot, f"{row}_{m['uid']}")
        text = f"{hdr('🔔 Reminder')}\n{msg}\nFrom group\n\n<i>Retry {count+1}/{max_r}</i>"
        try:
            sent = await ctx.bot.send_message(chat_id=int(m["uid"]), text=text, reply_markup=rem_kb(row), parse_mode="HTML")
            store_rem_msg(ctx.bot_data, f"{row}_{m['uid']}", sent.message_id, int(m["uid"]))
        except: pass
    sheet.update_cell(row, 7, count + 1)
    if count + 1 < max_r:
        ctx.job_queue.run_once(grp_retry, gap * 60, data=data, name=f"retry-{row}")

async def update_gstatus(ctx, tid, msg):
    d = ctx.bot_data.get(f"gstatus_{tid}")
    if not d: return
    members = get_tmembers(tid)
    active = [(m["uid"], m["name"], m["status"]) for m in members if m["status"] != "skipped"]
    all_done = all(s in ("done","missed") for _, _, s in active)
    if all_done:
        names = ", ".join(n for _, n, _ in active)
        text = f"{msg} · ✅ All done\n{names}"
    else:
        parts = []
        for _, n, s in active:
            ic = GT_IC.get(s, "⏳")
            parts.append(f"{ic} {n}")
        text = f"⏰ {msg}\n\n" + " · ".join(parts)
    try: await ctx.bot.edit_message_text(text, chat_id=d["cid"], message_id=d["mid"], parse_mode="HTML")
    except: pass

# ============= SNOOZE FIRE =============
async def snooze_fire(ctx: ContextTypes.DEFAULT_TYPE):
    data = ctx.job.data
    row = data["row"]; chat = data["chat"]
    r = sheet.row_values(row)
    if not r or len(r) < 6: return
    if r[5] != "snoozed": return
    sheet.update_cell(row, 6, "pending")
    sheet.update_cell(row, 7, 0)
    msg = r[1]
    text = f"{hdr('⏰ Reminder')}\n{msg}"
    try:
        sent = await ctx.bot.send_message(chat_id=chat, text=text, reply_markup=rem_kb(row), parse_mode="HTML")
        store_rem_msg(ctx.bot_data, row, sent.message_id, chat)
    except: pass
    uid_s = r[0]
    cfg = get_cfg(int(uid_s)) if uid_s else {"retry_gap": DEF_RETRY_GAP}
    gap = cfg.get("retry_gap", DEF_RETRY_GAP)
    ctx.job_queue.run_once(auto_retry, gap * 60, data={"row": row, "chat": chat}, name=f"retry-{row}")

# ============= ON TEXT =============
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if user: update_username(user)
    ud = ctx.user_data
    step = ud.get("step")
    text = update.message.text.strip()
    # Group text input
    if chat.type != "private":
        if step in ("g_message", "g_time") and str(ud.get("g_chat")) == str(chat.id):
            if user: set_gsub(chat.id, user.id, user.first_name, user.username or "")
            tz = get_tz(user.id)
            if step == "g_message":
                parsed = parse_nl_partial(text, tz)
                if parsed:
                    ud["message"] = parsed["message"] or text
                    if parsed["date"]: ud["date"] = parsed["date"]
                    if parsed["time"]: ud["time"] = parsed["time"]
                    if parsed["repeat"]: ud["repeat"] = parsed["repeat"]
                else:
                    ud["message"] = text
                if ud.get("date") and ud.get("time"):
                    if is_past(ud["date"], ud["time"], tz):
                        ud["date"] = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
                    await save_group_reminder(update.message, ud, ctx.bot, user.id, tz)
                    return
                if ud.get("time") and not ud.get("date"):
                    if is_past(datetime.now(tz).strftime("%Y-%m-%d"), ud["time"], tz):
                        ud["step"] = "g_date"
                        now = datetime.now(tz)
                        await update.message.reply_text(
                            f"{hdr('Group Reminder')}\n{ud['message']}\n{fmt_time(ud['time'])}\n\n⚠ Time passed. Pick date:",
                            reply_markup=cal_kb(now.year, now.month, tz, "gcancel"), parse_mode="HTML"
                        )
                        return
                    ud["date"] = datetime.now(tz).strftime("%Y-%m-%d")
                    await save_group_reminder(update.message, ud, ctx.bot, user.id, tz)
                    return
                if ud.get("date") and not ud.get("time"):
                    ud["step"] = "g_time"
                    await update.message.reply_text(
                        f"{hdr('Group Reminder')}\n{ud['message']}\n{fmt_date(ud['date'])}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM</i>",
                        parse_mode="HTML", reply_markup=ForceReply(selective=True)
                    )
                    return
                ud["step"] = "g_date"
                now = datetime.now(tz)
                await update.message.reply_text(
                    f"{hdr('Group Reminder')}\n{ud['message']}\n\nPick a date:",
                    reply_markup=cal_kb(now.year, now.month, tz, "gcancel"), parse_mode="HTML"
                )
                return
            elif step == "g_time":
                t = parse_time_input(text)
                if not t:
                    await update.message.reply_text("⚠ Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML", reply_markup=ForceReply(selective=True))
                    return
                d = ud.get("date", datetime.now(tz).strftime("%Y-%m-%d"))
                if is_past(d, t, tz):
                    await update.message.reply_text(past_msg(t), parse_mode="HTML", reply_markup=ForceReply(selective=True))
                    return
                ud["time"] = t
                await save_group_reminder(update.message, ud, ctx.bot, user.id, tz)
                return
        return
    # Private text input
    tz = get_tz(user.id)
    if step == "message":
        await del_prompt(ud, ctx.bot)
        parsed = parse_nl_partial(text, tz)
        if parsed:
            ud["message"] = parsed["message"] or text
            if parsed["date"]: ud["date"] = parsed["date"]
            if parsed["time"]: ud["time"] = parsed["time"]
            if parsed["repeat"]: ud["repeat"] = parsed["repeat"]
        else:
            ud["message"] = text
        if ud.get("date") and ud.get("time"):
            if is_past(ud["date"], ud["time"], tz):
                ud["date"] = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
            await save_reminder(update.message, ud, ctx.bot, user.id, tz)
            return
        if ud.get("time") and not ud.get("date"):
            today_str = datetime.now(tz).strftime("%Y-%m-%d")
            if is_past(today_str, ud["time"], tz):
                ud["step"] = "date"
                now = datetime.now(tz)
                sent = await update.message.reply_text(
                    f"{hdr('New Reminder')}\n{ud['message']}\n{fmt_time(ud['time'])}\n\n⚠ Time passed. Pick date:",
                    reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML"
                )
                store_prompt(ud, sent)
                return
            ud["date"] = today_str
            await save_reminder(update.message, ud, ctx.bot, user.id, tz)
            return
        if ud.get("date") and not ud.get("time"):
            ud["step"] = "time"
            sent = await update.message.reply_text(
                f"{hdr('New Reminder')}\n{ud['message']}\n{fmt_date(ud['date'])}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
                parse_mode="HTML"
            )
            store_prompt(ud, sent)
            return
        ud["step"] = "date"
        now = datetime.now(tz)
        sent = await update.message.reply_text(
            f"{hdr('New Reminder')}\n{ud['message']}\n\nPick a date:",
            reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML"
        )
        store_prompt(ud, sent)
        return
    elif step == "time":
        await del_prompt(ud, ctx.bot)
        t = parse_time_input(text)
        if not t:
            sent = await update.message.reply_text("⚠ Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML")
            store_prompt(ud, sent)
            return
        d = ud.get("date", datetime.now(tz).strftime("%Y-%m-%d"))
        if is_past(d, t, tz):
            sent = await update.message.reply_text(past_msg(t), parse_mode="HTML")
            store_prompt(ud, sent)
            return
        ud["time"] = t
        await save_reminder(update.message, ud, ctx.bot, user.id, tz)
        return
    elif step == "edit_msg":
        await del_prompt(ud, ctx.bot)
        row = ud.get("editing_row")
        if row:
            sheet.update_cell(row, 2, text)
            r = sheet.row_values(row)
            msg, d, t, rep, st = get_detail(r)
            sent = await update.message.reply_text(
                f"{hdr('Updated ✓')}\n{detail(text, d, t, rep)}",
                reply_markup=home_kb(), parse_mode="HTML"
            )
            store_home(ud, sent)
        ud.pop("step", None); ud.pop("editing_row", None)
        return
    elif step == "edit_time":
        await del_prompt(ud, ctx.bot)
        row = ud.get("editing_row")
        t = parse_time_input(text)
        if not t:
            sent = await update.message.reply_text("⚠ Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML", reply_markup=IKM([[IKB("« Back", callback_data=f"edit_{row}")]]))
            store_prompt(ud, sent)
            return
        if row:
            r = sheet.row_values(row)
            d = norm_date(r[2]) if len(r)>2 else ""
            tz2 = get_tz(user.id)
            if d and is_past(d, t, tz2):
                sent = await update.message.reply_text(past_msg(t), parse_mode="HTML", reply_markup=IKM([[IKB("« Back", callback_data=f"edit_{row}")]]))
                store_prompt(ud, sent)
                return
            sheet.update_cell(row, 4, t)
            r = sheet.row_values(row)
            msg, d, t2, rep, st = get_detail(r)
            sent = await update.message.reply_text(
                f"{hdr('Updated ✓')}\n{detail(msg, d, t2, rep)}",
                reply_markup=home_kb(), parse_mode="HTML"
            )
            store_home(ud, sent)
        ud.pop("step", None); ud.pop("editing_row", None)
        return
    elif step == "cfg_dtime":
        await del_prompt(ud, ctx.bot)
        t = parse_time_input(text)
        if not t:
            sent = await update.message.reply_text("⚠ Invalid time.\n<i>e.g. 7am, 8:30 AM</i>", parse_mode="HTML")
            store_prompt(ud, sent)
            return
        save_cfg(user.id, "digest_time", t)
        ud.pop("step", None)
        await show_settings(update.message, ctx, user.id, new=True)
        return
    elif step is None:
        parsed = parse_nl_partial(text, tz)
        if not parsed: return
        ud["message"] = parsed["message"]
        if parsed["date"]: ud["date"] = parsed["date"]
        if parsed["time"]: ud["time"] = parsed["time"]
        if parsed["repeat"]: ud["repeat"] = parsed["repeat"]
        if ud.get("date") and ud.get("time"):
            if is_past(ud["date"], ud["time"], tz):
                ud["date"] = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
            await save_reminder(update.message, ud, ctx.bot, user.id, tz)
            return
        if ud.get("time") and not ud.get("date"):
            today_str = datetime.now(tz).strftime("%Y-%m-%d")
            if is_past(today_str, ud["time"], tz):
                ud["step"] = "date"
                now = datetime.now(tz)
                sent = await update.message.reply_text(
                    f"{hdr('New Reminder')}\n{ud['message']}\n{fmt_time(ud['time'])}\n\n⚠ Time passed. Pick date:",
                    reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML"
                )
                store_prompt(ud, sent)
                return
            ud["date"] = today_str
            await save_reminder(update.message, ud, ctx.bot, user.id, tz)
            return
        if ud.get("date") and not ud.get("time"):
            ud["step"] = "time"
            sent = await update.message.reply_text(
                f"{hdr('New Reminder')}\n{ud['message']}\n{fmt_date(ud['date'])}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
                parse_mode="HTML"
            )
            store_prompt(ud, sent)
            return
        ud["step"] = "date"
        now = datetime.now(tz)
        sent = await update.message.reply_text(
            f"{hdr('New Reminder')}\n{ud['message']}\n\nPick a date:",
            reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML"
        )
        store_prompt(ud, sent)
        return

# ============= ON BUTTON =============
async def on_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    user = q.from_user
    ud = ctx.user_data
    if user: update_username(user)

    # HOME / NAV
    if d == "add":
        await rm_home(ud, ctx.bot)
        ud.clear()
        ud["step"] = "message"
        sent = await q.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", reply_markup=IKM([[IKB("✕ Cancel", callback_data="cancel")]]), parse_mode="HTML")
        store_prompt(ud, sent)
        return
    if d == "cancel":
        await rm_prompt(ud, ctx.bot)
        ud.clear()
        sent = await q.message.reply_text(f"Cancelled.\n\n{home_text()}", reply_markup=home_kb(), parse_mode="HTML")
        store_home(ud, sent)
        return
    if d == "back_home":
        ud.clear()
        await safe_edit(q.message, f"{hdr('Smart Reminder Bot')}\n\n{home_text()}", home_kb())
        store_home(ud, q.message)
        return
    if d == "noop": return

    # CLOSE / MINIMIZE (private)
    if d == "pclose_info":
        cid = q.message.chat_id
        text_data = ctx.bot_data.get(f"pinfo_{cid}", "")
        min_text = "<b>ℹ️ Info</b>"
        show_cb = f"pshow_info_{cid}"
        cancel_minimize(ctx, cid, q.message.message_id)
        await safe_edit(q.message, min_text, IKM([[IKB("📋 Show", callback_data=show_cb)]]))
        return
    if d.startswith("pshow_info_"):
        cid = int(d.replace("pshow_info_", ""))
        text_data = ctx.bot_data.get(f"pinfo_{cid}", "")
        if text_data:
            await safe_edit(q.message, text_data, IKM([[IKB("✕ Close", callback_data="pclose_info")], [IKB("＋ New", callback_data="add")]]))
        return
    if d.startswith("pshow_list_"):
        uid = int(d.replace("pshow_list_", ""))
        await show_list(q.message, ctx, uid, new=False)
        return
    if d.startswith("pshow_month_"):
        parts = d.replace("pshow_month_", "").split("_")
        uid = int(parts[0]); y = int(parts[1]); m = int(parts[2])
        await show_month(q.message, ctx, uid, y, m, new=False)
        return

    # GROUP CLOSE/SHOW
    if d == "gclose":
        mid = q.message.message_id
        gmin = ctx.bot_data.get(f"gmin_{mid}")
        if gmin:
            min_text = gmin.get("min_text", "<b>Bot</b>")
            show_cb = gmin.get("show_cb", "noop")
            await safe_edit(q.message, min_text, IKM([[IKB("📋 Show", callback_data=show_cb)]]))
        else:
            await safe_edit(q.message, "<b>Smart Reminder Bot</b>", IKM([[IKB("📋 Show", callback_data="noop")]]))
        return
    if d.startswith("gshow_start_"):
        mid = int(d.replace("gshow_start_", ""))
        gmin = ctx.bot_data.get(f"gmin_{mid}")
        if gmin:
            await safe_edit(q.message, gmin["text"], IKM([[IKB("✕ Close", callback_data="gclose")]]))
        return
    if d.startswith("gshow_list_"):
        parts = d.replace("gshow_list_", "").rsplit("_", 1)
        gid_s = parts[0]
        rows = sheet.get_all_values()
        items = []
        for i, r in enumerate(rows):
            if i == 0: continue
            if len(r) < 8: continue
            if str(r[7]) != gid_s: continue
            st = r[5] if len(r)>5 else ""
            if st not in ("active","pending","snoozed"): continue
            msg, dd, t, rep, _ = get_detail(r)
            items.append((i+1, msg, dd, t, rep, st))
        if not items:
            await safe_edit(q.message, f"{hdr('Group Reminders')}\n\nNo active reminders.", IKM([[IKB("✕ Close", callback_data="gclose")]]))
            return
        lines = [hdr("Group Reminders"), ""]
        for idx, (row, msg, dd, t, rep, st) in enumerate(items):
            ic = ST_IC.get(st, "○")
            short_msg = msg[:30] + "…" if len(msg) > 30 else msg
            lines.append(f"{idx+1} {ic} {short_msg}\n   {fmt_date(dd)} · {fmt_time(t)}")
        text = "\n".join(lines)
        await safe_edit(q.message, text, IKM([[IKB("✕ Close", callback_data="gclose")]]))
        return
    if d == "gcancel":
        ud.clear()
        try: await q.message.edit_text("Cancelled.", parse_mode="HTML")
        except: pass
        return

    # CALENDAR
    if d.startswith("cal_"):
        parts = d.replace("cal_", "").split("-")
        y, m = int(parts[0]), int(parts[1])
        tz = get_tz(user.id)
        step = ud.get("step")
        back_cb = "gcancel" if step and step.startswith("g_") else "cancel"
        if step == "edit_date":
            back_cb = f"edit_{ud.get('editing_row','')}"
        await safe_edit(q.message, q.message.text, cal_kb(y, m, tz, back_cb))
        return
    if d.startswith("day_"):
        ds = d.replace("day_", "")
        tz = get_tz(user.id)
        step = ud.get("step")
        # Edit date
        if step == "edit_date":
            row = ud.get("editing_row")
            if row:
                r = sheet.row_values(row)
                t = norm_time(r[3]) if len(r)>3 else ""
                if t and is_past(ds, t, tz):
                    now = datetime.now(tz)
                    txt = f"⚠ {fmt_time(t)} has already passed on {fmt_date(ds)}.\nPick a future date:"
                    await safe_edit(q.message, txt, cal_kb(now.year, now.month, tz, f"edit_{row}"))
                    return
                sheet.update_cell(row, 3, ds)
                r = sheet.row_values(row)
                msg, dd, t2, rep, st = get_detail(r)
                sent_text = f"{hdr('Updated ✓')}\n{detail(msg, dd, t2, rep)}"
                await safe_edit(q.message, sent_text, home_kb())
                store_home(ud, q.message)
            ud.pop("step", None); ud.pop("editing_row", None)
            return
        # Group date
        if step == "g_date":
            ud["date"] = ds
            if ud.get("time"):
                if is_past(ds, ud["time"], tz):
                    now = datetime.now(tz)
                    await safe_edit(q.message, f"⚠ {fmt_time(ud['time'])} passed on {fmt_date(ds)}. Pick another:", cal_kb(now.year, now.month, tz, "gcancel"))
                    return
                await save_group_reminder(q.message, ud, ctx.bot, user.id, tz)
                return
            ud["step"] = "g_time"
            await safe_edit(q.message, f"{hdr('Group Reminder')}\n{ud.get('message','')}\n{fmt_date(ds)}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM</i>", None)
            return
        # New reminder date
        ud["date"] = ds
        if ud.get("time"):
            if is_past(ds, ud["time"], tz):
                now = datetime.now(tz)
                await safe_edit(q.message, f"⚠ {fmt_time(ud['time'])} passed on {fmt_date(ds)}. Pick another:", cal_kb(now.year, now.month, tz))
                return
            await save_reminder(q.message, ud, ctx.bot, user.id, tz)
            return
        ud["step"] = "time"
        sent_text = f"{hdr('New Reminder')}\n{ud.get('message','')}\n{fmt_date(ds)}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>"
        await safe_edit(q.message, sent_text, None)
        store_prompt(ud, q.message)
        return

    # VIEW REMINDER
    if d.startswith("view_"):
        row = int(d.replace("view_", ""))
        r = sheet.row_values(row)
        if not r:
            await safe_edit(q.message, "Reminder not found.", IKM([[IKB("« Back", callback_data="back_home")]]))
            return
        msg, dd, t, rep, st = get_detail(r)
        ic = ST_IC.get(st, "○")
        text = f"{hdr('Reminder')}\n{msg}\n\n{fmt_date(dd)} · {fmt_time(t)}\n{fmt_repeat(rep)} · {ic} {st.title()}"
        btns = []
        if st in ("active","pending","snoozed"):
            btns.append([IKB("✎ Edit", callback_data=f"edit_{row}"), IKB("✕ Cancel", callback_data=f"crem_{row}")])
        elif st == "missed":
            btns.append([IKB("✕ Remove", callback_data=f"crem_{row}")])
        btns.append([IKB("« Back", callback_data="list_refresh")])
        await safe_edit(q.message, text, IKM(btns))
        return
    if d == "list_refresh":
        await show_list(q.message, ctx, user.id, new=False)
        return

    # EDIT
    if d.startswith("edit_"):
        row = int(d.replace("edit_", ""))
        r = sheet.row_values(row)
        if not r:
            await safe_edit(q.message, "Not found.", home_kb())
            return
        msg, dd, t, rep, st = get_detail(r)
        text = f"{hdr('Edit Reminder')}\n{detail(msg, dd, t, rep)}\n\nWhat to change?"
        kb = IKM([
            [IKB("Message", callback_data=f"emsg_{row}"), IKB("Date", callback_data=f"edate_{row}"), IKB("Time", callback_data=f"etime_{row}")],
            [IKB("« Back", callback_data=f"view_{row}")]
        ])
        await safe_edit(q.message, text, kb)
        return
    if d.startswith("emsg_"):
        row = int(d.replace("emsg_", ""))
        r = sheet.row_values(row)
        msg, dd, t, rep, st = get_detail(r)
        ud["step"] = "edit_msg"; ud["editing_row"] = row
        text = f"{hdr('Edit Message')}\n<i>Current: {msg}</i>\n{fmt_date(dd)} · {fmt_time(t)} · {fmt_repeat(rep)}\n\nEnter new message:"
        await safe_edit(q.message, text, IKM([[IKB("« Back", callback_data=f"edit_{row}")]]))
        store_prompt(ud, q.message)
        return
    if d.startswith("edate_"):
        row = int(d.replace("edate_", ""))
        r = sheet.row_values(row)
        msg, dd, t, rep, st = get_detail(r)
        ud["step"] = "edit_date"; ud["editing_row"] = row
        tz = get_tz(user.id); now = datetime.now(tz)
        text = f"{hdr('Edit Date')}\n{msg}\n<i>Current: {fmt_date(dd)} · {fmt_time(t)}</i>\n\nPick new date:"
        await safe_edit(q.message, text, cal_kb(now.year, now.month, tz, f"edit_{row}"))
        return
    if d.startswith("etime_"):
        row = int(d.replace("etime_", ""))
        r = sheet.row_values(row)
        msg, dd, t, rep, st = get_detail(r)
        ud["step"] = "edit_time"; ud["editing_row"] = row
        text = f"{hdr('Edit Time')}\n{msg}\n<i>Current: {fmt_date(dd)} · {fmt_time(t)}</i>\n\nEnter new time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>"
        await safe_edit(q.message, text, IKM([[IKB("« Back", callback_data=f"edit_{row}")]]))
        store_prompt(ud, q.message)
        return

    # CANCEL REMINDER
    if d.startswith("crem_"):
        row = int(d.replace("crem_", ""))
        jobs = ctx.job_queue.get_jobs_by_name(f"retry-{row}")
        for j in jobs: j.schedule_removal()
        sheet.update_cell(row, 6, "cancelled")
        r = sheet.row_values(row)
        msg, dd, t, rep, st = get_detail(r)
        await safe_edit(q.message, f"{hdr('Cancelled ✕')}\n{detail(msg, dd, t, rep)}", home_kb())
        store_home(ud, q.message)
        return

    # SNOOZE
    if d.startswith("snzp_"):
        row = int(d.replace("snzp_", ""))
        r = sheet.row_values(row)
        if not r or len(r) < 6 or r[5] != "pending":
            await safe_edit(q.message, f"{q.message.text}\n\n<i>Already handled</i>", None)
            return
        await safe_edit(q.message, q.message.text, snz_kb(row))
        return
    if d.startswith("snzb_"):
        row = int(d.replace("snzb_", ""))
        await safe_edit(q.message, q.message.text, rem_kb(row))
        return
    if d.startswith("snz_"):
        parts = d.replace("snz_", "").split("_")
        row = int(parts[0]); mins = int(parts[1])
        r = sheet.row_values(row)
        if not r or len(r) < 6 or r[5] != "pending":
            await safe_edit(q.message, f"{q.message.text}\n\n<i>Already handled</i>", None)
            return
        jobs = ctx.job_queue.get_jobs_by_name(f"retry-{row}")
        for j in jobs: j.schedule_removal()
        tz = get_tz(user.id)
        snooze_time = datetime.now(tz) + timedelta(minutes=mins)
        rep = r[4] if len(r)>4 else "none"
        msg = r[1]
        gid = r[7] if len(r)>7 else ""
        tid = r[8] if len(r)>8 else ""
        if rep != "none" and not gid:
            sheet.update_cell(row, 6, "snoozed")
            sheet.update_cell(row, 7, 0)
            ctx.job_queue.run_once(snooze_fire, mins * 60, data={"row": row, "chat": user.id}, name=f"retry-{row}")
        else:
            sheet.update_cell(row, 3, snooze_time.strftime("%Y-%m-%d"))
            sheet.update_cell(row, 4, snooze_time.strftime("%H:%M"))
            sheet.update_cell(row, 6, "active")
            sheet.update_cell(row, 7, 0)
        label = f"{mins}m" if mins < 60 else f"{mins//60}h"
        snz_time_str = fmt_time(snooze_time.strftime("%H:%M"))
        await safe_edit(q.message, f"{hdr('Snoozed')}\n{msg}\n\nSnoozed {label} → {snz_time_str}", home_kb())
        store_home(ud, q.message)
        if gid and tid:
            set_tstatus(tid, str(user.id), "snoozed")
            await update_gstatus(ctx, tid, msg)
        return

    # DONE
    if d.startswith("done_"):
        row = int(d.replace("done_", ""))
        r = sheet.row_values(row)
        if not r or len(r) < 6 or r[5] != "pending":
            await safe_edit(q.message, f"{q.message.text}\n\n<i>Already handled</i>", None)
            return
        jobs = ctx.job_queue.get_jobs_by_name(f"retry-{row}")
        for j in jobs: j.schedule_removal()
        msg = r[1]; rep = r[4] if len(r)>4 else "none"
        gid = r[7] if len(r)>7 else ""
        tid = r[8] if len(r)>8 else ""
        if gid and tid:
            set_tstatus(tid, str(user.id), "done")
            all_members = [m for m in get_tmembers(tid) if m["status"] not in ("skipped",)]
            all_done = all(m["status"] in ("done","missed") for m in all_members)
            if all_done:
                advance_rep(row, r)
            await update_gstatus(ctx, tid, msg)
        else:
            advance_rep(row, r)
        dd = norm_date(r[2]) if len(r)>2 else ""
        t = norm_time(r[3]) if len(r)>3 else ""
        await safe_edit(q.message, f"{hdr('Done ✓')}\n{detail(msg, dd, t, rep)}", home_kb())
        store_home(ud, q.message)
        return

    # REPEAT CHANGE
    if d.startswith("chrep_do_"):
        parts = d.replace("chrep_do_", "").split("_", 1)
        row = int(parts[0]); rep = parts[1]
        sheet.update_cell(row, 5, rep)
        r = sheet.row_values(row)
        msg, dd, t, rep2, st = get_detail(r)
        await safe_edit(q.message, f"{hdr('Updated ✓')}\n{detail(msg, dd, t, rep2)}", home_kb())
        store_home(ud, q.message)
        return
    if d.startswith("chrep_"):
        row = int(d.replace("chrep_", ""))
        await safe_edit(q.message, q.message.text + "\n\nRepeat?", repeat_kb(row))
        return
    if d.startswith("cust_"):
        val = d.replace("cust_", "")
        ud["cust_row"] = val
        ud["cust_days"] = set()
        await safe_edit(q.message, q.message.text, custom_day_kb(set()))
        return
    if d.startswith("cday_"):
        action = d.replace("cday_", "")
        sel = ud.get("cust_days", set())
        if action == "wkday": sel = {"mon","tue","wed","thu","fri"}
        elif action == "all": sel = set(DAYS)
        elif action == "clear": sel = set()
        elif action == "back":
            row_val = ud.get("cust_row", "new")
            if row_val == "new":
                await safe_edit(q.message, q.message.text, repeat_kb())
            else:
                await safe_edit(q.message, q.message.text, repeat_kb(int(row_val)))
            return
        elif action == "save":
            if not sel: return
            rep = "custom:" + ",".join(sorted(sel, key=DAYS.index))
            row_val = ud.get("cust_row", "new")
            if row_val != "new":
                row = int(row_val)
                sheet.update_cell(row, 5, rep)
                r = sheet.row_values(row)
                msg, dd, t, rep2, st = get_detail(r)
                await safe_edit(q.message, f"{hdr('Updated ✓')}\n{detail(msg, dd, t, rep2)}", home_kb())
                store_home(ud, q.message)
            else:
                ud["repeat"] = rep
                tz = get_tz(user.id)
                await save_reminder(q.message, ud, ctx.bot, user.id, tz)
            ud.pop("cust_days", None); ud.pop("cust_row", None)
            return
        elif action in DAYS:
            if action in sel: sel.discard(action)
            else: sel.add(action)
        ud["cust_days"] = sel
        await safe_edit(q.message, q.message.text, custom_day_kb(sel))
        return

    # GROUP JOIN/SKIP
    if d.startswith("gjoin_"):
        tid = d.replace("gjoin_", "")
        uid_s = str(user.id)
        members = get_tmembers(tid)
        already = any(m["uid"] == uid_s and m["status"] != "skipped" for m in members)
        if not already:
            skipped = [m for m in members if m["uid"] == uid_s and m["status"] == "skipped"]
            if skipped:
                set_tstatus(tid, uid_s, "waiting")
            else:
                add_tmember(tid, uid_s, user.first_name, "waiting")
        set_gsub(q.message.chat_id, user.id, user.first_name, user.username or "")
        rows = sheet.get_all_values()
        rem_row = None
        for i, r in enumerate(rows):
            if i == 0: continue
            if len(r) > 8 and r[8] == tid:
                rem_row = r; break
        if rem_row:
            msg, dd, t, rep, st = get_detail(rem_row)
            active = [m for m in get_tmembers(tid) if m["status"] != "skipped"]
            names = ", ".join(m["name"] for m in active)
            sub_line = f"{len(active)} subscribed: {names}" if active else "0 subscribed"
            text = f"{hdr('Group Reminder')}\n{detail(msg, dd, t, rep)}\n\n{sub_line}"
            rep_btn = []
            if rep == "none":
                for i2, r2 in enumerate(rows):
                    if i2 == 0: continue
                    if len(r2)>8 and r2[8] == tid:
                        rep_btn = [[IKB("🔁 Repeat", callback_data=f"chrep_{i2+1}")]]
                        break
            kb = [[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")]] + rep_btn
            await safe_edit(q.message, text, IKM(kb))
        return
    if d.startswith("gskip_"):
        tid = d.replace("gskip_", "")
        uid_s = str(user.id)
        members = get_tmembers(tid)
        exists = any(m["uid"] == uid_s for m in members)
        if exists:
            set_tstatus(tid, uid_s, "skipped")
        if user and user.username:
            set_gsub(q.message.chat_id, user.id, user.first_name, user.username)
        rows = sheet.get_all_values()
        rem_row = None
        for i, r in enumerate(rows):
            if i == 0: continue
            if len(r)>8 and r[8] == tid:
                rem_row = r; break
        if rem_row:
            msg, dd, t, rep, st = get_detail(rem_row)
            active = [m for m in get_tmembers(tid) if m["status"] != "skipped"]
            names = ", ".join(m["name"] for m in active)
            sub_line = f"{len(active)} subscribed: {names}" if active else "0 subscribed"
            text = f"{hdr('Group Reminder')}\n{detail(msg, dd, t, rep)}\n\n{sub_line}"
            rep_btn = []
            if rep == "none":
                for i2, r2 in enumerate(rows):
                    if i2 == 0: continue
                    if len(r2)>8 and r2[8] == tid:
                        rep_btn = [[IKB("🔁 Repeat", callback_data=f"chrep_{i2+1}")]]
                        break
            kb = [[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")]] + rep_btn
            await safe_edit(q.message, text, IKM(kb))
        return

    # SETTINGS
    if d == "cfg_digest":
        cfg = get_cfg(user.id)
        new_val = "false" if cfg["digest_on"] == "true" else "true"
        save_cfg(user.id, "digest_on", new_val)
        await show_settings(q.message, ctx, user.id, new=False)
        return
    if d == "cfg_dtime":
        ud["step"] = "cfg_dtime"
        sent = await q.message.reply_text("Enter digest time:\n<i>e.g. 7am, 8:30 AM</i>", parse_mode="HTML")
        store_prompt(ud, sent)
        return
    if d == "cfg_report":
        cfg = get_cfg(user.id)
        new_val = "false" if cfg.get("weekly_report","true") == "true" else "true"
        save_cfg(user.id, "weekly_report", new_val)
        await show_settings(q.message, ctx, user.id, new=False)
        return
    if d == "cfg_retries":
        opts = [1,2,3,5,7,10]
        kb = IKM([[IKB(f"{n}×", callback_data=f"cfg_r_{n}") for n in opts[:3]], [IKB(f"{n}×", callback_data=f"cfg_r_{n}") for n in opts[3:]], [IKB("« Back", callback_data="cfg_back")]])
        await safe_edit(q.message, f"{hdr('Max Retries')}\n\nPick:", kb)
        return
    if d.startswith("cfg_r_"):
        n = int(d.replace("cfg_r_", ""))
        save_cfg(user.id, "max_retries", n)
        await show_settings(q.message, ctx, user.id, new=False)
        return
    if d == "cfg_gap":
        opts = [5,10,15,20,30,60]
        labels = ["5m","10m","15m","20m","30m","1h"]
        kb = IKM([[IKB(labels[i], callback_data=f"cfg_g_{opts[i]}") for i in range(3)], [IKB(labels[i], callback_data=f"cfg_g_{opts[i]}") for i in range(3,6)], [IKB("« Back", callback_data="cfg_back")]])
        await safe_edit(q.message, f"{hdr('Retry Gap')}\n\nPick:", kb)
        return
    if d.startswith("cfg_g_"):
        n = int(d.replace("cfg_g_", ""))
        save_cfg(user.id, "retry_gap", n)
        await show_settings(q.message, ctx, user.id, new=False)
        return
    if d == "cfg_tz":
        rows = []
        for region, icon in TZ_ICONS.items():
            rows.append([IKB(f"{icon} {region}", callback_data=f"tzr_{region}")])
        rows.append([IKB("« Back", callback_data="cfg_back")])
        await safe_edit(q.message, f"{hdr('Timezone')}\n\nPick region:", IKM(rows))
        return
    if d.startswith("tzr_"):
        region = d.replace("tzr_", "")
        tzs = TZ_DATA.get(region, [])
        cfg = get_cfg(user.id)
        cur_tz = cfg["timezone"]
        rows = []
        for tz_id, lbl, off in tzs:
            marker = "→ " if tz_id == cur_tz else ""
            rows.append([IKB(f"{marker}{lbl} ({off})", callback_data=f"tzs_{tz_id}")])
        rows.append([IKB("« Regions", callback_data="cfg_tz")])
        await safe_edit(q.message, f"{hdr('Timezone — ' + region)}\n\nPick:", IKM(rows))
        return
    if d.startswith("tzs_"):
        tz_id = d.replace("tzs_", "")
        save_cfg(user.id, "timezone", tz_id)
        await show_settings(q.message, ctx, user.id, new=False)
        return
    if d == "cfg_back":
        await show_settings(q.message, ctx, user.id, new=False)
        return

    # MONTH
    if d.startswith("mw_"):
        parts = d.replace("mw_", "").split("_")
        y = int(parts[0]); m = int(parts[1]); wi = int(parts[2])
        await show_week(q.message, ctx, user.id, y, m, wi)
        return
    if d.startswith("mn_"):
        parts = d.replace("mn_", "").split("_")
        y = int(parts[0]); m = int(parts[1])
        await show_month(q.message, ctx, user.id, y, m, new=False)
        return

    # WEEKLY REPORT DETAIL
    if d.startswith("wrdet_"):
        key = d.replace("wrdet_", "")
        bd_key = f"wr_{key}"
        data = ctx.bot_data.get(bd_key)
        if not data:
            await q.message.reply_text("Report data expired.", parse_mode="HTML")
            return
        lines = [data["summary"], ""]
        if data.get("done"):
            lines.append("\n<b>Completed:</b>")
            lines.extend(data["done"][:20])
        if data.get("missed"):
            lines.append("\n<b>Missed:</b>")
            lines.extend(data["missed"][:20])
        text = "\n".join(lines)
        await safe_edit(q.message, text, IKM([[IKB("« Summary", callback_data=f"wrsm_{key}")]]))
        return
    if d.startswith("wrsm_"):
        key = d.replace("wrsm_", "")
        bd_key = f"wr_{key}"
        data = ctx.bot_data.get(bd_key)
        if data:
            await safe_edit(q.message, data["summary"], IKM([[IKB("📋 Details", callback_data=f"wrdet_{key}")]]))
        return

# ============= MAIN =============
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("add", "New reminder"),
        BotCommand("list", "All reminders"),
        BotCommand("month", "Monthly schedule"),
        BotCommand("settings", "Bot settings"),
        BotCommand("info", "About this bot"),
    ], scope={"type": "all_private_chats"})
    await app.bot.set_my_commands([
        BotCommand("start", "Bot info & commands"),
        BotCommand("remind", "Group reminder"),
        BotCommand("list", "Active reminders"),
    ], scope={"type": "all_group_chats"})

def main():
    app = Application.builder().token(TOKEN).job_queue(JobQueue()).post_init(post_init).build()
    for cmd, fn in [("start",start),("add",add_cmd),("list",list_cmd),("info",info_cmd),("settings",settings_cmd),("month",month_cmd),("remind",remind_cmd)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(check_reminders, interval=60, first=0)
    app.job_queue.run_repeating(check_digest, interval=60, first=30)
    app.job_queue.run_repeating(check_weekly_report, interval=60, first=45)
    print("🚀 Bot Running")
    app.run_polling()

if __name__ == "__main__":
    main()
