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

DEFAULT_RETRY_INTERVAL = 600   # 10 minutes
DEFAULT_MAX_RETRIES = 3

# =========================================

# =============== LOGGING =================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# ============= GOOGLE SHEET ==============

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

creds_json = os.environ.get("GOOGLE_CREDS")

if not creds_json:
    raise Exception("GOOGLE_CREDS missing")

creds = json.loads(creds_json)

credentials = ServiceAccountCredentials.from_json_keyfile_dict(
    creds, scope
)

client = gspread.authorize(credentials)

sheet = client.open_by_url(SHEET_URL).sheet1

# ============= UI ========================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Reminder", callback_data="add")],
        [InlineKeyboardButton("📋 My Reminders", callback_data="list")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ])

# Only 1-hour snooze and Done buttons (no 10-min button)
def reminder_buttons(row):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🕐 Remind in 1 Hour",
                callback_data=f"snooze_{row}"
            ),
            InlineKeyboardButton(
                "✅ Done",
                callback_data=f"done_{row}"
            )
        ]
    ])

# ============= START =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Smart Reminder Bot\n\nChoose:",
        reply_markup=main_menu()
    )

# ============= BUTTON HANDLER ============

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # ADD
    if data == "add":
        context.user_data.clear()
        context.user_data["step"] = "title"
        await query.message.reply_text("✍️ Title:")

    # LIST
    elif data == "list":
        await list_reminders(query)

    # HELP
    elif data == "help":
        await query.message.reply_text(
            "Use buttons to manage reminders.",
            reply_markup=main_menu()
        )

    # SAVE
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
            0
        ]

        sheet.append_row(row)
        context.user_data.clear()

        await query.message.reply_text(
            "✅ Saved",
            reply_markup=main_menu()
        )

    # SNOOZE (1 Hour)
    elif data.startswith("snooze_"):
        row = int(data.replace("snooze_", ""))

        # Cancel ALL pending retry jobs for this reminder
        current_jobs = context.job_queue.get_jobs_by_name(f"retry-{row}")
        for job in current_jobs:
            job.schedule_removal()

        # Snooze for 60 minutes
        snooze(row, 60)

        # Reset retry count to 0 (fresh start after snooze)
        sheet.update_cell(row, 9, 0)

        await query.message.reply_text(
            "🕐 Snoozed for 1 hour",
            reply_markup=main_menu()
        )

    # DONE
    elif data.startswith("done_"):
        row = int(data.replace("done_", ""))

        # Cancel ALL pending retry jobs
        current_jobs = context.job_queue.get_jobs_by_name(f"retry-{row}")
        for job in current_jobs:
            job.schedule_removal()

        # Mark as done
        sheet.update_cell(row, 8, "done")
        sheet.update_cell(row, 9, 0)

        await query.message.reply_text(
            "✅ Done",
            reply_markup=main_menu()
        )

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
                ]
            ])
        )

# ============= LIST ======================

async def list_reminders(query):
    rows = sheet.get_all_records()
    uid = query.from_user.id
    found = False

    for i, r in enumerate(rows, start=2):
        if str(r["user_id"]) != str(uid):
            continue

        if r["status"] != "active":
            continue

        found = True

        txt = (
            f"📌 {r['title']}\n"
            f"📅 {r['date']} ⏰ {r['time']}\n"
            f"🔁 {r['repeat']}"
        )

        await query.message.reply_text(
            txt,
            reply_markup=reminder_buttons(i)
        )

    if not found:
        await query.message.reply_text(
            "No reminders.",
            reply_markup=main_menu()
        )

# ============= SNOOZE ====================

def snooze(row, mins):
    r = sheet.row_values(row)

    dt = datetime.strptime(
        f"{r[4]} {r[5]}",
        "%Y-%m-%d %H:%M"
    )

    new = dt + timedelta(minutes=mins)

    sheet.update_cell(row, 5, new.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, new.strftime("%H:%M"))
    sheet.update_cell(row, 8, "active")

# ============= AUTO RETRY ================

