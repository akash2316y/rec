"""Environment configuration. Single BOT_TOKEN; clones live in MongoDB."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

from .reactions import parse_reactions

log = logging.getLogger("config")

FALLBACK_REACTIONS = ("👍", "❤", "🔥", "🥰", "👏", "😁")


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _rights(name: str, default: str) -> str:
    raw = os.getenv(name, "").strip() or default
    parts = [p for p in re.split(r"[\s,+]+", raw) if p]
    return "+".join(parts)


def _report_time(raw: str) -> tuple[int, int] | None:
    raw = (raw if raw is not None else "09:00").strip().lower()
    if raw in ("", "off", "none", "false", "0"):
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ConfigError(f"REPORT_TIME must be HH:MM (24h) or 'off', got {raw!r}")
    return int(m[1]), int(m[2])


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    mongo_uri: str
    db_name: str
    default_reactions: tuple[str, ...]
    admin_ids: frozenset[int]
    max_clones: int
    default_delay: int
    max_delay: int
    group_admin_rights: str = "manage_chat"
    channel_admin_rights: str = "manage_chat"
    report_time: tuple[int, int] | None = (9, 0)  # (hour, minute) in report_tz, None = disabled
    report_tz: str = "Asia/Kolkata"

    def is_admin(self, user_id: int) -> bool:
        """Owner/admins bypass the clone limit and can use /broadcast."""
        return user_id == self.owner_id or user_id in self.admin_ids

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        if os.getenv("BOT_TOKENS"):
            log.warning("BOT_TOKENS is no longer supported and is ignored — use BOT_TOKEN. "
                        "Clone bots are created with /clone and stored in MongoDB.")

        try:
            owner_id = int(_required("OWNER_ID"))
        except ValueError:
            raise ConfigError("OWNER_ID must be a numeric Telegram user ID") from None

        admin_ids = set()
        for part in os.getenv("ADMIN_IDS", "").split(","):
            part = part.strip()
            if part:
                try:
                    admin_ids.add(int(part))
                except ValueError:
                    raise ConfigError(f"ADMIN_IDS contains a non-numeric value: {part!r}") from None

        reactions, invalid = parse_reactions(os.getenv("EMOJI_LIST", ""))
        if invalid:
            log.warning("EMOJI_LIST has unsupported characters (ignored): %s", " ".join(invalid))
        if not reactions:
            reactions = list(FALLBACK_REACTIONS)

        max_delay = _int("MAX_DELAY", 300, 1)
        default_delay = min(_int("DEFAULT_DELAY", 0, 0), max_delay)

        return cls(
            bot_token=_required("BOT_TOKEN"),
            owner_id=owner_id,
            mongo_uri=_required("MONGO_URI"),
            db_name=os.getenv("DB_NAME", "autoreactionbot").strip() or "autoreactionbot",
            default_reactions=tuple(reactions),
            admin_ids=frozenset(admin_ids),
            max_clones=_int("MAX_CLONES", 5, 1),
            default_delay=default_delay,
            max_delay=max_delay,
            group_admin_rights=_rights("GROUP_ADMIN_RIGHTS", "manage_chat"),
            channel_admin_rights=_rights("CHANNEL_ADMIN_RIGHTS", "manage_chat"),
            report_time=_report_time(os.getenv("REPORT_TIME", "09:00")),
            report_tz=os.getenv("REPORT_TZ", "Asia/Kolkata").strip() or "Asia/Kolkata",
        )
