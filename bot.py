import logging, os, json, re, time as _time
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM, ForceReply, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, JobQueue
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ========== CONFIG ==========
TOKEN = "8235103406:AAFYJ2SNRW4A4AAEyz8t2h-5BeYk8rnzzwE"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1bHeyDgw9P-3iRLOp_6VpHGKSn9St6yjyqP-35hPg6Rs/edit?pli=1&gid=0#gid=0"

DEF_TZ = "Asia/Kolkata"
DEF_RETRIES = 3
DEF_RETRY_GAP = 10
DEF_DIGEST_TIME = "07:00"
DAYS = ["mon","tue","wed","thu","fri","sat","sun"]
DAY_NAMES = {"mon":"Monday","tue":"Tuesday","wed":"Wednesday","thu":"Thursday","fri":"Friday","sat":"Saturday","sun":"Sunday"}
DAY_SHORT = {"monday":"mon","tuesday":"tue","wednesday":"wed","thursday":"thu","friday":"fri","saturday":"sat","sunday":"sun",
             "mon":"mon","tue":"tue","wed":"wed","thu":"thu","fri":"fri","sat":"sat","sun":"sun"}
ST_IC = {"active":"○","pending":"●","snoozed":"◐","done":"✅","missed":"✗","cancelled":"—"}
ST_LB = {"active":"Active","pending":"Pending","snoozed":"Snoozed","done":"Done","missed":"Missed","cancelled":"Cancelled"}
GT_IC = {"waiting":"⏳","pending":"⏳","done":"✅","snoozed":"⏳","missed":"✗","skipped":"⏭"}
SNOOZE_OPTIONS = [("15m",15),("30m",30),("45m",45),("1h",60),("2h",120),("3h",180),("5h",300),("8h",480),("12h",720)]
TZ_DATA = {
    "Asia":[("India","Asia/Kolkata"),("UAE","Asia/Dubai"),("Pakistan","Asia/Karachi"),("Bangladesh","Asia/Dhaka"),
            ("Thailand","Asia/Bangkok"),("Singapore","Asia/Singapore"),("China","Asia/Shanghai"),("Japan","Asia/Tokyo"),
            ("Korea","Asia/Seoul"),("Indonesia","Asia/Jakarta"),("Saudi Arabia","Asia/Riyadh"),("Philippines","Asia/Manila")],
    "Europe":[("UK","Europe/London"),("Germany","Europe/Berlin"),("France","Europe/Paris"),("Russia","Europe/Moscow"),("Turkey","Europe/Istanbul")],
    "Americas":[("US East","America/New_York"),("US Central","America/Chicago"),("US Mountain","America/Denver"),
                ("US West","America/Los_Angeles"),("Brazil","America/Sao_Paulo"),("Mexico","America/Mexico_City")],
    "Africa":[("Nigeria","Africa/Lagos"),("Egypt","Africa/Cairo"),("Kenya","Africa/Nairobi"),("South Africa","Africa/Johannesburg")],
    "Oceania":[("Australia","Australia/Sydney"),("New Zealand","Pacific/Auckland")]
}
TZ_ICONS = {"Asia":"🌏","Europe":"🌍","Americas":"🌎","Africa":"🌍","Oceania":"🌏"}
USERNAME_CACHE = {}
UID_USERNAME = {}

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# ========== GOOGLE SHEETS ==========
scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
creds_json = os.environ.get("GOOGLE_CREDS")
if not creds_json: raise Exception("GOOGLE_CREDS missing")
credentials = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
gclient = gspread.authorize(credentials)
workbook = gclient.open_by_url(SHEET_URL)

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

# ========== HELPERS ==========
def hdr(t): return f"<b>{t}</b>\n━━━━━━━━━━━━━━━━━━━━"
def detail(m, d, t, r=None):
    ds = fmt_date(d); ts = fmt_time(t)
    line = f"{m}\n{ds} · {ts}"
    if r and r != "none":
        if r.startswith("custom:"): line += f" · {fmt_custom(r)}"
        else: line += f" · {r.title()}"
    return line
def fmt_date(d):
    try: return datetime.strptime(str(d),"%Y-%m-%d").strftime("%-d %b")
    except: return str(d)
def fmt_time(t):
    try:
        h,m = map(int, str(t).split(":"))
        ap = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {ap}"
    except: return str(t)
def fmt_custom(r):
    if not r.startswith("custom:"): return r.title()
    ds = r.replace("custom:","").split(",")
    if sorted(ds) == ["fri","mon","thu","tue","wed"]: return "Mon–Fri"
    if sorted(ds) == ["sat","sun"]: return "Weekends"
    if sorted(ds) == sorted(DAYS): return "Daily"
    return ", ".join(DAY_NAMES.get(d,d)[:3] for d in DAYS if d in ds)
def home_kb(): return IKM([[IKB("＋ New", callback_data="add")]])
def home_text(): return "Type a reminder:\n<i>\"Buy milk tomorrow at 5pm\"</i>"
def guard(q, r):
    if len(r) > 5 and r[5] != "pending": return True
    return False
def get_detail(r):
    m = r[1] if len(r) > 1 else "?"
    d = norm_date(r[2]) if len(r) > 2 else "?"
    t = norm_time(r[3]) if len(r) > 3 else "?"
    rp = r[4] if len(r) > 4 else "none"
    return m, d, t, rp
def row_detail(row):
    try:
        r = sheet.row_values(row)
        if not r or len(r) < 6: return None, None, None, None, None
        m, d, t, rp = get_detail(r)
        return r, m, d, t, rp
    except: return None, None, None, None, None

# ========== NORMALIZE ==========
def norm_date(v):
    s = str(v).strip()
    if not s: return ""
    try:
        f = float(s)
        if f > 50000 or f < 1: return s
        return (datetime(1899,12,30) + timedelta(days=int(f))).strftime("%Y-%m-%d")
    except: pass
    for fmt in ("%Y-%m-%d","%d/%m/%Y","%d-%m-%Y","%m/%d/%Y"):
        try: return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except: continue
    return s
def norm_time(v):
    s = str(v).strip()
    if not s: return ""
    try:
        f = float(s)
        h = int(f * 24); m = int((f * 24 - h) * 60)
        return f"{h:02d}:{m:02d}"
    except: pass
    parsed = parse_time(s)
    if parsed: return parsed
    return s

# ========== TIME PARSER ==========
def parse_time(t):
    t = t.strip().lower().replace(".",":")
    m_ap = re.match(r'^(\d{1,2})(?:[:](\d{1,2}))?\s*(am|pm)$', t)
    if m_ap:
        h = int(m_ap.group(1)); mi = int(m_ap.group(2) or 0); ap = m_ap.group(3)
        if ap == "pm" and h != 12: h += 12
        if ap == "am" and h == 12: h = 0
        if 0 <= h < 24 and 0 <= mi < 60: return f"{h:02d}:{mi:02d}"
    m24 = re.match(r'^(\d{1,2})[:](\d{2})$', t)
    if m24:
        h = int(m24.group(1)); mi = int(m24.group(2))
        if 0 <= h < 24 and 0 <= mi < 60: return f"{h:02d}:{mi:02d}"
    return None

# ========== TIMEZONE ==========
def safe_tz(name):
    try: return pytz.timezone(name)
    except: return pytz.timezone(DEF_TZ)
def get_tz(uid):
    cfg = get_cfg(uid)
    return safe_tz(cfg.get("timezone", DEF_TZ))
def tz_label(name):
    for region in TZ_DATA.values():
        for label, tz_name in region:
            if tz_name == name: return label
    return name
def tz_short(name):
    now = datetime.now(safe_tz(name))
    off = now.strftime("%z")
    h, m = off[:3], off[3:]
    off_str = f"+{h[1:]}:{m}" if off[0] == "+" else f"{h}:{m}"
    if m == "00": off_str = off_str[:-3] if off[0] == "-" else f"+{h[1:]}"
    return f"{tz_label(name)} ({off_str})"

# ========== SETTINGS ==========
def get_cfg(uid):
    uid_s = str(uid)
    rows = cfg_sheet.get_all_values()
    for i, r in enumerate(rows):
        if str(r[0]) == uid_s:
            return {"row": i+1, "digest_on": r[1] if len(r)>1 else "true",
                    "digest_time": r[2] if len(r)>2 else DEF_DIGEST_TIME,
                    "max_retries": r[3] if len(r)>3 else str(DEF_RETRIES),
                    "retry_gap": r[4] if len(r)>4 else str(DEF_RETRY_GAP),
                    "timezone": r[5] if len(r)>5 else DEF_TZ,
                    "username": r[6] if len(r)>6 else "",
                    "weekly_report": r[7] if len(r)>7 else "true"}
    cfg_sheet.append_row([uid_s, "true", DEF_DIGEST_TIME, str(DEF_RETRIES), str(DEF_RETRY_GAP), DEF_TZ, "", "true"], value_input_option="RAW")
    return {"row": len(rows)+1, "digest_on":"true", "digest_time":DEF_DIGEST_TIME,
            "max_retries":str(DEF_RETRIES), "retry_gap":str(DEF_RETRY_GAP),
            "timezone":DEF_TZ, "username":"", "weekly_report":"true"}
def save_cfg(uid, key, val):
    cfg = get_cfg(uid)
    row = cfg["row"]
    cols = {"digest_on":2,"digest_time":3,"max_retries":4,"retry_gap":5,"timezone":6,"username":7,"weekly_report":8}
    if key in cols:
        r = cfg_sheet.row_values(row)
        while len(r) < 8: r.append(""); cfg_sheet.update_cell(row, len(r), "")
        cfg_sheet.update_cell(row, cols[key], str(val))
def update_username(user):
    if not user or not user.username: return
    try: save_cfg(user.id, "username", user.username.lower())
    except: pass
    USERNAME_CACHE[user.username.lower()] = (str(user.id), user.first_name or "")
    UID_USERNAME[str(user.id)] = user.username.lower()

# ========== GROUP HELPERS ==========
def set_gsub(gid, uid, name, uname, sub=True):
    gid_s, uid_s = str(gid), str(uid)
    uname_l = (uname or "").lower()
    rows = gm_sheet.get_all_values()
    for i, r in enumerate(rows):
        if str(r[0]) == gid_s and str(r[1]) == uid_s:
            gm_sheet.update_cell(i+1, 3, name)
            gm_sheet.update_cell(i+1, 4, uname_l)
            gm_sheet.update_cell(i+1, 5, "true" if sub else "false")
            return
    gm_sheet.append_row([gid_s, uid_s, name, uname_l, "true" if sub else "false"], value_input_option="RAW")
def get_gsubs(gid):
    gid_s = str(gid)
    rows = gm_sheet.get_all_values()
    result = []
    for r in rows[1:]:
        if str(r[0]) == gid_s and len(r) > 4 and r[4] == "true":
            uname = r[3] if len(r) > 3 else ""
            result.append((r[1], r[2], uname))
    return result
def add_tmember(tid, uid, name, status="waiting"):
    rows = tm_sheet.get_all_values()
    for i, r in enumerate(rows):
        if str(r[0]) == str(tid) and str(r[1]) == str(uid):
            tm_sheet.update_cell(i+1, 4, status)
            return
    tm_sheet.append_row([str(tid), str(uid), name, status], value_input_option="RAW")
def get_tmembers(tid):
    rows = tm_sheet.get_all_values()
    return [(r[1], r[2], r[3]) for r in rows[1:] if str(r[0]) == str(tid) and (len(r) < 4 or r[3] != "skipped")]
def set_tstatus(tid, uid, status):
    rows = tm_sheet.get_all_values()
    for i, r in enumerate(rows):
        if str(r[0]) == str(tid) and str(r[1]) == str(uid):
            tm_sheet.update_cell(i+1, 4, status)
            return
def find_by_tid(tid):
    rows = sheet.get_all_values()
    for i, r in enumerate(rows):
        if len(r) > 8 and r[8] == str(tid): return i+1, r
    return None, None

