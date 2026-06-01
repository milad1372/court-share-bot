"""
fetch_telegram_polls.py — Court Share Bot for Telegram.

A long-polling bot that runs the booking + share-tracking flow for a group
that shares court time. State is persisted to three flat JSON files next to
this script so a crash/restart loses nothing:

  • polls.json    — every poll the bot has captured, including each option's
                    voters list and a "waitlist" for overflow Yes votes.
  • payments.json — append-only log of payments received by the collector
                    (and "credits" — same math, different note tag).
  • .users.json   — display name → telegram user_id, so the bot can build
                    tg://user mentions that fire real notifications.

Commands (registered with BotFather as well):

  Anyone in the group:
    /poll <title>   The bot posts the Yes/No poll, replies with a live
                    status message (edited in place as votes arrive), and
                    charges the sender as the payer for that session.
    /balance        Reply with your current net with the collector.
    /summary        Everyone's balance after payments are applied.
    /accounts       Full table: charged / paid / covered / net + month totals.
    /report         Session-by-session summary + .xlsx attachment.
    /rate           Court rate and cap.
    /polls          Recent polls (last 15) with waitlist counts.
    /help           List of commands.

  Collector only:
    /paid <name> [amount]    Record cash received. Without amount, settles
                             the person's full outstanding balance.
    /credit <name> [amount]  Same math as /paid, but logged as a credit.
    /reset                   Archive polls.json + payments.json with a
                             date-stamped suffix and start a fresh month.

Why bot-creates-polls (and not just `+ Poll` in the UI)?
The Telegram Bot API only delivers `poll_answer` events for polls **the bot
itself sent**. User-created polls show up to the bot as a creation message
but votes are invisible. So we make the bot the one calling `sendPoll`.

One-time setup:
  1. @BotFather → /newbot   (save the token)
     @BotFather → /mybots → <yours> → Bot Settings → Group Privacy → Disable
  2. Add the bot to your group; promote to admin only if you want it to
     delete or pin messages.
  3. `pip3 install "python-telegram-bot[ext]>=21" --break-system-packages`
  4. Put the token in `.env` next to this file:
        TELEGRAM_BOT_TOKEN=123456:abc…
     and run `./run_telegram.command` (or `python3 fetch_telegram_polls.py`).

Configuration via env vars or CLI flags (CLI wins):
  TELEGRAM_BOT_TOKEN      bot token from @BotFather
  BADMINTON_COLLECTOR     display name of the person who collects (default
                          "Milad Momeni"). Used for the collector-only
                          commands and for "Owes <name>" wording.
  BADMINTON_RATE          $ per court per hour. Default 25.
  BADMINTON_MAX_PER_COURT Yes-vote cap per court. Extras go on a waitlist.
                          Default 6.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from dataclasses import dataclass

try:
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        PollAnswerHandler,
        PollHandler,
        filters,
    )
except ImportError:
    sys.stderr.write(
        'python-telegram-bot is not installed.\n'
        'Run:  pip3 install "python-telegram-bot[ext]>=21" --break-system-packages\n'
    )
    sys.exit(1)


# Pull share-math helpers from the calculator script.
sys.path.insert(0, str(Path(__file__).parent))
from badminton_shares import (  # noqa
    Poll, compute_cost, build_collector_view, write_report,
    parse_courts, RATE_PER_COURT_PER_HOUR,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("badminton_bot")


# ────────────────────────────────────────────────────────────────────────────
# Storage
# ────────────────────────────────────────────────────────────────────────────

class PollStore:
    """JSON-backed map of poll_id → record."""

    def __init__(self, path: Path):
        self.path = path
        self._by_id: dict[str, dict] = {}
        if path.exists():
            try:
                for p in json.loads(path.read_text()):
                    self._by_id[p["id"]] = p
                log.info("Loaded %d existing polls from %s", len(self._by_id), path)
            except json.JSONDecodeError:
                log.warning("Could not parse %s, starting fresh", path)

    def upsert(self, poll: dict):
        self._by_id[poll["id"]] = poll
        self._save()

    def get(self, poll_id: str) -> dict | None:
        return self._by_id.get(poll_id)

    def all_records(self) -> list[dict]:
        return sorted(self._by_id.values(), key=lambda p: p.get("date", ""))

    def _save(self):
        self.path.write_text(json.dumps(self.all_records(), indent=2))


class PaymentLog:
    """Log of payments collected by the collector.

    Each entry: { date, debtor, amount, note, recorded_by }
    """

    def __init__(self, path: Path):
        self.path = path
        self._items: list[dict] = []
        if path.exists():
            try:
                self._items = json.loads(path.read_text())
            except json.JSONDecodeError:
                pass

    def add(self, *, debtor: str, amount: float, note: str = "", recorded_by: str = ""):
        from datetime import date
        self._items.append({
            "date": date.today().isoformat(),
            "debtor": debtor,
            "amount": amount,
            "note": note,
            "recorded_by": recorded_by,
        })
        self._save()

    def total_paid_by(self, name: str) -> float:
        return sum(p["amount"] for p in self._items
                   if p.get("debtor", "").lower() == name.lower())

    def totals_by_debtor(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self._items:
            d = p.get("debtor", "")
            out[d] = out.get(d, 0.0) + float(p.get("amount", 0))
        return out

    def all_items(self) -> list[dict]:
        return list(self._items)

    def clear(self):
        self._items = []
        self._save()

    def _save(self):
        self.path.write_text(json.dumps(self._items, indent=2))


class UserIndex:
    """display_name → telegram user_id, persisted to disk.

    We need user_ids so we can build tg://user?id=… mentions, which fire a
    real notification for the target (unlike plain text containing their name).
    """

    def __init__(self, path: Path):
        self.path = path
        self._by_name: dict[str, int] = {}
        if path.exists():
            try:
                self._by_name = {k: int(v) for k, v in json.loads(path.read_text()).items()}
            except (json.JSONDecodeError, ValueError):
                pass

    def record(self, user) -> str:
        """Remember this user, return their display name."""
        name = display_name(user)
        if user and getattr(user, "id", None):
            if self._by_name.get(name) != user.id:
                self._by_name[name] = user.id
                self._save()
        return name

    def id_for(self, name: str) -> int | None:
        return self._by_name.get(name)

    def _save(self):
        self.path.write_text(json.dumps(self._by_name, indent=2))


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def display_name(user) -> str:
    if not user:
        return "Unknown"
    name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
    if name:
        return name
    if user.username:
        return f"@{user.username}"
    return f"User {user.id}"


# Characters Markdown (legacy parse mode) treats as special inside link text.
def _md_escape(s: str) -> str:
    return s.replace("[", "(").replace("]", ")").replace("_", "\\_").replace("*", "\\*")


def mention(name: str, user_index: "UserIndex") -> str:
    """A Markdown mention that pings the user. Falls back to plain name."""
    uid = user_index.id_for(name)
    if uid:
        return f"[{_md_escape(name)}](tg://user?id={uid})"
    # tg://user mentions only work for users the bot has seen; otherwise
    # at least format the name as bold so it's visually distinct.
    return f"*{_md_escape(name)}*"


def records_to_polls(records: list[dict]) -> list[Poll]:
    """Convert stored JSON records → Poll dataclass list (yes = option 0)."""
    polls = []
    for r in records:
        opts = r.get("options") or []
        yes = opts[0].get("voters", []) if opts else []
        polls.append(Poll(
            date=r.get("date", ""),
            title=r.get("title", ""),
            yes_voters=list(yes),
            payer=r.get("sender") or "Milad",
        ))
    return polls


def fmt_amount(x: float) -> str:
    return f"${x:,.2f}"


def _chunk(text: str, limit: int) -> list[str]:
    """Split a long text into Telegram-safe chunks at newline boundaries."""
    if len(text) <= limit:
        return [text]
    out, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            out.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        out.append(current)
    return out


# ────────────────────────────────────────────────────────────────────────────
# Bot
# ────────────────────────────────────────────────────────────────────────────

class Bot:
    def __init__(self, store: PollStore, user_index: UserIndex,
                 payments: PaymentLog, collector: str,
                 rate: float, max_yes_per_court: int):
        self.store = store
        self.user_index = user_index
        self.payments = payments
        self.collector = collector
        self.rate = rate
        self.max_yes_per_court = max_yes_per_court
        # In-memory map: poll_id → the user who issued /poll (payer for that poll)
        # Persisted in each record's "sender" field as well, but we keep this for
        # quick lookup when poll-creation message comes back from Telegram.
        self._pending_payer: dict[str, str] = {}

    def _mention(self, name: str) -> str:
        return mention(name, self.user_index)

    def _is_collector(self, user) -> bool:
        return display_name(user).lower() == self.collector.lower()

    def _balances_after_payments(self):
        """Apply recorded payments to build_collector_view rows.

        Returns (rows, totals) where:
          rows = [(person, action, amount)] sorted high→low
          totals = {"inflow": …, "outflow": …, "net": …}

        A "Pays {collector}" row gets the corresponding paid-total subtracted.
        If they overpaid, the row flips to "{collector} pays them" for the
        remainder.
        """
        polls = records_to_polls(self.store.all_records())
        base_rows, _ = build_collector_view(polls, self.collector)
        paid_by = self.payments.totals_by_debtor()

        adjusted: list[tuple[str, str, float]] = []
        for person, action, amount in base_rows:
            if action.startswith("Pays"):
                paid = paid_by.get(person, 0.0)
                net = amount - paid
                if net > 0.005:
                    adjusted.append((person, action, net))
                elif net < -0.005:
                    adjusted.append((person, f"{self.collector} pays them", -net))
                # else: settled, drop
            else:
                # "Collector pays them" — payments don't reduce this side
                adjusted.append((person, action, amount))

        inflow = sum(a for _, ac, a in adjusted if ac.startswith("Pays"))
        outflow = sum(a for _, ac, a in adjusted if not ac.startswith("Pays"))
        adjusted.sort(key=lambda r: -r[2])
        return adjusted, {"inflow": inflow, "outflow": outflow, "net": inflow - outflow}

    def limit_for(self, title: str) -> int:
        """Per-poll Yes cap, based on # of courts parsed from the title."""
        return self.max_yes_per_court * parse_courts(title)

    # ── Command handlers ──────────────────────────────────────────────────

    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🏸 *Badminton Share Bot*\n\n"
            "*Anyone*\n"
            "`/poll <title>` — I post a Yes/No poll; you become the payer.\n"
            "`/balance` — Your current net.\n"
            "`/summary` — Everyone's balance, settled-out.\n"
            "`/accounts` — Detailed table: charged / paid / covered / net.\n"
            "`/report` — Session-by-session list + xlsx attachment.\n"
            "`/rate` — Court rate and cap.\n"
            "`/polls` — Recent polls.\n"
            "`/help` — This message.\n\n"
            f"*Collector only ({self.collector})*\n"
            "`/paid <name> [amount]` — Record a cash payment received. "
            "Reply to their message or pass the name; omit amount to settle "
            "their full balance.\n"
            "`/credit <name> [amount]` — Add credit to their account "
            "(same math as `/paid`, just logged differently).\n"
            "`/reset` — Archive current month and start fresh.\n\n"
            "_Title tip:_ include `(Court N)` or `(Court N,M)` and a time "
            "range like `5:00-6:00 pm` so I can parse courts × hours.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_rate(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"Court rate: *{fmt_amount(self.rate)}* per court per hour.\n"
            "Total cost = courts × hours × rate.\n"
            "Split equally among the *first N Yes voters* (the rest are waitlisted).\n\n"
            f"Cap: *{self.max_yes_per_court} Yes voters per court*. Extra Yes "
            "votes go on a waitlist; if someone retracts, the next waitlister "
            "is promoted automatically.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_poll(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Title is everything after the /poll command.
        msg = update.effective_message
        text = msg.text or ""
        # Strip the leading "/poll" (and any @BotName suffix)
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await msg.reply_text(
                "Usage: `/poll Sunday June 7th (Court 3) - 12:00-1:00 pm`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        title = parts[1].strip()
        payer = self.user_index.record(msg.from_user)
        try:
            sent = await ctx.bot.send_poll(
                chat_id=msg.chat_id,
                question=title,
                options=["Yes", "No"],
                is_anonymous=False,
                allows_multiple_answers=False,
            )
        except Exception as e:
            await msg.reply_text(f"Couldn't create poll: {e}")
            return
        # Pre-create the store record using the original sender as the payer.
        # Each option carries an optional "waitlist" list for overflow voters.
        record = {
            "id": sent.poll.id,
            "message_id": sent.message_id,
            "date": sent.date.date().isoformat(),
            "title": title,
            "sender": payer,
            "chat_id": msg.chat_id,
            "chat_title": msg.chat.title,
            "options": [
                {"name": o.text, "voters": [], "waitlist": []}
                for o in sent.poll.options
            ],
        }
        self.store.upsert(record)
        # Post the live status message as a reply to the poll. It will be
        # edited as votes come in.
        limit = self.limit_for(title)
        intro = (f"💸 Payer: {self._mention(payer)} · cap *{limit}* Yes\n"
                 "_I'll edit this message as votes arrive._\n")
        record["status_intro"] = intro  # remembered so refreshes keep the header
        text = intro + "\n" + self._format_status(record)
        try:
            status_msg = await ctx.bot.send_message(
                chat_id=msg.chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_to_message_id=sent.message_id,
            )
            record["status_message_id"] = status_msg.message_id
            self.store.upsert(record)
        except Exception as e:
            log.warning("Could not post initial status: %s", e)

    async def cmd_balance(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        polls = records_to_polls(self.store.all_records())
        if not polls:
            await update.message.reply_text("No polls captured yet.")
            return
        rows, totals = self._balances_after_payments()
        me = self.user_index.record(update.message.from_user)
        if me.lower() == self.collector.lower():
            await update.message.reply_text(
                f"You're the collector.\n"
                f"To collect: {fmt_amount(totals['inflow'])}\n"
                f"To pay out: {fmt_amount(totals['outflow'])}\n"
                f"Net: *{fmt_amount(totals['net'])}*",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        match = next((r for r in rows if r[0].lower() == me.lower()), None)
        if not match:
            await update.message.reply_text(
                f"No outstanding balance for {me}. (Settled, or haven't voted "
                f"Yes on any sessions yet.)"
            )
            return
        person, action, amount = match
        await update.message.reply_text(
            f"*{person}*: {action} *{fmt_amount(amount)}*",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_summary(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        polls = records_to_polls(self.store.all_records())
        if not polls:
            await update.message.reply_text("No polls captured yet.")
            return
        rows, totals = self._balances_after_payments()
        if not rows:
            await update.message.reply_text("Everyone is settled. 🎉")
            return
        lines = [f"*Settlement — everyone settles with {self.collector}*\n"]
        for person, action, amount in rows:
            arrow = "→" if action.startswith("Pays") else "←"
            lines.append(f"{arrow} {self._mention(person)}: {fmt_amount(amount)}  _({action})_")
        lines.append("")
        lines.append(f"Collect: {fmt_amount(totals['inflow'])}")
        lines.append(f"Pay out: {fmt_amount(totals['outflow'])}")
        lines.append(f"Net: *{fmt_amount(totals['net'])}*")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def cmd_accounts(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        """List everyone in a table + month totals."""
        polls = records_to_polls(self.store.all_records())
        if not polls:
            await update.message.reply_text("No polls captured yet.")
            return

        from collections import defaultdict
        charged = defaultdict(float)
        paid_out_to = defaultdict(float)   # collector owes them (they fronted a session)
        sessions = 0
        total_cost = 0.0
        for poll in polls:
            _, _, cost = compute_cost(poll)
            n = len(poll.yes_voters)
            if not n:
                continue
            sessions += 1
            total_cost += cost
            share = cost / n
            for v in poll.yes_voters:
                charged[v] += share
            paid_out_to[poll.payer] += cost

        paid_in = self.payments.totals_by_debtor()
        people = sorted(set(list(charged.keys())
                            + list(paid_out_to.keys())
                            + list(paid_in.keys())))

        lines = [f"*Accounts — settles with {self.collector}*"]
        lines.append("`Name              Charged    Paid  Covered     Net`")
        outstanding = 0.0
        owed_back = 0.0
        for p in people:
            c = charged.get(p, 0.0)
            paid = paid_in.get(p, 0.0)
            covered = paid_out_to.get(p, 0.0)
            net = c - paid - covered  # +ve = they owe, -ve = they're owed
            if p.lower() == self.collector.lower():
                pass  # collector's own net is the inverse of everyone else's
            elif net > 0.005:
                outstanding += net
            elif net < -0.005:
                owed_back += -net
            lines.append(
                f"`{p[:16]:16s} "
                f"{fmt_amount(c):>8s}  "
                f"{fmt_amount(paid):>6s}  "
                f"{fmt_amount(covered):>7s}  "
                f"{fmt_amount(net):>7s}`"
            )
        # Totals block
        lines.append("")
        lines.append("*Totals*")
        lines.append(f"Sessions: *{sessions}*")
        lines.append(f"Total court cost: *{fmt_amount(total_cost)}*")
        lines.append(f"Collected so far: *{fmt_amount(sum(paid_in.values()))}*")
        lines.append(f"Outstanding (owed to {self.collector}): *{fmt_amount(outstanding)}*")
        if owed_back > 0.005:
            lines.append(f"{self.collector} owes others: *{fmt_amount(owed_back)}*")
        lines.append("")
        lines.append("_Charged_ = your share across all Yes votes.")
        lines.append("_Paid_ = what you've paid (cash or credit).")
        lines.append("_Covered_ = sessions you fronted the court for.")
        lines.append("_Net_ > 0: you owe.  Net < 0: you're owed.")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def _record_payment_command(self, update, kind: str):
        """Shared logic for /paid and /credit. `kind` controls the note tag
        and the wording of the confirmation message."""
        msg = update.effective_message
        if not self._is_collector(msg.from_user):
            await msg.reply_text(
                f"Only the collector ({self.collector}) can record payments."
            )
            return

        target_name: str | None = None
        if msg.reply_to_message and msg.reply_to_message.from_user:
            target_name = self.user_index.record(msg.reply_to_message.from_user)

        text = msg.text or ""
        parts = text.split(maxsplit=2)
        amount: float | None = None
        if not target_name and len(parts) >= 2:
            arg1 = parts[1].lstrip("@")
            target_name = self._find_user(arg1)
            if len(parts) >= 3:
                try:
                    amount = float(parts[2].lstrip("$").replace(",", ""))
                except ValueError:
                    pass
        elif target_name and len(parts) >= 2:
            try:
                amount = float(parts[1].lstrip("$").replace(",", ""))
            except ValueError:
                pass

        if not target_name:
            cmd = "paid" if kind == "paid" else "credit"
            await msg.reply_text(
                f"Usage: `/{cmd} <name> [amount]` — or reply to their message "
                f"with `/{cmd} [amount]`. Omit amount to settle their full "
                "current balance.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        if amount is None:
            rows, _ = self._balances_after_payments()
            match = next((r for r in rows
                          if r[0].lower() == target_name.lower()
                          and r[1].startswith("Pays")), None)
            if not match:
                await msg.reply_text(
                    f"{self._mention(target_name)} has no outstanding balance.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            amount = match[2]

        self.payments.add(
            debtor=target_name, amount=amount, note=kind,
            recorded_by=display_name(msg.from_user),
        )
        verb = "paid" if kind == "paid" else "credited"
        await msg.reply_text(
            f"✅ Recorded: {self._mention(target_name)} {verb} "
            f"*{fmt_amount(amount)}*. Run /balance to see updated numbers.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_paid(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        """Record a cash payment received from <name>."""
        await self._record_payment_command(update, kind="paid")

    async def cmd_credit(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        """Record a credit on <name>'s account (treated the same as a payment,
        just tagged 'credit' in the log)."""
        await self._record_payment_command(update, kind="credit")

    async def cmd_reset(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        """Collector-only: archive polls + payments to dated files, start fresh."""
        msg = update.effective_message
        if not self._is_collector(msg.from_user):
            await msg.reply_text(
                f"Only the collector ({self.collector}) can /reset."
            )
            return
        from datetime import date
        stamp = date.today().isoformat()
        archived = []
        for p in (self.store.path, self.payments.path):
            if p.exists():
                archive = p.with_name(f"{p.stem}-archived-{stamp}{p.suffix}")
                p.rename(archive)
                archived.append(archive.name)
        # Re-init in-memory state
        self.store._by_id.clear()
        self.payments._items.clear()
        # Touch empty files so next save works cleanly
        self.store._save()
        self.payments._save()
        await msg.reply_text(
            "🧹 *Reset complete.* Archived "
            + ", ".join(f"`{a}`" for a in archived) + " and started a fresh month. "
            "The user index is preserved.",
            parse_mode=ParseMode.MARKDOWN,
        )

    def _find_user(self, query: str) -> str | None:
        """Fuzzy-match a name against the user index. Case-insensitive,
        matches first name or full name prefix."""
        q = query.lower()
        for name in self.user_index._by_name.keys():
            if name.lower() == q:
                return name
        for name in self.user_index._by_name.keys():
            first = name.split()[0].lower() if name else ""
            if first == q:
                return name
        for name in self.user_index._by_name.keys():
            if q in name.lower():
                return name
        # Fall back: literal name we've never seen
        return query

    async def cmd_report(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Session-by-session attendance report + xlsx attachment."""
        msg = update.effective_message
        records = self.store.all_records()
        if not records:
            await msg.reply_text("No polls captured yet.")
            return

        polls = records_to_polls(records)

        # ── Text summary (one line per session) ───────────────────────────
        lines = [f"*Session report — {len(polls)} session(s)*\n"]
        grand_cost = 0.0
        for poll in polls:
            courts, hours, cost = compute_cost(poll)
            n = len(poll.yes_voters)
            share = (cost / n) if n else 0.0
            grand_cost += cost
            who = ", ".join(poll.yes_voters) if poll.yes_voters else "_(no yes)_"
            lines.append(
                f"`{poll.date}`  {poll.title[:46]}\n"
                f"  {fmt_amount(cost)} ÷ {n} = *{fmt_amount(share)}*  →  {who}"
            )
        lines.append("")
        lines.append(f"Total court cost: *{fmt_amount(grand_cost)}*")
        lines.append(f"Total sessions: *{len(polls)}*")
        text = "\n".join(lines)

        # Telegram caps a single message at ~4096 chars; chunk if needed.
        for chunk in _chunk(text, 4000):
            await msg.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)

        # ── Excel attachment ──────────────────────────────────────────────
        from datetime import date
        from tempfile import NamedTemporaryFile
        stamp = date.today().isoformat()
        try:
            with NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
                tmp_path = f.name
            write_report(polls, tmp_path, collector=self.collector)
            with open(tmp_path, "rb") as f:
                await ctx.bot.send_document(
                    chat_id=msg.chat_id,
                    document=f,
                    filename=f"badminton-report-{stamp}.xlsx",
                    caption=f"Full breakdown — {len(polls)} sessions, "
                            f"{fmt_amount(grand_cost)} total.",
                )
        except Exception as e:
            log.warning("Could not generate/send xlsx: %s", e)
            await msg.reply_text(f"(Couldn't attach the xlsx: {e})")

    async def cmd_polls(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        records = self.store.all_records()
        if not records:
            await update.message.reply_text("No polls captured yet.")
            return
        lines = ["*Recent polls*\n"]
        for r in records[-15:]:
            opts = r.get("options") or []
            yes_voters = opts[0].get("voters", []) if opts else []
            waitlist = opts[0].get("waitlist", []) if opts else []
            _, _, cost = compute_cost(Poll(
                date=r["date"], title=r["title"],
                yes_voters=yes_voters,
                payer=r.get("sender", ""),
            ))
            tail = f" (+{len(waitlist)} waitlist)" if waitlist else ""
            lines.append(
                f"`{r['date']}`  {r['title'][:48]}  {fmt_amount(cost)} ÷ "
                f"{len(yes_voters)} yes{tail}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    # ── Passive listeners (still useful to capture metadata) ──────────────

    async def on_poll_message(self, update: Update, _ctx):
        """A poll was posted in the chat (by anyone). Store its metadata.

        Note: if it wasn't sent by THIS bot, Telegram will not send us vote
        events. The user should use /poll instead.
        """
        msg = update.effective_message
        if not msg or not msg.poll:
            return
        existing = self.store.get(msg.poll.id)
        if existing:
            return  # already stored (likely from cmd_poll)
        sender_is_bot = msg.from_user and msg.from_user.is_bot
        record = {
            "id": msg.poll.id,
            "date": msg.date.date().isoformat(),
            "title": msg.poll.question,
            "sender": self.user_index.record(msg.from_user),
            "chat_id": msg.chat_id,
            "chat_title": msg.chat.title,
            "options": [
                {"name": o.text, "voters": [], "waitlist": []}
                for o in msg.poll.options
            ],
        }
        self.store.upsert(record)
        if not sender_is_bot:
            await msg.reply_text(
                "⚠️ I can capture this poll's question, but Telegram won't tell "
                "me who votes on it (bots only get vote updates for polls they "
                "sent themselves). To track votes, recreate this with `/poll "
                f"{msg.poll.question}`.",
                parse_mode=ParseMode.MARKDOWN,
            )

    async def on_poll_answer(self, update: Update, ctx):
        answer = update.poll_answer
        record = self.store.get(answer.poll_id)
        if record is None:
            log.warning("Vote on unknown poll %s", answer.poll_id)
            return
        name = self.user_index.record(answer.user)
        limit = self.limit_for(record["title"])

        for opt in record["options"]:
            opt.setdefault("voters", [])
            opt.setdefault("waitlist", [])

        promoted_name: str | None = None  # set if a waitlister moved into "in"

        # Step 1: remove this user from every option.
        for idx, opt in enumerate(record["options"]):
            is_yes = (idx == 0)
            if name in opt["voters"]:
                opt["voters"].remove(name)
                if is_yes and opt["waitlist"]:
                    promoted_name = opt["waitlist"].pop(0)
                    opt["voters"].append(promoted_name)
            if name in opt["waitlist"]:
                opt["waitlist"].remove(name)

        # Step 2: add user to their newly-chosen option(s).
        for idx in answer.option_ids:
            if not (0 <= idx < len(record["options"])):
                continue
            opt = record["options"][idx]
            if name in opt["voters"]:
                continue
            is_yes = (idx == 0)
            cap = limit if is_yes else float("inf")
            if len(opt["voters"]) < cap:
                opt["voters"].append(name)
            else:
                opt["waitlist"].append(name)

        self.store.upsert(record)
        log.info(
            "Vote: %s → %s on '%s' (in=%d waitlist=%d)",
            name,
            [record["options"][i]["name"] for i in answer.option_ids] or ["retract"],
            record["title"],
            len(record["options"][0]["voters"]) if record["options"] else 0,
            len(record["options"][0].get("waitlist", [])) if record["options"] else 0,
        )

        # Update the live status message under the poll.
        await self._refresh_status(ctx, record)

        # One small ping when someone is promoted off the waitlist — edits don't
        # re-notify, so we send a tiny standalone message to fire a notification.
        if promoted_name:
            try:
                await ctx.bot.send_message(
                    chat_id=record["chat_id"],
                    text=f"📣 {self._mention(promoted_name)} you got a spot!",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_to_message_id=record.get("message_id"),
                )
            except Exception as e:
                log.warning("Could not post promotion ping: %s", e)

    def _format_status(self, record: dict) -> str:
        yes = record["options"][0] if record["options"] else {}
        voters = yes.get("voters", [])
        waitlist = yes.get("waitlist", [])
        limit = self.limit_for(record["title"])
        lines = [f"*Status — {len(voters)}/{limit} in*"]
        if voters:
            lines.append("✅ " + ", ".join(self._mention(v) for v in voters))
        else:
            lines.append("✅ _(nobody yet)_")
        if waitlist:
            lines.append("⏳ " + ", ".join(self._mention(w) for w in waitlist))
        return "\n".join(lines)

    async def _refresh_status(self, ctx, record: dict):
        """Edit the poll's status message, or create it if not yet present."""
        chat_id = record.get("chat_id")
        if not chat_id:
            return
        intro = record.get("status_intro", "")
        body = self._format_status(record)
        text = (intro + "\n" + body) if intro else body
        status_id = record.get("status_message_id")
        try:
            if status_id:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id, message_id=status_id,
                    text=text, parse_mode=ParseMode.MARKDOWN,
                )
                return
        except Exception as e:
            log.info("Status edit failed (will resend): %s", e)
        # First time, or edit failed — send fresh and remember id.
        try:
            sent = await ctx.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_to_message_id=record.get("message_id"),
            )
            record["status_message_id"] = sent.message_id
            self.store.upsert(record)
        except Exception as e:
            log.warning("Could not send status message: %s", e)

    async def on_poll_state(self, update: Update, _ctx):
        """Poll closed / counts changed."""
        poll = update.poll
        record = self.store.get(poll.id)
        if record and poll.is_closed:
            record["closed"] = True
            self.store.upsert(record)


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def build_application(*, token: str, output: str = "polls.json",
                      collector: str = "Milad Momeni",
                      rate: float = RATE_PER_COURT_PER_HOUR,
                      max_yes_per_court: int = 6) -> Application:
    """Build a fully-wired python-telegram-bot Application.

    Used by both polling mode (`main()` below) and webhook mode (`wsgi.py`).
    Side effect: overrides `badminton_shares.RATE_PER_COURT_PER_HOUR` so the
    share calculator agrees with the bot's `--rate` flag.
    """
    if not token:
        raise RuntimeError("Missing bot token (set TELEGRAM_BOT_TOKEN).")

    import badminton_shares
    badminton_shares.RATE_PER_COURT_PER_HOUR = rate

    store = PollStore(Path(output))
    out = Path(output)
    user_index = UserIndex(out.with_name(".users.json"))
    payments = PaymentLog(out.with_name("payments.json"))
    bot = Bot(store, user_index=user_index, payments=payments,
              collector=collector, rate=rate,
              max_yes_per_court=max_yes_per_court)

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("help",     bot.cmd_help))
    app.add_handler(CommandHandler("start",    bot.cmd_help))
    app.add_handler(CommandHandler("rate",     bot.cmd_rate))
    app.add_handler(CommandHandler("poll",     bot.cmd_poll))
    app.add_handler(CommandHandler("balance",  bot.cmd_balance))
    app.add_handler(CommandHandler("summary",  bot.cmd_summary))
    app.add_handler(CommandHandler("accounts", bot.cmd_accounts))
    app.add_handler(CommandHandler("paid",     bot.cmd_paid))
    app.add_handler(CommandHandler("credit",   bot.cmd_credit))
    app.add_handler(CommandHandler("reset",    bot.cmd_reset))
    app.add_handler(CommandHandler("polls",    bot.cmd_polls))
    app.add_handler(CommandHandler("report",   bot.cmd_report))

    app.add_handler(MessageHandler(filters.POLL, bot.on_poll_message))
    app.add_handler(PollAnswerHandler(bot.on_poll_answer))
    app.add_handler(PollHandler(bot.on_poll_state))

    log.info(
        "Application built. Collector=%s  rate=$%.2f/court/hour  cap=%d Yes/court",
        collector, rate, max_yes_per_court,
    )
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", "-o", default="polls.json",
                        help="Path to write/read the JSON store (default polls.json)")
    parser.add_argument("--token", default=os.environ.get("TELEGRAM_BOT_TOKEN"),
                        help="Bot token (defaults to $TELEGRAM_BOT_TOKEN)")
    parser.add_argument("--collector", default=os.environ.get("BADMINTON_COLLECTOR", "Milad Momeni"),
                        help='Who collects from everyone (default "Milad Momeni")')
    parser.add_argument("--rate", type=float,
                        default=float(os.environ.get("BADMINTON_RATE", RATE_PER_COURT_PER_HOUR)),
                        help="Court rate per court per hour (default 25)")
    parser.add_argument("--max-yes-per-court", type=int,
                        default=int(os.environ.get("BADMINTON_MAX_PER_COURT", 6)),
                        help="Yes-voter cap per court. First (max × # courts) Yes "
                             "votes get a spot; further votes go on a waitlist and "
                             "are auto-promoted if someone retracts. Default 6.")
    args = parser.parse_args()

    if not args.token:
        sys.stderr.write("Missing bot token. Set $TELEGRAM_BOT_TOKEN or pass --token.\n")
        sys.exit(2)

    app = build_application(
        token=args.token, output=args.output,
        collector=args.collector, rate=args.rate,
        max_yes_per_court=args.max_yes_per_court,
    )
    log.info("Bot online (polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
