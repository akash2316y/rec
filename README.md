# 🤖 Auto Reaction Bot — v4.1 (Python · MongoDB · polling)

Reacts to every message in private chats, groups and channels. One **main bot** (`BOT_TOKEN`) plus **clone bots**
users create themselves, stored in **MongoDB**. Runs by **long polling** — no webhook / domain / HTTPS needed.

## Architecture
```
main.py ─► BotManager (engine.py)
            ├─ BotRunner(main)       getUpdates loop ─► MainBot (main_bot.py)  UI · /clone · settings
            │                                            └─ AdminPanel (admin.py)  /users /clones /ban …
            ├─ BotRunner(clone A…)   one polling loop per ACTIVE clone ─► reacts using DB settings
            ├─ stats flush loop      counters → MongoDB every 60 s
            └─ ReportScheduler (report.py)   daily DM to OWNER_ID
Helpers: db.py (Motor) · tg.py (Bot API client) · reactions.py · links.py · ui.py · config.py
```
**Clone lifecycle** (nothing is ever recreated, config/stats/token stay in MongoDB):
`active ⇄ paused` (pause/resume) · `active → revoked` (auto, on 401) · `revoked → active` (🔑 Update Token) ·
owner banned → runners stopped, unban → restarted.

## Features
| Area | What it does |
|---|---|
| ⏸ Pause / Resume | Settings → your bot → Pause. Polling stops, the clone stays in the DB. Resume drops the backlog so it doesn't burst-react to old messages. |
| 🔑 Token hot-swap | Token revoked/expired → 🔑 Update Token → send the new token of the **same** bot (verified by bot ID). Runner restarts, settings/stats kept. |
| 🛠 Admin panel | `/admin`, `/users`, `/clones` (inline buttons, paginated), `/ban <id>`, `/unban <id>`, 🗑 Force Delete (owner notified), pause/resume any clone. Admins = `OWNER_ID` + `ADMIN_IDS`. |
| 📊 Daily report | Sent to the owner's DM at `REPORT_TIME`: users, clones (active/paused/revoked/running), messages seen, reactions sent/failed, health (MongoDB ping, failure rate, revoked clones, uptime). `/report` = today so far. |
| 🎲 Human-like delay | Per clone: random wait in [min, max] seconds (presets 2–10, 5–30, 2–60 or custom) **re-rolled for every reaction** → no bursts, fewer 429s. Or a fixed delay. |
| ➕ Add links | Every clone card has **Add to Group / Add to Channel** buttons: `t.me/<bot>?startgroup&admin=<rights>` / `?startchannel&admin=<rights>` — admin rights pre-selected (`GROUP_ADMIN_RIGHTS`, `CHANNEL_ADMIN_RIGHTS`). |
| Limits | 5 clones per user, unlimited for owner/admins. Token verified with `getMe` before saving. |

## MongoDB schema
`clone_bots` (new fields marked ★; old documents are migrated to `status: "active"` on startup)
```json
{
  "user_id": 12345678,
  "bot_token": "123456:ABC...",
  "bot_id": 987654321,
  "bot_username": "MyCloneBot",
  "created_at": "<datetime UTC>",
  "status": "active",                                   // ★ active | paused | revoked
  "stats": { "messages": 0, "reactions": 0, "failed": 0 },   // ★ flushed every 60 s
  "settings": {
    "custom_reactions": ["👍", "❤", "🔥"],
    "reaction_delay": 5,
    "random_delay": { "enabled": false, "min": 2, "max": 60 }   // ★ human-like delay
  }
}
```
`users`: `{ user_id, first_name, username, joined_at, last_seen, banned, banned_at, banned_by }` ★ (ban fields)
`daily_stats` ★: `{ date: "2026-10-31", messages, reactions, failed }`
Indexes: `clone_bots.bot_id` (unique), `.user_id`, `.status` · `users.user_id` (unique) · `daily_stats.date` (unique)

## Run
```bash
cp .env.example .env      # set BOT_TOKEN and OWNER_ID
docker compose up -d --build && docker compose logs -f bot
# or: pip install -r requirements.txt && python main.py
```
Then open the main bot and press **/start** once (needed so the daily report DM can reach you).

## Notes
- Clone tokens are stored in **plain text** in MongoDB (as per schema). Keep MongoDB private, use auth, back it up.
- The admin panel is **inside the bot** (inline buttons) — there's no web server, so it works on any polling host.
- Banned users are blocked in the main bot's private chat and their clones are stopped; admins can't be banned.
- Never run one token in two places (409 Conflict). Channels: bot must be admin. Telegram allows only **1 reaction per bot per message**.
- Only Telegram's allowed reaction emoji work; the editor validates them.

## Tests
`python -m unittest discover -s tests -v` — in-memory fakes, no MongoDB/network needed.
