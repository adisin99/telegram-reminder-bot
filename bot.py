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


# ============== COMMANDS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Reminder Bot!\n\n"
        "Use /add to add reminder\n"
        "Use /list to see reminders\n\n"
        "Format:\n"
        "Title | Message | YYYY-MM-DD | HH:MM | none/daily/weekly/monthly"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send reminder like this:\n\n"
        "Title | Message | 2026-02-15 | 18:30 | none"
    )


async def save(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        parts = update.message.text.split("|")

        if len(parts) != 5:
            raise ValueError

        title = parts[0].strip()
        msg = parts[1].strip()
        date = parts[2].strip()
        time = parts[3].strip()
        repeat = parts[4].strip().lower()

        row = [
            "",
            update.effective_user.id,
            title,
            msg,
            date,
            time,
            repeat,
            "active"
        ]

        gsheet.add_reminder(row)

        await update.message.reply_text("✅ Reminder saved!")

    except:
        await update.message.reply_text(
            "❌ Wrong format.\n\n"
            "Use:\n"
            "Title | Message | YYYY-MM-DD | HH:MM | none"
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
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

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

