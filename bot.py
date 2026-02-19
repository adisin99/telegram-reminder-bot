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

DEFAULT_RETRY_INTERVAL = 600   # 10 min
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

    # SNOOZE
    elif data.startswith("snooze_"):
        row = int(data.replace("snooze_", ""))
    
    # Cancel priors
        current_jobs = context.job_queue.get_jobs_by_name(f"notify-{row}")
        for job in current_jobs:
            job.schedule_removal()
    
        snooze(row, 60)  # or 10 for testing
    
        # Schedule direct notification
        r = sheet.row_values(row)
        dt = IST.localize(datetime.strptime(f"{r[4]} {r[5]}", "%Y-%m-%d %H:%M"))
        context.job_queue.run_once(
            send_notification,
            when=dt,
            data={"row": row, "chat": query.from_user.id},
            job_kwargs={"name": f"notify-{row}"}
        )
    
        await query.message.reply_text(
            f"🕐 Snoozed {60 if mins==60 else 10} min",
            reply_markup=main_menu()
        )

    # DONE - unchanged, but add cancel
    elif data.startswith("done_"):
        row = int(data.replace("done_", ""))
    
        # Cancel pending jobs
        current_jobs = context.job_queue.get_jobs_by_name(f"notify-{row}")
        for job in current_jobs:
            job.schedule_removal()
    
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
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🕐 1h", callback_data=f"snooze_{i}"),
                    InlineKeyboardButton("✅ Done", callback_data=f"done_{i}")
                ]
            ])
        )

    if not found:

        await query.message.reply_text(
            "No reminders.",
            reply_markup=main_menu()
        )

# ============= SNOOZE ====================

def snooze(row, mins):
    r = sheet.row_values(row)
    
    dt = datetime.strptime(f"{r[4]} {r[5]}", "%Y-%m-%d %H:%M")
    new_dt = IST.localize(dt) + timedelta(minutes=mins)  # IST-aware
    
    sheet.update_cell(row, 5, new_dt.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, new_dt.strftime("%H:%M"))
    sheet.update_cell(row, 8, "active")  # status
    sheet.update_cell(row, 9, 0)  # retries

# ============= SEND NOTIFICATION ================
async def send_notification(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    row = data["row"]
    chat = data["chat"]
    
    context.job.schedule_removal()
    logging.info(f"Snooze/Retry notification row {row} to {chat}")
    
    r = sheet.row_values(row)
    if not r or r[7] != "active":
        logging.warning(f"Row {row} inactive")
        return
    
    title = r[2]
    msg = r[3]
    
    text = f"🔔 Still pending...\n\n📌 {title}\n📝 {msg}"
    
    await context.bot.send_message(
        chat_id=chat,
        text=text,
        reply_markup=reminder_buttons(row)
    )

async def send_notification_immediate(context: ContextTypes.DEFAULT_TYPE, row: int, chat_id: int):
    """Send NOW + schedule 10min retry"""
    r = sheet.row_values(row)
    if not r:
        logging.error(f"No row data {row}")
        return
        
    title = r[2] or "Reminder"
    msg = r[3] or ""
    
    text = f"⏰ {title}\n\n{msg}"
    logging.info(f"Sending immediate to {chat_id}, row {row}")
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reminder_buttons(row)
    )
    
    # 10min retry
    retry_time = datetime.now(IST) + timedelta(minutes=10)
    context.job_queue.run_once(
        send_notification,
        when=retry_time,
        data={"row": row, "chat": chat_id},
        job_kwargs={"name": f"notify-{row}"}
    )
    logging.info(f"Scheduled retry {retry_time} for row {row}")
    
# ============= AUTO RETRY ================

async def auto_retry(context: ContextTypes.DEFAULT_TYPE):

    data = context.job.data

    row = data["row"]
    chat = data["chat"]

    # Cancel this job itself after running (prevents chain if multiple)
    context.job.schedule_removal()

    r = sheet.row_values(row)

    if not r:
        return

    if r[7] != "active":  # status col (1-indexed 8, 0-indexed 7)
        return

    try:
        count = int(r[8])  # retries col (1-indexed 9, 0-indexed 8)
    except:
        count = 0

    if count >= DEFAULT_MAX_RETRIES:
        return

    title = r[2]
    msg = r[3]

    text = f"🔔 Still pending...\n\n📌 {title}\n📝 {msg}"

    await context.bot.send_message(
        chat_id=chat,
        text=text,
        reply_markup=reminder_buttons(row)
    )

    sheet.update_cell(row, 9, count + 1)

    # Reschedule next retry if under max
    if count + 1 < DEFAULT_MAX_RETRIES:
        context.job_queue.run_once(
            auto_retry,
            DEFAULT_RETRY_INTERVAL,
            data={
                "row": row,
                "chat": chat
            },
            job_kwargs={"name": f"retry-{row}"}
        )

# ============= SCHEDULER =================

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(IST)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    logging.info(f"[{now_str}] Checking {len(sheet.get_all_records())} rows")
    
    rows = sheet.get_all_records()
    
    for i, r in enumerate(rows, start=2):
        if r["status"] != "active":
            continue
            
        rem_str = f"{r['date']} {r['time']}"
        if rem_str != now_str:
            continue
        
        logging.info(f"Potential match row {i}: {rem_str}")
        
        # Wider fuzzy: ±60s + try naive if localize fails
        try:
            rem_dt = IST.localize(datetime.strptime(rem_str, "%Y-%m-%d %H:%M"))
            delta = abs((now - rem_dt).total_seconds())
            logging.info(f"Delta: {delta}s for row {i}")
            if delta > 60:  # 60s window
                continue
        except Exception as e:
            logging.error(f"DT error row {i}: {e}")
            continue
        
        uid = r["user_id"]
        logging.info(f"TRIGGERING row {i} for user {uid}")
        
        # Cancel priors
        current_jobs = context.job_queue.get_jobs_by_name(f"notify-{i}")
        for job in current_jobs:
            job.schedule_removal()
        
        # Send + retry
        await send_notification_immediate(context, i, uid)
        
        # Repeats...
        if r["repeat"] == "none":
            sheet.update_cell(i, 8, "done")
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
            sheet.update_cell(i, 5, nd.strftime("%Y-%m-%d"))
        
        sheet.update_cell(i, 9, 0)
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
        interval=30,
        first=0
    )

    print("🚀 Smart Reminder Bot Running")

    app.run_polling()

# =======================================

if __name__ == "__main__":
    main()




