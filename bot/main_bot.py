"""Main bot: /start, ⚙️ Settings (per-clone control panel), clone creation, admin entry points."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from .admin import ADMIN_ACTIONS, AdminPanel
from .links import add_links_row
from .pick import DELAY_PRESETS, RANGE_PRESETS  # noqa: F401  (re-exported constants)
from .reactions import MAX_REACTIONS, parse_reactions, pick_delay, react_to_update
from .tg import TelegramAPI, TelegramError
from .ui import PAGE_SIZE, STATUS_TEXT, btn, fmt_delay, fmt_dt, pager, show, url_btn

log = logging.getLogger("main_bot")

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{20,}$")
RANGE_RE = re.compile(r"^\s*(\d+)\s*[-–\s]\s*(\d+)\s*$")
PENDING_TTL = 600  # seconds a "send me X" prompt stays active

CLONE_ACTIONS = frozenset({
    "cb", "rx", "rxe", "rxr", "dl", "dls", "dlc", "rd", "rdt", "rds", "rdc",
    "pz", "tk", "del", "delc",
})


def parse_command(text: str, my_username: str) -> tuple[str | None, str, bool]:
    """-> (command, args, addressed_to_me). command is None when text isn't a command."""
    if not text.startswith("/"):
        return None, "", True
    head, _, args = text.partition(" ")
    cmd, _, target = head[1:].partition("@")
    mine = not target or target.lower() == my_username.lower()
    return cmd.lower(), args.strip(), mine


