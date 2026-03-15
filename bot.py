import logging, os, json, re, time as _time
from datetime import datetime, timedelta, date
from calendar import monthcalendar
import pytz
from telegram import Update, InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM, ForceReply, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, JobQueue

# ═══════════════════ CONFIG ═══════════════════
TOKEN = "8235103406:AAFYJ2SNRW4A4AAEyz8t2h-5BeYk8rnzzwE"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1bHeyDgw9P-3iRLOp_6VpHGKSn9St6yjyqP-35hPg6Rs/edit?pli=1&gid=0#gid=0"

DEF_TZ = "Asia/Kolkata"
DEF_RETRIES = 3; DEF_RETRY_GAP = 10; DEF_DIGEST_TIME = "07:00"
DAYS = ["mon","tue","wed","thu","fri","sat","sun"]
DAY_NAMES = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
DAY_FULL = {"mon":"monday","tue":"tuesday","wed":"wednesday","thu":"thursday","fri":"friday","sat":"saturday","sun":"sunday"}
ST_IC = {"active":"○","pending":"●","snoozed":"⏸","done":"✅","missed":"✗","cancelled":"✕"}
ST_LB = {"active":"Active","pending":"Pending","snoozed":"Snoozed","done":"Done","missed":"Missed","cancelled":"Cancelled"}
GT_IC = {"waiting":"⏳","pending":"⏳","done":"✅","missed":"✗","snoozed":"⏸","skipped":"⏭"}
SNOOZE_OPTS = [(15,"15m"),(30,"30m"),(45,"45m"),(60,"1h"),(120,"2h"),(180,"3h"),(300,"5h"),(480,"8h"),(720,"12h")]
TZ_DATA = {
    "🌏 Asia":{"Asia/Kolkata":"India +5:30","Asia/Dubai":"UAE +4","Asia/Karachi":"Pakistan +5","Asia/Dhaka":"Bangladesh +6","Asia/Bangkok":"Thailand +7","Asia/Singapore":"Singapore +8","Asia/Shanghai":"China +8","Asia/Tokyo":"Japan +9","Asia/Seoul":"Korea +9","Asia/Jakarta":"Indonesia +7","Asia/Riyadh":"Saudi +3","Asia/Manila":"Philippines +8"},
    "🌍 Europe":{"Europe/London":"UK +0","Europe/Berlin":"Germany +1","Europe/Paris":"France +1","Europe/Moscow":"Russia +3","Europe/Istanbul":"Turkey +3"},
    "🌎 Americas":{"America/New_York":"US East -5","America/Chicago":"US Central -6","America/Denver":"US Mountain -7","America/Los_Angeles":"US West -8","America/Sao_Paulo":"Brazil -3","America/Mexico_City":"Mexico -6"},
    "🌏 Oceania":{"Australia/Sydney":"Australia +11","Pacific/Auckland":"NZ +12"},
    "🌍 Africa":{"Africa/Lagos":"Nigeria +1","Africa/Cairo":"Egypt +2","Africa/Nairobi":"Kenya +3","Africa/Johannesburg":"S.Africa +2"},
}
FILLER_RE = re.compile(r"^(remind\s+me\s+to|remind\s+me|reminder|remember\s+to|don'?t\s+forget\s+to|set\s+reminder)\s+", re.I)

# ═══════════════════ LOGGING ═══════════════════
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ═══════════════════ GOOGLE SHEETS ═══════════════════
import gspread
from oauth2client.service_account import ServiceAccountCredentials
_scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
_cj = os.environ.get("GOOGLE_CREDS")
if not _cj: raise Exception("GOOGLE_CREDS missing")
_creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(_cj), _scope)
client = gspread.authorize(_creds)
_wb = client.open_by_url(SHEET_URL)

def get_or_create_sheet(name, headers):
    try:
        ws = _wb.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = _wb.add_worksheet(name, rows=100, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
    return ws

sheet = get_or_create_sheet("Reminders", ["user_id","message","date","time","repeat","status","retry_count","group_id","task_id"])
cfg_sheet = get_or_create_sheet("Settings", ["user_id","digest_on","digest_time","max_retries","retry_gap","timezone","username","report_on"])
gm_sheet = get_or_create_sheet("GroupMembers", ["group_id","user_id","first_name","username","subscribed"])
tm_sheet = get_or_create_sheet("TaskMembers", ["task_id","user_id","first_name","status"])

# ═══════════════════ FORMATTERS ═══════════════════
def hdr(t): return f"<b>{t}</b>\n━━━━━━━━━━━━━━━━━━━━"
def detail(m, d, t, r=None):
    parts = [m, f"{fmt_ds(d)} · {fmt_t12(t)}"]
    if r and r != "none": parts[1] += f" · {fmt_rep(r)}"
    return "\n".join(parts)
def fmt_ds(d):
    try:
        dt = datetime.strptime(str(d), "%Y-%m-%d")
        return dt.strftime("%-d %b")
    except: return str(d)
def fmt_t12(t):
    try:
        dt = datetime.strptime(str(t), "%H:%M")
        return dt.strftime("%-I:%M %p")
    except: return str(t)
def fmt_rep(r):
    if not r or r == "none": return "Once"
    if r.startswith("custom:"):
        ds = r.replace("custom:","").split(",")
        if set(ds) == {"mon","tue","wed","thu","fri"}: return "Mon–Fri"
        if set(ds) == {"sat","sun"}: return "Weekends"
        if len(ds) == 7: return "Daily"
        return ", ".join(d.capitalize() for d in ds)
    return r.capitalize()
def fmt_date_day(d):
    try:
        dt = datetime.strptime(str(d), "%Y-%m-%d")
        return dt.strftime("%-d %b, %a")
    except: return str(d)

# ═══════════════════ NORMALIZERS ═══════════════════
def norm_date(v):
    s = str(v).strip()
    if not s: return ""
    try:
        f = float(s)
        if 40000 < f < 100000:
            return (datetime(1899,12,30) + timedelta(days=int(f))).strftime("%Y-%m-%d")
    except: pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try: return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except: pass
    return s
def norm_time(v):
    s = str(v).strip()
    if not s: return ""
    try:
        f = float(s)
        h = int(f * 24); m = int((f * 24 - h) * 60)
        return f"{h:02d}:{m:02d}"
    except: pass
    return s

# ═══════════════════ SETTINGS ═══════════════════
def get_cfg(uid):
    uid_s = str(uid)
    rows = cfg_sheet.get_all_values()
    for i, r in enumerate(rows):
        if r and r[0] == uid_s:
            return {"row": i+1, "digest_on": r[1] if len(r)>1 else "true", "digest_time": r[2] if len(r)>2 else DEF_DIGEST_TIME,
                    "max_retries": int(r[3]) if len(r)>3 and r[3] else DEF_RETRIES, "retry_gap": int(r[4]) if len(r)>4 and r[4] else DEF_RETRY_GAP,
                    "tz": r[5] if len(r)>5 and r[5] else DEF_TZ, "username": r[6] if len(r)>6 else "", "report_on": r[7] if len(r)>7 else "true"}
    cfg_sheet.append_row([uid_s, "true", DEF_DIGEST_TIME, DEF_RETRIES, DEF_RETRY_GAP, DEF_TZ, "", "true"], value_input_option="RAW")
    rows2 = cfg_sheet.get_all_values()
    for i, r in enumerate(rows2):
        if r and r[0] == uid_s: return {"row": i+1, "digest_on": "true", "digest_time": DEF_DIGEST_TIME, "max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP, "tz": DEF_TZ, "username": "", "report_on": "true"}
    return {"row": len(rows2), "digest_on": "true", "digest_time": DEF_DIGEST_TIME, "max_retries": DEF_RETRIES, "retry_gap": DEF_RETRY_GAP, "tz": DEF_TZ, "username": "", "report_on": "true"}
def save_cfg(uid, key, val):
    cfg = get_cfg(uid)
    col_map = {"digest_on":2,"digest_time":3,"max_retries":4,"retry_gap":5,"tz":6,"username":7,"report_on":8}
    if key in col_map: cfg_sheet.update_cell(cfg["row"], col_map[key], val)
def get_tz(uid):
    try: return pytz.timezone(get_cfg(uid)["tz"])
    except: return pytz.timezone(DEF_TZ)
def update_username(user):
    if not user or not user.username: return
    uid_s, uname = str(user.id), user.username
    rows = cfg_sheet.get_all_values()
    for i, r in enumerate(rows):
        if r and r[0] == uid_s:
            cur = r[6] if len(r) > 6 else ""
            if cur != uname:
                if len(r) < 7:
                    while len(r) < 7: cfg_sheet.update_cell(i+1, len(r)+1, "")
                cfg_sheet.update_cell(i+1, 7, uname)
            return
    get_cfg(user.id)
    save_cfg(user.id, "username", uname)

# ═══════════════════ REMINDER HELPERS ═══════════════════
def get_rm(row):
    r = sheet.row_values(row)
    if not r or len(r) < 7: return None
    return r
def get_detail(r):
    return {"msg": r[1], "date": norm_date(r[2]), "time": norm_time(r[3]), "repeat": r[4] if len(r)>4 else "none", "status": r[5] if len(r)>5 else "active"}
def rd(row):
    r = get_rm(row)
    if not r: return None, None
    return r, get_detail(r)
def handled(r):
    return len(r) > 5 and r[5] not in ("pending", "snoozed")
def is_past(ds, ts, tz):
    try:
        now = datetime.now(tz)
        dt = datetime.strptime(f"{ds} {ts}", "%Y-%m-%d %H:%M")
        dt = tz.localize(dt)
        return dt < now
    except: return False
def past_msg(ts): return f"⚠ {fmt_t12(ts)} has already passed today.\nEnter a future time:"
def advance_rep(row, r):
    rep = r[4] if len(r) > 4 else "none"
    if rep == "none":
        sheet.update_cell(row, 6, "done")
        return
    d = datetime.strptime(norm_date(r[2]), "%Y-%m-%d")
    if rep == "daily": nd = d + timedelta(days=1)
    elif rep == "weekly": nd = d + timedelta(days=7)
    elif rep == "monthly":
        m, y = d.month + 1, d.year
        if m > 12: m, y = 1, y + 1
        try: nd = d.replace(year=y, month=m)
        except: nd = d.replace(year=y, month=m, day=28)
    elif rep.startswith("custom:"):
        ds = rep.replace("custom:", "").split(",")
        nd = d + timedelta(days=1)
        for _ in range(7):
            if DAYS[nd.weekday()] in ds: break
            nd += timedelta(days=1)
    else: nd = d + timedelta(days=1)
    sheet.update_cell(row, 3, nd.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, "active")
    sheet.update_cell(row, 7, 0)

# ═══════════════════ CALENDAR ═══════════════════
def cal_kb(year, month, tz, back_cb="cancel", back_txt="✕ Cancel"):
    now = datetime.now(tz); today = now.date()
    rows = []
    rows.append([IKB(f"{datetime(year, month, 1).strftime('%B %Y')}", callback_data="noop")])
    rows.append([IKB(d, callback_data="noop") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]])
    cal = monthcalendar(year, month)
    for week in cal:
        all_past = all((d == 0 or date(year, month, d) < today) for d in week)
        if all_past: continue
        r = []
        for d in week:
            if d == 0: r.append(IKB(" ", callback_data="noop"))
            elif date(year, month, d) < today: r.append(IKB("·", callback_data="noop"))
            else:
                lbl = f"[{d}]" if date(year, month, d) == today else str(d)
                r.append(IKB(lbl, callback_data=f"day_{year}-{month:02d}-{d:02d}"))
        rows.append(r)
    btns = []
    btns.append(IKB("Today", callback_data=f"day_{today.strftime('%Y-%m-%d')}"))
    tmr = today + timedelta(days=1)
    btns.append(IKB("Tomorrow", callback_data=f"day_{tmr.strftime('%Y-%m-%d')}"))
    rows.append(btns)
    nav = []
    pm, py = (month - 1, year) if month > 1 else (12, year - 1)
    nm, ny = (month + 1, year) if month < 12 else (1, year + 1)
    if date(py, pm, 1) >= today.replace(day=1) or (py == today.year and pm == today.month):
        nav.append(IKB("‹", callback_data=f"cal_{py}_{pm}"))
    nav.append(IKB("›", callback_data=f"cal_{ny}_{nm}"))
    rows.append(nav)
    rows.append([IKB(back_txt, callback_data=back_cb)])
    return IKM(rows)

# ═══════════════════ TIME PARSER ═══════════════════
def parse_time(text):
    t = text.strip().lower().replace(".",":")
    m = re.match(r'^(\d{1,2})(?:[:](\d{1,2}))?\s*(am|pm)?$', t)
    if not m: return None
    h, mn, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if h == 12: h = 0 if ap == "am" else 12
        elif ap == "pm": h += 12
    if h > 23 or mn > 59: return None
    return f"{h:02d}:{mn:02d}"

# ═══════════════════ NL PARSER ═══════════════════
def _find_time(text):
    pats = [r'(?:at|by)\s+(\d{1,2}(?:[.:]\d{1,2})?)\s*(am|pm)', r'(?:at|by)\s+(\d{1,2}:\d{2})', r'(\d{1,2}(?:[.:]\d{1,2})?)\s*(am|pm)']
    for p in pats:
        m = re.search(p, text, re.I)
        if m:
            raw = m.group(1).replace(".", ":")
            ap = m.group(2) if m.lastindex >= 2 else None
            t = parse_time(f"{raw} {ap}" if ap else raw)
            if t: return t, m.start(), m.end()
    return None, 0, 0

def _find_date(text, tz):
    now = datetime.now(tz); today = now.date(); low = text.lower()
    pats = [
        (r'\b(today|tonight)\b', lambda m: today),
        (r'\b(tmrw|tmr|tomorrow)\b', lambda m: today + timedelta(days=1)),
        (r'\bday\s+after\s+tomorrow\b', lambda m: today + timedelta(days=2)),
        (r'\bnext\s+week\b', lambda m: today + timedelta(days=7)),
    ]
    for dn_short, dn_full in [("mon","monday"),("tue","tuesday"),("wed","wednesday"),("thu","thursday"),("fri","friday"),("sat","saturday"),("sun","sunday")]:
        def _mk(full=dn_full):
            idx = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"].index(full)
            def fn(m):
                diff = (idx - today.weekday()) % 7
                if diff == 0: diff = 7
                return today + timedelta(days=diff)
            return fn
        pats.append((rf'\b(?:on\s+)?{dn_full}\b', _mk()))
        if dn_short != dn_full[:3]: continue
        pats.append((rf'\b(?:on\s+)?{dn_short}\b', _mk()))
    months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
              "january":1,"february":2,"march":3,"april":4,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
    m = re.search(r'(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|march|april|june|july|august|september|october|november|december)', low)
    if m:
        dy, mo = int(m.group(1)), months.get(m.group(2))
        if mo:
            yr = today.year if date(today.year, mo, dy) >= today else today.year + 1
            try: return date(yr, mo, dy), m.start(), m.end()
            except: pass
    m = re.search(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|march|april|june|july|august|september|october|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?', low)
    if m:
        mo, dy = months.get(m.group(1)), int(m.group(2))
        if mo:
            yr = today.year if date(today.year, mo, dy) >= today else today.year + 1
            try: return date(yr, mo, dy), m.start(), m.end()
            except: pass
    m = re.search(r'(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)\b', low)
    if m:
        dy = int(m.group(1))
        if 1 <= dy <= 31:
            mo, yr = today.month, today.year
            try:
                d = date(yr, mo, dy)
                if d < today: mo += 1
                if mo > 12: mo, yr = 1, yr + 1
                return date(yr, mo, dy), m.start(), m.end()
            except: pass
    for p, fn in pats:
        m = re.search(p, low)
        if m: return fn(m), m.start(), m.end()
    return None, 0, 0

def _find_repeat(text, tz):
    low = text.lower()
    now = datetime.now(tz); today = now.date()
    for dn in ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]:
        m = re.search(rf'\bevery\s+{dn[:3]}(?:\w*)\b', low)
        if m:
            idx = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"].index(dn)
            diff = (idx - today.weekday()) % 7
            if diff == 0: diff = 7
            return "weekly", m.start(), m.end(), today + timedelta(days=diff)
    if re.search(r'\bevery\s*day\b', low):
        m = re.search(r'\bevery\s*day\b', low)
        return "daily", m.start(), m.end(), None
    for word, val in [("daily","daily"),("weekly","weekly"),("monthly","monthly")]:
        m = re.search(rf'\b{word}\b', low)
        if m: return val, m.start(), m.end(), None
    return None, 0, 0, None

def _find_relative(text, tz):
    now = datetime.now(tz); low = text.lower()
    m = re.search(r'(?:in|after)\s+(\d+)\s*(min(?:ute)?s?|hrs?|hours?|days?|weeks?|h|m)\b', low)
    if not m: return None, None, 0, 0
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("m"): dt = now + timedelta(minutes=n)
    elif unit.startswith("h"): dt = now + timedelta(hours=n)
    elif unit.startswith("d"): dt = now + timedelta(days=n)
    elif unit.startswith("w"): dt = now + timedelta(weeks=n)
    else: return None, None, 0, 0
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), m.start(), m.end()

