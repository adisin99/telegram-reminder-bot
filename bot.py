from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from telegram.ext import CallbackQueryHandler

import pytz
from datetime import timezone, timedelta

import logging
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    JobQueue,
)

import gsheet


# ================= CONFIG =================

TOKEN = "8464632180:AAGh_semPGrVtKBcMFVDy5EvIAl9bzTwcVs"

# =========================================


# Logging (shows status in CMD)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

#==============MENU=====================
def main_menu():
    keyboard = [
        [InlineKeyboardButton("➕ Add Reminder", callback_data="add")],
        [InlineKeyboardButton("📋 My Reminders", callback_data="list")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]

    return InlineKeyboardMarkup(keyboard)

# ============== COMMANDS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "👋 Welcome to Reminder Bot!\n\nChoose an option:",
        reply_markup=main_menu()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "add":

        context.user_data["step"] = "title"

        await query.message.reply_text(
            "✍️ Send reminder title:"
        )

    elif data == "list":

        await list_rem(query, context)

    elif data == "help":
            elif data.startswith("rep_"):

        repeat = data.replace("rep_", "")

        title = context.user_data["title"]
        msg = context.user_data["message"]
        date = context.user_data["date"]
        time = context.user_data["time"]

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

        gsheet.add_reminder(row)

        context.user_data.clear()

        await query.message.reply_text(
            "✅ Reminder saved!",
            reply_markup=main_menu()
        )


        await query.message.reply_text(
            "ℹ️ How to use:\n\n"
            "➕ Add: Create reminder\n"
            "📋 My Reminders: View reminders\n\n"
            "Bot works 24/7 ⏰"
        )

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send reminder like this:\n\n"
        "Title | Message | 2026-02-15 | 18:30 | none"
    )


async def save(update: Update, context: ContextTypes.DEFAULT_TYPE):

    step = context.user_data.get("step")

    if not step:
        return

    text = update.message.text

    # STEP 1: TITLE
    if step == "title":

        context.user_data["title"] = text
        context.user_data["step"] = "message"

        await update.message.reply_text("📝 Send reminder message:")

    # STEP 2: MESSAGE
    elif step == "message":

        context.user_data["message"] = text
        context.user_data["step"] = "date"

        await update.message.reply_text(
            "📅 Send date (YYYY-MM-DD):"
        )

    # STEP 3: DATE
    elif step == "date":

        context.user_data["date"] = text
        context.user_data["step"] = "time"

        await update.message.reply_text(
            "⏰ Send time (HH:MM 24hr):"
        )

    # STEP 4: TIME
    elif step == "time":

        context.user_data["time"] = text
        context.user_data["step"] = "repeat"

        keyboard = [
            [
                InlineKeyboardButton("One Time", callback_data="rep_none"),
                InlineKeyboardButton("Daily", callback_data="rep_daily")
            ],
            [
                InlineKeyboardButton("Weekly", callback_data="rep_weekly"),
                InlineKeyboardButton("Monthly", callback_data="rep_monthly")
            ]
        ]

        await update.message.reply_text(
            "🔁 Choose repeat:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def list_rem(update: Update, context: ContextTypes.DEFAULT_TYPE):

    reminders = gsheet.get_reminders(update.effective_user.id)

    if not reminders:
        await update.message.reply_text("No reminders found.")
        return

    msg = "📋 Your Reminders:\n\n"

    for i, r in enumerate(reminders, 1):
        msg += (
            f"{i}. {r['title']}\n"
            f"📅 {r['date']} ⏰ {r['time']}\n"
            f"🔁 {r['repeat']}\n\n"
        )

    await update.message.reply_text(msg)


# ============== SCHEDULER =================

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):

    try:
        ist = pytz.timezone("Asia/Kolkata")
        now = datetime.now(ist).strftime("%Y-%m-%d %H:%M")



        print("⏰ Checking reminders at:", now)

        records = gsheet.sheet.get_all_records()

        for i, row in enumerate(records, start=2):

            if row["status"] != "active":
                continue

            reminder_time = f"{row['date']} {row['time']}"

            print("Comparing:", reminder_time, "vs", now)

            if reminder_time == now:

                user_id = row["user_id"]
                title = row["title"]
                msg = row["message"]
                repeat = row["repeat"]

                text = f"⏰ {title}\n{msg}"

                print("Sending reminder to:", user_id)

                await context.bot.send_message(
                    chat_id=user_id,
                    text=text
                )

                if repeat == "none":
                    gsheet.sheet.update_cell(i, 8, "done")

    except Exception as e:
        print("Reminder Error:", e)


# ============== MAIN ======================

def main():

    app = (
        Application.builder()
        .token(TOKEN)
        .job_queue(JobQueue())
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_rem))

    # Text handler (for saving)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Scheduler (runs every minute)
    app.job_queue.run_repeating(
    check_reminders,
    interval=60,
    first=0
    )


    print("✅ Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()




