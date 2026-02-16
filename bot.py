import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
import pytz

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler


# ================== CONFIG ==================

TOKEN = os.getenv("BOT_TOKEN")

IST = pytz.timezone("Asia/Kolkata")

MAX_RETRIES = 3
RETRY_INTERVAL = 600  # 10 min
SNOOZE_1H = 3600


# ================== LOGGING ==================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


# ================== GOOGLE SHEET ==================

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

creds_json = os.getenv("GOOGLE_CREDS")

creds_dict = json.loads(creds_json)

creds = ServiceAccountCredentials.from_json_keyfile_dict(
    creds_dict, scope
)

client = gspread.authorize(creds)

sheet = client.open_by_key("1bHeyDgw9P-3iRLOp_6VpHGKSn9St6yjyqP-35hPg6Rs").sheet1



# ================== HELPERS ==================

def get_rows():
    data = sheet.get_all_records()
    return data


def now_ist():
    return datetime.now(IST)


def build_buttons(rem_id):

    keyboard = [
        [
            InlineKeyboardButton(
                "🕐 Remind in 1 Hour",
                callback_data=f"snooze_{rem_id}",
            ),
            InlineKeyboardButton(
                "✅ Done",
                callback_data=f"done_{rem_id}",
            ),
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# ================== COMMANDS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "👋 Welcome to Smart Reminder Bot!\n\n"
        "Use /add to add reminder\n"
        "Use /list to see reminders"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Send in format:\n\n"
        "Title | Message | YYYY-MM-DD | HH:MM | repeat\n\n"
        "Repeat: none/daily/weekly/monthly\n\n"
        "Example:\n"
        "Workout | Gym time | 2026-02-20 | 07:00 | daily"
    )


async def save_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    if "|" not in text:
        return

    try:
        parts = [p.strip() for p in text.split("|")]

        title = parts[0]
        msg = parts[1]
        date = parts[2]
        time = parts[3]
        repeat = parts[4]

        user_id = update.message.chat_id

        sheet.append_row([
            user_id,
            title,
            date,
            time,
            msg,
            repeat,
            "",
            "active",  # status
            0          # retry_count
        ])

        await update.message.reply_text("✅ Reminder saved!")

    except Exception:
        await update.message.reply_text("❌ Format error!")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    uid = update.message.chat_id

    rows = get_rows()

    txt = "📋 Your Reminders:\n\n"

    found = False

    for i, r in enumerate(rows, start=2):

        if str(r["user_id"]) == str(uid):

            found = True

            txt += f"{i-1}. {r['title']} | {r['date']} {r['time']} | {r['status']}\n"

    if not found:
        txt = "No reminders found."

    await update.message.reply_text(txt)


# ================== BUTTON HANDLER ==================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    data = query.data

    rem_id = int(data.split("_")[1])

    row = rem_id + 1


    # DONE
    if data.startswith("done_"):

        sheet.update_cell(row, 8, "done")
        sheet.update_cell(row, 9, 0)

        await query.message.reply_text("✅ Marked as done!")


    # SNOOZE 1 HOUR
    elif data.startswith("snooze_"):

        now = now_ist() + timedelta(seconds=SNOOZE_1H)

        sheet.update_cell(row, 3, now.strftime("%Y-%m-%d"))
        sheet.update_cell(row, 4, now.strftime("%H:%M"))

        sheet.update_cell(row, 8, "snoozed")
        sheet.update_cell(row, 9, 0)

        await query.message.reply_text("⏰ Snoozed for 1 hour!")


# ================== AUTO RETRY ==================

async def auto_retry(app, row_id):

    row = sheet.row_values(row_id)

    if len(row) < 9:
        return

    status = row[7]
    retries = int(row[8])

    if status != "active":
        return

    if retries >= MAX_RETRIES:
        return


    chat_id = row[0]
    title = row[1]
    msg = row[4]

    await app.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ Reminder (Retry)\n\n{title}\n{msg}",
        reply_markup=build_buttons(row_id - 2),
    )

    sheet.update_cell(row_id, 9, retries + 1)


# ================== MAIN CHECKER ==================

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):

    rows = get_rows()

    now = now_ist()


    for i, r in enumerate(rows, start=2):

        status = r["status"]

        if status not in ["active", "snoozed"]:
            continue


        try:
            rem_time = datetime.strptime(
                f"{r['date']} {r['time']}",
                "%Y-%m-%d %H:%M"
            ).replace(tzinfo=IST)

        except Exception:
            continue


        if now >= rem_time:


            chat_id = r["user_id"]
            title = r["title"]
            msg = r["message"]


            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ Reminder\n\n{title}\n{msg}",
                reply_markup=build_buttons(i - 2),
            )


            # Activate retry
            sheet.update_cell(i, 8, "active")
            sheet.update_cell(i, 9, 0)


            # Schedule retry
            context.job_queue.run_once(
                lambda c, rid=i: auto_retry(context.application, rid),
                RETRY_INTERVAL,
            )


            # Handle repeat
            if r["repeat"] == "none":

                sheet.update_cell(i, 8, "done")


            elif r["repeat"] == "daily":

                nxt = rem_time + timedelta(days=1)
                sheet.update_cell(i, 3, nxt.strftime("%Y-%m-%d"))


            elif r["repeat"] == "weekly":

                nxt = rem_time + timedelta(days=7)
                sheet.update_cell(i, 3, nxt.strftime("%Y-%m-%d"))


            elif r["repeat"] == "monthly":

                nxt = rem_time + timedelta(days=30)
                sheet.update_cell(i, 3, nxt.strftime("%Y-%m-%d"))


# ================== MAIN ==================

async def main():

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(CommandHandler("text", save_reminder))


    scheduler = AsyncIOScheduler()
    scheduler.start()

    scheduler.add_job(
        lambda: asyncio.create_task(
            check_reminders(app)
        ),
        "interval",
        minutes=1,
    )


    print("🚀 Smart Reminder Bot Running")

    await app.run_polling()


if __name__ == "__main__":

    asyncio.run(main())


