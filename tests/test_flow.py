"""Logic tests with in-memory fakes (no MongoDB / network needed).  Run: python -m unittest -v"""
import asyncio
import os
import sys
import time
import types
import unittest
from collections import Counter
from datetime import datetime, timezone

# Let the tests run even if httpx/motor/dotenv aren't installed.
for name, attrs in {
    "httpx": {"AsyncClient": object, "Limits": object, "Timeout": object, "HTTPError": Exception},
    "motor": {}, "motor.motor_asyncio": {"AsyncIOMotorClient": object},
    "dotenv": {"load_dotenv": lambda *a, **k: None},
}.items():
    try:
        __import__(name)
    except ImportError:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import Config, _report_time, _rights  # noqa: E402
from bot.engine import BotManager, BotRunner  # noqa: E402
from bot.links import add_links_row  # noqa: E402
from bot.main_bot import parse_command  # noqa: E402
from bot.reactions import parse_reactions, pick_delay, react_to_update  # noqa: E402
from bot.report import build_report, get_tz, seconds_until  # noqa: E402
from bot.tg import TelegramError  # noqa: E402

OWNER, USER, OTHER = 1, 2, 3
MAIN_TOKEN = "111111:MAINTOKENMAINTOKENMAINTOKEN"


def now(): return datetime.now(timezone.utc)


class FakeDB:
    def __init__(self):
        self.clones, self.users, self.daily = {}, {}, {}

    async def ping(self): return 1.0
    async def add_clone(self, user_id, token, bot_id, username, reactions, delay):
        doc = {"user_id": user_id, "bot_token": token, "bot_id": bot_id, "bot_username": username,
               "created_at": now(), "status": "active", "stats": {"messages": 0, "reactions": 0, "failed": 0},
               "settings": {"custom_reactions": list(reactions), "reaction_delay": delay,
                            "random_delay": {"enabled": False, "min": 2, "max": 60}}}
        self.clones[bot_id] = doc
        return doc
    async def get_clone(self, bot_id): return self.clones.get(bot_id)
    async def get_user_clones(self, uid): return [c for c in self.clones.values() if c["user_id"] == uid]
    async def get_all_clones(self): return list(self.clones.values())
    async def count_user_clones(self, uid): return len(await self.get_user_clones(uid))
    async def list_clones(self, owner=None, skip=0, limit=8):
        return [c for c in self.clones.values() if not owner or c["user_id"] == owner][skip:skip + limit]
    async def count_clones(self, owner=None, status=None):
        return len([c for c in self.clones.values()
                    if (not owner or c["user_id"] == owner) and (not status or c["status"] == status)])
    async def set_clone_setting(self, bot_id, key, value): self.clones[bot_id]["settings"][key] = value
    async def set_clone_status(self, bot_id, status): self.clones[bot_id]["status"] = status
    async def set_clone_token(self, bot_id, token, status=None):
        self.clones[bot_id]["bot_token"] = token
        if status: self.clones[bot_id]["status"] = status
    async def inc_clone_stats(self, bot_id, m, r, f):
        for k, v in (("messages", m), ("reactions", r), ("failed", f)): self.clones[bot_id]["stats"][k] += v
    async def delete_clone(self, bot_id): self.clones.pop(bot_id, None)
    async def upsert_user(self, uid, first_name="", username=""):
        self.users.setdefault(uid, {"user_id": uid, "joined_at": now(), "banned": False}).update(
            first_name=first_name, username=username, last_seen=now())
    async def get_user(self, uid): return self.users.get(uid)
    async def list_users(self, skip=0, limit=8): return list(self.users.values())[skip:skip + limit]
    async def count_users(self): return len(self.users)
    async def count_banned(self): return len([u for u in self.users.values() if u.get("banned")])
    async def set_banned(self, uid, banned, by=None):
        self.users.setdefault(uid, {"user_id": uid, "joined_at": now(), "first_name": "", "username": ""})["banned"] = banned
    async def banned_ids(self): return {u for u, d in self.users.items() if d.get("banned")}
    async def all_user_ids(self): return [u for u, d in self.users.items() if not d.get("banned")]
    async def remove_user(self, uid): self.users.pop(uid, None)
    async def inc_daily(self, date, m, r, f):
        d = self.daily.setdefault(date, {"messages": 0, "reactions": 0, "failed": 0})
        d["messages"] += m; d["reactions"] += r; d["failed"] += f
    async def get_daily(self, date): return self.daily.get(date, {"messages": 0, "reactions": 0, "failed": 0})