# ========== CALENDAR ==========
def cal_kb(y, m, tz, back_cb="cancel", back_txt="✕ Cancel"):
    now = datetime.now(tz)
    import calendar
    cal = calendar.Calendar(0)
    weeks = cal.monthdayscalendar(y, m)
    mn = datetime(y, m, 1).strftime("%B %Y")
    rows = [[IKB(mn, callback_data="noop")]]
    rows.append([IKB(d, callback_data="noop") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]])
    today_btn = None; tmr_btn = None
    for week in weeks:
        all_past = True
        row = []
        for d in week:
            if d == 0: row.append(IKB(" ", callback_data="noop"))
            else:
                dt = datetime(y, m, d)
                if dt.date() < now.date(): row.append(IKB("·", callback_data="noop"))
                else:
                    all_past = False
                    label = f"[{d}]" if dt.date() == now.date() else str(d)
                    row.append(IKB(label, callback_data=f"day_{y}-{m:02d}-{d:02d}"))
                    if dt.date() == now.date(): today_btn = True
                    if dt.date() == now.date() + timedelta(days=1): tmr_btn = True
        if not all_past: rows.append(row)
    quick = []
    if today_btn: quick.append(IKB("Today", callback_data=f"day_{now.strftime('%Y-%m-%d')}"))
    if tmr_btn:
        tmr = now + timedelta(days=1)
        quick.append(IKB("Tomorrow", callback_data=f"day_{tmr.strftime('%Y-%m-%d')}"))
    if quick: rows.append(quick)
    pm = m - 1; py = y
    if pm < 1: pm = 12; py -= 1
    nm = m + 1; ny = y
    if nm > 12: nm = 1; ny += 1
    rows.append([IKB("‹", callback_data=f"cal_{py}-{pm:02d}"), IKB("›", callback_data=f"cal_{ny}-{nm:02d}")])
    rows.append([IKB(back_txt, callback_data=back_cb)])
    return IKM(rows)

# ========== PAST CHECK ==========
def is_past(ds, ts, tz):
    try:
        dt = tz.localize(datetime.strptime(f"{ds} {ts}", "%Y-%m-%d %H:%M"))
        return dt < datetime.now(tz)
    except: return False
def past_msg(ts): return f"⚠ {fmt_time(ts)} has already passed today.\nEnter a future time:"

# ========== REPEAT ==========
def repeat_kb(row=None):
    prefix = f"chrep_{row}_" if row else "rep_"
    rows = [
        [IKB("Daily", callback_data=f"{prefix}daily"), IKB("Weekly", callback_data=f"{prefix}weekly")],
        [IKB("Monthly", callback_data=f"{prefix}monthly"), IKB("Customize", callback_data=f"{prefix}custom")]
    ]
    if row: rows.append([IKB("« Back", callback_data=f"view_{row}")])
    return IKM(rows)
def custom_days_kb(selected, row=None):
    prefix = f"cday_{row}_" if row else "cday__"
    save_cb = f"csave_{row}" if row else "csave_"
    back_cb = f"chrep_{row}_back" if row else "rep_back"
    sel = set(selected)
    r1 = [IKB(f"{'✓ ' if d in sel else ''}{DAY_NAMES[d][:3]}", callback_data=f"{prefix}{d}") for d in DAYS[:4]]
    r2 = [IKB(f"{'✓ ' if d in sel else ''}{DAY_NAMES[d][:3]}", callback_data=f"{prefix}{d}") for d in DAYS[4:]]
    r3 = [IKB("Mon–Fri", callback_data=f"{prefix}mf"), IKB("All", callback_data=f"{prefix}all"), IKB("Clear", callback_data=f"{prefix}clear")]
    rows = [r1, r2, r3]
    if sel: rows.append([IKB("✓ Save", callback_data=save_cb)])
    rows.append([IKB("« Back", callback_data=back_cb)])
    return IKM(rows)
def advance_rep(row, r):
    rep = r[4] if len(r) > 4 else "none"
    if rep == "none": return
    d = datetime.strptime(norm_date(r[2]), "%Y-%m-%d")
    if rep == "daily": nd = d + timedelta(days=1)
    elif rep == "weekly": nd = d + timedelta(days=7)
    elif rep == "monthly":
        m = d.month + 1; y = d.year
        if m > 12: m = 1; y += 1
        try: nd = d.replace(year=y, month=m)
        except: nd = d.replace(year=y, month=m, day=28)
    elif rep.startswith("custom:"):
        cdays = rep.replace("custom:","").split(",")
        for i in range(1, 8):
            nd = d + timedelta(days=i)
            if DAYS[nd.weekday()] in cdays: break
        else: nd = d + timedelta(days=1)
    else: return
    sheet.update_cell(row, 3, nd.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, "active")
    sheet.update_cell(row, 7, 0)

# ========== NL PARSER ==========
def _find_time(text):
    patterns = [
        r'(?:at|by|@)\s*(\d{1,2})[\.:]\s*(\d{2})\s*(am|pm)',
        r'(?:at|by|@)\s*(\d{1,2})\s*(am|pm)',
        r'(\d{1,2})[\.:]\s*(\d{2})\s*(am|pm)',
        r'(\d{1,2})\s*(am|pm)',
        r'(?:at|by|@)\s*(\d{1,2})[\.:](\d{2})(?:\s|$)',
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            gs = m.groups()
            if len(gs) == 3:
                h, mi, ap = int(gs[0]), int(gs[1]), gs[2].lower()
                if ap == "pm" and h != 12: h += 12
                if ap == "am" and h == 12: h = 0
            elif len(gs) == 2:
                if gs[1].lower() in ("am","pm"):
                    h, mi, ap = int(gs[0]), 0, gs[1].lower()
                    if ap == "pm" and h != 12: h += 12
                    if ap == "am" and h == 12: h = 0
                else: h, mi = int(gs[0]), int(gs[1])
            else: continue
            if 0 <= h < 24 and 0 <= mi < 60:
                return f"{h:02d}:{mi:02d}", m.start(), m.end()
    return None, -1, -1

def _find_date(text, tz):
    now = datetime.now(tz); today = now.date()
    patterns = [
        (r'\b(today|tonight)\b', lambda m: today),
        (r'\b(tomorrow|tmrw|tmr)\b', lambda m: today + timedelta(days=1)),
        (r'\bday\s+after\s+tomorrow\b', lambda m: today + timedelta(days=2)),
        (r'\bnext\s+week\b', lambda m: today + timedelta(days=7)),
    ]
    for d_name, d_short in DAY_SHORT.items():
        def mk(dn):
            def fn(m):
                tgt = DAYS.index(dn)
                diff = (tgt - today.weekday()) % 7
                if diff == 0: diff = 7
                return today + timedelta(days=diff)
            return fn
        patterns.append((rf'\b{d_name}\b', mk(d_short)))
    date_pats = [
        (r'\bon\s+(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*', 'dm'),
        (r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*', 'dm'),
        (r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{1,2})(?:st|nd|rd|th)?', 'md'),
        (r'\bon\s+(\d{1,2})(?:st|nd|rd|th)\b', 'day_only'),
    ]
    for p, fn in patterns:
        m = re.search(p, text, re.I)
        if m: return fn(m).strftime("%Y-%m-%d"), m.start(), m.end()
    months_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    for p, fmt in date_pats:
        m = re.search(p, text, re.I)
        if m:
            if fmt == 'dm':
                day = int(m.group(1)); mon = months_map.get(m.group(2).lower()[:3], 1)
            elif fmt == 'md':
                mon = months_map.get(m.group(1).lower()[:3], 1); day = int(m.group(2))
            elif fmt == 'day_only':
                day = int(m.group(1)); mon = now.month
                if day < now.day: mon += 1
                if mon > 12: mon = 1
            try:
                y = now.year
                dt = datetime(y, mon, day).date()
                if dt < today: dt = datetime(y+1, mon, day).date()
                return dt.strftime("%Y-%m-%d"), m.start(), m.end()
            except: continue
    return None, -1, -1

def _find_repeat(text, tz):
    now = datetime.now(tz); today = now.date()
    m = re.search(r'\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b', text, re.I)
    if m:
        day_key = DAY_SHORT.get(m.group(1).lower())
        if day_key:
            tgt = DAYS.index(day_key)
            diff = (tgt - today.weekday()) % 7
            if diff == 0: diff = 7
            dt = today + timedelta(days=diff)
            return "weekly", m.start(), m.end(), dt.strftime("%Y-%m-%d")
    if re.search(r'\bevery\s*day\b', text, re.I):
        m = re.search(r'\bevery\s*day\b', text, re.I)
        return "daily", m.start(), m.end(), None
    pats = [(r'\b(daily)\b', "daily"), (r'\b(weekly)\b', "weekly"), (r'\b(monthly)\b', "monthly")]
    for p, rp in pats:
        m = re.search(p, text, re.I)
        if m: return rp, m.start(), m.end(), None
    return None, -1, -1, None

def _find_relative(text, tz):
    now = datetime.now(tz)
    m = re.search(r'\b(?:in|after)\s+(\d+)\s*(min(?:ute)?s?|hrs?|hours?|days?|weeks?|h|m)\b', text, re.I)
    if m:
        n = int(m.group(1)); unit = m.group(2).lower()
        if unit.startswith("m") and not unit.startswith("mo"): dt = now + timedelta(minutes=n)
        elif unit.startswith("h"): dt = now + timedelta(hours=n)
        elif unit.startswith("d"): dt = now + timedelta(days=n)
        elif unit.startswith("w"): dt = now + timedelta(weeks=n)
        else: return None, None, -1, -1
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), m.start(), m.end()
    return None, None, -1, -1

FILLER = re.compile(r"^(remind\s+me\s+to|remind\s+me|reminder|remember\s+to|don'?t\s+forget\s+to|set\s+reminder)\s+", re.I)

def parse_nl_partial(text, tz):
    spans = []
    rel_d, rel_t, rs, re_ = _find_relative(text, tz)
    if rel_d: spans.append((rs, re_))
    rep, rps, rpe, rep_date = _find_repeat(text, tz)
    if rep: spans.append((rps, rpe))
    t_val, ts, te = (None, -1, -1) if rel_t else _find_time(text)
    if t_val: spans.append((ts, te))
    d_val, ds, de = (None, -1, -1) if rel_d else _find_date(text, tz)
    if d_val: spans.append((ds, de))
    if rel_d: d_val = rel_d
    if rel_t: t_val = rel_t
    if rep_date and not d_val: d_val = rep_date
    has_trigger = t_val or d_val or rep or FILLER.search(text)
    if not has_trigger: return None
    msg = text
    for s, e in sorted(spans, reverse=True):
        if s >= 0: msg = msg[:s] + msg[e:]
    msg = FILLER.sub("", msg).strip()
    msg = re.sub(r'\s+', ' ', msg).strip(" .,;:-!?")
    if not msg: return None
    return {"message": msg, "date": d_val, "time": t_val, "repeat": rep}

# ========== PROMPT HELPERS ==========
def store_prompt(ud, msg):
    ud["p_mid"] = msg.message_id; ud["p_cid"] = msg.chat_id
async def del_prompt(ctx, ud):
    mid = ud.pop("p_mid", None); cid = ud.pop("p_cid", None)
    if mid and cid:
        try: await ctx.bot.delete_message(cid, mid)
        except: pass
async def rm_prompt_kb(ctx, ud):
    mid = ud.pop("p_mid", None); cid = ud.pop("p_cid", None)
    if mid and cid:
        try: await ctx.bot.edit_message_reply_markup(cid, mid, reply_markup=None)
        except: pass
def store_home(ud, msg):
    ud["h_mid"] = msg.message_id; ud["h_cid"] = msg.chat_id
async def rm_home(ctx, ud):
    mid = ud.pop("h_mid", None); cid = ud.pop("h_cid", None)
    if mid and cid:
        try: await ctx.bot.edit_message_reply_markup(cid, mid, reply_markup=None)
        except: pass
def store_rmsg(bd, row, mid, cid):
    bd[f"rmsg_{row}"] = {"mid": mid, "cid": cid}
async def rm_old_rmsg(ctx, bd, row):
    key = f"rmsg_{row}"
    d = bd.pop(key, None)
    if d:
        try: await ctx.bot.edit_message_reply_markup(d["cid"], d["mid"], reply_markup=None)
        except: pass
async def safe_edit(msg, text, kb=None):
    try: await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except: pass

# ========== SNOOZE KB ==========
def snz_kb(row):
    rows_btn = []
    for i in range(0, len(SNOOZE_OPTIONS), 3):
        chunk = SNOOZE_OPTIONS[i:i+3]
        rows_btn.append([IKB(label, callback_data=f"snz_{row}_{mins}") for label, mins in chunk])
    rows_btn.append([IKB("« Back", callback_data=f"snzb_{row}")])
    return IKM(rows_btn)
def reminder_kb(row):
    return IKM([[IKB("Snooze", callback_data=f"snzp_{row}"), IKB("✅ Done", callback_data=f"done_{row}")]])

# ========== TAG HELPERS ==========
def extract_tag_texts(msg):
    if not msg or not msg.entities: return []
    tags = []
    for ent in msg.entities:
        if ent.type == "mention":
            uname = msg.text[ent.offset+1:ent.offset+ent.length].lower()
            tags.append(uname)
        elif ent.type == "text_mention" and ent.user:
            tags.append(str(ent.user.id))
    return tags

def is_subscriber_tagged(sub_uid, sub_name, sub_uname, tags):
    for tag in tags:
        if tag == str(sub_uid): return True
        if sub_uname and tag == sub_uname.lower(): return True
        cached_uname = UID_USERNAME.get(str(sub_uid), "")
        if cached_uname and tag == cached_uname: return True
    return False

# ========== SEND & TRACK ==========
async def send_and_track(ctx, chat_id, text, kb, track_key=None, track_cid=None):
    try:
        msg = await ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")
        if track_key: ctx.bot_data[track_key] = {"mid": msg.message_id, "cid": track_cid or chat_id}
        return msg
    except: return None

# ========== GROUP STATUS ==========
def gstatus_text(tid, msg):
    members = get_tmembers(tid)
    if not members: return f"{msg}\n\n<i>No subscribers</i>"
    active = [(u, n, s) for u, n, s in members if s != "skipped"]
    if not active: return f"{msg}\n\n<i>No active subscribers</i>"
    all_done = all(s in ("done","missed") for _,_,s in active)
    if all_done and all(s == "done" for _,_,s in active):
        names = ", ".join(n for _,n,_ in active)
        return f"{msg} · ✅ All done\n{names}"
    parts = " · ".join(f"{GT_IC.get(s,'⏳')} {n}" for _, n, s in active)
    prefix = "⏰ " if any(s not in ("done","missed") for _,_,s in active) else ""
    return f"{prefix}{msg}\n\n{parts}"

async def update_gstatus(ctx, tid, msg):
    key = f"gstatus_{tid}"
    d = ctx.bot_data.get(key)
    if not d: return
    text = gstatus_text(tid, msg)
    try: await ctx.bot.edit_message_text(text, chat_id=d["cid"], message_id=d["mid"], parse_mode="HTML")
    except: pass

# ========== AUTO MINIMIZE ==========
async def p_auto_minimize(ctx):
    d = ctx.job.data
    mid = d.get("mid"); cid = d.get("cid")
    min_text = d.get("min_text", "📋"); show_cb = d.get("show_cb", "noop")
    try: await ctx.bot.edit_message_text(min_text, chat_id=cid, message_id=mid, reply_markup=IKM([[IKB("📋 Show", callback_data=show_cb)]]), parse_mode="HTML")
    except: pass

async def g_auto_minimize(ctx):
    d = ctx.job.data
    mid = d.get("mid"); cid = d.get("cid")
    min_text = d.get("min_text", "📋"); show_cb = d.get("show_cb", "noop")
    try: await ctx.bot.edit_message_text(min_text, chat_id=cid, message_id=mid, reply_markup=IKM([[IKB("📋 Show", callback_data=show_cb)]]), parse_mode="HTML")
    except: pass

# ========== COMMANDS ==========
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    chat = update.effective_chat
    if chat.type != "private":
        text = f"{hdr('Smart Reminder Bot')}\n\n<b>Commands</b>\n/remind — Group reminder\n/list — Active reminders\n\n<b>Examples</b>\n<code>/remind Buy milk at 5pm</code>\n<code>/remind Meeting tomorrow 10am daily</code>\n<code>/remind</code> — step-by-step\n\nTag members to assign:\n<code>/remind @user Submit report at 5pm</code>"
        msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=IKM([[IKB("✕ Close", callback_data="gclose")]]))
        ctx.bot_data[f"gmin_{msg.message_id}"] = {"text": "<b>Smart Reminder Bot</b>", "show_cb": f"gshow_start_{msg.message_id}"}
        ctx.bot_data[f"gfull_{msg.message_id}"] = text
        ctx.job_queue.run_once(g_auto_minimize, 60, data={"mid": msg.message_id, "cid": chat.id, "min_text": "<b>Smart Reminder Bot</b>", "show_cb": f"gshow_start_{msg.message_id}"})
        return
    ud = ctx.user_data
    await rm_home(ctx, ud)
    msg = await update.message.reply_text(f"{hdr('Smart Reminder Bot')}\n\n{home_text()}", reply_markup=home_kb(), parse_mode="HTML")
    store_home(ud, msg)

