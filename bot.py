import os
import json
import logging
from datetime import datetime, timedelta

import pytz
import gspread
from oauth2client.service_account import ServiceAccountCredentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ================= CONFIG =================

TOKEN = os.getenv("BOT_TOKEN")
CREDS = os.getenv("GOOGLE_CREDS")

IST = pytz.timezone("Asia/Kolkata")

MAX_RETRY = 3
RETRY_GAP = 600       # 10 min
SNOOZE_1H = 3600      # 1 hour


# ================= LOG ====================

logging.basicConfig(level=logging.INFO)


# ================= GOOGLE SHEET ===========

if not CREDS:
    raise Exception("GSHEET_CREDS missing")

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

creds_dict = json.loads(CREDS)

creds = ServiceAccountCredentials.from_json_keyfile_dict(
    creds_dict, scope
)

client = gspread.authorize(creds)

# 👉 Use sheet ID (recommended)
sheet = client.open_by_key("1bHeyDgw9P-3iRLOp_6VpHGKSn9St6yjyqP-35hPg6Rs").sheet1


# ================= HELPERS ================

def now():
    return datetime.now(IST)


def buttons(rid):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🕐 Remind in 1 Hour",
                callback_data=f"snooze_{rid}"
            ),
            InlineKeyboardButton(
                "✅ Done",
                callback_data=f"done_{rid}"
            ),
        ]
    ])


def rows():
    return sheet.get_all_records()


# ================= COMMANDS ===============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "👋 Smart Reminder Bot\n\n"
        "/add → Add reminder\n"
        "/list → View reminders"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Format:\n\n"
        "Title | Message | YYYY-MM-DD | HH:MM | repeat\n\n"
        "repeat = none/daily/weekly/monthly"
    )


async def save(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if "|" not in update.message.text:
        return

    try:
        t, m, d, tm, r = [x.strip() for x in update.message.text.split("|")]

        uid = update.message.chat_id

        sheet.append_row([
            uid, t, d, tm, m, r, "",
            "active",  # status
            0          # retry
        ])

        await update.message.reply_text("✅ Saved")

    except:
        await update.message.reply_text("❌ Wrong format")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    uid = str(update.message.chat_id)

    txt = "📋 Your Reminders\n\n"

    found = False

    for i, r in enumerate(rows(), start=2):

        if str(r["user_id"]) == uid:

            found = True

            txt += f"{i-1}. {r['title']} | {r['date']} {r['time']} | {r['status']}\n"

    if not found:
        txt = "No reminders"

    await update.message.reply_text(txt)


# ================= BUTTONS ================

async def buttons_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query
    await q.answer()

    data = q.data
    rid = int(data.split("_")[1])

    row = rid + 1


    # DONE
    if data.startswith("done_"):

        sheet.update_cell(row, 8, "done")
        sheet.update_cell(row, 9, 0)

        await q.message.reply_text("✅ Done")


    # SNOOZE
    elif data.startswith("snooze_"):

        t = now() + timedelta(seconds=SNOOZE_1H)

        sheet.update_cell(row, 3, t.strftime("%Y-%m-%d"))
        sheet.update_cell(row, 4, t.strftime("%H:%M"))

        sheet.update_cell(row, 8, "active")
        sheet.update_cell(row, 9, 0)

        await q.message.reply_text("⏰ Snoozed 1 hour")


# ================= RETRY ==================

async def retry_job(ctx: ContextTypes.DEFAULT_TYPE):

    row_id = ctx.job.data

    r = sheet.row_values(row_id)

    if len(r) < 9:
        return

    if r[7] != "active":
        return

    count = int(r[8])

    if count >= MAX_RETRY:
        return


    await ctx.bot.send_message(
        r[0],
        f"⏰ Reminder (Retry)\n\n{r[1]}\n{r[4]}",
        reply_markup=buttons(row_id - 2)
    )

    sheet.update_cell(row_id, 9, count + 1)


# ================= MAIN CHECK =============

async def check(ctx: ContextTypes.DEFAULT_TYPE):

    now_t = now()

    for i, r in enumerate(rows(), start=2):

        if r["status"] != "active":
            continue

        try:
            rt = datetime.strptime(
                f"{r['date']} {r['time']}",
                "%Y-%m-%d %H:%M"
            ).replace(tzinfo=IST)

        except:
            continue


        if now_t >= rt:

            await ctx.bot.send_message(
                r["user_id"],
                f"⏰ Reminder\n\n{r['title']}\n{r['message']}",
                reply_markup=buttons(i - 2)
            )


            # reset retry
            sheet.update_cell(i, 9, 0)


            # schedule retry
            ctx.job_queue.run_once(
                retry_job,
                RETRY_GAP,
                data=i
            )


            # repeat logic
            if r["repeat"] == "none":

                sheet.update_cell(i, 8, "done")


            elif r["repeat"] == "daily":

                n = rt + timedelta(days=1)
                sheet.update_cell(i, 3, n.strftime("%Y-%m-%d"))


            elif r["repeat"] == "weekly":

                n = rt + timedelta(days=7)
                sheet.update_cell(i, 3, n.strftime("%Y-%m-%d"))


            elif r["repeat"] == "monthly":

                n = rt + timedelta(days=30)
                sheet.update_cell(i, 3, n.strftime("%Y-%m-%d"))


# ================= MAIN ===================

def main():

    if not TOKEN:
        raise Exception("BOT_TOKEN missing")


    app = ApplicationBuilder().token(TOKEN).build()


    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))

    app.add_handler(CallbackQueryHandler(buttons_cb))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, save)
    )


    # check every minute
    app.job_queue.run_repeating(check, 60)


    print("🚀 Smart Reminder Bot Running")

    app.run_polling()


if __name__ == "__main__":
    main()