class FakeAPI:
    def __init__(self): self.calls = []
    def __getattr__(self, name):
        async def f(*a, **k):
            self.calls.append((name, a, k))
            return {"message_id": 99} if name == "send_message" else {}
        return f
    def last(self, name): return [c for c in self.calls if c[0] == name][-1]
    def all(self, name): return [c for c in self.calls if c[0] == name]


def make_cfg(**kw):
    base = dict(bot_token=MAIN_TOKEN, owner_id=OWNER, mongo_uri="x", db_name="x",
                default_reactions=("👍", "❤"), admin_ids=frozenset(), max_clones=5, default_delay=0, max_delay=300)
    base.update(kw)
    return Config(**base)


class Base(unittest.IsolatedAsyncioTestCase):
    """Real BotManager + MainBot wired to FakeDB; polling is stubbed out (no network)."""

    async def asyncSetUp(self):
        import bot.engine as eng
        import bot.main_bot as mb
        self.db = FakeDB()
        self.mgr = BotManager(make_cfg(), self.db, None)
        self.runner = BotRunner(self.mgr, "main", MAIN_TOKEN, 111, "MainBot")
        self.runner.api = FakeAPI()
        self.mgr.main_runner = self.runner
        self.mgr.runners[111] = self.runner
        self.bot = self.mgr.main_bot
        self.next_bot = 1000
        self.started = []  # (bot_id, drop_pending) of every spawned clone runner

        # stub: no network
        self._patches = []
        def patch(obj, attr, val):
            self._patches.append((obj, attr, getattr(obj, attr))); setattr(obj, attr, val)
        async def fake_get_me(this):
            if "BAD" in this._base: raise TelegramError("getMe", 401, "Unauthorized")
            if this._base.startswith("777777"): return {"id": 777, "is_bot": True, "username": "Revoked"}
            self.next_bot += 1
            return {"id": self.next_bot, "is_bot": True, "username": f"Clone{self.next_bot}"}
        patch(mb.TelegramAPI, "get_me", fake_get_me)
        patch(mb.TelegramAPI, "__init__", lambda s, token, client: setattr(s, "_base", token))
        def fake_start(r, drop_pending=False):
            self.started.append((r.bot_id, drop_pending))
            r._task = asyncio.get_running_loop().create_future()  # looks "running"
        async def fake_stop(r):
            r._task = None
        patch(BotRunner, "start", fake_start)
        patch(BotRunner, "stop", fake_stop)
        patch(BotRunner, "running", property(lambda r: r._task is not None))

    async def asyncTearDown(self):
        for obj, attr, orig in reversed(self._patches):
            setattr(obj, attr, orig)

    def token(self, n): return f"{200000 + n}:ABCDEFGHIJKLMNOPQRSTUVWXYZ{n}"

    def msg(self, uid, text, chat_type="private"):
        return {"message_id": 5, "chat": {"id": uid, "type": chat_type},
                "from": {"id": uid, "first_name": "Tester", "username": "tester"}, "text": text}

    def cb(self, uid, data):
        return {"callback_query": {"id": "c", "from": {"id": uid, "first_name": "T"}, "data": data,
                                   "message": {"message_id": 7, "chat": {"id": uid}}}}

    async def send(self, uid, text): await self.bot.handle(self.runner, {"message": self.msg(uid, text)})
    async def tap(self, uid, data): await self.bot.handle(self.runner, self.cb(uid, data))

    async def make_clone(self, uid=USER, n=1):
        ok, _, doc = await self.bot.create_clone(self.runner, uid, self.token(n))
        assert ok
        return doc["bot_id"]

    def labels(self, kb): return [b["text"] for row in kb for b in row]
    def last_kb(self): return self.runner.api.last("edit_message_text")[1][3]
    def alert(self): return self.runner.api.last("answer_callback")[1][1]


