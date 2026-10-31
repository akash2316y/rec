"""Admin panel (inside the main bot): /admin /users /clones /ban /unban /report.

Everything here is gated by cfg.is_admin(); callbacks start with 'a' (see ADMIN_ACTIONS).
"""
from __future__ import annotations

import html
import logging

from .links import add_links_row
from .report import build_report
from .tg import TelegramError
from .ui import PAGE_SIZE, STATUS_ICON, STATUS_TEXT, btn, fmt_delay, fmt_dt, pager, show

log = logging.getLogger("admin")

ADMIN_ACTIONS = frozenset({"adm", "au", "aud", "aub", "auu", "acl", "acd", "acp", "acx", "acy", "arp"})


def _name(u: dict) -> str:
    return html.escape(u.get("first_name") or "") or "(no name)"


class AdminPanel:
    def __init__(self, cfg, db, manager, bot):
        self.cfg, self.db, self.manager, self.bot = cfg, db, manager, bot

    # ═══════════ commands ═══════════
    async def command(self, runner, msg: dict, cmd: str, args: str) -> bool:
        """Handle an admin command. Returns False if `cmd` isn't an admin command."""
        if cmd not in ("admin", "users", "clones", "ban", "unban", "report"):
            return False
        api, chat_id, uid = runner.api, msg["chat"]["id"], msg["from"]["id"]
        if not self.cfg.is_admin(uid):
            await api.send_message(chat_id, "⛔ Admins only.")
            return True

        if cmd == "admin":
            text, kb = await self.view_home()
        elif cmd == "users":
            text, kb = await self.view_users(0)
        elif cmd == "clones":
            text, kb = await self.view_clones(0, 0)
        elif cmd == "report":
            await api.send_message(chat_id, await build_report(self.manager, "today"))
            return True
        else:  # ban / unban
            parts = args.split(None, 1)
            if not parts or not parts[0].lstrip("-").isdigit():
                await api.send_message(chat_id, f"Usage: <code>/{cmd} &lt;user_id&gt;</code>")
                return True
            text = await self.set_ban(runner, uid, int(parts[0]), cmd == "ban")
            kb = [[btn("👥 Users", "au:0")]]
        await api.send_message(chat_id, text, kb)
        return True

    async def set_ban(self, runner, admin_id: int, target: int, ban: bool) -> str:
        if ban:
            if self.cfg.is_admin(target):
                return "⛔ You can't ban an owner/admin."
            await self.manager.ban_user(target, admin_id)
            note = "🚫 You have been banned from using this bot."
            result = f"🚫 User <code>{target}</code> banned. Their clones were stopped."
        else:
            await self.manager.unban_user(target)
            note = "✅ You have been unbanned. You can use the bot again."
            result = f"✅ User <code>{target}</code> unbanned. Their active clones were restarted."
        try:
            await runner.api.send_message(target, note)
        except TelegramError:
            pass
        return result

    # ═══════════ callbacks ═══════════
    async def callback(self, runner, cq: dict, parts: list[str]) -> str | None:
        """Returns an alert text (or None)."""
        api, uid = runner.api, cq["from"]["id"]
        chat_id, mid = cq["message"]["chat"]["id"], cq["message"]["message_id"]
        action = parts[0]

        if action == "adm":
            await show(api, chat_id, mid, *await self.view_home())
        elif action == "au":
            await show(api, chat_id, mid, *await self.view_users(int(parts[1])))
        elif action == "aud":
            await show(api, chat_id, mid, *await self.view_user(int(parts[1])))
        elif action in ("aub", "auu"):
            target = int(parts[1])
            result = await self.set_ban(runner, uid, target, action == "aub")
            await show(api, chat_id, mid, *await self.view_user(target))
            return result.replace("<code>", "").replace("</code>", "")[:190]
        elif action == "acl":
            await show(api, chat_id, mid, *await self.view_clones(int(parts[1]), int(parts[2])))
        elif action == "acd":
            await show(api, chat_id, mid, *await self.view_clone(int(parts[1])))
        elif action == "acp":  # pause / resume any clone
            bot_id = int(parts[1])
            doc = await self.db.get_clone(bot_id)
            if not doc:
                return "Clone not found."
            if doc.get("status") == "active":
                await self.manager.pause_clone(bot_id)
                alert = "Paused."
            else:
                err = await self.manager.resume_clone(bot_id)
                if err:
                    return err
                alert = "Resumed."
            await show(api, chat_id, mid, *await self.view_clone(bot_id))
            return alert
        elif action == "acx":  # force-delete: ask first
            doc = await self.db.get_clone(int(parts[1]))
            if not doc:
                return "Clone not found."
            await show(api, chat_id, mid,
                       f"🗑️ <b>Force-delete @{doc['bot_username']}?</b>\n\nOwner: <code>{doc['user_id']}</code>\n"
                       "The clone is stopped and removed from the database. The owner is notified.",
                       [[btn("✅ Yes, delete", f"acy:{doc['bot_id']}"), btn("❌ Cancel", f"acd:{doc['bot_id']}")]])
        elif action == "acy":
            return await self._force_delete(runner, chat_id, mid, int(parts[1]))
        elif action == "arp":
            await api.send_message(chat_id, await build_report(self.manager, "today"))
            return "Report sent."
        return None

    async def _force_delete(self, runner, chat_id, mid, bot_id: int) -> str:
        doc = await self.db.get_clone(bot_id)
        if doc:
            await self.manager.remove_clone(bot_id)
            try:
                await runner.api.send_message(
                    doc["user_id"], f"🗑️ Your clone <b>@{doc['bot_username']}</b> was removed by an admin.")
            except TelegramError:
                pass
        await show(runner.api, chat_id, mid, *await self.view_clones(0, 0))
        return "Clone deleted."

    # ═══════════ views ═══════════
    async def view_home(self) -> tuple[str, list]:
        users, banned = await self.db.count_users(), await self.db.count_banned()
        total = await self.db.count_clones()
        paused = await self.db.count_clones(status="paused")
        revoked = await self.db.count_clones(status="revoked")
        text = (f"🛠 <b>Admin Panel</b>\n\n👥 Users: <b>{users}</b> (🚫 {banned} banned)\n"
                f"🤖 Clones: <b>{total}</b> (⏸ {paused} paused · ⚠️ {revoked} revoked)\n"
                f"🟢 Running bots: <b>{len(self.manager.runners)}</b>\n\n"
                "Commands: /users · /clones · /ban &lt;id&gt; · /unban &lt;id&gt; · /report")
        return text, [[btn("👥 Users", "au:0"), btn("🤖 Clones", "acl:0:0")], [btn("📊 Report now", "arp")]]

    async def view_users(self, page: int) -> tuple[str, list]:
        total = await self.db.count_users()
        page = max(0, min(page, max(0, (total - 1) // PAGE_SIZE)))
        users = await self.db.list_users(page * PAGE_SIZE, PAGE_SIZE)
        kb = [[btn(f"{'🚫' if u.get('banned') else '✅'} {_name(u)[:22]} · {u['user_id']}", f"aud:{u['user_id']}")]
              for u in users]
        nav = pager("au", page, total)
        if nav:
            kb.append(nav)
        kb.append([btn("🔙 Back", "adm")])
        return f"👥 <b>Users</b> — {total} total (page {page + 1})\n\nTap a user for details:", kb

    async def view_user(self, uid: int) -> tuple[str, list]:
        u = await self.db.get_user(uid)
        if not u:
            return "User not found.", [[btn("🔙 Back", "au:0")]]
        n = await self.db.count_clones(owner=uid)
        banned = bool(u.get("banned"))
        uname = f"@{u['username']}" if u.get("username") else "—"
        text = (f"👤 <b>{_name(u)}</b>\n\nID: <code>{uid}</code>\nUsername: {uname}\n"
                f"Status: {'🚫 Banned' if banned else '✅ Active'}\nClones: <b>{n}</b>\n"
                f"Joined: {fmt_dt(u.get('joined_at'))}\nLast seen: {fmt_dt(u.get('last_seen'))}")
        kb = []
        if not self.cfg.is_admin(uid):
            kb.append([btn("✅ Unban", f"auu:{uid}") if banned else btn("🚫 Ban", f"aub:{uid}")])
        if n:
            kb.append([btn(f"🤖 Clones ({n})", f"acl:0:{uid}")])
        kb.append([btn("🔙 Back", "au:0")])
        return text, kb

    async def view_clones(self, page: int, owner: int) -> tuple[str, list]:
        total = await self.db.count_clones(owner=owner or None)
        page = max(0, min(page, max(0, (total - 1) // PAGE_SIZE)))
        clones = await self.db.list_clones(owner or None, page * PAGE_SIZE, PAGE_SIZE)
        kb = [[btn(f"{STATUS_ICON.get(c.get('status', 'active'), '🟢')} @{c['bot_username']} · {c['user_id']}",
                   f"acd:{c['bot_id']}")] for c in clones]
        nav = pager("acl", page, total, f":{owner}")
        if nav:
            kb.append(nav)
        kb.append([btn("🔙 Back", f"aud:{owner}" if owner else "adm")])
        scope = f" of <code>{owner}</code>" if owner else ""
        return f"🤖 <b>Clones</b>{scope} — {total} total (page {page + 1})\n🟢 active · ⏸ paused · ⚠️ revoked", kb

    async def view_clone(self, bot_id: int) -> tuple[str, list]:
        c = await self.db.get_clone(bot_id)
        if not c:
            return "Clone not found.", [[btn("🔙 Back", "acl:0:0")]]
        s, st = c.get("settings") or {}, c.get("stats") or {}
        status = c.get("status", "active")
        rd = s.get("random_delay") or {}
        delay = (f"🎲 {fmt_delay(rd.get('min', 2))}–{fmt_delay(rd.get('max', 60))}" if rd.get("enabled")
                 else fmt_delay(s.get("reaction_delay", 0)))
        running = bot_id in self.manager.runners and self.manager.runners[bot_id].running
        text = (f"🤖 <b>@{c['bot_username']}</b>\n\nOwner: <code>{c['user_id']}</code>\n"
                f"Status: {STATUS_TEXT.get(status, status)}{' (polling)' if running else ''}\n"
                f"🎭 {' '.join(s.get('custom_reactions') or []) or '—'}\n⏱️ Delay: {delay}\n"
                f"📈 Messages: {st.get('messages', 0)} · Reactions: {st.get('reactions', 0)} · Failed: {st.get('failed', 0)}\n"
                f"📅 Created: {fmt_dt(c.get('created_at'))}")
        kb = []
        if status == "active":
            kb.append([btn("⏸ Pause", f"acp:{bot_id}")])
        elif status == "paused":
            kb.append([btn("▶️ Resume", f"acp:{bot_id}")])
        kb.append([btn("🗑️ Force Delete", f"acx:{bot_id}")])
        kb.append(add_links_row(c["bot_username"], self.cfg))
        kb.append([btn("👤 Owner", f"aud:{c['user_id']}"), btn("🔙 Back", "acl:0:0")])
        return text, kb
