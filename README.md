# Court Share Bot

A Telegram bot for groups that share court time — badminton, tennis, squash, pickleball, anything you book by the hour and split between the players who showed up.

Type `/poll Sunday June 7th (Court 3) — 12:00-1:00 pm` in your group chat. The bot posts a Yes/No poll, charges you as the payer, tracks who voted Yes, enforces a per-court Yes cap with an auto-promoting waitlist, and keeps a running ledger of who owes whom. Members can check `/balance` or `/summary` any time; you can record cash with `/paid` and generate a monthly `/report` that arrives in the chat as an Excel file.

## Why this exists

I split court bookings with friends every week. The arithmetic is simple — court fee ÷ Yes votes — but the bookkeeping isn't: people change their minds, someone covers a session you didn't, and at end of month you're squinting at a chat history trying to remember who owes what. This bot does it for me.

The implementation is intentionally small (two Python files plus a Flask shim for webhook mode). Data lives in flat JSON files, so it's easy to inspect, back up, or hand-edit when reality and the bot disagree.

## How it works

```
┌──────────────────────┐       /poll, /balance, …       ┌──────────────────┐
│  Your Telegram group │  ──────────────────────────►   │  fetch_telegram_ │
│  (members + bot)     │  ◄──── poll status, pings ──   │   polls.py       │
└──────────────────────┘                                └─────────┬────────┘
                                                                  │
                                                          state   ▼
                                                    ┌───────────────────────┐
                                                    │  polls.json           │
                                                    │  payments.json        │
                                                    │  .users.json          │
                                                    └─────────┬─────────────┘
                                                              │
                                                  /report     ▼
                                                    ┌───────────────────────┐
                                                    │  badminton_shares.py  │
                                                    │  parses titles for    │
                                                    │  courts/hours, splits │
                                                    │  equally, writes xlsx │
                                                    └───────────────────────┘
```

**Why the bot creates polls instead of you using Telegram's `+ → Poll` UI:** the Bot API only delivers vote events (`poll_answer`) for polls the bot itself sent. User-created polls show up to the bot as a creation message but votes are invisible. So we make the bot the one calling `sendPoll`.

**Cost math:** the bot parses the poll title for `(Court N)` (single court) or `(Court N,M)` (multiple courts) and a time range like `5:00-6:00 pm`. Total = `courts × hours × rate`. Default rate is $25/court/hour. Each Yes voter pays an equal share.

**Waitlist:** the poll never auto-closes. The first `cap × courts` Yes voters are "in"; further Yes votes are silently waitlisted. If someone retracts, the next waitlister is promoted automatically and pinged. Status lives in one message that the bot edits in place under the poll, so the chat stays clean.

## Quick start (local long-polling)

You need a Mac/Linux box that can run Python and a Telegram account.

1. **Create a bot.** In Telegram message **@BotFather**:

   ```
   /newbot                                 → choose a name + handle, save the token
   /mybots → <your bot> → Bot Settings → Group Privacy → Turn off
   ```

   Without disabling privacy mode, the bot only sees messages starting with `/`. With it off, the bot sees every message in groups it's added to, including the polls it sends.

2. **Add the bot to your group.** Group title → Add Member → search the bot's `@handle`. No admin permission needed unless you want it to delete or pin messages.

3. **Clone, install, configure.**

   ```bash
   git clone https://github.com/milad1372/court-share-bot.git
   cd court-share-bot
   pip3 install -r requirements.txt
   cp .env.example .env             # then put your bot token in .env
   chmod +x run_telegram.command    # macOS launcher
   ```

   `.env` only needs the token; everything else has defaults:

   ```env
   TELEGRAM_BOT_TOKEN=123456:AA…
   # Optional:
   # BADMINTON_COLLECTOR=Your Name
   # BADMINTON_RATE=25
   # BADMINTON_MAX_PER_COURT=6
   ```

4. **Run.**

   ```bash
   ./run_telegram.command       # macOS, double-clickable from Finder too
   # or:
   python3 fetch_telegram_polls.py
   ```

   You should see `Application built. Collector=… rate=$25.00/court/hour cap=6 Yes/court`. The bot long-polls Telegram; if you stop and restart, Telegram queues missed updates for ~24h so you'll catch up.

## Deploying to the cloud (free, no credit card)

