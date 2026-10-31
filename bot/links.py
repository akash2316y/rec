"""One-tap 'Add to Group / Channel' deep links with admin rights pre-selected.

Format (Telegram deep links):
  https://t.me/<bot>?startgroup&admin=<right+right>     -> pick a group, bot added as admin
  https://t.me/<bot>?startchannel&admin=<right+right>   -> pick a channel, bot added as admin
"""
from __future__ import annotations

from .ui import url_btn


def group_link(username: str, rights: str) -> str:
    return f"https://t.me/{username}?startgroup&admin={rights}"


def channel_link(username: str, rights: str) -> str:
    return f"https://t.me/{username}?startchannel&admin={rights}"


def add_links_row(username: str, cfg) -> list[dict]:
    return [
        url_btn("➕ Add to Group", group_link(username, cfg.group_admin_rights)),
        url_btn("➕ Add to Channel", channel_link(username, cfg.channel_admin_rights)),
    ]
