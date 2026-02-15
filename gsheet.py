import gspread
from oauth2client.service_account import ServiceAccountCredentials

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

import os
import json

creds_json = os.environ.get("GOOGLE_CREDS")

creds_dict = json.loads(creds_json)

creds = ServiceAccountCredentials.from_json_keyfile_dict(
    creds_dict, scope
)

client = gspread.authorize(creds)

sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1bHeyDgw9P-3iRLOp_6VpHGKSn9St6yjyqP-35hPg6Rs/edit?gid=0#gid=0").sheet1


def add_reminder(data):
    sheet.append_row(data)


def get_reminders(user_id):
    records = sheet.get_all_records()
    return [r for r in records if str(r["user_id"]) == str(user_id)]
