import logging
import os
import json
from datetime import datetime, timedelta

import pytz

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
TOKEN = "8464632180:AAGh_semPGrVtKBcMFVDy5EvIAl9bzTwcVs"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1bHeyDgw9P-3iRLOp_6VpHGKSn9St6yjyqP-35hPg6Rs/edit?pli=1&gid=0#gid=0"

IST = pytz.timezone("Asia/Kolkata")

DEFAULT_RETRY_INTERVAL = 600  # 10 minutes
DEFAULT_MAX_RETRIES = 3

# =============== LOGGING =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============= GOOGLE SHEET ==============
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

creds_json = os.environ.get("GOOGLE_CREDS")
if not creds_json:
    raise Exception("GOOGLE_CREDS missing")

creds = json.loads(creds_json)
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds, scope)
client = gspread.authorize(credentials)
sheet = client.open_by_url(SHEET_URL).sheet1


# ============= UI ========================

def home_text():
    return (
        "<b>Smart Reminder Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Manage your reminders easily."
    )


def home_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("＋ New", callback_data="add"),
            InlineKeyboardButton("☰ List", callback_data="list"),
        ],
    ])


def back_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Back", callback_data="home")],
    ])


def cancel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✕ Cancel", callback_data="cancel_add")],
    ])


def repeat_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Once", callback_data="rep_none"),
            InlineKeyboardButton("Daily", callback_data="rep_daily"),
        ],
        [
            InlineKeyboardButton("Weekly", callback_data="rep_weekly"),
            InlineKeyboardButton("Monthly", callback_data="rep_monthly"),
        ],
        [InlineKeyboardButton("✕ Cancel", callback_data="cancel_add")],
    ])


def reminder_action_kb(row):
    """Buttons shown on fired/retry reminders."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1h", callback_data=f"snooze_{row}"),
            InlineKeyboardButton("Done", callback_data=f"done_{row}"),
        ],
    ])


def list_item_kb(row):
    """Buttons for each item in the list view."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1h", callback_data=f"snooze_{row}"),
            InlineKeyboardButton("Done", callback_data=f"done_{row}"),
        ],
    ])


# ============= HELPERS ====================

def normalize_date(val):
    """Convert any date value from the sheet to YYYY-MM-DD string."""
    s = str(val).strip()
    # Already correct format
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    # Try common formats Google Sheets might return
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def normalize_time(val):
    """Convert any time value from the sheet to HH:MM string."""
    s = str(val).strip()
    # Already correct format like 09:30 or 9:30
    if ":" in s:
        parts = s.split(":")
        try:
            h = int(parts[0])
            m = int(parts[1].split()[0])  # handle "9:30 AM" etc
            # Handle AM/PM
            upper = s.upper()
            if "PM" in upper and h != 12:
                h += 12
            elif "AM" in upper and h == 12:
                h = 0
            return f"{h:02d}:{m:02d}"
        except (ValueError, IndexError):
            return s
    # Might be a float (time serial: 0.604166 = 14:30)
    try:
        f = float(s)
        total_minutes = round(f * 24 * 60)
        h = total_minutes // 60
        m = total_minutes % 60
        return f"{h:02d}:{m:02d}"
    except ValueError:
        return s


def advance_repeat(row, r):
    repeat = r[6] if len(r) > 6 else "none"
    if not repeat or repeat == "none":
        return False

    d = datetime.strptime(normalize_date(r[4]), "%Y-%m-%d")

    if repeat == "daily":
        nd = d + timedelta(days=1)
    elif repeat == "weekly":
        nd = d + timedelta(days=7)
    elif repeat == "monthly":
        m = d.month + 1
        y = d.year
        if m > 12:
            m = 1
            y += 1
        nd = d.replace(year=y, month=m)
    else:
        return False

    sheet.update_cell(row, 5, nd.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 8, "active")
    sheet.update_cell(row, 9, 0)
    return True


def cancel_retry_jobs(job_queue, row):
    jobs = job_queue.get_jobs_by_name(f"retry-{row}")
    for job in jobs:
        job.schedule_removal()
    logger.info(f"Cancelled {len(jobs)} retry job(s) for row {row}")


def format_date_short(date_str):
    """Convert YYYY-MM-DD to a shorter display like 25 Jun."""
    try:
        d = datetime.strptime(normalize_date(date_str), "%Y-%m-%d")
        return d.strftime("%-d %b")
    except Exception:
        return str(date_str)