def parse_nl(text, tz):
    spans = []; result = {"msg": None, "date": None, "time": None, "repeat": None}
    rd, rt, rs, re_ = _find_relative(text, tz)
    if rd:
        result["date"], result["time"] = rd, rt
        spans.append((rs, re_))
    else:
        t, ts, te = _find_time(text)
        if t: result["time"] = t; spans.append((ts, te))
        rep, rps, rpe, rep_date = _find_repeat(text, tz)
        if rep: result["repeat"] = rep; spans.append((rps, rpe))
        if rep_date: result["date"] = rep_date.strftime("%Y-%m-%d")
        d, ds, de = _find_date(text, tz)
        if d and not result["date"]: result["date"] = d.strftime("%Y-%m-%d"); spans.append((ds, de))
    spans.sort(key=lambda x: x[0], reverse=True)
    msg = text
    for s, e in spans: msg = msg[:s] + msg[e:]
    msg = FILLER_RE.sub("", msg).strip(" ,.\n\t")
    msg = re.sub(r'\s+', ' ', msg).strip()
    if msg: result["msg"] = msg
    has_trigger = result["time"] or result["date"] or result["repeat"] or FILLER_RE.match(text.strip())
    if not has_trigger or not msg: return None
    if not result["date"] and result["time"]:
        if is_past(datetime.now(tz).strftime("%Y-%m-%d"), result["time"], tz):
            result["date"] = (datetime.now(tz).date() + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            result["date"] = datetime.now(tz).strftime("%Y-%m-%d")
    if not result["repeat"]: result["repeat"] = None
    return result

# ═══════════════════ UI KEYBOARDS ═══════════════════
def home_kb(): return IKM([[IKB("＋ New", callback_data="add")]])
def cancel_kb(): return IKM([[IKB("✕ Cancel", callback_data="cancel")]])
def repeat_kb():
    return IKM([[IKB("Daily", callback_data="rep_daily"), IKB("Weekly", callback_data="rep_weekly")],
                [IKB("Monthly", callback_data="rep_monthly"), IKB("Customize", callback_data="cusrep")],
                [IKB("✕ Cancel", callback_data="cancel")]])
def snz_kb(row):
    rows = []
    for i in range(0, len(SNOOZE_OPTS), 3):
        rows.append([IKB(lbl, callback_data=f"snz_{row}_{mins}") for mins, lbl in SNOOZE_OPTS[i:i+3]])
    rows.append([IKB("« Back", callback_data=f"snzb_{row}")])
    return IKM(rows)
def remind_kb(row): return IKM([[IKB("Snooze", callback_data=f"snzp_{row}"), IKB("Done", callback_data=f"done_{row}")]])
def saved_kb(row): return IKM([[IKB("🔁 Repeat", callback_data=f"chrep_{row}"), IKB("＋ New", callback_data="add")]])
def saved_kb_norep(): return home_kb()
def cus_day_kb(selected, back_cb="cancel"):
    rows = []
    r1, r2 = [], []
    for i, d in enumerate(DAYS):
        lbl = f"[{DAY_NAMES[i]}]" if d in selected else DAY_NAMES[i]
        btn = IKB(lbl, callback_data=f"cusday_{d}")
        if i < 4: r1.append(btn)
        else: r2.append(btn)
    rows.append(r1); rows.append(r2)
    rows.append([IKB("Mon–Fri", callback_data="cuswk"), IKB("All", callback_data="cusall"), IKB("Clear", callback_data="cusclear")])
    if selected: rows.append([IKB("✓ Save", callback_data="cussave")])
    rows.append([IKB("« Back", callback_data=back_cb)])
    return IKM(rows)

# ═══════════════════ MESSAGE UTILS ═══════════════════
async def safe_edit(msg, text, kb=None):
    try: return await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except: return None
def store_prompt(ud, msg):
    ud["p_mid"], ud["p_cid"] = msg.message_id, msg.chat_id
async def delete_prompt(ud, bot):
    mid, cid = ud.pop("p_mid", None), ud.pop("p_cid", None)
    if mid and cid:
        try: await bot.delete_message(cid, mid)
        except: pass
def rm_home(ud, bot):
    mid, cid = ud.pop("h_mid", None), ud.pop("h_cid", None)
    if mid and cid:
        try:
            import asyncio
            asyncio.create_task(bot.edit_message_reply_markup(cid, mid, reply_markup=None))
        except: pass

# ═══════════════════ AUTO-MINIMIZE ═══════════════════
async def p_auto_minimize(ctx):
    d = ctx.job.data
    mid, cid, min_text, show_cb = d["mid"], d["cid"], d["min_text"], d["show_cb"]
    try:
        await ctx.bot.edit_message_text(min_text, chat_id=cid, message_id=mid, reply_markup=IKM([[IKB("📋 Show", callback_data=f"{show_cb}_{mid}")]]), parse_mode="HTML")
        ctx.bot_data[f"pmin_{mid}"] = d.get("full_text", "")
        ctx.bot_data[f"pminkb_{mid}"] = d.get("full_kb")
    except: pass

def schedule_minimize(ctx, msg, min_text, show_cb, full_text, full_kb=None, delay=60):
    ctx.job_queue.run_once(p_auto_minimize, delay, data={"mid": msg.message_id, "cid": msg.chat_id, "min_text": min_text, "show_cb": show_cb, "full_text": full_text, "full_kb": full_kb})

# ═══════════════════ GROUP HELPERS ═══════════════════
def set_gsub(gid, uid, fname, uname, sub=True):
    gid_s, uid_s = str(gid), str(uid)
    rows = gm_sheet.get_all_values()
    for i, r in enumerate(rows):
        if r and len(r) >= 2 and r[0] == gid_s and r[1] == uid_s:
            gm_sheet.update_cell(i+1, 3, fname)
            if uname: gm_sheet.update_cell(i+1, 4, uname)
            gm_sheet.update_cell(i+1, 5, str(sub).lower())
            return
    gm_sheet.append_row([gid_s, uid_s, fname, uname or "", str(sub).lower()], value_input_option="RAW")
def get_gsubs(gid):
    gid_s = str(gid); result = []
    for r in gm_sheet.get_all_values():
        if r and len(r) >= 5 and r[0] == gid_s and r[4] == "true":
            uname = r[3] if len(r) > 3 else ""
            result.append((r[1], r[2], uname))
    return result
def add_tmember(tid, uid, fname, status="waiting"):
    tm_sheet.append_row([tid, str(uid), fname, status], value_input_option="RAW")
def set_tstatus(tid, uid, status):
    uid_s = str(uid)
    for i, r in enumerate(tm_sheet.get_all_values()):
        if r and len(r) >= 2 and r[0] == tid and r[1] == uid_s:
            tm_sheet.update_cell(i+1, 4, status); return
def get_tmembers(tid):
    return [(r[1], r[2], r[3]) for r in tm_sheet.get_all_values() if r and len(r) >= 4 and r[0] == tid]
def gstatus_text(tid, msg):
    members = get_tmembers(tid)
    active = [(u, n, s) for u, n, s in members if s != "skipped"]
    if not active: return f"{msg}\n\nNo subscribers"
    all_done = all(s in ("done","missed") for _, _, s in active)
    ic = "" if all_done else "⏰ "
    default_icon = "⏳"
    parts = [f"{GT_IC.get(s, default_icon)} {n}" for _, n, s in active]
    if all_done and all(s == "done" for _, _, s in active):
        return f"{msg} · ✅ All done\n{', '.join(n for _, n, _ in active)}"
    return f"{ic}{msg}\n\n{' · '.join(parts)}"

def extract_tag_texts(message):
    if not message or not message.entities: return []
    tags = []
    for ent in message.entities:
        if ent.type == "mention":
            raw = message.text[ent.offset:ent.offset + ent.length]
            uname = raw.lstrip("@").lower()
            if uname: tags.append(uname)
        elif ent.type == "text_mention" and ent.user:
            tags.append(str(ent.user.id))
    return tags

def is_subscriber_tagged(sub_uid, sub_fname, sub_uname, tags):
    for tag in tags:
        if tag == str(sub_uid): return True
        if sub_uname and tag.lower() == sub_uname.lower(): return True
        if sub_fname and tag.lower() == sub_fname.lower(): return True
    return False

# ═══════════════════ SAVE REMINDER ═══════════════════
def save_reminder(uid, msg, ds, ts, rep="none", gid="", tid=""):
    sheet.append_row([str(uid), msg, ds, ts, rep, "active", 0, str(gid) if gid else "", tid], value_input_option="RAW")
    rows = sheet.get_all_values()
    for i in range(len(rows)-1, 0, -1):
        if rows[i][0] == str(uid) and rows[i][1] == msg and rows[i][2] == ds and rows[i][3] == ts:
            return i + 1
    return len(rows)

# ═══════════════════ COMMANDS ═══════════════════
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    if update.effective_chat.type != "private":
        text = f"{hdr('Smart Reminder Bot')}\n\n<b>Commands</b>\n/remind — Group reminder\n/list — Active reminders\n\n<b>Examples</b>\n<code>/remind Buy milk at 5pm</code>\n<code>/remind Meeting tomorrow 10am daily</code>\n<code>/remind</code> — step-by-step\n\nTag members to assign:\n<code>/remind @user Submit report at 5pm</code>"
        msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=IKM([[IKB("✕ Close", callback_data="gclose")]]))
        min_text = hdr("Smart Reminder Bot")
        ctx.bot_data[f"gmin_{msg.message_id}"] = {"min_text": min_text, "full_text": text, "show_cb": f"gshow_start_{msg.message_id}"}
        ctx.job_queue.run_once(g_auto_minimize, 30, data={"mid": msg.message_id, "cid": msg.chat_id, "min_text": min_text, "show_cb": f"gshow_start_{msg.message_id}", "full_text": text})
        return
    ctx.user_data.clear()
    ht = f"{hdr('Smart Reminder Bot')}\n\nType a reminder:\n<i>\"Buy milk tomorrow at 5pm\"</i>\n<i>\"Meeting in 30 min\"</i>\n\nOr tap ＋ New for step-by-step."
    msg = await update.message.reply_text(ht, reply_markup=home_kb(), parse_mode="HTML")
    ctx.user_data["h_mid"], ctx.user_data["h_cid"] = msg.message_id, msg.chat_id