class MainBot:
    def __init__(self, cfg, db, manager):
        self.cfg = cfg
        self.db = db
        self.manager = manager
        self.admin = AdminPanel(cfg, db, manager, self)
        self.pending: dict[int, dict] = {}  # user_id -> {"kind", "bot_id", "ts"}

    # ══════════════ entry point ══════════════
    async def handle(self, runner, update: dict) -> None:
        if "callback_query" in update:
            await self.on_callback(runner, update["callback_query"])
            return
        msg = update.get("message")
        if msg and await self.on_message(runner, msg):
            return
        # Not a command / input for us → the main bot reacts like any other bot.
        await react_to_update(runner.api, update, self.cfg.default_reactions, 0, runner.counters)

    def _banned(self, uid: int | None) -> bool:
        return bool(uid) and self.manager.is_banned(uid) and not self.cfg.is_admin(uid)

    # ══════════════ messages ══════════════
    async def on_message(self, runner, msg: dict) -> bool:
        """Return True if the message was consumed (no reaction needed)."""
        api: TelegramAPI = runner.api
        chat, user = msg["chat"], msg.get("from") or {}
        uid, text = user.get("id"), msg.get("text") or ""
        private = chat["type"] == "private"
        cmd, args, mine = parse_command(text, runner.username)

        if private and self._banned(uid):
            await api.send_message(chat["id"], "🚫 You are banned from using this bot.")
            return True

        if cmd is not None:
            if not mine:
                return False
            if not private:  # in groups only the light commands work
                if cmd in ("start", "help", "ping"):
                    await self._group_reply(api, chat["id"], msg["message_id"], cmd, runner)
                    return True
                return False
            self.pending.pop(uid, None)  # a new command cancels any pending prompt
            return await self._private_command(runner, msg, cmd, args)

        if private and uid in self.pending:
            return await self._pending_input(runner, msg, text)
        return False

    async def _group_reply(self, api, chat_id, message_id, cmd, runner):
        if cmd == "ping":
            await api.send_message(chat_id, "🏓 Pong!", reply_to=message_id)
        else:
            await api.send_message(
                chat_id,
                "👋 I react to every message here automatically.\n"
                f"Make your own reaction bot: @{runner.username}",
                reply_to=message_id,
            )

    async def _private_command(self, runner, msg, cmd, args) -> bool:
        api, chat_id, user = runner.api, msg["chat"]["id"], msg.get("from") or {}
        uid = user["id"]

        if await self.admin.command(runner, msg, cmd, args):
            return True

        if cmd == "start":
            await self.db.upsert_user(uid, user.get("first_name") or "", user.get("username") or "")
            name = html.escape(user.get("first_name") or "there")
            await api.send_message(chat_id, self.home_text(name), self.kb_home(runner.username, self.cfg.is_admin(uid)))
        elif cmd == "help":
            await api.send_message(chat_id, self.help_text(self.cfg.is_admin(uid)), [[btn("⚙️ Settings", "sl:0")]])
        elif cmd == "settings":
            text, kb = await self.view_list(uid, 0)
            await api.send_message(chat_id, text, kb)
        elif cmd == "clone":
            await self._cmd_clone(runner, msg, args)
        elif cmd == "ping":
            t0 = time.monotonic()
            sent = await api.send_message(chat_id, "🏓 Pong!")
            ms = int((time.monotonic() - t0) * 1000)
            await api.edit_message_text(chat_id, sent["message_id"], f"🏓 Pong! <code>{ms} ms</code>")
        elif cmd == "stats":
            users, clones = await self.db.count_users(), await self.db.count_clones()
            await api.send_message(
                chat_id,
                f"📊 <b>Stats</b>\n\n👥 Users: <b>{users}</b>\n🤖 Clone bots: <b>{clones}</b>\n"
                f"🟢 Running bots: <b>{len(self.manager.runners)}</b>",
            )
        elif cmd == "broadcast":
            await self._cmd_broadcast(runner, msg)
        else:
            return False
        return True

    # ══════════════ clone creation / token update ══════════════
    async def _verify_token(self, token: str):
        """-> (me, error). Telegram getMe is the validity check."""
        token = token.strip()
        if not TOKEN_RE.match(token):
            return None, ("❌ That doesn't look like a bot token.\nIt looks like "
                          "<code>123456789:ABC-DEF...</code> — get it from @BotFather.")
        if token == self.cfg.bot_token:
            return None, "❌ That is the main bot's own token."
        try:
            me = await TelegramAPI(token, self.manager.http).get_me()
        except TelegramError:
            return None, "❌ Invalid token — Telegram rejected it. Check it in @BotFather and try again."
        if not me.get("is_bot"):
            return None, "❌ That token doesn't belong to a bot."
        return me, None

    async def create_clone(self, runner, user_id: int, token: str) -> tuple[bool, str, dict | None]:
        token = token.strip()
        admin = self.cfg.is_admin(user_id)
        if TOKEN_RE.match(token) and not admin and await self.db.count_user_clones(user_id) >= self.cfg.max_clones:
            return False, f"🚫 Clone limit reached ({self.cfg.max_clones}). Delete one in ⚙️ Settings first.", None

        me, err = await self._verify_token(token)
        if err:
            return False, err, None
        if await self.db.get_clone(me["id"]) or me["id"] == runner.bot_id:
            return False, f"⚠️ @{me['username']} is already added.", None

        doc = await self.db.add_clone(
            user_id, token, me["id"], me["username"],
            list(self.cfg.default_reactions), self.cfg.default_delay,
        )
        await self.manager.add_clone(doc)
        return True, (
            f"✅ <b>@{me['username']}</b> is live!\n\n"
            "Tap a button below to add it to your group/channel — admin rights are pre-selected.\n"
            "Customise it in ⚙️ Settings."
        ), doc

    async def update_token(self, user_id: int, bot_id: int, token: str) -> tuple[bool, str]:
        """Hot-swap: same bot (same ID), new token. Config/stats/data are untouched."""
        me, err = await self._verify_token(token)
        if err:
            return False, err
        if me["id"] != bot_id:
            return False, (f"❌ That token belongs to @{me['username']}, a different bot.\n"
                           "Send the new token of the <b>same</b> bot (BotFather → your bot → API Token).")
        status = await self.manager.update_token(bot_id, token.strip())
        if status == "paused":
            return True, "✅ Token updated. The clone is still ⏸ paused — resume it from its panel."
        return True, "✅ Token updated — the clone is running again with all its settings kept."

    async def _cmd_clone(self, runner, msg, args):
        api, chat_id, uid = runner.api, msg["chat"]["id"], msg["from"]["id"]
        if not args:
            self.pending[uid] = {"kind": "add_clone", "ts": time.time()}
            await api.send_message(chat_id, self.add_clone_prompt(), [[btn("🔙 Back", "sl:0")]])
            return
        await self._finish_clone(runner, msg, args)

    async def _delete_quietly(self, api, chat_id, message_id):
        try:  # don't leave the token sitting in the chat
            await api.delete_message(chat_id, message_id)
        except TelegramError:
            pass

    async def _finish_clone(self, runner, msg, token):
        api, chat_id, uid = runner.api, msg["chat"]["id"], msg["from"]["id"]
        self.pending.pop(uid, None)
        ok, text, doc = await self.create_clone(runner, uid, token)
        await self._delete_quietly(api, chat_id, msg["message_id"])
        kb = [add_links_row(doc["bot_username"], self.cfg)] if doc else []
        kb.append([btn("⚙️ Settings", "sl:0")])
        await api.send_message(chat_id, text, kb)

    # ══════════════ pending text input ══════════════
    async def _pending_input(self, runner, msg, text) -> bool:
        api, chat_id, uid = runner.api, msg["chat"]["id"], msg["from"]["id"]
        state = self.pending.get(uid)
        if not state or time.time() - state["ts"] > PENDING_TTL:
            self.pending.pop(uid, None)
            return False

        kind = state["kind"]
        if kind == "add_clone":
            await self._finish_clone(runner, msg, text)
            return True

        doc = await self._owned_doc(uid, state["bot_id"])
        if not doc:
            self.pending.pop(uid, None)
            await api.send_message(chat_id, "❌ That clone no longer exists.", [[btn("⚙️ Settings", "sl:0")]])
            return True
        bot_id = doc["bot_id"]
        back_to = {"reactions": f"rx:{bot_id}", "delay": f"dl:{bot_id}", "range": f"rd:{bot_id}",
                   "token_update": f"cb:{bot_id}"}[kind]
        back = [[btn("🔙 Back", back_to)]]

        if kind == "token_update":
            ok, result = await self.update_token(uid, bot_id, text)
            await self._delete_quietly(api, chat_id, msg["message_id"])
            if ok:
                self.pending.pop(uid, None)
            await api.send_message(chat_id, result, back)

        elif kind == "reactions":
            valid, invalid = parse_reactions(text)
            if invalid:
                await api.send_message(
                    chat_id, f"❌ Telegram doesn't allow these as reactions: {' '.join(invalid)}\nSend the list again.", back)
            elif not valid:
                await api.send_message(chat_id, "❌ I couldn't find any emoji. Send e.g. <code>👍 ❤ 🔥</code>", back)
            else:
                valid = valid[:MAX_REACTIONS]
                await self._save(bot_id, "custom_reactions", valid)
                self.pending.pop(uid, None)
                await api.send_message(chat_id, f"✅ Reactions updated: {' '.join(valid)}", back)

        elif kind == "delay":
            if not text.strip().isdigit() or int(text) > self.cfg.max_delay:
                await api.send_message(chat_id, f"❌ Send a whole number between 0 and {self.cfg.max_delay} (seconds).", back)
            else:
                await self._save(bot_id, "reaction_delay", int(text))
                self.pending.pop(uid, None)
                await api.send_message(chat_id, f"✅ Reaction delay set to <b>{fmt_delay(int(text))}</b>.", back)

        else:  # range: "min max" for human-like delay
            m = RANGE_RE.match(text)
            lo, hi = (int(m[1]), int(m[2])) if m else (-1, -1)
            if not m or lo > hi or hi > self.cfg.max_delay:
                await api.send_message(
                    chat_id, f"❌ Send two numbers, min then max, e.g. <code>2 60</code> (0 ≤ min ≤ max ≤ {self.cfg.max_delay}).", back)
            else:
                await self._save(bot_id, "random_delay", {"enabled": True, "min": lo, "max": hi})
                self.pending.pop(uid, None)
                await api.send_message(chat_id, f"✅ Human-like delay enabled: random <b>{lo}–{hi}s</b> per reaction.", back)
        return True

    async def _save(self, bot_id: int, key: str, value) -> None:
        await self.db.set_clone_setting(bot_id, key, value)
        self.manager.invalidate_settings(bot_id)

    # ══════════════ callbacks ══════════════
    async def on_callback(self, runner, cq: dict) -> None:
        api = runner.api
        uid, data = cq["from"]["id"], cq.get("data") or ""
        msg = cq.get("message")
        if not msg:
            await api.answer_callback(cq["id"])
            return
        chat_id, mid = msg["chat"]["id"], msg["message_id"]
        parts = data.split(":")
        action = parts[0]
        alert: str | None = None

        try:
            if self._banned(uid):
                alert = "🚫 You are banned."
            elif action in ADMIN_ACTIONS:
                alert = "⛔ Admins only." if not self.cfg.is_admin(uid) else await self.admin.callback(runner, cq, parts)
            elif action == "home":
                self.pending.pop(uid, None)
                name = html.escape(cq["from"].get("first_name") or "there")
                await show(api, chat_id, mid, self.home_text(name), self.kb_home(runner.username, self.cfg.is_admin(uid)))
            elif action == "help":
                await show(api, chat_id, mid, self.help_text(self.cfg.is_admin(uid)), [[btn("🔙 Back", "home")]])
            elif action == "sl":  # settings → clone list
                self.pending.pop(uid, None)
                await show(api, chat_id, mid, *await self.view_list(uid, int(parts[1])))
            elif action == "add":
                self.pending[uid] = {"kind": "add_clone", "ts": time.time()}
                await show(api, chat_id, mid, self.add_clone_prompt(), [[btn("🔙 Back", "sl:0")]])
            elif action in CLONE_ACTIONS:
                doc = await self._owned_doc(uid, int(parts[1]))
                if not doc:
                    alert = "This clone no longer exists."
                    await show(api, chat_id, mid, *await self.view_list(uid, 0))
                else:
                    alert = await self._clone_action(api, uid, chat_id, mid, action, doc, parts)
            else:
                alert = "Unknown action."
        except TelegramError as e:
            log.warning("callback error: %s", e.description)
        except (ValueError, IndexError):
            alert = "Invalid request."
        finally:
            try:
                await api.answer_callback(cq["id"], alert or "", bool(alert))
            except TelegramError:
                pass

    async def _panel(self, api, chat_id, mid, bot_id):
        doc = await self.db.get_clone(bot_id)
        await show(api, chat_id, mid, self.panel_text(doc), self.kb_panel(doc))

    async def _clone_action(self, api, uid, chat_id, mid, action, doc, parts) -> str | None:
        bot_id = doc["bot_id"]
        if action not in ("rxe", "dlc", "rdc", "tk"):
            self.pending.pop(uid, None)

        if action == "cb":
            await show(api, chat_id, mid, self.panel_text(doc), self.kb_panel(doc))

        # ── pause / resume ──
        elif action == "pz":
            status = doc.get("status", "active")
            if status == "revoked":
                return "Token revoked — tap 🔑 Update Token first."
            if status == "active":
                await self.manager.pause_clone(bot_id)
                alert = "Paused — it will not react until resumed."
            else:
                err = await self.manager.resume_clone(bot_id)
                if err:
                    return err
                alert = "Resumed."
            await self._panel(api, chat_id, mid, bot_id)
            return alert

        # ── token hot-swap ──
        elif action == "tk":
            self.pending[uid] = {"kind": "token_update", "bot_id": bot_id, "ts": time.time()}
            await show(api, chat_id, mid,
                       f"🔑 <b>Update token — @{doc['bot_username']}</b>\n\nSend the <b>new token of this same bot</b> "
                       "(BotFather → /mybots → your bot → API Token).\nSettings and stats are kept; "
                       "I'll delete your message after reading it.",
                       [[btn("🔙 Back", f"cb:{bot_id}")]])

        # ── reactions ──
        elif action == "rx":
            await show(api, chat_id, mid, self.reactions_text(doc), self.kb_reactions(bot_id))
        elif action == "rxe":
            self.pending[uid] = {"kind": "reactions", "bot_id": bot_id, "ts": time.time()}
            await show(api, chat_id, mid,
                       "✏️ <b>Send the new reaction list</b>\n\nExample: <code>👍 ❤ 🔥 🎉</code>\n"
                       f"Only Telegram's supported reaction emoji work. Max {MAX_REACTIONS}.",
                       [[btn("🔙 Back", f"rx:{bot_id}")]])
        elif action == "rxr":
            await self._save(bot_id, "custom_reactions", list(self.cfg.default_reactions))
            doc = await self.db.get_clone(bot_id)
            await show(api, chat_id, mid, self.reactions_text(doc), self.kb_reactions(bot_id))
            return "Reset to default reactions."

        # ── fixed delay ──
        elif action == "dl":
            await show(api, chat_id, mid, self.delay_text(doc), self.kb_delay(doc))
        elif action == "dls":
            secs = int(parts[2])
            if secs not in DELAY_PRESETS:
                return "Invalid value."
            await self._save(bot_id, "reaction_delay", secs)
            await self._save(bot_id, "random_delay", {**self._rd(doc), "enabled": False})  # fixed ≠ random
            doc = await self.db.get_clone(bot_id)
            await show(api, chat_id, mid, self.delay_text(doc), self.kb_delay(doc))
            return f"Fixed delay set to {fmt_delay(secs)}."
        elif action == "dlc":
            self.pending[uid] = {"kind": "delay", "bot_id": bot_id, "ts": time.time()}
            await show(api, chat_id, mid,
                       f"✏️ <b>Send the delay in seconds</b> (0–{self.cfg.max_delay}).\nExample: <code>7</code>",
                       [[btn("🔙 Back", f"dl:{bot_id}")]])

        # ── human-like random delay (anti-flood) ──
        elif action == "rd":
            await show(api, chat_id, mid, self.random_text(doc), self.kb_random(doc))
        elif action == "rdt":
            rd = self._rd(doc)
            await self._save(bot_id, "random_delay", {**rd, "enabled": not rd["enabled"]})
            doc = await self.db.get_clone(bot_id)
            await show(api, chat_id, mid, self.random_text(doc), self.kb_random(doc))
        elif action == "rds":
            lo, hi = int(parts[2]), int(parts[3])
            if (lo, hi) not in RANGE_PRESETS or hi > self.cfg.max_delay:
                return "Invalid range."
            await self._save(bot_id, "random_delay", {"enabled": True, "min": lo, "max": hi})
            doc = await self.db.get_clone(bot_id)
            await show(api, chat_id, mid, self.random_text(doc), self.kb_random(doc))
            return f"Human-like delay: {lo}–{hi}s."
        elif action == "rdc":
            self.pending[uid] = {"kind": "range", "bot_id": bot_id, "ts": time.time()}
            await show(api, chat_id, mid,
                       "✏️ <b>Send the range</b> as <code>min max</code> in seconds.\nExample: <code>2 60</code>",
                       [[btn("🔙 Back", f"rd:{bot_id}")]])

        # ── delete ──
        elif action == "del":
            await show(api, chat_id, mid,
                       f"🗑️ Delete <b>@{doc['bot_username']}</b>?\nIt will stop reacting immediately and its settings are lost.",
                       [[btn("✅ Yes, delete", f"delc:{bot_id}"), btn("❌ Cancel", f"cb:{bot_id}")]])
        elif action == "delc":
            await self.manager.remove_clone(bot_id)
            await show(api, chat_id, mid, *await self.view_list(uid, 0))
            return f"@{doc['bot_username']} deleted."
        return None

    # ══════════════ views ══════════════
    async def _owned_doc(self, uid: int, bot_id: int) -> dict | None:
        doc = await self.db.get_clone(bot_id)
        return doc if doc and doc["user_id"] == uid else None

    @staticmethod
    def _rd(doc: dict) -> dict:
        rd = (doc.get("settings") or {}).get("random_delay") or {}
        return {"enabled": bool(rd.get("enabled")), "min": rd.get("min", 2), "max": rd.get("max", 60)}

    def home_text(self, name: str) -> str:
        return (
            f"👋 Hello <b>{name}</b>!\n\n"
            "I'm an <b>Auto Reaction Bot</b> — add me to a group or channel and I'll react to every message.\n\n"
            "🤖 Want your own reaction bot? Create a clone with /clone, then set its emoji and "
            "delay in ⚙️ Settings."
        )

    def help_text(self, admin: bool = False) -> str:
        text = (
            "❓ <b>Help</b>\n\n"
            "/start — home\n/settings — manage your clone bots\n/clone — create a clone bot\n"
            "/ping — latency\n/stats — bot statistics\n\n"
            "<b>Make a clone</b>\n1. Create a bot in @BotFather and copy its token.\n"
            "2. Send /clone and paste the token.\n3. Tap ➕ Add to Group/Channel (admin rights pre-selected).\n"
            "4. Tune it in ⚙️ Settings → your bot: reactions, delay, ⏸ pause, 🔑 token update."
        )
        if admin:
            text += "\n\n<b>Admin</b>\n/admin · /users · /clones · /ban id · /unban id · /report · /broadcast"
        return text

    def add_clone_prompt(self) -> str:
        return (
            "➕ <b>Add a clone bot</b>\n\nSend the bot token from @BotFather "
            "(<code>123456789:ABC-DEF...</code>).\nI'll delete your message after reading it."
        )

    def kb_home(self, username: str, admin: bool = False) -> list:
        kb = [
            [btn("⚙️ Settings", "sl:0")],
            [url_btn("➕ Add to Group", f"https://t.me/{username}?startgroup=true"),
             url_btn("➕ Add to Channel", f"https://t.me/{username}?startchannel=true")],
            [btn("❓ Help", "help")],
        ]
        if admin:
            kb.append([btn("🛠 Admin Panel", "adm")])
        return kb

    async def view_list(self, uid: int, page: int) -> tuple[str, list]:
        clones = await self.db.get_user_clones(uid)
        limit = "unlimited" if self.cfg.is_admin(uid) else str(self.cfg.max_clones)
        page = max(0, min(page, max(0, (len(clones) - 1) // PAGE_SIZE)))
        chunk = clones[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

        text = f"⚙️ <b>Settings</b>\n\nYour clone bots: <b>{len(clones)}</b> / {limit}\n"
        text += "\nChoose a bot to manage:" if clones else "\nYou have no clone bots yet. Tap ➕ Add Clone to create one."

        icon = {"active": "🟢", "paused": "⏸", "revoked": "⚠️"}
        kb = [[btn(f"{icon.get(c.get('status', 'active'), '🟢')} @{c['bot_username']}", f"cb:{c['bot_id']}")] for c in chunk]
        nav = pager("sl", page, len(clones))
        if nav:
            kb.append(nav)
        kb.append([btn("➕ Add Clone", "add")])
        kb.append([btn("🔙 Back", "home")])
        return text, kb

    def panel_text(self, doc: dict) -> str:
        s, st = doc.get("settings") or {}, doc.get("stats") or {}
        rd = self._rd(doc)
        delay = f"🎲 random {fmt_delay(rd['min'])}–{fmt_delay(rd['max'])}" if rd["enabled"] else fmt_delay(s.get("reaction_delay", 0))
        status = doc.get("status", "active")
        return (
            f"🤖 <b>@{doc['bot_username']}</b> — Control Panel\n\n"
            f"📡 Status: <b>{STATUS_TEXT.get(status, status)}</b>\n"
            f"🎭 Reactions: {' '.join(s.get('custom_reactions') or []) or '—'}\n"
            f"⏱️ Delay: <b>{delay}</b>\n"
            f"📈 Reactions sent: <b>{st.get('reactions', 0)}</b>\n📅 Created: {fmt_dt(doc.get('created_at'))}"
        )

    def kb_panel(self, doc: dict) -> list:
        bot_id, status = doc["bot_id"], doc.get("status", "active")
        kb = [[btn("🎭 Custom Reactions", f"rx:{bot_id}")], [btn("⏱️ Reaction Time", f"dl:{bot_id}")]]
        if status == "active":
            kb.append([btn("⏸ Pause", f"pz:{bot_id}")])
        elif status == "paused":
            kb.append([btn("▶️ Resume", f"pz:{bot_id}")])
        kb.append([btn("🔑 Update Token", f"tk:{bot_id}")])
        kb.append(add_links_row(doc["bot_username"], self.cfg))
        kb.append([btn("🗑️ Delete Clone", f"del:{bot_id}")])
        kb.append([btn("🔙 Back", "sl:0")])
        return kb

    def reactions_text(self, doc: dict) -> str:
        reactions = " ".join((doc.get("settings") or {}).get("custom_reactions") or []) or "—"
        return (f"🎭 <b>Custom Reactions</b> — @{doc['bot_username']}\n\nCurrent: {reactions}\n\n"
                "The bot picks one of these at random for every message.")

    def kb_reactions(self, bot_id: int) -> list:
        return [
            [btn("✏️ Edit Reactions", f"rxe:{bot_id}")],
            [btn("♻️ Reset to Default", f"rxr:{bot_id}")],
            [btn("🔙 Back", f"cb:{bot_id}")],
        ]

    def delay_text(self, doc: dict) -> str:
        secs = (doc.get("settings") or {}).get("reaction_delay", 0)
        rd = self._rd(doc)
        mode = (f"🎲 Human-like <b>ON</b> ({rd['min']}–{rd['max']}s random)" if rd["enabled"]
                else f"Fixed: <b>{fmt_delay(secs)}</b>")
        return (f"⏱️ <b>Reaction Time</b> — @{doc['bot_username']}\n\n{mode}\n\n"
                "How long the bot waits before reacting. Use 🎲 Human-like delay to spread reactions out randomly.")

    def kb_delay(self, doc: dict) -> list:
        bot_id = doc["bot_id"]
        cur = (doc.get("settings") or {}).get("reaction_delay", 0)
        fixed_on = not self._rd(doc)["enabled"]
        presets = [btn(("✅ " if fixed_on and s == cur else "") + f"{s}s", f"dls:{bot_id}:{s}") for s in DELAY_PRESETS]
        return [presets, [btn("✏️ Custom", f"dlc:{bot_id}")],
                [btn(f"🎲 Human-like delay: {'ON' if self._rd(doc)['enabled'] else 'OFF'}", f"rd:{bot_id}")],
                [btn("🔙 Back", f"cb:{bot_id}")]]

    def random_text(self, doc: dict) -> str:
        rd = self._rd(doc)
        return (f"🎲 <b>Human-like delay</b> — @{doc['bot_username']}\n\n"
                f"Status: <b>{'ON' if rd['enabled'] else 'OFF'}</b> · range {rd['min']}–{rd['max']}s\n\n"
                "Every reaction waits a <b>different random time</b> in this range. This avoids bursts, "
                "Telegram rate limits (429) and makes activity look natural.")

    def kb_random(self, doc: dict) -> list:
        bot_id, rd = doc["bot_id"], self._rd(doc)
        rows = [[btn(f"{'🔴 Turn OFF' if rd['enabled'] else '🟢 Turn ON'}", f"rdt:{bot_id}")]]
        rows.append([btn(("✅ " if (rd['enabled'] and (rd['min'], rd['max']) == r) else "") + f"{r[0]}–{r[1]}s",
                         f"rds:{bot_id}:{r[0]}:{r[1]}") for r in RANGE_PRESETS])
        rows.append([btn("✏️ Custom range", f"rdc:{bot_id}")])
        rows.append([btn("🔙 Back", f"dl:{bot_id}")])
        return rows

    # ══════════════ owner tools ══════════════
    async def _cmd_broadcast(self, runner, msg) -> None:
        api, chat_id, uid = runner.api, msg["chat"]["id"], msg["from"]["id"]
        if not self.cfg.is_admin(uid):
            return
        target = msg.get("reply_to_message")
        if not target:
            await api.send_message(chat_id, "Reply to the message you want to broadcast with /broadcast.")
            return
        ids = await self.db.all_user_ids()
        await api.send_message(chat_id, f"📢 Broadcasting to {len(ids)} users…")
        ok = blocked = failed = 0
        for user_id in ids:
            try:
                await api.copy_message(user_id, chat_id, target["message_id"])
                ok += 1
            except TelegramError as e:
                if e.code in (400, 403):
                    blocked += 1
                    await self.db.remove_user(user_id)
                else:
                    failed += 1
            await asyncio.sleep(0.05)  # ~20 msg/s, under Telegram's limit
        await api.send_message(chat_id, f"✅ Done.\nSent: {ok}\nBlocked/removed: {blocked}\nFailed: {failed}")