# ═══════════════ clone creation rules ═══════════════
class CloneRules(Base):
    async def test_normal_user_capped_at_5(self):
        for i in range(5): await self.make_clone(USER, i)
        ok, text, _ = await self.bot.create_clone(self.runner, USER, self.token(99))
        self.assertFalse(ok); self.assertIn("limit", text.lower())
        self.assertEqual(await self.db.count_user_clones(USER), 5)

    async def test_owner_unlimited(self):
        for i in range(8): await self.make_clone(OWNER, i)
        self.assertEqual(await self.db.count_user_clones(OWNER), 8)

    async def test_invalid_token_rejected(self):
        self.assertFalse((await self.bot.create_clone(self.runner, USER, "not a token"))[0])
        ok, text, _ = await self.bot.create_clone(self.runner, USER, "123456:BADBADBADBADBADBADBADBADBAD")
        self.assertFalse(ok); self.assertIn("Invalid", text); self.assertEqual(len(self.db.clones), 0)

    async def test_schema(self):
        await self.make_clone()
        doc = next(iter(self.db.clones.values()))
        self.assertEqual(set(doc), {"user_id", "bot_token", "bot_id", "bot_username", "created_at", "status", "stats", "settings"})
        self.assertEqual(doc["status"], "active")
        self.assertEqual(doc["settings"]["custom_reactions"], ["👍", "❤"])
        self.assertIn(doc["bot_id"], [b for b, _ in self.started])


# ═══════════════ settings menu ═══════════════
class SettingsMenu(Base):
    async def test_start_has_settings_button(self):
        await self.send(USER, "/start")
        self.assertIn("⚙️ Settings", self.labels(self.runner.api.last("send_message")[1][2]))

    async def test_panel_buttons_and_isolation(self):
        bid = await self.make_clone()
        await self.tap(USER, "sl:0")
        self.assertIn(f"🟢 @Clone{bid}", self.labels(self.last_kb()))
        await self.tap(USER, f"cb:{bid}")
        labels = self.labels(self.last_kb())
        for want in ("🎭 Custom Reactions", "⏱️ Reaction Time", "⏸ Pause", "🔑 Update Token", "🗑️ Delete Clone", "🔙 Back"):
            self.assertIn(want, labels)
        await self.tap(OTHER, f"delc:{bid}")  # not the owner
        self.assertIn(bid, self.db.clones)

    async def test_fixed_delay_and_custom(self):
        bid = await self.make_clone()
        await self.tap(USER, f"dls:{bid}:5")
        self.assertEqual(self.db.clones[bid]["settings"]["reaction_delay"], 5)
        await self.tap(USER, f"dlc:{bid}"); await self.send(USER, "42")
        self.assertEqual(self.db.clones[bid]["settings"]["reaction_delay"], 42)
        await self.tap(USER, f"dlc:{bid}"); await self.send(USER, "99999")
        self.assertEqual(self.db.clones[bid]["settings"]["reaction_delay"], 42)

    async def test_edit_reactions(self):
        bid = await self.make_clone()
        await self.tap(USER, f"rxe:{bid}"); await self.send(USER, "🎉 🔥❤️ 🤷‍♂️")
        self.assertEqual(self.db.clones[bid]["settings"]["custom_reactions"], ["🎉", "🔥", "❤", "🤷‍♂"])
        await self.tap(USER, f"rxe:{bid}"); await self.send(USER, "🎉 abc")
        self.assertEqual(len(self.db.clones[bid]["settings"]["custom_reactions"]), 4)
        await self.tap(USER, f"rxr:{bid}")
        self.assertEqual(self.db.clones[bid]["settings"]["custom_reactions"], ["👍", "❤"])

    async def test_delete_flow(self):
        bid = await self.make_clone()
        await self.tap(USER, f"del:{bid}"); self.assertIn(bid, self.db.clones)
        await self.tap(USER, f"delc:{bid}"); self.assertNotIn(bid, self.db.clones)

    async def test_clone_command_flow_deletes_token_message(self):
        await self.send(USER, "/clone"); await self.send(USER, self.token(3))
        self.assertEqual(await self.db.count_user_clones(USER), 1)
        self.assertEqual(self.runner.api.last("delete_message")[1], (USER, 5))