async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /remind here for group reminders.")
        return
    ctx.user_data.clear()
    rm_home(ctx.user_data, ctx.bot)
    msg = await update.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
    ctx.user_data["step"] = "message"
    store_prompt(ctx.user_data, msg)

async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    if update.effective_chat.type != "private":
        await _group_list(update, ctx)
        return
    await _show_list(update.message, update.effective_user.id, ctx, new=True)

async def _show_list(target, uid, ctx, new=True):
    uid_s = str(uid); rows = sheet.get_all_values(); items = []
    for i, r in enumerate(rows):
        if not r or r[0] != uid_s: continue
        if len(r) < 6: continue
        if r[5] in ("active","pending","snoozed","missed"):
            d = get_detail(r)
            items.append((i+1, d))
    if not items:
        text = f"{hdr('Reminders')}\n\nNo active reminders."
        if new: msg = await target.reply_text(text, reply_markup=IKM([[IKB("✕ Close", callback_data="pclose_list")], [IKB("＋ New", callback_data="add")]]), parse_mode="HTML")
        else: msg = await safe_edit(target, text, IKM([[IKB("✕ Close", callback_data="pclose_list")], [IKB("＋ New", callback_data="add")]]))
        if msg: schedule_minimize(ctx, msg, "📋 No active reminders", "pshow_list", text, IKM([[IKB("✕ Close", callback_data="pclose_list")], [IKB("＋ New", callback_data="add")]]))
        return
    lines = [hdr("Reminders"), ""]
    for row, d in items:
        ic = ST_IC.get(d["status"], "○")
        m_short = d["msg"][:25] + "…" if len(d["msg"]) > 25 else d["msg"]
        lines.append(f"{items.index((row,d))+1} {ic} {m_short}")
        lines.append(f"   {fmt_ds(d['date'])} · {fmt_t12(d['time'])}")
    text = "\n".join(lines)
    btns = []
    for idx, (row, d) in enumerate(items):
        btns.append(IKB(str(idx+1), callback_data=f"view_{row}"))
    btn_rows = [btns[i:i+5] for i in range(0, len(btns), 5)]
    btn_rows.append([IKB("✕ Close", callback_data="pclose_list")])
    btn_rows.append([IKB("＋ New", callback_data="add")])
    kb = IKM(btn_rows)
    if new: msg = await target.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else: msg = await safe_edit(target, text, kb)
    if msg:
        cnt = len(items)
        schedule_minimize(ctx, msg, f"📋 Reminders ({cnt} active)", "pshow_list", text, kb)

async def info_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    text = f"""{hdr('Smart Reminder Bot')}

<b>Features</b>
• Natural language: <i>Buy milk tomorrow at 5pm</i>
• Relative time: <i>Call mom in 30 min</i>
• Recurring: <i>Gym at 6pm daily</i>
• Day-specific: <i>Meeting every monday 10am</i>
• Custom days: Mon–Fri only
• Smart snooze: 15m to 12h
• Auto-retry if missed
• Daily digest every morning
• Weekly report with analytics
• Monthly schedule view
• Group reminders with /remind
• Per-user timezone

<b>Commands</b>
/add — Step-by-step reminder
/list — View reminders
/month — Monthly schedule
/settings — Preferences
/info — This page"""
    kb = IKM([[IKB("✕ Close", callback_data="pclose_info")], [IKB("＋ New", callback_data="add")]])
    msg = await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    schedule_minimize(ctx, msg, "ℹ️ Info", "pshow_info", text, kb)

async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    if update.effective_chat.type != "private": return
    await _show_settings(update.message, update.effective_user.id, ctx, new=True)

async def _show_settings(target, uid, ctx, new=True):
    cfg = get_cfg(uid)
    tz_name = cfg["tz"].split("/")[-1].replace("_"," ")
    text = f"""{hdr('Settings')}

Daily Digest: {'ON' if cfg['digest_on']=='true' else 'OFF'} · {fmt_t12(cfg['digest_time'])}
Max Retries: {cfg['max_retries']}×
Retry Gap: {cfg['retry_gap']} min
Timezone: {tz_name}
Weekly Report: {'ON' if cfg.get('report_on','true')=='true' else 'OFF'}"""
    kb = IKM([
        [IKB(f"Digest: {'ON' if cfg['digest_on']=='true' else 'OFF'}", callback_data="cfg_digest"), IKB(f"⏰ {fmt_t12(cfg['digest_time'])}", callback_data="cfg_dtime")],
        [IKB(f"Retries: {cfg['max_retries']}×", callback_data="cfg_retries"), IKB(f"Gap: {cfg['retry_gap']}m", callback_data="cfg_gap")],
        [IKB(f"🌍 {tz_name}", callback_data="cfg_tz")],
        [IKB(f"Report: {'ON' if cfg.get('report_on','true')=='true' else 'OFF'}", callback_data="cfg_report")],
        [IKB("« Back", callback_data="home")],
    ])
    if new: await target.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else: await safe_edit(target, text, kb)

# ═══════════════════ MONTH COMMAND ═══════════════════
async def month_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_username(update.effective_user)
    if update.effective_chat.type != "private": return
    now = datetime.now(get_tz(update.effective_user.id))
    await _show_month(update.message, update.effective_user.id, now.year, now.month, ctx, new=True)

async def _show_month(target, uid, year, month, ctx, new=True):
    tz = get_tz(uid); now = datetime.now(tz); today = now.date()
    uid_s = str(uid); rows = sheet.get_all_values()
    month_start = date(year, month, 1)
    if month == 12: month_end = date(year+1, 1, 1) - timedelta(days=1)
    else: month_end = date(year, month+1, 1) - timedelta(days=1)
    # Split into 4 weeks
    weeks = []
    ws = month_start
    for w in range(4):
        if w < 3:
            we = ws + timedelta(days=6)
            if we > month_end: we = month_end
        else:
            we = month_end
        weeks.append((ws, we))
        ws = we + timedelta(days=1)
        if ws > month_end: break
    # Count reminders per week
    all_rems = []
    for i, r in enumerate(rows):
        if not r or r[0] != uid_s or len(r) < 6: continue
        ds = norm_date(r[2])
        if not ds: continue
        try: rd_ = datetime.strptime(ds, "%Y-%m-%d").date()
        except: continue
        d = get_detail(r)
        all_rems.append((rd_, d, r))
    # Expand recurring into month
    expanded = {}
    for rd_, d, r in all_rems:
        rep = d["repeat"]
        if rep == "none" or not rep:
            if month_start <= rd_ <= month_end:
                expanded.setdefault(rd_, []).append(d)
        elif rep == "daily":
            sd = max(rd_, month_start)
            while sd <= month_end:
                expanded.setdefault(sd, []).append(d)
                sd += timedelta(days=1)
        elif rep == "weekly":
            sd = rd_
            while sd <= month_end:
                if sd >= month_start: expanded.setdefault(sd, []).append(d)
                sd += timedelta(days=7)
        elif rep == "monthly":
            try:
                md = rd_.replace(month=month, year=year)
                if month_start <= md <= month_end: expanded.setdefault(md, []).append(d)
            except: pass
        elif rep.startswith("custom:"):
            cdays = rep.replace("custom:", "").split(",")
            sd = max(rd_, month_start)
            while sd <= month_end:
                if DAYS[sd.weekday()] in cdays: expanded.setdefault(sd, []).append(d)
                sd += timedelta(days=1)
    mn = datetime(year, month, 1).strftime("%B %Y")
    lines = [hdr(f"📅 {mn}"), ""]
    total = sum(len(v) for v in expanded.values())
    done_c = sum(1 for ds in expanded.values() for d in ds if d["status"] == "done")
    missed_c = sum(1 for ds in expanded.values() for d in ds if d["status"] == "missed")
    upcoming = total - done_c - missed_c
    cur_week = None
    for wi, (ws, we) in enumerate(weeks):
        cnt = sum(len(expanded.get(ws + timedelta(days=x), [])) for x in range((we - ws).days + 1))
        marker = " ◂" if (today >= ws and today <= we) else ""
        if today >= ws and today <= we: cur_week = wi
        lines.append(f"W{wi+1}: {ws.strftime('%-d %b')}–{we.strftime('%-d %b')} · {cnt} reminder{'s' if cnt != 1 else ''}{marker}")
    lines.append("")
    lines.append(f"Total: {total} · ✅ {done_c} done · ✗ {missed_c} missed · ○ {upcoming} upcoming")
    text = "\n".join(lines)
    btns = [IKB(str(i+1), callback_data=f"mw_{year}_{month}_{i}") for i in range(len(weeks))]
    btn_rows = [btns]
    nav = []
    pm, py = (month-1, year) if month > 1 else (12, year-1)
    nm, ny = (month+1, year) if month < 12 else (1, year+1)
    nav.append(IKB(f"‹ {datetime(py,pm,1).strftime('%b')}", callback_data=f"mn_{py}_{pm}"))
    nav.append(IKB(f"{datetime(ny,nm,1).strftime('%b')} ›", callback_data=f"mn_{ny}_{nm}"))
    btn_rows.append(nav)
    btn_rows.append([IKB("✕ Close", callback_data="pclose_month")])
    kb = IKM(btn_rows)
    if new: msg = await target.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else: msg = await safe_edit(target, text, kb); msg = target if msg is None else msg
    if msg and hasattr(msg, 'message_id'):
        schedule_minimize(ctx, msg, f"📅 {mn}", "pshow_month", text, kb)

