"""
set_webhook.py — one-shot helper to tell Telegram where to POST updates.

Run from a PythonAnywhere Bash console (or anywhere with the token in env)
ONCE per deploy URL change:

    python3 set_webhook.py

It calls Telegram's setWebhook with:
  • the URL of your PythonAnywhere webapp + /bot/<secret>
  • the list of update types we actually care about
  • optionally, a header secret Telegram will send on every POST
"""

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request
import json


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("BOT_WEBHOOK_SECRET", "")
HEADER_SECRET = os.environ.get("BOT_HEADER_SECRET", "")
BASE_URL = os.environ.get("BOT_BASE_URL")  # e.g. https://miladmomeni.pythonanywhere.com

if not TOKEN:
    sys.exit("Missing TELEGRAM_BOT_TOKEN.")
if not BASE_URL:
    sys.exit("Missing BOT_BASE_URL, e.g. https://<you>.pythonanywhere.com")
if not WEBHOOK_SECRET:
    sys.exit("Missing BOT_WEBHOOK_SECRET — needed for URL path.")

webhook_url = f"{BASE_URL.rstrip('/')}/bot/{WEBHOOK_SECRET}"

params = {
    "url": webhook_url,
    # Updates we actually care about:
    "allowed_updates": json.dumps([
        "message", "edited_message",
        "poll", "poll_answer",
    ]),
    "drop_pending_updates": "false",
}
if HEADER_SECRET:
    params["secret_token"] = HEADER_SECRET

query = urllib.parse.urlencode(params)
api = f"https://api.telegram.org/bot{TOKEN}/setWebhook?{query}"

print(f"Setting webhook to {webhook_url}")
with urllib.request.urlopen(api) as resp:
    body = resp.read().decode("utf-8")
print(body)
