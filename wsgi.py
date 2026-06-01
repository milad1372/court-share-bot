"""
wsgi.py — Webhook entry point for the Court Share Bot.

Designed to be served by any WSGI host (PythonAnywhere, gunicorn, mod_wsgi,
etc.). Polling mode is *not* used here — instead, Telegram POSTs every update
to a secret URL on your domain and this app dispatches it to the bot.

PythonAnywhere setup is documented in DEPLOYMENT.md. The shortest version:

  1. Put your bot token in the WSGI app's environment via the PA dashboard:
       Web → your webapp → Environment Variables → add TELEGRAM_BOT_TOKEN.
     Set BOT_WEBHOOK_SECRET to any random string; it becomes part of the URL
     Telegram will hit, so non-Telegram traffic can't talk to your bot.
  2. Point the WSGI configuration file at this module's `application`.
  3. Run `python set_webhook.py` once (from a PA Bash console) to tell
     Telegram the URL.

Why a secret in the URL? Telegram's webhook spec recommends putting a long
random token in the path so only Telegram (which you told the URL to) can
reach the bot. We also verify Telegram's `X-Telegram-Bot-Api-Secret-Token`
header when it's set.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from flask import Flask, abort, jsonify, request
from telegram import Update

from fetch_telegram_polls import build_application

log = logging.getLogger("wsgi")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)


# ── Config -----------------------------------------------------------------

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]           # required
WEBHOOK_SECRET = os.environ.get("BOT_WEBHOOK_SECRET", "")  # required-but-recommended
HEADER_SECRET = os.environ.get("BOT_HEADER_SECRET", "")    # optional extra header check

# State files live next to this module by default. On PythonAnywhere this is
# `/home/<username>/court-share-bot/polls.json` etc. Override with
# BOT_STATE_DIR if you'd rather keep them elsewhere.
STATE_DIR = Path(os.environ.get("BOT_STATE_DIR", Path(__file__).parent))
OUTPUT = str(STATE_DIR / "polls.json")

COLLECTOR = os.environ.get("BADMINTON_COLLECTOR", "Milad Momeni")
RATE = float(os.environ.get("BADMINTON_RATE", "25"))
MAX_PER_COURT = int(os.environ.get("BADMINTON_MAX_PER_COURT", "6"))


# ── Build the bot application once, at module import time ----------------

application = build_application(
    token=TOKEN, output=OUTPUT,
    collector=COLLECTOR, rate=RATE,
    max_yes_per_court=MAX_PER_COURT,
)

# python-telegram-bot 21+ requires initialize() before process_update().
# We can't await it at import time without a running loop, so we run it on
# the dedicated event loop we create below.
_loop = asyncio.new_event_loop()
_loop.run_until_complete(application.initialize())
log.info("Application initialised; waiting for webhook POSTs.")


# ── Flask wrapper that PythonAnywhere serves ------------------------------

flask_app = Flask(__name__)


@flask_app.get("/")
def healthcheck():
    """Plain page so you can `curl` the deployment URL to verify it's up."""
    return (
        "Court Share Bot is alive. POST updates to "
        f"/bot/&lt;your-secret&gt;.",
        200,
    )


@flask_app.post(f"/bot/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/bot/<token>")
def telegram_webhook(token: str | None = None):
    """Endpoint Telegram POSTs every update to."""
    # When the path used <token>, verify it matches.
    if not WEBHOOK_SECRET and token != TOKEN.split(":", 1)[0]:
        abort(403, "bad path")
    # Optional extra header check (set in BotFather → setWebhook secret token).
    if HEADER_SECRET:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if provided != HEADER_SECRET:
            abort(403, "bad secret header")

    payload = request.get_json(silent=True)
    if not payload:
        abort(400, "no body")

    update = Update.de_json(payload, application.bot)
    if update is None:
        abort(400, "could not parse update")

    # Hand off to python-telegram-bot.
    _loop.run_until_complete(application.process_update(update))
    return jsonify(ok=True)


# WSGI conventions: most servers look for `application`; we already have one
# from build_application(). Expose Flask under that name too so PythonAnywhere
# can serve either.
application_wsgi = flask_app
# PythonAnywhere's default wsgi.py uses `application` as the WSGI callable, so
# we rename for clarity. If you configure PA to point at this module, set the
# wsgi.py to: `from wsgi import flask_app as application`.
