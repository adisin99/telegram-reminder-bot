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
# Sheet columns (1-indexed):
#   1: id (blank)
#   2: user_id
#   3: title
#   4: message
#   5: date       (YYYY-MM-DD)
#   6: time       (HH:MM)
#   7: repeat     (none / daily / weekly / monthly)
#   8: status     (active / pending / done / missed)
#   9: retries    (0-3)
# ==========================================

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
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Reminder", callback_data="add")],
        [InlineKeyboardButton("📋 My Reminders", callback_data="list")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ])


def reminder_buttons(row):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🕐 Remind in 1 Hour",
                callback_data=f"snooze_{row}",
            ),
            InlineKeyboardButton(
                "✅ Done",
                callback_data=f"done_{row}",
            ),
        ]
    ])


# ============= HELPER: advance repeat date =
def advance_repeat(row, r):
    """
    For repeating reminders, update the date to the next occurrence,
    set status back to 'active', and reset retry count.
    Returns True if it was a repeating reminder, False otherwise.
    """
    repeat = r[6] if len(r) > 6 else "none"
    if not repeat or repeat == "none":
        return False

    d = datetime.strptime(r[4], "%Y-%m-%d")

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


# ============= HELPER: cancel retry jobs ===
def cancel_retry_jobs(job_queue, row):
    """Cancel all pending retry jobs for a given row."""
    jobs = job_queue.get_jobs_by_name(f"retry-{row}")
    for job in jobs:
        job.schedule_removal()
    logger.info(f"Cancelled {len(jobs)} retry job(s) for row {row}")


# ============= START =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Smart Reminder Bot\n\nChoose:",
        reply_markup=main_menu(),
    )


# ============= BUTTON HANDLER ============
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # ---- ADD ----
    if data == "add":
        context.user_data.clear()
        context.user_data["step"] = "title"
        await query.message.reply_text("✍️ Title:")

    # ---- LIST ----
    elif data == "list":
        await list_reminders(query)

    # ---- HELP ----
    elif data == "help":
        await query.message.reply_text(
            "Use buttons to manage reminders.",
            reply_markup=main_menu(),
        )

    # ---- SAVE (repeat choice) ----
    elif data.startswith("rep_"):
        repeat = data.replace("rep_", "")
        row = [
            "",
            query.from_user.id,
            context.user_data["title"],
            context.user_data["message"],
            context.user_data["date"],
            context.user_data["time"],
            repeat,
            "active",
            0,
        ]
        sheet.append_row(row)
        context.user_data.clear()
        await query.message.reply_text("✅ Saved", reply_markup=main_menu())

    # ---- SNOOZE (remind in 1 hour, NO retries in between) ----
    elif data.startswith("snooze_"):
        row = int(data.replace("snooze_", ""))

        # 1. Cancel any pending retry jobs
        cancel_retry_jobs(context.job_queue, row)

        # 2. Compute new reminder time = NOW + 1 hour
        now = datetime.now(IST)
        new_time = now + timedelta(hours=1)

        # 3. Update sheet: new date/time, status back to 'active', reset retries
        sheet.update_cell(row, 5, new_time.strftime("%Y-%m-%d"))
        sheet.update_cell(row, 6, new_time.strftime("%H:%M"))
        sheet.update_cell(row, 8, "active")   # will be picked up by check_reminders later
        sheet.update_cell(row, 9, 0)

        await query.message.reply_text(
            f"🕐 Snoozed — will remind at {new_time.strftime('%H:%M')}",
            reply_markup=main_menu(),
        )

    # ---- DONE ----
    elif data.startswith("done_"):
        row = int(data.replace("done_", ""))

        # 1. Cancel any pending retry jobs
        cancel_retry_jobs(context.job_queue, row)

        # 2. Read current row
        r = sheet.row_values(row)

        # 3. If repeating, advance to next date; otherwise mark done
        if not advance_repeat(row, r):
            sheet.update_cell(row, 8, "done")
            sheet.update_cell(row, 9, 0)

        await query.message.reply_text("✅ Done", reply_markup=main_menu())


# ============= TEXT HANDLER ==============
async def save_text(update, context):
    step = context.user_data.get("step")
    if not step:
        return

    text = update.message.text.strip()

    if step == "title":
        context.user_data["title"] = text
        context.user_data["step"] = "message"
        await update.message.reply_text("📝 Message:")

    elif step == "message":
        context.user_data["message"] = text
        context.user_data["step"] = "date"
        await update.message.reply_text("📅 Date YYYY-MM-DD:")

    elif step == "date":
        context.user_data["date"] = text
        context.user_data["step"] = "time"
        await update.message.reply_text("⏰ Time HH:MM:")

    elif step == "time":
        context.user_data["time"] = text
        context.user_data["step"] = "repeat"
        await update.message.reply_text(
            "Repeat?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("One", callback_data="rep_none"),
                    InlineKeyboardButton("Daily", callback_data="rep_daily"),
                ],
                [
                    InlineKeyboardButton("Weekly", callback_data="rep_weekly"),
                    InlineKeyboardButton("Monthly", callback_data="rep_monthly"),
                ],
            ]),
        )


