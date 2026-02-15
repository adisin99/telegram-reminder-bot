import logging
import os
import json
from datetime import datetime

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
    raise Exception("GOOGLE_CREDS not set in Railway Variables")

creds_dict = json.loads(creds_json)

creds = ServiceAccountCredentials.from_json_keyfile_dict(
    creds_dict, scope
)

client = gspread.authorize(creds)

SHEET_URL = "https://docs.google.com/spreadsheets/d/1bHeyDgw9P-3iRLOp_6VpHGKSn9St6yjyqP-35hPg6Rs/edit?pli=1&gid=0#gid=0"

sheet = client.open_by_url(SHEET_URL).sheet1


# ============= UI MENU ===================

def main_menu():
    keyboard = [
        [InlineKeyboardButton("➕ Add Reminder", callback_data="add")],
        [InlineKeyboardButton("📋 My Reminders", callback_data="list")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ]

    return InlineKeyboardMarkup(keyboard)


# ============= COMMANDS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "👋 Welcome to Reminder Bot!\n\nChoose an option:",
        reply_markup=main_menu()
    )


# ============= BUTTON HANDLER ============

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    data = query.data


    # ADD FLOW
    if data == "add":

        context.user_data.clear()
        context.user_data["step"] = "title"

        await query.message.reply_text(
            "✍️ Send reminder title:"
        )


    # LIST
    elif data == "list":

        await list_reminders(query, context)


    # HELP
    elif data == "help":

        await query.message.reply_text(
            "ℹ️ How to use:\n\n"
            "➕ Add → Create reminder\n"
            "📋 My Reminders → View reminders\n\n"
            "Bot works 24/7 ⏰",
            reply_markup=main_menu()
        )


    # REPEAT SELECT
    elif data.startswith("rep_"):

        repeat = data.replace("rep_", "")

        title = context.user_data.get("title")
        msg = context.user_data.get("message")
        date = context.user_data.get("date")
        time = context.user_data.get("time")

        row = [
            "",
            query.from_user.id,
            title,
            msg,
            date,
            time,
            repeat,
            "active"
        ]

        sheet.append_row(row)

        context.user_data.clear()

        await query.message.reply_text(
            "✅ Reminder saved!",
            reply_markup=main_menu()
        )
#Delete
    elif data.startswith("del_"):

        row_num = int(data.replace("del_", ""))

        sheet.update_cell(row_num, 8, "deleted")

        await query.message.reply_text(
            "🗑 Reminder deleted.",
            reply_markup=main_menu()
        )

#Skip
    elif data.startswith("skip_"):

        row_num = int(data.replace("skip_", ""))

        sheet.update_cell(row_num, 8, "skip")

        await query.message.reply_text(
            "⏭ Next reminder skipped.",
            reply_markup=main_menu()
        )

# ============= TEXT HANDLER ==============

async def save_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    step = context.user_data.get("step")

    if not step:
        return


    text = update.message.text.strip()


    # STEP 1
    if step == "title":

        context.user_data["title"] = text
        context.user_data["step"] = "message"

        await update.message.reply_text(
            "📝 Send reminder message:"
        )


    # STEP 2
    elif step == "message":

        context.user_data["message"] = text
        context.user_data["step"] = "date"

        await update.message.reply_text(
            "📅 Send date (YYYY-MM-DD):"
        )


    # STEP 3
    elif step == "date":

        context.user_data["date"] = text
        context.user_data["step"] = "time"

        await update.message.reply_text(
            "⏰ Send time (HH:MM 24hr):"
        )


    # STEP 4
    elif step == "time":

        context.user_data["time"] = text
        context.user_data["step"] = "repeat"

        keyboard = [
            [
                InlineKeyboardButton("One Time", callback_data="rep_none"),
                InlineKeyboardButton("Daily", callback_data="rep_daily"),
            ],
            [
                InlineKeyboardButton("Weekly", callback_data="rep_weekly"),
                InlineKeyboardButton("Monthly", callback_data="rep_monthly"),
            ]
        ]

        await update.message.reply_text(
            "🔁 Choose repeat:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ============= LIST REMINDERS ============

async def list_reminders(query, context):

    records = sheet.get_all_records()
    user_id = query.from_user.id

    user_rows = []

    # Collect active reminders with row index
    for idx, r in enumerate(records, start=2):

        if (
            str(r["user_id"]) == str(user_id)
            and r["status"] == "active"
        ):
            user_rows.append((idx, r))


    if not user_rows:
        await query.message.reply_text(
            "No active reminders found.",
            reply_markup=main_menu()
        )
        return


    for count, (row_num, r) in enumerate(user_rows, 1):

        text = (
            f"{count}. {r['title']}\n"
            f"📅 {r['date']} ⏰ {r['time']}\n"
            f"🔁 {r['repeat']}"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "🗑 Delete",
                    callback_data=f"del_{row_num}"
                ),
                InlineKeyboardButton(
                    "⏭ Skip Once",
                    callback_data=f"skip_{row_num}"
                )
            ]
        ]

        await query.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


    await query.message.reply_text(
        "⬅️ Back to menu",
        reply_markup=main_menu()
    )


# ============= SCHEDULER =================

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):

    try:

        now = datetime.now(IST).strftime("%Y-%m-%d %H:%M")

        print("⏰ Checking at:", now)

        records = sheet.get_all_records()

        for i, row in enumerate(records, start=2):

        status = row["status"]

# Skip once
        if status == "skip":

    # Reset back to active
            sheet.update_cell(i, 8, "active")
            continue


# Ignore inactive
        if status != "active":
            continue


            reminder_time = f"{row['date']} {row['time']}"

            if reminder_time == now:

                user_id = row["user_id"]
                title = row["title"]
                msg = row["message"]
                repeat = row["repeat"]

                text = f"⏰ {title}\n{msg}"

                await context.bot.send_message(
                    chat_id=user_id,
                    text=text
                )

                # One time
                if repeat == "none":
                    sheet.update_cell(i, 8, "done")

    except Exception as e:
        print("Reminder Error:", e)


# ============= MAIN ======================

def main():

    app = (
        Application.builder()
        .token(TOKEN)
        .job_queue(JobQueue())
        .build()
    )


    # Commands
    app.add_handler(CommandHandler("start", start))

    # Buttons
    app.add_handler(CallbackQueryHandler(button_handler))

    # Text input
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, save_text)
    )


    # Scheduler
    app.job_queue.run_repeating(
        check_reminders,
        interval=60,
        first=0
    )


    print("✅ Bot is running...")

    app.run_polling()


# =======================================

if __name__ == "__main__":
    main()