# ═══════════════ 1. pause / resume + token hot-swap ═══════════════
class PauseResumeToken(Base):
    async def test_pause_keeps_clone_and_resume_restarts(self):
        bid = await self.make_clone()
        self.assertIn(bid, self.mgr.runners)
        await self.tap(USER, f"pz:{bid}")
        self.assertEqual(self.db.clones[bid]["status"], "paused")
        self.assertNotIn(bid, self.mgr.runners)                 # polling stopped
        self.assertEqual(self.db.clones[bid]["settings"]["custom_reactions"], ["👍", "❤"])  # config kept
        self.assertIn("▶️ Resume", self.labels(self.last_kb()))
        self.started.clear()
        await self.tap(USER, f"pz:{bid}")
        self.assertEqual(self.db.clones[bid]["status"], "active")
        self.assertEqual(self.started, [(bid, True)])           # restarted, backlog dropped

    async def test_token_hot_swap_same_bot(self):
        bid = await self.make_clone()
        self.db.clones[bid]["status"] = "revoked"; self.mgr.runners.pop(bid)
        self.db.clones[bid]["settings"]["reaction_delay"] = 7
        old_bid = bid
        # new token that getMe resolves to the SAME bot id
        import bot.main_bot as mb
        async def same_bot(this): return {"id": old_bid, "is_bot": True, "username": f"Clone{old_bid}"}
        orig = mb.TelegramAPI.get_me; mb.TelegramAPI.get_me = same_bot
        try:
            self.started.clear()
            await self.tap(USER, f"tk:{bid}")
            await self.send(USER, "300001:NEWTOKENNEWTOKENNEWTOKENNEW")
        finally:
            mb.TelegramAPI.get_me = orig
        d = self.db.clones[bid]
        self.assertEqual(d["bot_token"], "300001:NEWTOKENNEWTOKENNEWTOKENNEW")
        self.assertEqual(d["status"], "active")
        self.assertEqual(d["settings"]["reaction_delay"], 7)    # nothing lost
        self.assertEqual(self.started, [(bid, True)])           # restarted with the new token
        self.assertEqual(self.runner.api.last("delete_message")[1], (USER, 5))

    async def test_token_of_different_bot_rejected(self):
        bid = await self.make_clone()
        await self.tap(USER, f"tk:{bid}")
        await self.send(USER, self.token(77))                  # resolves to a new, different bot id
        self.assertNotEqual(self.db.clones[bid]["bot_token"], self.token(77))
        self.assertIn("different bot", self.runner.api.last("send_message")[1][1])

    async def test_revoked_marks_status_and_blocks_resume(self):
        bid = await self.make_clone()
        runner = self.mgr.runners[bid]
        await self.mgr.on_token_dead(runner)
        self.assertEqual(self.db.clones[bid]["status"], "revoked")
        self.assertNotIn(bid, self.mgr.runners)
        self.assertIn("revoked", self.runner.api.last("send_message")[1][1])  # owner notified
        await self.tap(USER, f"cb:{bid}")
        labels = self.labels(self.last_kb())
        self.assertIn("🔑 Update Token", labels); self.assertNotIn("▶️ Resume", labels)
        await self.tap(USER, f"pz:{bid}")
        self.assertIn("Update Token", self.alert())