For 24/7 uptime without leaving your laptop on, deploy in **webhook mode** to PythonAnywhere's free tier. Telegram POSTs every update to a secret URL on your domain, the Flask app in `wsgi.py` dispatches it to the bot.

Full walkthrough in **[DEPLOYMENT.md](DEPLOYMENT.md)** — ~15 minutes end-to-end. The short version:

```bash
# On PythonAnywhere's Bash console:
git clone https://github.com/milad1372/court-share-bot.git
cd court-share-bot
pip3 install --user -r requirements.txt
# Then in the Web tab, point the WSGI file at wsgi.py, set TELEGRAM_BOT_TOKEN
# + BOT_WEBHOOK_SECRET as env vars, reload, and run:
python3 set_webhook.py
```

DEPLOYMENT.md also covers `launchd` on macOS and `systemd` on a Pi/Linux box for self-hosting.

## Commands

| Command | Who | What it does |
| --- | --- | --- |
| `/poll <title>` | anyone | Posts a Yes/No poll, replies with a live status message that updates as votes come in. Sender is the payer. |
| `/balance` | anyone | Your current net with the collector. |
| `/summary` | anyone | Everyone's balance, settled-out, in one message. |
| `/accounts` | anyone | Table: Name / Charged / Paid / Covered / Net + month totals. |
| `/report` | anyone | Session-by-session list and an attached `.xlsx`. |
| `/rate` | anyone | Court rate and cap. |
| `/polls` | anyone | Last 15 captured polls. |
| `/paid <name> [amount]` | collector | Record cash received. Reply-to also works. Omit amount to settle in full. |
| `/credit <name> [amount]` | collector | Same math as `/paid`, logged differently. |
| `/reset` | collector | Archive current month's state to a date-stamped file and start fresh. |

Each command also appears in Telegram's `/` autocomplete because the bot registers them with `setMyCommands` on startup.

## Data files

All live in the project folder. Local-only — kept out of git via `.gitignore`.

| File | Purpose | Lifespan |
| --- | --- | --- |
| `polls.json` | Every captured poll: id, date, title, sender (payer), options with voters + waitlist, status message id. | Until `/reset` archives it. |
| `payments.json` | Append-only log of payments received and credits granted. | Until `/reset` archives it. |
| `.users.json` | `display name → telegram user_id` so the bot can build clickable mentions. | Forever; preserved across resets. |
| `polls-archived-YYYY-MM-DD.json` | Snapshot of `polls.json` when you ran `/reset`. | Forever — your historical record. |
| `.env` | `TELEGRAM_BOT_TOKEN` and optional config. | Forever (not committed). |

## Standalone calculator

If you just need the spreadsheet math — say you have polls in a different format you want to import — `badminton_shares.py` runs without the bot:

```bash
# Compute shares from a JSON file the bot wrote:
python3 badminton_shares.py --input polls.json --output may.xlsx

# Or edit the hardcoded POLLS list at the bottom of the script and run:
python3 badminton_shares.py --output may.xlsx
```

The output has four sheets: **Polls** (one row per session), **Owes \<collector\>** (settlement view), **Settlement** (per-person net), and **Who owes whom** (pairwise).

## Configuration knobs

Either env vars (in `.env` or the shell) or CLI flags. Examples:

```bash
BADMINTON_MAX_PER_COURT=2 ./run_telegram.command         # smaller cap for testing
python3 fetch_telegram_polls.py --rate 30                # weekend rate
python3 fetch_telegram_polls.py --collector "Sara Lee"   # someone else collects
```

## Limitations and gotchas

- **Bots can only see vote events for polls they themselves sent.** Always use `/poll`. Polls created via the regular `+ → Poll` UI will show up to the bot as a title with no voters, and the bot will warn the creator in chat.
- **The bot must be in the group before the poll is posted.** No history API.
- **Anonymous polls are useless** — the API doesn't reveal voter identity. The bot sends polls with `is_anonymous=false`; don't override that.
- **Edit window:** Telegram lets you edit a message for ~48 hours. After that the live status message stops updating in place; the bot transparently posts a fresh one.
- **No multi-currency, multi-payer-per-poll, or partial-attendance support.** Equal split among Yes voters, single payer (the `/poll` sender). If reality differs, adjust `polls.json` by hand and re-run `/report`.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

Built iteratively with [Claude](https://claude.ai). Telegram's free Bot API made the whole thing radically simpler than the alternatives.