async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /remind here for group reminders.")
        return
    ud = ctx.user_data
    await rm_home(ctx, ud)
    ud["step"] = "message"
    msg = await update.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", reply_markup=IKM([[IKB("✕ Cancel", callback_data="cancel")]]), parse_mode="HTML")
    store_prompt(ud, msg)

async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    chat = update.effective_chat
    if chat.type != "private":
        gid = str(chat.id)
        set_gsub(gid, update.effective_user.id, update.effective_user.first_name, (update.effective_user.username or "").lower())
        rows = sheet.get_all_values()
        items = []
        for i, r in enumerate(rows[1:], start=2):
            if len(r) < 8: continue
            if str(r[7]) != gid: continue
            if r[5] in ("done","cancelled"): continue
            m, d, t, rp = get_detail(r)
            items.append((i, m, d, t, r[5]))
        if not items:
            msg = await update.message.reply_text("<b>Group Reminders</b> — No active", parse_mode="HTML", reply_markup=IKM([[IKB("✕ Close", callback_data="gclose")]]))
            ctx.bot_data[f"gmin_{msg.message_id}"] = {"text": "<b>Group Reminders</b> — No active", "show_cb": "noop"}
            ctx.job_queue.run_once(g_auto_minimize, 60, data={"mid": msg.message_id, "cid": chat.id, "min_text": "<b>Group Reminders</b> — No active", "show_cb": "noop"})
            return
        lines = [hdr("Group Reminders"), ""]
        for idx, (i, m, d, t, s) in enumerate(items, 1):
            ic = ST_IC.get(s, "○")
            lines.append(f"{idx} {ic} {m[:30]}{'…' if len(m)>30 else ''}\n   {fmt_date(d)} · {fmt_time(t)}")
        text = "\n".join(lines)
        msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=IKM([[IKB("✕ Close", callback_data="gclose")]]))
        ctx.bot_data[f"gmin_{msg.message_id}"] = {"text": f"<b>Group Reminders</b> ({len(items)})", "show_cb": f"gshow_list_{gid}_{msg.message_id}"}
        ctx.bot_data[f"gfull_{msg.message_id}"] = text
        ctx.job_queue.run_once(g_auto_minimize, 60, data={"mid": msg.message_id, "cid": chat.id, "min_text": f"<b>Group Reminders</b> ({len(items)})", "show_cb": f"gshow_list_{gid}_{msg.message_id}"})
        return
    await show_list(update, ctx, new=True)

async def show_list(update, ctx, new=True):
    uid = update.effective_user.id
    ud = ctx.user_data
    rows = sheet.get_all_values()
    items = []
    for i, r in enumerate(rows[1:], start=2):
        if str(r[0]) != str(uid): continue
        if len(r) < 6: continue
        if r[5] in ("done","cancelled"): continue
        if len(r) > 7 and r[7]: continue
        m, d, t, rp = get_detail(r)
        items.append((i, m, d, t, r[5]))
    if not items:
        text = f"{hdr('Reminders')}\n\nNo active reminders."
        if new:
            msg = await update.effective_message.reply_text(text, reply_markup=IKM([[IKB("« Back", callback_data="home")]]), parse_mode="HTML")
        else:
            await safe_edit(update.effective_message, text, IKM([[IKB("« Back", callback_data="home")]]))
        return
    lines = [hdr("Reminders"), ""]
    for idx, (i, m, d, t, s) in enumerate(items, 1):
        ic = ST_IC.get(s, "○")
        lines.append(f"{idx} {ic} {m[:30]}{'…' if len(m)>30 else ''}\n   {fmt_date(d)} · {fmt_time(t)}")
    text = "\n".join(lines)
    btns = []
    row_btns = []
    for idx, (i, *_) in enumerate(items, 1):
        row_btns.append(IKB(str(idx), callback_data=f"view_{i}"))
        if len(row_btns) == 5:
            btns.append(row_btns); row_btns = []
    if row_btns: btns.append(row_btns)
    btns.append([IKB("« Back", callback_data="home")])
    kb = IKM(btns)
    if new:
        msg = await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
        ctx.job_queue.run_once(p_auto_minimize, 60, data={"mid": msg.message_id, "cid": update.effective_chat.id, "min_text": f"<b>📋 Reminders</b> ({len(items)})", "show_cb": "pshow_list"})
    else:
        await safe_edit(update.effective_message, text, kb)

async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    if update.effective_chat.type != "private": return
    await show_settings(update.effective_message, update.effective_user.id, new=True)

async def show_settings(msg, uid, new=True):
    cfg = get_cfg(uid)
    don = "ON" if cfg["digest_on"] == "true" else "OFF"
    dt = fmt_time(cfg["digest_time"])
    mr = cfg["max_retries"]; rg = cfg["retry_gap"]
    tz = tz_label(cfg.get("timezone", DEF_TZ))
    wr = "ON" if cfg.get("weekly_report","true") == "true" else "OFF"
    text = f"{hdr('Settings')}\n\nDaily Digest: {don} · {dt}\nMax Retries: {mr}×\nRetry Gap: {rg} min\nTimezone: {tz}\nWeekly Report: {wr}"
    kb = IKM([
        [IKB(f"Digest: {don}", callback_data="cfg_digest"), IKB(f"⏰ {dt}", callback_data="cfg_dtime")],
        [IKB(f"Retries: {mr}×", callback_data="cfg_retries"), IKB(f"Gap: {rg}m", callback_data="cfg_gap")],
        [IKB(f"🌍 {tz}", callback_data="cfg_tz")],
        [IKB(f"Report: {wr}", callback_data="cfg_report")],
        [IKB("« Back", callback_data="home")]
    ])
    if new: await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else: await safe_edit(msg, text, kb)

async def info_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    if update.effective_chat.type != "private": return
    text = f"""{hdr('Smart Reminder Bot')}

<b>Just type naturally:</b>
<code>Buy milk tomorrow at 5pm</code>
<code>Gym at 6pm daily</code>
<code>Meeting in 30 min</code>
<code>Call mom every monday at 9am</code>

<b>Features:</b>
• Smart snooze (15m – 12h)
• Auto-retry if missed
• Daily digest
• Weekly report
• Monthly schedule
• Custom repeat days
• Group reminders
• Per-user timezone

<b>Commands:</b>
/add — Step-by-step reminder
/list — View reminders
/month — Monthly schedule
/settings — Preferences"""
    msg = await update.message.reply_text(text, reply_markup=IKM([[IKB("✕ Close", callback_data="pclose_info")], [IKB("＋ New", callback_data="add")]]), parse_mode="HTML")
    ctx.bot_data[f"pinfo_{msg.message_id}"] = text
    ctx.job_queue.run_once(p_auto_minimize, 60, data={"mid": msg.message_id, "cid": update.effective_chat.id, "min_text": "<b>ℹ️ Info</b>", "show_cb": f"pshow_info_{msg.message_id}"})

# ========== MONTH VIEW ==========
async def month_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    if update.effective_chat.type != "private": return
    tz = get_tz(update.effective_user.id)
    now = datetime.now(tz)
    msg = await update.message.reply_text("Loading...", parse_mode="HTML")
    text, kb = build_month_view(update.effective_user.id, now.year, now.month, tz)
    await safe_edit(msg, text, kb)
    ctx.job_queue.run_once(p_auto_minimize, 60, data={"mid": msg.message_id, "cid": update.effective_chat.id, "min_text": f"<b>📅 {now.strftime('%B %Y')}</b>", "show_cb": f"pshow_month_{now.year}_{now.month}"})

