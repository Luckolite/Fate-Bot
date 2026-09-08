"""Read Fate's delivered Activity Log messages for the web dashboard."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import discord

from .validation import ValidationError

DISCORD_MENTION = re.compile(r"<(@!?|@&|#)(\d+)>")


def _text(value: Any, *, limit: int = 4_000) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _archive_summary(
    config: dict[str, Any], stats: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a stable archive-status payload even when usage is unavailable."""
    has_stats = isinstance(stats, dict)
    return {
        "enabled": True,
        "retention_days": int(config.get("retention_days", 30)),
        "cache_messages": bool(config.get("cache_messages", True)),
        "store_attachments": bool(config.get("store_attachments", False)),
        "attachment_size_limit_mb": int(
            config.get("attachment_size_limit_mb", 25)
        ),
        "stored_bytes": int(stats.get("stored_bytes", 0)) if has_stats else None,
        "limit_bytes": int(stats.get("limit_bytes", 1024**3)) if has_stats else 1024**3,
        "log_count": int(stats.get("log_count", 0)) if has_stats else None,
        "message_count": int(stats.get("message_count", 0)) if has_stats else None,
    }


def _resolved_name(value: Any, fallback: str) -> str:
    name = getattr(value, "display_name", None) or getattr(value, "name", None)
    return _text(name, limit=100) or fallback


def _resolve_discord_mentions(value: Any, guild, *, limit: int = 4_000) -> str:
    """Replace raw Discord mention tokens with names visible in the dashboard."""
    content = _text(value, limit=limit)

    def replace(match: re.Match[str]) -> str:
        mention_type, raw_id = match.groups()
        resource_id = int(raw_id)
        suffix = raw_id[-6:]
        if mention_type in {"@", "@!"}:
            member = guild.get_member(resource_id)
            return f"@{_resolved_name(member, f'user …{suffix}')}"
        if mention_type == "@&":
            role = guild.get_role(resource_id)
            return f"@{_resolved_name(role, f'role …{suffix}')}"
        get_channel = getattr(guild, "get_channel_or_thread", None) or getattr(
            guild, "get_channel", lambda _resource_id: None
        )
        channel = get_channel(resource_id)
        return f"#{_resolved_name(channel, f'channel …{suffix}')}"

    return DISCORD_MENTION.sub(replace, content)


def _embed_summary(data: dict[str, Any], guild) -> str:
    pieces = [
        _resolve_discord_mentions(
            (data.get("author") or {}).get("name"), guild, limit=180
        ),
        _resolve_discord_mentions(data.get("title"), guild, limit=256),
        _resolve_discord_mentions(data.get("description"), guild),
    ]
    pieces.extend(
        f"{_resolve_discord_mentions(field.get('name'), guild, limit=256)}: "
        f"{_resolve_discord_mentions(field.get('value'), guild, limit=1_024)}"
        for field in data.get("fields", [])[:12]
    )
    return "\n".join(piece for piece in pieces if piece)


def serialize_activity_message(message, channel) -> dict[str, Any] | None:
    """Convert one Discord logger message into a compact, browser-safe record."""
    if not message.embeds:
        return None

    embeds = [embed.to_dict() for embed in message.embeds]
    primary = embeds[0]
    guild = channel.guild
    author = primary.get("author") or {}
    title = (
        _resolve_discord_mentions(author.get("name"), guild, limit=180)
        or _resolve_discord_mentions(primary.get("title"), guild, limit=256)
        or "Activity update"
    )
    description = _resolve_discord_mentions(primary.get("description"), guild) or (
        _resolve_discord_mentions(message.content, guild)
    )
    details = [
        {
            "name": _resolve_discord_mentions(field.get("name"), guild, limit=256)
            or "Details",
            "value": _resolve_discord_mentions(
                field.get("value"), guild, limit=1_024
            ),
        }
        for field in primary.get("fields", [])[:12]
        if _resolve_discord_mentions(field.get("value"), guild, limit=1_024)
    ]

    for index, extra in enumerate(embeds[1:6], start=1):
        summary = _embed_summary(extra, guild)
        if summary:
            details.append({"name": f"Related item {index}", "value": summary[:1_024]})

    attachment_names = [
        _text(getattr(attachment, "filename", ""), limit=180)
        for attachment in getattr(message, "attachments", [])[:10]
    ]
    attachment_names = [name for name in attachment_names if name]
    if attachment_names:
        details.append({"name": "Attachments", "value": ", ".join(attachment_names)})

    created_at = message.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    timestamp = created_at.astimezone(timezone.utc).isoformat()
    channel_id = str(channel.id)
    guild_id = str(channel.guild.id)
    message_id = str(message.id)
    color = primary.get("color")
    if type(color) is not int or not 0 <= color <= 0xFFFFFF:
        color = None

    return {
        "id": message_id,
        "type": title,
        "title": title,
        "description": description,
        "details": details,
        "timestamp": timestamp,
        "channel_id": channel_id,
        "channel_name": _text(getattr(channel, "name", "activity-log"), limit=100),
        "color": color,
        "jump_url": f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}",
        "source": "discord",
    }