# ============= LIST ======================
async def list_reminders(query):
    rows = sheet.get_all_records()
    uid = query.from_user.id
    found = False

    for i, r in enumerate(rows, start=2):
        if str(r["user_id"]) != str(uid):
            continue
        # Show both 'active' and 'pending' reminders
        if r["status"] not in ("active", "pending"):
            continue

        found = True
        status_label = "🔔 Awaiting action" if r["status"] == "pending" else "📌 Scheduled"
        txt = (
            f"{status_label}\n"
            f"📌 {r['title']}\n"
            f"📅 {r['date']} ⏰ {r['time']}\n"
            f"🔁 {r['repeat']}"
        )
        await query.message.reply_text(
            txt,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🕐 1h", callback_data=f"snooze_{i}"),
                    InlineKeyboardButton("✅ Done", callback_data=f"done_{i}"),
                ]
            ]),
        )

    if not found:
        await query.message.reply_text("No reminders.", reply_markup=main_menu())


# ============= AUTO RETRY ================
async def auto_retry(context: ContextTypes.DEFAULT_TYPE):
    """
    Called every 10 min after a reminder fires.
    Re-notifies the user up to DEFAULT_MAX_RETRIES times.
    On the last retry, marks the reminder as 'missed' immediately
    (no extra notification after the final retry).
    """
    data = context.job.data
    row = data["row"]
    chat = data["chat"]

    # Read the current row from the sheet
    r = sheet.row_values(row)

    if not r:
        logger.warning(f"auto_retry: row {row} is empty, skipping.")
        return

    # Only act if status is still 'pending'
    # (If user clicked Snooze → 'active', or Done → 'done', we stop)
    if r[7] != "pending":
        logger.info(f"auto_retry: row {row} status is '{r[7]}', not 'pending'. Stopping retries.")
        return

    try:
        count = int(r[8])
    except (IndexError, ValueError):
        count = 0

    # Safety: if already at or past max, just mark missed silently
    if count >= DEFAULT_MAX_RETRIES:
        logger.info(f"auto_retry: row {row} already at max retries. Marking missed silently.")
        if not advance_repeat(row, r):
            sheet.update_cell(row, 8, "missed")
            sheet.update_cell(row, 9, 0)
        return

    # ---- Send retry notification ----
    title = r[2]
    msg = r[3]

    text = (
        f"🔔 Retry {count + 1}/{DEFAULT_MAX_RETRIES} — Still pending!\n\n"
        f"📌 {title}\n📝 {msg}"
    )

    await context.bot.send_message(
        chat_id=chat,
        text=text,
        reply_markup=reminder_buttons(row),
    )

    # Increment retry count in sheet
    new_count = count + 1
    sheet.update_cell(row, 9, new_count)

    # ---- Check if this was the LAST retry ----
    if new_count >= DEFAULT_MAX_RETRIES:
        # Mark as missed right now — no 4th notification
        logger.info(f"auto_retry: row {row} sent final retry {new_count}/{DEFAULT_MAX_RETRIES}. Marking missed.")
        if not advance_repeat(row, r):
            sheet.update_cell(row, 8, "missed")
            sheet.update_cell(row, 9, 0)
        # Do NOT schedule another job
    else:
        # Schedule the next retry
        context.job_queue.run_once(
            auto_retry,
            DEFAULT_RETRY_INTERVAL,
            data={"row": row, "chat": chat},
            name=f"retry-{row}",
        )


# ============= SCHEDULER =================
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs every 60 seconds.
    Checks for reminders whose date+time matches NOW and status is 'active'.
    Sends the initial notification, sets status to 'pending',
    and schedules the first auto-retry in 10 minutes.
    """
    now = datetime.now(IST)
    now_str = now.strftime("%Y-%m-%d %H:%M")

    rows = sheet.get_all_records()

    for i, r in enumerate(rows, start=2):
        # Only fire reminders that are 'active' (not pending/done/missed)
        if r["status"] != "active":
            continue

        rem_str = f"{r['date']} {r['time']}"
        if rem_str != now_str:
            continue

        # Fuzzy check: within ±30 seconds
        try:
            rem_dt = IST.localize(datetime.strptime(rem_str, "%Y-%m-%d %H:%M"))
            delta = abs((now - rem_dt).total_seconds())
            if delta > 30:
                continue
        except Exception:
            continue

        uid = r["user_id"]
        logger.info(f"Firing reminder row {i}: {r['title']} for user {uid}")

        # Cancel any stale retry jobs (safety)
        cancel_retry_jobs(context.job_queue, i)

        # Send initial reminder
        text = f"⏰ {r['title']}\n{r['message']}"
        await context.bot.send_message(
            chat_id=uid,
            text=text,
            reply_markup=reminder_buttons(i),
        )

        # Mark status as 'pending' — prevents check_reminders from firing again
        # and lets auto_retry know the reminder is awaiting user action.
        sheet.update_cell(i, 8, "pending")
        sheet.update_cell(i, 9, 0)

        # Schedule first auto-retry in 10 minutes
        context.job_queue.run_once(
            auto_retry,
            DEFAULT_RETRY_INTERVAL,
            data={"row": i, "chat": uid},
            name=f"retry-{i}",       # <-- PTB job name (used by get_jobs_by_name)
        )


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

    print("🚀 Smart Reminder Bot Running")
    app.run_polling()


if __name__ == "__main__":
    main()