# ═══════════════ 2. admin panel ═══════════════
class AdminPanelTests(Base):
    async def test_admin_only(self):
        await self.send(USER, "/users")
        self.assertIn("Admins only", self.runner.api.last("send_message")[1][1])
        await self.tap(USER, "au:0")
        self.assertIn("Admins only", self.alert())

    async def test_users_and_clones_views(self):
        await self.send(USER, "/start")
        await self.make_clone(USER, 1)
        await self.send(OWNER, "/users")
        self.assertTrue(any(f"· {USER}" in l for l in self.labels(self.runner.api.last("send_message")[1][2])))
        await self.tap(OWNER, f"aud:{USER}")
        self.assertIn("🚫 Ban", self.labels(self.last_kb()))
        await self.send(OWNER, "/clones")
        self.assertTrue(any("@Clone" in l for l in self.labels(self.runner.api.last("send_message")[1][2])))

    async def test_ban_stops_clones_and_blocks_user_unban_restores(self):
        await self.send(USER, "/start")
        bid = await self.make_clone(USER, 1)
        await self.send(OWNER, f"/ban {USER}")
        self.assertTrue(self.mgr.is_banned(USER))
        self.assertNotIn(bid, self.mgr.runners)                # their clone stopped
        self.runner.api.calls.clear()
        await self.send(USER, "/start")                        # blocked
        self.assertIn("banned", self.runner.api.last("send_message")[1][1])
        await self.tap(USER, "sl:0"); self.assertIn("banned", self.alert())
        await self.send(OWNER, f"/unban {USER}")
        self.assertFalse(self.mgr.is_banned(USER)); self.assertIn(bid, self.mgr.runners)

    async def test_cannot_ban_admin(self):
        await self.send(OWNER, f"/ban {OWNER}")
        self.assertFalse(self.mgr.is_banned(OWNER))

    async def test_force_delete_any_clone_and_notify(self):
        bid = await self.make_clone(USER, 1)
        await self.tap(OWNER, f"acx:{bid}"); self.assertIn(bid, self.db.clones)  # confirm first
        await self.tap(OWNER, f"acy:{bid}")
        self.assertNotIn(bid, self.db.clones); self.assertNotIn(bid, self.mgr.runners)
        self.assertTrue(any(c[1][0] == USER and "removed by an admin" in c[1][1] for c in self.runner.api.all("send_message")))

    async def test_admin_can_pause_resume_any_clone(self):
        bid = await self.make_clone(USER, 1)
        await self.tap(OWNER, f"acp:{bid}"); self.assertEqual(self.db.clones[bid]["status"], "paused")
        await self.tap(OWNER, f"acp:{bid}"); self.assertEqual(self.db.clones[bid]["status"], "active")


# ═══════════════ 3. daily report ═══════════════
class ReportTests(Base):
    async def test_report_contents(self):
        await self.send(USER, "/start")
        a, b = await self.make_clone(USER, 1), await self.make_clone(USER, 2)
        await self.mgr.pause_clone(b)
        self.mgr.runners[a].counters.update(messages=10, reactions=8, failed=2)
        text = await build_report(self.mgr, "today")
        for want in ("Users: <b>1</b>", "2</b> — 🟢 1 active · ⏸ 1 paused", "Messages seen: <b>10</b>",
                     "Reactions sent: <b>8</b>", "Failed: <b>2</b>", "System health", "Uptime"):
            self.assertIn(want, text)

    async def test_flush_persists_counters(self):
        bid = await self.make_clone()
        self.mgr.runners[bid].counters.update(messages=3, reactions=3)
        await self.mgr.flush_stats()
        self.assertEqual(self.db.clones[bid]["stats"]["reactions"], 3)
        self.assertEqual(self.db.daily[self.mgr.today()]["messages"], 3)

    def test_seconds_until_next_run(self):
        tz = get_tz("UTC")
        t = datetime(2026, 10, 31, 8, 0, tzinfo=tz)
        self.assertEqual(seconds_until(t, 9, 0), 3600)
        self.assertEqual(seconds_until(datetime(2026, 10, 31, 10, 0, tzinfo=tz), 9, 0), 23 * 3600)

    def test_config_parsing(self):
        self.assertEqual(_report_time("09:30"), (9, 30)); self.assertIsNone(_report_time("off"))
        self.assertEqual(_rights("X", "manage_chat"), "manage_chat")


