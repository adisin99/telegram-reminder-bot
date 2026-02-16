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


    elif data.startswith("snooze_"):

        row = int(data.replace("snooze_", ""))

        snooze(row, 60)

        sheet.update_cell(row, 8, "snoozed")
        sheet.update_cell(row, 9, 0)

        await query.message.reply_text(
            "🕐 Snoozed 1 hour",
            reply_markup=main_menu()
        )


    elif data.startswith("done_"):

        row = int(data.replace("done_", ""))

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

    dt = datetime.strptime(
        f"{r[4]} {r[5]}",
        "%Y-%m-%d %H:%M"
    )

    new = dt + timedelta(minutes=mins)

    sheet.update_cell(row, 5, new.strftime("%Y-%m-%d"))
    sheet.update_cell(row, 6, new.strftime("%H:%M"))
    sheet.update_cell(row, 8, "active")


# ============= AUTO RETRY ================

async def auto_retry(context):

    data = context.job.data

    row = data["row"]
    chat = data["chat"]


    r = sheet.row_values(row)

    if not r:
        return


    status = r[7]

    # Only retry if active
    if status != "active":
        return


    try:
        count = int(r[8])
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


    # Update counter
    sheet.update_cell(row, 9, count + 1)


    # Schedule next retry
    context.job_queue.run_once(
        auto_retry,
        DEFAULT_RETRY_INTERVAL,
        data={
            "row": row,
            "chat": chat
        }
    )


# ============= SCHEDULER =================

async def check_reminders(context):

    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M")

    rows = sheet.get_all_records()


for i, r in enumerate(rows, start=2):

    # Only active or snoozed reminders
    if r["status"] not in ["active", "snoozed"]:
        continue


    try:
        rem_time = datetime.strptime(
            f"{r['date']} {r['time']}",
            "%Y-%m-%d %H:%M"
        ).replace(tzinfo=IST)

    except:
        print(f"⚠️ Invalid date/time in row {i}")
        continue


    now_dt = datetime.now(IST)

    # Not yet time
    if rem_time > now_dt:
        continue


    # Send reminder
    await context.bot.send_message(
        chat_id=r["user_id"],
        text=f"⏰ {r['title']}\n\n{r['message']}",
        reply_markup=reminder_buttons(i)
    )


    # Activate retry cycle
    sheet.update_cell(i, 8, "active")
    sheet.update_cell(i, 9, 0)


    # Schedule auto retry
    context.job_queue.run_once(
        auto_retry,
        DEFAULT_RETRY_INTERVAL,
        data={
            "row": i,
            "chat": r["user_id"]
        }
    )



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

    app.run_polling()


# =======================================

if __name__ == "__main__":
    main()




