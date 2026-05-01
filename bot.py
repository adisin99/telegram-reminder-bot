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
TOKEN = os.environ.get("BOT_TOKEN")
SHEET_URL = os.environ.get("SHEET_URL")
creds_json = os.environ.get("GOOGLE_CREDS")

DIV = "━━━━━━━━━━━━━━━━━━━━"
AUTO_MIN_SEC = 180  # 3 minutes

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
    'ok', 'okay', 'k', 'kk', 'yes', 'yeah', 'yep', 'yup', 'y', 'no', 'nah', 'nope', 'n',
    'bye', 'goodbye', 'cya', 'see you', 'good morning', 'good night', 'gm', 'gn',
    'lol', 'haha', 'hehe', 'what', 'why', 'how', 'when', 'where', 'help', '?'
}

TZ_REGIONS = list(dict.fromkeys(t[3] for t in TZ_DATA))
TZ_ICONS = {"Asia": "🌏", "Europe": "🌍", "Americas": "🌎", "Oceania": "🌏", "Africa": "🌍"}

# =============== LOGGING =================
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ============= GOOGLE SHEET ==============
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
if not TOKEN or not SHEET_URL or not creds_json:
    raise Exception("Missing env vars")
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

# ============= FORMATTERS & HELPERS ================
def hdr(t): return f"<b>{t}</b>\n{DIV}"
ST_IC = {"active": "○", "pending": "●", "missed": "✗", "snoozed": "◷", "done": "✅", "cancelled": "✕"}

# (Keep all your existing formatters, normalizers, get_cfg, save_cfg, etc. — unchanged)
# ... [all your existing helper functions remain exactly the same] ...

# ============= HOME SCREEN — NEW CLEAN VERSION ================
HOME_TEXT = (
    f"{hdr('RemindX')}\n"
    "Just type your reminder:\n\n"
    "<i>Buy milk tomorrow at 5pm</i>\n"
    "<i>Gym at 6pm daily</i>\n"
    "<i>Meeting Monday 10am weekly</i>\n"
    "<i>Call mom in 30 min</i>\n"
)

def home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Reminder", callback_data="add")],
        [InlineKeyboardButton("📅 Schedule", callback_data="schedule_view")],
        [InlineKeyboardButton("📊 Insights", callback_data="insights")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="cfg_settings")],
    ])

# ============= NEW UNIFIED SCHEDULE VIEW ================
def build_schedule_month(uid, year, month, utz):
    txt, kb = build_month_view(uid, year, month, utz, ctx=None)
    # Insert tab row just above navigation
    tab_row = [
        InlineKeyboardButton(" Today ", callback_data="sched_today"),
        InlineKeyboardButton(" Upcoming ", callback_data="sched_upcoming"),
    ]
    kb.inline_keyboard.insert(-1, tab_row)  # before the nav row
    return txt, kb

async def show_today_digest(target, uid, ctx, from_insights=False):
    utz = get_tz(uid)
    now = datetime.now(utz)
    today = now.strftime("%Y-%m-%d")
    try:
        rows = sheet.get_all_values()
    except:
        rows = []
    todays = [r for r in rows[1:] 
              if len(r) >= 6 and str(r[0]) == str(uid) and norm_date(r[2]) == today 
              and r[5] in ("active", "pending", "snoozed")]
    todays.sort(key=lambda x: norm_time(x[3]))

    lines = [f" Today's Reminders · {now.strftime('%-d %b')}\n{DIV}\n"]
    if todays:
        for i, r in enumerate(todays, 1):
            row_idx = rows.index(r) + 1
            msg = r[1][:45] + ("…" if len(r[1]) > 45 else "")
            lines.append(f"{i}. {ST_IC.get(r[5], '○')} {fmt_time(norm_time(r[3]))} · {msg}")
        btns = []
        row = []
        for i, r in enumerate(todays):
            row_idx = rows.index(r) + 1
            row.append(InlineKeyboardButton(str(i+1), callback_data=f"view_{row_idx}"))
            if len(row) == 6:
                btns.append(row)
                row = []
        if row:
            btns.append(row)
        back_cb = "insights" if from_insights else "schedule_view"
        btns.append([InlineKeyboardButton("← Back", callback_data=back_cb)])
        kb = InlineKeyboardMarkup(btns)
    else:
        lines.append("No reminders today — enjoy your free day! 🎉")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="schedule_view" if not from_insights else "insights")]])

    await safe_edit(target, "\n".join(lines), kb)

async def show_upcoming_list(target, uid, ctx):
    try:
        rows = sheet.get_all_values()
    except:
        rows = []
    upcoming = [r for r in rows[1:] 
                if len(r) >= 6 and str(r[0]) == str(uid) and r[5] in ("active", "pending", "snoozed")]
    upcoming.sort(key=lambda x: (norm_date(x[2]), norm_time(x[3])))

    if not upcoming:
        await safe_edit(target, f"{hdr('Upcoming')}\nNo upcoming reminders.", 
                        InlineKeyboardMarkup([[InlineKeyboardButton("← Schedule", callback_data="schedule_view")]]))
        return

    lines = [hdr("Upcoming Reminders")]
    btns = []
    row = []
    for idx, r in enumerate(upcoming[:50], 1):
        row_idx = rows.index(r) + 1
        rep = fmt_rep(r[4])+ " · " if r[4] != "none" else ""
        short = r[1][:38] + ("…" if len(r[1]) > 38 else "")
        lines.append(f"\n<b>{idx}</b> {ST_IC.get(r[5], '○')} {fmt_date(norm_date(r[2]))} {fmt_time(norm_time(r[3]))}\n    {rep}{short}")
        row.append(InlineKeyboardButton(str(idx), callback_data=f"view_{row_idx}"))
        if len(row) == 5:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([InlineKeyboardButton("← Schedule", callback_data="schedule_view")])
    await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup(btns))