# ═══════════════ 4. human-like random delay ═══════════════
class RandomDelay(unittest.IsolatedAsyncioTestCase):
    def test_pick_delay_modes(self):
        self.assertEqual(pick_delay({"reaction_delay": 5, "random_delay": {"enabled": False}}), 5)
        vals = {pick_delay({"reaction_delay": 5, "random_delay": {"enabled": True, "min": 2, "max": 60}}) for _ in range(200)}
        self.assertTrue(all(2 <= v <= 60 for v in vals)); self.assertGreater(len(vals), 50)  # genuinely random

    async def test_react_sleeps_then_reacts_and_counts(self):
        api, c = FakeAPI(), Counter()
        upd = {"channel_post": {"message_id": 3, "chat": {"id": -100, "type": "channel"}, "text": "hi"}}
        t0 = time.monotonic()
        self.assertTrue(await react_to_update(api, upd, ["🔥", "🎉"], 0.3, c))
        self.assertGreaterEqual(time.monotonic() - t0, 0.29)
        self.assertEqual((c["messages"], c["reactions"], c["failed"]), (1, 1, 0))

    async def test_failure_counted(self):
        class Bad(FakeAPI):
            async def set_message_reaction(self, *a): raise TelegramError("x", 400, "REACTION_INVALID")
        c = Counter()
        upd = {"message": {"message_id": 3, "chat": {"id": 1, "type": "group"}, "text": "hi"}}
        self.assertFalse(await react_to_update(Bad(), upd, ["🔥"], 0, c))
        self.assertEqual(c["failed"], 1)

    async def test_service_message_skipped(self):
        api = FakeAPI()
        upd = {"message": {"message_id": 3, "chat": {"id": 1, "type": "group"}, "new_chat_members": [{}]}}
        self.assertFalse(await react_to_update(api, upd, ["🔥"], 0)); self.assertEqual(api.calls, [])


class RandomDelayUI(Base):
    async def test_toggle_presets_custom_range(self):
        bid = await self.make_clone()
        await self.tap(USER, f"rdt:{bid}")
        self.assertTrue(self.db.clones[bid]["settings"]["random_delay"]["enabled"])
        await self.tap(USER, f"rds:{bid}:5:30")
        self.assertEqual(self.db.clones[bid]["settings"]["random_delay"], {"enabled": True, "min": 5, "max": 30})
        await self.tap(USER, f"rdc:{bid}"); await self.send(USER, "3-45")
        self.assertEqual(self.db.clones[bid]["settings"]["random_delay"], {"enabled": True, "min": 3, "max": 45})
        await self.tap(USER, f"rdc:{bid}"); await self.send(USER, "50 10")      # min > max rejected
        self.assertEqual(self.db.clones[bid]["settings"]["random_delay"]["min"], 3)
        await self.tap(USER, f"dls:{bid}:2")                                   # picking a fixed delay turns random off
        self.assertFalse(self.db.clones[bid]["settings"]["random_delay"]["enabled"])

    async def test_clone_uses_random_delay_from_db(self):
        bid = await self.make_clone()
        await self.db.set_clone_setting(bid, "random_delay", {"enabled": True, "min": 0, "max": 0.2})
        runner = self.mgr.runners[bid]; runner.api = FakeAPI()
        t0 = time.monotonic()
        await self.mgr.handle_clone_update(runner, {"channel_post": {"message_id": 1, "chat": {"id": -1, "type": "channel"}, "text": "a"}})
        self.assertLessEqual(time.monotonic() - t0, 0.5)
        self.assertEqual(runner.counters["reactions"], 1)