async def _show_week(target, uid, year, month, week_idx, ctx):
    tz = get_tz(uid); now = datetime.now(tz); today = now.date()
    month_start = date(year, month, 1)
    if month == 12: month_end = date(year+1, 1, 1) - timedelta(days=1)
    else: month_end = date(year, month+1, 1) - timedelta(days=1)
    weeks = []
    ws = month_start
    for w in range(4):
        if w < 3:
            we = ws + timedelta(days=6)
            if we > month_end: we = month_end
        else:
            we = month_end
        weeks.append((ws, we))
        ws = we + timedelta(days=1)
        if ws > month_end: break
    if week_idx >= len(weeks): return
    ws, we = weeks[week_idx]
    uid_s = str(uid); rows = sheet.get_all_values()
    all_rems = []
    for i, r in enumerate(rows):
        if not r or r[0] != uid_s or len(r) < 6: continue
        ds = norm_date(r[2])
        if not ds: continue
        try: rd_ = datetime.strptime(ds, "%Y-%m-%d").date()
        except: continue
        d = get_detail(r)
        all_rems.append((rd_, d, r))
    expanded = {}
    for rd_, d, r in all_rems:
        rep = d["repeat"]
        if rep == "none" or not rep:
            if ws <= rd_ <= we: expanded.setdefault(rd_, []).append(d)
        elif rep == "daily":
            sd = max(rd_, ws)
            while sd <= we:
                expanded.setdefault(sd, []).append(d)
                sd += timedelta(days=1)
        elif rep == "weekly":
            sd = rd_
            while sd <= we:
                if sd >= ws: expanded.setdefault(sd, []).append(d)
                sd += timedelta(days=7)
        elif rep == "monthly":
            try:
                md = rd_.replace(month=month, year=year)
                if ws <= md <= we: expanded.setdefault(md, []).append(d)
            except: pass
        elif rep.startswith("custom:"):
            cdays = rep.replace("custom:", "").split(",")
            sd = max(rd_, ws)
            while sd <= we:
                if DAYS[sd.weekday()] in cdays: expanded.setdefault(sd, []).append(d)
                sd += timedelta(days=1)
    # Group recurring that appear every day
    daily_items = []
    non_daily = {}
    seen_msgs = {}
    for day in sorted(expanded.keys()):
        for d in expanded[day]:
            key = f"{d['msg']}_{d['time']}_{d['repeat']}"
            seen_msgs.setdefault(key, []).append(day)
    num_days = (we - ws).days + 1
    daily_grouped = set()
    for key, days_list in seen_msgs.items():
        if len(days_list) >= min(num_days, 5):
            daily_grouped.add(key)
    lines = [hdr(f"Week {week_idx+1}: {ws.strftime('%-d %b')}–{we.strftime('%-d %b')}"), ""]
    # Show daily grouped items first
    for key in daily_grouped:
        parts = key.split("_")
        msg_t, time_t = "_".join(parts[:-2]), parts[-2]
        ds_list = seen_msgs[key]
        if len(ds_list) == num_days:
            label = "Daily"
        else:
            label = ", ".join(DAY_NAMES[d.weekday()] for d in sorted(ds_list))
        ic = ST_IC.get("active", "○")
        lines.append(f"{label}")
        lines.append(f"  {ic} {msg_t} · {fmt_t12(time_t)}")
        lines.append("")
    # Show per-day items (excluding daily grouped)
    for day in sorted(expanded.keys()):
        day_items = []
        for d in expanded[day]:
            key = f"{d['msg']}_{d['time']}_{d['repeat']}"
            if key not in daily_grouped:
                day_items.append(d)
        if not day_items: continue
        if day == today:
            lines.append(f"<b>Today, {day.strftime('%-d %b, %a')}</b>")
        else:
            lines.append(f"{day.strftime('%-d %b, %a')}")
        for d in sorted(day_items, key=lambda x: x["time"]):
            ic = ST_IC.get(d["status"], "○")
            lines.append(f"  {ic} {d['msg']} · {fmt_t12(d['time'])}")
        lines.append("")
    if len(lines) <= 3:
        lines.append("No reminders this week.")
    text = "\n".join(lines)
    mn = datetime(year, month, 1).strftime("%B %Y")
    btns = []
    if week_idx + 1 < len(weeks):
        btns.append([IKB(f"Week {week_idx+2} ›", callback_data=f"mw_{year}_{month}_{week_idx+1}")])
    btns.append([IKB(f"« {mn}", callback_data=f"mb_{year}_{month}")])
    await safe_edit(target, text, IKM(btns))

