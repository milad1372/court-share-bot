# Deployment

The bot supports two modes:

- **Polling mode** (`fetch_telegram_polls.py`) — a long-running process that calls `getUpdates` in a loop. Easy local dev, works on any always-on machine.
- **Webhook mode** (`wsgi.py`) — a Flask app that receives POSTs from Telegram. Required for serverless / PaaS hosts that don't keep long-running processes alive without a credit card.

## Recommended: PythonAnywhere (free, no credit card)

PythonAnywhere has a real free tier — no card, no auto-charges, no surprise expiry. Caveat: free accounts must log in once every 3 months or the webapp is paused. That's the only catch.

We deploy in webhook mode. Telegram POSTs every update to `https://<your-handle>.pythonanywhere.com/bot/<your-secret>`, and our Flask app dispatches it to the bot.

### One-time setup

1. **Sign up.** Create a free Beginner account at <https://www.pythonanywhere.com/registration/register/beginner/>. No card needed.

2. **Clone the repo.** On PythonAnywhere's dashboard → **Consoles** → start a **Bash** console:

   ```bash
   git clone https://github.com/milad1372/court-share-bot.git
   cd court-share-bot
   pip3 install --user -r requirements.txt
   ```

3. **Create the webapp.** Dashboard → **Web** → **Add a new web app** → **Manual configuration** → Python 3.10 (or whatever PA offers latest). PA gives you a URL like `https://<your-handle>.pythonanywhere.com`.

4. **Point the WSGI file at our app.** In the Web tab, scroll to *Code* → click the **WSGI configuration file** link. Replace its contents with:

   ```python
   import sys, os
   project = "/home/<your-handle>/court-share-bot"
   if project not in sys.path:
       sys.path.insert(0, project)

   # Load .env-style env vars (only needed if you don't set them in the PA UI)
   from pathlib import Path
   envfile = Path(project) / ".env"
   if envfile.exists():
       for line in envfile.read_text().splitlines():
           if "=" in line and not line.startswith("#"):
               k, _, v = line.partition("=")
               os.environ.setdefault(k.strip(), v.strip())

   from wsgi import flask_app as application
   ```

   Replace `<your-handle>` with your actual PythonAnywhere username.

5. **Set env vars.** In the Web tab → *Environment variables*, add:

   | Variable | Value |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | the token from @BotFather |
   | `BOT_WEBHOOK_SECRET` | any random string ≥16 chars — becomes part of the webhook URL |
   | `BOT_HEADER_SECRET` | another random string (optional but recommended) |
   | `BADMINTON_COLLECTOR` | display name of the collector (optional) |
   | `BADMINTON_RATE` | $/court/hour (optional) |
   | `BADMINTON_MAX_PER_COURT` | Yes cap (optional) |

6. **Reload the webapp** (green Reload button on the Web tab).

7. **Tell Telegram where the bot lives.** Back in the Bash console:

   ```bash
   cd ~/court-share-bot
   export TELEGRAM_BOT_TOKEN="$(grep TELEGRAM_BOT_TOKEN .env | cut -d= -f2- || echo $TELEGRAM_BOT_TOKEN)"
   # Make sure these match what you set in the Web tab:
   export BOT_WEBHOOK_SECRET="...your-webhook-secret..."
   export BOT_HEADER_SECRET="...your-header-secret..."   # optional
   export BOT_BASE_URL="https://<your-handle>.pythonanywhere.com"
   python3 set_webhook.py
   ```

   Output should be `{"ok":true,"result":true,"description":"Webhook was set"}`.

### Verify it works

Send `/help` to the bot in your Telegram group. The bot should reply within ~2 seconds.

If nothing happens, check:

- **Server log:** Web tab → Log files → **error log**. Most issues (missing dependency, wrong path, missing env var) show up here.
- **Telegram-side log:** in a Bash console, `curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"` shows the most recent error Telegram saw when it tried to deliver an update.

### Keeping it alive

- Once every ~3 months PythonAnywhere will email you to log in or the account is paused. Just log in and click any link.
- Whenever you change env vars or push new code (`git pull`), hit the green **Reload** button on the Web tab.

### State files

`polls.json`, `payments.json`, `.users.json`, plus any `*-archived-*.json` files live in `/home/<your-handle>/court-share-bot/`. The free tier has 512 MB of disk; the bot uses a few KB. To back them up:

```bash
cd ~/court-share-bot
tar czf ../bot-state-$(date +%Y%m%d).tgz polls.json payments.json .users.json
```

…then download via the Files tab.

## Alternative: long-polling on a machine you control

If you have an always-on Mac, Raspberry Pi, or any Linux box, polling mode is simpler — no public URL, no webhook secret. See the `launchd` install script for macOS:

```bash
./deploy/install-launchd.sh install
./deploy/install-launchd.sh status
tail -f logs/bot.out
```

…or on a Pi / Linux:

```ini
# /etc/systemd/system/court-share-bot.service
[Unit]
Description=Court Share Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/pi/court-share-bot
EnvironmentFile=/home/pi/court-share-bot/.env
ExecStart=/usr/bin/python3 fetch_telegram_polls.py
Restart=always
RestartSec=10
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now court-share-bot
journalctl -u court-share-bot -f
```

## Switching from one to the other

If you set up the webhook and want to go back to polling (or vice versa):

```bash
# Polling mode requires NO webhook to be set. Clear it:
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/deleteWebhook"

# Then run polling locally:
python3 fetch_telegram_polls.py
```

…or switch the other way by running `set_webhook.py` again.

## Files involved in webhook mode

| File | Role |
| --- | --- |
| `wsgi.py` | Flask app PythonAnywhere serves. Receives POSTs, dispatches to the bot. |
| `set_webhook.py` | One-shot helper that calls Telegram's `setWebhook` with the right URL. |
| `fetch_telegram_polls.py` | Same file — `build_application()` is reused by `wsgi.py`. The `main()` here is unused in webhook mode. |
| `badminton_shares.py` | Same math module used in both modes. |