def format_repeat(repeat):
    mapping = {"none": "Once", "daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
    return mapping.get(str(repeat), str(repeat))


# ============= SAFE EDIT ==================
async def safe_edit(message, text, reply_markup=None, parse_mode="HTML"):
    """Edit message if possible, otherwise send new one."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


# ============= START =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        home_text(),
        reply_markup=home_kb(),
        parse_mode="HTML",
    )


# ============= BUTTON HANDLER ============
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ---- HOME ----
    if data == "home":
        context.user_data.clear()
        await safe_edit(query.message, home_text(), home_kb())

    # ---- ADD ----
    elif data == "add":
        context.user_data.clear()
        context.user_data["step"] = "title"
        await safe_edit(
            query.message,
            "<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\nEnter title:",
            cancel_kb(),
        )

    # ---- CANCEL ADD ----
    elif data == "cancel_add":
        context.user_data.clear()
        await safe_edit(query.message, home_text(), home_kb())

    # ---- LIST ----
    elif data == "list":
        await list_reminders(query)

    # ---- SAVE (repeat choice) ----
    elif data.startswith("rep_"):
        repeat = data.replace("rep_", "")
        title = context.user_data.get("title", "")
        message = context.user_data.get("message", "")
        date = context.user_data.get("date", "")
        time = context.user_data.get("time", "")

        row = ["", query.from_user.id, title, message, date, time, repeat, "active", 0]
        sheet.append_row(row, value_input_option="RAW")
        context.user_data.clear()

        await safe_edit(
            query.message,
            (
                f"<b>Saved</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{title}\n"
                f"{format_date_short(date)} · {time} · {format_repeat(repeat)}"
            ),
            home_kb(),
        )

    # ---- SNOOZE ----
    elif data.startswith("snooze_"):
        row = int(data.replace("snooze_", ""))

        cancel_retry_jobs(context.job_queue, row)

        now = datetime.now(IST)
        new_time = now + timedelta(hours=1)

        sheet.update_cell(row, 5, new_time.strftime("%Y-%m-%d"))
        sheet.update_cell(row, 6, new_time.strftime("%H:%M"))
        sheet.update_cell(row, 8, "active")
        sheet.update_cell(row, 9, 0)

        await safe_edit(
            query.message,
            f"<b>Snoozed</b> — {new_time.strftime('%H:%M')}",
            home_kb(),
        )

    # ---- DONE ----
    elif data.startswith("done_"):
        row = int(data.replace("done_", ""))

        cancel_retry_jobs(context.job_queue, row)

        r = sheet.row_values(row)
        is_repeat = advance_repeat(row, r)

        if not is_repeat:
            sheet.update_cell(row, 8, "done")
            sheet.update_cell(row, 9, 0)

        await safe_edit(
            query.message,
            "<b>Done</b> ✓",
            home_kb(),
        )


# ============= TEXT HANDLER ==============
async def save_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")
    if not step:
        return

    text = update.message.text.strip()

    if step == "title":
        context.user_data["title"] = text
        context.user_data["step"] = "message"
        await update.message.reply_text(
            "<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\nEnter message:",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )

    elif step == "message":
        context.user_data["message"] = text
        context.user_data["step"] = "date"
        await update.message.reply_text(
            "<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\nDate (YYYY-MM-DD):",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )

    elif step == "date":
        context.user_data["date"] = text
        context.user_data["step"] = "time"
        await update.message.reply_text(
            "<b>New Reminder</b>\n━━━━━━━━━━━━━━━━━━━━\nTime (HH:MM):",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )

    elif step == "time":
        context.user_data["time"] = text
        context.user_data["step"] = "repeat"

        title = context.user_data["title"]
        date = context.user_data["date"]

        await update.message.reply_text(
            (
                f"<b>New Reminder</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{title}\n"
                f"{format_date_short(date)} · {text}\n\n"
                f"Repeat?"
            ),
            reply_markup=repeat_kb(),
            parse_mode="HTML",
        )


# ============= LIST ======================
async def list_reminders(query):
    rows = sheet.get_all_records()
    uid = query.from_user.id

    items = []
    for i, r in enumerate(rows, start=2):
        if str(r.get("user_id", "")) != str(uid):
            continue
        status = str(r.get("status", "")).strip()
        if status not in ("active", "pending"):
            continue
        items.append((i, r))

    if not items:
        await safe_edit(
            query.message,
            "<b>Reminders</b>\n━━━━━━━━━━━━━━━━━━━━\nNo active reminders.",
            home_kb(),
        )
        return

    # Build a single consolidated message
    lines = ["<b>Reminders</b>\n━━━━━━━━━━━━━━━━━━━━"]

    # If only one reminder, show it with action buttons
    if len(items) == 1:
        row_idx, r = items[0]
        status = str(r.get("status", ""))
        status_dot = "●" if status == "pending" else "○"
        date_str = normalize_date(r.get("date", ""))
        time_str = normalize_time(r.get("time", ""))
        repeat_str = format_repeat(r.get("repeat", "none"))
        lines.append(
            f"\n{status_dot} <b>{r.get('title', '')}</b>\n"
            f"   {format_date_short(date_str)} · {time_str} · {repeat_str}"
        )
        await safe_edit(
            query.message,
            "\n".join(lines),
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("1h", callback_data=f"snooze_{row_idx}"),
                    InlineKeyboardButton("Done", callback_data=f"done_{row_idx}"),
                ],
                [InlineKeyboardButton("« Back", callback_data="home")],
            ]),
        )
        return

    # Multiple reminders — show compact list then individual detail buttons
    buttons = []
    for idx, (row_idx, r) in enumerate(items):
        status = str(r.get("status", ""))
        status_dot = "●" if status == "pending" else "○"
        date_str = normalize_date(r.get("date", ""))
        time_str = normalize_time(r.get("time", ""))
        repeat_str = format_repeat(r.get("repeat", "none"))
        title = str(r.get("title", ""))
        lines.append(
            f"\n{status_dot} <b>{title}</b>\n"
            f"   {format_date_short(date_str)} · {time_str} · {repeat_str}"
        )
        buttons.append([
            InlineKeyboardButton(f"1h › {title[:15]}", callback_data=f"snooze_{row_idx}"),
            InlineKeyboardButton(f"Done › {title[:15]}", callback_data=f"done_{row_idx}"),
        ])

    buttons.append([InlineKeyboardButton("« Back", callback_data="home")])

    await safe_edit(
        query.message,
        "\n".join(lines),
        InlineKeyboardMarkup(buttons),
    )


# ============= AUTO RETRY ================
async def auto_retry(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    row = data["row"]
    chat = data["chat"]

    try:
        r = sheet.row_values(row)
    except Exception as e:
        logger.error(f"auto_retry: failed to read row {row}: {e}")
        return

    if not r:
        logger.warning(f"auto_retry: row {row} is empty")
        return

    if r[7] != "pending":
        logger.info(f"auto_retry: row {row} status is '{r[7]}', not 'pending'. Skipping.")
        return

    try:
        count = int(r[8])
    except (IndexError, ValueError):
        count = 0

    if count >= DEFAULT_MAX_RETRIES:
        logger.info(f"auto_retry: row {row} already at max retries ({count}). Marking missed.")
        if not advance_repeat(row, r):
            sheet.update_cell(row, 8, "missed")
            sheet.update_cell(row, 9, 0)
        return

    title = r[2]
    msg = r[3]

    text = (
        f"<b>Reminder</b> ({count + 1}/{DEFAULT_MAX_RETRIES})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{title}\n{msg}"
    )

    await context.bot.send_message(
        chat_id=chat,
        text=text,
        reply_markup=reminder_action_kb(row),
        parse_mode="HTML",
    )

    new_count = count + 1
    sheet.update_cell(row, 9, new_count)
    logger.info(f"auto_retry: row {row} retry {new_count}/{DEFAULT_MAX_RETRIES}")

    if new_count >= DEFAULT_MAX_RETRIES:
        # 3rd retry just sent — mark missed now, no more scheduling
        logger.info(f"auto_retry: row {row} max retries reached. Marking missed.")
        if not advance_repeat(row, r):
            sheet.update_cell(row, 8, "missed")
            sheet.update_cell(row, 9, 0)
    else:
        context.job_queue.run_once(
            auto_retry,
            DEFAULT_RETRY_INTERVAL,
            data={"row": row, "chat": chat},
            name=f"retry-{row}",
        )


# ============= SCHEDULER =================
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(IST)
    now_str = now.strftime("%Y-%m-%d %H:%M")

    try:
        rows = sheet.get_all_records()
    except Exception as e:
        logger.error(f"check_reminders: failed to read sheet: {e}")
        return

    logger.debug(f"check_reminders: now={now_str}, rows={len(rows)}")

    for i, r in enumerate(rows, start=2):
        status = str(r.get("status", "")).strip()
        if status != "active":
            continue

        # Normalize date and time from sheet (handles auto-formatting)
        date_val = normalize_date(r.get("date", ""))
        time_val = normalize_time(r.get("time", ""))
        rem_str = f"{date_val} {time_val}"

        logger.debug(f"check_reminders: row {i} — rem='{rem_str}' vs now='{now_str}' (raw date={r.get('date')!r}, raw time={r.get('time')!r})")

        if rem_str != now_str:
            continue

        # Minute matches — fire the reminder
        uid = r.get("user_id")
        title = str(r.get("title", ""))
        message = str(r.get("message", ""))
        logger.info(f"Firing reminder row {i}: '{title}' for user {uid}")

        cancel_retry_jobs(context.job_queue, i)

        text = (
            f"<b>Reminder</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{title}\n{message}"
        )

        try:
            await context.bot.send_message(
                chat_id=uid,
                text=text,
                reply_markup=reminder_action_kb(i),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"check_reminders: failed to send to {uid}: {e}")
            continue

        sheet.update_cell(i, 8, "pending")
        sheet.update_cell(i, 9, 0)

        context.job_queue.run_once(
            auto_retry,
            DEFAULT_RETRY_INTERVAL,
            data={"row": i, "chat": uid},
            name=f"retry-{i}",
        )

        logger.info(f"Row {i}: status → pending, retry scheduled in {DEFAULT_RETRY_INTERVAL}s")


# ============= MAIN ======================
def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .job_queue(JobQueue())
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_text))

    app.job_queue.run_repeating(
        check_reminders,
        interval=60,
        first=0,
    )

    print("Smart Reminder Bot Running")
    app.run_polling()


if __name__ == "__main__":
    main()