# ═══════════════════ GROUP COMMANDS ═══════════════════
async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Use /remind in groups.\nFor private reminders, just type or use /add.")
        return
    user = update.effective_user; gid = update.effective_chat.id
    update_username(user)
    set_gsub(gid, user.id, user.first_name, user.username or "")
    text_after = (update.message.text or "").replace("/remind", "", 1).strip()
    tags = extract_tag_texts(update.message)
    if text_after:
        # Remove @mentions from the text for NL parsing
        clean_text = re.sub(r'@\w+', '', text_after).strip()
        tz = get_tz(user.id)
        result = parse_nl(clean_text, tz)
        if result:
            msg, ds, ts, rep = result["msg"], result.get("date"), result.get("time"), result.get("repeat") or "none"
            if msg and ds and ts:
                await _finish_group(update, ctx, msg, ds, ts, rep, gid, user, tags)
                return
            elif msg:
                ud = ctx.user_data; ud.clear()
                ud["step"] = "g_date" if ts else ("g_time" if ds else "g_date")
                ud["g_msg"] = msg; ud["g_chat"] = gid; ud["g_tags"] = tags
                if ts: ud["g_time"] = ts
                if ds: ud["g_date"] = ds
                if rep and rep != "none": ud["g_rep"] = rep
                if not ds:
                    tz = get_tz(user.id)
                    now = datetime.now(tz)
                    await update.message.reply_text(f"{msg}\n━━━━━━━━━━━━━━━━━━━━\n{fmt_t12(ts) if ts else ''}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz, "gcancel", "✕ Cancel"), parse_mode="HTML")
                else:
                    await update.message.reply_text(f"{msg}\n━━━━━━━━━━━━━━━━━━━━\n{fmt_ds(ds)}\n\nEnter time:\ne.g. 9pm, 9:30 PM, 21:30", reply_markup=ForceReply(selective=True), parse_mode="HTML")
                return
    # Step-by-step
    ud = ctx.user_data; ud.clear()
    ud["step"] = "g_message"; ud["g_chat"] = gid; ud["g_tags"] = tags
    await update.message.reply_text(f"{hdr('Group Reminder')}\nEnter message:", reply_markup=ForceReply(selective=True), parse_mode="HTML")

async def _finish_group(update, ctx, msg, ds, ts, rep, gid, user, tags):
    tid = f"t_{int(_time.time())}_{user.id}"
    row = save_reminder(user.id, msg, ds, ts, rep, gid, tid)
    # Add members
    subs = get_gsubs(gid)
    if tags:
        for uid_s, fname, uname in subs:
            if is_subscriber_tagged(uid_s, fname, uname, tags):
                add_tmember(tid, uid_s, fname, "waiting")
            else:
                add_tmember(tid, uid_s, fname, "skipped")
    else:
        for uid_s, fname, uname in subs: add_tmember(tid, uid_s, fname, "waiting")
    members = get_tmembers(tid)
    active = [(u, n) for u, n, s in members if s != "skipped"]
    tag_line = f"\nFor: {', '.join(n for _, n in active)}" if tags and active else f"\n{len(active)} subscribed" + (f": {', '.join(n for _, n in active)}" if active else "")
    text = f"{hdr('Group Reminder')}\n{detail(msg, ds, ts, rep)}\nBy {user.first_name}\n{tag_line}"
    kb = IKM([[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")], [IKB("🔁 Repeat", callback_data=f"grep_{tid}")]])
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    ctx.user_data.clear()

async def _group_list(update, ctx):
    gid = update.effective_chat.id; gid_s = str(gid)
    update_username(update.effective_user)
    set_gsub(gid, update.effective_user.id, update.effective_user.first_name, update.effective_user.username or "")
    rows = sheet.get_all_values(); items = []
    for i, r in enumerate(rows):
        if not r or len(r) < 8: continue
        if r[7] != gid_s: continue
        if len(r) > 5 and r[5] in ("active","pending","snoozed"):
            d = get_detail(r); items.append((i+1, d))
    if not items:
        text = f"{hdr('Group Reminders')}\n\nNo active reminders."
        msg = await update.message.reply_text(text, reply_markup=IKM([[IKB("✕ Close", callback_data="gclose")]]), parse_mode="HTML")
        ctx.bot_data[f"gmin_{msg.message_id}"] = {"min_text": "Group Reminders — No active", "full_text": text, "show_cb": f"gshow_list_{gid}_{msg.message_id}"}
        ctx.job_queue.run_once(g_auto_minimize, 30, data={"mid": msg.message_id, "cid": msg.chat_id, "min_text": "Group Reminders — No active", "show_cb": f"gshow_list_{gid}_{msg.message_id}", "full_text": text})
        return
    lines = [hdr("Group Reminders"), ""]
    for idx, (row, d) in enumerate(items):
        ic = ST_IC.get(d["status"], "○")
        m_short = d["msg"][:25] + "…" if len(d["msg"]) > 25 else d["msg"]
        lines.append(f"{idx+1} {ic} {m_short}")
        lines.append(f"   {fmt_ds(d['date'])} · {fmt_t12(d['time'])}")
    text = "\n".join(lines)
    btns = [IKB(str(idx+1), callback_data=f"gl_{row}_{gid}") for idx, (row, d) in enumerate(items)]
    btn_rows = [btns[i:i+5] for i in range(0, len(btns), 5)]
    btn_rows.append([IKB("✕ Close", callback_data="gclose")])
    kb = IKM(btn_rows)
    msg = await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    ctx.bot_data[f"gmin_{msg.message_id}"] = {"min_text": f"Group Reminders ({len(items)})", "full_text": text, "show_cb": f"gshow_list_{gid}_{msg.message_id}"}
    ctx.job_queue.run_once(g_auto_minimize, 60, data={"mid": msg.message_id, "cid": msg.chat_id, "min_text": f"Group Reminders ({len(items)})", "show_cb": f"gshow_list_{gid}_{msg.message_id}", "full_text": text})

async def g_auto_minimize(ctx):
    d = ctx.job.data
    try:
        show_cb = d["show_cb"]
        await ctx.bot.edit_message_text(d["min_text"], chat_id=d["cid"], message_id=d["mid"], reply_markup=IKM([[IKB("📋 Show", callback_data=show_cb)]]), parse_mode="HTML")
    except: pass

# ═══════════════════ WEEKLY REPORT ═══════════════════
async def check_weekly_report(ctx: ContextTypes.DEFAULT_TYPE):
    now_utc = datetime.now(pytz.utc)
    rows = cfg_sheet.get_all_values()
    for r in rows[1:]:
        if not r or len(r) < 6: continue
        uid_s = r[0]
        report_on = r[7] if len(r) > 7 else "true"
        if report_on != "true": continue
        try: tz = pytz.timezone(r[5] if r[5] else DEF_TZ)
        except: tz = pytz.timezone(DEF_TZ)
        now = now_utc.astimezone(tz)
        if now.weekday() != 6: continue  # Sunday only
        if now.strftime("%H:%M") != "09:00": continue
        await _send_weekly_report(ctx, int(uid_s), tz, now)

async def _send_weekly_report(ctx, uid, tz, now):
    week_end = now.date(); week_start = week_end - timedelta(days=6)
    uid_s = str(uid); rows = sheet.get_all_values()
    done_list, missed_list, snoozed = [], [], 0
    day_done, day_missed = {}, {}
    for r in rows:
        if not r or r[0] != uid_s or len(r) < 6: continue
        ds = norm_date(r[2])
        if not ds: continue
        try: rd_ = datetime.strptime(ds, "%Y-%m-%d").date()
        except: continue
        if not (week_start <= rd_ <= week_end): continue
        d = get_detail(r)
        if d["status"] == "done":
            done_list.append(d)
            dn = DAY_NAMES[rd_.weekday()]
            day_done[dn] = day_done.get(dn, 0) + 1
        elif d["status"] == "missed":
            missed_list.append(d)
            dn = DAY_NAMES[rd_.weekday()]
            day_missed[dn] = day_missed.get(dn, 0) + 1
    total = len(done_list) + len(missed_list)
    if total == 0: return
    pct = int(len(done_list) / total * 100) if total else 0
    best_day = max(day_done, key=day_done.get) if day_done else "—"
    worst_day = max(day_missed, key=day_missed.get) if day_missed else "—"
    streak = 0
    for i in range(6, -1, -1):
        d = week_end - timedelta(days=i)
        dn = DAY_NAMES[d.weekday()]
        if day_missed.get(dn, 0) == 0 and day_done.get(dn, 0) > 0: streak += 1
        elif day_done.get(dn, 0) == 0 and day_missed.get(dn, 0) == 0: continue
        else: break
    if pct >= 90: mood = "Outstanding! 🏆"
    elif pct >= 70: mood = "Keep it up! 💪"
    elif pct >= 50: mood = "Room to improve 📈"
    else: mood = "Let's do better next week 🎯"
    iso_week = now.isocalendar()[1]
    text = f"""{hdr('📊 Weekly Report')}
{week_start.strftime('%-d %b')} — {week_end.strftime('%-d %b')}

✅ Completed: {len(done_list)}/{total} ({pct}%)
❌ Missed: {len(missed_list)}
⏭ Snoozed: {snoozed} times

📅 Most Productive: {best_day}
📉 Most Missed: {worst_day}

🔥 Streak: {streak} day{'s' if streak != 1 else ''} without missing!

{mood}"""
    kb = IKM([[IKB("📋 Details", callback_data=f"wrdet_{now.year}_{iso_week}")]])
    try: await ctx.bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode="HTML")
    except: pass

# ═══════════════════ BUTTON HANDLER ═══════════════════
async def on_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data; ud = ctx.user_data; uid = q.from_user.id
    update_username(q.from_user)

    if d == "noop": return

    # Home
    if d == "home":
        ht = f"{hdr('Smart Reminder Bot')}\n\nType a reminder:\n<i>\"Buy milk tomorrow at 5pm\"</i>\n\nOr tap ＋ New."
        await safe_edit(q.message, ht, home_kb())
        ud["h_mid"], ud["h_cid"] = q.message.message_id, q.message.chat_id
        return

    # Add
    if d == "add":
        ud.clear()
        ud["step"] = "message"
        msg = await q.message.reply_text(f"{hdr('New Reminder')}\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
        store_prompt(ud, msg)
        return

    # Cancel private
    if d == "cancel":
        ud.clear()
        await safe_edit(q.message, f"{hdr('Cancelled')}", home_kb())
        return

    # Cancel group
    if d == "gcancel":
        ud.clear()
        try: await q.message.delete()
        except: pass
        return

    # Group close → minimize
    if d == "gclose":
        mid = q.message.message_id
        gmin = ctx.bot_data.get(f"gmin_{mid}")
        if gmin:
            await safe_edit(q.message, gmin["min_text"], IKM([[IKB("📋 Show", callback_data=gmin["show_cb"])]]))
        else:
            await safe_edit(q.message, "—", IKM([[IKB("📋 Show", callback_data="home")]]))
        return

    # Private close → minimize
    if d.startswith("pclose_"):
        kind = d.replace("pclose_", "")
        mid = q.message.message_id
        ft = ctx.bot_data.get(f"pmin_{mid}")
        fkb = ctx.bot_data.get(f"pminkb_{mid}")
        if kind == "list":
            await safe_edit(q.message, "📋 Reminders", IKM([[IKB("📋 Show", callback_data=f"pshow_list_{mid}")]]))
            if not ft: ctx.bot_data[f"pmin_{mid}"] = q.message.text
            if not fkb: ctx.bot_data[f"pminkb_{mid}"] = q.message.reply_markup
        elif kind == "info":
            await safe_edit(q.message, "ℹ️ Info", IKM([[IKB("📋 Show", callback_data=f"pshow_info_{mid}")]]))
            if not ft: ctx.bot_data[f"pmin_{mid}"] = q.message.text
            if not fkb: ctx.bot_data[f"pminkb_{mid}"] = q.message.reply_markup
        elif kind == "month":
            await safe_edit(q.message, "📅 Schedule", IKM([[IKB("📋 Show", callback_data=f"pshow_month_{mid}")]]))
            if not ft: ctx.bot_data[f"pmin_{mid}"] = q.message.text
            if not fkb: ctx.bot_data[f"pminkb_{mid}"] = q.message.reply_markup
        return

    # Private show (expand from minimized)
    if d.startswith("pshow_info_"):
        ft = ctx.bot_data.get(f"pmin_{q.message.message_id}")
        fkb = ctx.bot_data.get(f"pminkb_{q.message.message_id}")
        if ft and fkb: await safe_edit(q.message, ft, fkb)
        elif ft: await safe_edit(q.message, ft, IKM([[IKB("✕ Close", callback_data="pclose_info")], [IKB("＋ New", callback_data="add")]]))
        return
    if d.startswith("pshow_list_"):
        await _show_list(q.message, uid, ctx, new=False)
        return
    if d.startswith("pshow_month_"):
        now = datetime.now(get_tz(uid))
        await _show_month(q.message, uid, now.year, now.month, ctx, new=False)
        return

    # Group show
    if d.startswith("gshow_start_"):
        text = f"{hdr('Smart Reminder Bot')}\n\n<b>Commands</b>\n/remind — Group reminder\n/list — Active reminders\n\n<b>Examples</b>\n<code>/remind Buy milk at 5pm</code>\n<code>/remind Meeting tomorrow 10am daily</code>\n\nTag: <code>/remind @user task at 5pm</code>"
        await safe_edit(q.message, text, IKM([[IKB("✕ Close", callback_data="gclose")]]))
        return
    if d.startswith("gshow_list_"):
        parts = d.replace("gshow_list_", "").split("_")
        if len(parts) >= 2:
            gid = int(parts[0])
            gid_s = str(gid); rows = sheet.get_all_values(); items = []
            for i, r in enumerate(rows):
                if not r or len(r) < 8: continue
                if r[7] != gid_s: continue
                if len(r) > 5 and r[5] in ("active","pending","snoozed"):
                    dd = get_detail(r); items.append((i+1, dd))
            if not items:
                await safe_edit(q.message, f"{hdr('Group Reminders')}\n\nNo active reminders.", IKM([[IKB("✕ Close", callback_data="gclose")]]))
            else:
                lines = [hdr("Group Reminders"), ""]
                for idx, (row, dd) in enumerate(items):
                    ic = ST_IC.get(dd["status"], "○")
                    ms = dd["msg"][:25] + "…" if len(dd["msg"]) > 25 else dd["msg"]
                    lines.append(f"{idx+1} {ic} {ms}")
                    lines.append(f"   {fmt_ds(dd['date'])} · {fmt_t12(dd['time'])}")
                text = "\n".join(lines)
                btns = [IKB(str(idx+1), callback_data=f"gl_{row}_{gid}") for idx, (row, dd) in enumerate(items)]
                brs = [btns[i:i+5] for i in range(0, len(btns), 5)]
                brs.append([IKB("✕ Close", callback_data="gclose")])
                await safe_edit(q.message, text, IKM(brs))
        return

    # Weekly report detail
    if d.startswith("wrdet_"):
        parts = d.replace("wrdet_", "").split("_")
        if len(parts) == 2:
            yr, wk = int(parts[0]), int(parts[1])
            await _show_report_detail(q, uid, yr, wk)
        return

    # Calendar
    if d.startswith("cal_"):
        parts = d.replace("cal_", "").split("_")
        if len(parts) == 2:
            y, m = int(parts[0]), int(parts[1])
            tz = get_tz(uid)
            step = ud.get("step", "")
            back_cb = "gcancel" if step.startswith("g_") else ("cancel" if step == "edit_date" or step == "date" else "cancel")
            back_txt = "✕ Cancel"
            await safe_edit(q.message, q.message.text, cal_kb(y, m, tz, back_cb, back_txt))
        return

    if d.startswith("day_"):
        ds = d.replace("day_", "")
        step = ud.get("step", "")
        tz = get_tz(uid)
        if step in ("date", "g_date"):
            ts = ud.get("time") or ud.get("g_time")
            if ts and is_past(ds, ts, tz):
                await safe_edit(q.message, past_msg(ts) + "\n\nPick a future date:", cal_kb(int(ds[:4]), int(ds[5:7]), tz, "gcancel" if step == "g_date" else "cancel", "✕ Cancel"))
                return
            if step == "g_date":
                ud["g_date"] = ds
                if ud.get("g_time"):
                    rep = ud.get("g_rep", "none")
                    msg = ud.get("g_msg", ""); gid = ud.get("g_chat"); tags = ud.get("g_tags", [])
                    user = q.from_user
                    await safe_edit(q.message, f"{hdr('Saving...')}", None)
                    await _finish_group(update, ctx, msg, ds, ud["g_time"], rep, gid, user, tags)
                    return
                ud["step"] = "g_time"
                await safe_edit(q.message, f"{ud.get('g_msg','')}\n━━━━━━━━━━━━━━━━━━━━\n{fmt_ds(ds)}\n\nEnter time:\ne.g. 9pm, 9:30 PM, 21:30", None)
                return
            ud["date"] = ds
            if ud.get("time"):
                rep = ud.get("repeat", "none")
                row = save_reminder(uid, ud["message"], ds, ud["time"], rep)
                text = f"{hdr('Saved ✓')}\n{detail(ud['message'], ds, ud['time'], rep)}"
                kb = saved_kb(row) if rep == "none" else saved_kb_norep()
                await safe_edit(q.message, text, kb)
                ud.clear()
                return
            ud["step"] = "time"
            await safe_edit(q.message, f"{ud.get('message','')}\n━━━━━━━━━━━━━━━━━━━━\n{fmt_ds(ds)}\n\nEnter time:\ne.g. 9pm, 9:30 PM, 21:30", None)
            return
        elif step == "edit_date":
            row = ud.get("editing_row")
            r = get_rm(row)
            if r:
                ts = norm_time(r[3])
                if is_past(ds, ts, tz):
                    await safe_edit(q.message, past_msg(ts) + "\n\nPick a future date:", cal_kb(int(ds[:4]), int(ds[5:7]), tz))
                    return
                old_ds = norm_date(r[2])
                sheet.update_cell(row, 3, ds)
                text = f"{hdr('Updated ✓')}\n{detail(r[1], ds, ts, r[4])}\n\n<i>Date: {fmt_ds(old_ds)} → <b>{fmt_ds(ds)}</b></i>"
                await safe_edit(q.message, text, home_kb())
                ud.clear()
            return
        return

    # Repeat
    if d.startswith("rep_"):
        rep = d.replace("rep_", "")
        step = ud.get("step", "")
        if step.startswith("g_") or ud.get("g_chat"):
            msg = ud.get("g_msg", ""); ds = ud.get("g_date", ""); ts = ud.get("g_time", "")
            gid = ud.get("g_chat"); tags = ud.get("g_tags", [])
            await _finish_group(update, ctx, msg, ds, ts, rep, gid, q.from_user, tags)
            return
        msg_t, ds, ts = ud.get("message", ""), ud.get("date", ""), ud.get("time", "")
        row = save_reminder(uid, msg_t, ds, ts, rep)
        text = f"{hdr('Saved ✓')}\n{detail(msg_t, ds, ts, rep)}"
        await safe_edit(q.message, text, saved_kb_norep())
        ud.clear()
        return

    # Custom repeat
    if d == "cusrep":
        ud["cus_days"] = ud.get("cus_days", [])
        await safe_edit(q.message, f"{hdr('Select Days')}\nPick days for reminder:", cus_day_kb(ud["cus_days"]))
        return
    if d.startswith("cusday_"):
        day = d.replace("cusday_", "")
        cds = ud.get("cus_days", [])
        if day in cds: cds.remove(day)
        else: cds.append(day)
        ud["cus_days"] = cds
        await safe_edit(q.message, f"{hdr('Select Days')}\nPick days:", cus_day_kb(cds))
        return
    if d == "cuswk":
        ud["cus_days"] = ["mon","tue","wed","thu","fri"]
        await safe_edit(q.message, f"{hdr('Select Days')}\nPick days:", cus_day_kb(ud["cus_days"]))
        return
    if d == "cusall":
        ud["cus_days"] = list(DAYS)
        await safe_edit(q.message, f"{hdr('Select Days')}\nPick days:", cus_day_kb(ud["cus_days"]))
        return
    if d == "cusclear":
        ud["cus_days"] = []
        await safe_edit(q.message, f"{hdr('Select Days')}\nPick days:", cus_day_kb([]))
        return
    if d == "cussave":
        cds = ud.get("cus_days", [])
        if not cds: return
        rep = "custom:" + ",".join(sorted(cds, key=DAYS.index))
        step = ud.get("step", "")
        if step.startswith("g_") or ud.get("g_chat"):
            msg = ud.get("g_msg", ""); ds = ud.get("g_date", ""); ts = ud.get("g_time", "")
            gid = ud.get("g_chat"); tags = ud.get("g_tags", [])
            await _finish_group(update, ctx, msg, ds, ts, rep, gid, q.from_user, tags)
            return
        msg_t, ds, ts = ud.get("message", ""), ud.get("date", ""), ud.get("time", "")
        row = save_reminder(uid, msg_t, ds, ts, rep)
        text = f"{hdr('Saved ✓')}\n{detail(msg_t, ds, ts, rep)}"
        await safe_edit(q.message, text, saved_kb_norep())
        ud.clear()
        return
    if d == "cusback":
        await safe_edit(q.message, f"Repeat?", repeat_kb())
        return

    # Change repeat (from saved reminder)
    if d.startswith("chrep_"):
        row = int(d.replace("chrep_", ""))
        ud["chrep_row"] = row
        r = get_rm(row)
        if r:
            text = f"{detail(r[1], norm_date(r[2]), norm_time(r[3]))}\n\nRepeat?"
            await safe_edit(q.message, text, IKM([
                [IKB("Daily", callback_data=f"chrv_{row}_daily"), IKB("Weekly", callback_data=f"chrv_{row}_weekly")],
                [IKB("Monthly", callback_data=f"chrv_{row}_monthly"), IKB("Customize", callback_data=f"chcus_{row}")],
                [IKB("« Back", callback_data="home")]
            ]))
        return
    if d.startswith("chrv_"):
        parts = d.replace("chrv_", "").split("_", 1)
        row, rep = int(parts[0]), parts[1]
        r = get_rm(row)
        if r:
            sheet.update_cell(row, 5, rep)
            text = f"{hdr('Updated ✓')}\n{detail(r[1], norm_date(r[2]), norm_time(r[3]), rep)}"
            await safe_edit(q.message, text, home_kb())
        return
    if d.startswith("chcus_"):
        row = int(d.replace("chcus_", ""))
        ud["chrep_row"] = row; ud["cus_days"] = []
        await safe_edit(q.message, f"{hdr('Select Days')}\nPick days:", cus_day_kb([], f"chrep_{row}"))
        return

    # Group repeat change
    if d.startswith("grep_"):
        tid = d.replace("grep_", "")
        rows = sheet.get_all_values()
        for i, r in enumerate(rows):
            if len(r) > 8 and r[8] == tid:
                await safe_edit(q.message, f"{detail(r[1], norm_date(r[2]), norm_time(r[3]))}\n\nRepeat?", IKM([
                    [IKB("Daily", callback_data=f"grv_{tid}_daily"), IKB("Weekly", callback_data=f"grv_{tid}_weekly")],
                    [IKB("Monthly", callback_data=f"grv_{tid}_monthly")]
                ]))
                return
        return
    if d.startswith("grv_"):
        parts = d.replace("grv_", "").rsplit("_", 1)
        tid, rep = parts[0], parts[1]
        rows = sheet.get_all_values()
        for i, r in enumerate(rows):
            if len(r) > 8 and r[8] == tid:
                sheet.update_cell(i+1, 5, rep)
                text = f"{hdr('Updated ✓')}\n{detail(r[1], norm_date(r[2]), norm_time(r[3]), rep)}"
                await safe_edit(q.message, text, IKM([[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")]]))
                return
        return

    # View reminder detail
    if d.startswith("view_"):
        row = int(d.replace("view_", ""))
        r, det = rd(row)
        if not r:
            await safe_edit(q.message, "Reminder not found.", home_kb())
            return
        ic = ST_IC.get(det["status"], "○")
        text = f"{hdr('Reminder')}\n{det['msg']}\n\n{fmt_ds(det['date'])} · {fmt_t12(det['time'])}\n{fmt_rep(det['repeat'])} · {ic} {ST_LB.get(det['status'], det['status'])}"
        btns = []
        if det["status"] in ("active","pending","snoozed"):
            btns.append([IKB("✎ Edit", callback_data=f"edit_{row}"), IKB("✕ Cancel", callback_data=f"crem_{row}")])
        else:
            btns.append([IKB("✕ Remove", callback_data=f"crem_{row}")])
        btns.append([IKB("« Back", callback_data="list_r")])
        await safe_edit(q.message, text, IKM(btns))
        return

    # Refresh list
    if d == "list_r":
        await _show_list(q.message, uid, ctx, new=False)
        return

    # Edit
    if d.startswith("edit_"):
        row = int(d.replace("edit_", ""))
        r, det = rd(row)
        if not r: return
        text = f"{hdr('Edit Reminder')}\n{detail(det['msg'], det['date'], det['time'], det['repeat'])}\n\nWhat to change?"
        await safe_edit(q.message, text, IKM([
            [IKB("Message", callback_data=f"emsg_{row}"), IKB("Date", callback_data=f"edate_{row}"), IKB("Time", callback_data=f"etime_{row}")],
            [IKB("« Back", callback_data=f"view_{row}")]
        ]))
        return
    if d.startswith("emsg_"):
        row = int(d.replace("emsg_", ""))
        ud["step"] = "edit_msg"; ud["editing_row"] = row
        r, det = rd(row)
        if r:
            text = f"{hdr('Edit Message')}\n<i>{det['msg']}</i>\n{fmt_ds(det['date'])} · {fmt_t12(det['time'])}\n\nEnter new message:"
            msg = await safe_edit(q.message, text, IKM([[IKB("« Back", callback_data=f"edit_{row}")]]))
            if msg: store_prompt(ud, msg)
        return
    if d.startswith("edate_"):
        row = int(d.replace("edate_", ""))
        ud["step"] = "edit_date"; ud["editing_row"] = row
        r, det = rd(row)
        if r:
            tz = get_tz(uid); now = datetime.now(tz)
            text = f"{hdr('Edit Date')}\n{det['msg']}\n<i>{fmt_ds(det['date'])} · {fmt_t12(det['time'])}</i>\n\nPick new date:"
            await safe_edit(q.message, text, cal_kb(now.year, now.month, tz, f"edit_{row}", "« Back"))
        return
    if d.startswith("etime_"):
        row = int(d.replace("etime_", ""))
        ud["step"] = "edit_time"; ud["editing_row"] = row
        r, det = rd(row)
        if r:
            text = f"{hdr('Edit Time')}\n{det['msg']}\n<i>{fmt_ds(det['date'])} · {fmt_t12(det['time'])}</i>\n\nEnter new time:\ne.g. 9pm, 9:30 PM, 21:30"
            msg = await safe_edit(q.message, text, IKM([[IKB("« Back", callback_data=f"edit_{row}")]]))
            if msg: store_prompt(ud, msg)
        return

    # Cancel reminder
    if d.startswith("crem_"):
        row = int(d.replace("crem_", ""))
        r = get_rm(row)
        if r:
            sheet.update_cell(row, 6, "cancelled")
            text = f"{hdr('Cancelled ✕')}\n{detail(r[1], norm_date(r[2]), norm_time(r[3]))}"
            await safe_edit(q.message, text, home_kb())
        return

    # Snooze picker
    if d.startswith("snzp_"):
        row = int(d.replace("snzp_", ""))
        r = get_rm(row)
        if not r or handled(r):
            await safe_edit(q.message, f"{hdr('Already handled')}", home_kb())
            return
        await safe_edit(q.message, f"⏰ {r[1]}\n\nSnooze for:", snz_kb(row))
        return
    if d.startswith("snzb_"):
        row = int(d.replace("snzb_", ""))
        r = get_rm(row)
        if not r or handled(r):
            await safe_edit(q.message, f"{hdr('Already handled')}", home_kb())
            return
        await safe_edit(q.message, f"⏰ {r[1]}", remind_kb(row))
        return
    if d.startswith("snz_"):
        parts = d.replace("snz_", "").split("_")
        row, mins = int(parts[0]), int(parts[1])
        r = get_rm(row)
        if not r or handled(r):
            await safe_edit(q.message, f"{hdr('Already handled')}", home_kb())
            return
        tz = get_tz(uid); now = datetime.now(tz)
        snz_time = now + timedelta(minutes=mins)
        # Cancel retries
        for j in ctx.job_queue.get_jobs_by_name(f"retry-{row}"): j.schedule_removal()
        rep = r[4] if len(r) > 4 else "none"
        if rep == "none" or not rep or rep == "none":
            sheet.update_cell(row, 3, snz_time.strftime("%Y-%m-%d"))
            sheet.update_cell(row, 4, snz_time.strftime("%H:%M"))
            sheet.update_cell(row, 6, "active")
            sheet.update_cell(row, 7, 0)
        else:
            sheet.update_cell(row, 6, "snoozed")
            ctx.job_queue.run_once(snooze_fire, mins * 60, data={"row": row, "chat": uid}, name=f"snzfire-{row}")
        text = f"{hdr('Snoozed')}\n{detail(r[1], norm_date(r[2]), norm_time(r[3]))}\n\n⏰ Snoozed → {snz_time.strftime('%-I:%M %p')}"
        await safe_edit(q.message, text, home_kb())
        return

    # Done
    if d.startswith("done_"):
        row = int(d.replace("done_", ""))
        r = get_rm(row)
        if not r or handled(r):
            await safe_edit(q.message, f"{hdr('Already handled')}", home_kb())
            return
        for j in ctx.job_queue.get_jobs_by_name(f"retry-{row}"): j.schedule_removal()
        for j in ctx.job_queue.get_jobs_by_name(f"snzfire-{row}"): j.schedule_removal()
        rep = r[4] if len(r) > 4 else "none"
        if rep != "none" and rep:
            advance_rep(row, r)
        else:
            sheet.update_cell(row, 6, "done")
        sheet.update_cell(row, 7, 0)
        # Update group status if group reminder
        if len(r) > 8 and r[8]:
            tid = r[8]
            set_tstatus(tid, uid, "done")
        text = f"{hdr('Done ✓')}\n{detail(r[1], norm_date(r[2]), norm_time(r[3]), r[4] if len(r)>4 else 'none')}"
        await safe_edit(q.message, text, home_kb())
        return

    # Group join
    if d.startswith("gjoin_"):
        tid = d.replace("gjoin_", "")
        gid = q.message.chat_id
        set_gsub(gid, uid, q.from_user.first_name, q.from_user.username or "")
        members = get_tmembers(tid)
        uid_s = str(uid)
        found = False
        for u, n, s in members:
            if u == uid_s:
                found = True
                if s == "skipped": set_tstatus(tid, uid, "waiting")
                break
        if not found: add_tmember(tid, uid, q.from_user.first_name, "waiting")
        # Refresh message
        rows = sheet.get_all_values()
        for i, r in enumerate(rows):
            if len(r) > 8 and r[8] == tid:
                members = get_tmembers(tid)
                active = [(u, n) for u, n, s in members if s != "skipped"]
                text_up = f"{hdr('Group Reminder')}\n{detail(r[1], norm_date(r[2]), norm_time(r[3]), r[4])}\n\n{len(active)} subscribed" + (f": {', '.join(n for _, n in active)}" if active else "")
                await safe_edit(q.message, text_up, IKM([[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")], [IKB("🔁 Repeat", callback_data=f"grep_{tid}")]]))
                break
        return

    # Group skip
    if d.startswith("gskip_"):
        tid = d.replace("gskip_", "")
        gid = q.message.chat_id
        set_gsub(gid, uid, q.from_user.first_name, q.from_user.username or "")
        uid_s = str(uid)
        members = get_tmembers(tid)
        found = False
        for u, n, s in members:
            if u == uid_s: found = True; set_tstatus(tid, uid, "skipped"); break
        if not found: add_tmember(tid, uid, q.from_user.first_name, "skipped")
        rows = sheet.get_all_values()
        for i, r in enumerate(rows):
            if len(r) > 8 and r[8] == tid:
                members = get_tmembers(tid)
                active = [(u, n) for u, n, s in members if s != "skipped"]
                text_up = f"{hdr('Group Reminder')}\n{detail(r[1], norm_date(r[2]), norm_time(r[3]), r[4])}\n\n{len(active)} subscribed" + (f": {', '.join(n for _, n in active)}" if active else "")
                await safe_edit(q.message, text_up, IKM([[IKB("＋ Count Me In", callback_data=f"gjoin_{tid}"), IKB("✕ Skip", callback_data=f"gskip_{tid}")], [IKB("🔁 Repeat", callback_data=f"grep_{tid}")]]))
                break
        return

    # Group list detail
    if d.startswith("gl_"):
        parts = d.replace("gl_", "").split("_")
        row, gid = int(parts[0]), parts[1]
        r, det = rd(row)
        if not r: return
        tid = r[8] if len(r) > 8 else ""
        members = get_tmembers(tid) if tid else []
        active = [(u, n, s) for u, n, s in members if s != "skipped"]
        text = f"{hdr('Reminder')}\n{det['msg']}\n\n{fmt_ds(det['date'])} · {fmt_t12(det['time'])}\n{fmt_rep(det['repeat'])} · {ST_IC.get(det['status'],'○')} {ST_LB.get(det['status'],'')}"
        if active:
            default_icon = "⏳"
            text += "\n\n" + " · ".join(f"{GT_IC.get(s, default_icon)} {n}" for _, n, s in active)
        await safe_edit(q.message, text, IKM([[IKB("« Back", callback_data=f"gshow_list_{gid}_{q.message.message_id}")]]))
        return

    # Month week
    if d.startswith("mw_"):
        parts = d.replace("mw_", "").split("_")
        y, m, w = int(parts[0]), int(parts[1]), int(parts[2])
        await _show_week(q.message, uid, y, m, w, ctx)
        return
    if d.startswith("mn_"):
        parts = d.replace("mn_", "").split("_")
        y, m = int(parts[0]), int(parts[1])
        await _show_month(q.message, uid, y, m, ctx, new=False)
        return
    if d.startswith("mb_"):
        parts = d.replace("mb_", "").split("_")
        y, m = int(parts[0]), int(parts[1])
        await _show_month(q.message, uid, y, m, ctx, new=False)
        return

    # Settings
    if d == "cfg_digest":
        cfg = get_cfg(uid)
        new_val = "false" if cfg["digest_on"] == "true" else "true"
        save_cfg(uid, "digest_on", new_val)
        await _show_settings(q.message, uid, ctx, new=False)
        return
    if d == "cfg_dtime":
        ud["step"] = "cfg_dtime"
        msg = await safe_edit(q.message, f"{hdr('Digest Time')}\nEnter new time:\ne.g. 7am, 8:30 AM", IKM([[IKB("« Back", callback_data="cfg_back")]]))
        if msg: store_prompt(ud, msg)
        return
    if d == "cfg_retries":
        opts = [1, 2, 3, 5, 7, 10]
        btns = [IKB(f"{n}×", callback_data=f"cfg_retryn_{n}") for n in opts]
        await safe_edit(q.message, f"{hdr('Max Retries')}\nPick:", IKM([btns[:3], btns[3:], [IKB("« Back", callback_data="cfg_back")]]))
        return
    if d.startswith("cfg_retryn_"):
        n = int(d.replace("cfg_retryn_", ""))
        save_cfg(uid, "max_retries", n)
        await _show_settings(q.message, uid, ctx, new=False)
        return
    if d == "cfg_gap":
        opts = [5, 10, 15, 20, 30, 60]
        btns = [IKB(f"{n}m", callback_data=f"cfg_gapn_{n}") for n in opts]
        await safe_edit(q.message, f"{hdr('Retry Gap')}\nPick:", IKM([btns[:3], btns[3:], [IKB("« Back", callback_data="cfg_back")]]))
        return
    if d.startswith("cfg_gapn_"):
        n = int(d.replace("cfg_gapn_", ""))
        save_cfg(uid, "retry_gap", n)
        await _show_settings(q.message, uid, ctx, new=False)
        return
    if d == "cfg_tz":
        btns = [[IKB(r, callback_data=f"tzr_{r}")] for r in TZ_DATA.keys()]
        btns.append([IKB("« Back", callback_data="cfg_back")])
        await safe_edit(q.message, f"{hdr('Timezone')}\nSelect region:", IKM(btns))
        return
    if d.startswith("tzr_"):
        region = d.replace("tzr_", "")
        if region in TZ_DATA:
            cfg = get_cfg(uid)
            btns = []
            for tz_key, tz_lbl in TZ_DATA[region].items():
                lbl = f"[{tz_lbl}]" if tz_key == cfg["tz"] else tz_lbl
                btns.append([IKB(lbl, callback_data=f"tzs_{tz_key}")])
            btns.append([IKB("« Regions", callback_data="cfg_tz")])
            await safe_edit(q.message, f"{hdr('Timezone')}\n{region}", IKM(btns))
        return
    if d.startswith("tzs_"):
        tz_key = d.replace("tzs_", "")
        save_cfg(uid, "tz", tz_key)
        await _show_settings(q.message, uid, ctx, new=False)
        return
    if d == "cfg_report":
        cfg = get_cfg(uid)
        new_val = "false" if cfg.get("report_on", "true") == "true" else "true"
        save_cfg(uid, "report_on", new_val)
        await _show_settings(q.message, uid, ctx, new=False)
        return
    if d == "cfg_back":
        await _show_settings(q.message, uid, ctx, new=False)
        return

# ═══════════════════ WEEKLY REPORT DETAIL ═══════════════════
async def _show_report_detail(q, uid, year, week_num):
    uid_s = str(uid); rows = sheet.get_all_values()
    # Calculate week date range from ISO week
    jan4 = date(year, 1, 4)
    start_of_week1 = jan4 - timedelta(days=jan4.weekday())
    week_start = start_of_week1 + timedelta(weeks=week_num - 1)
    week_end = week_start + timedelta(days=6)

    done_list, missed_list = [], []
    for r in rows:
        if not r or r[0] != uid_s or len(r) < 6: continue
        ds = norm_date(r[2])
        if not ds: continue
        try: rd_ = datetime.strptime(ds, "%Y-%m-%d").date()
        except: continue
        if not (week_start <= rd_ <= week_end): continue
        d = get_detail(r)
        if d["status"] == "done":
            done_list.append(d)
        elif d["status"] == "missed":
            missed_list.append(d)

    lines = [hdr(f"📊 Week Details"), f"{week_start.strftime('%-d %b')} — {week_end.strftime('%-d %b')}", ""]
    if done_list:
        lines.append("✅ <b>Completed:</b>")
        for d in done_list:
            lines.append(f"  • {d['msg']} · {fmt_ds(d['date'])}")
        lines.append("")
    if missed_list:
        lines.append("✗ <b>Missed:</b>")
        for d in missed_list:
            lines.append(f"  • {d['msg']} · {fmt_ds(d['date'])}")
        lines.append("")
    if not done_list and not missed_list:
        lines.append("No data for this week.")

    text = "\n".join(lines)
    await safe_edit(q.message, text, IKM([[IKB("« Back", callback_data="home")]]))

# ═══════════════════ TEXT HANDLER ═══════════════════
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user = update.effective_user; uid = user.id; ud = ctx.user_data
    text = update.message.text.strip()
    update_username(user)

    step = ud.get("step", "")
    is_group = update.effective_chat.type != "private"

    # Group text input
    if is_group and step.startswith("g_"):
        g_chat = ud.get("g_chat")
        if g_chat != update.effective_chat.id: return
        tz = get_tz(uid)
        if step == "g_message":
            # Try NL on message
            result = parse_nl(text, tz)
            if result and result.get("msg"):
                msg = result["msg"]; ds = result.get("date"); ts = result.get("time"); rep = result.get("repeat") or "none"
                if ds and ts:
                    await _finish_group(update, ctx, msg, ds, ts, rep, g_chat, user, ud.get("g_tags", []))
                    return
                ud["g_msg"] = msg
                if ts: ud["g_time"] = ts
                if ds: ud["g_date"] = ds
                if rep and rep != "none": ud["g_rep"] = rep
                if not ds:
                    now = datetime.now(tz)
                    ud["step"] = "g_date"
                    await update.message.reply_text(f"{msg}\n━━━━━━━━━━━━━━━━━━━━\n{fmt_t12(ts) if ts else ''}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz, "gcancel", "✕ Cancel"), parse_mode="HTML")
                    return
                ud["step"] = "g_time"
                await update.message.reply_text(f"{msg}\n━━━━━━━━━━━━━━━━━━━━\n{fmt_ds(ds)}\n\nEnter time:", reply_markup=ForceReply(selective=True), parse_mode="HTML")
                return
            ud["g_msg"] = text; ud["step"] = "g_date"
            now = datetime.now(tz)
            await update.message.reply_text(f"{text}\n━━━━━━━━━━━━━━━━━━━━\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz, "gcancel", "✕ Cancel"), parse_mode="HTML")
            return
        if step == "g_time":
            ts = parse_time(text)
            if not ts:
                await update.message.reply_text("⚠ Invalid time. Try: 9pm, 9:30 PM, 21:30", reply_markup=ForceReply(selective=True))
                return
            ds = ud.get("g_date", "")
            if ds and is_past(ds, ts, tz):
                await update.message.reply_text(past_msg(ts), reply_markup=ForceReply(selective=True))
                return
            rep = ud.get("g_rep", "none")
            msg = ud.get("g_msg", "")
            await _finish_group(update, ctx, msg, ds, ts, rep, g_chat, user, ud.get("g_tags", []))
            return
        return

    if is_group: return

    # Private steps
    if step == "message":
        await delete_prompt(ud, ctx.bot)
        tz = get_tz(uid)
        result = parse_nl(text, tz)
        if result and result.get("msg"):
            msg = result["msg"]; ds = result.get("date"); ts = result.get("time"); rep = result.get("repeat") or "none"
            if ds and ts:
                row = save_reminder(uid, msg, ds, ts, rep)
                t = f"{hdr('Saved ✓')}\n{detail(msg, ds, ts, rep)}"
                kb = saved_kb(row) if rep == "none" else saved_kb_norep()
                await update.message.reply_text(t, reply_markup=kb, parse_mode="HTML")
                ud.clear(); return
            ud["message"] = msg
            if ts: ud["time"] = ts
            if ds: ud["date"] = ds
            if rep and rep != "none": ud["repeat"] = rep
            if not ds:
                now = datetime.now(tz)
                ud["step"] = "date"
                prompt_text = f"{msg}\n━━━━━━━━━━━━━━━━━━━━"
                if ts: prompt_text += f"\n{fmt_t12(ts)}"
                prompt_text += "\n\nPick a date:"
                msg2 = await update.message.reply_text(prompt_text, reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML")
                store_prompt(ud, msg2)
                return
            if not ts:
                ud["step"] = "time"
                msg2 = await update.message.reply_text(f"{msg}\n━━━━━━━━━━━━━━━━━━━━\n{fmt_ds(ds)}\n\nEnter time:\ne.g. 9pm, 9:30 PM, 21:30", parse_mode="HTML")
                store_prompt(ud, msg2)
                return
        ud["message"] = text; ud["step"] = "date"
        now = datetime.now(tz)
        msg2 = await update.message.reply_text(f"{text}\n━━━━━━━━━━━━━━━━━━━━\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML")
        store_prompt(ud, msg2)
        return

    if step == "time":
        await delete_prompt(ud, ctx.bot)
        tz = get_tz(uid)
        ts = parse_time(text)
        if not ts:
            msg2 = await update.message.reply_text("⚠ Invalid time. Try: 9pm, 9:30 PM, 21:30")
            store_prompt(ud, msg2)
            return
        ds = ud.get("date", "")
        if ds and is_past(ds, ts, tz):
            msg2 = await update.message.reply_text(past_msg(ts))
            store_prompt(ud, msg2)
            return
        ud["time"] = ts
        rep = ud.get("repeat", "none")
        row = save_reminder(uid, ud["message"], ds, ts, rep)
        t = f"{hdr('Saved ✓')}\n{detail(ud['message'], ds, ts, rep)}"
        kb = saved_kb(row) if rep == "none" else saved_kb_norep()
        await update.message.reply_text(t, reply_markup=kb, parse_mode="HTML")
        ud.clear()
        return

    if step == "edit_msg":
        await delete_prompt(ud, ctx.bot)
        row = ud.get("editing_row")
        r = get_rm(row)
        if r:
            old_msg = r[1]
            sheet.update_cell(row, 2, text)
            t = f"{hdr('Updated ✓')}\n{detail(text, norm_date(r[2]), norm_time(r[3]), r[4])}\n\n<i>Message: {old_msg} → <b>{text}</b></i>"
            await update.message.reply_text(t, reply_markup=home_kb(), parse_mode="HTML")
        ud.clear()
        return

    if step == "edit_time":
        await delete_prompt(ud, ctx.bot)
        tz = get_tz(uid)
        ts = parse_time(text)
        if not ts:
            msg2 = await update.message.reply_text("⚠ Invalid time. Try: 9pm, 9:30 PM, 21:30")
            store_prompt(ud, msg2)
            return
        row = ud.get("editing_row")
        r = get_rm(row)
        if r:
            ds = norm_date(r[2])
            if is_past(ds, ts, tz):
                msg2 = await update.message.reply_text(past_msg(ts))
                store_prompt(ud, msg2)
                return
            old_ts = norm_time(r[3])
            sheet.update_cell(row, 4, ts)
            t = f"{hdr('Updated ✓')}\n{detail(r[1], ds, ts, r[4])}\n\n<i>Time: {fmt_t12(old_ts)} → <b>{fmt_t12(ts)}</b></i>"
            await update.message.reply_text(t, reply_markup=home_kb(), parse_mode="HTML")
        ud.clear()
        return

    if step == "cfg_dtime":
        await delete_prompt(ud, ctx.bot)
        ts = parse_time(text)
        if not ts:
            msg2 = await update.message.reply_text("⚠ Invalid time. Try: 7am, 8:30 AM")
            store_prompt(ud, msg2)
            return
        save_cfg(uid, "digest_time", ts)
        await update.message.reply_text(f"✅ Digest time set to {fmt_t12(ts)}", reply_markup=home_kb())
        ud.clear()
        return

    # No active step → try NL
    if not step:
        tz = get_tz(uid)
        result = parse_nl(text, tz)
        if not result: return
        msg = result["msg"]; ds = result.get("date"); ts = result.get("time"); rep = result.get("repeat") or "none"
        if ds and ts:
            row = save_reminder(uid, msg, ds, ts, rep)
            t = f"{hdr('Saved ✓')}\n{detail(msg, ds, ts, rep)}"
            kb = saved_kb(row) if rep == "none" else saved_kb_norep()
            await update.message.reply_text(t, reply_markup=kb, parse_mode="HTML")
            return
        ud["message"] = msg
        if ts: ud["time"] = ts
        if ds: ud["date"] = ds
        if rep and rep != "none": ud["repeat"] = rep
        if not ds and ts:
            now = datetime.now(tz)
            ud["step"] = "date"
            msg2 = await update.message.reply_text(f"{msg}\n━━━━━━━━━━━━━━━━━━━━\n{fmt_t12(ts)}\n\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML")
            store_prompt(ud, msg2)
            return
        if ds and not ts:
            ud["step"] = "time"
            msg2 = await update.message.reply_text(f"{msg}\n━━━━━━━━━━━━━━━━━━━━\n{fmt_ds(ds)}\n\nEnter time:\ne.g. 9pm, 9:30 PM, 21:30", parse_mode="HTML")
            store_prompt(ud, msg2)
            return
        if not ds and not ts:
            now = datetime.now(tz)
            ud["step"] = "date"
            msg2 = await update.message.reply_text(f"{msg}\n━━━━━━━━━━━━━━━━━━━━\nPick a date:", reply_markup=cal_kb(now.year, now.month, tz), parse_mode="HTML")
            store_prompt(ud, msg2)
            return

# ═══════════════════ SCHEDULERS ═══════════════════
async def snooze_fire(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data; row = d["row"]; chat = d["chat"]
    r = get_rm(row)
    if not r or r[5] != "snoozed": return
    sheet.update_cell(row, 6, "pending"); sheet.update_cell(row, 7, 0)
    text = f"⏰ {r[1]}"
    try:
        msg = await ctx.bot.send_message(chat_id=chat, text=text, reply_markup=remind_kb(row))
        ctx.bot_data[f"rmsg_{row}"] = (msg.message_id, chat)
    except: pass
    cfg = get_cfg(chat)
    ctx.job_queue.run_once(auto_retry, cfg["retry_gap"] * 60, data={"row": row, "chat": chat, "count": 0}, name=f"retry-{row}")

async def auto_retry(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data; row = d["row"]; chat = d["chat"]; count = d.get("count", 0)
    r = get_rm(row)
    if not r: return
    if r[5] not in ("pending",): return
    cfg = get_cfg(chat)
    max_ret = cfg["max_retries"]
    if count >= max_ret:
        sheet.update_cell(row, 6, "missed")
        if len(r) > 8 and r[8]: set_tstatus(r[8], chat, "missed")
        return
    # Remove old buttons
    old = ctx.bot_data.get(f"rmsg_{row}")
    if old:
        try: await ctx.bot.edit_message_reply_markup(old[1], old[0], reply_markup=None)
        except: pass
    text = f"🔔 {r[1]} ({count+1}/{max_ret})"
    try:
        msg = await ctx.bot.send_message(chat_id=chat, text=text, reply_markup=remind_kb(row))
        ctx.bot_data[f"rmsg_{row}"] = (msg.message_id, chat)
    except: pass
    sheet.update_cell(row, 7, count + 1)
    ctx.job_queue.run_once(auto_retry, cfg["retry_gap"] * 60, data={"row": row, "chat": chat, "count": count + 1}, name=f"retry-{row}")

async def check_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        rows = sheet.get_all_values()
    except:
        try: client.login(); rows = sheet.get_all_values()
        except: return
    cfg_rows = cfg_sheet.get_all_values()
    tz_map = {}
    cfg_map = {}
    for r in cfg_rows:
        if r and r[0]:
            try: tz_map[r[0]] = pytz.timezone(r[5] if len(r) > 5 and r[5] else DEF_TZ)
            except: tz_map[r[0]] = pytz.timezone(DEF_TZ)
            cfg_map[r[0]] = {"max_retries": int(r[3]) if len(r) > 3 and r[3] else DEF_RETRIES, "retry_gap": int(r[4]) if len(r) > 4 and r[4] else DEF_RETRY_GAP}
    for i, r in enumerate(rows):
        if i == 0: continue
        if not r or len(r) < 6: continue
        if r[5] != "active": continue
        uid_s = r[0]
        tz = tz_map.get(uid_s, pytz.timezone(DEF_TZ))
        now = datetime.now(tz)
        now_str = now.strftime("%Y-%m-%d %H:%M")
        ds = norm_date(r[2]); ts = norm_time(r[3])
        rem_str = f"{ds} {ts}"
        if rem_str != now_str: continue
        row = i + 1
        # Check custom days
        rep = r[4] if len(r) > 4 else "none"
        if rep.startswith("custom:"):
            cdays = rep.replace("custom:", "").split(",")
            if DAYS[now.weekday()] not in cdays: continue
        uid = int(uid_s) if uid_s.isdigit() else uid_s
        # Cancel prior retries
        for j in ctx.job_queue.get_jobs_by_name(f"retry-{row}"): j.schedule_removal()
        # Check if group reminder
        gid = r[7] if len(r) > 7 else ""
        tid = r[8] if len(r) > 8 else ""
        if gid and tid:
            # Group reminder
            sheet.update_cell(row, 6, "pending"); sheet.update_cell(row, 7, 0)
            # Update setup message buttons
            members = get_tmembers(tid)
            active = [(u, n, s) for u, n, s in members if s not in ("skipped",)]
            for u, n, s in active:
                if s == "waiting": set_tstatus(tid, u, "pending")
            # Send status in group
            status_text = gstatus_text(tid, r[1])
            try:
                gmsg = await ctx.bot.send_message(chat_id=int(gid), text=status_text, parse_mode="HTML")
                ctx.bot_data[f"gmsg_{tid}"] = (gmsg.message_id, int(gid))
            except: pass
            # DM each active member
            for u, n, s in active:
                if s in ("skipped",): continue
                try:
                    dm = await ctx.bot.send_message(chat_id=int(u), text=f"⏰ {r[1]}\nFrom group", reply_markup=remind_kb(row))
                    ctx.bot_data[f"rmsg_{row}_{u}"] = (dm.message_id, int(u))
                except: pass
            cfg = cfg_map.get(uid_s, {"retry_gap": DEF_RETRY_GAP})
            ctx.job_queue.run_once(group_retry, cfg["retry_gap"] * 60, data={"row": row, "tid": tid, "gid": gid, "count": 0}, name=f"retry-{row}")
            if rep == "none": pass
            else: advance_rep(row, r)
        else:
            # Personal reminder
            sheet.update_cell(row, 6, "pending"); sheet.update_cell(row, 7, 0)
            text = f"⏰ {r[1]}"
            try:
                msg = await ctx.bot.send_message(chat_id=uid, text=text, reply_markup=remind_kb(row))
                ctx.bot_data[f"rmsg_{row}"] = (msg.message_id, uid)
            except: pass
            cfg = cfg_map.get(uid_s, {"retry_gap": DEF_RETRY_GAP})
            ctx.job_queue.run_once(auto_retry, cfg["retry_gap"] * 60, data={"row": row, "chat": uid, "count": 0}, name=f"retry-{row}")

async def group_retry(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data; row = d["row"]; tid = d["tid"]; gid = d["gid"]; count = d.get("count", 0)
    r = get_rm(row)
    if not r: return
    members = get_tmembers(tid)
    pending = [(u, n) for u, n, s in members if s == "pending"]
    if not pending: return
    uid_s = r[0]
    cfg = get_cfg(int(uid_s) if uid_s.isdigit() else 0)
    max_ret = cfg["max_retries"]
    if count >= max_ret:
        for u, n in pending: set_tstatus(tid, u, "missed")
        # Update group message
        gmsg_data = ctx.bot_data.get(f"gmsg_{tid}")
        if gmsg_data:
            try: await ctx.bot.edit_message_text(gstatus_text(tid, r[1]), chat_id=gmsg_data[1], message_id=gmsg_data[0], parse_mode="HTML")
            except: pass
        return
    for u, n in pending:
        old = ctx.bot_data.get(f"rmsg_{row}_{u}")
        if old:
            try: await ctx.bot.edit_message_reply_markup(old[1], old[0], reply_markup=None)
            except: pass
        try:
            dm = await ctx.bot.send_message(chat_id=int(u), text=f"🔔 {r[1]} ({count+1}/{max_ret})", reply_markup=remind_kb(row))
            ctx.bot_data[f"rmsg_{row}_{u}"] = (dm.message_id, int(u))
        except: pass
    ctx.job_queue.run_once(group_retry, cfg["retry_gap"] * 60, data={"row": row, "tid": tid, "gid": gid, "count": count + 1}, name=f"retry-{row}")

async def check_digest(ctx: ContextTypes.DEFAULT_TYPE):
    now_utc = datetime.now(pytz.utc)
    rows = cfg_sheet.get_all_values()
    for r in rows[1:]:
        if not r or len(r) < 3: continue
        if r[1] != "true": continue
        uid_s = r[0]
        try: tz = pytz.timezone(r[5] if len(r) > 5 and r[5] else DEF_TZ)
        except: tz = pytz.timezone(DEF_TZ)
        now = now_utc.astimezone(tz)
        digest_time = r[2] if r[2] else DEF_DIGEST_TIME
        if now.strftime("%H:%M") != digest_time: continue
        today_str = now.strftime("%Y-%m-%d")
        rem_rows = sheet.get_all_values()
        items = []
        for rr in rem_rows:
            if not rr or rr[0] != uid_s or len(rr) < 6: continue
            ds = norm_date(rr[2])
            if ds != today_str: continue
            if rr[5] not in ("active", "snoozed"): continue
            d = get_detail(rr)
            rep = d["repeat"]
            if rep.startswith("custom:"):
                cdays = rep.replace("custom:", "").split(",")
                if DAYS[now.weekday()] not in cdays: continue
            items.append(d)
        if not items: continue
        items.sort(key=lambda x: x["time"])
        lines = [f"☀️ Good morning!\n{hdr(f'Today — {now.strftime(\"%-d %b\")}')}", ""]
        for d in items:
            lines.append(f"  {fmt_t12(d['time'])} · {d['msg']}")
        lines.append(f"\n{len(items)} reminder{'s' if len(items) != 1 else ''} today")
        text = "\n".join(lines)
        try: await ctx.bot.send_message(chat_id=int(uid_s), text=text, reply_markup=home_kb(), parse_mode="HTML")
        except: pass

# ═══════════════════ MAIN ═══════════════════
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
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    for cmd, fn in [("start", start), ("add", add_cmd), ("list", list_cmd), ("month", month_cmd), ("settings", settings_cmd), ("info", info_cmd), ("remind", remind_cmd)]:
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
