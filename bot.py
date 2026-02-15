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
    raise Exception("GOOGLE_CREDS not set in Railway Variables")

creds_dict = json.loads(creds_json)

creds = ServiceAccountCredentials.from_json_keyfile_dict(
    creds_dict, scope
)

client = gspread.authorize(creds)

sheet = client.open_by_url(SHEET_URL).sheet1


# ============= UI MENU ===================

def main_menu():

    keyboard = [
        [InlineKeyboardButton("➕ Add Reminder", callback_data="add")],
        [InlineKeyboardButton("📋 My Reminders", callback_data="list")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ]

    return InlineKeyboardMarkup(keyboard)


# ============= START =====================

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


    # ADD
    if data == "add":

        context.user_data.clear()
        context.user_data["step"] = "title"

        await query.message.reply_text("✍️ Send reminder title:")


    # LIST
    elif data == "list":

        await list_reminders(query, context)


    # HELP
    elif data == "help":

        await query.message.reply_text(
            "ℹ️ Use buttons to manage reminders.",
            reply_markup=main_menu()
        )


    # SAVE (REPEAT)
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


    # DELETE
    elif data.startswith("del_"):

        row_num = int(data.replace("del_", ""))

        sheet.update_cell(row_num, 8, "deleted")

        await qu