# ═══════════════ 5. add-to-channel/group links ═══════════════
class LinkTests(Base):
    def test_links_have_admin_rights(self):
        g, c = add_links_row("MyBot", make_cfg(group_admin_rights="manage_chat+delete_messages", channel_admin_rights="post_messages"))
        self.assertEqual(g["url"], "https://t.me/MyBot?startgroup&admin=manage_chat+delete_messages")
        self.assertEqual(c["url"], "https://t.me/MyBot?startchannel&admin=post_messages")

    async def test_panel_and_admin_card_have_links(self):
        bid = await self.make_clone()
        await self.tap(USER, f"cb:{bid}")
        urls = [b["url"] for row in self.last_kb() for b in row if "url" in b]
        self.assertEqual(len(urls), 2); self.assertTrue(all("admin=" in u for u in urls))
        await self.tap(OWNER, f"acd:{bid}")
        self.assertTrue(any("url" in b for row in self.last_kb() for b in row))


# ═══════════════ engine loop + misc ═══════════════
class EngineLoop(unittest.IsolatedAsyncioTestCase):
    async def test_clone_polls_and_reacts_with_db_settings_and_delay(self):
        db = FakeDB()
        await db.add_clone(USER, "t", 555, "CloneX", ["🎉"], 0)
        await db.set_clone_setting(555, "reaction_delay", 0.2)
        mgr = BotManager(make_cfg(), db, None)
        runner = BotRunner(mgr, "clone", "t", 555, "CloneX")
        batches = [[{"update_id": 10, "channel_post": {"message_id": 1, "chat": {"id": -1, "type": "channel"}, "text": "a"}}]]
        reacted = []

        class API(FakeAPI):
            async def delete_webhook(self, drop=False): self.dropped = drop
            async def get_updates(self, offset, timeout, allowed):
                self.offset_seen = offset
                if batches: return batches.pop(0)
                await asyncio.sleep(10)
            async def set_message_reaction(self, chat_id, mid, emoji): reacted.append((time.monotonic(), emoji))
        runner.api = API()
        t0 = time.monotonic()
        runner.start(); await asyncio.sleep(0.5); await runner.stop()
        self.assertEqual([e for _, e in reacted], ["🎉"])
        self.assertGreaterEqual(reacted[0][0] - t0, 0.19)
        self.assertEqual(runner.api.offset_seen, 11)
        self.assertEqual(runner.counters["reactions"], 1)

    async def test_settings_change_picked_up_after_invalidate(self):
        db = FakeDB()
        await db.add_clone(USER, "t", 555, "CloneX", ["🎉"], 0)
        runner = BotRunner(BotManager(make_cfg(), db, None), "clone", "t", 555, "CloneX")
        self.assertEqual((await runner.get_settings())["custom_reactions"], ["🎉"])
        await db.set_clone_setting(555, "custom_reactions", ["🔥"]); runner.invalidate_settings()
        self.assertEqual((await runner.get_settings())["custom_reactions"], ["🔥"])


class Helpers(unittest.TestCase):
    def test_parse_reactions(self):
        self.assertEqual(parse_reactions("👍❤️🔥")[0], ["👍", "❤", "🔥"])
        self.assertEqual(parse_reactions("❤‍🔥 ❤")[0], ["❤‍🔥", "❤"])
        self.assertEqual(parse_reactions("👍 x 🦖")[1], ["x", "🦖"])

    def test_parse_command(self):
        self.assertEqual(parse_command("/start@MainBot hi", "MainBot"), ("start", "hi", True))
        self.assertFalse(parse_command("/start@Other", "MainBot")[2])
        self.assertIsNone(parse_command("hello", "MainBot")[0])


if __name__ == "__main__":
    unittest.main()