async def auto_retry(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    row = data["row"]
    chat = data["chat"]

    # Cancel this job after running
    context.job.schedule_removal()

    r = sheet.row_values(row)

    if not r:
        return

    # Check if status is still active
    if r[7] != "active":
        return

    try:
        count = int(r[8])  # Current retry count
    except:
        count = 0

    # Check if max retries reached
    if count >= DEFAULT_MAX_RETRIES:
        logging.info(f"Max retries ({DEFAULT_MAX_RETRIES}) reached for row {row}. Marking as missed.")
        sheet.update_cell(row, 8, "missed")
        return

    title = r[2]
    msg = r[3]

    # Show retry count in message
    text = f"🔔 Still pending... (Retry {count + 1}/{DEFAULT_MAX_RETRIES})\n\n📌 {title}\n📝 {msg}"

    await context.bot.send_message(
        chat_id=chat,
        text=text,
        reply_markup=reminder_buttons(row)
    )

    # Increment retry count
    sheet.update_cell(row, 9, count + 1)

    # Schedule next retry if under max
    if count + 1 < DEFAULT_MAX_RETRIES:
        logging.info(f"Scheduling retry {count + 2}/{DEFAULT_MAX_RETRIES} for row {row}")
        context.job_queue.run_once(
            auto_retry,
            DEFAULT_RETRY_INTERVAL,
            data={
                "row": row,
                "chat": chat
            },
            job_kwargs={"name": f"retry-{row}"}
        )
    else:
        logging.info(f"This is the last retry for row {row}")

# ============= SCHEDULER =================

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(IST)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    
    rows = sheet.get_all_records()

    for i, r in enumerate(rows, start=2):
        if r["status"] != "active":
            continue

        rem_str = f"{r['date']} {r['time']}"
        if rem_str != now_str:
            continue

        # Fuzzy: confirm within ±30s
        try:
            rem_dt = IST.localize(datetime.strptime(rem_str, "%Y-%m-%d %H:%M"))
            delta = abs((now - rem_dt).total_seconds())
            if delta > 30:
                continue
        except:
            continue

        uid = r["user_id"]

        # Cancel any prior retry jobs (safety)
        current_jobs = context.job_queue.get_jobs_by_name(f"retry-{i}")
        for job in current_jobs:
            job.schedule_removal()

        # Send the reminder
        text = f"⏰ {r['title']}\n{r['message']}"
        await context.bot.send_message(
            chat_id=uid,
            text=text,
            reply_markup=reminder_buttons(i)
        )

        # Reset retry count for fresh reminder
        sheet.update_cell(i, 9, 0)

        # Schedule auto-retry job (triggers every 10 min if user doesn't respond)
        logging.info(f"Scheduling auto-retry for row {i}")
        context.job_queue.run_once(
            auto_retry,
            DEFAULT_RETRY_INTERVAL,
            data={
                "row": i,
                "chat": uid
            },
            job_kwargs={"name": f"retry-{i}"}
        )

        # Handle repeat logic
        if r["repeat"] == "none":
            # Don't mark as done yet - let auto-retry handle it
            pass
        else:
            d = datetime.strptime(r["date"], "%Y-%m-%d")
            if r["repeat"] == "daily":
                nd = d + timedelta(days=1)
            elif r["repeat"] == "weekly":
                nd = d + timedelta(days=7)
            elif r["repeat"] == "monthly":
                m = d.month + 1
                y = d.year
                if m > 12:
                    m = 1
                    y += 1
                nd = d.replace(year=y, month=m)

            sheet.update_cell(
                i, 5, nd.strftime("%Y-%m-%d")
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
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, save_text)
    )

    app.job_queue.run_repeating(
        check_reminders,
        interval=60,
        first=0
    )

    print("🚀 Smart Reminder Bot Running")
    print("✅ 1-hour snooze button available")
    print("✅ Auto-retry every 10 minutes (max 3 retries)")
    print("✅ Marks as 'missed' after all retries")

    app.run_polling()

# =======================================

if __name__ == "__main__":
    main()