def build_month_view(uid, year, month, tz):
    import calendar
    now = datetime.now(tz); today = now.date()
    first = datetime(year, month, 1).date()
    if month == 12: last = datetime(year+1, 1, 1).date() - timedelta(days=1)
    else: last = datetime(year, month+1, 1).date() - timedelta(days=1)
    rows = sheet.get_all_values()
    all_events = []
    for i, r in enumerate(rows[1:], start=2):
        if str(r[0]) != str(uid): continue
        if len(r) < 6: continue
        if len(r) > 7 and r[7]: continue
        m, d, t, rp = get_detail(r)
        try: rd = datetime.strptime(d, "%Y-%m-%d").date()
        except: continue
        if rp == "none":
            if first <= rd <= last: all_events.append((rd, t, m, r[5]))
        else:
            cur = first
            while cur <= last:
                if rp == "daily":
                    if cur >= rd: all_events.append((cur, t, m, r[5] if cur < today else "active"))
                elif rp == "weekly":
                    if cur >= rd and cur.weekday() == rd.weekday(): all_events.append((cur, t, m, r[5] if cur < today else "active"))
                elif rp == "monthly":
                    if cur >= rd and cur.day == rd.day: all_events.append((cur, t, m, r[5] if cur < today else "active"))
                elif rp.startswith("custom:"):
                    cdays = rp.replace("custom:","").split(",")
                    if cur >= rd and DAYS[cur.weekday()] in cdays: all_events.append((cur, t, m, r[5] if cur < today else "active"))
                cur += timedelta(days=1)
    cal_obj = calendar.Calendar(0)
    weeks_raw = cal_obj.monthdayscalendar(year, month)
    weeks = []
    for wi, week in enumerate(weeks_raw):
        ds = [d for d in week if d > 0]
        if not ds: continue
        w_start = datetime(year, month, ds[0]).date()
        w_end = datetime(year, month, ds[-1]).date()
        count = sum(1 for ev in all_events if w_start <= ev[0] <= w_end)
        is_current = w_start <= today <= w_end
        weeks.append((wi+1, w_start, w_end, count, is_current))
    total = len(all_events)
    done_c = sum(1 for e in all_events if e[3] == "done")
    missed_c = sum(1 for e in all_events if e[3] == "missed")
    upcoming = total - done_c - missed_c
    mn = datetime(year, month, 1).strftime("%B %Y")
    lines = [hdr(f"📅 {mn}"), ""]
    for wi, (wn, ws, we, cnt, is_cur) in enumerate(weeks, 1):
        cur_mark = " ◂" if is_cur else ""
        lines.append(f"W{wi}: {ws.day}–{we.day} {ws.strftime('%b')}{cur_mark} · {cnt} reminder{'s' if cnt!=1 else ''}")
    lines.append("")
    parts = []
    if done_c: parts.append(f"✅ {done_c} done")
    if missed_c: parts.append(f"✗ {missed_c} missed")
    if upcoming: parts.append(f"○ {upcoming} upcoming")
    lines.append(f"Total: {total} · {' · '.join(parts)}" if parts else f"Total: {total}")
    btns = []
    row_btns = []
    for wi, (wn, ws, we, cnt, is_cur) in enumerate(weeks, 1):
        row_btns.append(IKB(str(wi), callback_data=f"mw_{year}_{month}_{wn}"))
        if len(row_btns) == 4: btns.append(row_btns); row_btns = []
    if row_btns: btns.append(row_btns)
    pm = month - 1; py = year
    if pm < 1: pm = 12; py -= 1
    nm = month + 1; ny = year
    if nm > 12: nm = 1; ny += 1
    btns.append([IKB(f"‹ {datetime(py,pm,1).strftime('%b')}", callback_data=f"mn_{py}_{pm}"), IKB(f"{datetime(ny,nm,1).strftime('%b')} ›", callback_data=f"mn_{ny}_{nm}")])
    btns.append([IKB("« Back", callback_data="home")])
    return "\n".join(lines), IKM(btns)

def build_week_view(uid, year, month, week_num, tz, total_weeks=4):
    import calendar
    now = datetime.now(tz); today = now.date()
    cal_obj = calendar.Calendar(0)
    weeks_raw = cal_obj.monthdayscalendar(year, month)
    real_weeks = [w for w in weeks_raw if any(d > 0 for d in w)]
    if week_num < 1 or week_num > len(real_weeks): return "Invalid week", IKM([[IKB("« Back", callback_data=f"mn_{year}_{month}")]])
    week = real_weeks[week_num - 1]
    ds = [d for d in week if d > 0]
    w_start = datetime(year, month, ds[0]).date()
    w_end = datetime(year, month, ds[-1]).date()
    rows = sheet.get_all_values()
    events = {}
    for i, r in enumerate(rows[1:], start=2):
        if str(r[0]) != str(uid): continue
        if len(r) < 6: continue
        if len(r) > 7 and r[7]: continue
        m, d, t, rp = get_detail(r)
        try: rd = datetime.strptime(d, "%Y-%m-%d").date()
        except: continue
        cur = w_start
        while cur <= w_end:
            should_add = False
            if rp == "none" and cur == rd: should_add = True
            elif rp == "daily" and cur >= rd: should_add = True
            elif rp == "weekly" and cur >= rd and cur.weekday() == rd.weekday(): should_add = True
            elif rp == "monthly" and cur >= rd and cur.day == rd.day: should_add = True
            elif rp.startswith("custom:"):
                cdays = rp.replace("custom:","").split(",")
                if cur >= rd and DAYS[cur.weekday()] in cdays: should_add = True
            if should_add:
                s = r[5] if cur < today else ("done" if r[5] == "done" else "active")
                events.setdefault(cur, []).append((t, m, s))
            cur += timedelta(days=1)
    mn = datetime(year, month, 1).strftime("%B %Y")
    lines = [hdr(f"Week {week_num}: {w_start.day}–{w_end.day} {w_start.strftime('%b')}"), ""]
    for d in range((w_end - w_start).days + 1):
        dt = w_start + timedelta(days=d)
        evts = events.get(dt, [])
        if not evts: continue
        day_label = "Today" if dt == today else f"{dt.day} {dt.strftime('%b')}, {dt.strftime('%a')}"
        lines.append(f"<b>{day_label}</b>")
        for t_val, m_val, s in sorted(evts):
            ic = ST_IC.get(s, "○")
            lines.append(f"  {ic} {fmt_time(t_val)} · {m_val}")
        lines.append("")
    if len(lines) <= 2: lines.append("<i>No reminders this week</i>")
    btns = []
    if week_num < len(real_weeks):
        btns.append([IKB(f"Week {week_num+1} ›", callback_data=f"mw_{year}_{month}_{week_num+1}")])
    btns.append([IKB(f"« {mn}", callback_data=f"mn_{year}_{month}")])
    return "\n".join(lines), IKM(btns)

