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
            InlineKeyboardButton("🕐 Remind in 1 Hour", callback_data=f"snooze_{row}"),
            InlineKeyboardButton("✅ Done", callback_data=f"done_{row}")
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

    if data == "add":
        context.user_data.clear()
        context.user_data["step"] = "title"
        await query.message.reply_text("✍️ Title:")

    elif data == "list":
        await list_reminders(query)

    elif data == "help":
        await query.message.reply_text(
            "Use buttons to manage reminders.",
            reply_markup=main_menu()
        )

    elif data.startswith("rep_"):
        repeat = data.replace("rep_", "")
        row = [
            "", query.from_user.id, context.user_data["title"],
            context.user_data["message"], context.user_data["date"],
            context.user_data["time"], repeat, "active", 0
        ]
        sheet.append_row(row)
        context.user_data.clear()
        await query.message.reply_text("✅ Saved", reply_markup=main_menu())

    # CLEAN SNOOZE - 1hr single notification
    elif data.startswith("snooze_"):
        row = int(data.replace("snooze_", ""))
        
        # Cancel ALL alarm jobs for this row
        jobs = context.job_queue.get_jobs_by_name(f"alarm-{row}")
        for job in jobs:
            job.schedule_removal()
        
        snooze(row, 60)
        
        # Schedule SINGLE notification (no retry chain)
        r = sheet.row_values(row)
        alarm_time = IST.localize(datetime.strptime(f"{r[4]} {r[5]}", "%Y-%m-%d %H:%M"))
        context.job_queue.run_once(
            alarm_notification,
            when=alarm_time,
            data={"row": row, "uid": query.from_user.id},
            job_kwargs={"name": f"alarm-{row}"}
        )
        
        await query.message.reply_text("🕐 Snoozed 1 hour", reply_markup=main_menu())

    # CLEAN DONE
    elif data.startswith("done_"):
        row = int(data.replace("done_", ""))
        
        # Cancel ALL jobs
        jobs = context.job_queue.get_jobs_by_name(f"alarm-{row}")
        for job in jobs:
            job.schedule_removal()
        
        sheet.update_cell(row, 8, "done")
        sheet.update_cell(row, 9, 0)
        await query.message.reply_text("✅ Done", reply_markup=main_menu())

# ============= TEXT HANDLER ==============
async def save_text(update, context):
    step = context.user_data.get("step")
    if not step: return
    
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
                [InlineKeyboardButton("One", callback_data="rep_none"),
                 InlineKeyboardButton("Daily", callback_data="rep_daily")],
                [InlineKeyboardButton("Weekly", callback_data="rep_weekly"),
                 InlineKeyboardButton("Monthly", callback_data="rep_monthly")]
            ])
        )

# ============= LIST ======================
async def list_reminders(query):
    rows = sheet.get_all_records()
    uid = query.from_user.id
    found = False
    
    for i, r in enumerate(rows, start=2):
        if str(r["user_id"]) != str(uid) or r["status"] != "active":
            continue
        
        found = True
        txt = f"📌 {r['title']}\n📅 {r['date']} ⏰ {r['time']}\n🔁 {r['repeat']}"
        await query.message.reply_text(txt, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🕐 1h", callback_data=f"snooze_{i}"),
             InlineKeyboardButton("✅ Done", callback_data=f"done_{i}")]
        ]))
    
    if not found:
        await query.message.reply_text("No reminders.", reply_markup=main_menu())

# ============= SNOOZE HELPER ==============
def snooze(row, minutes):
    r = sheet.row_values(row)
    dt = datetime.strptime(f"{r[4]} {r[5]}", "%Y-%m-%d %H:%M")
    new_dt = IST.localize(dt) + timedelta(minutes=minutes)
    
    sheet.update_cell(row, 5, new_dt.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, new_dt.strftime("%H:%M"))
    sheet.update_cell(row, 8, "active")
    sheet.update_cell(row, 9, 0)

# ============= MAIN ALARM LOGIC ================
async def alarm_notification(context: ContextTypes.DEFAULT_TYPE):
    """Handles initial + 3 auto-retries → missed"""
    data = context.job.data
    row, uid = data["row"], data["uid"]
    
    context.job.schedule_removal()
    
    r = sheet.row_values(row)
    if not r or r[7] not in ["active", "retry"]:
        return
    
    title, message = r[2], r[3]
    retry_count = int(r[9])
    
    text = f"🔔 {'Still pending...' if retry_count > 0 else ''} {title}\n\n{message}"
    await context.bot.send_message(uid, text, reply_markup=reminder_buttons(row))
    
    logging.info(f"Alarm row={row}, retry={retry_count}")
    
    # 3 RETRIES MAX
    if retry_count < 3:
        next_time = datetime.now(IST) + timedelta(minutes=10)
        context.job_queue.run_once(
            alarm_notification,
            when=next_time,
            data={"row": row, "uid": uid},
            job_kwargs={"name": f"alarm-{row}"}
        )
        sheet.update_cell(row, 9, retry_count + 1)
        sheet.update_cell(row, 8, "retry")
    else:
        sheet.update_cell(row, 8, "missed")
        logging.info(f"Row {row} MARKED MISSED")

# ============= SCHEDULER =================
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    rows = sheet.get_all_records()
    
    for i, r in enumerate(rows, start=2):
        if r.get("status") != "active": continue
        if f"{r['date']} {r['time']}" != now_str: continue
        
        # Fuzzy ±45s
        try:
            rem_dt = IST.localize(datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M"))
            if abs((datetime.now(IST) - rem_dt).total_seconds()) > 45: continue
        except: continue
        
        uid = r["user_id"]
        
        # Cancel existing jobs
        jobs = context.job_queue.get_jobs_by_name(f"alarm-{i}")
        for job in jobs: job.schedule_removal()
        
        # Start alarm chain
        context.job_queue.run_once(
            alarm_notification,
            datetime.now(IST) + timedelta(seconds=2),
            data={"row": i, "uid": uid},
            job_kwargs={"name": f"alarm-{i}"}
        )
        
        # Repeats
        if r["repeat"] != "none":
            d = datetime.strptime(r["date"], "%Y-%m-%d")
            if r["repeat"] == "daily": nd = d + timedelta(days=1)
            elif r["repeat"] == "weekly": nd = d + timedelta(days=7)
            elif r["repeat"] == "monthly":
                m, y = d.month + 1, d.year
                if m > 12: m, y = 1, y + 1
                nd = d.replace(year=y, month=m)
            sheet.update_cell(i, 5, nd.strftime("%Y-%m-%d"))
        
        sheet.update_cell(i, 9, 0)

# ============= MAIN ======================
def main():
    app = Application.builder().token(TOKEN).job_queue(JobQueue()).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_text))
    
    app.job_queue.run_repeating(check_reminders, interval=30, first=0)
    
    print("🚀 Smart Reminder Bot Running ✅")
    app.run_polling()

if __name__ == "__main__":
    main()
