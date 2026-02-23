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
RETRY_INTERVAL = 600
MAX_RETRIES = 3
DIV = "━━━━━━━━━━━━━━━━━━━━"

# Sheet: user_id | message | date | time | repeat | status | retry_count

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
sheet = client.open_by_url(SHEET_URL).sheet1


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
    return {"active": "○", "pending": "●", "missed": "✗"}.get(str(s), "?")


def s_label(s):
    return {"active": "Active", "pending": "Pending", "missed": "Missed"}.get(str(s), str(s))


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


def _find_date(text):
    now = datetime.now(IST)
    low = text.lower()

    for pat, delta in [
        (r'\bday\s+after\s+tomorrow\b', 2), (r'\b(today|tonight)\b', 0),
        (r'\b(tomorrow|tmrw|tmr)\b', 1), (r'\bnext\s+week\b', 7),
    ]:
        m = re.search(pat, low)
        if m:
            return (now + timedelta(days=delta)).strftime("%Y-%m-%d"), m.start(), m.end()

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
            except ValueError:
                pass

    months = ['january','february','march','april','may','june',
              'july','august','september','october','november','december']
    mabbr = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec']
    for mi, (mf, ma) in enumerate(zip(months, mabbr), 1):
        for pt in [rf'\b(?:on\s+)?({mf}|{ma})\s+(\d{{1,2}})\b',
                    rf'\b(?:on\s+)?(\d{{1,2}})\s+({mf}|{ma})\b']:
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


def _clean(text, t_span, d_span):
    spans = sorted([s for s in [t_span, d_span] if s], key=lambda x: x[0], reverse=True)
    r = text
    for s, e in spans:
        r = r[:s] + r[e:]
    for f in [r'^\s*remind\s+me\s+to\s+', r'^\s*reminder\s+to\s+', r'^\s*reminder\s+', r'^\s*remind\s+to\s+',
              r'^\s*remind\s+me\s+', r'^\s*remember\s+to\s+', r"^\s*don'?t\s+forget\s+to\s+",
              r'^\s*set\s+(?:a\s+)?reminder\s+(?:to\s+|for\s+)?']:
        r = re.sub(f, '', r, flags=re.I)
    r = re.sub(r'\s+', ' ', r).strip().strip('.,;:!? ')
    r = re.sub(r'^\s*on\s+', '', r, flags=re.I).strip()
    r = re.sub(r'\s+on\s*$', '', r, flags=re.I).strip()
    return r[0].upper() + r[1:] if r else r


def parse_nl(text):
    tr, dr = _find_time(text), _find_date(text)
    ts = tr[0] if tr else None
    ds = dr[0] if dr else None
    t_sp = (tr[1], tr[2]) if tr else None
    d_sp = (dr[1], dr[2]) if dr else None
    msg = _clean(text, t_sp, d_sp)
    if not msg or (not ds and not ts):
        return None
    return {'message': msg, 'date': ds, 'time': ts}


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
    ud["p_mid"], ud["p_cid"] = msg.message_id, msg.chat.id


async def rm_btns(ctx, row):
    prev = ctx.bot_data.pop(f"r_{row}", None)
    if prev:
        try:
            await ctx.bot.edit_message_reply_markup(chat_id=prev["c"], message_id=prev["m"], reply_markup=None)
        except Exception:
            pass


def save_rm(ctx, row, cid, mid):
    ctx.bot_data[f"r_{row}"] = {"c": cid, "m": mid}


# ============= POST INIT =================

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("add", "New reminder"),
        BotCommand("list", "All reminders"),
        BotCommand("info", "About this bot")])


# ============= COMMANDS ===================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(home_text(), reply_markup=home_kb(), parse_mode="HTML")


async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["step"] = "message"
    sent = await update.message.reply_text(
        f"{hdr('New Reminder')}\nEnter message:", reply_markup=cancel_kb(), parse_mode="HTML")
    save_p(ctx.user_data, sent)


async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await show_list(update.message, update.effective_user.id, new=True)


