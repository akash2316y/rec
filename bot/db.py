"""MongoDB access layer (Motor). Collections: clone_bots, users, daily_stats."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

DEFAULT_RANDOM_DELAY = {"enabled": False, "min": 2, "max": 60}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, uri: str, name: str):
        self._client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10_000)
        db = self._client[name]
        self.clones = db["clone_bots"]
        self.users = db["users"]
        self.daily = db["daily_stats"]

    async def init(self) -> None:
        await self.ping()  # fail fast if Mongo is unreachable
        await self.clones.create_index("bot_id", unique=True)
        await self.clones.create_index("user_id")
        await self.clones.create_index("status")
        await self.users.create_index("user_id", unique=True)
        await self.daily.create_index("date", unique=True)
        # migration for clones created before pause/resume existed
        await self.clones.update_many({"status": {"$exists": False}}, {"$set": {"status": "active"}})

    async def ping(self) -> float:
        t0 = time.monotonic()
        await self._client.admin.command("ping")
        return (time.monotonic() - t0) * 1000

    def close(self) -> None:
        self._client.close()

    # ═══════════ clone_bots ═══════════
    async def add_clone(self, user_id: int, token: str, bot_id: int, username: str,
                        reactions: list[str], delay: int) -> dict:
        doc = {
            "user_id": user_id,
            "bot_token": token,
            "bot_id": bot_id,
            "bot_username": username,
            "created_at": _now(),
            "status": "active",                       # active | paused | revoked
            "stats": {"messages": 0, "reactions": 0, "failed": 0},
            "settings": {
                "custom_reactions": list(reactions),
                "reaction_delay": delay,
                "random_delay": dict(DEFAULT_RANDOM_DELAY),  # human-like anti-flood delay
            },
        }
        await self.clones.insert_one(doc)
        return doc

    async def get_clone(self, bot_id: int) -> dict | None:
        return await self.clones.find_one({"bot_id": bot_id})

    async def get_user_clones(self, user_id: int) -> list[dict]:
        return await self.clones.find({"user_id": user_id}).sort("created_at", 1).to_list(length=None)

    async def count_user_clones(self, user_id: int) -> int:
        return await self.clones.count_documents({"user_id": user_id})

    async def get_all_clones(self) -> list[dict]:
        return await self.clones.find({}).to_list(length=None)

    async def list_clones(self, owner: int | None = None, skip: int = 0, limit: int = 8) -> list[dict]:
        q = {"user_id": owner} if owner else {}
        return await self.clones.find(q).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)

    async def count_clones(self, owner: int | None = None, status: str | None = None) -> int:
        q: dict = {}
        if owner:
            q["user_id"] = owner
        if status:
            q["status"] = status
        return await self.clones.count_documents(q)

    async def set_clone_setting(self, bot_id: int, key: str, value) -> None:
        await self.clones.update_one({"bot_id": bot_id}, {"$set": {f"settings.{key}": value}})

    async def set_clone_status(self, bot_id: int, status: str) -> None:
        await self.clones.update_one({"bot_id": bot_id}, {"$set": {"status": status, "status_at": _now()}})

    async def set_clone_token(self, bot_id: int, token: str, status: str | None = None) -> None:
        fields = {"bot_token": token, "token_updated_at": _now()}
        if status:
            fields.update(status=status, status_at=_now())
        await self.clones.update_one({"bot_id": bot_id}, {"$set": fields})

    async def inc_clone_stats(self, bot_id: int, messages: int, reactions: int, failed: int) -> None:
        await self.clones.update_one({"bot_id": bot_id}, {"$inc": {
            "stats.messages": messages, "stats.reactions": reactions, "stats.failed": failed}})

    async def delete_clone(self, bot_id: int) -> None:
        await self.clones.delete_one({"bot_id": bot_id})

    # ═══════════ users ═══════════
    async def upsert_user(self, user_id: int, first_name: str = "", username: str = "") -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"first_name": first_name, "username": username, "last_seen": _now()},
             "$setOnInsert": {"user_id": user_id, "joined_at": _now(), "banned": False}},
            upsert=True,
        )

    async def get_user(self, user_id: int) -> dict | None:
        return await self.users.find_one({"user_id": user_id})

    async def list_users(self, skip: int = 0, limit: int = 8) -> list[dict]:
        return await self.users.find({}).sort("joined_at", -1).skip(skip).limit(limit).to_list(length=limit)

    async def count_users(self) -> int:
        return await self.users.count_documents({})

    async def count_banned(self) -> int:
        return await self.users.count_documents({"banned": True})

    async def set_banned(self, user_id: int, banned: bool, by: int | None = None) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"banned": banned, "banned_at": _now() if banned else None, "banned_by": by if banned else None},
             "$setOnInsert": {"user_id": user_id, "joined_at": _now(), "first_name": "", "username": ""}},
            upsert=True,
        )

    async def banned_ids(self) -> set[int]:
        return {d["user_id"] async for d in self.users.find({"banned": True}, {"user_id": 1})}

    async def all_user_ids(self) -> list[int]:
        return [d["user_id"] async for d in self.users.find({"banned": {"$ne": True}}, {"user_id": 1})]

    async def remove_user(self, user_id: int) -> None:
        await self.users.delete_one({"user_id": user_id})

    # ═══════════ daily_stats (one doc per local date) ═══════════
    async def inc_daily(self, date: str, messages: int, reactions: int, failed: int) -> None:
        await self.daily.update_one(
            {"date": date},
            {"$inc": {"messages": messages, "reactions": reactions, "failed": failed}},
            upsert=True,
        )

    async def get_daily(self, date: str) -> dict:
        doc = await self.daily.find_one({"date": date}) or {}
        return {"messages": doc.get("messages", 0), "reactions": doc.get("reactions", 0), "failed": doc.get("failed", 0)}
