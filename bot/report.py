"""Daily owner report: scheduler + report builder."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone, tzinfo

from .tg import TelegramError

log = logging.getLogger("report")


def get_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # missing tzdata / bad name
        log.warning("Timezone %r unavailable (pip install tzdata) — using UTC", name)
        return timezone.utc


def seconds_until(now: datetime, hour: int, minute: int) -> float:
    """Seconds from `now` (tz-aware) to the next hh:mm in now's timezone."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def fmt_uptime(seconds: float) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d {h}h {rem // 60}m" if d else f"{h}h {rem // 60}m"


async def build_report(manager, day: str = "yesterday") -> str:
    """day='yesterday' (scheduled) or 'today' (manual, so far)."""
    db = manager.db
    await manager.flush_stats()  # make sure in-memory counters are included

    now = datetime.now(manager.tz)
    date = now.date() if day == "today" else (now - timedelta(days=1)).date()
    stats = await db.get_daily(date.isoformat())

    users, banned = await db.count_users(), await db.count_banned()
    total = await db.count_clones()
    active = await db.count_clones(status="active")
    paused = await db.count_clones(status="paused")
    revoked = await db.count_clones(status="revoked")
    running = sum(1 for r in manager.runners.values() if r.kind == "clone" and r.running)

    try:
        ping_ms: float | None = await db.ping()
    except Exception:
        ping_ms = None

    sent, failed = stats["reactions"], stats["failed"]
    fail_rate = failed / (sent + failed) * 100 if (sent + failed) else 0.0

    issues = []
    if ping_ms is None:
        issues.append("🔴 MongoDB unreachable")
    if revoked:
        issues.append(f"🟡 {revoked} clone(s) with revoked token")
    expected = active
    for uid in manager.banned:  # clones of banned users are intentionally stopped
        expected -= await db.count_clones(owner=uid, status="active")
    if running < expected:
        issues.append(f"🟡 only {running}/{expected} expected clones are running")
    if fail_rate > 20:
        issues.append(f"🟡 high reaction failure rate ({fail_rate:.0f}%)")
    health = "🔴 Problem" if ping_ms is None else ("🟡 Degraded" if issues else "🟢 Healthy")

    label = "Today (so far)" if day == "today" else "Yesterday"
    lines = [
        f"📊 <b>Daily Report</b> — {date.isoformat()}",
        "",
        f"👥 Users: <b>{users}</b> (banned: {banned})",
        f"🤖 Clones: <b>{total}</b> — 🟢 {active} active · ⏸ {paused} paused · ⚠️ {revoked} revoked",
        f"🏃 Running now: <b>{running}</b> clone(s)",
        "",
        f"📈 <b>Activity — {label}</b>",
        f"💬 Messages seen: <b>{stats['messages']}</b>",
        f"❤️ Reactions sent: <b>{sent}</b>",
        f"❌ Failed: <b>{failed}</b> ({fail_rate:.0f}%)",
        "",
        f"🩺 <b>System health:</b> {health}",
        f"🗄 MongoDB: {'%.0f ms' % ping_ms if ping_ms is not None else 'down'}",
        f"⏱ Uptime: {fmt_uptime(time.time() - manager.started_at)}",
    ]
    lines += [f"• {i}" for i in issues]
    return "\n".join(lines)


class ReportScheduler:
    def __init__(self, manager):
        self.manager = manager

    async def run(self) -> None:
        hour, minute = self.manager.cfg.report_time
        log.info("Daily report scheduled at %02d:%02d (%s)", hour, minute, self.manager.cfg.report_tz)
        while True:
            now = datetime.now(self.manager.tz)
            await asyncio.sleep(seconds_until(now, hour, minute))
            try:
                await self.send()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("daily report failed")
            await asyncio.sleep(2)  # avoid double-firing within the same minute

    async def send(self, day: str = "yesterday") -> None:
        text = await build_report(self.manager, day)
        try:
            await self.manager.main_runner.api.send_message(self.manager.cfg.owner_id, text)
        except TelegramError as e:
            log.warning("could not DM the report to the owner (has the owner pressed /start?): %s", e.description)