# ========== REMIND (GROUP) ==========
async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Use /add here. /remind is for groups.")
        return
    uid = update.effective_user.id
    uname = (update.effective_user.username or "").lower()
    fname = update.effective_user.first_name or ""
    set_gsub(chat.id, uid, fname, uname)
    ud = ctx.user_data
    text = (update.message.text or "").strip()
    after = text[len("/remind"):].strip() if text.lower().startswith("/remind") else ""
    tags = extract_tag_texts(update.message)
    if tags: ud["g_tags"] = tags
    else: ud.pop("g_tags", None)
    if after:
        for tag in tags:
            after = re.sub(rf'@{re.escape(tag)}', '', after, flags=re.I).strip()
        tz = get_tz(uid)
        parsed = parse_nl_partial(after, tz)
        if parsed:
            ud["g_chat"] = chat.id; ud["g_creator"] = uid; ud["g_fname"] = fname
            ud["message"] = parsed["message"]
            if parsed.get("date"): ud["date"] = parsed["date"]
            if parsed.get("time"): ud["time"] = parsed["time"]
            if parsed.get("repeat"): ud["g_repeat"] = parsed["repeat"]
            if parsed.get("date") and parsed.get("time"):
                if is_past(parsed["date"], parsed["time"], tz):
                    ud["step"] = "g_date"; ud["message"] = parsed["message"]
                    if parsed.get("time"): ud["time"] = parsed["time"]
                    msg = await update.message.reply_text(f"{parsed['message']}\n{past_msg(parsed['time'])}\n\nPick a date:", reply_markup=cal_kb(datetime.now(tz).year, datetime.now(tz).month, tz, "gcancel", "✕ Cancel"), parse_mode="HTML")
                    return
                await finish_group_remind(update, ctx)
                return
            elif parsed.get("time"):
                ud["step"] = "g_date"
                now = datetime.now(tz)
                msg = await update.message.reply_text(f"{parsed['message']}\n{fmt_time(parsed['time'])}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz, "gcancel", "✕ Cancel"), parse_mode="HTML")
                return
            elif parsed.get("date"):
                ud["step"] = "g_time"
                msg = await update.message.reply_text(f"{parsed['message']}\n{fmt_date(parsed['date'])}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", reply_markup=ForceReply(selective=True), parse_mode="HTML")
                return
            else:
                ud["step"] = "g_date"
                now = datetime.now(tz)
                msg = await update.message.reply_text(f"{parsed['message']}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz, "gcancel", "✕ Cancel"), parse_mode="HTML")
                return
    ud["step"] = "g_message"; ud["g_chat"] = chat.id; ud["g_creator"] = uid; ud["g_fname"] = fname
    await update.message.reply_text(f"{hdr('Group Reminder')}\nEnter message:", reply_markup=ForceReply(selective=True), parse_mode="HTML")

async def finish_group_remind(update, ctx):
    ud = ctx.user_data
    msg = ud.get("message","?"); date = ud.get("date"); time = ud.get("time")
    rep = ud.get("g_repeat","none"); gid = ud.get("g_chat"); creator = ud.get("g_creator")
    fname = ud.get("g_fname","")
    tags = ud.get("g_tags")
    tid = f"t_{int(_time.time())}"
    sheet.append_row([str(creator), msg, date, time, rep, "active", 0, str(gid), tid], value_input_option="RAW")
    subs = get_gsubs(gid)
    sub_count = 0
    if tags:
        for s_uid, s_name, s_uname in subs:
            if is_subscriber_tagged(s_uid, s_name, s_uname, tags):
                add_tmember(tid, s_uid, s_name, "waiting"); sub_count += 1
            else:
                add_tmember(tid, s_uid, s_name, "skipped")
        tagged_names = []
        for s_uid, s_name, s_uname in subs:
            if is_subscriber_tagged(s_uid, s_name, s_uname, tags): tagged_names.append(s_name)
        for_text = f"\nFor: {', '.join(tagged_names)}" if tagged_names else ""
    else:
        for s_uid, s_name, _ in subs:
            add_tmember(tid, s_uid, s_name, "waiting"); sub_count += 1
        for_text = ""
    text = f"{detail(msg, date, time, rep)}\nBy {fname}{for_text}\n\n{sub_count} subscribed"
    kb = IKM([[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")],
              [IKB("🔁 Repeat", callback_data=f"grep_{tid}")]])
    target = update.effective_message or update.callback_query.message
    sent = await target.reply_text(text, reply_markup=kb, parse_mode="HTML")
    ctx.bot_data[f"gsetup_{tid}"] = {"mid": sent.message_id, "cid": gid}
    for k in ["step","message","date","time","g_repeat","g_chat","g_creator","g_fname","g_tags"]:
        ud.pop(k, None)

# ========== SAVE REMINDER (PRIVATE) ==========
async def save_reminder(update, ctx, msg, date, time, rep="none", edit_msg=None):
    uid = update.effective_user.id
    sheet.append_row([str(uid), msg, date, time, rep, "active", 0, "", ""], value_input_option="RAW")
    rows = sheet.get_all_values()
    row = len(rows)
    text = f"{hdr('Saved ✓')}\n{detail(msg, date, time, rep)}"
    btns = []
    if rep == "none": btns.append([IKB("🔁 Repeat", callback_data=f"chrep_{row}_show")])
    btns.append([IKB("✎ Edit", callback_data=f"edit_{row}")])
    btns.append([IKB("＋ New", callback_data="add")])
    kb = IKM(btns)
    if edit_msg:
        await safe_edit(edit_msg, text, kb)
    else:
        target = update.effective_message or update.callback_query.message
        await target.reply_text(text, reply_markup=kb, parse_mode="HTML")

# ========== TEXT HANDLER ==========
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    ud = ctx.user_data; text = update.message.text.strip()
    uid = update.effective_user.id; tz = get_tz(uid)
    chat = update.effective_chat
    step = ud.get("step")
    # GROUP TEXT STEPS
    if step in ("g_message","g_time") and chat.type != "private":
        if str(chat.id) != str(ud.get("g_chat")): return
        if step == "g_message":
            parsed = parse_nl_partial(text, tz)
            if parsed:
                ud["message"] = parsed["message"]
                if parsed.get("time"): ud["time"] = parsed["time"]
                if parsed.get("date"): ud["date"] = parsed["date"]
                if parsed.get("repeat"): ud["g_repeat"] = parsed["repeat"]
                if parsed.get("date") and parsed.get("time"):
                    if is_past(parsed["date"], parsed["time"], tz):
                        ud["step"] = "g_date"
                        now = datetime.now(tz)
                        await update.message.reply_text(past_msg(parsed["time"]) + "\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz, "gcancel", "✕ Cancel"), parse_mode="HTML")
                        return
                    await finish_group_remind(update, ctx)
                    return
                elif parsed.get("time"):
                    ud["step"] = "g_date"
                    now = datetime.now(tz)
                    await update.message.reply_text(f"{parsed['message']}\n{fmt_time(parsed['time'])}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz, "gcancel", "✕ Cancel"), parse_mode="HTML")
                    return
                elif parsed.get("date"):
                    ud["step"] = "g_time"
                    await update.message.reply_text(f"{parsed['message']}\n{fmt_date(parsed['date'])}\n\nEnter time:", reply_markup=ForceReply(selective=True), parse_mode="HTML")
                    return
            ud["message"] = text; ud["step"] = "g_date"
            now = datetime.now(tz)
            await update.message.reply_text(f"{text}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz, "gcancel", "✕ Cancel"), parse_mode="HTML")
            return
        elif step == "g_time":
            t = parse_time(text)
            if not t:
                await update.message.reply_text("⚠ Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", reply_markup=ForceReply(selective=True), parse_mode="HTML")
                return
            d = ud.get("date")
            if d and is_past(d, t, tz):
                await update.message.reply_text(past_msg(t), reply_markup=ForceReply(selective=True), parse_mode="HTML")
                return
            ud["time"] = t
            if not d:
                now = datetime.now(tz)
                ud["date"] = now.strftime("%Y-%m-%d")
            await finish_group_remind(update, ctx)
            return
    # PRIVATE STEPS
    if step == "message":
        await del_prompt(ctx, ud)
        parsed = parse_nl_partial(text, tz)
        if parsed:
            ud["message"] = parsed["message"]
            if parsed.get("time"): ud["time"] = parsed["time"]
            if parsed.get("date"): ud["date"] = parsed["date"]
            if parsed.get("repeat"): ud["p_repeat"] = parsed["repeat"]
            if parsed.get("date") and parsed.get("time"):
                if is_past(parsed["date"], parsed["time"], tz):
                    ud["step"] = "date"
                    now = datetime.now(tz)
                    msg = await update.message.reply_text(f"{parsed['message']}\n{past_msg(parsed['time'])}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML")
                    store_prompt(ud, msg)
                    return
                rep = parsed.get("repeat") or "none"
                await save_reminder(update, ctx, parsed["message"], parsed["date"], parsed["time"], rep)
                ud.clear()
                return
            elif parsed.get("time"):
                ud["step"] = "date"
                now = datetime.now(tz)
                msg = await update.message.reply_text(f"{parsed['message']}\n{fmt_time(parsed['time'])}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML")
                store_prompt(ud, msg)
                return
            elif parsed.get("date"):
                ud["step"] = "time"
                msg = await update.message.reply_text(f"{parsed['message']}\n{fmt_date(parsed['date'])}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", reply_markup=IKM([[IKB("✕ Cancel", callback_data="cancel")]]), parse_mode="HTML")
                store_prompt(ud, msg)
                return
        ud["message"] = text; ud["step"] = "date"
        now = datetime.now(tz)
        msg = await update.message.reply_text(f"{text}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML")
        store_prompt(ud, msg)
        return
    elif step == "time":
        t = parse_time(text)
        if not t:
            await update.message.reply_text("⚠ Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML")
            return
        d = ud.get("date")
        if d and is_past(d, t, tz):
            await update.message.reply_text(past_msg(t), parse_mode="HTML")
            return
        await del_prompt(ctx, ud)
        if not d:
            now = datetime.now(tz); d = now.strftime("%Y-%m-%d")
            ud["date"] = d
        ud["time"] = t
        rep = ud.get("p_repeat", "none")
        await save_reminder(update, ctx, ud["message"], d, t, rep)
        ud.clear()
        return
    elif step == "edit_msg":
        row = ud.get("editing_row")
        if row:
            sheet.update_cell(row, 2, text)
            r, m, d, t, rp = row_detail(row)
            await update.message.reply_text(f"{hdr('Updated ✓')}\n{detail(text, d, t, rp)}", reply_markup=IKM([[IKB("« Back", callback_data=f"view_{row}")]]), parse_mode="HTML")
        ud.pop("step", None); ud.pop("editing_row", None)
        return
    elif step == "edit_time":
        row = ud.get("editing_row")
        t = parse_time(text)
        if not t:
            await update.message.reply_text("⚠ Invalid time.\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", parse_mode="HTML")
            return
        if row:
            r = sheet.row_values(row)
            d = norm_date(r[2]) if len(r) > 2 else ""
            if d and is_past(d, t, tz):
                await update.message.reply_text(past_msg(t), parse_mode="HTML")
                return
            sheet.update_cell(row, 4, t)
            r, m, dd, tt, rp = row_detail(row)
            await update.message.reply_text(f"{hdr('Updated ✓')}\n{detail(m, dd, t, rp)}", reply_markup=IKM([[IKB("« Back", callback_data=f"view_{row}")]]), parse_mode="HTML")
        ud.pop("step", None); ud.pop("editing_row", None)
        return
    elif step == "cfg_dtime":
        t = parse_time(text)
        if not t:
            await update.message.reply_text("⚠ Invalid time.\n<i>e.g. 7am, 8:30 PM</i>", parse_mode="HTML")
            return
        save_cfg(uid, "digest_time", t)
        await show_settings(update.message, uid, new=True)
        ud.pop("step", None)
        return
    # NL PARSING (no active step)
    if step: return
    if chat.type != "private": return
    parsed = parse_nl_partial(text, tz)
    if not parsed: return
    ud["message"] = parsed["message"]
    if parsed.get("repeat"): ud["p_repeat"] = parsed["repeat"]
    if parsed.get("date") and parsed.get("time"):
        if is_past(parsed["date"], parsed["time"], tz):
            ud["step"] = "date"; ud["date"] = parsed["date"]; ud["time"] = parsed["time"]
            now = datetime.now(tz)
            msg = await update.message.reply_text(f"{parsed['message']}\n{past_msg(parsed['time'])}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML")
            store_prompt(ud, msg)
            return
        rep = parsed.get("repeat") or "none"
        await save_reminder(update, ctx, parsed["message"], parsed["date"], parsed["time"], rep)
        return
    elif parsed.get("time"):
        ud["time"] = parsed["time"]; ud["step"] = "date"
        now = datetime.now(tz)
        ud["date_for_today"] = now.strftime("%Y-%m-%d")
        if not is_past(now.strftime("%Y-%m-%d"), parsed["time"], tz):
            rep = parsed.get("repeat") or "none"
            await save_reminder(update, ctx, parsed["message"], now.strftime("%Y-%m-%d"), parsed["time"], rep)
            return
        msg = await update.message.reply_text(f"{parsed['message']}\n{past_msg(parsed['time'])}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML")
        store_prompt(ud, msg)
        return
    elif parsed.get("date"):
        ud["date"] = parsed["date"]; ud["step"] = "time"
        msg = await update.message.reply_text(f"{parsed['message']}\n{fmt_date(parsed['date'])}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", reply_markup=IKM([[IKB("✕ Cancel", callback_data="cancel")]]), parse_mode="HTML")
        store_prompt(ud, msg)
        return
    else:
        ud["step"] = "date"
        now = datetime.now(tz)
        msg = await update.message.reply_text(f"{parsed['message']}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML")
        store_prompt(ud, msg)
        return

# ========== BUTTON HANDLER ==========
async def on_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data; ud = ctx.user_data; uid = q.from_user.id
    update_username(q.from_user)
    if d == "noop": return
    # HOME
    if d == "home":
        await rm_home(ctx, ud)
        ud.clear()
        msg = await q.message.reply_text(f"{hdr('Smart Reminder Bot')}\n\n{home_text()}", reply_markup=home_kb(), parse_mode="HTML")
        store_home(ud, msg)
        return
    # ADD
    if d == "add":
        await rm_home(ctx, ud)
        ud.clear(); ud["step"] = "message"
        msg = await q.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", reply_markup=IKM([[IKB("✕ Cancel", callback_data="cancel")]]), parse_mode="HTML")
        store_prompt(ud, msg)
        return
    # CANCEL
    if d == "cancel":
        ud.clear()
        msg = await q.message.reply_text(f"{hdr('Smart Reminder Bot')}\n\n{home_text()}", reply_markup=home_kb(), parse_mode="HTML")
        store_home(ud, msg)
        return
    # GROUP CANCEL
    if d == "gcancel":
        ud.clear()
        try: await q.message.delete()
        except: pass
        return
    # PRIVATE CLOSE (info)
    if d == "pclose_info":
        mid = q.message.message_id
        text = ctx.bot_data.get(f"pinfo_{mid}", "<b>ℹ️ Info</b>")
        await safe_edit(q.message, "<b>ℹ️ Info</b>", IKM([[IKB("📋 Show", callback_data=f"pshow_info_{mid}")]]))
        return
    # PRIVATE SHOW INFO
    if d.startswith("pshow_info_"):
        mid = int(d.replace("pshow_info_", ""))
        text = ctx.bot_data.get(f"pinfo_{mid}")
        if text:
            await safe_edit(q.message, text, IKM([[IKB("✕ Close", callback_data="pclose_info")], [IKB("＋ New", callback_data="add")]]))
        return
    # PRIVATE SHOW LIST
    if d == "pshow_list":
        await show_list(update, ctx, new=False)
        return
    # PRIVATE SHOW MONTH
    if d.startswith("pshow_month_"):
        parts = d.replace("pshow_month_", "").split("_")
        y, m = int(parts[0]), int(parts[1])
        tz = get_tz(uid)
        text, kb = build_month_view(uid, y, m, tz)
        await safe_edit(q.message, text, kb)
        return
    # GROUP CLOSE/SHOW
    if d == "gclose":
        mid = q.message.message_id
        gmin = ctx.bot_data.get(f"gmin_{mid}")
        if gmin:
            await safe_edit(q.message, gmin["text"], IKM([[IKB("📋 Show", callback_data=gmin["show_cb"])]]))
        else:
            await safe_edit(q.message, "<b>📋</b>", IKM([[IKB("📋 Show", callback_data="noop")]]))
        return
    if d.startswith("gshow_start_"):
        mid = int(d.replace("gshow_start_", ""))
        text = ctx.bot_data.get(f"gfull_{mid}")
        if text:
            await safe_edit(q.message, text, IKM([[IKB("✕ Close", callback_data="gclose")]]))
        return
    if d.startswith("gshow_list_"):
        parts = d.replace("gshow_list_", "").split("_")
        gid = parts[0]
        mid = int(parts[1]) if len(parts) > 1 else 0
        text = ctx.bot_data.get(f"gfull_{mid}")
        if text:
            await safe_edit(q.message, text, IKM([[IKB("✕ Close", callback_data="gclose")]]))
        return
    # CALENDAR
    if await _btn_calendar(q, ctx, d, ud, uid): return
    # REMINDER ACTIONS
    if await _btn_reminder(q, ctx, d, ud, uid): return
    # EDIT
    if await _btn_edit(q, ctx, d, ud, uid): return
    # SETTINGS
    if await _btn_settings(q, ctx, d, ud, uid): return
    # GROUP ACTIONS
    if await _btn_group(q, ctx, d, ud, uid): return
    # MONTH
    if await _btn_month(q, ctx, d, ud, uid): return

async def _btn_calendar(q, ctx, d, ud, uid):
    if d.startswith("cal_"):
        parts = d.replace("cal_","").split("-")
        y, m = int(parts[0]), int(parts[1])
        tz = get_tz(uid)
        step = ud.get("step","")
        if step == "edit_date":
            row = ud.get("editing_row")
            await safe_edit(q.message, q.message.text, cal_kb(y, m, tz, f"view_{row}", "« Back"))
        elif step == "g_date":
            await safe_edit(q.message, q.message.text, cal_kb(y, m, tz, "gcancel", "✕ Cancel"))
        else:
            await safe_edit(q.message, q.message.text, cal_kb(y, m, tz))
        return True
    if d.startswith("day_"):
        ds = d.replace("day_","")
        tz = get_tz(uid)
        step = ud.get("step","")
        if step == "edit_date":
            row = ud.get("editing_row")
            r = sheet.row_values(row)
            t_val = norm_time(r[3]) if len(r) > 3 else ""
            if t_val and is_past(ds, t_val, tz):
                await safe_edit(q.message, f"{past_msg(t_val)}\n\nPick another date:", cal_kb(int(ds[:4]), int(ds[5:7]), tz, f"view_{row}", "« Back"))
                return True
            sheet.update_cell(row, 3, ds)
            r, m, dd, t, rp = row_detail(row)
            await safe_edit(q.message, f"{hdr('Updated ✓')}\n{detail(m, ds, t, rp)}", IKM([[IKB("« Back", callback_data=f"view_{row}")]]))
            ud.pop("step", None); ud.pop("editing_row", None)
            return True
        elif step == "g_date":
            ud["date"] = ds
            if ud.get("time"):
                if is_past(ds, ud["time"], tz):
                    now = datetime.now(tz)
                    await safe_edit(q.message, f"{past_msg(ud['time'])}\n\nPick another date:", cal_kb(now.year, now.month, tz, "gcancel", "✕ Cancel"))
                    return True
                await finish_group_remind(update=None, ctx=ctx)
                await safe_edit(q.message, "✓", None)
                return True
            ud["step"] = "g_time"
            gid = ud.get("g_chat")
            await safe_edit(q.message, f"{ud.get('message','')}\n{fmt_date(ds)}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", None)
            return True
        else:
            ud["date"] = ds; ud["step"] = "time"
            await safe_edit(q.message, f"{ud.get('message','')}\n{fmt_date(ds)}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>", IKM([[IKB("✕ Cancel", callback_data="cancel")]]))
            return True
    return False

async def _btn_reminder(q, ctx, d, ud, uid):
    # VIEW
    if d.startswith("view_"):
        row = int(d.replace("view_",""))
        r, m, dd, t, rp = row_detail(row)
        if not r: await safe_edit(q.message, "Not found.", IKM([[IKB("« Back", callback_data="home")]])); return True
        s = r[5] if len(r) > 5 else "active"
        ic = ST_IC.get(s,"○"); sl = ST_LB.get(s,"Active")
        text = f"{hdr('Reminder')}\n{detail(m, dd, t, rp)}\n{ic} {sl}"
        btns = []
        if s in ("active","pending","snoozed"):
            btns.append([IKB("✎ Edit", callback_data=f"edit_{row}"), IKB("✕ Cancel", callback_data=f"crem_{row}")])
        elif s == "missed":
            btns.append([IKB("✕ Remove", callback_data=f"crem_{row}")])
        elif s == "cancelled":
            btns.append([IKB("↩ Undo", callback_data=f"undo_{row}")])
        btns.append([IKB("« Back", callback_data="list_refresh")])
        await safe_edit(q.message, text, IKM(btns))
        return True
    # LIST REFRESH
    if d == "list_refresh":
        await show_list(update=None, ctx=ctx, new=False)
        uid_val = q.from_user.id
        rows = sheet.get_all_values()
        items = []
        for i, r in enumerate(rows[1:], start=2):
            if str(r[0]) != str(uid_val): continue
            if len(r) < 6: continue
            if r[5] in ("done","cancelled"): continue
            if len(r) > 7 and r[7]: continue
            m, dd, t, rp = get_detail(r)
            items.append((i, m, dd, t, r[5]))
        if not items:
            await safe_edit(q.message, f"{hdr('Reminders')}\n\nNo active reminders.", IKM([[IKB("« Back", callback_data="home")]]))
            return True
        lines = [hdr("Reminders"), ""]
        for idx, (i, m, dd, t, s) in enumerate(items, 1):
            ic = ST_IC.get(s, "○")
            lines.append(f"{idx} {ic} {m[:30]}{'…' if len(m)>30 else ''}\n   {fmt_date(dd)} · {fmt_time(t)}")
        btns = []; row_btns = []
        for idx, (i, *_) in enumerate(items, 1):
            row_btns.append(IKB(str(idx), callback_data=f"view_{i}"))
            if len(row_btns) == 5: btns.append(row_btns); row_btns = []
        if row_btns: btns.append(row_btns)
        btns.append([IKB("« Back", callback_data="home")])
        await safe_edit(q.message, "\n".join(lines), IKM(btns))
        return True
    # SNOOZE PICKER
    if d.startswith("snzp_"):
        row = int(d.replace("snzp_",""))
        r = sheet.row_values(row)
        if guard(q, r):
            m, dd, t, rp = get_detail(r)
            await safe_edit(q.message, f"{detail(m,dd,t,rp)}\n\n<i>Already handled</i>", None)
            return True
        await safe_edit(q.message, q.message.text, snz_kb(row))
        return True
    # SNOOZE BACK
    if d.startswith("snzb_"):
        row = int(d.replace("snzb_",""))
        await safe_edit(q.message, q.message.text, reminder_kb(row))
        return True
    # SNOOZE DO
    if d.startswith("snz_"):
        parts = d.replace("snz_","").split("_")
        row = int(parts[0]); mins = int(parts[1])
        r = sheet.row_values(row)
        if guard(q, r):
            m, dd, t, rp = get_detail(r)
            await safe_edit(q.message, f"{detail(m,dd,t,rp)}\n\n<i>Already handled</i>", None)
            return True
        tz = get_tz(uid)
        now = datetime.now(tz); snz_time = now + timedelta(minutes=mins)
        m, dd, t, rp = get_detail(r)
        jobs = ctx.job_queue.get_jobs_by_name(f"retry-{row}")
        for j in jobs: j.schedule_removal()
        rep = r[4] if len(r) > 4 else "none"
        is_group = len(r) > 7 and r[7]
        if rep == "none" or rep == "":
            sheet.update_cell(row, 3, snz_time.strftime("%Y-%m-%d"))
            sheet.update_cell(row, 4, snz_time.strftime("%H:%M"))
            sheet.update_cell(row, 6, "active")
            sheet.update_cell(row, 7, 0)
        else:
            sheet.update_cell(row, 6, "snoozed")
            ctx.job_queue.run_once(snooze_fire, mins*60, data={"row":row,"chat":uid}, name=f"snzfire-{row}")
        if is_group:
            tid = r[8] if len(r) > 8 else ""
            if tid: set_tstatus(tid, str(uid), "snoozed"); await update_gstatus(ctx, tid, m)
        label = f"{mins}m" if mins < 60 else f"{mins//60}h"
        await safe_edit(q.message, f"{detail(m,dd,t,rp)}\n\n<b>Snoozed {label}</b> → {fmt_time(snz_time.strftime('%H:%M'))}", None)
        return True
    # DONE
    if d.startswith("done_"):
        row = int(d.replace("done_",""))
        r = sheet.row_values(row)
        if guard(q, r):
            m, dd, t, rp = get_detail(r)
            await safe_edit(q.message, f"{detail(m,dd,t,rp)}\n\n<i>Already handled</i>", None)
            return True
        m, dd, t, rp = get_detail(r)
        jobs = ctx.job_queue.get_jobs_by_name(f"retry-{row}")
        for j in jobs: j.schedule_removal()
        rep = r[4] if len(r) > 4 else "none"
        is_group = len(r) > 7 and r[7]
        tid = r[8] if len(r) > 8 else ""
        if rep != "none" and rep != "":
            advance_rep(row, r)
        else:
            sheet.update_cell(row, 6, "done")
            sheet.update_cell(row, 7, 0)
        if is_group and tid:
            set_tstatus(tid, str(uid), "done")
            await update_gstatus(ctx, tid, m)
        await safe_edit(q.message, f"{detail(m,dd,t,rp)}\n\n<b>Done ✓</b>", None)
        return True
    # CANCEL REMINDER
    if d.startswith("crem_"):
        row = int(d.replace("crem_",""))
        r, m, dd, t, rp = row_detail(row)
        if not r: return True
        jobs = ctx.job_queue.get_jobs_by_name(f"retry-{row}")
        for j in jobs: j.schedule_removal()
        sheet.update_cell(row, 6, "cancelled")
        sheet.update_cell(row, 7, 0)
        await safe_edit(q.message, f"{hdr('Cancelled ✕')}\n{detail(m,dd,t,rp)}", IKM([[IKB("↩ Undo", callback_data=f"undo_{row}")], [IKB("＋ New", callback_data="add")]]))
        return True
    # UNDO CANCEL
    if d.startswith("undo_"):
        row = int(d.replace("undo_",""))
        r, m, dd, t, rp = row_detail(row)
        if not r: return True
        sheet.update_cell(row, 6, "active")
        await safe_edit(q.message, f"{hdr('Restored ✓')}\n{detail(m,dd,t,rp)}", IKM([[IKB("« Back", callback_data=f"view_{row}")]]))
        return True
    # CHANGE REPEAT (from saved)
    if d.startswith("chrep_") and d.endswith("_show"):
        row = int(d.replace("chrep_","").replace("_show",""))
        await safe_edit(q.message, q.message.text, repeat_kb(row))
        return True
    if d.startswith("chrep_") and d.endswith("_back"):
        row = int(d.replace("chrep_","").replace("_back",""))
        r, m, dd, t, rp = row_detail(row)
        if not r: return True
        text = f"{hdr('Saved ✓')}\n{detail(m,dd,t,rp)}"
        btns = []
        if rp == "none": btns.append([IKB("🔁 Repeat", callback_data=f"chrep_{row}_show")])
        btns.append([IKB("✎ Edit", callback_data=f"edit_{row}")])
        btns.append([IKB("＋ New", callback_data="add")])
        await safe_edit(q.message, text, IKM(btns))
        return True
    if d.startswith("chrep_") and "_custom" in d:
        row = int(d.split("_")[1])
        ud["custom_days"] = []; ud["custom_row"] = row
        await safe_edit(q.message, q.message.text, custom_days_kb([], row))
        return True
    if d.startswith("chrep_"):
        parts = d.replace("chrep_","").split("_")
        if len(parts) == 2:
            row = int(parts[0]); rep = parts[1]
            if rep in ("daily","weekly","monthly"):
                sheet.update_cell(row, 5, rep)
                r, m, dd, t, rp = row_detail(row)
                text = f"{hdr('Updated ✓')}\n{detail(m,dd,t,rep)}"
                await safe_edit(q.message, text, IKM([[IKB("✎ Edit", callback_data=f"edit_{row}")], [IKB("＋ New", callback_data="add")]]))
                return True
    # CUSTOM DAYS
    if d.startswith("cday_"):
        parts = d.replace("cday_","").split("_")
        row_s = parts[0]; day = parts[1] if len(parts) > 1 else ""
        row = int(row_s) if row_s else None
        sel = ud.get("custom_days", [])
        if day == "mf": sel = ["mon","tue","wed","thu","fri"]
        elif day == "all": sel = list(DAYS)
        elif day == "clear": sel = []
        elif day in DAYS:
            if day in sel: sel.remove(day)
            else: sel.append(day)
        ud["custom_days"] = sel
        await safe_edit(q.message, q.message.text, custom_days_kb(sel, row))
        return True
    if d.startswith("csave_"):
        row_s = d.replace("csave_","")
        row = int(row_s) if row_s else None
        sel = ud.get("custom_days", [])
        rep = "custom:" + ",".join(d for d in DAYS if d in sel)
        if row:
            sheet.update_cell(row, 5, rep)
            r, m, dd, t, rp = row_detail(row)
            text = f"{hdr('Updated ✓')}\n{detail(m,dd,t,rep)}"
            await safe_edit(q.message, text, IKM([[IKB("✎ Edit", callback_data=f"edit_{row}")], [IKB("＋ New", callback_data="add")]]))
        ud.pop("custom_days", None); ud.pop("custom_row", None)
        return True
    # REPEAT (from step-by-step / NL)
    if d.startswith("rep_"):
        rep = d.replace("rep_","")
        if rep == "custom":
            ud["custom_days"] = []
            await safe_edit(q.message, q.message.text, custom_days_kb([]))
            return True
        if rep == "back":
            await safe_edit(q.message, q.message.text, repeat_kb())
            return True
        m = ud.get("message"); dd = ud.get("date"); t = ud.get("time")
        if m and dd and t:
            await save_reminder(update, ctx, m, dd, t, rep, q.message)
            ud.clear()
        return True
    return False

async def _btn_edit(q, ctx, d, ud, uid):
    if d.startswith("edit_"):
        row = int(d.replace("edit_",""))
        r, m, dd, t, rp = row_detail(row)
        if not r: return True
        text = f"{hdr('Edit Reminder')}\n{detail(m,dd,t,rp)}\n\nWhat to change?"
        kb = IKM([
            [IKB("Message", callback_data=f"emsg_{row}"), IKB("Date", callback_data=f"edate_{row}"), IKB("Time", callback_data=f"etime_{row}")],
            [IKB("Repeat", callback_data=f"chrep_{row}_show")],
            [IKB("« Back", callback_data=f"view_{row}")]
        ])
        await safe_edit(q.message, text, kb)
        return True
    if d.startswith("emsg_"):
        row = int(d.replace("emsg_",""))
        r, m, dd, t, rp = row_detail(row)
        ud["step"] = "edit_msg"; ud["editing_row"] = row
        text = f"{hdr('Edit Message')}\n<i>{m}</i>\n{fmt_date(dd)} · {fmt_time(t)}\n\nEnter new message:"
        await safe_edit(q.message, text, IKM([[IKB("« Back", callback_data=f"edit_{row}")]]))
        return True
    if d.startswith("edate_"):
        row = int(d.replace("edate_",""))
        ud["step"] = "edit_date"; ud["editing_row"] = row
        tz = get_tz(uid); now = datetime.now(tz)
        r, m, dd, t, rp = row_detail(row)
        text = f"{hdr('Edit Date')}\n{m}\n<i>{fmt_date(dd)}</i> · {fmt_time(t)}\n\nPick new date:"
        await safe_edit(q.message, text, cal_kb(now.year, now.month, tz, f"view_{row}", "« Back"))
        return True
    if d.startswith("etime_"):
        row = int(d.replace("etime_",""))
        ud["step"] = "edit_time"; ud["editing_row"] = row
        r, m, dd, t, rp = row_detail(row)
        text = f"{hdr('Edit Time')}\n{m}\n{fmt_date(dd)} · <i>{fmt_time(t)}</i>\n\nEnter new time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>"
        await safe_edit(q.message, text, IKM([[IKB("« Back", callback_data=f"edit_{row}")]]))
        return True
    return False

async def _btn_settings(q, ctx, d, ud, uid):
    if d == "cfg_digest":
        cfg = get_cfg(uid)
        new_val = "false" if cfg["digest_on"] == "true" else "true"
        save_cfg(uid, "digest_on", new_val)
        await show_settings(q.message, uid, new=False)
        return True
    if d == "cfg_report":
        cfg = get_cfg(uid)
        new_val = "false" if cfg.get("weekly_report","true") == "true" else "true"
        save_cfg(uid, "weekly_report", new_val)
        await show_settings(q.message, uid, new=False)
        return True
    if d == "cfg_dtime":
        ud["step"] = "cfg_dtime"
        await safe_edit(q.message, f"{hdr('Digest Time')}\n\nEnter time:\n<i>e.g. 7am, 8:30 PM</i>", IKM([[IKB("« Back", callback_data="cfg_back")]]))
        return True
    if d == "cfg_back":
        ud.pop("step", None)
        await show_settings(q.message, uid, new=False)
        return True
    if d == "cfg_retries":
        opts = [1,2,3,5,7,10]
        btns = [[IKB(f"{n}×", callback_data=f"cfgr_{n}") for n in opts[:3]], [IKB(f"{n}×", callback_data=f"cfgr_{n}") for n in opts[3:]]]
        btns.append([IKB("« Back", callback_data="cfg_back")])
        await safe_edit(q.message, f"{hdr('Max Retries')}\n\nPick:", IKM(btns))
        return True
    if d.startswith("cfgr_"):
        n = int(d.replace("cfgr_",""))
        save_cfg(uid, "max_retries", n)
        await show_settings(q.message, uid, new=False)
        return True
    if d == "cfg_gap":
        opts = [5,10,15,20,30,60]
        btns = [[IKB(f"{n}m", callback_data=f"cfgg_{n}") for n in opts[:3]], [IKB(f"{n}m", callback_data=f"cfgg_{n}") for n in opts[3:]]]
        btns.append([IKB("« Back", callback_data="cfg_back")])
        await safe_edit(q.message, f"{hdr('Retry Gap')}\n\nPick:", IKM(btns))
        return True
    if d.startswith("cfgg_"):
        n = int(d.replace("cfgg_",""))
        save_cfg(uid, "retry_gap", n)
        await show_settings(q.message, uid, new=False)
        return True
    if d == "cfg_tz":
        btns = [[IKB(f"{TZ_ICONS.get(r,'🌍')} {r}", callback_data=f"tzr_{r}") for r in list(TZ_DATA.keys())[:2]]]
        btns.append([IKB(f"{TZ_ICONS.get(r,'🌍')} {r}", callback_data=f"tzr_{r}") for r in list(TZ_DATA.keys())[2:4]])
        btns.append([IKB(f"{TZ_ICONS.get(r,'🌍')} {r}", callback_data=f"tzr_{r}") for r in list(TZ_DATA.keys())[4:]])
        btns.append([IKB("« Back", callback_data="cfg_back")])
        await safe_edit(q.message, f"{hdr('Timezone')}\n\nPick region:", IKM(btns))
        return True
    if d.startswith("tzr_"):
        region = d.replace("tzr_","")
        tzs = TZ_DATA.get(region, [])
        cfg = get_cfg(uid); cur = cfg.get("timezone", DEF_TZ)
        btns = []
        for label, tz_name in tzs:
            mark = "▸ " if tz_name == cur else ""
            btns.append([IKB(f"{mark}{label}", callback_data=f"tzs_{tz_name}")])
        btns.append([IKB("« Regions", callback_data="cfg_tz")])
        await safe_edit(q.message, f"{hdr('Timezone')}\n\n{region}:", IKM(btns))
        return True
    if d.startswith("tzs_"):
        tz_name = d.replace("tzs_","")
        save_cfg(uid, "timezone", tz_name)
        await show_settings(q.message, uid, new=False)
        return True
    return False

async def _btn_group(q, ctx, d, ud, uid):
    if d.startswith("gjoin_"):
        tid = d.replace("gjoin_","")
        fname = q.from_user.first_name or ""; uname = (q.from_user.username or "").lower()
        gid = q.message.chat_id
        set_gsub(gid, uid, fname, uname)
        add_tmember(tid, str(uid), fname, "waiting")
        row_i, r = find_by_tid(tid)
        if r:
            m, dd, t, rp = get_detail(r)
            subs = [(u,n,s) for u,n,s in get_tmembers(tid) if s != "skipped"]
            sub_names = ", ".join(n for _,n,_ in subs)
            text = f"{detail(m,dd,t,rp)}\nBy {r[0]}\n\n{len(subs)} subscribed: {sub_names}"
            kb = IKM([[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")],
                      [IKB("🔁 Repeat", callback_data=f"grep_{tid}")]])
            await safe_edit(q.message, text, kb)
        return True
    if d.startswith("gskip_"):
        tid = d.replace("gskip_","")
        set_tstatus(tid, str(uid), "skipped")
        fname = q.from_user.first_name or ""; uname = (q.from_user.username or "").lower()
        gid = q.message.chat_id
        set_gsub(gid, uid, fname, uname)
        row_i, r = find_by_tid(tid)
        if r:
            m, dd, t, rp = get_detail(r)
            subs = [(u,n,s) for u,n,s in get_tmembers(tid) if s != "skipped"]
            sub_names = ", ".join(n for _,n,_ in subs)
            text = f"{detail(m,dd,t,rp)}\nBy {r[0]}\n\n{len(subs)} subscribed: {sub_names}"
            kb = IKM([[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")],
                      [IKB("🔁 Repeat", callback_data=f"grep_{tid}")]])
            await safe_edit(q.message, text, kb)
        return True
    if d.startswith("grep_"):
        tid = d.replace("grep_","")
        row_i, r = find_by_tid(tid)
        if not r: return True
        prefix = f"grp_{tid}_"
        btns = [
            [IKB("Daily", callback_data=f"{prefix}daily"), IKB("Weekly", callback_data=f"{prefix}weekly")],
            [IKB("Monthly", callback_data=f"{prefix}monthly")],
            [IKB("« Back", callback_data=f"grpb_{tid}")]
        ]
        await safe_edit(q.message, q.message.text, IKM(btns))
        return True
    if d.startswith("grp_") and not d.startswith("grpb_"):
        parts = d.replace("grp_","").rsplit("_",1)
        tid = parts[0]; rep = parts[1]
        row_i, r = find_by_tid(tid)
        if row_i and rep in ("daily","weekly","monthly"):
            sheet.update_cell(row_i, 5, rep)
            r = sheet.row_values(row_i)
            m, dd, t, rp = get_detail(r)
            subs = [(u,n,s) for u,n,s in get_tmembers(tid) if s != "skipped"]
            sub_names = ", ".join(n for _,n,_ in subs)
            text = f"{detail(m,dd,t,rep)}\nBy {r[0]}\n\n{len(subs)} subscribed: {sub_names}"
            kb = IKM([[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")],
                      [IKB("🔁 Repeat", callback_data=f"grep_{tid}")]])
            await safe_edit(q.message, text, kb)
        return True
    if d.startswith("grpb_"):
        tid = d.replace("grpb_","")
        row_i, r = find_by_tid(tid)
        if r:
            m, dd, t, rp = get_detail(r)
            subs = [(u,n,s) for u,n,s in get_tmembers(tid) if s != "skipped"]
            sub_names = ", ".join(n for _,n,_ in subs)
            text = f"{detail(m,dd,t,rp)}\nBy {r[0]}\n\n{len(subs)} subscribed: {sub_names}"
            kb = IKM([[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")],
                      [IKB("🔁 Repeat", callback_data=f"grep_{tid}")]])
            await safe_edit(q.message, text, kb)
        return True
    return False

async def _btn_month(q, ctx, d, ud, uid):
    if d.startswith("mw_"):
        parts = d.replace("mw_","").split("_")
        y, m, w = int(parts[0]), int(parts[1]), int(parts[2])
        tz = get_tz(uid)
        text, kb = build_week_view(uid, y, m, w, tz)
        await safe_edit(q.message, text, kb)
        return True
    if d.startswith("mn_"):
        parts = d.replace("mn_","").split("_")
        y, m = int(parts[0]), int(parts[1])
        tz = get_tz(uid)
        text, kb = build_month_view(uid, y, m, tz)
        await safe_edit(q.message, text, kb)
        return True
    if d == "mb_month":
        tz = get_tz(uid); now = datetime.now(tz)
        text, kb = build_month_view(uid, now.year, now.month, tz)
        await safe_edit(q.message, text, kb)
        return True
    return False

# ========== SNOOZE FIRE ==========
async def snooze_fire(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data; row = d["row"]; chat = d["chat"]
    r = sheet.row_values(row)
    if not r or r[5] not in ("snoozed","pending"): return
    m, dd, t, rp = get_detail(r)
    sheet.update_cell(row, 6, "pending"); sheet.update_cell(row, 7, 0)
    text = f"⏰ {m}\n{fmt_date(dd)} · {fmt_time(t)}"
    msg = await send_and_track(ctx, chat, text, reminder_kb(row))
    if msg: store_rmsg(ctx.bot_data, row, msg.message_id, chat)
    cfg = get_cfg(chat)
    gap = int(cfg.get("retry_gap", DEF_RETRY_GAP)) * 60
    ctx.job_queue.run_once(auto_retry, gap, data={"row":row,"chat":chat,"count":0}, name=f"retry-{row}")

# ========== AUTO RETRY ==========
async def auto_retry(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data; row = d["row"]; chat = d["chat"]; count = d.get("count", 0)
    r = sheet.row_values(row)
    if not r: return
    if r[5] not in ("pending",): return
    cfg = get_cfg(chat)
    max_r = int(cfg.get("max_retries", DEF_RETRIES))
    gap = int(cfg.get("retry_gap", DEF_RETRY_GAP)) * 60
    if count >= max_r:
        is_group = len(r) > 7 and r[7]
        rep = r[4] if len(r) > 4 else "none"
        if rep != "none" and rep != "": advance_rep(row, r)
        else: sheet.update_cell(row, 6, "missed")
        if is_group:
            tid = r[8] if len(r) > 8 else ""
            if tid:
                set_tstatus(tid, str(chat), "missed")
                m = r[1] if len(r) > 1 else "?"
                await update_gstatus(ctx, tid, m)
        return
    m, dd, t, rp = get_detail(r)
    sheet.update_cell(row, 7, count + 1)
    await rm_old_rmsg(ctx, ctx.bot_data, row)
    text = f"🔔 {m} ({count+1}/{max_r})\n{fmt_date(dd)} · {fmt_time(t)}"
    msg = await send_and_track(ctx, chat, text, reminder_kb(row))
    if msg: store_rmsg(ctx.bot_data, row, msg.message_id, chat)
    ctx.job_queue.run_once(auto_retry, gap, data={"row":row,"chat":chat,"count":count+1}, name=f"retry-{row}")

# ========== SCHEDULER ==========
async def check_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    try: rows = sheet.get_all_values()
    except:
        try: gclient.login(); rows = sheet.get_all_values()
        except: return
    if len(rows) <= 1: return
    cfg_rows = {}
    try:
        all_cfg = cfg_sheet.get_all_values()
        for cr in all_cfg[1:]:
            if cr: cfg_rows[cr[0]] = cr
    except: pass
    for i, r in enumerate(rows[1:], start=2):
        if len(r) < 6: continue
        if r[5] != "active": continue
        uid_s = r[0]; d = norm_date(r[2]); t = norm_time(r[3])
        tz_name = DEF_TZ
        if uid_s in cfg_rows:
            cr = cfg_rows[uid_s]
            if len(cr) > 5 and cr[5]: tz_name = cr[5]
        tz = safe_tz(tz_name)
        now = datetime.now(tz); now_str = now.strftime("%Y-%m-%d %H:%M")
        rem_str = f"{d} {t}"
        if rem_str != now_str: continue
        is_group = len(r) > 7 and r[7]
        tid = r[8] if len(r) > 8 else ""
        m = r[1] if len(r) > 1 else "?"
        rep = r[4] if len(r) > 4 else "none"
        sheet.update_cell(i, 6, "pending"); sheet.update_cell(i, 7, 0)
        jobs = ctx.job_queue.get_jobs_by_name(f"retry-{i}")
        for j in jobs: j.schedule_removal()
        cfg = {}
        if uid_s in cfg_rows:
            cr = cfg_rows[uid_s]
            cfg = {"max_retries": cr[3] if len(cr)>3 else str(DEF_RETRIES), "retry_gap": cr[4] if len(cr)>4 else str(DEF_RETRY_GAP)}
        gap = int(cfg.get("retry_gap", DEF_RETRY_GAP)) * 60
        if is_group and tid:
            members = get_tmembers(tid)
            active = [(u,n,s) for u,n,s in members if s == "waiting"]
            for u, n, s in active:
                set_tstatus(tid, u, "pending")
            setup_key = f"gsetup_{tid}"
            sd = ctx.bot_data.get(setup_key)
            if sd:
                try: await ctx.bot.edit_message_reply_markup(sd["cid"], sd["mid"], reply_markup=None)
                except: pass
            grp_text = gstatus_text(tid, m)
            gid = r[7]
            msg = await send_and_track(ctx, gid, grp_text, None)
            if msg: ctx.bot_data[f"gstatus_{tid}"] = {"mid": msg.message_id, "cid": gid}
            for u, n, s in active:
                text = f"⏰ {m}\nFrom: Group\n{fmt_date(d)} · {fmt_time(t)}"
                await send_and_track(ctx, int(u), text, reminder_kb(i))
            ctx.job_queue.run_once(grp_retry, gap, data={"row":i,"tid":tid,"count":0}, name=f"retry-{i}")
        else:
            text = f"⏰ {m}\n{fmt_date(d)} · {fmt_time(t)}"
            msg = await send_and_track(ctx, int(uid_s), text, reminder_kb(i))
            if msg: store_rmsg(ctx.bot_data, i, msg.message_id, int(uid_s))
            ctx.job_queue.run_once(auto_retry, gap, data={"row":i,"chat":int(uid_s),"count":0}, name=f"retry-{i}")

async def grp_retry(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data; row = d["row"]; tid = d["tid"]; count = d.get("count",0)
    r = sheet.row_values(row)
    if not r or r[5] != "pending": return
    members = get_tmembers(tid)
    pending = [(u,n) for u,n,s in members if s == "pending"]
    if not pending: return
    uid_s = r[0]
    cfg = get_cfg(int(uid_s))
    max_r = int(cfg.get("max_retries", DEF_RETRIES))
    gap = int(cfg.get("retry_gap", DEF_RETRY_GAP)) * 60
    if count >= max_r:
        for u, n in pending:
            set_tstatus(tid, u, "missed")
        m = r[1] if len(r) > 1 else "?"
        rep = r[4] if len(r) > 4 else "none"
        if rep != "none" and rep != "": advance_rep(row, r)
        else: sheet.update_cell(row, 6, "missed")
        await update_gstatus(ctx, tid, m)
        return
    m, dd, t, rp = get_detail(r)
    for u, n in pending:
        text = f"🔔 {m} ({count+1}/{max_r})\nFrom: Group"
        await send_and_track(ctx, int(u), text, reminder_kb(row))
    ctx.job_queue.run_once(grp_retry, gap, data={"row":row,"tid":tid,"count":count+1}, name=f"retry-{i}")

# ========== DAILY DIGEST ==========
async def check_digest(ctx: ContextTypes.DEFAULT_TYPE):
    try: all_cfg = cfg_sheet.get_all_values()
    except: return
    for cr in all_cfg[1:]:
        if not cr or len(cr) < 3: continue
        if cr[1] != "true": continue
        uid_s = cr[0]; dt = cr[2] if cr[2] else DEF_DIGEST_TIME
        tz_name = cr[5] if len(cr) > 5 and cr[5] else DEF_TZ
        tz = safe_tz(tz_name); now = datetime.now(tz)
        if now.strftime("%H:%M") != dt: continue
        rows = sheet.get_all_values()
        today_s = now.strftime("%Y-%m-%d")
        items = []
        for r in rows[1:]:
            if str(r[0]) != uid_s: continue
            if len(r) < 6 or r[5] in ("done","cancelled","missed"): continue
            d = norm_date(r[2])
            if d != today_s: continue
            items.append((norm_time(r[3]), r[1]))
        today_fmt = now.strftime("%-d %b")
        lines = [f"☀️ Good morning!\n{hdr('Today ' + today_fmt)}", ""]
        if items:
            for t, m in sorted(items):
                lines.append(f"  {fmt_time(t)} · {m}")
            lines.append(f"\n{len(items)} reminder{'s' if len(items)!=1 else ''} today")
        else:
            lines.append("<i>No reminders today. Enjoy your day!</i>")
        text = "\n".join(lines)
        try: await ctx.bot.send_message(chat_id=int(uid_s), text=text, reply_markup=home_kb(), parse_mode="HTML")
        except: pass

# ========== WEEKLY REPORT ==========
async def check_weekly_report(ctx: ContextTypes.DEFAULT_TYPE):
    try: all_cfg = cfg_sheet.get_all_values()
    except: return
    for cr in all_cfg[1:]:
        if not cr or len(cr) < 8: continue
        if cr[7] != "true": continue
        uid_s = cr[0]
        tz_name = cr[5] if len(cr) > 5 and cr[5] else DEF_TZ
        tz = safe_tz(tz_name); now = datetime.now(tz)
        if now.weekday() != 6 or now.strftime("%H:%M") != "09:00": continue
        week_end = now.date(); week_start = week_end - timedelta(days=6)
        rows = sheet.get_all_values()
        done_c = 0; missed_c = 0; snooze_c = 0
        day_done = {}; day_missed = {}
        done_items = []; missed_items = []
        for r in rows[1:]:
            if str(r[0]) != uid_s: continue
            if len(r) < 6: continue
            d = norm_date(r[2])
            try: rd = datetime.strptime(d, "%Y-%m-%d").date()
            except: continue
            if not (week_start <= rd <= week_end): continue
            m = r[1] if len(r) > 1 else "?"; t = norm_time(r[3]) if len(r) > 3 else ""
            if r[5] == "done":
                done_c += 1; wd = rd.strftime("%A")
                day_done[wd] = day_done.get(wd, 0) + 1
                done_items.append(f"  ✅ {fmt_time(t)} · {m}")
            elif r[5] == "missed":
                missed_c += 1; wd = rd.strftime("%A")
                day_missed[wd] = day_missed.get(wd, 0) + 1
                missed_items.append(f"  ✗ {fmt_time(t)} · {m}")
        total = done_c + missed_c
        if total == 0: continue
        pct = int(done_c / total * 100) if total > 0 else 0
        best_day = max(day_done, key=day_done.get) if day_done else "—"
        worst_day = max(day_missed, key=day_missed.get) if day_missed else "—"
        streak = 0
        for i in range(6, -1, -1):
            dd = week_end - timedelta(days=i)
            wd = dd.strftime("%A")
            if day_missed.get(wd, 0) == 0 and day_done.get(wd, 0) > 0: streak += 1
            else: break
        if pct >= 90: mood = "Outstanding! 🏆"
        elif pct >= 70: mood = "Keep it up! 💪"
        elif pct >= 50: mood = "Room to improve 📈"
        else: mood = "Let's do better next week 🎯"
        ws = week_start.strftime("%-d %b"); we = week_end.strftime("%-d %b")
        lines = [hdr("📊 Weekly Report"), f"{ws} — {we}", "",
                 f"✅ Completed: {done_c}/{total} ({pct}%)",
                 f"❌ Missed: {missed_c}"]
        if streak: lines.append(f"🔥 Streak: {streak} days without missing!")
        lines.extend(["", f"📅 Most Productive: {best_day}"])
        if worst_day != "—": lines.append(f"📉 Most Missed: {worst_day}")
        lines.extend(["", mood])
        text = "\n".join(lines)
        detail_text = text
        if done_items:
            detail_text += "\n\n<b>Completed:</b>\n" + "\n".join(done_items[:10])
        if missed_items:
            detail_text += "\n\n<b>Missed:</b>\n" + "\n".join(missed_items[:10])
        try:
            msg = await ctx.bot.send_message(chat_id=int(uid_s), text=text, reply_markup=IKM([[IKB("📋 Details", callback_data=f"wrdet_{msg.message_id}")]]), parse_mode="HTML")
            ctx.bot_data[f"wrdet_{msg.message_id}"] = detail_text
            ctx.bot_data[f"wrsum_{msg.message_id}"] = text
        except: pass

# ========== MAIN ==========
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("add", "New reminder"),
        BotCommand("list", "All reminders"),
        BotCommand("month", "Monthly schedule"),
        BotCommand("settings", "Bot settings"),
        BotCommand("info", "About this bot"),
    ], scope={"type":"all_private_chats"})
    await app.bot.set_my_commands([
        BotCommand("start", "Bot info & commands"),
        BotCommand("remind", "Group reminder"),
        BotCommand("list", "Active reminders"),
    ], scope={"type":"all_group_chats"})

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(check_reminders, interval=60, first=0)
    app.job_queue.run_repeating(check_digest, interval=60, first=30)
    app.job_queue.run_repeating(check_weekly_report, interval=60, first=45)
    print("🚀 Bot Running")
    app.run_polling()

if __name__ == "__main__":
    main()