async def info_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"{hdr('Smart Reminder Bot')}\n\n"
        "Set reminders and get notified on time.\n\n"
        "<b>Features</b>\n• One-time & recurring reminders\n• Calendar date picker\n"
        "• Flexible time input\n• Snooze (15m to 12h)\n"
        "• Auto-retry 3× every 10 min if missed\n• Edit or cancel anytime\n\n"
        "<b>Smart Input</b>\nJust type naturally:\n"
        "<code>Buy milk tomorrow at 5pm</code>\n"
        "<code>Call mom today at 3:30pm</code>\n"
        "<code>Meeting on Monday at 10am</code>\n\n"
        "<b>Commands</b>\n/add — New reminder\n/list — All reminders\n/info — This page\n\n"
        "<b>Time Formats</b>\n"
        "<code>9pm</code>  <code>9:30 PM</code>  <code>21:30</code>  <code>7:05pm</code>",
        parse_mode="HTML")


# ============= SHOW LIST =================

async def show_list(target, uid, new=False):
    rows = sheet.get_all_records()
    items = [(i, r) for i, r in enumerate(rows, 2)
             if str(r.get("user_id", "")) == str(uid)
             and str(r.get("status", "")).strip() in ("active", "pending", "missed")]

    if not items:
        t = f"{hdr('Reminders')}\nNo reminders found."
        if new:
            await target.reply_text(t, reply_markup=home_kb(), parse_mode="HTML")
        else:
            await safe_edit(target, t, home_kb())
        return

    lines = [hdr("Reminders")]
    btns = []
    for ri, r in items:
        st = str(r.get("status", ""))
        msg = str(r.get("message", ""))
        short = msg[:40] + "…" if len(msg) > 40 else msg
        bl = msg[:12] if len(msg) > 12 else msg
        lines.append(
            f"\n{s_icon(st)} {short}\n   {fmt_date(norm_date(r.get('date', '')))} · "
            f"{fmt_time(norm_time(r.get('time', '')))} · "
            f"{fmt_rep(r.get('repeat', 'none'))} · <i>{s_label(st)}</i>")
        if st == "missed":
            btns.append([InlineKeyboardButton(f"✕ {bl}", callback_data=f"crem_{ri}")])
        else:
            btns.append([InlineKeyboardButton(f"✎ {bl}", callback_data=f"edit_{ri}"),
                         InlineKeyboardButton(f"✕ {bl}", callback_data=f"crem_{ri}")])
    btns.append([InlineKeyboardButton("« Back", callback_data="home")])
    t = "\n".join(lines)
    if new:
        await target.reply_text(t, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
    else:
        await safe_edit(target, t, InlineKeyboardMarkup(btns))


# ============= BUTTON HANDLER ============

async def on_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data, ud = q.data, ctx.user_data

    if data == "noop":
        return

    if data in ("home", "cancel"):
        ud.clear()
        await safe_edit(q.message, home_text(), home_kb())

    elif data == "add":
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
                ud["step"] = "repeat"
                await safe_edit(q.message,
                    f"{hdr('New Reminder')}\n{detail(msg, date_str, ts)}\n\nRepeat?", repeat_kb())
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
        sheet.append_row([q.from_user.id, msg, date, time, rep, "active", 0], value_input_option="RAW")
        ud.clear()
        await safe_edit(q.message,
            f"{hdr('Saved ✓')}\n{detail(msg, date, time, fmt_rep(rep))}", home_kb())

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
                [InlineKeyboardButton("« Back", callback_data="list_refresh")]]))

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

    elif data == "list_refresh":
        ud.clear()
        await show_list(q.message, q.from_user.id)


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
    msg, date, time = result['message'], result['date'], result['time']
    if not msg:
        return
    ud = ctx.user_data
    ud.clear()
    ud["message"] = msg

    if date and time:
        if is_past(date, time):
            ud["time"], ud["step"] = time, "date"
            now = datetime.now(IST)
            sent = await update.message.reply_text(
                f"{hdr('New Reminder')}\n{msg}\n\n{past_msg(time)}\nPick a future date:",
                reply_markup=cal_kb(now.year, now.month), parse_mode="HTML")
            save_p(ud, sent)
        else:
            ud["date"], ud["time"] = date, time
            sent = await update.message.reply_text(
                f"{hdr('New Reminder')}\n{detail(msg, date, time)}\n\nRepeat?",
                reply_markup=repeat_kb(), parse_mode="HTML")
            save_p(ud, sent)

    elif time and not date:
        ud["time"], ud["step"] = time, "date"
        now = datetime.now(IST)
        sent = await update.message.reply_text(
            f"{hdr('New Reminder')}\n{msg}\n{fmt_time(time)}\n\nPick a date:",
            reply_markup=cal_kb(now.year, now.month), parse_mode="HTML")
        save_p(ud, sent)

    elif date and not time:
        ud["date"], ud["step"] = date, "time"
        sent = await update.message.reply_text(
            f"{hdr('New Reminder')}\n{msg}\n{fmt_date(date)}\n\nEnter time:\n<i>e.g. 9pm, 9:30 PM, 21:30</i>",
            reply_markup=cancel_kb(), parse_mode="HTML")
        save_p(ud, sent)


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
        ud["time"], ud["step"] = parsed, "repeat"
        msg = ud.get("message", "")
        sent = await update.message.reply_text(
            f"{hdr('New Reminder')}\n{detail(msg, ds, parsed)}\n\nRepeat?",
            reply_markup=repeat_kb(), parse_mode="HTML")
        save_p(ud, sent)

    elif step == "edit_message":
        row = ud.get("editing_row")
        if not row: return
        await rm_prompt(ctx, ud)
        r = sheet.row_values(row)
        old, ds, ts, rs = get_detail(r)
        sheet.update_cell(row, 2, text)
        ud.clear()
        await update.message.reply_text(
            f"{hdr('Updated ✓')}\nMessage: {old} → <b>{text}</b>\n"
            f"{fmt_date(ds)} · {fmt_time(ts)} · {rs}",
            reply_markup=home_kb(), parse_mode="HTML")

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
        await update.message.reply_text(
            f"{hdr('Updated ✓')}\n{msg}\nDate: {fmt_date(ds)}\n"
            f"Time: {fmt_time(old_t)} → <b>{fmt_time(parsed)}</b> · {rs}",
            reply_markup=home_kb(), parse_mode="HTML")


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
    try:
        count = int(r[6])
    except (IndexError, ValueError):
        count = 0
    if count >= MAX_RETRIES:
        if not advance_rep(row, r):
            sheet.update_cell(row, 6, "missed")
            sheet.update_cell(row, 7, 0)
        return

    await rm_btns(ctx, row)
    nc = count + 1
    sent = await ctx.bot.send_message(chat_id=chat,
        text=f"{str(r[1]).strip()}\n\n<b>⏰Reminder</b> ({nc}/{MAX_RETRIES})",
        reply_markup=act_kb(row), parse_mode="HTML")
    save_rm(ctx, row, chat, sent.message_id)
    sheet.update_cell(row, 7, nc)

    if nc >= MAX_RETRIES:
        if not advance_rep(row, r):
            sheet.update_cell(row, 6, "missed")
            sheet.update_cell(row, 7, 0)
    else:
        ctx.job_queue.run_once(auto_retry, RETRY_INTERVAL,
            data={"row": row, "chat": chat}, name=f"retry-{row}")


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
        ctx.job_queue.run_once(auto_retry, RETRY_INTERVAL,
            data={"row": idx, "chat": uid}, name=f"retry-{idx}")


# ============= MAIN ======================

def main():
    app = Application.builder().token(TOKEN).job_queue(JobQueue()).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(check_reminders, interval=60, first=0)
    print("Smart Reminder Bot Running")
    app.run_polling()


if __name__ == "__main__":
    main()


