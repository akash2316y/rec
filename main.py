import asyncio
import logging
import os
import signal

from bot.config import Config, ConfigError
from bot.db import Database
from bot.engine import BotManager
from bot.tg import make_http_client


async def run() -> None:
    log = logging.getLogger("main")
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        log.error("%s", e)
        raise SystemExit(1)

    db = Database(cfg.mongo_uri, cfg.db_name)
    await db.init()

    http = make_http_client()
    stop = asyncio.Event()
    manager = BotManager(cfg, db, http, stop)
    try:
        await manager.start()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass
        await stop.wait()
    finally:
        log.info("Shutting down…")
        await manager.stop_all()
        await http.aclose()
        db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx INFO logs full URLs (= bot tokens)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