# ============= INSIGHTS HUB ================
async def show_insights(target, uid, ctx):
    txt = f"{hdr('Insights')}\n\n• Today's Digest\n• This Week Report\n• Monthly Report (any month)"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Today's Digest", callback_data="insight_today")],
        [InlineKeyboardButton("This Week Report", callback_data="insight_weekly")],
        [InlineKeyboardButton("Monthly Report", callback_data="insight_month_picker")],
        [InlineKeyboardButton("🔴 Close", callback_data="cfg_close")],
    ])
    await safe_edit(target, txt, kb)

async def send_weekly_report(target, uid, utz, on_demand=False):
    text = build_weekly_detail(uid, datetime.now(utz), utz)
    if not text:
        text = f"{hdr('This Week')}\nNo data yet."
    await safe_edit(target, text, home_kb())

async def send_monthly_report(target, uid, year, month, utz):
    first = datetime(year, month, 1).date()
    last_day = cal_module.monthrange(year, month)[1]
    last = datetime(year, month, last_day).date()
    reminders = get_user_reminders(uid)
    expanded = expand_recur(reminders, first, last)
    
    done = sum(1 for x in expanded if x["status"] == "done")
    missed = sum(1 for x in expanded if x["status"] == "missed")
    total = len(expanded)
    if total == 0:
        await safe_edit(target, f"{hdr('Monthly Report')}\n{cal_module.month_name[month]} {year}\n\nNo reminders.", home_kb())
        return
    
    pct = round(done/total*100) if total else 0
    mot = "Outstanding! 🏆" if pct>=90 else "Keep it up! 💪" if pct>=70 else "Room to improve 📈" if pct>=50 else "Let's do better 🎯"
    
    txt = (
        f"Monthly Report · {cal_module.month_name[month]} {year}\n{DIV}\n\n"
        f"✅ {done}/{total} completed ({pct}%)\n"
        f"❌ {missed} missed\n\n"
        f"{mot}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Detail", callback_data=f"mw_{year}_{month:02d}_0")],
        [InlineKeyboardButton("← Insights", callback_data="insights")]
    ])
    await safe_edit(target, txt, kb)

# ============= CALLBACK HANDLER — ADD NEW CASES ================
async def on_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id
    ud = ctx.user_data

    if update.effective_chat.type == "private":
        update_username(uid, get_username(q.from_user))

    # === NEW UNIFIED FLOWS ===
    if data == "schedule_view":
        utz = get_tz(uid)
        now = datetime.now(utz)
        txt, kb = build_schedule_month(uid, now.year, now.month, utz)
        await safe_edit(q.message, txt, kb)
        return

    if data == "sched_today":
        await show_today_digest(q.message, uid, ctx)
        return
    if data == "sched_upcoming":
        await show_upcoming_list(q.message, uid, ctx)
        return
    if data.startswith("sched_month_"):
        parts = data[12:].split("_")
        y, m = int(parts[0]), int(parts[1])
        txt, kb = build_schedule_month(uid, y, m, get_tz(uid))
        await safe_edit(q.message, txt, kb)
        return

    if data == "insights":
        await show_insights(q.message, uid, ctx)
        return
    if data == "insight_today":
        await show_today_digest(q.message, uid, ctx, from_insights=True)
        return
    if data == "insight_weekly":
        await send_weekly_report(q.message, uid, get_tz(uid), on_demand=True)
        return
    if data == "insight_month_picker":
        now = datetime.now(get_tz(uid))
        kb = []
        row = []
        for m in range(1,13):
            name = cal_module.month_name[m][:3]
            row.append(InlineKeyboardButton(f"{name} {now.year}" if m==now.month else name, 
                                          callback_data=f"insight_month_{now.year}_{m:02d}"))
            if len(row)==3:
                kb.append(row)
                row=[]
        if row:
            kb.append(row)
        kb.append([InlineKeyboardButton("‹ Previous Year", callback_data=f"insight_month_{now.year-1}_01")])
        kb.append([InlineKeyboardButton("← Back", callback_data="insights")])
        await safe_edit(q.message, f"{hdr('Monthly Report')}\nChoose month:", InlineKeyboardMarkup(kb))
        return
    if data.startswith("insight_month_"):
        y, m = map(int, data[14:].split("_"))
        await send_monthly_report(q.message, uid, y, m, get_tz(uid))
        return

    # === ALL YOUR EXISTING CALLBACKS BELOW (unchanged) ===
    # ... keep everything from your original on_btn function exactly as it was ...
    # (cancel, gclose, pclose_, view_, done_, snz_, edit_, cfg_, etc.)

# ============= START COMMAND — SHOW NEW HOME ================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        # your existing group start code
        return
    await rm_home(ctx, ctx.user_data)
    ctx.user_data.clear()
    user = update.effective_user
    get_cfg(user.id)
    update_username(user.id, get_username(user))
    sent = await update.message.reply_text(HOME_TEXT, reply_markup=home_kb(), parse_mode="HTML")
    save_home(ctx.user_data, sent)

# ============= MAIN ================
def main():
    app = Application.builder().token(TOKEN).job_queue(JobQueue()).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    # you can remove /list and /month commands or keep them pointing to schedule_view

    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.job_queue.run_repeating(check_reminders, interval=60, first=0)
    app.job_queue.run_repeating(check_digest, interval=60, first=10)
    app.job_queue.run_repeating(check_weekly_report, interval=60, first=20)

    print("RemindX v2 — New Flow Active")
    app.run_polling()

if __name__ == "__main__":
    main()
