"""Polling engine: one BotRunner per bot (main + every active clone), managed by BotManager.

No webhooks: each runner long-polls getUpdates, so no public URL / HTTPS is needed.
Clone lifecycle (all without recreating the clone or losing its config):
    active ⇄ paused      pause_clone / resume_clone
    active → revoked     automatic on 401;  revoked → active  via update_token (hot-swap)
    banned owner         ban_user stops their runners, unban_user restarts them
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from datetime import datetime

from .reactions import pick_delay, react_to_update
from .report import ReportScheduler, get_tz
from .tg import TelegramAPI, TelegramError

log = logging.getLogger("engine")

SETTINGS_TTL = 30      # seconds a clone's settings are cached before re-reading MongoDB
STATS_FLUSH_EVERY = 60  # seconds between counter flushes to MongoDB
MAIN_UPDATES = ["message", "channel_post", "callback_query"]
CLONE_UPDATES = ["message", "channel_post"]


class BotRunner:
    def __init__(self, manager: "BotManager", kind: str, token: str, bot_id: int, username: str):
        self.manager = manager
        self.kind = kind  # "main" | "clone"
        self.bot_id = bot_id
        self.username = username
        self.api = TelegramAPI(token, manager.http)
        self.counters: Counter = Counter()
        self._task: asyncio.Task | None = None
        self._handlers: set[asyncio.Task] = set()
        self._settings: dict | None = None
        self._settings_at = 0.0

    # ── lifecycle ──
    def start(self, drop_pending: bool = False) -> None:
        self._task = asyncio.create_task(self._poll(drop_pending), name=f"poll-{self.username}")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._handlers) if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._handlers.clear()

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    # ── clone settings (always sourced from MongoDB, cached briefly) ──
    async def get_settings(self) -> dict:
        if self._settings is None or time.monotonic() - self._settings_at > SETTINGS_TTL:
            doc = await self.manager.db.get_clone(self.bot_id)
            if doc:
                self._settings = doc.get("settings") or {}
                self._settings_at = time.monotonic()
        cfg = self.manager.cfg
        s = self._settings or {}
        return {
            "custom_reactions": s.get("custom_reactions") or list(cfg.default_reactions),
            "reaction_delay": s.get("reaction_delay", cfg.default_delay),
            "random_delay": s.get("random_delay") or {},
        }

    def invalidate_settings(self) -> None:
        self._settings = None

    # ── polling loop ──
    async def _poll(self, drop_pending: bool) -> None:
        try:
            await self.api.delete_webhook(drop_pending)  # webhook and getUpdates can't coexist
        except TelegramError as e:
            if e.code == 401:
                return await self.manager.on_token_dead(self)
            log.warning("@%s deleteWebhook failed: %s", self.username, e.description)

        allowed = MAIN_UPDATES if self.kind == "main" else CLONE_UPDATES
        offset, backoff = 0, 1
        log.info("Polling started: @%s (%s)", self.username, self.kind)

        while True:
            try:
                updates = await self.api.get_updates(offset, 30, allowed)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                if e.code == 401:
                    return await self.manager.on_token_dead(self)
                if e.code == 409:  # same token polled elsewhere / webhook set
                    log.warning("@%s: 409 conflict — is another instance running this token?", self.username)
                    await asyncio.sleep(10)
                elif e.code == 429:
                    await asyncio.sleep((e.retry_after or 5) + 1)
                else:
                    log.warning("@%s getUpdates error: %s", self.username, e.description)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                t = asyncio.create_task(self._dispatch(update))  # slow handlers/delays never block polling
                self._handlers.add(t)
                t.add_done_callback(self._handlers.discard)

    async def _dispatch(self, update: dict) -> None:
        try:
            if self.kind == "main":
                await self.manager.main_bot.handle(self, update)
            else:
                await self.manager.handle_clone_update(self, update)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unhandled error while processing update for @%s", self.username)


class BotManager:
    def __init__(self, cfg, db, http, stop_event: asyncio.Event | None = None):
        from .main_bot import MainBot  # local import: main_bot depends on this module's types

        self.cfg = cfg
        self.db = db
        self.http = http
        self.stop_event = stop_event or asyncio.Event()
        self.runners: dict[int, BotRunner] = {}
        self.main_runner: BotRunner | None = None
        self.banned: set[int] = set()
        self.started_at = time.time()
        self.tz = get_tz(cfg.report_tz)
        self.main_bot = MainBot(cfg, db, self)
        self._bg: list[asyncio.Task] = []

    @property
    def main_username(self) -> str:
        return self.main_runner.username if self.main_runner else ""

    def is_banned(self, user_id: int) -> bool:
        return user_id in self.banned

    def should_run(self, doc: dict) -> bool:
        return doc.get("status", "active") == "active" and doc["user_id"] not in self.banned

    def today(self) -> str:
        return datetime.now(self.tz).date().isoformat()

    # ═══════════ startup / shutdown ═══════════
    async def start(self) -> None:
        api = TelegramAPI(self.cfg.bot_token, self.http)
        try:
            me = await api.get_me()
        except TelegramError as e:
            raise RuntimeError(f"BOT_TOKEN rejected by Telegram: {e.description}") from None

        self.main_runner = BotRunner(self, "main", self.cfg.bot_token, me["id"], me["username"])
        self.runners[me["id"]] = self.main_runner
        self.main_runner.start()

        self.banned = await self.db.banned_ids()
        count = skipped = 0
        for doc in await self.db.get_all_clones():
            if doc["bot_id"] == me["id"] or doc["bot_id"] in self.runners:
                continue
            if self.should_run(doc):
                self._spawn(doc)
                count += 1
            else:
                skipped += 1
        log.info("Main bot @%s running; %d clone(s) started, %d paused/revoked/banned skipped",
                 me["username"], count, skipped)

        self._bg.append(asyncio.create_task(self._flush_loop(), name="stats-flush"))
        if self.cfg.report_time:
            self._bg.append(asyncio.create_task(ReportScheduler(self).run(), name="daily-report"))

    async def stop_all(self) -> None:
        for t in self._bg:
            t.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        await asyncio.gather(*(r.stop() for r in list(self.runners.values())), return_exceptions=True)
        await self.flush_stats()
        self.runners.clear()

    # ═══════════ runner helpers ═══════════
    def _spawn(self, doc: dict, drop_pending: bool = False) -> BotRunner:
        runner = BotRunner(self, "clone", doc["bot_token"], doc["bot_id"], doc["bot_username"])
        self.runners[doc["bot_id"]] = runner
        runner.start(drop_pending)
        return runner

    async def _stop_runner(self, bot_id: int) -> None:
        runner = self.runners.get(bot_id)
        if not runner or runner is self.main_runner:
            return
        await runner.stop()
        await self._flush_runner(runner)
        self.runners.pop(bot_id, None)

    # ═══════════ clone lifecycle ═══════════
    async def add_clone(self, doc: dict) -> None:
        self._spawn(doc, drop_pending=True)  # a brand-new clone shouldn't react to old backlog

    async def remove_clone(self, bot_id: int) -> None:
        await self._stop_runner(bot_id)
        await self.db.delete_clone(bot_id)

    async def pause_clone(self, bot_id: int) -> None:
        """Stop polling but keep the clone (settings, stats, token) in MongoDB."""
        await self.db.set_clone_status(bot_id, "paused")
        await self._stop_runner(bot_id)

    async def resume_clone(self, bot_id: int) -> str | None:
        """Returns an error message, or None on success."""
        doc = await self.db.get_clone(bot_id)
        if not doc:
            return "Clone not found."
        if doc.get("status") == "revoked":
            return "The token was revoked — update the token first."
        if doc["user_id"] in self.banned:
            return "The owner of this clone is banned."
        await self.db.set_clone_status(bot_id, "active")
        if bot_id not in self.runners or not self.runners[bot_id].running:
            self.runners.pop(bot_id, None)
            doc["status"] = "active"
            self._spawn(doc, drop_pending=True)  # don't react to what piled up while paused
        return None

    async def update_token(self, bot_id: int, new_token: str) -> str:
        """Hot-swap the token of an existing clone. Returns the resulting status."""
        doc = await self.db.get_clone(bot_id)
        status = "paused" if doc.get("status") == "paused" else "active"
        await self.db.set_clone_token(bot_id, new_token, status)
        await self._stop_runner(bot_id)
        doc = await self.db.get_clone(bot_id)
        if self.should_run(doc):
            self._spawn(doc, drop_pending=True)
        return status

    async def ban_user(self, user_id: int, by: int) -> None:
        self.banned.add(user_id)
        await self.db.set_banned(user_id, True, by)
        for doc in await self.db.get_user_clones(user_id):
            await self._stop_runner(doc["bot_id"])

    async def unban_user(self, user_id: int) -> None:
        self.banned.discard(user_id)
        await self.db.set_banned(user_id, False)
        for doc in await self.db.get_user_clones(user_id):
            if self.should_run(doc) and doc["bot_id"] not in self.runners:
                self._spawn(doc, drop_pending=True)

    def invalidate_settings(self, bot_id: int) -> None:
        runner = self.runners.get(bot_id)
        if runner:
            runner.invalidate_settings()

    # ═══════════ update handling ═══════════
    async def handle_clone_update(self, runner: BotRunner, update: dict) -> None:
        msg = update.get("message")
        if msg and msg["chat"]["type"] == "private" and (msg.get("text") or "").startswith("/start"):
            main = f"@{self.main_username}" if self.main_username else "the main bot"
            await runner.api.send_message(
                msg["chat"]["id"],
                "👋 <b>Auto Reaction Bot</b>\n\nAdd me to your group or channel and I'll react to "
                f"every message.\n\n⚙️ Reactions and delay are managed from {main}.",
            )
            return
        s = await runner.get_settings()
        await react_to_update(runner.api, update, s["custom_reactions"], pick_delay(s), runner.counters)

    async def on_token_dead(self, runner: BotRunner) -> None:
        if runner.kind == "main":
            log.critical("Main BOT_TOKEN was rejected (401). Shutting down.")
            self.stop_event.set()
            return
        log.warning("Clone @%s: token revoked (401) — marked as revoked", runner.username)
        await self._flush_runner(runner)
        self.runners.pop(runner.bot_id, None)
        doc = await self.db.get_clone(runner.bot_id)
        if not doc:
            return
        await self.db.set_clone_status(runner.bot_id, "revoked")
        if self.main_runner:
            try:
                await self.main_runner.api.send_message(
                    doc["user_id"],
                    f"⚠️ The token of <b>@{runner.username}</b> was revoked, so it stopped.\n"
                    "Open ⚙️ Settings → your bot → 🔑 <b>Update Token</b> and send the new token. "
                    "Your settings are kept.",
                )
            except TelegramError:
                pass

    # ═══════════ stats ═══════════
    async def _flush_runner(self, runner: BotRunner) -> tuple[int, int, int]:
        c = runner.counters
        m, r, f = c["messages"], c["reactions"], c["failed"]
        if not (m or r or f):
            return 0, 0, 0
        c.clear()
        try:
            if runner.kind == "clone":
                await self.db.inc_clone_stats(runner.bot_id, m, r, f)
            await self.db.inc_daily(self.today(), m, r, f)
        except Exception:
            log.exception("stats flush failed; keeping counters")
            c.update(messages=m, reactions=r, failed=f)
            return 0, 0, 0
        return m, r, f

    async def flush_stats(self) -> None:
        for runner in list(self.runners.values()):
            await self._flush_runner(runner)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_FLUSH_EVERY)
            try:
                await self.flush_stats()
            except Exception:
                log.exception("stats flush loop error")