def serialize_archived_log(row: dict[str, Any], guild) -> dict[str, Any]:
    """Convert one local archive row into the same browser-safe shape."""
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    embeds = [item for item in payload.get("embeds", []) if isinstance(item, dict)][:6]
    primary = embeds[0] if embeds else {}
    author = primary.get("author") if isinstance(primary.get("author"), dict) else {}
    fallback_title = str(row.get("event_type") or "activity update").replace("_", " ").title()
    title = (
        _resolve_discord_mentions(author.get("name"), guild, limit=180)
        or _resolve_discord_mentions(primary.get("title"), guild, limit=256)
        or fallback_title
    )
    description = (
        _resolve_discord_mentions(primary.get("description"), guild)
        or _resolve_discord_mentions(payload.get("content"), guild)
        or _resolve_discord_mentions(payload.get("description"), guild)
    )
    fields = primary.get("fields") if isinstance(primary.get("fields"), list) else []
    details = [
        {
            "name": _resolve_discord_mentions(field.get("name"), guild, limit=256)
            or "Details",
            "value": _resolve_discord_mentions(field.get("value"), guild, limit=1_024),
        }
        for field in fields[:12]
        if isinstance(field, dict)
        and _resolve_discord_mentions(field.get("value"), guild, limit=1_024)
    ]
    for index, extra in enumerate(embeds[1:], start=1):
        summary = _embed_summary(extra, guild)
        if summary:
            details.append({"name": f"Related item {index}", "value": summary[:1_024]})

    attachments = payload.get("attachments", payload.get("files", []))
    if not isinstance(attachments, list):
        attachments = []
    attachment_names = []
    for attachment in attachments[:10]:
        if isinstance(attachment, dict):
            name = attachment.get("filename") or attachment.get("name")
        else:
            name = attachment
        name = _text(name, limit=180)
        if name:
            attachment_names.append(name)
    if attachment_names:
        details.append({"name": "Attachments", "value": ", ".join(attachment_names)})

    delivery_status = _text(row.get("delivery_status"), limit=30).lower()
    if delivery_status and delivery_status != "delivered":
        status_labels = {
            "pending": "Waiting to send to Discord",
            "failed": "Discord delivery failed; local copy preserved",
            "dropped": "Discord queue was full; local copy preserved",
            "partial": "Only part of this event reached Discord; local copy preserved",
            "uncertain": "Fate restarted before Discord delivery could be confirmed",
        }
        details.append(
            {
                "name": "Discord delivery",
                "value": status_labels.get(delivery_status, delivery_status.title()),
            }
        )

    created_at = row.get("created_at")
    try:
        timestamp = datetime.fromtimestamp(float(created_at), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        timestamp = datetime.now(timezone.utc).isoformat()
    channel_id = _text(row.get("channel_id"), limit=20)
    channel = None
    if channel_id.isdigit():
        get_channel = getattr(guild, "get_channel_or_thread", None) or getattr(
            guild, "get_channel", lambda _channel_id: None
        )
        channel = get_channel(int(channel_id))
    channel_name = _text(row.get("channel_name"), limit=100) or _resolved_name(
        channel, "logging"
    )
    color = primary.get("color")
    if type(color) is not int or not 0 <= color <= 0xFFFFFF:
        color = None

    return {
        "id": f"local:{row['id']}",
        "type": title,
        "title": title,
        "description": description,
        "details": details,
        "timestamp": timestamp,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "color": color,
        "jump_url": _text(row.get("jump_url"), limit=500) or None,
        "source": "local",
        "search_match": row.get("match_mode") or row.get("search_match"),
        "matched_query": row.get("matched_query"),
    }


class UnavailableActivityLogStore:
    """Explain why history is absent when no live Fate process is attached."""

    def __init__(self, *, preview: bool = False):
        self.preview = preview

    async def get(
        self,
        _guild_id: int,
        *,
        actor_id: int,
        before: int | str | None,
        limit: int,
        query: str | None = None,
    ) -> dict[str, Any]:
        del actor_id, before, limit, query
        reason = (
            "Live activity appears here when this dashboard is connected to Fate."
            if self.preview
            else "Activity history is temporarily unavailable because Fate is not connected."
        )
        return {
            "available": False,
            "entries": [],
            "has_more": False,
            "next_before": None,
            "reason": reason,
            "warning": None,
            "source": None,
            "archive": None,
        }


class BotActivityLogStore:
    """Read the newest logger messages across primary and redirected channels."""

    def __init__(self, bot):
        self.bot = bot

    async def _channel_messages(self, channel, *, before, limit: int):
        messages = []
        async for message in channel.history(
            limit=min(500, (limit + 1) * 5),
            before=before,
            oldest_first=False,
        ):
            bot_user = getattr(self.bot, "user", None)
            if bot_user is not None and message.author.id != bot_user.id:
                continue
            if message.embeds:
                messages.append(message)
                if len(messages) >= limit + 1:
                    break
        return messages

    async def get(
        self,
        guild_id: int,
        *,
        actor_id: int,
        before: int | str | None,
        limit: int,
        query: str | None = None,
    ) -> dict[str, Any]:
        guild = self.bot.get_guild(guild_id)
        logger = self.bot.get_cog("Logger")
        if guild is None or logger is None:
            raise ValidationError("Fate's Logging feature is temporarily unavailable.")

        config = logger.config.get(str(guild_id))
        if not config:
            return {
                "available": True,
                "entries": [],
                "has_more": False,
                "next_before": None,
                "reason": "Enable Logging above to begin recording server activity.",
                "warning": None,
                "source": None,
                "archive": None,
            }

        actor = guild.get_member(actor_id)
        if actor is None:
            try:
                actor = await guild.fetch_member(actor_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                actor = None
        if actor is None:
            raise ValidationError("Your Discord membership could not be verified.")

        channel_ids = list(
            dict.fromkeys(
                int(value)
                for value in (
                    config.get("channel"),
                    *(config.get("channels") or {}).values(),
                )
                if value
            )
        )
        channels = []
        inaccessible = 0
        user_inaccessible = 0
        for channel_id in channel_ids:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    inaccessible += 1
                    continue
            if getattr(getattr(channel, "guild", None), "id", None) != guild_id:
                inaccessible += 1
                continue
            if not hasattr(channel, "history"):
                inaccessible += 1
                continue
            actor_permissions = channel.permissions_for(actor)
            if not (
                actor_permissions.view_channel
                and actor_permissions.read_message_history
            ):
                user_inaccessible += 1
                continue
            channels.append(channel)

        archive_config = config.get("local_archive")
        archive_config = archive_config if isinstance(archive_config, dict) else {}
        archive_enabled = bool(archive_config.get("enabled", False))
        archive = getattr(logger, "local_archive", None)
        archive_warning = None
        archive_stats = None
        if archive_enabled and archive is not None:
            local_before = None
            if before:
                before_text = str(before)
                if before_text.startswith("local:"):
                    archive_cursor = before_text.removeprefix("local:")
                    if not re.fullmatch(
                        r"(?:(?:exact|prefix|fuzzy):)?\d+", archive_cursor
                    ):
                        raise ValidationError("That saved Logging page is not available.")
                    local_before = archive_cursor
                elif before_text.isdigit():
                    local_before = int(before_text)
                else:
                    raise ValidationError("That saved Logging page is not available.")
            result, stats = await asyncio.gather(
                archive.search_logs(
                    str(guild_id),
                    before=local_before,
                    limit=limit,
                    query=query,
                    channel_ids=[channel.id for channel in channels],
                ),
                archive.stats(str(guild_id)),
                return_exceptions=True,
            )
            if isinstance(stats, dict):
                archive_stats = stats
            if isinstance(result, Exception):
                archive_warning = (
                    "Saved Logging history could not be read right now; showing recent "
                    "Discord records instead."
                )
            else:
                match_mode = result.get("match_mode") if query else None
                matched_query = str(query).strip() if query else None
                entries = []
                for row in result["entries"]:
                    row_for_dashboard = dict(row)
                    row_for_dashboard.setdefault("match_mode", match_mode)
                    row_for_dashboard.setdefault("matched_query", matched_query)
                    entries.append(serialize_archived_log(row_for_dashboard, guild))
                warnings = []
                if inaccessible:
                    warnings.append(
                        f"{inaccessible} log destination could not be checked. "
                        "Check Fate's View Channel permission."
                    )
                if user_inaccessible:
                    warnings.append(
                        f"{user_inaccessible} log destination is hidden because your Discord "
                        "account cannot view its history."
                    )
                if archive_stats is None:
                    warnings.append(
                        "Current saved-history storage use could not be read right now."
                    )
                reason = None
                if not entries and query and result.get("has_more"):
                    reason = "No matches in this part of history. Load older activity to keep searching."
                elif not entries and query:
                    reason = "No saved activity matches that search."
                elif not entries and (inaccessible or user_inaccessible):
                    reason = "No saved Logging history is currently visible to you."
                elif not entries:
                    reason = "Local history is on. New Logging activity will appear here."
                return {
                    "available": True,
                    "entries": entries,
                    "has_more": bool(result["has_more"]),
                    "next_before": (
                        f"local:{result['next_before']}" if result.get("next_before") else None
                    ),
                    "reason": reason,
                    "warning": " ".join(warnings) or None,
                    "source": "local",
                    "match_mode": match_mode,
                    "archive": _archive_summary(archive_config, archive_stats),
                }
        elif archive_enabled:
            archive_warning = (
                "Saved Logging history is enabled but is not available until Fate reconnects."
            )

        discord_before = None
        if before and not str(before).startswith("local:"):
            discord_before = int(before)
        before_object = discord.Object(id=discord_before) if discord_before else None

        async def read_channel(channel):
            try:
                return await self._channel_messages(
                    channel,
                    before=before_object,
                    limit=limit,
                )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return None

        batches = await asyncio.gather(*(read_channel(channel) for channel in channels))
        messages = []
        channel_by_id = {channel.id: channel for channel in channels}
        for batch in batches:
            if batch is None:
                inaccessible += 1
            else:
                messages.extend(batch)
        messages.sort(key=lambda item: item.id, reverse=True)
        has_more = len(messages) > limit
        messages = messages[:limit]

        entries = []
        for message in messages:
            channel = channel_by_id.get(message.channel.id, message.channel)
            entry = serialize_activity_message(message, channel)
            if entry:
                entries.append(entry)

        warnings = []
        if archive_warning:
            warnings.append(archive_warning)
        if inaccessible:
            warnings.append(
                f"{inaccessible} log destination could not be read. "
                "Check Fate's View Channel and Read Message History permissions."
            )
        if user_inaccessible:
            warnings.append(
                f"{user_inaccessible} log destination is hidden because your Discord "
                "account cannot view its history."
            )
        warning = " ".join(warnings) or None
        reason = None
        if not entries and (inaccessible or user_inaccessible):
            reason = "No configured Logging history is currently visible to you."
        elif not entries:
            reason = "No activity has been recorded in the loaded time period."

        return {
            "available": True,
            "entries": entries,
            "has_more": has_more,
            "next_before": entries[-1]["id"] if has_more and entries else None,
            "reason": reason,
            "warning": warning,
            "source": "discord",
            "archive": (
                _archive_summary(archive_config, archive_stats)
                if archive_enabled
                else {
                    "enabled": False,
                    "retention_days": int(archive_config.get("retention_days", 30)),
                    "cache_messages": bool(archive_config.get("cache_messages", True)),
                    "store_attachments": bool(
                        archive_config.get("store_attachments", False)
                    ),
                    "attachment_size_limit_mb": int(
                        archive_config.get("attachment_size_limit_mb", 25)
                    ),
                    "stored_bytes": None,
                    "limit_bytes": 1024**3,
                }
            ),
        }
