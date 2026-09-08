"""
cogs.moderation.logger
~~~~~~~~~~~~~~~~~~~~~~~

A cog for logging event related actions to a text channel(s)

:copyright: (C) 2020-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import json
import re
import traceback
from contextlib import suppress
from copy import copy, deepcopy
from datetime import datetime, timezone, timedelta
from io import BytesIO
from os import path
from time import time, monotonic
from typing import *
from uuid import uuid4

from PIL import Image
from aiohttp.client_exceptions import ClientOSError
from discord import (
    Object,
    Color,
    Emoji,
    Sticker,
    Embed,
    File,

    Message,
    User,
    Member,
    Role,
    Guild,

    ChannelType,
    TextChannel,
    VoiceChannel,
    ForumChannel,
    Thread,

    Interaction,
    ButtonStyle,
    AllowedMentions,
    ui,

    AuditLogAction,
    AuditLogAction as Action,
    AuditLogDiff,

    ContentFilter,
    NotificationLevel,

    NotFound,
    Forbidden,
    HTTPException,

    utils
)
from discord.ext import commands
from discord.ext.commands import Context
from discord.utils import utcnow

from botutils import (
    split,
    GetChoice,
    GetConfirmation,
    Cooldown,
    emojis,
    format_date,
    s,
    url_from
)
from botutils.colors import *
from botutils.log_archive import (
    DEFAULT_RETENTION_DAYS,
    MAX_ATTACHMENT_BYTES,
    MAX_GUILD_BYTES,
    MAX_MESSAGE_ATTACHMENT_BYTES,
    MAX_RETENTION_DAYS,
    LocalLogArchive,
)
from checks.exceptions import IgnoredExit
from cogs.moderation.logger_ui import LoggerMenu

default_config = {
    "channel": 1234,
    "channels": {},
    "secure": False,
    "ignored_roles": [],
    "ignored_channels": [],
    "ignored_bots": [],
    "disabled": [],
    "theme": None,
    "local_archive": {
        "enabled": False,
        "retention_days": DEFAULT_RETENTION_DAYS,
        "cache_messages": True,
        "store_attachments": False,
        "attachment_size_limit_mb": 25,
    },
}

# Gateway audit entries arrive without a guaranteed ordering relative to the
# event they describe. Keep a tiny shared window so event handlers can prefer
# the exact gateway entry, then fall back to a targeted REST lookup.
audit_entry_cache: Dict[int, list] = {}
audit_rest_tasks: Dict[int, asyncio.Task] = {}
audit_rest_cache: Dict[int, tuple] = {}
AUDIT_REST_CACHE_SECONDS = 2.0
MAX_AUDIT_REST_CACHE_GUILDS = 4096


async def shared_audit_entries(guild: Guild, cutoff: datetime) -> tuple:
    """Share one REST page, including misses, across a guild's event burst."""
    cached = audit_rest_cache.get(guild.id)
    if cached is not None and cached[0] > monotonic():
        return cached[1]
    task = audit_rest_tasks.get(guild.id)
    if task is None:
        async def fetch():
            try:
                try:
                    entries = tuple([
                        entry async for entry in guild.audit_logs(limit=25, after=cutoff)
                    ])
                except (NotFound, Forbidden, HTTPException, ClientOSError):
                    entries = ()
                # Keep failure/miss caching bounded even across many guilds.
                if len(audit_rest_cache) >= MAX_AUDIT_REST_CACHE_GUILDS:
                    audit_rest_cache.pop(next(iter(audit_rest_cache)))
                audit_rest_cache[guild.id] = (
                    monotonic() + AUDIT_REST_CACHE_SECONDS, entries,
                )
                return entries
            finally:
                audit_rest_tasks.pop(guild.id, None)

        task = asyncio.create_task(fetch())
        audit_rest_tasks[guild.id] = task
    # Cancelling one event must not cancel every other event's lookup.
    return await asyncio.shield(task)


custom_emoji_pattern = re.compile(r"<a?:[A-Za-z0-9_]+:[0-9]+>")


def format_diff_block(content: str, marker: str) -> str:
    """Format untrusted message content without allowing it to close the fence."""
    safe_content = content.replace("```", "``\u200b`")
    return "\n".join(f"{marker} {line}" for line in safe_content.split("\n"))


def format_emoji_diff_block(content: str, marker: str) -> str:
    """Format a diff outside code fences so Discord custom emojis can render."""
    symbol = "➖" if marker == "-" else "➕"
    safe_content = utils.escape_mentions(utils.escape_markdown(content))
    return "\n".join(
        f"> {symbol} {line or chr(8203)}"
        for line in safe_content.split("\n")
    )


def format_message_edit_diff(before: str, after: str) -> str:
    """Use colored code diffs unless either version contains a custom emoji."""
    if custom_emoji_pattern.search(before) or custom_emoji_pattern.search(after):
        return (
            f"\n\n**From:**\n{format_emoji_diff_block(before, '-')}"
            f"\n**To:**\n{format_emoji_diff_block(after, '+')}"
        )
    return (
        f"\n\n**From:**```diff\n{format_diff_block(before, '-')}\n```"
        f"\n**To:**```diff\n{format_diff_block(after, '+')}\n```"
    )


def count_message_lines(content: str) -> int:
    """Count the visible text lines in message content."""
    return content.count("\n") + 1 if content else 0


def format_member_role_action(actor_mention: Optional[str], verb: str, roles) -> str:
    """Describe a role change as a compact actor-first action."""
    role_mentions = ", ".join(role.mention for role in roles)
    if actor_mention:
        return f"{actor_mention} {verb.lower()} {role_mentions}"
    return f"{verb.title()} {role_mentions}"


def message_author_role_line(author: Union[User, Member]) -> str:
    """Describe a cached member's top role, if member data is available."""
    top_role = getattr(author, "top_role", None)
    if top_role is None:
        return ""
    return (
        f"{emojis.reply} Role: {top_role.mention} "
        f"{emojis.members}{len(top_role.members)}\n\n"
    )


def message_edit_time(message: Message, fallback: Optional[datetime] = None) -> datetime:
    """Return Discord's edit time, or the listener time when it is absent."""
    return message.edited_at or fallback or utcnow()


def poll_question_text(poll: Any) -> str:
    """Return a poll question across discord.py's string/object representations."""
    question = getattr(poll, "question", "")
    return str(getattr(question, "text", question))


def message_type_label(message_type: Any) -> Optional[str]:
    """Return a friendly label for non-default Discord message types."""
    name = getattr(message_type, "name", None)
    if not name or name == "default":
        return None
    return {
        "chat_input_command": "Slash Command",
        "context_menu_command": "Context Menu Command",
        "interaction_premium_upsell": "Premium Interaction",
    }.get(name, name.replace("_", " ").title())


def _component_payload(component: Any) -> dict:
    """Return a stable, JSON-safe payload for a Discord message component."""
    if isinstance(component, dict):
        return deepcopy(component)
    to_dict = getattr(component, "to_dict", None)
    if callable(to_dict):
        with suppress(TypeError, ValueError):
            payload = to_dict()
            if isinstance(payload, dict):
                return payload
    return {}


def message_component_payloads(components) -> list:
    """Serialize top-level message components for comparison and recovery."""
    return [
        payload
        for component in (components or [])
        if (payload := _component_payload(component))
    ]


def is_component_only_message_update(payload: dict) -> bool:
    """Return whether a raw message update only changes Discord components."""
    return "components" in payload and not any(
        field in payload
        for field in ("content", "embeds", "attachments", "poll")
    )


def _button_payloads(value: Any):
    """Yield every button payload, including buttons nested in action rows."""
    if isinstance(value, list):
        for item in value:
            yield from _button_payloads(item)
        return
    if not isinstance(value, dict):
        return
    component_type = getattr(value.get("type"), "value", value.get("type"))
    if component_type == 2:
        yield value
        return
    for item in value.values():
        if isinstance(item, (dict, list)):
            yield from _button_payloads(item)


def message_button_payloads(components) -> list:
    """Return stable payloads for all buttons in a message or saved snapshot."""
    return list(_button_payloads(message_component_payloads(components)))


def _safe_component_text(value: Any, limit: int = 512) -> str:
    text = str(value or "").replace("`", "\u02cb").replace("\r", " ").replace("\n", " ")
    return utils.escape_mentions(utils.escape_markdown(text))[:limit]


def _button_emoji(payload: dict) -> str:
    emoji = payload.get("emoji")
    if not isinstance(emoji, dict):
        return _safe_component_text(emoji, 100)
    raw_name = str(emoji.get("name") or "")
    emoji_id = emoji.get("id")
    if not emoji_id:
        return _safe_component_text(raw_name, 100)
    name = re.sub(r"[^A-Za-z0-9_]", "", raw_name)[:100]
    prefix = "a" if emoji.get("animated") else ""
    return f"<{prefix}:{name or '_'}:{emoji_id}>"


def format_message_buttons(components) -> list:
    """Render button labels and their actionable internals for logger fields."""
    style_names = {
        1: "Primary",
        2: "Secondary",
        3: "Success",
        4: "Danger",
        5: "Link",
        6: "Premium",
    }
    rendered = []
    for index, payload in enumerate(message_button_payloads(components), start=1):
        style_value = getattr(payload.get("style"), "value", payload.get("style"))
        style = style_names.get(style_value, f"Style {style_value or 'unknown'}")
        emoji = _button_emoji(payload)
        label = _safe_component_text(payload.get("label"), 100)
        title = " ".join(part for part in (emoji, label) if part) or "Unlabelled button"
        parts = [f"**{index}. {title}**", style]
        if payload.get("url"):
            parts.append(f"URL: {_safe_component_text(payload['url'])}")
        if payload.get("custom_id"):
            parts.append(f"Custom ID: `{_safe_component_text(payload['custom_id'], 150)}`")
        if payload.get("sku_id"):
            parts.append(f"SKU ID: `{_safe_component_text(payload['sku_id'], 30)}`")
        parts.append("Disabled" if payload.get("disabled") else "Enabled")
        rendered.append(" | ".join(parts))
    return rendered


class Images:
    pencil = "https://cdn.discordapp.com/attachments/632084935506788385/926729853984727090/pencil-png-643.png"
    trash = "https://cdn4.iconfinder.com/data/icons/social-messaging-ui-coloricon-1/21/52-512.png"
    create = "https://cdn3.iconfinder.com/data/icons/rest/30/add_order-512.png"
    text_channel = "https://cdn.discordapp.com/emojis/679179620867899412.webp?size=160&quality=lossless"
    voice_channel = "https://cdn.discordapp.com/emojis/679179727994617881.webp?size=160&quality=lossless"


def is_guild_owner():
    async def predicate(ctx: Context):
        has_perms = ctx.author.id == ctx.guild.owner.id
        if not has_perms:
            await ctx.send("You need to be the owner of the server to use this")
        return has_perms

    return commands.check(predicate)


def chain(obj: Union[list, str] = None, *args, skip_first=True) -> str:
    """ Chain replies an embed description """
    if isinstance(obj, str):
        obj = [obj]
    rows = [line for row in obj for line in row.split("\n")]
    last_index = len(rows) - 1
    return "".join(
        row
        if skip_first and index == 0
        else f"\n{emojis.reply if index == last_index else emojis.creply} {row}"
        for index, row in enumerate(rows)
    )


def get_avatar(user: Union[Member, User]) -> Optional[str]:
    if not user.avatar:
        return user.display_avatar.url
    return user.avatar.url


def overwrite_target_name(guild: Guild, target) -> str:
    """Resolve an overwrite target without assuming it is still cached."""
    resolved = (
        guild.get_role(target.id)
        or guild.get_member(target.id)
        or target
    )
    return getattr(resolved, "name", f"Unknown target ({target.id})")


def profile_value_key(attribute: str, value: Any) -> Any:
    """Return stable profile data instead of comparing Discord wrappers by identity."""
    if attribute == "primary_guild" and value is not None:
        primary_guild = (
            getattr(value, "id", None),
            getattr(value, "tag", None),
            getattr(value, "identity_enabled", None),
            str(getattr(value, "badge", None) or ""),
        )
        if not any(primary_guild):
            return None
        return primary_guild
    if attribute in {"avatar", "banner", "avatar_decoration"}:
        return str(value or "")
    if attribute == "accent_color":
        return getattr(value, "value", value)
    return value


def format_profile_value(attribute: str, value: Any, link_label: str) -> str:
    """Render Discord profile values as compact, readable embed content."""
    if value is None:
        return "Not set"
    if attribute in {"avatar", "banner", "avatar_decoration"}:
        url = str(value)
        return (
            f"[{link_label}]({url})"
            if url.startswith(("https://", "http://"))
            else link_label
        )
    if attribute == "primary_guild":
        tag = getattr(value, "tag", None)
        enabled = getattr(value, "identity_enabled", None)
        badge = str(getattr(value, "badge", None) or "")
        if not tag and not enabled and not badge:
            return "Not set"
        parts = [f"`{str(tag).replace('`', 'ˋ')}`" if tag else "Unknown tag"]
        if enabled is not None:
            parts.append("Visible" if enabled else "Hidden")
        if badge.startswith(("https://", "http://")):
            parts.append(f"[Badge]({badge})")
        return " • ".join(parts)
    if attribute == "accent_color":
        color = getattr(value, "value", value)
        try:
            return f"`#{int(color):06X}`"
        except (TypeError, ValueError):
            pass
    return f"`{str(value).replace('`', 'ˋ')}`"


def format_profile_change(attribute: str, before: Any, after: Any) -> str:
    return (
        f"{format_profile_value(attribute, before, 'Previous')} → "
        f"{format_profile_value(attribute, after, 'Current')}"
    )


class AuditLogSearch:
    def __init__(
        self,
        guild: Optional[Guild],
        *actions: AuditLogAction,
        target=None,
        channel=None,
        message_id=None,
    ):
        self.guild = guild
        self.actions = actions
        self.expected_target_id = getattr(target, "id", target)
        self.expected_channel_id = getattr(channel, "id", channel)
        self.expected_message_id = getattr(message_id, "id", message_id)

        # Awaited vars
        self.action: Optional[AuditLogAction] = None
        self.entry_id: Optional[int] = None
        self.created_at: Optional[datetime] = None

        self.user: Optional[Union[User, Member]] = None
        self.user_mention: str = "Unknown-User"
        self.user_avatar: Optional[str] = None
        self.user_display_avatar: Optional[str] = None

        self.target: Optional[Any] = None
        self.target_mention: str = "Unknown-User"
        self.target_avatar: Optional[str] = None
        self.target_display_avatar: Optional[str] = None

        self.reason: Optional[str] = None
        self.extra: Optional[Any] = None
        self.before: Optional[AuditLogDiff] = None
        self.after: Optional[AuditLogDiff] = None

    def __await__(self) -> Generator[Any, None, "AuditLogSearch"]:
        """ Fills in the search results """
        return self._search().__await__()

    def _matches(self, entry, cutoff: datetime) -> bool:
        if entry.created_at <= cutoff or entry.action not in self.actions:
            return False
        target_id = getattr(entry.target, "id", None)
        if self.expected_target_id is not None and target_id != self.expected_target_id:
            return False
        if self.expected_channel_id is not None:
            extra_channel = getattr(entry.extra, "channel", None)
            entry_channel_id = getattr(extra_channel, "id", None)
            if entry_channel_id is None and isinstance(
                entry.target, (TextChannel, VoiceChannel, ForumChannel, Thread)
            ):
                entry_channel_id = entry.target.id
            if entry_channel_id != self.expected_channel_id:
                return False
        if self.expected_message_id is not None:
            entry_message_id = getattr(entry.extra, "message_id", None)
            if entry_message_id != self.expected_message_id:
                return False
        return True

    def _apply(self, entry) -> None:
        self.action = entry.action
        self.entry_id = entry.id
        self.created_at = entry.created_at
        if entry.user:
            self.user = entry.user
            self.user_mention = entry.user.mention
            self.user_avatar = get_avatar(entry.user)
            self.user_display_avatar = entry.user.display_avatar.url
        self.target = entry.target
        if isinstance(entry.target, (User, Member)):
            self.target_mention = entry.target.mention
            self.target_avatar = get_avatar(entry.target)
            self.target_display_avatar = entry.target.display_avatar.url
        self.reason = entry.reason
        self.extra = entry.extra
        self.before = entry.before
        self.after = entry.after

    async def _search(self) -> "AuditLogSearch":
        if (
            self.guild is None
            or self.guild.me is None
            or not self.guild.me.guild_permissions.view_audit_log
        ):
            return self
        cutoff = Logger.past()
        # Discord does not guarantee whether the domain event or its audit
        # entry reaches us first. Give the gateway cache one short chance to
        # catch up before making a REST request.
        for attempt in range(2):
            cached_entries = audit_entry_cache.get(self.guild.id, [])
            for entry in reversed(cached_entries):
                if self._matches(entry, cutoff):
                    self._apply(entry)
                    return self
            if attempt == 0:
                await asyncio.sleep(0.35)
        for entry in await shared_audit_entries(self.guild, cutoff):
            if not self._matches(entry, cutoff):
                continue
            self._apply(entry)
            break
        return self


class Log(object):
    """ A dataclass-like object for sorting information """
    def __init__(
        self,
        log_type,
        embed=None,
        embeds=None,
        files=None,
        file=None,
        created_at=None,
        *,
        actor=None,
        target=None,
        channel=None,
        reason=None,
        details=None,
        links=None,
        actor_label="Actor",
        target_label="Subject",
    ):
        """
        :param str log_type:        The category the log goes into
        :param Embed embed:         The embed to send to the log channel
        :param List[Embed] embeds:  The embeds to send in one msg
        :param List[File] files:    Optional list of files to send alongside the embed
        :param File file:           Optional file to attach
        :param float created_at:    When the log event occured
        """
        files = list(files or [])
        if file:
            files.append(file)
        self.type = log_type
        self.embeds = list(embeds or [])
        if embed:
            self.embeds.append(embed)
        self.files = files
        self.created_at = created_at if created_at is not None else time()
        self.actor = actor
        self.target = target
        self.channel = channel
        self.reason = reason
        self.details = dict(details or {})
        self.links = list(links or [])
        self.actor_label = actor_label
        self.target_label = target_label
        self.archive_key = uuid4().hex
        self.archive_staged = False
        self.collapse_signature = None


class LoggerHealthView(ui.View):
    """Refreshable logger health display bound to the command author."""
    def __init__(self, logger: "Logger", author_id: int, guild_id: Optional[str] = None):
        super().__init__(timeout=300)
        self.logger = logger
        self.author_id = author_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "This dashboard belongs to someone else", ephemeral=True
        )
        return False

    @ui.button(label="Refresh", emoji="🔄", style=ButtonStyle.blurple)
    async def refresh(self, interaction: Interaction, _button):
        if self.guild_id is None:
            embed = self.logger.build_dashboard_embed()
        else:
            embed = self.logger.build_health_embed(self.guild_id)
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="Close", emoji="✖️", style=ButtonStyle.red)
    async def close(self, interaction: Interaction, _button):
        self.stop()
        await interaction.response.edit_message(view=None)

    async def on_error(self, interaction: Interaction, error: Exception, _item) -> None:
        self.logger.bot.log.critical(
            "Logger dashboard error:\n"
            f"{''.join(traceback.format_exception(type(error), error, error.__traceback__))}"
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "The dashboard couldn't refresh", ephemeral=True
            )


class Logger(commands.Cog):
    queue_size = 256
    queue_batch_window = 3
    queue_full_alert_interval = 60
    message_fetch_spacing = 1.1
    message_fetch_max_retry_delay = 15
    reload_teardown_timeout = 2
    permission_queue_disable_delay = 24 * 60 * 60
    recent_log_limit = 500
    emoji_actor_ttl = 30
    emoji_actor_limit = 25
    dashboard_guild_id = 397415086295089155
    threaded_log_guilds = {
        397415086295089155,
        523678393565315077,
        850956124168519700,
    }

    categories = {
        "Mentions": [
            "role_mention",
        ],
        "Message Edit": [
            "message_edit"
        ],
        "Message Delete": [
            "message_delete",
            "attachment_delete",
            "message_purge"
        ],
        "Message Update": [
            "embed_hidden",
            "attachment_update",
            "reactions_clear",
            "message_pin"
        ],
        "Server Update": [
            "server_rename",
            "server_settings",
            "new_server_icon",
            "new_server_banner",
            "new_server_splash",
            "afk_timeout_change",
            "afk_channel_change",
            "owner_change",
            "features_change",
            "new_boost_tier",
            "boost",
            "system_channel",
            "system_channel_flags",
            "2fa_update",
            "verification_level",
            "explicit_filter",
            "default_notifications"
        ],
        "Channel Update": [
            "channel_create",
            "channel_delete",
            "channel_rename",
            "channel_move",
            "channel_topic",
            "channel_overwrites",
            "channel_settings",
            "tag_create",
            "tag_delete",
            "tag_update"
        ],
        "Role Update": [
            "role_create",
            "role_delete",
            "role_rename",
            "role_recolor",
            "role_icon",
            "role_visibility",
            "role_mentionable",
            "role_move",
            "role_permissions"
        ],
        "Webhook Update": [
            "webhook_update"
        ],
        "Associations": [
            "member_join",
            "member_leave",
            "member_kick",
            "member_ban",
            "member_unban",
            "bot_add"
        ],
        "Member Update": [
            "nick_change",
            "member_roles_update",
            "member_screening",
            "username_change",
            "user_profile_update",
            "new_server_avatar"
        ],
        "Emoji Update": [
            "emoji_create",
            "emoji_delete",
            "emoji_rename"
        ],
        "Sticker Update": [
            "sticker_create",
            "sticker_delete",
            "sticker_update"
        ],
        "Invite Update": [
            "invite_create",
            "invite_delete"
        ],
        "Auto Moderation": [
            "warn",
            "chat_filter",
            "timeout",
            "mute",
            "unmute",
            "automod_action",
            "automod_rule_create",
            "automod_rule_update",
            "automod_rule_delete"
        ],
        "Threads": [
            "thread_create",
            "thread_delete",
            "thread_update",
            "thread_tagged"
        ],
        "Ghost Typing": [
            "ghost_typing"
        ],
        "Scheduled Events": [
            "scheduled_event_create",
            "scheduled_event_update",
            "scheduled_event_delete"
        ],
        "Stage Events": [
            "stage_create",
            "stage_update",
            "stage_delete"
        ],
        "Soundboard": [
            "soundboard_create",
            "soundboard_update",
            "soundboard_delete"
        ]
    }

    category_icons = {
        "Mentions": "🔔",
        "Message Edit": "✏️",
        "Message Delete": "🗑️",
        "Message Update": "📌",
        "Server Update": "🏠",
        "Channel Update": "#️⃣",
        "Role Update": "🛡️",
        "Webhook Update": "🪝",
        "Associations": "👋",
        "Member Update": "👤",
        "Emoji Update": "😀",
        "Sticker Update": "🏷️",
        "Invite Update": "✉️",
        "Auto Moderation": "⚖️",
        "Threads": "🧵",
        "Ghost Typing": "👻",
        "Scheduled Events": "🗓️",
        "Stage Events": "🎙️",
        "Soundboard": "🔊",
    }

    log_types = []
    for events in categories.values():
        log_types.extend(events)
    log_types = sorted(log_types)

    def __init__(self, bot):
        self.bot = bot

        bot.tasks.setdefault("logger", {})

        self.config = {}
        self.path = "./data/userdata/secure-log.json"
        if path.isfile(self.path):
            with open(self.path, "r") as f:
                self.config = json.load(f)  # type: dict
        self._config_migration_pending = self._normalize_config()

        shared_archive = getattr(self.bot, "_local_log_archive", None)
        if (
            shared_archive is not None
            and not shared_archive._closing
            and shared_archive.task is not None
            and not shared_archive.task.done()
        ):
            self.local_archive = shared_archive
            self.local_archive.report_error = self._report_archive_error
        else:
            if shared_archive is not None:
                self.bot.loop.create_task(
                    shared_archive.close(), name="logging:retired-local-archive"
                )
            self.local_archive = LocalLogArchive(
                self.bot.get_fp_for("userdata/logging/archive.sqlite3"),
                report_error=self._report_archive_error,
            )
            self.local_archive.start(self.bot.loop)
            self.bot._local_log_archive = self.local_archive

        self.queue = {
            guild_id: asyncio.Queue(maxsize=self.queue_size)
            for guild_id in self.config
        }
        self.health_started_at = time()
        self.health = {
            guild_id: self._new_health_record()
            for guild_id in self.config
        }
        self.queue_full_alerts = {}
        self.recent_logs = {
            guild_id: [] for guild_id in self.config.keys()
        }
        self.last_log_deliveries = {
            guild_id: {} for guild_id in self.config.keys()
        }

        self.pool = {}
        self.permission_queue_watchdogs = {}
        self.typing = {}
        self.username_cd = Cooldown(1, 5)
        self.channel_moved_cd = Cooldown(1, 2)
        self.role_moved_cd = Cooldown(1, 5)
        self.emoji_command_actors = {}
        self.recent_member_bans = {}
        self.cycle = {}  # RGB theme
        self.colors = {}

        self.invites = {}
        self.invite_init_task = None
        self.unavailable_channel_notified = set()
        self.message_fetch_locks = {}
        self.message_fetch_rate_limit_alerts = {}

    async def cog_load(self) -> None:
        started_at = time()
        if self._config_migration_pending:
            await self.save_data()
            self._config_migration_pending = False
        if self.bot.is_ready():
            for guild_id in self.config:
                self.start_worker(guild_id)
            self.start_invite_initialization()
        self.bot.log.info(
            f"Logger cog load completed in {round((time() - started_at) * 1000)}ms"
        )

    def record_emoji_actor(self, guild_id: int, action: str, target: str, user) -> None:
        """Remember who requested an emoji API action performed by Fate."""
        self._prune_emoji_actor_cache()
        key = (guild_id, action)
        pending = self.emoji_command_actors.setdefault(key, [])
        pending.append((str(target), user.id, time()))
        self.emoji_command_actors[key] = pending[-self.emoji_actor_limit:]

    def _prune_emoji_actor_cache(self, current_time=None) -> None:
        current_time = current_time or time()
        for key, entries in list(self.emoji_command_actors.items()):
            active = [
                entry for entry in entries
                if current_time - entry[2] < self.emoji_actor_ttl
            ]
            if active:
                self.emoji_command_actors[key] = active
            else:
                self.emoji_command_actors.pop(key, None)

    def pop_emoji_actor(self, guild_id: int, action: str, *targets):
        key = (guild_id, action)
        now = time()
        self._prune_emoji_actor_cache(now)
        pending = [
            entry for entry in self.emoji_command_actors.get(key, [])
            if now - entry[2] < self.emoji_actor_ttl
        ]
        target_values = {str(target) for target in targets}
        for index, (target, user_id, _created_at) in enumerate(pending):
            if target in target_values:
                pending.pop(index)
                if pending:
                    self.emoji_command_actors[key] = pending
                else:
                    self.emoji_command_actors.pop(key, None)
                guild = self.bot.get_guild(guild_id)
                return (guild.get_member(user_id) if guild else None) or self.bot.get_user(user_id)
        return None

    def _normalize_config(self) -> bool:
        """Migrate stored guild configs and fill in newly added options."""
        original = deepcopy(self.config)
        for guild_id, config in list(self.config.items()):
            if not isinstance(config, dict):
                del self.config[guild_id]
                continue

            config.pop("themes", None)
            if "disabled" not in config and "disabled_logs" in config:
                config["disabled"] = config.pop("disabled_logs")
            for key, value in default_config.items():
                config.setdefault(key, deepcopy(value))
            disabled = config.get("disabled")
            if not isinstance(disabled, list):
                disabled = []
                config["disabled"] = disabled
            config["disabled"] = [
                event for event in disabled if event != "component_update"
            ]
            channels = config.get("channels")
            if isinstance(channels, dict):
                channels.pop("component_update", None)
            for key in ("ignored_channels", "ignored_bots"):
                values = config.get(key)
                if not isinstance(values, list):
                    values = []
                config[key] = list(dict.fromkeys(
                    snowflake
                    for value in values
                    if (snowflake := self.snowflake_id(value)) is not None
                ))
            archive = config.get("local_archive")
            if not isinstance(archive, dict):
                archive = deepcopy(default_config["local_archive"])
                config["local_archive"] = archive
            archive["enabled"] = (
                archive.get("enabled")
                if type(archive.get("enabled")) is bool
                else False
            )
            archive["cache_messages"] = (
                archive.get("cache_messages")
                if type(archive.get("cache_messages")) is bool
                else True
            )
            archive["store_attachments"] = (
                archive.get("store_attachments")
                if type(archive.get("store_attachments")) is bool
                else False
            )
            try:
                attachment_limit_value = archive.get("attachment_size_limit_mb", 25)
                if isinstance(attachment_limit_value, bool) or (
                    isinstance(attachment_limit_value, float)
                    and not attachment_limit_value.is_integer()
                ):
                    raise ValueError
                attachment_limit = int(attachment_limit_value)
            except (TypeError, ValueError):
                attachment_limit = 25
            archive["attachment_size_limit_mb"] = min(
                max(attachment_limit, 1), MAX_ATTACHMENT_BYTES // 1024**2
            )
            try:
                retention_days = int(
                    archive.get("retention_days", DEFAULT_RETENTION_DAYS)
                )
            except (TypeError, ValueError):
                retention_days = DEFAULT_RETENTION_DAYS
            archive["retention_days"] = min(
                max(retention_days, 1), MAX_RETENTION_DAYS
            )
        return self.config != original

    def _report_archive_error(self, message: str) -> None:
        self.bot.log.critical(message)

    @staticmethod
    def _new_health_record() -> dict:
        return {
            "worker_started_at": None,
            "last_success": None,
            "last_error": None,
            "last_error_at": None,
            "sent": 0,
            "failed": 0,
            "dropped": 0,
            "restarts": 0,
            "consecutive_failures": 0,
            "archive_dropped": 0,
            "permission_queue_full_at": None,
        }

    def get_health_record(self, guild_id: str) -> dict:
        return self.health.setdefault(guild_id, self._new_health_record())

    def record_delivery_failure(self, guild_id: str, error: str) -> None:
        health = self.get_health_record(guild_id)
        health["failed"] += 1
        health["consecutive_failures"] += 1
        health["last_error"] = str(error)[:1000]
        health["last_error_at"] = time()

    def record_delivery_success(self, guild_id: str) -> None:
        health = self.get_health_record(guild_id)
        health["sent"] += 1
        health["consecutive_failures"] = 0
        health["last_success"] = time()

    def queue_full_alert_message(
        self,
        guild_id: str,
        log_type: str,
        snapshot: dict,
        *,
        now: Optional[float] = None,
    ) -> Optional[str]:
        """Aggregate queue overflow alerts so one burst cannot ping repeatedly."""
        now = time() if now is None else now
        alerts = getattr(self, "queue_full_alerts", None)
        if alerts is None:
            alerts = self.queue_full_alerts = {}
        state = alerts.setdefault(
            guild_id,
            {"last_alert_at": None, "dropped": 0, "types": {}},
        )
        state["dropped"] += 1
        state["types"][log_type] = state["types"].get(log_type, 0) + 1

        last_alert_at = state["last_alert_at"]
        if (
            last_alert_at is not None
            and now - last_alert_at < self.queue_full_alert_interval
        ):
            return None

        dropped = state["dropped"]
        type_counts = ", ".join(
            f"{name}={count}"
            for name, count in sorted(state["types"].items())
        )
        state["last_alert_at"] = now
        state["dropped"] = 0
        state["types"] = {}
        event_label = "event" if dropped == 1 else "events"
        return (
            f"Logger queue is full for guild {guild_id}; dropped {dropped} "
            f"{event_label} ({type_counts}). Queue "
            f"{snapshot['queue_size']}/{snapshot['queue_capacity']}; "
            f"worker {'running' if snapshot['worker_running'] else 'stopped'}. "
            f"Further overflow alerts are grouped for "
            f"{self.queue_full_alert_interval} seconds."
        )

    def get_worker_snapshot(self, guild_id: str) -> dict:
        """Return a point-in-time queue and worker health snapshot."""
        health = self.get_health_record(guild_id)
        queue = self.queue.get(guild_id)
        task = self.bot.tasks.get("logger", {}).get(guild_id)
        blocked_permissions = [
            permission
            for (pool_guild_id, permission, _channel_id), pool_task in self.pool.items()
            if pool_guild_id == guild_id and not pool_task.done()
        ]

        queue_size = queue.qsize() if queue else 0
        queue_capacity = queue.maxsize if queue else self.queue_size
        queue_ratio = queue_size / queue_capacity if queue_capacity else 0

        if queue is None or task is None or task.cancelled() or task.done():
            status = "down"
        elif blocked_permissions:
            status = "blocked"
        elif (
            queue_ratio >= 0.75
            or health["consecutive_failures"] >= 3
            or (
                health["restarts"] >= 3
                and health["last_error_at"]
                and time() - health["last_error_at"] < 300
            )
        ):
            status = "degraded"
        else:
            status = "healthy"

        return {
            "guild_id": guild_id,
            "status": status,
            "queue_size": queue_size,
            "queue_capacity": queue_capacity,
            "queue_ratio": queue_ratio,
            "worker_running": bool(task and not task.done()),
            "blocked_permissions": sorted(set(blocked_permissions)),
            **health,
        }

    def start_worker(self, guild_id: str) -> asyncio.Task:
        """Start one queue consumer for a guild, unless one is already running."""
        existing = self.bot.tasks["logger"].get(guild_id)
        if existing and not existing.done():
            return existing

        task = self.bot.loop.create_task(
            self.start_queue(guild_id), name=f"logger:{guild_id}"
        )
        self.bot.tasks["logger"][guild_id] = task
        self.get_health_record(guild_id)["worker_started_at"] = time()
        task.add_done_callback(
            lambda finished, gid=guild_id: self._worker_finished(gid, finished)
        )
        return task

    def _worker_finished(self, guild_id: str, task: asyncio.Task) -> None:
        """Report unexpected worker exits and restart the consumer."""
        if self.bot.tasks["logger"].get(guild_id) is not task:
            return
        self.bot.tasks["logger"].pop(guild_id, None)
        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return
        health = self.get_health_record(guild_id)
        health["restarts"] += 1
        self.record_delivery_failure(guild_id, f"Worker crashed: {error}")
        self.bot.log.critical(
            f"Logger worker crashed for guild {guild_id}:\n"
            f"{''.join(traceback.format_exception(type(error), error, error.__traceback__))}"
        )
        if guild_id in self.config and guild_id in self.queue and not self.bot.is_closed():
            self.start_worker(guild_id)

    def stop_worker(self, guild_id: str) -> None:
        task = self.bot.tasks["logger"].pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    def stop_permission_queue_watchdog(self, guild_id: str) -> None:
        task = self.permission_queue_watchdogs.pop(guild_id, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def start_invite_initialization(self) -> asyncio.Task:
        """Refresh invite counters without blocking extension load or reload."""
        if self.invite_init_task and not self.invite_init_task.done():
            return self.invite_init_task
        self.invite_init_task = self.bot.loop.create_task(
            self.init_invites(), name="logger:invite-initialization"
        )
        self.invite_init_task.add_done_callback(self._invite_initialization_finished)
        return self.invite_init_task

    def _invite_initialization_finished(self, task: asyncio.Task) -> None:
        if self.invite_init_task is task:
            self.invite_init_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.bot.log.warning(f"Logger invite initialization failed: {error}")

    def start_permission_queue_watchdog(self, guild_id: str) -> asyncio.Task:
        """Start the one-day grace period after a permission-blocked queue fills."""
        existing = self.permission_queue_watchdogs.get(guild_id)
        if existing and not existing.done():
            return existing

        health = self.get_health_record(guild_id)
        health["permission_queue_full_at"] = time()
        task = self.bot.loop.create_task(
            self._disable_if_permissions_still_missing(guild_id),
            name=f"logger-permission-watchdog:{guild_id}",
        )
        self.permission_queue_watchdogs[guild_id] = task
        task.add_done_callback(
            lambda finished, gid=guild_id: self._permission_watchdog_finished(
                gid, finished
            )
        )
        return task

    def _permission_watchdog_finished(
        self, guild_id: str, task: asyncio.Task
    ) -> None:
        if self.permission_queue_watchdogs.get(guild_id) is task:
            self.permission_queue_watchdogs.pop(guild_id, None)
            health = self.health.get(guild_id)
            if health is not None:
                health["permission_queue_full_at"] = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.bot.log.critical(
                f"Logger permission watchdog crashed for guild {guild_id}:\n"
                f"{''.join(traceback.format_exception(type(error), error, error.__traceback__))}"
            )

    async def _current_blocked_permissions(self, guild_id: str) -> list[str]:
        guild = self.bot.get_guild(int(guild_id))
        if not guild or not guild.me:
            return []

        missing = []
        for (pool_guild_id, permission, channel_id), task in list(self.pool.items()):
            if pool_guild_id != guild_id or task.done():
                continue
            if channel_id is None:
                permissions = guild.me.guild_permissions
            else:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.get_or_fetch(channel_id)
                if channel is None:
                    missing.append(permission)
                    continue
                permissions = channel.permissions_for(guild.me)
            if not getattr(permissions, permission, False):
                missing.append(permission)
        return sorted(set(missing))

    async def _disable_if_permissions_still_missing(self, guild_id: str) -> None:
        await asyncio.sleep(self.permission_queue_disable_delay)
        if guild_id not in self.config:
            return
        missing = await self._current_blocked_permissions(guild_id)
        if not missing:
            return
        if await self.disable_for_guild(int(guild_id)):
            self.bot.log.warning(
                f"Disabled Logger for guild {guild_id} after its queue remained "
                f"permission-blocked for 24 hours ({', '.join(missing)})"
            )

    async def cog_unload(self):
        started_at = time()
        audit_tasks = list(audit_rest_tasks.values())
        for task in audit_tasks:
            task.cancel()
        audit_rest_cache.clear()
        invite_init_task = self.invite_init_task
        if invite_init_task and not invite_init_task.done():
            invite_init_task.cancel()
        worker_tasks = list(self.bot.tasks["logger"].values())
        for guild_id in list(self.bot.tasks["logger"]):
            self.stop_worker(guild_id)
        for guild_id in list(audit_entry_cache):
            if str(guild_id) in self.config:
                audit_entry_cache.pop(guild_id, None)
        permission_tasks = list(self.pool.values())
        for task in permission_tasks:
            if not task.done():
                task.cancel()
        self.pool.clear()
        watchdog_tasks = list(self.permission_queue_watchdogs.values())
        for guild_id in list(self.permission_queue_watchdogs):
            self.stop_permission_queue_watchdog(guild_id)
        teardown_tasks = [
            *audit_tasks,
            *worker_tasks,
            *permission_tasks,
            *watchdog_tasks,
        ]
        if invite_init_task:
            teardown_tasks.append(invite_init_task)
        if teardown_tasks:
            done, pending = await asyncio.wait(
                teardown_tasks,
                timeout=self.reload_teardown_timeout,
            )
            for task in done:
                with suppress(asyncio.CancelledError):
                    task.exception()
            if pending:
                self.bot.log.debug(
                    f"Logger reload stopped waiting for {len(pending)} cancelled "
                    "background task(s) after 2 seconds"
                )
        self.bot.log.info(
            f"Logger cog unload completed in {round((time() - started_at) * 1000)}ms"
        )

    async def cog_check(self, ctx: Context) -> bool:
        if ctx.command.parent:
            available_without_logging = {
                "logger enable",
                "logger disable",
                "logger set-channel",
                "logger config",
                "logger dashboard",
                "logger archive clear",
            }
            if ctx.command.qualified_name not in available_without_logging:
                guild_id = str(ctx.guild.id)
                if guild_id not in self.config:
                    raise commands.CheckFailure("Logger isn't enabled")
            if not ctx.author.guild_permissions.manage_channels:
                raise commands.MissingPermissions(["manage_channels"])
        return True

    def is_enabled(self, guild_id):
        return str(guild_id) in self.config

    def archive_config(self, guild_id) -> dict:
        config = self.config.get(str(guild_id), {})
        archive = config.get("local_archive")
        if not isinstance(archive, dict):
            return deepcopy(default_config["local_archive"])
        return archive

    def archive_enabled(self, guild_id) -> bool:
        return bool(self.archive_config(guild_id).get("enabled", False))

    def archive_retention(self, guild_id) -> int:
        try:
            value = int(
                self.archive_config(guild_id).get(
                    "retention_days", DEFAULT_RETENTION_DAYS
                )
            )
        except (TypeError, ValueError):
            value = DEFAULT_RETENTION_DAYS
        return min(max(value, 1), MAX_RETENTION_DAYS)

    async def enable_for_guild(self, guild_id: int, channel_id: int) -> bool:
        """Enable logging in an existing channel and start its worker."""
        guild_id = str(guild_id)
        if guild_id in self.config:
            return False
        self.config[guild_id] = deepcopy(default_config)
        self.config[guild_id]["channel"] = channel_id
        self.queue.setdefault(
            guild_id, asyncio.Queue(maxsize=self.queue_size)
        )
        self.start_worker(guild_id)
        await self.save_data()
        return True

    async def disable_for_guild(self, guild_id: int) -> bool:
        """Disable logging and stop its worker while leaving channels intact."""
        guild_id = str(guild_id)
        if guild_id not in self.config:
            return False
        self.stop_permission_queue_watchdog(guild_id)
        for pool_id, task in list(self.pool.items()):
            if pool_id[0] != guild_id:
                continue
            self.pool.pop(pool_id, None)
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        self.stop_worker(guild_id)
        del self.config[guild_id]
        self.queue.pop(guild_id, None)
        self.health.pop(guild_id, None)
        self.last_log_deliveries.pop(guild_id, None)
        await self.save_data()
        return True

    @property
    def template(self):
        return deepcopy(default_config)

    @classmethod
    def event_category(cls, log_type: str) -> str:
        for category, events in cls.categories.items():
            if log_type in events:
                return category
        return "Discord Event"

    @classmethod
    def has_log_value(cls, value: Any) -> bool:
        """Return whether a value is meaningful enough to deserve a field."""
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float, complex)):
            return value != 0
        if isinstance(value, str):
            normalised = (
                value.strip()
                .strip("`*_~")
                .replace("`", "")
                .strip()
                .casefold()
            )
            if normalised in {
                "",
                "0",
                "none",
                "not set",
                "unknown",
                "unavailable",
                "no reason",
                "no reason provided",
            }:
                return False
            number, separator, unit = normalised.partition(" ")
            zero_units = {
                "byte", "bytes", "second", "seconds", "minute", "minutes",
                "hour", "hours", "day", "days", "use", "uses", "vote",
                "votes", "member", "members", "message", "messages", "file",
                "files", "attachment", "attachments", "embed", "embeds",
                "sticker", "stickers", "component", "components", "role",
                "roles", "reaction", "reactions", "channel", "channels",
                "kbps", "mb", "gb",
            }
            if separator and number in {"0", "0.0", "0.00"} and unit in zero_units:
                return False
            return True
        if isinstance(value, dict):
            return any(cls.has_log_value(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(cls.has_log_value(item) for item in value)
        return True

    @classmethod
    def format_log_entity(cls, value: Any) -> Optional[str]:
        """Format a Discord object as a readable mention and name."""
        if not cls.has_log_value(value):
            return None
        if isinstance(value, (list, tuple, set)):
            values = [
                cls.format_log_entity(item)
                for item in value
                if cls.has_log_value(item)
            ]
            return "\n".join(item for item in values if item)[:1024] or None
        if isinstance(value, str):
            return value[:1024]
        if isinstance(value, bool):
            return "✅ Enabled" if value else "❌ Disabled"
        if isinstance(value, int):
            return f"`{value}`"

        mention = getattr(value, "mention", None)
        display_name = (
            getattr(value, "display_name", None)
            or getattr(value, "name", None)
            or str(value)
        )
        lines = [mention or f"**{utils.escape_markdown(str(display_name))}**"]
        identity = []
        if getattr(value, "bot", False):
            identity.append("Bot")
        if identity:
            lines.append(" • ".join(identity))
        return "\n".join(lines)[:1024]

    @classmethod
    def format_log_detail(cls, value: Any) -> str:
        if value is None:
            return "Not set"
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return f"{utils.format_dt(value, style='F')}\n{utils.format_dt(value, style='R')}"
        if isinstance(value, timedelta):
            return format_date(seconds=max(0, int(value.total_seconds())))
        if isinstance(value, bool):
            return "✅ Enabled" if value else "❌ Disabled"
        if isinstance(value, (list, tuple, set)):
            rendered = [
                cls.format_log_entity(item) or str(item)
                for item in value
                if cls.has_log_value(item)
            ]
            return "\n".join(rendered)[:1024] or "None"
        if isinstance(value, dict):
            rendered = [
                f"{name}: {cls.format_log_detail(item)}"
                for name, item in value.items()
                if cls.has_log_value(item)
            ]
            return "\n".join(rendered)[:1024] or "None"
        if hasattr(value, "mention") or hasattr(value, "id"):
            return cls.format_log_entity(value) or "Unknown"
        if hasattr(value, "name") and hasattr(value, "value"):
            return str(value.name).replace("_", " ").title()
        return str(value)[:1024]

    @staticmethod
    def attachment_log_detail(attachment) -> str:
        parts = [
            attachment.filename,
            f"{attachment.size:,} bytes",
            attachment.content_type or "unknown type",
        ]
        if attachment.width and attachment.height:
            parts.append(f"{attachment.width}×{attachment.height}")
        if attachment.is_voice_message():
            parts.append(f"voice message • {attachment.duration or 0:.1f}s")
        if attachment.is_spoiler():
            parts.append("spoiler")
        if attachment.description:
            parts.append(f"alt: {attachment.description}")
        return " • ".join(parts)

    @classmethod
    def format_message_interaction(cls, message: Message) -> Optional[str]:
        """Render interaction metadata without exposing Discord object reprs."""
        metadata = getattr(message, "interaction_metadata", None)
        legacy = getattr(message, "_interaction", None)
        interaction = metadata or legacy
        if interaction is None:
            return None

        message_type = getattr(getattr(message, "type", None), "name", None)
        interaction_type = getattr(getattr(interaction, "type", None), "name", None)
        kind = {
            "chat_input_command": "Slash command",
            "context_menu_command": "Context menu command",
            "application_command": "Application command",
            "component": "Message component",
            "modal_submit": "Modal submission",
        }.get(message_type) or {
            "application_command": "Application command",
            "component": "Message component",
            "modal_submit": "Modal submission",
        }.get(interaction_type, "Interaction")

        command_name = (
            getattr(interaction, "name", None)
            or getattr(legacy, "name", None)
        )
        if command_name:
            command_name = str(command_name).replace("`", "\u02cb")[:100]
            if message_type == "chat_input_command":
                command_name = f"/{command_name.lstrip('/')}"
            heading = f"**{kind}:** `{command_name}`"
        else:
            heading = f"**{kind}**"

        user = getattr(interaction, "user", None) or getattr(legacy, "user", None)
        user_text = cls.format_log_entity(user) if user is not None else None
        interaction_id = getattr(interaction, "id", None)
        facts = []
        if user_text:
            facts.append(f"Invoked by {user_text}")
        if interaction_id:
            facts.append(f"ID: `{interaction_id}`")

        lines = [heading]
        if facts:
            lines.append(" • ".join(facts))

        target_user = getattr(interaction, "target_user", None)
        target_message_id = getattr(interaction, "target_message_id", None)
        interacted_message_id = getattr(interaction, "interacted_message_id", None)
        if target_user:
            lines.append(f"Target: {cls.format_log_entity(target_user)}")
        elif target_message_id:
            lines.append(f"Target message: `{target_message_id}`")
        elif interacted_message_id:
            lines.append(f"Source message: `{interacted_message_id}`")
        return "\n".join(lines)[:1024]

    @staticmethod
    async def member_export_file(guild: Guild, heading: str, members) -> Optional[File]:
        """Build a useful member export without exceeding the guild upload cap."""
        members = list(members or [])
        if not members:
            return None
        upload_budget = max(1024, guild.filesize_limit - 2048)
        buffer = BytesIO()
        buffer.write(heading.encode())
        included = 0
        for index, member in enumerate(members):
            if index % 250 == 0:
                await asyncio.sleep(0)
            row = (
                f"\n{member.id}, {member.mention}, {member}, "
                f"{member.display_name}"
            ).encode()
            if buffer.tell() + len(row) > upload_budget:
                break
            buffer.write(row)
            included += 1
        omitted = len(members) - included
        if omitted:
            note = f"\n... {omitted:,} more member{s(omitted)} omitted".encode()
            if buffer.tell() + len(note) <= upload_budget:
                buffer.write(note)
        buffer.seek(0)
        return File(buffer, filename="members.txt")

    @classmethod
    def message_log_details(cls, message: Message) -> dict:
        """Return compact metadata for modern message features."""
        components = message_component_payloads(message.components)
        buttons = format_message_buttons(components)
        details = {
            "🆔 Message ID": message.id,
            "💬 Message Type": message_type_label(message.type),
            "🕒 Sent": message.created_at,
            "✏️ Last Edited": message.edited_at,
            "📎 Attachments": len(message.attachments),
            "📄 Attachment Details": [
                cls.attachment_log_detail(attachment)
                for attachment in message.attachments
            ],
            "🖼️ Embeds": len(message.embeds),
            "🏷️ Stickers": [
                sticker.name
                for sticker in message.stickers
            ],
            "🧩 Components": len(components),
            "🔘 Buttons": buttons,
            "📨 Forwarded Snapshots": len(message.message_snapshots),
            "🚩 Flags": f"`{message.flags.value}`",
        }
        if message.reference:
            details["↩️ Reply / Reference"] = (
                f"Message `{message.reference.message_id or 'Unknown'}`"
            )
        if interaction := cls.format_message_interaction(message):
            details["⚡ Interaction"] = interaction
        if message.poll:
            poll = message.poll
            details.update({
                "📊 Poll Question": poll_question_text(poll),
                "🗳️ Poll Answers": [
                    f"{answer.emoji or ''} {answer.text} • {answer.vote_count} vote{s(answer.vote_count)}"
                    for answer in poll.answers
                ],
                "🔢 Poll Votes": poll.total_votes,
                "☑️ Multiple Choice": poll.multiple,
                "⌛ Poll Expires": poll.expires_at,
                "✅ Poll Finalized": poll.is_finalized(),
            })
        return details

    @classmethod
    def message_edit_log_details(cls, message: Message) -> dict:
        """Keep edited-message fields limited to metadata outside the summary."""
        details = cls.message_log_details(message)
        for name in (
            "🆔 Message ID",
            "✏️ Last Edited",
            "🚩 Flags",
            "↩️ Reply / Reference",
        ):
            details.pop(name, None)
        return details

    @staticmethod
    def channel_log_details(channel) -> dict:
        channel_type = channel.type
        if channel_type is ChannelType.media:
            channel_type = "Media"
        details = {
            "🧭 Type": channel_type,
            "📂 Category": getattr(channel, "category", None),
            "↕️ Position": getattr(channel, "position", None),
            "🔐 Permissions Synced": getattr(channel, "permissions_synced", None),
            "🛡️ Permission Overwrites": len(getattr(channel, "overwrites", {})),
        }
        optional = (
            ("topic", "📝 Topic"),
            ("nsfw", "🔞 Age Restricted"),
            ("slowmode_delay", "⏱️ Slowmode"),
            ("default_auto_archive_duration", "🗄️ Default Auto Archive"),
            ("default_thread_slowmode_delay", "🧵 Default Thread Slowmode"),
            ("bitrate", "🎚️ Bitrate"),
            ("user_limit", "👥 User Limit"),
            ("rtc_region", "🌐 RTC Region"),
            ("video_quality_mode", "🎥 Video Quality"),
            ("default_layout", "📰 Default Layout"),
            ("default_sort_order", "↕️ Default Sort"),
            ("default_reaction_emoji", "😀 Default Reaction"),
        )
        for attribute, label in optional:
            if hasattr(channel, attribute):
                details[label] = getattr(channel, attribute)
        if isinstance(channel, ForumChannel):
            details["🏷️ Available Tags"] = list(channel.available_tags)
            details["🖼️ Surface"] = (
                "Media Channel" if channel.type is ChannelType.media else "Forum Channel"
            )
        return details

    @classmethod
    def _add_event_field(cls, embed: Embed, name: str, value: Optional[str], *, inline=True) -> bool:
        """Add a metadata field without crossing Discord's hard embed limits."""
        if not cls.has_log_value(value) or len(embed.fields) >= 25:
            return False
        name = str(name)[:256]
        remaining = 5900 - len(embed) - len(name)
        if remaining <= 0:
            return False
        embed.add_field(name=name, value=str(value)[:min(1024, remaining)], inline=inline)
        return True

    @staticmethod
    def _field_key(name: Any) -> str:
        """Normalise decorated field names for duplicate detection."""
        return " ".join(
            "".join(character if character.isalnum() else " " for character in str(name))
            .casefold()
            .split()
        )

    @staticmethod
    def _same_log_entity(first: Any, second: Any) -> bool:
        if first is None or second is None:
            return False
        if first is second:
            return True
        first_id = getattr(first, "id", None)
        second_id = getattr(second, "id", None)
        if first_id is not None and second_id is not None:
            return first_id == second_id
        return isinstance(first, str) and isinstance(second, str) and first == second

    @classmethod
    def _summary_contains_entity(cls, description: str, value: Any) -> bool:
        if not description or not cls.has_log_value(value):
            return False
        if isinstance(value, (list, tuple, set)):
            meaningful = [item for item in value if cls.has_log_value(item)]
            return bool(meaningful) and all(
                cls._summary_contains_entity(description, item)
                for item in meaningful
            )

        folded = description.casefold()
        mention = getattr(value, "mention", None)
        if mention and str(mention).casefold() in folded:
            return True
        entity_id = getattr(value, "id", None)
        if entity_id is not None and str(entity_id) in description:
            return True

        text = str(value).strip().strip("`*_~").strip()
        if len(text) >= 4 and text.casefold() in folded:
            return True
        digit_groups = "".join(
            character if character.isdigit() else " " for character in text
        ).split()
        return any(len(group) >= 10 and group in description for group in digit_groups)

    @classmethod
    def _summary_contains_detail(cls, description: str, value: Any) -> bool:
        if not description or not cls.has_log_value(value):
            return False
        if isinstance(value, datetime):
            return any(
                utils.format_dt(value, style=style) in description
                for style in ("F", "R")
            )
        if isinstance(value, (list, tuple, set)):
            meaningful = [item for item in value if cls.has_log_value(item)]
            return bool(meaningful) and all(
                cls._summary_contains_detail(description, item)
                for item in meaningful
            )
        if isinstance(value, (dict, bool)):
            return False
        if hasattr(value, "mention") or hasattr(value, "id"):
            return cls._summary_contains_entity(description, value)
        text = str(value).strip().strip("`*_~").strip()
        if isinstance(value, (int, float)):
            return (
                (len(text) >= 6 and text in description)
                or f"`{text}`" in description
                or f"**{text}**" in description
            )
        return len(text) >= 4 and text.casefold() in description.casefold()

    @classmethod
    def _heading_implies_detail(
        cls,
        heading: str,
        name: Any,
        value: Any,
    ) -> bool:
        """Return whether an event heading already communicates a detail."""
        if not heading or not cls.has_log_value(value):
            return False

        field_key = cls._field_key(name)
        heading_key = cls._field_key(heading)
        if field_key in {"new state", "state", "status", "action"}:
            rendered = str(value).strip().strip("`*_~").strip()
            if len(rendered) >= 4 and rendered.casefold() in heading.casefold():
                return True

        parts = re.split(r"\s*(?:\u2192|->)\s*", str(value), maxsplit=1)
        if len(parts) != 2:
            return False
        after = parts[1].strip().strip("`*_~").strip().casefold()
        if after not in {"true", "false", "enabled", "disabled"}:
            return False

        implied_states = {
            "archived": (("archived", True), ("reopened", False)),
            "locked": (("locked", True), ("unlocked", False)),
            "pinned": (("pinned", True), ("unpinned", False)),
            "muted": (("muted", True), ("unmuted", False)),
            "deafened": (("deafened", True), ("undeafened", False)),
            "enabled": (("enabled", True), ("disabled", False)),
            "available": (("available", True), ("unavailable", False)),
        }
        after_enabled = after in {"true", "enabled"}
        for state_name, heading_states in implied_states.items():
            if state_name not in field_key.split():
                continue
            return any(
                action in heading_key.split() and after_enabled is expected
                for action, expected in heading_states
            )
        return False

    @classmethod
    def _prune_redundant_embed_fields(cls, embed: Embed) -> None:
        """Drop state fields already expressed by the event card heading."""
        heading = (
            str(embed.author.name)
            if embed.author and embed.author.name
            else str(embed.title or "")
        )
        fields = list(embed.fields)
        if not fields:
            return
        embed.clear_fields()
        for field in fields:
            if cls._heading_implies_detail(heading, field.name, field.value):
                continue
            embed.add_field(
                name=field.name,
                value=field.value,
                inline=field.inline,
            )

    def _remove_duplicate_action_links(self, embed: Embed, links) -> None:
        """Remove legacy jump-link lines when an equivalent button exists."""
        if not embed.description:
            return
        urls = {
            normalised[1]
            for link in links
            if (normalised := self._normalise_link(link)) is not None
        }
        lines = []
        for line in str(embed.description).splitlines():
            folded = line.casefold()
            duplicate = (
                "](" in line
                and any(url in line for url in urls)
                and any(label in folded for label in ("jump", "open"))
            )
            empty_context = any(
                placeholder in folded
                for placeholder in (
                    "by unknown-user",
                    "by unknown user",
                    "in unknown channel",
                    "owned by unknown-user",
                    "owned by unknown user",
                )
            )
            if not duplicate and not empty_context:
                lines.append(line)
        embed.description = "\n".join(lines).strip() or None

    @staticmethod
    def _classic_detail_label(name: Any) -> str:
        """Strip decorative prefixes while retaining a readable detail label."""
        return re.sub(r"^[^A-Za-z0-9]+", "", str(name)).strip() or "Detail"

    @classmethod
    def _append_classic_context(
        cls,
        embed: Embed,
        rows: list[Tuple[str, str]],
    ) -> list[Tuple[str, str]]:
        """Fold structured metadata into the classic chained description layout."""
        if not rows:
            return []

        description = str(embed.description or "").rstrip()
        split_at = len(description)
        for marker in ("\n\n>>>", "\n\n**From:", "\n\n**To:"):
            marker_at = description.find(marker)
            if marker_at != -1:
                split_at = min(split_at, marker_at)
        summary = description[:split_at].rstrip()
        body = description[split_at:]

        available = min(
            4096 - len(description),
            5900 - len(embed),
        )
        included = []
        overflow = []
        for name, value in rows:
            value = str(value).strip()
            if not value:
                continue
            if name in {"By", "In"}:
                row = f"{name} {value}"
            else:
                row = f"{name}: {value}" if name else value
            row = row.replace("\n", f"\n{emojis.empty}")
            estimated_cost = len(row) + len(emojis.creply) + 3
            if estimated_cost <= available:
                included.append(row)
                available -= estimated_cost
            else:
                overflow.append((name or "Detail", value))

        if included:
            if summary:
                rendered_rows = [
                    f"{emojis.reply if index == len(included) - 1 else emojis.creply} {row}"
                    for index, row in enumerate(included)
                ]
                rendered_context = "\n".join(rendered_rows)
                embed.description = f"{summary}\n{rendered_context}{body}"
            else:
                first, *remaining = included
                rendered_rows = [first]
                rendered_rows.extend(
                    f"{emojis.reply if index == len(remaining) - 1 else emojis.creply} {row}"
                    for index, row in enumerate(remaining)
                )
                rendered_context = "\n".join(rendered_rows)
                embed.description = f"{rendered_context}{body}"
        return overflow

    @staticmethod
    def _normalise_link(link) -> Optional[Tuple[str, str, Optional[str]]]:
        if isinstance(link, dict):
            label = link.get("label")
            url = link.get("url")
            emoji = link.get("emoji")
        else:
            try:
                label, url, *rest = link
            except (TypeError, ValueError):
                return None
            emoji = rest[0] if rest else None
        if not label or not url or not str(url).startswith(("https://", "http://")):
            return None
        return str(label)[:80], str(url)[:512], emoji

    def prepare_log_embed(self, guild_id: str, log: Log) -> None:
        """Render current structured log data in Fate's classic card style."""
        embed = next((item for item in log.embeds if isinstance(item, Embed)), None)
        if embed is None:
            return

        event_name = log.type.replace("_", " ").title()
        primary_link = next(
            (
                normalised
                for item in log.links
                if (normalised := self._normalise_link(item)) is not None
            ),
            None,
        )
        self._remove_duplicate_action_links(embed, log.links)

        if embed.author and embed.author.name:
            author_name = str(embed.author.name)
            embed.set_author(
                name=author_name[:256],
                url=(primary_link[1] if primary_link else embed.author.url),
                icon_url=embed.author.icon_url,
            )
        elif embed.title:
            if primary_link and not embed.url:
                embed.url = primary_link[1]
        else:
            embed.title = event_name[:256]
            if primary_link:
                embed.url = primary_link[1]

        description = str(embed.description or "")
        context_rows: list[Tuple[str, str]] = []
        if (
            log.target is not None
            and not self._summary_contains_entity(description, log.target)
        ):
            target = self.format_log_entity(log.target)
            if target:
                context_rows.append((log.target_label, target))
        if (
            log.actor is not None
            and not self._same_log_entity(log.actor, log.target)
            and not self._summary_contains_entity(description, log.actor)
        ):
            actor = self.format_log_entity(log.actor)
            if actor:
                context_rows.append(("By", actor))
        if (
            log.channel is not None
            and not self._same_log_entity(log.channel, log.target)
            and not self._same_log_entity(log.channel, log.actor)
            and not self._summary_contains_entity(description, log.channel)
        ):
            channel = self.format_log_entity(log.channel)
            if channel:
                context_rows.append(("In", channel))
        if (
            self.has_log_value(log.reason)
            and not self._summary_contains_detail(description, log.reason)
        ):
            context_rows.append(
                ("Reason", utils.escape_markdown(str(log.reason)))
            )

        heading = (
            str(embed.author.name)
            if embed.author and embed.author.name
            else str(embed.title or "")
        )
        for name, value in log.details.items():
            if not self.has_log_value(value):
                continue
            if any(
                self._field_key(name) == self._field_key(field.name)
                for field in embed.fields
            ):
                continue
            if self._summary_contains_detail(description, value):
                continue
            if self._heading_implies_detail(heading, name, value):
                continue
            context_rows.append(
                (
                    self._classic_detail_label(name),
                    self.format_log_detail(value),
                )
            )

        for name, value in self._append_classic_context(embed, context_rows):
            self._add_event_field(embed, name, value, inline=False)

        self._prune_redundant_embed_fields(embed)

        old_footer = str(embed.footer.text) if embed.footer and embed.footer.text else None
        footer_icon = embed.footer.icon_url if embed.footer else None
        reference = (
            getattr(log, "archive_key", "")[:8]
            if self.archive_enabled(guild_id)
            else ""
        )
        footer_parts = [old_footer] if old_footer else []
        if reference:
            footer_parts.append(f"Log {reference}")
        if footer_parts:
            embed.set_footer(
                text=" • ".join(footer_parts)[:2048],
                icon_url=footer_icon,
            )
        elif embed.footer:
            embed.remove_footer()
        if embed.timestamp is None:
            embed.timestamp = datetime.fromtimestamp(log.created_at, timezone.utc)

    @staticmethod
    def _collapse_signature_text(value: str) -> str:
        """Remove per-delivery identifiers while retaining meaningful log text."""
        value = re.sub(
            r"(?i)(\b(?:message\s+)?id:\s*`?)\d{15,22}(`?)",
            r"\1<id>\2",
            value,
        )
        value = re.sub(
            r"(?i)(message\s+`)\d{15,22}(`)",
            r"\1<id>\2",
            value,
        )
        return value

    @classmethod
    def collapse_signature(cls, log: Log) -> str:
        """Return the stable visible identity used for consecutive collapsing."""
        embeds = []
        for embed in log.embeds:
            payload = (
                deepcopy(embed.to_dict())
                if isinstance(embed, Embed)
                else deepcopy(dict(embed))
            )
            payload.pop("timestamp", None)
            footer = payload.get("footer")
            if isinstance(footer, dict) and footer.get("text"):
                footer["text"] = re.sub(
                    r"(?:\s*[•·]\s*)?Log [0-9a-f]{8}\b",
                    "",
                    str(footer["text"]),
                    flags=re.IGNORECASE,
                ).strip()
                if not footer["text"]:
                    payload.pop("footer", None)

            def normalise(value):
                if isinstance(value, str):
                    return cls._collapse_signature_text(value)
                if isinstance(value, list):
                    return [normalise(item) for item in value]
                if isinstance(value, dict):
                    return {
                        key: normalise(item)
                        for key, item in sorted(value.items())
                    }
                return value

            embeds.append(normalise(payload))

        links = [
            list(item)
            for link in log.links
            if (item := cls._normalise_link(link)) is not None
        ]
        return json.dumps(
            {"type": log.type, "embeds": embeds, "links": links},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    async def collapse_consecutive_log(
        self,
        guild_id: str,
        channel,
        log: Log,
    ) -> Optional[Message]:
        """Edit the previous matching card and return its delivery message."""
        deliveries = self.last_log_deliveries.setdefault(guild_id, {})
        previous = deliveries.get(channel.id)
        if (
            previous is None
            or previous.get("failed")
            or previous["signature"] != log.collapse_signature
        ):
            return None

        count = previous["count"] + 1
        previous["count"] = count
        if count > 2:
            pending_update = previous.get("pending_update")
            if pending_update is None or pending_update.done():
                previous["pending_update"] = asyncio.create_task(
                    self.flush_collapsed_delivery(previous)
                )
            return previous["primary"]

        if not await self.update_collapsed_delivery(previous):
            deliveries.pop(channel.id, None)
            return None
        return previous["primary"]

    async def update_collapsed_delivery(
        self,
        delivery: dict,
        count: Optional[int] = None,
    ) -> bool:
        """Apply the latest occurrence count to a collapsed delivery."""
        count = delivery["count"] if count is None else count
        primary = delivery["primary"]
        try:
            await primary.edit(content=f"{count} Occurrences")
        except (Forbidden, NotFound, HTTPException, ClientOSError):
            delivery["failed"] = True
            return False

        for mirror in delivery["mirrors"]:
            with suppress(Forbidden, NotFound, HTTPException, ClientOSError):
                await mirror.edit(content=f"{count} Occurrences")
        return True

    async def flush_collapsed_delivery(self, delivery: dict) -> None:
        """Collect rapid occurrence 3+ updates before editing Discord again."""
        while True:
            await asyncio.sleep(2)
            count = delivery["count"]
            if not await self.update_collapsed_delivery(delivery, count):
                return
            if delivery["count"] == count:
                return

    def remember_log_delivery(
        self,
        guild_id: str,
        channel,
        log: Log,
        primary: Message,
        mirrors: Sequence[Message],
        count: int = 1,
    ) -> None:
        """Remember the newest collapsible card for one destination."""
        self.last_log_deliveries.setdefault(guild_id, {})[channel.id] = {
            "signature": log.collapse_signature,
            "primary": primary,
            "mirrors": list(mirrors),
            "count": count,
            "pending_update": None,
        }

    def is_ignored_bot(self, guild_id: str, user_id: int) -> bool:
        """Return whether a user ID is in this guild's ignored-bot filter."""
        ignored = self.config.get(guild_id, {}).get("ignored_bots", [])
        return user_id in {
            stored_id
            for value in ignored
            if (stored_id := self.snowflake_id(value)) is not None
        }

    @staticmethod
    def snowflake_id(value: Any) -> Optional[int]:
        """Normalize a stored Discord snowflake without accepting booleans."""
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def capture_message_snapshot(self, guild_id: str, msg: Message) -> bool:
        try:
            return await self._capture_message_snapshot(guild_id, msg)
        except Exception as error:
            self.get_health_record(guild_id)["archive_dropped"] += 1
            self.bot.log.critical(
                f"Could not save a local message snapshot for guild {guild_id}: {error}"
            )
            return False

    async def _capture_message_snapshot(self, guild_id: str, msg: Message) -> bool:
        """Retain a bounded source-message snapshot for raw edit/delete recovery."""
        archive = self.archive_config(guild_id)
        if not archive.get("enabled") or not archive.get("cache_messages", True):
            return False
        config = self.config.get(guild_id, {})
        if (
            msg.author.id == self.bot.user.id
            or msg.author.id in config.get("ignored_bots", [])
            or msg.channel.id in config.get("ignored_channels", [])
        ):
            return False

        reference = getattr(msg, "reference", None)
        payload = {
            "message_id": str(msg.id),
            "guild_id": guild_id,
            "channel_id": str(msg.channel.id),
            "channel_name": getattr(msg.channel, "name", None),
            "author_id": str(msg.author.id),
            "author_name": str(msg.author),
            "author_display_name": getattr(msg.author, "display_name", str(msg.author)),
            "author_mention": getattr(msg.author, "mention", f"<@{msg.author.id}>"),
            "author_avatar": str(getattr(msg.author.display_avatar, "url", "")),
            "author_bot": bool(getattr(msg.author, "bot", False)),
            "content": msg.content or "",
            "created_at": msg.created_at.timestamp(),
            "edited_at": msg.edited_at.timestamp() if msg.edited_at else None,
            "pinned": bool(msg.pinned),
            "jump_url": msg.jump_url,
            "attachments": [
                {
                    "id": str(attachment.id),
                    "filename": attachment.filename,
                    "size": attachment.size,
                    "content_type": attachment.content_type,
                    "url": attachment.url,
                    "description": getattr(attachment, "description", None),
                    "stored": False,
                }
                for attachment in msg.attachments
            ],
            "embeds": [embed.to_dict() for embed in msg.embeds],
            "components": message_component_payloads(msg.components),
            "reference": (
                {
                    "message_id": str(reference.message_id),
                    "channel_id": str(reference.channel_id) if reference.channel_id else None,
                    "guild_id": str(reference.guild_id) if reference.guild_id else None,
                }
                if reference and reference.message_id
                else None
            ),
        }
        accepted = self.local_archive.enqueue_message(
            guild_id=guild_id,
            message_id=msg.id,
            channel_id=msg.channel.id,
            created_at=time(),
            payload=payload,
            retention_days=self.archive_retention(guild_id),
        )
        if archive.get("store_attachments") and msg.attachments:
            files = []
            total_bytes = 0
            attachment_limit = min(
                int(archive.get("attachment_size_limit_mb", 25)) * 1024**2,
                MAX_ATTACHMENT_BYTES,
            )
            for position, attachment in enumerate(msg.attachments[:10]):
                if (
                    attachment.size > attachment_limit
                    or total_bytes + attachment.size > MAX_MESSAGE_ATTACHMENT_BYTES
                ):
                    continue
                try:
                    data = await attachment.read(use_cached=True)
                except (NotFound, Forbidden, HTTPException, ClientOSError):
                    continue
                if (
                    len(data) > attachment_limit
                    or total_bytes + len(data) > MAX_MESSAGE_ATTACHMENT_BYTES
                ):
                    continue
                total_bytes += len(data)
                payload["attachments"][position]["stored"] = True
                files.append(
                    {
                        "id": attachment.id,
                        "filename": attachment.filename,
                        "content_type": attachment.content_type,
                        "data": data,
                    }
                )
            if files:
                accepted = self.local_archive.enqueue_message(
                    guild_id=guild_id,
                    message_id=msg.id,
                    channel_id=msg.channel.id,
                    created_at=time(),
                    payload=payload,
                    retention_days=self.archive_retention(guild_id),
                    files=files,
                ) and accepted
        if not accepted:
            self.get_health_record(guild_id)["archive_dropped"] += 1
        return accepted

    def stage_local_log(
        self,
        guild_id: str,
        log: Log,
        *,
        destination=None,
        status: str = "pending",
    ) -> bool:
        try:
            return self._stage_local_log(
                guild_id,
                log,
                destination=destination,
                status=status,
            )
        except Exception as error:
            self.get_health_record(guild_id)["archive_dropped"] += 1
            self.bot.log.critical(
                f"Could not save a local Logging event for guild {guild_id}: {error}"
            )
            return False

    def _stage_local_log(
        self,
        guild_id: str,
        log: Log,
        *,
        destination=None,
        status: str = "pending",
    ) -> bool:
        """Store one logical Logging event before its Discord delivery."""
        if not self.archive_enabled(guild_id) or log.archive_staged:
            return False
        config = self.config[guild_id]
        destination_id = getattr(destination, "id", None) or config.get(
            "channels", {}
        ).get(log.type, config.get("channel"))
        destination_name = getattr(destination, "name", None)
        payload = {
            "event_type": log.type,
            "category": self.event_category(log.type),
            "created_at": log.created_at,
            "source_channel_id": str(getattr(log.channel, "id", "")) or None,
            "destination_channel_id": str(destination_id) if destination_id else None,
            "destination_channel_name": destination_name,
            "embeds": [
                embed.to_dict() if isinstance(embed, Embed) else dict(embed)
                for embed in log.embeds
            ],
            "attachments": [
                {"filename": getattr(file, "filename", "attachment")}
                for file in log.files[:10]
            ],
            "links": [
                {"label": item[0], "url": item[1]}
                for link in log.links
                if (item := self._normalise_link(link)) is not None
            ],
        }
        search_text = json.dumps(payload, ensure_ascii=False, default=str)
        accepted = self.local_archive.enqueue_log(
            archive_key=log.archive_key,
            guild_id=guild_id,
            event_type=log.type,
            created_at=log.created_at,
            channel_id=destination_id,
            channel_name=destination_name,
            payload=payload,
            search_text=search_text,
            retention_days=self.archive_retention(guild_id),
            status=status,
        )
        log.archive_staged = accepted
        if not accepted:
            self.get_health_record(guild_id)["archive_dropped"] += 1
        return accepted

    def update_local_delivery(
        self,
        log: Log,
        status: str,
        message: Optional[Message] = None,
    ) -> None:
        if not log.archive_staged:
            return
        try:
            accepted = self.local_archive.update_delivery(
                log.archive_key,
                status=status,
                discord_message_id=getattr(message, "id", None),
                jump_url=getattr(message, "jump_url", None),
            )
        except Exception as error:
            self.bot.log.critical(
                f"Could not update local Logging delivery {log.archive_key}: {error}"
            )
            accepted = False
        if not accepted:
            log.archive_staged = False

    def build_log_view(self, links) -> Optional[ui.View]:
        """Build a modern, link-only action row that can coexist with embeds."""
        buttons = []
        seen_urls = set()
        for link in links or []:
            normalised = self._normalise_link(link)
            if normalised is None:
                continue
            label, url, emoji = normalised
            if url in seen_urls:
                continue
            seen_urls.add(url)
            buttons.append((label, url, emoji))
            if len(buttons) == 5:
                break
        if not buttons:
            return None

        view = ui.View(timeout=None)
        for label, url, emoji in buttons:
            view.add_item(
                ui.Button(
                    style=ButtonStyle.link,
                    label=label,
                    url=url,
                    emoji=emoji,
                )
            )
        return view

    def normalise_embed(self, embed: Embed, log_type: str) -> Embed:
        """Clamp one embed to Discord's documented per-embed limits."""
        if embed.title:
            embed.title = str(embed.title)[:256]
        if embed.description:
            embed.description = str(embed.description)[:4096]

        if embed.author and embed.author.name:
            embed.set_author(
                name=str(embed.author.name)[:256],
                url=embed.author.url,
                icon_url=embed.author.icon_url,
            )
        if embed.footer and embed.footer.text:
            embed.set_footer(
                text=str(embed.footer.text)[:2048],
                icon_url=embed.footer.icon_url,
            )

        fields = list(embed.fields)[:25]
        embed.clear_fields()
        for field in fields:
            name = str(field.name or "Details")[:256]
            value = str(field.value or "None")[:1024]
            remaining = 6000 - len(embed) - len(name)
            if remaining <= 0:
                break
            embed.add_field(
                name=name,
                value=value[:remaining],
                inline=field.inline,
            )

        if len(embed) > 6000:
            # A fully populated title, description, author, and footer can cross
            # 6,000 even without fields. Preserve the event summary first.
            overflow = len(embed) - 6000
            if embed.footer and embed.footer.text:
                footer = str(embed.footer.text)
                keep = max(0, len(footer) - overflow)
                embed.set_footer(
                    text=footer[:keep] or None,
                    icon_url=embed.footer.icon_url,
                )
            if len(embed) > 6000 and embed.description:
                overflow = len(embed) - 6000
                embed.description = str(embed.description)[:-overflow] or None

        if len(embed) > 6000:
            self.bot.log.critical(
                f"Could not fully normalise a {log_type} embed ({len(embed)} chars)"
            )
        return embed

    @staticmethod
    def embed_batches(embeds: Sequence[Embed]) -> List[List[Embed]]:
        """Split embeds across messages while respecting 10/6,000 limits."""
        batches = []
        current = []
        current_length = 0
        for embed in embeds:
            embed_length = len(embed)
            if current and (len(current) >= 10 or current_length + embed_length > 6000):
                batches.append(current)
                current = []
                current_length = 0
            current.append(embed)
            current_length += embed_length
        if current:
            batches.append(current)
        return batches

    def configured_log_destination(self, guild_id: str, log: Log):
        """Return the configured destination ID without a Discord request."""
        config = self.config[guild_id]
        return config["channels"].get(log.type, config["channel"])

    def log_can_join_batch(
        self,
        guild_id: str,
        guild: Guild,
        channel,
        log: Log,
        *,
        shared_links,
        embed_count: int,
        embed_length: int,
    ) -> bool:
        """Return whether an event can share the current Discord message."""
        if guild.id in self.threaded_log_guilds or log.files:
            return False
        # A Discord message can only have one view. Logs may still batch when
        # every card points to the same destination (for example, a burst of
        # deletions in one channel); the first card's shared button remains
        # correct for the whole batch.
        if log.links != shared_links:
            return False
        if not log.embeds:
            return False
        if self.configured_log_destination(guild_id, log) != channel.id:
            return False
        candidate_length = sum(len(embed) for embed in log.embeds)
        return (
            embed_count + len(log.embeds) <= 10
            and embed_length + candidate_length <= 6000
        )

    async def collect_log_batch(
        self,
        guild_id: str,
        guild: Guild,
        channel,
        first: Log,
    ) -> tuple[List[Log], Optional[Log]]:
        """Collect compatible consecutive events for a short delivery window."""
        logs = [first]
        if not self.log_can_join_batch(
            guild_id,
            guild,
            channel,
            first,
            shared_links=first.links,
            embed_count=0,
            embed_length=0,
        ):
            return logs, None

        embed_count = len(first.embeds)
        embed_length = sum(len(embed) for embed in first.embeds)
        signatures = {first.collapse_signature}
        duplicate_batch = None
        deadline = asyncio.get_running_loop().time() + self.queue_batch_window
        queue = self.queue[guild_id]

        while embed_count < 10 and embed_length < 6000:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                candidate = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break

            if candidate.type in self.config[guild_id]["disabled"]:
                queue.task_done()
                continue
            if not self.log_can_join_batch(
                guild_id,
                guild,
                channel,
                candidate,
                shared_links=first.links,
                embed_count=embed_count,
                embed_length=embed_length,
            ):
                return logs, candidate

            is_first_duplicate = candidate.collapse_signature == first.collapse_signature
            if len(logs) == 1:
                duplicate_batch = is_first_duplicate
            elif duplicate_batch != is_first_duplicate:
                return logs, candidate

            # Mixed batches contain one card per signature. Homogeneous duplicate
            # batches remain one card and use the existing Occurrences counter.
            if not duplicate_batch and candidate.collapse_signature in signatures:
                return logs, candidate

            logs.append(candidate)
            signatures.add(candidate.collapse_signature)
            embed_count += len(candidate.embeds)
            embed_length += sum(len(embed) for embed in candidate.embeds)

        return logs, None

    def prepare_dequeued_log(self, guild_id: str, guild: Guild, log: Log) -> None:
        """Apply final presentation settings and stage one logical event."""
        for embed_index, embed in enumerate(log.embeds):
            if not isinstance(embed, Embed):
                continue
            if theme := self.config[guild_id]["theme"]:
                match theme:
                    case "RGB":
                        if guild_id not in self.cycle:
                            self.cycle[guild_id] = 0
                        if guild_id not in self.colors:
                            self.colors[guild_id] = generate_rainbow_rgb(20)
                        embed.colour = Color.from_rgb(
                            *self.colors[guild_id][self.cycle[guild_id]]
                        )
                        self.cycle[guild_id] += 1
                        if self.cycle[guild_id] >= len(self.colors[guild_id]):
                            self.colors[guild_id] = list(reversed(self.colors[guild_id]))
                            self.cycle[guild_id] = 0
                    case "Role Color":
                        for word in (embed.description or "").split():
                            if word.startswith("<@") and "&" not in word and "!" not in word:
                                user_id = word.strip("<@>")
                                if not user_id.isdigit():
                                    continue
                                if member := guild.get_member(int(user_id)):
                                    embed.colour = member.color
                                    break
                    case "Solid Color":
                        embed.colour = Color(self.config[guild_id]["color"])
                    case "Custom":
                        if log.type in self.config[guild_id]["colors"]:
                            embed.colour = Color(self.config[guild_id]["colors"][log.type])

            if embed.timestamp is None:
                embed.timestamp = datetime.fromtimestamp(log.created_at, timezone.utc)
            log.embeds[embed_index] = self.normalise_embed(embed, log.type)

        # Persist before Discord delivery so all logical events stay searchable.
        self.stage_local_log(guild_id, log)

    async def restore_secure_log_message(self, message: Message) -> None:
        """Recreate a deleted protected log without noisy recovery text."""
        files = []
        for attachment in message.attachments[:10]:
            if attachment.size > message.guild.filesize_limit:
                continue
            with suppress(NotFound, Forbidden, HTTPException):
                files.append(await attachment.to_file(use_cached=True))
        restored_embeds = [
            self.normalise_embed(
                Embed.from_dict(embed.to_dict()),
                "secure_restore",
            )
            for embed in message.embeds
        ]
        batches = self.embed_batches(restored_embeds) or [[]]
        for batch_index, batch in enumerate(batches):
            options = {
                "files": files if batch_index == 0 else [],
                "silent": True,
                "allowed_mentions": AllowedMentions.none(),
            }
            if batch:
                options["embeds"] = batch
            if batch or options["files"]:
                await message.channel.send(**options)

    @staticmethod
    def health_timestamp(timestamp: Optional[float]) -> str:
        return f"<t:{int(timestamp)}:R>" if timestamp else "Not yet"

    @staticmethod
    def format_storage_size(size: int) -> str:
        value = float(max(size, 0))
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024

    async def archive_command_config(self, ctx: Context) -> Optional[dict]:
        guild_id = str(ctx.guild.id)
        config = self.config.get(guild_id)
        if config is None:
            await ctx.send("Turn on Logging before configuring local history.")
            return None
        if config.get("secure") and ctx.author.id != ctx.guild.owner.id:
            await ctx.send(
                "Only the server owner can manage local history while secure Logging is on."
            )
            return None
        return config

    def build_health_embed(self, guild_id: str) -> Embed:
        snapshot = self.get_worker_snapshot(guild_id)
        guild = self.bot.get_guild(int(guild_id))
        guild_name = str(guild) if guild else f"Unknown Guild ({guild_id})"
        status = snapshot["status"]
        icons = {
            "healthy": "🟢",
            "degraded": "🟠",
            "blocked": "🟡",
            "down": "🔴",
        }
        colors = {
            "healthy": green,
            "degraded": orange,
            "blocked": yellow,
            "down": red,
        }

        e = Embed(
            title=f"Logger Health — {guild_name}",
            color=colors[status],
            description=f"{icons[status]} **{status.title()}**",
        )
        e.add_field(
            name="Queue",
            value=(
                f"`{snapshot['queue_size']}/{snapshot['queue_capacity']}` queued "
                f"(`{snapshot['queue_ratio']:.0%}` full)"
            ),
        )
        e.add_field(
            name="Worker",
            value=(
                f"`{'Running' if snapshot['worker_running'] else 'Stopped'}`\n"
                f"Restarts: `{snapshot['restarts']}`"
            ),
        )
        e.add_field(
            name="Delivery",
            value=(
                f"Sent: `{snapshot['sent']}`\n"
                f"Failed: `{snapshot['failed']}`\n"
                f"Dropped: `{snapshot['dropped']}`"
            ),
        )

        destination_id = self.config.get(guild_id, {}).get("channel")
        destination = f"<#{destination_id}>" if destination_id else "Not configured"
        blocked = ", ".join(snapshot["blocked_permissions"]) or "None"
        e.add_field(name="Destination", value=destination, inline=False)
        e.add_field(name="Permission Wait", value=f"`{blocked}`", inline=False)
        e.add_field(
            name="Activity",
            value=(
                f"Worker started: {self.health_timestamp(snapshot['worker_started_at'])}\n"
                f"Last delivered: {self.health_timestamp(snapshot['last_success'])}\n"
                f"Last failure: {self.health_timestamp(snapshot['last_error_at'])}"
            ),
            inline=False,
        )
        if snapshot["last_error"]:
            e.add_field(
                name="Last Error",
                value=f"```{snapshot['last_error'][:950]}```",
                inline=False,
            )
        e.set_footer(text=f"Runtime counters since {datetime.fromtimestamp(self.health_started_at):%Y-%m-%d %H:%M:%S}")
        return e

    def build_dashboard_embed(self) -> Embed:
        snapshots = [
            self.get_worker_snapshot(guild_id)
            for guild_id in self.config
        ]
        status_counts = {
            status: sum(snapshot["status"] == status for snapshot in snapshots)
            for status in ("healthy", "degraded", "blocked", "down")
        }
        problem_count = (
            status_counts["degraded"]
            + status_counts["blocked"]
            + status_counts["down"]
        )
        color = red if status_counts["down"] else orange if problem_count else green
        e = Embed(
            title="Logger Dashboard",
            color=color,
            description=(
                f"Tracking `{len(self.config)}` configured logger"
                f"{s(len(self.config))} since "
                f"<t:{int(self.health_started_at)}:R>."
            ),
        )
        e.add_field(
            name="Health",
            value=(
                f"🟢 Healthy: `{status_counts['healthy']}`\n"
                f"🟠 Degraded: `{status_counts['degraded']}`\n"
                f"🟡 Blocked: `{status_counts['blocked']}`\n"
                f"🔴 Down: `{status_counts['down']}`"
            ),
        )
        e.add_field(
            name="Workers & Queues",
            value=(
                f"Workers running: `{sum(item['worker_running'] for item in snapshots)}`\n"
                f"Queues available: `{len(self.queue)}`\n"
                f"Logs queued: `{sum(item['queue_size'] for item in snapshots)}`"
            ),
        )
        e.add_field(
            name="Runtime Delivery",
            value=(
                f"Sent: `{sum(item['sent'] for item in snapshots)}`\n"
                f"Failed: `{sum(item['failed'] for item in snapshots)}`\n"
                f"Dropped: `{sum(item['dropped'] for item in snapshots)}`\n"
                f"Restarts: `{sum(item['restarts'] for item in snapshots)}`"
            ),
        )

        priorities = {"down": 0, "blocked": 1, "degraded": 2, "healthy": 3}
        attention = sorted(
            (item for item in snapshots if item["status"] != "healthy"),
            key=lambda item: (
                priorities[item["status"]],
                -item["queue_ratio"],
                -item["consecutive_failures"],
            ),
        )
        if attention:
            icons = {"degraded": "🟠", "blocked": "🟡", "down": "🔴"}
            lines = []
            for item in attention:
                guild = self.bot.get_guild(int(item["guild_id"]))
                name = str(guild) if guild else item["guild_id"]
                line = (
                    f"{icons[item['status']]} **{name}** — "
                    f"`{item['queue_size']}/{item['queue_capacity']}` queued, "
                    f"`{item['consecutive_failures']}` consecutive failures"
                )
                if len("\n".join([*lines, line])) > 1000:
                    break
                lines.append(line)
            e.add_field(
                name=f"Needs Attention ({problem_count})",
                value="\n".join(lines) or "No details available",
                inline=False,
            )
        else:
            e.add_field(name="Needs Attention", value="None", inline=False)

        e.set_footer(text="Runtime counters reset whenever the logger cog reloads")
        return e

    def add_to_queue(self, guild_id, *args, **kwargs):
        log = Log(*args, **kwargs)
        if guild_id not in self.queue:
            self.stage_local_log(guild_id, log, status="dropped")
            return
        self.prepare_log_embed(guild_id, log)
        log.collapse_signature = self.collapse_signature(log)
        try:
            self.queue[guild_id].put_nowait(log)
            telemetry = getattr(self.bot, "telemetry", None)
            if telemetry is not None:
                telemetry.increment("logger_events")
        except asyncio.QueueFull:
            self.stage_local_log(guild_id, log, status="dropped")
            health = self.get_health_record(guild_id)
            health["dropped"] += 1
            self.record_delivery_failure(
                guild_id, f"Queue full; dropped {log.type}"
            )
            snapshot = self.get_worker_snapshot(guild_id)
            blocked_permissions = snapshot["blocked_permissions"]
            if blocked_permissions:
                self.start_permission_queue_watchdog(guild_id)
                self.bot.log.debug(
                    "Logger queue is full for guild "
                    f"{guild_id} while waiting on permissions "
                    f"({', '.join(blocked_permissions)}); dropped {log.type}"
                )
            else:
                alert = self.queue_full_alert_message(
                    guild_id, log.type, snapshot
                )
                if alert:
                    self.bot.log.critical(alert)

    async def save_data(self) -> None:
        """ Saves local variables """
        await self.bot.utils.save_json(self.path, self.config)

    async def get_or_fetch(self, channel_id: int) -> Optional[Union[TextChannel, Thread]]:
        if not channel_id:
            return None
        if channel := self.bot.get_channel(channel_id):
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except (NotFound, Forbidden, HTTPException):
            return None

    @staticmethod
    def message_fetch_retry_after(error: HTTPException) -> float:
        """Read Discord's retry delay without trusting an unbounded value."""
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", {}) or {}
        try:
            return max(1.1, float(headers.get("Retry-After", 1.1)))
        except (TypeError, ValueError):
            return 1.1

    def report_message_fetch_rate_limit(self, channel, message_id: int) -> None:
        """Warn once per channel per minute when logger recovery skips a fetch."""
        alerts = getattr(self, "message_fetch_rate_limit_alerts", None)
        if alerts is None:
            alerts = self.message_fetch_rate_limit_alerts = {}
        now = time()
        if now - alerts.get(channel.id, 0) < 60:
            return
        alerts[channel.id] = now
        self.bot.log.warning(
            f"Logger skipped rate-limited message fetch {message_id} in "
            f"channel {channel.id}; the gateway listener stayed active"
        )

    async def fetch_message_for_log(self, channel, message_id: int):
        """Serialize uncached reads and absorb bounded channel rate limits."""
        lock = self.message_fetch_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            for attempt in range(2):
                try:
                    message = await channel.fetch_message(message_id)
                except HTTPException as error:
                    if error.status != 429:
                        await asyncio.sleep(self.message_fetch_spacing)
                        raise
                    retry_after = self.message_fetch_retry_after(error)
                    if (
                        attempt == 0
                        and retry_after <= self.message_fetch_max_retry_delay
                    ):
                        await asyncio.sleep(
                            max(self.message_fetch_spacing, retry_after)
                        )
                        continue
                    self.report_message_fetch_rate_limit(channel, message_id)
                    await asyncio.sleep(self.message_fetch_spacing)
                    return None
                except Exception:
                    await asyncio.sleep(self.message_fetch_spacing)
                    raise
                await asyncio.sleep(self.message_fetch_spacing)
                return message
        return None

    async def ensure_channels(self, guild, log_type=None):
        """Ensure the primary and current redirect channels are available."""
        guild_id = str(guild.id)
        config = self.config[guild_id]
        channel = await self.get_or_fetch(config["channel"])

        if not channel:
            if not config["secure"]:
                if guild_id not in self.unavailable_channel_notified:
                    self.unavailable_channel_notified.add(guild_id)
                    self.bot.log.debug(
                        f"Logger channel is unavailable for guild {guild_id}; "
                        "use the logger set-channel command to replace it"
                    )
                return False
            if not guild or not guild.me:
                return False
            if not guild.me.guild_permissions.manage_channels:
                result = await self.wait_for_permissions(
                    guild, "manage_channels"
                )
                if not result:
                    return await self.destruct(guild_id)
            try:
                channel = await self.bot.fetch_channel(
                    config["channel"]
                )
            except NotFound as err:
                channel = await guild.create_text_channel(name="bot-logs")
                await channel.send(f"Couldn't get the old channel with ID: {config['channel']}\n{err}")
            config["channel"] = channel.id
            await self.save_data()
        self.unavailable_channel_notified.discard(guild_id)

        # Only validate the redirect used by this log. Split loggers can have
        # dozens of redirects, so checking all of them per event is wasteful.
        if log_type and log_type in config["channels"]:
            redirect = await self.get_or_fetch(config["channels"][log_type])
            if redirect is None:
                del config["channels"][log_type]
                await self.save_data()
        return True

    async def destruct(self, guild_id):
        if guild_id in self.queue:
            del self.queue[guild_id]
        if guild_id in self.recent_logs:
            del self.recent_logs[guild_id]
        self.last_log_deliveries.pop(guild_id, None)
        self.stop_worker(guild_id)
        await self.save_data()

    async def wait_for_permissions(self, guild, permission, channel=None, host=False):
        def perms():
            return channel.permissions_for(guild.me) if channel else guild.me.guild_permissions

        if not guild or not guild.me:
            raise IgnoredExit
        if getattr(perms(), permission):
            return True

        guild_id = str(guild.id)
        pool_id = (guild_id, permission, getattr(channel, "id", None))
        if not host:
            if pool_id not in self.pool:
                coro = self.wait_for_permissions(guild, permission, channel, host=True)
                task = self.bot.loop.create_task(coro)
                self.pool[pool_id] = task
            return await self.pool[pool_id]

        owner = guild.owner  # type: Member
        lmt = datetime.now(tz=timezone.utc) - timedelta(days=1)
        if owner.dm_channel:
            msg = None
            async for msg in owner.dm_channel.history(limit=1, after=lmt):
                msg = msg
            if not msg or "I'm missing" in msg.content:
                with suppress(Forbidden, NotFound, AttributeError):
                    await owner.send(
                        f"I need {permission} permissions in {guild} for the logger module to function. "
                        "If the logger queue fills and the permissions are still missing 24 hours later, "
                        "the Logger module will be disabled automatically."
                    )

        try:
            while guild_id in self.config:
                if not guild or not guild.me:
                    await self.destruct(guild_id)
                    raise IgnoredExit
                if getattr(perms(), permission):
                    return True
                await asyncio.sleep(60)
            raise IgnoredExit
        finally:
            if self.pool.get(pool_id) is asyncio.current_task():
                self.pool.pop(pool_id, None)

    async def start_queue(self, guild_id: str) -> None:
        guild = self.bot.get_guild(int(guild_id))
        for _ in range(60):
            if guild:
                break
            await asyncio.sleep(60)
            guild = self.bot.get_guild(int(guild_id))
        else:
            return await self.destruct(guild_id)

        pending_log = None
        while True:  # Listen for new logs
            if guild_id not in self.queue:
                return
            if pending_log is None:
                log = await self.queue[guild_id].get()  # type: Log
            else:
                log = pending_log
                pending_log = None
            if log.type in self.config[guild_id]["disabled"]:
                self.queue[guild_id].task_done()
                continue

            self.prepare_dequeued_log(guild_id, guild, log)

            # Permission checks to ensure the secure features can function
            if self.config[guild_id]["secure"]:
                await self.wait_for_permissions(guild, "administrator")

            # Get the channel this log will be sent to
            if not await self.ensure_channels(guild, log.type):
                self.update_local_delivery(log, "failed")
                self.record_delivery_failure(
                    guild_id, f"Destination unavailable for {log.type}"
                )
                self.queue[guild_id].task_done()
                continue

            if log.type in self.config[guild_id]["channels"]:
                channel = await self.get_or_fetch(self.config[guild_id]["channels"][log.type])
            else:
                channel = await self.get_or_fetch(self.config[guild_id]["channel"])

            if channel is None:
                self.update_local_delivery(log, "failed")
                self.record_delivery_failure(
                    guild_id, f"Destination unavailable for {log.type}"
                )
                self.bot.log.critical(
                    f"Logger destination for {log.type} is unavailable in guild {guild_id}"
                )
                self.queue[guild_id].task_done()
                continue

            # Ensure basic send-embed level permissions
            await self.wait_for_permissions(guild, "send_messages", channel)
            await self.wait_for_permissions(guild, "embed_links", channel)

            # Ensure this still exists
            if guild_id not in self.recent_logs:
                self.recent_logs[guild_id] = []

            ignored_exceptions = (
                Forbidden,
                NotFound,
                HTTPException,
                ConnectionResetError,
                ClientOSError
            )

            logs = [log]
            delivered_messages = []
            try:
                batches = self.embed_batches(log.embeds) or [[]]
                files = log.files[:10]
                collapsible = len(batches) == 1 and not files
                if len(log.files) > len(files):
                    self.bot.log.critical(
                        f"A {log.type} log had {len(log.files)} files; only 10 can be sent"
                    )
                if collapsible:
                    collapsed_message = await self.collapse_consecutive_log(
                        guild_id, channel, log
                    )
                    if collapsed_message is not None:
                        self.update_local_delivery(log, "delivered", collapsed_message)
                        self.record_delivery_success(guild_id)
                        self.queue[guild_id].task_done()
                        continue

                logs, pending_log = await self.collect_log_batch(
                    guild_id, guild, channel, log
                )
                for queued_log in logs[1:]:
                    self.prepare_dequeued_log(guild_id, guild, queued_log)

                duplicate_batch = len(logs) > 1 and all(
                    queued_log.collapse_signature == log.collapse_signature
                    for queued_log in logs
                )
                if duplicate_batch:
                    batches = [log.embeds]
                    files = []
                    collapsible = True
                elif len(logs) > 1:
                    batches = [[
                        embed
                        for queued_log in logs
                        for embed in queued_log.embeds
                    ]]
                    files = []
                    collapsible = False

                for batch_index, batch in enumerate(batches):
                    send_options = {
                        "files": files if batch_index == 0 else [],
                        # Log traffic should not create push notifications for
                        # members following a busy destination or mirror thread.
                        "silent": True,
                        "allowed_mentions": AllowedMentions.none(),
                        "view": (
                            self.build_log_view(log.links)
                            if batch_index == 0
                            else None
                        ),
                    }
                    if duplicate_batch and batch_index == 0:
                        send_options["content"] = f"{len(logs)} Occurrences"
                    if batch:
                        send_options["embeds"] = batch
                    sent_message = await channel.send(**send_options)
                    delivered_messages.append(sent_message)
                mirrored_messages = await self.mirror_to_category_thread(
                    guild, channel, log
                )
            except ignored_exceptions as error:
                for queued_log in logs:
                    self.update_local_delivery(
                        queued_log,
                        "partial" if delivered_messages else "failed",
                        delivered_messages[-1] if delivered_messages else None,
                    )
                    self.record_delivery_failure(
                        guild_id, f"{type(error).__name__}: {error}"
                    )
                all_embeds = [
                    embed for queued_log in logs for embed in queued_log.embeds
                ]
                e = Embed(title="Failed to send embed")
                e.set_author(name=guild if guild else "Unknown Guild")
                e.description = traceback.format_exc()[-3500:]
                e.add_field(
                    name="Event Type",
                    value=f"`{', '.join(item.type for item in logs)[:1000]}`",
                )
                e.add_field(name="Embed Count", value=f"`{len(all_embeds)}`")
                e.add_field(
                    name="File Count",
                    value=f"`{sum(len(item.files) for item in logs)}`",
                )
                e.add_field(
                    name="Payload Shape",
                    value="\n".join(
                        (
                            f"`#{index + 1}` • `{len(embed)}` chars • "
                            f"`{len(embed.fields)}` fields"
                        )
                        for index, embed in enumerate(all_embeds[:10])
                    ) or "No embeds",
                    inline=False,
                )
                e.set_footer(text=guild.name if guild else "Unknown Guild")

                debug = self.bot.get_channel(self.bot.config["debug_channel"])
                try:
                    if debug:
                        await debug.send(embed=e)
                except ignored_exceptions:
                    pass
                for _queued_log in logs:
                    self.queue[guild_id].task_done()
                continue

            for queued_log in logs:
                self.update_local_delivery(
                    queued_log,
                    "delivered",
                    delivered_messages[-1] if delivered_messages else None,
                )
                self.record_delivery_success(guild_id)
            if collapsible and delivered_messages:
                self.remember_log_delivery(
                    guild_id,
                    channel,
                    log,
                    delivered_messages[-1],
                    mirrored_messages,
                    count=len(logs) if duplicate_batch else 1,
                )
            else:
                self.last_log_deliveries.setdefault(guild_id, {}).pop(
                    channel.id, None
                )
            self.recent_logs[guild_id].extend(
                [queued_log.embeds, queued_log.created_at]
                for queued_log in logs
            )
            cutoff = time() - (60 * 60 * 24)
            self.recent_logs[guild_id] = [
                log_data
                for log_data in self.recent_logs[guild_id]
                if log_data[1] >= cutoff
            ][-self.recent_log_limit:]

            for _queued_log in logs:
                self.queue[guild_id].task_done()

    async def mirror_to_category_thread(self, guild: Guild, channel, log: Log) -> List[Message]:
        """Mirror logs into category threads for the guilds using that layout."""
        if guild.id not in self.threaded_log_guilds or not isinstance(channel, TextChannel):
            return []

        category = next(
            (name for name, events in self.categories.items() if log.type in events),
            None,
        )
        if category is None:
            return []

        mirrored_messages = []
        try:
            thread = utils.get(channel.threads, name=category)
            if thread is None:
                async for archived_thread in channel.archived_threads():
                    if archived_thread.name == category:
                        thread = archived_thread
                        break
            if thread is None:
                thread = await channel.create_thread(
                    name=category,
                    type=ChannelType.public_thread,
                    reason="Logging",
                )
            # discord.File objects cannot be sent twice, so attachments remain on
            # the primary log message. Construct a fresh link view for the mirror.
            for batch_index, batch in enumerate(self.embed_batches(log.embeds)):
                mirrored_messages.append(
                    await thread.send(
                        embeds=batch,
                        silent=True,
                        allowed_mentions=AllowedMentions.none(),
                        view=(
                            self.build_log_view(log.links)
                            if batch_index == 0
                            else None
                        ),
                    )
                )
        except (Forbidden, NotFound, HTTPException) as error:
            self.bot.log.critical(
                f"Failed to mirror {log.type} in guild {guild.id}: {error}"
            )
        return mirrored_messages

    async def init_invites(self) -> None:
        """ Indexes each server's invites """
        semaphore = asyncio.Semaphore(5)

        async def refresh_guild(guild_id: str) -> None:
            guild = self.bot.get_guild(int(guild_id))
            if not isinstance(guild, Guild):
                return
            self.invites.setdefault(guild_id, {})
            async with semaphore:
                try:
                    invites = await asyncio.wait_for(guild.invites(), timeout=10)
                except (Forbidden, HTTPException, asyncio.TimeoutError):
                    return
            for invite in invites:
                self.invites[guild_id][invite.url] = invite.uses

        await asyncio.gather(
            *(refresh_guild(guild_id) for guild_id in list(self.config.keys()))
        )

    @staticmethod
    def past():
        """Get the lower bound of the audit-correlation window in UTC."""
        return datetime.now(tz=timezone.utc) - timedelta(seconds=10)

    async def open_logger_menu(
        self,
        ctx: Context,
        *,
        page: str = "overview",
    ) -> LoggerMenu:
        """Open the logger-specific control center at a requested page."""
        menu = LoggerMenu(self, ctx, initial_page=page)
        await menu.start()
        return menu

    @commands.group(
        name="logger",
        aliases=["log", "logging"],
        description="Opens the interactive logger control center",
    )
    @commands.cooldown(2, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def logger(self, ctx: Context):
        if not ctx.invoked_subcommand:
            await self.open_logger_menu(ctx)

    @logger.group(
        name="archive",
        aliases=["history", "storage", "local"],
        description="Manages optional local Logging history",
        invoke_without_command=True,
    )
    @commands.has_permissions(administrator=True)
    async def logger_archive(self, ctx: Context):
        config = await self.archive_command_config(ctx)
        if config is None:
            return
        guild_id = str(ctx.guild.id)
        await self.local_archive.flush()
        stats = await self.local_archive.stats(guild_id)
        archive = self.archive_config(guild_id)
        used = stats["stored_bytes"]
        percent = min(used / MAX_GUILD_BYTES, 1) if MAX_GUILD_BYTES else 0
        e = Embed(
            title="Local Logging History",
            color=green if archive.get("enabled") else light_grey,
            description=(
                f"{'🟢 **On**' if archive.get('enabled') else '⚪ **Off**'} — "
                + (
                    "new Logging events and message snapshots are being stored."
                    if archive.get("enabled")
                    else "nothing new is being stored; existing history is preserved."
                )
            ),
        )
        e.add_field(
            name="Storage",
            value=(
                f"**{self.format_storage_size(used)} / 1.0 GB** ({percent:.1%})\n"
                "The oldest data is removed automatically at the limit."
            ),
            inline=False,
        )
        e.add_field(
            name="Saved",
            value=(
                f"Logging events: `{stats['log_count']}`\n"
                f"Message snapshots: `{stats['message_count']}`\n"
                f"Stored files: `{stats.get('file_count', 0)}`"
            ),
        )
        e.add_field(
            name="Options",
            value=(
                f"Keep for: `{archive['retention_days']} days`\n"
                f"Recover uncached messages: "
                f"`{'On' if archive.get('cache_messages', True) else 'Off'}`\n"
                f"Store images/files: "
                f"`{'On' if archive.get('store_attachments') else 'Off'}`\n"
                f"Per-file limit: `{archive.get('attachment_size_limit_mb', 25)} MB`"
            ),
        )
        p = ctx.clean_prefix
        e.add_field(
            name="Commands",
            value=(
                f"`{p}log archive enable [days]`\n"
                f"`{p}log archive retention <days>`\n"
                f"`{p}log archive files <on|off>`\n"
                f"`{p}log archive file-limit <MB>`\n"
                f"`{p}log archive search [words]`\n"
                f"`{p}log archive disable` / `clear`"
            ),
            inline=False,
        )
        e.set_footer(
            text=(
                "Local history is opt-in. Oversized files keep their embed and "
                "attachment details without the file body."
            )
        )
        await ctx.send(embed=e)

    @logger_archive.command(name="enable", aliases=["on"])
    @commands.has_permissions(administrator=True)
    async def logger_archive_enable(
        self, ctx: Context, retention_days: int = DEFAULT_RETENTION_DAYS
    ):
        config = await self.archive_command_config(ctx)
        if config is None:
            return
        if not 1 <= retention_days <= MAX_RETENTION_DAYS:
            return await ctx.send("Choose a retention period from 1 to 365 days.")
        archive = config["local_archive"]
        archive["enabled"] = True
        archive["retention_days"] = retention_days
        await self.save_data()
        await self.local_archive.prune_guild(str(ctx.guild.id), retention_days)
        await ctx.send(
            f"Local Logging history is now on for `{retention_days}` days. "
            "The fixed storage limit is 1 GB for this server. "
            + (
                "Image and file storage is also on."
                if archive.get("store_attachments")
                else "Image and file storage remains off until you turn it on separately."
            )
        )

    @logger_archive.command(name="disable", aliases=["off"])
    @commands.has_permissions(administrator=True)
    async def logger_archive_disable(self, ctx: Context):
        config = await self.archive_command_config(ctx)
        if config is None:
            return
        config["local_archive"]["enabled"] = False
        await self.save_data()
        await ctx.send(
            "Local Logging history is off. Existing history is preserved until it "
            f"expires or you run `{ctx.clean_prefix}log archive clear`."
        )

    @logger_archive.command(name="retention", aliases=["keep", "days"])
    @commands.has_permissions(administrator=True)
    async def logger_archive_retention(self, ctx: Context, retention_days: int):
        config = await self.archive_command_config(ctx)
        if config is None:
            return
        if not 1 <= retention_days <= MAX_RETENTION_DAYS:
            return await ctx.send("Choose a retention period from 1 to 365 days.")
        config["local_archive"]["retention_days"] = retention_days
        await self.save_data()
        await self.local_archive.prune_guild(str(ctx.guild.id), retention_days)
        await ctx.send(
            f"Local history will be kept for `{retention_days}` days. "
            "Older data has been removed now."
        )

    @logger_archive.command(name="files", aliases=["attachments", "images"])
    @commands.has_permissions(administrator=True)
    async def logger_archive_files(self, ctx: Context, enabled: Optional[bool] = None):
        config = await self.archive_command_config(ctx)
        if config is None:
            return
        archive = config["local_archive"]
        if enabled is None:
            enabled = not archive.get("store_attachments", False)
        archive["store_attachments"] = enabled
        await self.save_data()
        state = "on" if enabled else "off"
        size_limit = archive.get("attachment_size_limit_mb", 25)
        await ctx.send(
            f"Local image and file storage is now **{state}**. "
            + (
                (
                    f"Files up to {size_limit} MB each (50 MB per message) count "
                    "toward this server's 1 GB limit."
                    if archive.get("enabled")
                    else "It will begin when local history is turned on. Files up to "
                    f"{size_limit} MB each (50 MB per message) count toward this "
                    "server's 1 GB limit."
                )
                if enabled
                else "Attachment names and links, plus the log embed, will still be "
                "recorded."
            )
        )

    @logger_archive.command(
        name="file-limit",
        aliases=["attachment-limit", "file-size", "filesize"],
    )
    @commands.has_permissions(administrator=True)
    async def logger_archive_file_limit(self, ctx: Context, size_mb: int):
        config = await self.archive_command_config(ctx)
        if config is None:
            return
        maximum = MAX_ATTACHMENT_BYTES // 1024**2
        if not 1 <= size_mb <= maximum:
            return await ctx.send(
                f"Choose a per-file limit from 1 to {maximum} MB."
            )
        config["local_archive"]["attachment_size_limit_mb"] = size_mb
        await self.save_data()
        await ctx.send(
            f"Future local file saves are limited to `{size_mb} MB` per attachment. "
            "Larger attachments will keep their embed and metadata without the file body."
        )

    @logger_archive.command(name="recovery", aliases=["messages", "cache"])
    @commands.has_permissions(administrator=True)
    async def logger_archive_recovery(
        self, ctx: Context, enabled: Optional[bool] = None
    ):
        config = await self.archive_command_config(ctx)
        if config is None:
            return
        archive = config["local_archive"]
        if enabled is None:
            enabled = not archive.get("cache_messages", True)
        archive["cache_messages"] = enabled
        await self.save_data()
        await ctx.send(
            "Uncached message recovery is now "
            f"**{'on' if enabled else 'off'}**."
        )

    async def build_archive_search_embed(
        self,
        guild: Guild,
        *,
        query: Optional[str] = None,
    ) -> Optional[Embed]:
        """Build a private history result embed shared by commands and the menu."""
        result = await self.local_archive.search_logs(
            str(guild.id), limit=10, query=query
        )
        entries = result["entries"]
        if not entries:
            return None
        safe_query = None
        if query:
            safe_query = utils.escape_mentions(utils.escape_markdown(query)).replace(
                "`", "ˋ"
            )
        embed = Embed(
            title=f"Local Logging History — {guild.name}",
            color=fate,
            description=(
                f"Newest {len(entries)} result(s)"
                + (f" for `{safe_query}`" if safe_query else "")
                + (
                    "\nNo exact spelling matched, so these are one-letter close matches."
                    if result.get("match_mode") == "fuzzy"
                    else ""
                )
            ),
        )
        for row in entries:
            payload = row.get("payload", {})
            embeds = payload.get("embeds") or []
            primary = embeds[0] if embeds else {}
            author = primary.get("author") or {}
            title = (
                author.get("name")
                or primary.get("title")
                or row["event_type"].replace("_", " ").title()
            )
            summary = primary.get("description") or "No text summary"
            summary = utils.escape_mentions(str(summary)).replace("```", "``\u200b`")
            destination = (
                f"<#{row['channel_id']}>"
                if row.get("channel_id")
                else "No destination"
            )
            embed.add_field(
                name=str(title)[:256],
                value=(
                    f"<t:{int(row['created_at'])}:R> • {destination} • "
                    f"`{row['delivery_status']}`\n{summary[:420]}"
                ),
                inline=False,
            )
        embed.set_footer(
            text="Sent privately because saved logs can contain sensitive content."
        )
        return embed

    @logger_archive.command(name="search", aliases=["find"])
    @commands.has_permissions(administrator=True)
    async def logger_archive_search(self, ctx: Context, *, query: str = None):
        config = await self.archive_command_config(ctx)
        if config is None:
            return
        if query and len(query) > 200:
            return await ctx.send("Keep searches to 200 characters or fewer.")
        embed = await self.build_archive_search_embed(ctx.guild, query=query)
        if embed is None:
            return await ctx.send("No saved Logging events matched that search.")
        try:
            await ctx.author.send(embed=embed)
        except (Forbidden, HTTPException):
            return await ctx.send(
                "I couldn't send the results privately. Enable direct messages and try again."
            )
        await ctx.send("I sent the matching Logging history to your direct messages.")

    @logger_archive.command(name="clear", aliases=["erase", "purge"])
    @commands.has_permissions(administrator=True)
    async def logger_archive_clear(self, ctx: Context):
        config = self.config.get(str(ctx.guild.id))
        if (
            config
            and config.get("secure")
            and ctx.author.id != ctx.guild.owner.id
        ):
            return await ctx.send(
                "Only the server owner can clear local history while secure Logging is on."
            )
        if not await GetConfirmation(
            ctx,
            "Permanently delete this server's saved Logging history and message snapshots?",
        ):
            return
        archive = config.get("local_archive") if config else None
        was_enabled = bool(archive and archive.get("enabled"))
        if archive:
            archive["enabled"] = False
            await self.save_data()
        await self.local_archive.clear_guild(str(ctx.guild.id))
        if archive:
            archive["enabled"] = was_enabled
            await self.save_data()
        await ctx.send("This server's locally saved Logging history has been cleared.")

    @logger.command(name="health", aliases=["status"], description="Shows this logger's runtime health")
    async def logger_health(self, ctx: Context):
        guild_id = str(ctx.guild.id)
        view = LoggerHealthView(self, ctx.author.id, guild_id)
        await ctx.send(embed=self.build_health_embed(guild_id), view=view)

    @logger.command(name="dashboard", description="Shows global logger queue and worker health")
    async def logger_dashboard(self, ctx: Context, guild_id: Optional[int] = None):
        if ctx.guild.id != self.dashboard_guild_id:
            return await ctx.send("The global logger dashboard isn't available in this server")

        selected_guild_id = str(guild_id) if guild_id is not None else None
        if selected_guild_id is not None and selected_guild_id not in self.config:
            return await ctx.send("That server doesn't have a configured logger")

        view = LoggerHealthView(self, ctx.author.id, selected_guild_id)
        if selected_guild_id is None:
            embed = self.build_dashboard_embed()
        else:
            embed = self.build_health_embed(selected_guild_id)
        await ctx.send(embed=embed, view=view)

    @logger.command(name="enable", description="Enables the module and creates a log channel")
    async def _enable(self, ctx: Context, *, event: str = None):
        """ Creates a multi-log """
        guild_id = str(ctx.guild.id)
        if event:
            if guild_id not in self.config:
                return await ctx.send("Logger isn't enabled")
            replies = []
            events = []
            if event == "*":
                self.config[guild_id]["disabled"].clear()
                replies.append("Enabled `all` events")
            else:
                for chunk in event.split(", "):
                    for sub_chunk in chunk.split():
                        if sub_chunk == "*":
                            events = copy(self.config[guild_id]["disabled"])
                            break
                        elif sub_chunk.endswith("*"):
                            sub_chunk = sub_chunk.rstrip("*")
                            for event in self.log_types:
                                if event.startswith(sub_chunk):
                                    events.append(event)
                        else:
                            events.append(sub_chunk)
                for event in events:
                    if event not in self.log_types:
                        replies.append(f"`{event}` isn't a valid event. Use `.log events` to view the available log types")
                    elif event not in self.config[guild_id]["disabled"]:
                        replies.append(f"`{event}` isn't disabled")
                    else:
                        self.config[guild_id]["disabled"].remove(event)
                        replies.append(f"Enabled `{event}`")
            e = Embed(color=fate)
            e.description = chain("\n".join(replies)[:3900], skip_first=False)
            await ctx.send(embed=e)
            return await self.save_data()
        if guild_id in self.config:
            return await ctx.send("Logger is already enabled")
        perm_error = (
            "I can't send messages in the channel I made\n"
            "If you want me to use your own, try `.logger setchannel`"
        )
        try:
            channel = await ctx.guild.create_text_channel(name="discord-log")
        except Forbidden:
            return await ctx.send(perm_error)
        if not channel.permissions_for(ctx.guild.me).send_messages:
            try:
                await ctx.send(perm_error)
            except Forbidden:
                pass
            return
        if not channel.permissions_for(ctx.guild.me).embed_links:
            return await ctx.send("I need embed_links permission(s) in that channel")
        if not channel.permissions_for(ctx.guild.me).attach_files:
            return await ctx.send("I need attach_files permission(s) in that channel")
        await self.enable_for_guild(ctx.guild.id, channel.id)
        await ctx.send("Enabled Logger")
        await self.bot.create_log(
            message=f"!on **Logger** - `{ctx.guild}`",
            channel="module_log",
            embedded=True,
            color=green
        )

    @logger.command(name="disable", description="Disables the module")
    async def _disable(self, ctx: Context, *, event: str = None):
        """Disables the log"""
        guild_id = str(ctx.guild.id)
        if event:
            if guild_id not in self.config:
                return await ctx.send("Logger isn't enabled")
            replies = []
            events = []
            if event == "*":
                self.config[guild_id]["disabled"] = copy(self.log_types)
                replies.append("Disabled `all` events")
            else:
                for chunk in event.split(", "):
                    for sub_chunk in chunk.split():
                        if sub_chunk == "*":
                            events = copy(self.log_types)
                            break
                        elif sub_chunk.endswith("*"):
                            sub_chunk = sub_chunk.rstrip("*")
                            for event in self.log_types:
                                if event.startswith(sub_chunk):
                                    events.append(event)
                        else:
                            events.append(sub_chunk)
                for event in events:
                    if event not in self.log_types:
                        replies.append(f"`{event}` isn't a valid event. Use `.log events` to view the available log types")
                    elif event in self.config[guild_id]["disabled"]:
                        replies.append(f"`{event}` is already disabled")
                    else:
                        self.config[guild_id]["disabled"].append(event)
                        replies.append(f"Disabled {event}")
            e = Embed(color=fate)
            e.description = chain("\n".join(replies)[:3900], skip_first=False)
            await ctx.send(embed=e)
            return await self.save_data()
        if guild_id not in self.config:
            return await ctx.send("Logger isn't enabled")
        if self.config[guild_id]["secure"]:
            if ctx.author.id != ctx.guild.owner.id:
                return await ctx.send(
                    "Due to security settings, only the owner of the server can use this"
                )
        await self.disable_for_guild(ctx.guild.id)
        await ctx.send("Disabled Logger")
        await self.bot.create_log(
            message=f"!off **Logger** - `{ctx.guild}`",
            channel="module_log",
            embedded=True,
            color=red
        )

    @logger.command(name="split", description="Splits logging across separate threads")
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def split(self, ctx: Context):
        guild_id = str(ctx.guild.id)
        if guild_id not in self.config:
            return await ctx.send("Logger isn't enabled")
        if not await GetConfirmation(ctx, "Are you sure you wanna split all the log events into different threads?"):
            return
        for name, events in self.categories.items():
            thread = await ctx.channel.create_thread(name=name, type=ChannelType.public_thread)
            for log_type in events:
                self.config[guild_id]["channels"][log_type] = thread.id
        await self.save_data()
        await ctx.send(
            "Alright done. If the way I organized them isn't quite right; go to the new thread you want an event in, "
            "and run `.log move [event_name]`. To reset back to normal run `.log move *` in the main channel"
        )

    @logger.command(name="set-channel", aliases=["setchannel"], description="Enables the module in a specific channel")
    async def _set_channel(self, ctx, channel: TextChannel = None):
        """ Creates a multi-log """
        if not channel:
            channel = ctx.channel
        if not channel.permissions_for(ctx.guild.me).send_messages:
            try:
                await ctx.send("I can't send messages in that channel")
            except Forbidden:
                pass
            return
        if not channel.permissions_for(ctx.guild.me).embed_links:
            return await ctx.send("I need embed_links permission(s) in that channel")
        if not channel.permissions_for(ctx.guild.me).attach_files:
            return await ctx.send("I need attach_files permission(s) in that channel")
        guild_id = str(ctx.guild.id)
        if guild_id in self.config:
            self.config[guild_id]["channel"] = channel.id
            if self.config[guild_id]["channels"]:
                resp = await GetConfirmation(
                    ctx, f"Do you want all the redirected logs switched to {channel.mention} as well?"
                )
                if resp:
                    self.config[guild_id]["channels"] = {}
        else:
            self.config[guild_id] = deepcopy(default_config)
            self.config[guild_id]["channel"] = channel.id
        if guild_id not in self.queue:
            self.queue[guild_id] = asyncio.Queue(maxsize=self.queue_size)
        self.start_worker(guild_id)
        await ctx.send(f"Enabled Logger in {channel.mention}")
        await self.save_data()
        await self.bot.create_log(
            message=f"!on **Logger** - `{ctx.guild}`",
            channel="module_log",
            embedded=True,
            color=green
        )

    @logger.command(name="set-theme", description="Sets the color scheme for logs", aliases=["themes"])
    async def set_theme(self, ctx: Context):
        guild_id = str(ctx.guild.id)
        options = {
            "Default": "My preset log colors",
            "Role Color": "For who the logs related to",
            "Solid Color": "Set every log the same color",
            "RGB": "Makes your server faster",
            "Custom": f"Uses {ctx.prefix}log set-color"
        }
        if "colors" in self.config[guild_id]:
            options["Reset Custom"] = "Erase all log colors"
        if not (choice := await GetChoice(ctx, options, placeholder="Choose a theme")):
            return

        # Themes needing extra configuration
        match choice:
            case "Default":
                choice = None
            case "Solid Color":
                await ctx.send("Reply with the hex or rgb you wanna use")
                reply = await self.bot.utils.get_message(ctx)
                try:
                    # Hex
                    if "#" in reply.content:
                        value = int("0x" + reply.content.lstrip("#"), 0)
                        color = Color(value)
                    # RGB
                    else:
                        stripped = (
                            reply.content
                                .strip("()")
                                .replace(", ", " ")
                                .replace(",", " ")
                        )
                        rgb = [int(value) for value in stripped.split()]
                        color = Color.from_rgb(*rgb)
                except (TypeError, ValueError):
                    return await ctx.send("That's not a valid color. Rerun the command to retry")
                self.config[guild_id]["color"] = color.value
            case "Custom":
                if "colors" not in self.config[guild_id]:
                    self.config[guild_id]["colors"] = {}
            case "Reset Custom":
                if self.config[guild_id]["theme"] == "Custom":
                    self.config[guild_id]["colors"] = {}
                else:
                    del self.config[guild_id]["colors"]
                await self.save_data()
                return await ctx.send("Reset custom log colors 👍")
        if self.config[guild_id]["theme"] != "Custom":
            if len(self.config[guild_id].get("colors", {})):
                del self.config[guild_id]["colors"]
        if choice != "Solid Color" and "color" in self.config[guild_id]:
            del self.config[guild_id]["color"]

        self.config[guild_id]["theme"] = choice
        await ctx.send(f"Set the theme to `{choice or 'Default'}`")
        await self.save_data()

    @logger.command(name="set-color", description="Sets a single log types color")
    async def set_color(self, ctx: Context, event: str, color: Color):
        if event not in self.log_types:
            return await ctx.send(f"`{event}` isn't a log event! Run `{ctx.invoked_with}log events` to see what you can use")
        guild_id = str(ctx.guild.id)
        self.config[guild_id]["theme"] = "Custom"
        self.config[guild_id].setdefault("colors", {})[event] = color.value
        await ctx.send(f"Set the color for `{event}` 👍")
        await self.save_data()

    @logger.command(name="move", description="Sets a log type to send to a diff channel")
    async def _move(self, ctx, log_type=None, channel: Union[TextChannel, Thread] = None):
        """ Switches a log between multi and single """
        guild_id = str(ctx.guild.id)
        if guild_id not in self.config:
            return await ctx.send("Logger isn't enabled")
        if not log_type:
            return await ctx.send("Usage: `.log move event_name #channel`\n"
                                  "the event name can be found with `.log events`")
        log_types = []
        if len(log_type) > 1 and log_type.endswith("*"):
            name = log_type.rstrip("*")
            log_types.extend(
                event for event in self.log_types if event.startswith(name)
            )
        else:
            log_types.append(log_type)
        replies = []
        for event in log_types:
            if event not in self.log_types:
                replies.append(chain(
                    f"`{event}` isn't a valid log type. Here's a list of the accepted types:\n"
                    f"{', '.join(f'`{ltype}`' for ltype in self.log_types)}"
                ))
                break
            await self.ensure_channels(ctx.guild)
            if not channel:
                channel = ctx.channel
            if channel.id == self.config[guild_id]["channel"]:
                if event not in self.config[guild_id]["channels"]:
                    replies.append(f"{event} is already in {channel.mention}")
                else:
                    del self.config[guild_id]["channels"][event]
                    replies.append(f"Moved {event} to {channel.mention}")
            elif event in self.config[guild_id]["channels"] and channel.id == self.config[guild_id]["channels"][event]:
                replies.append(f"`{event}` is already in {channel.mention}")
            else:
                self.config[guild_id]["channels"][event] = channel.id
                replies.append(f"Moved {event} to {channel.mention}")
        e = Embed(color=fate)
        e.description = chain("\n".join(replies), skip_first=False)
        await ctx.send(embed=e)
        await self.save_data()

    @logger.command(name="events", description="Lists all the names of logs")
    @commands.cooldown(1, 25, commands.BucketType.channel)
    async def events(self, ctx: Context):
        await ctx.send(
            f"Log types: {', '.join(f'`{ltype}`' for ltype in self.log_types)}"
        )

    @logger.command(name="security", description="Toggles resending deleted logs")
    @is_guild_owner()
    async def _toggle_security(self, ctx: Context):
        """ Toggles whether to keep the log secure """
        guild_id = str(ctx.guild.id)
        if self.config[guild_id]["secure"]:
            self.config[guild_id]["secure"] = False
            await ctx.send("Disabled secure features")
        else:
            self.config[guild_id]["secure"] = True
            await ctx.send("Enabled secure features")
        await self.save_data()

    @logger.command(name="ignore", description="Ignores chat events from a channel")
    async def _ignore(
        self,
        ctx: Context,
        *,
        target: Union[User, Member, TextChannel, VoiceChannel],
    ):
        """ ignore channels and/or bots """
        guild_id = str(ctx.guild.id)
        if self.config[guild_id]["secure"] and ctx.author.id != ctx.guild.owner.id:
            return await ctx.send(
                "Due to security settings, only the owner of the server can use this"
            )

        if isinstance(target, (User, Member)):
            if target.id in self.config[guild_id]["ignored_bots"]:
                return await ctx.send(f"{target.mention} is already ignored")
            else:
                self.config[guild_id]["ignored_bots"].append(target.id)
        else:
            if target.id in self.config[guild_id]["ignored_channels"]:
                return await ctx.send(f"{target.mention} is already ignored")
            else:
                self.config[guild_id]["ignored_channels"].append(target.id)
        await ctx.send(
            f"I'll now ignore spammish events from {target.mention}"
        )
        await self.save_data()

    @logger.command(name="unignore", description="Unignores chat events from a channel")
    @commands.has_permissions(administrator=True)
    async def _unignore(
        self,
        ctx,
        *,
        target: Union[User, Member, TextChannel, VoiceChannel],
    ):
        """ unignore channels and/or bots """
        guild_id = str(ctx.guild.id)
        if self.config[guild_id]["secure"] and ctx.author.id != ctx.guild.owner.id:
            return await ctx.send(
                "Due to security settings, only the owner of the server can use this"
            )
        if isinstance(target, (User, Member)):
            if target.id not in self.config[guild_id]["ignored_bots"]:
                return await ctx.send(f"{target.mention} isn't ignored")
            self.config[guild_id]["ignored_bots"].remove(target.id)
        else:
            if target.id not in self.config[guild_id]["ignored_channels"]:
                return await ctx.send(f"{target.mention} isn't ignored")
            else:
                self.config[guild_id]["ignored_channels"].remove(target.id)
        await ctx.send(
            f"I'll no longer ignore chat related events from {target.mention}"
        )
        await self.save_data()

    @logger.command(
        name="config",
        aliases=["settings"],
        description="Opens logger setup and configuration",
    )
    @commands.has_permissions(manage_channels=True)
    async def _config(self, ctx: Context):
        """Open the dedicated logger control center on its setup page."""
        await self.open_logger_menu(ctx, page="setup")

    @commands.Cog.listener()
    async def on_ready(self):
        for guild_id in list(self.config.keys()):
            self.start_worker(guild_id)
        self.start_invite_initialization()

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry):
        """Cache precise gateway audit entries for nearby domain events."""
        guild_id = str(entry.guild.id)
        if guild_id not in self.config:
            return
        cutoff = utcnow() - timedelta(seconds=30)
        entries = audit_entry_cache.setdefault(entry.guild.id, [])
        entries.append(entry)
        audit_entry_cache[entry.guild.id] = [
            item for item in entries
            if item.created_at > cutoff
        ][-100:]

    @commands.Cog.listener()
    async def on_typing(self, channel, user, _when):
        guild = getattr(channel, "guild", None)
        if not isinstance(guild, Guild):
            return
        guild_id = str(guild.id)
        if guild_id not in self.config:
            return
        if self.is_ignored_channel(guild_id, channel):
            return
        if not await self.bot.get_privacy(user, "mod_logs"):
            return

        now = time()
        if channel.id not in self.typing:
            self.typing[channel.id] = {}
        self.typing[channel.id][user.id] = now
        try:
            await self.bot.wait_for(
                "message",
                check=lambda m: m.author.id == user.id and m.channel.id == channel.id,
                timeout=60
            )
        except asyncio.TimeoutError:
            guild = channel.guild
            still_indexed = (
                isinstance(guild, Guild)
                and channel.id in self.typing
                and user.id in self.typing[channel.id]
            )
            if not still_indexed:
                return
            if now == self.typing[channel.id][user.id]:
                async for msg in channel.history(limit=10):
                    if user.id == msg.author.id:
                        if msg.edited_at and msg.edited_at > utcnow() - timedelta(seconds=65):
                            return

                role = await self.bot.attrs.get_mute_role(channel.guild)
                ghost_typed = (
                    (member := channel.guild.get_member(user.id))
                    and not member.is_timed_out()
                    and role not in member.roles
                )
                if ghost_typed:
                    typing_began = datetime.fromtimestamp(now, timezone.utc)
                    e = Embed(color=0x000000)
                    e.set_author(name="Ghost Typing", icon_url=get_avatar(member))
                    e.set_thumbnail(url=member.display_avatar.url)
                    e.description = chain(
                        f"{user.mention} typed but didn't send\n"
                        f"In {channel.mention}\n"
                        f"Began {utils.format_dt(typing_began, style='F')} "
                        f"({utils.format_dt(typing_began, style='R')})"
                    )
                    self.add_to_queue(
                        guild_id,
                        "ghost_typing",
                        embed=e,
                        target=member,
                        channel=channel,
                        links=[("Open channel", channel.jump_url, "↗️")],
                    )

        # Cleanup
        if channel.id in self.typing and user.id in self.typing[channel.id]:
            if self.typing[channel.id][user.id] == now:
                del self.typing[channel.id][user.id]
        for channel_id, items in list(self.typing.items()):
            await asyncio.sleep(0)
            if not items and channel_id in self.typing:
                del self.typing[channel_id]

    async def run_checks(self, object: Any) -> str:
        if guild := getattr(object, "guild", None):
            guild_id = guild.id
        else:
            guild_id = getattr(object, "guild_id", None)
        if guild_id:
            guild_id = str(guild_id)
        if not guild_id or guild_id not in self.config:
            raise IgnoredExit()

        if isinstance(object, Message):
            if object.id in self.bot.suppressed:
                raise IgnoredExit()

        channel = getattr(object, "channel", object)
        if (
            isinstance(channel, (TextChannel, Thread))
            and self.is_ignored_channel(guild_id, channel)
        ):
            raise IgnoredExit()

        return guild_id

    def is_ignored_channel(self, guild_id: str, channel) -> bool:
        """Return whether a channel or its thread parent is filtered."""
        channel_ids = {channel.id, getattr(channel, "parent_id", None)}
        return bool(
            channel_ids & set(self.config[guild_id].get("ignored_channels", []))
        )

    @commands.Cog.listener()
    async def on_message(self, msg: Message):
        """ @everyone and @here event """
        if msg.author.id == self.bot.user.id:
            return
        guild_id = await self.run_checks(msg)
        await self.capture_message_snapshot(guild_id, msg)
        mentions = []
        if "@everyone" in msg.content and msg.mention_everyone:
            mentions.append("everyone")
        if "@here" in msg.content and msg.mention_everyone:
            mentions.append("here")
        color = None
        for role in list(set(msg.role_mentions)):
            if not color:
                color = role.color
            mentions.append(role)
        if mentions:
            e = Embed(color=color or white)
            e.set_thumbnail(url=msg.author.display_avatar.url)
            icon_url = msg.author.avatar.url if msg.author.avatar else msg.author.display_avatar.url
            if len(mentions) == 1:
                mention = mentions[0]
                title = mention
                if isinstance(mention, Role):
                    title = "Role"
                e.set_author(name=f"{title.title()} Mentioned", icon_url=icon_url)
                action = f"{msg.author.mention} pinged {getattr(mention, 'mention', '@' + title)}"
            else:
                e.set_author(name=f"{len(mentions)} Mentions", icon_url=icon_url)
                action = f"{msg.author.mention} pinged {len(mentions)} mentions"
            safe_content = utils.escape_markdown(msg.content)
            e.description = chain(
                f"{action}\n"
                f"In {msg.channel.mention}\n"
                f"**[Jump URL]({msg.jump_url})**"
            ) + f"\n\n>>> {safe_content}"
            files = []
            for attachment in msg.attachments[:10]:
                if attachment.size > msg.guild.filesize_limit:
                    continue
                with suppress(NotFound, Forbidden, HTTPException):
                    files.append(await attachment.to_file(use_cached=True))
            self.add_to_queue(
                guild_id,
                "role_mention",
                embed=e,
                files=files,
                actor=msg.author,
                target=mentions,
                target_label="Mentions",
                channel=msg.channel,
                details=self.message_log_details(msg),
                links=[("Open message", msg.jump_url, "↗️")],
                created_at=msg.created_at.timestamp(),
            )

    @commands.Cog.listener()
    async def on_message_edit(self, before: Message, after: Message):
        guild_id = await self.run_checks(after)
        if self.is_ignored_bot(guild_id, before.author.id):
            return
        await self.capture_message_snapshot(guild_id, after)
        if before.channel.id in self.typing:
            if before.author.id in self.typing[before.channel.id]:
                del self.typing[before.channel.id][before.author.id]

        if before.content != after.content:
            if before.channel.id in self.config[guild_id]["ignored_channels"]:
                return

            e = Embed(color=pink)
            e.set_author(
                name="Message Edited", icon_url=getattr(before.author.avatar, "url", before.author.display_avatar.url)
            )
            e.set_thumbnail(url=before.author.display_avatar.url)
            edited_at = message_edit_time(after)
            reference_line = ""
            if after.reference:
                reference_line = (
                    f"{emojis.creply} Reply to Message "
                    f"`{after.reference.message_id or 'Unknown'}`\n"
                )
            e.description = f"**{before.author} edited their message** {emojis.pin if after.pinned else ''}\n" \
                            f"{message_author_role_line(after.author)}" \
                            f"**Created** {utils.format_dt(before.created_at, style='F')} ({utils.format_dt(before.created_at, style='R')})\n" \
                            f"{emojis.creply} Edited {utils.format_dt(edited_at, style='F')} ({utils.format_dt(edited_at, style='R')})\n" \
                            f"{emojis.creply} In {before.channel.mention}\n" \
                            f"{emojis.creply} ID: `{before.id}`\n" \
                            f"{reference_line}" \
                            f"{emojis.creply} Line count: `{count_message_lines(before.content)}` → `{count_message_lines(after.content)}`\n" \
                            f"{emojis.reply} **[Jump URL]({before.jump_url})**"
            edit_diff = format_message_edit_diff(before.content, after.content)

            files = []
            if len(e.description) + len(edit_diff) > 4096:
                files = [
                    File(BytesIO(message.content.encode()), filename=f"{state}.txt")
                    for state, message in (("before", before), ("after", after))
                ]
            else:
                e.description += edit_diff
            self.add_to_queue(
                guild_id,
                "message_edit",
                embed=e,
                files=files,
                actor=before.author,
                target=f"Message `{before.id}`",
                target_label="Message",
                channel=before.channel,
                details=self.message_edit_log_details(after),
                links=[("Open message", after.jump_url, "↗️")],
                created_at=(after.edited_at or utcnow()).timestamp(),
            )

        before_attachments = {attachment.id: attachment for attachment in before.attachments}
        after_attachments = {attachment.id: attachment for attachment in after.attachments}
        if before_attachments != after_attachments:
            removed = [
                before_attachments[attachment_id]
                for attachment_id in before_attachments.keys() - after_attachments.keys()
            ]
            added = [
                after_attachments[attachment_id]
                for attachment_id in after_attachments.keys() - before_attachments.keys()
            ]
            files = []
            for attachment in removed[:10]:
                if attachment.size <= after.guild.filesize_limit:
                    with suppress(NotFound, Forbidden, HTTPException):
                        files.append(await attachment.to_file(use_cached=True))
            e = Embed(color=orange)
            e.set_author(name="Message Attachments Updated", icon_url=get_avatar(after.author))
            e.set_thumbnail(url=after.author.display_avatar.url)
            e.description = chain(
                f"{after.author.mention} updated message attachments\n"
                f"In {after.channel.mention}\n"
                f"**[Jump URL]({after.jump_url})**"
            )
            self.add_to_queue(
                guild_id,
                "attachment_update",
                embed=e,
                files=files,
                actor=after.author,
                target=f"Message `{after.id}`",
                target_label="Message",
                channel=after.channel,
                details={
                    **self.message_log_details(after),
                    "➕ Added": [
                        f"{attachment.filename} • {attachment.size:,} bytes"
                        for attachment in added
                    ] or ["None"],
                    "➖ Removed": [
                        f"{attachment.filename} • {attachment.size:,} bytes"
                        for attachment in removed
                    ] or ["None"],
                },
                links=[("Open message", after.jump_url, "↗️")],
                created_at=(after.edited_at or utcnow()).timestamp(),
            )

        if before.embeds and not after.embeds:
            if before.channel.id in self.config[guild_id]["ignored_channels"]:
                return

            if (
                before.channel.id == self.config[guild_id]["channel"]
                or before.channel.id in self.config[guild_id]["channels"].values()
            ) and before.author.id == self.bot.user.id:
                await asyncio.sleep(0.5)  # prevent updating too fast and not showing on the users end
                return await after.edit(suppress=False, embed=before.embeds[0])
            e = Embed(color=pink)
            e.set_author(name="Embed Hidden", icon_url=before.author.display_avatar.url)
            e.set_thumbnail(url=before.author.display_avatar.url)
            e.description = chain(
                f"On {after.author.mention}'s message\n"
                f"In {after.channel.mention}\n"
                f"Message ID: `{after.id}`\n"
                f"**[Jump URL]({after.jump_url})**"
            )
            e.set_footer(text="⇓ Embed ⇓")
            self.add_to_queue(
                guild_id,
                "embed_hidden",
                embeds=[e, *before.embeds],
                target=after.author,
                target_label="Message Author",
                channel=after.channel,
                links=[("Open message", after.jump_url, "↗️")],
            )

        if before.pinned != after.pinned:
            action = "Unpinned" if before.pinned else "Pinned"
            audit = await AuditLogSearch(
                after.guild,
                Action.message_pin,
                Action.message_unpin,
                channel=after.channel,
                message_id=after.id,
            )
            e = Embed(color=cyan)
            e.set_author(
                name=f"Message {action}", icon_url=after.author.display_avatar.url
            )
            e.set_thumbnail(url=audit.user_display_avatar or after.author.display_avatar.url)
            e.description = chain([
                f"{after.author.mention}'s msg {action}",
                f"In {after.channel.mention}",
                f"By {audit.user_mention}",
                f"[Jump to MSG]({after.jump_url})"
            ])
            for text_group in split(after.content, 1000):
                e.add_field(name="Content", value=text_group, inline=False)
            if after.attachments:
                e.set_image(url=after.attachments[0].url)
            self.add_to_queue(
                guild_id,
                "message_pin",
                embed=e,
                actor=audit.user,
                target=after.author,
                target_label="Message Author",
                channel=after.channel,
                reason=audit.reason,
                details={
                    **self.message_log_details(after),
                    "📌 State": action,
                },
                links=[("Open message", after.jump_url, "↗️")],
            )

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        channel = self.bot.get_channel(int(payload.data["channel_id"]))
        if isinstance(channel, (TextChannel, Thread)) and channel.guild:
            guild_id = str(channel.guild.id)
            if guild_id in self.config and not payload.cached_message:
                if channel.id in self.config[guild_id]["ignored_channels"]:
                    return
                if is_component_only_message_update(payload.data):
                    return
                author_id = payload.data.get("author", {}).get("id")
                if author_id and int(author_id) == self.bot.user.id:
                    return
                cached = self.bot.get_message(payload.message_id)
                if cached and cached.author.id == self.bot.user.id:
                    return
                snapshot = None
                if self.archive_enabled(guild_id):
                    snapshot = await self.local_archive.get_message(
                        guild_id, payload.message_id
                    )
                try:
                    msg = await self.fetch_message_for_log(
                        channel, payload.message_id
                    )
                except (NotFound, Forbidden):
                    return
                if msg is None:
                    return
                if self.is_ignored_bot(guild_id, msg.author.id):
                    return
                if msg.author.id == self.bot.user.id:
                    return
                e = Embed(color=pink)
                e.set_author(
                    name="Message Edited",
                    icon_url=get_avatar(msg.author),
                )
                e.set_thumbnail(url=msg.author.display_avatar.url)
                edited_at = message_edit_time(msg)
                description_lines = [
                    f"{msg.author.mention}'s message was edited",
                    f"In {msg.channel.mention}",
                    f"Sent {utils.format_dt(msg.created_at, style='R')}",
                    (
                        f"Edited {utils.format_dt(edited_at, style='F')} "
                        f"({utils.format_dt(edited_at, style='R')})"
                    ),
                    f"Message ID: `{msg.id}`",
                ]
                if msg.reference:
                    description_lines.append(
                        "Reply to Message "
                        f"`{msg.reference.message_id or 'Unknown'}`"
                    )
                e.description = chain("\n".join(description_lines))
                files = []
                if snapshot and snapshot.get("content") != msg.content:
                    before_content = snapshot.get("content", "")
                    edit_diff = format_message_edit_diff(
                        before_content,
                        msg.content,
                    )
                    if len(e.description) + len(edit_diff) > 4096:
                        files = [
                            File(BytesIO(before_content.encode()), filename="before.txt"),
                            File(BytesIO(msg.content.encode()), filename="after.txt"),
                        ]
                    else:
                        e.description += edit_diff
                elif len(msg.content) > 3000:
                    files.append(
                        File(
                            BytesIO(msg.content.encode()),
                            filename="current-message.txt",
                        )
                    )
                else:
                    e.description += f"\n\n>>> {utils.escape_markdown(msg.content)}"
                self.add_to_queue(
                    guild_id,
                    "message_edit",
                    embed=e,
                    files=files,
                    actor=msg.author,
                    target=f"Message `{msg.id}`",
                    target_label="Message",
                    channel=msg.channel,
                    details=self.message_edit_log_details(msg),
                    links=[("Open message", msg.jump_url, "↗️")],
                    created_at=(msg.edited_at or utcnow()).timestamp(),
                )
                await self.capture_message_snapshot(guild_id, msg)

    @commands.Cog.listener()
    async def on_message_delete(self, msg: Message):
        if isinstance(msg.guild, Guild):
            guild_id = str(msg.guild.id)
            if guild_id in self.config:
                if (
                    self.archive_enabled(guild_id)
                    and not self.archive_config(guild_id).get("store_attachments")
                ):
                    self.local_archive.forget_messages(guild_id, [msg.id])
                if self.config[guild_id]["secure"]:
                    protected_channel = (
                        msg.channel.id == self.config[guild_id]["channel"]
                        or msg.channel.id
                        in self.config[guild_id]["channels"].values()
                    )
                    if protected_channel:
                        await self.restore_secure_log_message(msg)
                        return

                if msg.id in self.bot.suppressed:
                    return
                if msg.author.id in self.config[guild_id]["ignored_bots"]:
                    return
                if msg.channel.id in self.config[guild_id]["ignored_channels"]:
                    return
                if (
                    msg.channel.id == self.config[guild_id]["channel"]
                    and not self.config[guild_id]["secure"]
                    and msg.author.id == self.bot.user.id
                ):
                    return

                audit = await AuditLogSearch(
                    msg.guild,
                    Action.message_delete,
                    target=msg.author,
                    channel=msg.channel,
                )

                e = Embed(color=purple)
                e.set_author(name="Message Deleted", icon_url=Images.trash)
                e.set_thumbnail(url=msg.author.display_avatar.url)
                e.description = "A message"
                if msg.pinned:
                    e.description = f"A {emojis.pin}'d message"
                e.description += f" by {msg.author.mention} was deleted"
                channel = getattr(msg.channel, 'mention', f"<#{msg.channel.id}>")
                e.description += f"\n{emojis.creply} In {channel}"
                if audit.user:
                    e.description += f"\n{emojis.creply} By {audit.user_mention}"

                if msg.edited_at:
                    e.description += f"\n{emojis.creply} Edited {utils.format_dt(msg.edited_at, style='R')}"

                e.description += (
                    f"\n{emojis.creply} Message ID: `{msg.id}`"
                )

                # Format the message content
                if msg.content:
                    content = msg.content[:1900] + ("..." if len(msg.content) > 1900 else "")
                    content = utils.escape_markdown(content)
                    if "\n> " not in content:
                        content = "> " + content.replace("\n", "\n> ")
                    e.description += f"\n\n{content}"

                # Show any message replies
                if msg.reference and msg.reference.cached_message:
                    await asyncio.sleep(0)
                    m = msg.reference.cached_message
                    reply_content = m.content[:1900] + ("..." if len(m.content) > 1900 else "")
                    reply_content = utils.escape_markdown(reply_content).replace("\n", "\n> ")
                    e.description += f"\nReplies to {m.author.mention}\n> [{reply_content}]({m.jump_url})"

                if msg.embeds:
                    e.set_footer(text="⇓ Embed ⇓")
                files = []
                log_type = "message_delete"
                if msg.attachments:
                    log_type = "attachment_delete"
                    for attachment in msg.attachments:
                        if attachment.size > msg.guild.filesize_limit:
                            continue
                        with suppress(Exception):
                            files.append(await attachment.to_file(use_cached=True))

                channel_url = getattr(msg.channel, "jump_url", None)
                links = [("Open channel", channel_url, "↗️")] if channel_url else []
                self.add_to_queue(
                    guild_id,
                    log_type,
                    embeds=[e, *msg.embeds],
                    files=files,
                    actor=audit.user,
                    target=msg.author,
                    target_label="Message Author",
                    channel=msg.channel,
                    reason=audit.reason,
                    details=self.message_log_details(msg),
                    links=links,
                )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        guild_id = str(payload.guild_id)
        if guild_id in self.config and not payload.cached_message:
            if payload.channel_id in self.config[guild_id]["ignored_channels"]:
                return
            guild = self.bot.get_guild(payload.guild_id)
            c = self.bot.get_channel(payload.channel_id)
            audit = await AuditLogSearch(
                guild,
                Action.message_delete,
                channel=c,
            )
            channel_url = getattr(c, "jump_url", None)
            snapshot = None
            if self.archive_enabled(guild_id):
                snapshot = await self.local_archive.get_message(
                    guild_id, payload.message_id, include_files=True
                )

            e = Embed(color=purple)
            if snapshot:
                author_name = snapshot.get("author_name") or "Unknown member"
                author_mention = snapshot.get("author_mention") or author_name
                e.set_author(
                    name="Message Deleted - Recovered from Local History",
                    icon_url=snapshot.get("author_avatar") or Images.trash,
                )
                if snapshot.get("author_avatar"):
                    e.set_thumbnail(url=snapshot["author_avatar"])
                desc = f"{author_mention}'s message was deleted\n"
                if audit.user:
                    desc += f"By {audit.user_mention}\n"
                desc += (
                    f"ID: `{payload.message_id}`\n"
                    f"In {c.mention if c else '#unknown'}\n"
                    "Recovery: Content recovered from local history"
                )
                content = snapshot.get("content", "")
                if content:
                    quoted = "> " + utils.escape_markdown(content[:2500]).replace(
                        "\n", "\n> "
                    )
                    desc += f"\n\n{quoted}"
                e.description = chain(desc)
                archived_embeds = []
                for embed_data in snapshot.get("embeds", [])[:9]:
                    with suppress(TypeError, ValueError, KeyError):
                        archived_embeds.append(Embed.from_dict(embed_data))
                files = []
                upload_limit = getattr(guild, "filesize_limit", MAX_ATTACHMENT_BYTES)
                for stored_file in snapshot.get("stored_files", [])[:10]:
                    if len(stored_file["data"]) > upload_limit:
                        continue
                    files.append(
                        File(
                            BytesIO(stored_file["data"]),
                            filename=stored_file["filename"],
                        )
                    )
                self.add_to_queue(
                    guild_id,
                    "message_delete",
                    embeds=[e, *archived_embeds],
                    files=files,
                    actor=audit.user,
                    target=f"{author_name} (`{snapshot.get('author_id', 'unknown')}`)",
                    target_label="Message Author",
                    channel=c,
                    reason=audit.reason,
                    details={
                        "Sent": datetime.fromtimestamp(
                            snapshot["created_at"], timezone.utc
                        ),
                        "Attachments": len(snapshot.get("attachments", [])),
                        "Recovered files": len(files),
                        "Attachment details": [
                            (
                                f"{attachment.get('filename', 'attachment')} • "
                                f"{int(attachment.get('size') or 0):,} bytes • "
                                f"{attachment.get('content_type') or 'unknown type'} • "
                                f"{'saved locally' if attachment.get('stored') else 'metadata only'}"
                            )
                            for attachment in snapshot.get("attachments", [])[:10]
                        ],
                        "Buttons": format_message_buttons(
                            snapshot.get("components", [])
                        ),
                    },
                    links=(
                        [("Open channel", channel_url, "↗️")]
                        if channel_url
                        else []
                    ),
                )
                if not self.archive_config(guild_id).get("store_attachments"):
                    self.local_archive.forget_messages(
                        guild_id, [payload.message_id]
                    )
                return

            e.set_author(name="Unknown Message Deleted", icon_url=audit.target_avatar)
            if audit.target:
                e.set_thumbnail(url=audit.target_display_avatar)
            desc = ""
            if audit.target:
                desc = f"{audit.target_mention}'s message deleted\n"
            if audit.user:
                desc += f"By {audit.user_mention}\n"
            desc += (
                f"ID: `{payload.message_id}`\n"
                f"In {c.mention if c else '#unknown'}\n"
                "Recovery: No cached or local copy was available"
            )
            e.description = chain(desc)
            self.add_to_queue(
                guild_id,
                "message_delete",
                embed=e,
                actor=audit.user,
                target=audit.target,
                target_label="Probable Author",
                channel=c,
                reason=audit.reason,
                links=[("Open channel", channel_url, "↗️")] if channel_url else [],
            )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        guild_id = str(payload.guild_id)
        if guild_id in self.config and payload.channel_id not in self.config[guild_id]["ignored_channels"]:
            guild = self.bot.get_guild(payload.guild_id)
            channel = self.bot.get_channel(payload.channel_id)
            cached_ids = {str(msg.id) for msg in payload.cached_messages}
            missing_ids = [
                message_id
                for message_id in payload.message_ids
                if str(message_id) not in cached_ids
            ]
            local_messages = {}
            if self.archive_enabled(guild_id):
                local_messages = await self.local_archive.get_messages(
                    guild_id, missing_ids
                )
            recovered_rows = []
            author_names = set()
            for msg in payload.cached_messages:
                await asyncio.sleep(0)
                if self.config[guild_id]["secure"]:
                    protected_channel = (
                        msg.channel.id == self.config[guild_id]["channel"]
                        or msg.channel.id
                        in self.config[guild_id]["channels"].values()
                    )
                    if protected_channel:
                        await self.restore_secure_log_message(msg)
                        continue

                recovered_rows.append(
                    (msg.created_at.timestamp(), str(msg.author), msg.content)
                )
                author_names.add(str(msg.author))

            for snapshot in local_messages.values():
                recovered_rows.append(
                    (
                        float(snapshot.get("created_at") or 0),
                        snapshot.get("author_name") or "Unknown member",
                        snapshot.get("content") or "",
                    )
                )
                author_names.add(snapshot.get("author_name") or "Unknown member")

            purged_messages = "\n".join(
                f"{datetime.fromtimestamp(created_at, timezone.utc):%I:%M:%S%p} | "
                f"{author}: {content}"
                for created_at, author, content in sorted(
                    recovered_rows, key=lambda item: item[0]
                )
            )

            if payload.cached_messages and not recovered_rows:  # only logs were purged
                return
            buffer = (
                File(BytesIO(purged_messages.encode()), filename="messages.txt")
                if purged_messages
                else None
            )

            e = Embed(color=lime_green)
            audit = await AuditLogSearch(
                guild,
                Action.message_bulk_delete,
                target=channel,
            )
            purge_count = getattr(audit.extra, "count", None)
            if purge_count is not None:
                e.set_author(
                    name=f"{purge_count} Messages Purged",
                    icon_url=audit.user_avatar,
                )
                e.set_thumbnail(url=audit.user_display_avatar)
            else:
                amount = len(payload.message_ids)
                e.set_author(name=f"{amount} Messages Purged")
            user = audit.user_mention if audit.user else "Unknown-User"
            users = [f"`{name}`" for name in sorted(author_names)]
            channel_mention = getattr(channel, "mention", f"<#{payload.channel_id}>")
            message_count = (
                purge_count
                if purge_count is not None
                else len(payload.message_ids)
            )
            message_label = "message" if message_count == 1 else "messages"
            e.description = (
                f"By {user} in {channel_mention}\n"
                f"{emojis.creply} `{message_count}` {message_label} "
                f"(`{len(payload.cached_messages)}` cached)\n"
                f"{emojis.reply} `{len(users)}` users affected"
            )
            if users and len(users) <= 16:
                e.description += f"\n{emojis.empty}{emojis.reply} {', '.join(users)}"
            self.add_to_queue(
                guild_id,
                "message_purge",
                embed=e,
                file=buffer,
                actor=audit.user,
                channel=channel,
                reason=audit.reason,
                details={
                    "👥 Recovered Authors": len(users),
                    "💾 Local History": len(local_messages),
                },
                links=(
                    [("Open channel", channel.jump_url, "↗️")]
                    if channel and hasattr(channel, "jump_url")
                    else []
                ),
            )
            if (
                self.archive_enabled(guild_id)
                and not self.archive_config(guild_id).get("store_attachments")
            ):
                self.local_archive.forget_messages(
                    guild_id, payload.message_ids
                )

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload):
        guild_id = str(payload.guild_id)
        if guild_id in self.config:
            channel = self.bot.get_channel(payload.channel_id)
            if channel is None:
                return
            try:
                msg = await self.fetch_message_for_log(channel, payload.message_id)
            except (NotFound, Forbidden):
                return
            if msg is None:
                return
            if msg.author.id in self.config[guild_id]["ignored_bots"]:
                return
            if msg.channel.id in self.config[guild_id]["ignored_channels"]:
                return
            e = Embed(color=yellow)
            e.set_author(name="Reactions Cleared", icon_url=msg.author.display_avatar.url)
            e.description = f"On {msg.author.mention}'s [message]({msg.jump_url})"
            self.add_to_queue(
                guild_id,
                "reactions_clear",
                embed=e,
                target=msg.author,
                target_label="Message Author",
                channel=msg.channel,
                details=self.message_log_details(msg),
                links=[("Open message", msg.jump_url, "↗️")],
            )

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload):
        guild_id = str(payload.guild_id)
        if guild_id not in self.config:
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None or payload.channel_id in self.config[guild_id]["ignored_channels"]:
            return
        try:
            msg = await self.fetch_message_for_log(channel, payload.message_id)
        except (NotFound, Forbidden, HTTPException):
            return
        if msg is None:
            return
        if msg.author.id in self.config[guild_id]["ignored_bots"]:
            return

        e = Embed(color=yellow)
        e.set_author(name="Reaction Removed Everywhere", icon_url=msg.author.display_avatar.url)
        e.set_thumbnail(url=msg.author.display_avatar.url)
        e.description = chain(
            f"All {payload.emoji} reactions were removed\n"
            f"From {msg.author.mention}'s message\n"
            f"In {channel.mention}\n"
            f"**[Jump URL]({msg.jump_url})**"
        )
        self.add_to_queue(
            guild_id,
            "reactions_clear",
            embed=e,
            target=msg.author,
            target_label="Message Author",
            channel=channel,
            details=self.message_log_details(msg),
            links=[("Open message", msg.jump_url, "↗️")],
        )

    @commands.Cog.listener()
    async def on_guild_update(self, before: Guild, after: Guild):  # due for rewrite
        guild_id = str(after.id)
        if guild_id in self.config:
            audit = await AuditLogSearch(after, Action.guild_update, target=after)

            def make_embed(name: str) -> Embed:
                """ Creates a new embed to work with """
                e = Embed(color=lime_green)
                icon = after.icon.url if after.icon else None
                e.set_author(name=name, icon_url=audit.user_avatar or icon)
                if after.icon:
                    e.set_thumbnail(url=audit.user_display_avatar or icon)
                return e

            if before.name != after.name:
                e = make_embed("Server Renamed")
                e.description = f"To `{after.name}`" \
                                f"\nFrom `{before.name}`" \
                                f"\nBy {audit.user_mention}"
                self.add_to_queue(
                    guild_id,
                    "server_rename",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.name,
                        "➡️ After": after.name,
                    },
                )

            before_url = url_from(before.icon)
            after_url = url_from(after.icon)
            if before_url != after_url:
                fn = "before.png"
                if before_url and ".gif" in before_url:
                    fn = "before.gif"
                file = None
                if before.icon:
                    with suppress(NotFound, Forbidden, HTTPException, ClientOSError):
                        file = await before.icon.to_file(filename=fn)

                e = Embed(color=lime_green)
                e.description = f"{audit.user_mention} set the icon"
                if after.icon:
                    e.set_author(name="Icon Changed", icon_url=audit.user_display_avatar)
                    if file:
                        e.set_thumbnail(url="attachment://" + fn)
                    e.set_image(url=after.icon.url)
                else:
                    e.set_author(name="Icon Removed", icon_url=audit.user_avatar)
                    e.set_thumbnail(url=audit.user_display_avatar)
                    if file:
                        e.set_image(url="attachment://" + fn)

                self.add_to_queue(
                    guild_id,
                    "new_server_icon",
                    embed=e,
                    file=file,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details={"🖼️ New State": "Changed" if after.icon else "Removed"},
                )

            before_url = url_from(before.banner)
            after_url = url_from(after.banner)
            if before_url != after_url:
                e = Embed(color=lime_green)
                e.set_author(name="Banner Changed", icon_url=audit.user_display_avatar)
                fn = "before.png"
                if before_url and ".gif" in before_url:
                    fn = "before.gif"
                file = None
                if before_url:
                    with suppress(NotFound, Forbidden, HTTPException, ClientOSError):
                        file = await before.banner.to_file(filename=fn)
                if file:
                    e.set_thumbnail(url="attachment://" + fn)
                e.description = (
                    f"{audit.user_mention} "
                    f"{'set' if after_url else 'removed'} the server banner"
                )
                if after_url:
                    e.set_image(url=after_url)
                elif file:
                    e.set_image(url="attachment://" + fn)
                self.add_to_queue(
                    guild_id,
                    "new_server_banner",
                    embed=e,
                    file=file,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details={"🖼️ New State": "Changed" if after.banner else "Removed"},
                )

            before_url = url_from(before.splash)
            after_url = url_from(after.splash)
            if before_url != after_url:
                e = Embed(color=lime_green)
                e.set_author(name="Splash Changed", icon_url=audit.user_display_avatar)
                fn = "before.png"
                if before_url and ".gif" in before_url:
                    fn = "before.gif"
                file = None
                if before_url:
                    with suppress(NotFound, Forbidden, HTTPException, ClientOSError):
                        file = await before.splash.to_file(filename=fn)
                if file:
                    e.set_thumbnail(url="attachment://" + fn)
                e.description = (
                    f"{audit.user_mention} "
                    f"{'set' if after_url else 'removed'} the server splash"
                )
                if after_url:
                    e.set_image(url=after_url)
                elif file:
                    e.set_image(url="attachment://" + fn)
                self.add_to_queue(
                    guild_id,
                    "new_server_splash",
                    embed=e,
                    file=file,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details={"🖼️ New State": "Changed" if after.splash else "Removed"},
                )

            # if before.region != after.region:
            #     e = create_template_embed()
            #     e.description = (
            #         f"> 》__**Region Changed**__《" f"\n**Changed by:** {dat['user']}"
            #     )
            #     e.add_field(name="Before", value=str(before.region), inline=False)
            #     e.add_field(name="After", value=str(after.region), inline=False)
            #     log = Log("region_change", embed=e)
            #     self.add_to_queue(guild_id, log)

            if before.afk_timeout != after.afk_timeout:
                e = make_embed("AFK Timeout Changed")
                if after.afk_timeout:
                    e.description = f"{audit.user} set it to {format_date(seconds=after.afk_timeout)}"
                    if before.afk_timeout:
                        e.description += f"\nFrom {format_date(seconds=before.afk_timeout)}"
                        e.description = chain(e.description)
                else:
                    e.description = f"{audit.user} disabled afk timeout"
                self.add_to_queue(
                    guild_id,
                    "afk_timeout_change",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": format_date(seconds=before.afk_timeout),
                        "➡️ After": format_date(seconds=after.afk_timeout),
                    },
                )

            if before.afk_channel != after.afk_channel:
                e = make_embed("AFK Channel Changed")
                if after.afk_channel:
                    desc = f"{audit.user_mention} set the afk channel\n" \
                           f"To {after.afk_channel.mention}"
                    if before.afk_channel:
                        desc += f"\nFrom {before.afk_channel.mention}"
                else:
                    desc = f"{audit.user_mention} removed the afk channel\n" \
                           f"Was {before.afk_channel.mention}"
                e.description = chain(desc)
                self.add_to_queue(
                    guild_id,
                    "afk_channel_change",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.afk_channel,
                        "➡️ After": after.afk_channel,
                    },
                    links=(
                        [("Open channel", after.afk_channel.jump_url, "↗️")]
                        if after.afk_channel else []
                    ),
                )

            if before.owner != after.owner:
                e = make_embed("Owner Changed")
                e.description = chain(
                    "Server ownership was transferred\n"
                    f"To {after.owner.mention}\n"
                    f"From {before.owner.mention}"
                )
                self.add_to_queue(
                    guild_id,
                    "owner_change",
                    embed=e,
                    actor=audit.user,
                    target=after.owner,
                    target_label="New Owner",
                    reason=audit.reason,
                    details={"⬅️ Previous Owner": before.owner},
                )

            if before.features != after.features and before.premium_tier == after.premium_tier:
                e = make_embed("Features Changed")
                e.description = "Features were added, or removed"
                changes = ""
                for feature in before.features:
                    if feature not in after.features:
                        changes += f"\n❌ {feature}"
                for feature in after.features:
                    if feature not in before.features:
                        changes += f"\n<:plus:548465119462424595> {feature}"
                if changes:
                    e.description += f"\n\n{changes}"
                    added = sorted(set(after.features) - set(before.features))
                    removed = sorted(set(before.features) - set(after.features))
                    self.add_to_queue(
                        guild_id,
                        "features_change",
                        embed=e,
                        actor=audit.user,
                        target=after,
                        target_label="Server",
                        reason=audit.reason,
                        details={
                            "➕ Added": added or ["None"],
                            "➖ Removed": removed or ["None"],
                        },
                    )

            if before.premium_tier != after.premium_tier:
                e = make_embed("Boost Tier Changed")
                action = "Lowered" if before.premium_tier > after.premium_tier else "Raised"
                e.description = chain(
                    f"{action} to **level {after.premium_tier}**\n"
                    f"Boosts: `{after.premium_subscription_count}`"
                )
                before_filesize = round(before.filesize_limit / 1024 / 1024)
                after_filesize = round(after.filesize_limit / 1024 / 1024)
                before_bitrate = str(before.bitrate_limit)[:3].rstrip("0")
                after_bitrate = str(after.bitrate_limit)[:3].rstrip("0")
                e.description += "\n\n" + chain(
                    f"**Changes:**\n"
                    f"Emoji Limit: `{before.emoji_limit}` -> `{after.emoji_limit}`\n"
                    f"Sticker Limit: `{before.sticker_limit}` -> `{after.sticker_limit}`\n"
                    f"Filesize Limit: `{before_filesize}mb` -> `{after_filesize}mb`\n"
                    f"Bitrate: `{before_bitrate}kbps` -> `{after_bitrate}kbps`"
                )
                changes = "\n"
                for feature in before.features:
                    if feature not in after.features:
                        changes += f"\n{emojis.off} {feature.replace('_', ' ').lower().title()}"
                for feature in after.features:
                    if feature not in before.features:
                        changes += f"\n{emojis.on} {feature.replace('_', ' ').lower().title()}"
                e.description += changes
                self.add_to_queue(
                    guild_id,
                    "new_boost_tier",
                    embed=e,
                    target=after,
                    target_label="Server",
                    details={
                        "⬅️ Previous Tier": before.premium_tier,
                        "➡️ Current Tier": after.premium_tier,
                        "🚀 Boosts": after.premium_subscription_count or 0,
                    },
                )

            if before.premium_subscription_count != after.premium_subscription_count and before.premium_tier == after.premium_tier:
                e = make_embed("Boosts Changed")
                if after.premium_subscription_count > before.premium_subscription_count:
                    action = "boosted"
                else:
                    action = "unboosted"
                who = "Unknown member"
                booster = None
                if before.premium_subscribers != after.premium_subscribers:
                    changed = [
                        m
                        for m in before.premium_subscribers
                        if m not in after.premium_subscribers
                    ]
                    if not changed:
                        changed = [
                            m
                            for m in after.premium_subscribers
                            if m not in before.premium_subscribers
                        ]
                    if changed:
                        booster = changed[0]
                        who = booster.mention
                before_boosts = before.premium_subscription_count or 0
                after_boosts = after.premium_subscription_count or 0
                e.description = chain(
                    f"{who} {action}\n"
                    f"Total boosts: `{before_boosts}` → `{after_boosts}`"
                )
                self.add_to_queue(
                    guild_id,
                    "boost",
                    embed=e,
                    target=booster or after,
                    target_label="Member" if booster else "Server",
                )

            if before.system_channel != after.system_channel:
                e = make_embed("System Channel Changed")
                if after.system_channel:
                    e.description = f"{audit.user_mention} set it to {after.system_channel.mention}"
                    if before.system_channel:
                        e.description += f"{emojis.reply} From {before.system_channel.mention}"
                else:
                    e.description = chain(
                        f"{audit.user_mention} removed the system channel\n"
                        f"Was {before.system_channel.mention}"
                    )
                self.add_to_queue(
                    guild_id,
                    "system_channel",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.system_channel,
                        "➡️ After": after.system_channel,
                    },
                    links=(
                        [("Open channel", after.system_channel.jump_url, "↗️")]
                        if after.system_channel else []
                    ),
                )

            if before.system_channel_flags != after.system_channel_flags:
                flags = ""
                for i, (flag, setting) in enumerate(list(after.system_channel_flags)):
                    if setting != list(before.system_channel_flags)[i][1]:
                        flags += f"\n{emojis.on if setting else emojis.off} {flag.replace('_', ' ').title()}"
                e = make_embed("System Channel Flags")
                if emojis.on not in flags:
                    action = "disabled"
                elif emojis.off not in flags:
                    action = "enabled"
                else:
                    action = "updated"
                e.description = f"{audit.user_mention} {action} flags\n{flags}"
                self.add_to_queue(
                    guild_id,
                    "system_channel_flags",
                    embed=e,
                    actor=audit.user,
                    target=after.system_channel or after,
                    target_label="System Channel" if after.system_channel else "Server",
                    reason=audit.reason,
                    details={"⚙️ Changed Flags": flags.strip() or "Unknown"},
                )

            if before.mfa_level != after.mfa_level:
                action = "enabled" if after.mfa_level.name == "require_2fa" else "disabled"
                e = make_embed("2FA Requirement Changed")
                e.description = f"{audit.user_mention} {action} 2-factor-authentication for moderation"
                self.add_to_queue(
                    guild_id,
                    "2fa_update",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.mfa_level,
                        "➡️ After": after.mfa_level,
                    },
                )

            if before.verification_level != after.verification_level:
                match str(after.verification_level):
                    case "none":
                        description = "No criteria set"
                    case "low":
                        description = "Must have a verified email on their Discord account"
                    case "medium":
                        description = "Must have a verified email and be registered on Discord for more than five minutes"
                    case "high":
                        description = "Must have a verified email, be registered on Discord for more than five minutes, and be a member of the guild itself for more than ten minutes"
                    case _highest:
                        description = "Must have a verified phone on their Discord account"
                e = make_embed("Verification Level Changed")
                e.description = f"Set to `{after.verification_level}` by {audit.user_mention}\n\n" \
                                f"***{description}***"
                self.add_to_queue(
                    guild_id,
                    "verification_level",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.verification_level,
                        "➡️ After": after.verification_level,
                    },
                )

            if before.explicit_content_filter != after.explicit_content_filter:
                action = "Disabled"
                if after.explicit_content_filter is ContentFilter.no_role:
                    action = "Enabled for members without a role"
                elif after.explicit_content_filter is ContentFilter.all_members:
                    action = "Enabled for everyone"
                e = make_embed("Explicit Filter Updated")
                e.description = chain(f"{action}\nBy {audit.user_mention}")
                self.add_to_queue(
                    guild_id,
                    "explicit_filter",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.explicit_content_filter,
                        "➡️ After": after.explicit_content_filter,
                    },
                )

            if before.default_notifications != after.default_notifications:
                mentions_for = "all messages"
                if after.default_notifications is NotificationLevel.only_mentions:
                    mentions_for = "only mentions"
                e = make_embed("Default Notifications")
                e.description = chain(f"Set to notify for {mentions_for}\nBy {audit.user_mention}")
                self.add_to_queue(
                    guild_id,
                    "default_notifications",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.default_notifications,
                        "➡️ After": after.default_notifications,
                    },
                )

            modern_settings = {}
            for attribute, label in (
                ("description", "📝 Description"),
                ("preferred_locale", "🌐 Preferred Locale"),
                ("nsfw_level", "🔞 Age Restriction Level"),
                ("rules_channel", "📜 Rules Channel"),
                ("public_updates_channel", "📣 Public Updates Channel"),
                ("safety_alerts_channel", "🛡️ Safety Alerts Channel"),
                ("widget_enabled", "🧩 Server Widget"),
                ("widget_channel", "📍 Widget Channel"),
                ("premium_progress_bar_enabled", "🚀 Boost Progress Bar"),
                ("vanity_url_code", "🔗 Vanity URL"),
                ("invites_paused_until", "⏸️ Invites Paused Until"),
                ("dms_paused_until", "⏸️ DMs Paused Until"),
                ("discovery_splash", "🖼️ Discovery Splash"),
            ):
                before_value = getattr(before, attribute, None)
                after_value = getattr(after, attribute, None)
                if before_value != after_value:
                    old = before_value if before_value is not None else "Not set"
                    new = after_value if after_value is not None else "Not set"
                    modern_settings[label] = f"{old} → {new}"
            if modern_settings:
                e = make_embed("Server Settings Updated")
                e.description = f"{audit.user_mention} updated **{after.name}**"
                if before.discovery_splash != after.discovery_splash and after.discovery_splash:
                    e.set_image(url=after.discovery_splash.url)
                self.add_to_queue(
                    guild_id,
                    "server_settings",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Server",
                    reason=audit.reason,
                    details=modern_settings,
                    created_at=(audit.created_at.timestamp() if audit.created_at else None),
                )

            # Union[emoji_limit, bitrate_limit, filesize_limit]

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: TextChannel):
        guild_id = str(channel.guild.id)
        if guild_id in self.config:
            audit = await AuditLogSearch(
                channel.guild, Action.channel_create, target=channel
            )
            if (
                audit.user
                and audit.user.bot
                and audit.user.id in self.config[guild_id]["ignored_bots"]
            ):
                return
            e = Embed(color=yellow)
            e.set_author(name="Channel Created", icon_url=audit.user_avatar)
            e.set_thumbnail(url=audit.user_display_avatar)
            if isinstance(channel, TextChannel):
                name = emojis.text_channel + channel.name
            elif isinstance(channel, VoiceChannel):
                name = emojis.voice_channel + channel.name
            else:
                name = f"{channel.name} (`{channel.type.name}`)"
            e.description = f"{audit.user_mention} created **{name}**\n" \
                            f"{emojis.reply} With `{len(getattr(channel, 'members', []))}` members"

            self.add_to_queue(
                guild_id,
                "channel_create",
                embed=e,
                actor=audit.user,
                target=channel,
                target_label="Channel",
                reason=audit.reason,
                details=self.channel_log_details(channel),
                links=[("Open channel", channel.jump_url, "↗️")],
            )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        guild_id = str(channel.guild.id)
        if guild_id in self.config:
            # anti log channel deletion
            if self.config[guild_id]["secure"]:
                if channel.id == self.config[guild_id]["channel"]:
                    all_embeds = []
                    for embeds, _timestamp in self.recent_logs[guild_id]:
                        await asyncio.sleep(0)
                        all_embeds.extend(embeds)
                    # Resend in chunks of 9
                    for i in range(0, len(all_embeds), 9):
                        batch = all_embeds[i:i + 9]
                        self.add_to_queue(
                            guild_id,
                            "message_delete",
                            embeds=batch,
                            target=channel,
                            target_label="Deleted Log Channel",
                            details={
                                "♻️ Recovery": "Replaying protected recent log cards",
                                "📚 Entries": len(batch),
                            },
                        )
                    return

            audit = await AuditLogSearch(
                channel.guild, Action.channel_delete, target=channel
            )
            if (
                audit.user
                and audit.user.bot
                and audit.user.id in self.config[guild_id]["ignored_bots"]
            ):
                return
            e = Embed(color=red)
            e.set_author(name="Channel Deleted", icon_url=audit.user_avatar)
            e.set_thumbnail(url=audit.user_display_avatar)

            if isinstance(channel, TextChannel):
                name = emojis.text_channel + channel.name
            elif isinstance(channel, VoiceChannel):
                name = emojis.voice_channel + channel.name
            else:
                name = f"{channel.name} (`{channel.type.name}`)"
            e.description = f"{audit.user_mention} deleted **{name}**"

            files = []
            if members := getattr(channel, "members", None):
                e.description += f"\n{emojis.creply} With `{len(members)}` members"
                member_file = await self.member_export_file(
                    channel.guild,
                    f"{channel.name} - Member List",
                    members,
                )
                files = [member_file] if member_file else []

            if getattr(channel, "category", None):
                e.description += f"\n{emojis.creply if members else emojis.reply} In `{channel.category}`"

            self.add_to_queue(
                guild_id,
                "channel_delete",
                embed=e,
                files=files,
                actor=audit.user,
                target=channel,
                target_label="Deleted Channel",
                reason=audit.reason,
                details={
                    **self.channel_log_details(channel),
                    "👥 Members": len(getattr(channel, "members", [])),
                },
            )

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):  # due for rewrite
        guild_id = str(after.guild.id)
        if guild_id in self.config:
            audit = await AuditLogSearch(
                after.guild,
                Action.channel_update,
                Action.overwrite_create,
                Action.overwrite_delete,
                Action.overwrite_update,
                target=after,
            )

            if (
                audit.user
                and audit.user.bot
                and audit.user.id in self.config[guild_id]["ignored_bots"]
            ):
                return
            if before.name != after.name:
                if before.id in self.config[guild_id]["ignored_channels"]:
                    return
                icon = None
                if isinstance(before, TextChannel):
                    icon = Images.text_channel
                elif isinstance(before, VoiceChannel):
                    icon = Images.voice_channel
                e = Embed(color=orange)
                e.set_author(name="Channel Renamed", icon_url=icon)
                e.set_thumbnail(url=audit.user_avatar)
                e.description = f"#{before.name} was renamed to #{after.name} by {audit.user_mention}"
                self.add_to_queue(
                    guild_id,
                    "channel_rename",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Channel",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.name,
                        "➡️ After": after.name,
                    },
                    links=[("Open channel", after.jump_url, "↗️")],
                )

            channel_moved = (
                before.position != after.position
                or getattr(before, "category_id", None) != getattr(after, "category_id", None)
            )
            audit_target_id = getattr(audit.target, "id", None)
            is_audit_target = audit_target_id is None or audit_target_id == after.id
            if (
                channel_moved
                and is_audit_target
                and not self.channel_moved_cd.check(after.guild.id)
            ):
                e = Embed(color=orange)
                e.set_author(name="Channel Moved", icon_url=audit.user_avatar)
                e.set_thumbnail(url=audit.user_display_avatar)
                description = f"{after.mention} was moved"
                if before.category != after.category:
                    description += f"\nFrom `{before.category}`\nTo `{after.category}`"
                elif len(after.guild.channels) > 1:
                    try:
                        pos = after.guild.channels.index(after)
                    except ValueError:
                        pos = None
                    if pos is not None and pos > 0:
                        description += f"\nTo below {after.guild.channels[pos - 1].mention}"
                    elif pos == 0:
                        description += f"\nTo above {after.guild.channels[pos + 1].mention}"
                e.description = chain(description)
                self.add_to_queue(
                    guild_id,
                    "channel_move",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Channel",
                    reason=audit.reason,
                    details={
                        "⬅️ Previous Category": before.category,
                        "➡️ Current Category": after.category,
                        "↕️ Position": f"{before.position} → {after.position}",
                    },
                    links=[("Open channel", after.jump_url, "↗️")],
                )

            if isinstance(before, TextChannel):
                if before.topic != after.topic:
                    if before.id in self.config[guild_id]["ignored_channels"]:
                        return
                    e = Embed(color=orange)
                    e.set_author(name="Topic Updated", icon_url=audit.user_avatar)
                    e.set_thumbnail(url=audit.user_display_avatar)
                    e.description = f"{audit.user_mention} edited {after.mention}"
                    if before.topic:
                        for text_group in split(before.topic):
                            e.add_field(
                                name="Before", value=text_group, inline=False
                            )
                    if after.topic:
                        for text_group in split(after.topic):
                            e.add_field(
                                name="After", value=text_group, inline=False
                            )
                    self.add_to_queue(
                        guild_id,
                        "channel_topic",
                        embed=e,
                        actor=audit.user,
                        target=after,
                        target_label="Channel",
                        reason=audit.reason,
                        links=[("Open channel", after.jump_url, "↗️")],
                    )

            if before.overwrites != after.overwrites:
                e = Embed(color=orange)
                e.set_author(name="Overwrites Updated", icon_url=audit.user_avatar)
                e.set_thumbnail(url=audit.user_display_avatar)
                for obj, permissions in before.overwrites.items():
                    await asyncio.sleep(0)
                    target_name = overwrite_target_name(after.guild, obj)
                    if obj not in after.overwrites:
                        perms = [
                             f"{emojis.on if value else emojis.off} {perm}"
                             for perm, value in list(permissions)
                             if value is not None
                        ]
                        e.add_field(
                            name=f"❌ {target_name} removed",
                            value="\n".join(perms) if perms else "`had no permissions`"[:1024],
                            inline=False,
                        )
                        continue

                    after_values = list(after.overwrites[obj])
                    if list(permissions) != after_values:
                        updated_perms = [
                            f"{emojis.on if after_values[i][1] else emojis.off} {perm}"
                              for i, (perm, value) in enumerate(list(permissions))
                                if (value != after_values[i][1])
                        ]
                        e.add_field(
                            name=f"<:edited:550291696861315093> {target_name}",
                            value="\n".join(updated_perms)[:1024],
                            inline=False,
                        )

                for obj, permissions in after.overwrites.items():
                    await asyncio.sleep(0)
                    if obj not in before.overwrites:
                        target_name = overwrite_target_name(after.guild, obj)
                        perms = [
                            f"{emojis.on if value else emojis.off} {perm}"
                              for perm, value in list(permissions)
                                if value is not None
                        ]
                        e.add_field(
                            name=f"<:plus:548465119462424595> {target_name}",
                            value="\n".join(perms) if perms else "`has no permissions`"[:1024],
                            inline=False,
                        )

                e.description = f"{audit.user_mention} edited {after.mention}"
                self.add_to_queue(
                    guild_id,
                    "channel_overwrites",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Channel",
                    reason=audit.reason,
                    details={
                        "🔐 Before": len(before.overwrites),
                        "🔐 After": len(after.overwrites),
                        "🔗 Synced": getattr(after, "permissions_synced", None),
                    },
                    links=[("Open channel", after.jump_url, "↗️")],
                )

            if isinstance(before, ForumChannel):
                if before.available_tags != after.available_tags:
                    for tag in before.available_tags:
                        if tag not in after.available_tags:
                            e = Embed(color=orange)
                            e.set_author(name="Forum Tag Deleted", icon_url=audit.user_avatar)
                            e.set_thumbnail(url=audit.user_display_avatar)
                            name = tag.name
                            if tag.emoji:
                                name = f"{tag.emoji} {tag.name}"
                            e.description = chain(
                                f"**{name}** was deleted\n"
                                f"In {before.mention}\n"
                                f"By {audit.user_mention}\n"
                                f"**[Jump URL]({after.jump_url})**"
                            )
                            self.add_to_queue(
                                guild_id,
                                "tag_delete",
                                embed=e,
                                actor=audit.user,
                                target=after,
                                target_label="Forum / Media Channel",
                                reason=audit.reason,
                                details={
                                    "🏷️ Tag": name,
                                },
                                links=[("Open channel", after.jump_url, "↗️")],
                            )
                    for tag in after.available_tags:
                        if tag not in before.available_tags:
                            e = Embed(color=orange)
                            e.set_author(name="Forum Tag Created", icon_url=audit.user_avatar)
                            e.set_thumbnail(url=audit.user_display_avatar)
                            name = tag.name
                            if tag.emoji:
                                name = f"{tag.emoji} {tag.name}"
                            e.description = chain(
                                f"**{name}** was created\n"
                                f"In {before.mention}\n"
                                f"By {audit.user_mention}\n"
                                f"**[Jump URL]({after.jump_url})**"
                            )
                            self.add_to_queue(
                                guild_id,
                                "tag_create",
                                embed=e,
                                actor=audit.user,
                                target=after,
                                target_label="Forum / Media Channel",
                                reason=audit.reason,
                                details={
                                    "🏷️ Tag": name,
                                    "🛡️ Moderated": tag.moderated,
                                },
                                links=[("Open channel", after.jump_url, "↗️")],
                            )
                    if len(before.available_tags) == len(after.available_tags):
                        for i, tag in enumerate(before.available_tags):
                            new_tag = list(after.available_tags)[i]
                            old_name = tag.name
                            if tag.emoji:
                                old_name = f"{tag.emoji} {tag.name}"
                            new_name = new_tag.name
                            if new_tag.emoji:
                                new_name = f"{new_tag.emoji} {new_tag.name}"
                            if old_name != new_name:
                                e = Embed(color=orange)
                                e.set_author(name="Forum Tag Updated", icon_url=audit.user_avatar)
                                e.set_thumbnail(url=audit.user_display_avatar)
                                e.description = chain(
                                    f"**{old_name}** was renamed\n"
                                    f"To **{new_name}**\n"
                                    f"In {before.mention}\n"
                                    f"By {audit.user_mention}\n"
                                    f"**[Jump URL]({after.jump_url})**"
                                )
                                self.add_to_queue(
                                    guild_id,
                                    "tag_update",
                                    embed=e,
                                    actor=audit.user,
                                    target=after,
                                    target_label="Forum / Media Channel",
                                    reason=audit.reason,
                                    details={
                                        "⬅️ Before": old_name,
                                        "➡️ After": new_name,
                                    },
                                    links=[("Open channel", after.jump_url, "↗️")],
                                )

            setting_labels = {
                "nsfw": "🔞 Age Restricted",
                "slowmode_delay": "⏱️ Slowmode",
                "default_auto_archive_duration": "🗄️ Default Auto Archive",
                "default_thread_slowmode_delay": "🧵 Default Thread Slowmode",
                "bitrate": "🎚️ Bitrate",
                "user_limit": "👥 User Limit",
                "rtc_region": "🌐 RTC Region",
                "video_quality_mode": "🎥 Video Quality",
                "default_layout": "📰 Default Layout",
                "default_sort_order": "↕️ Default Sort",
                "default_reaction_emoji": "😀 Default Reaction",
            }
            setting_changes = {}
            for attribute, label in setting_labels.items():
                before_value = getattr(before, attribute, None)
                after_value = getattr(after, attribute, None)
                if before_value != after_value:
                    old = before_value if before_value is not None else "Not set"
                    new = after_value if after_value is not None else "Not set"
                    setting_changes[label] = f"{old} → {new}"
            if setting_changes:
                e = Embed(color=orange)
                e.set_author(name="Channel Settings Updated", icon_url=audit.user_avatar)
                e.set_thumbnail(url=audit.user_display_avatar)
                e.description = f"{audit.user_mention} updated {after.mention}"
                self.add_to_queue(
                    guild_id,
                    "channel_settings",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Channel",
                    reason=audit.reason,
                    details=setting_changes,
                    links=[("Open channel", after.jump_url, "↗️")],
                )

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        guild_id = str(role.guild.id)
        if guild_id in self.config:
            audit = await AuditLogSearch(role.guild, Action.role_create, target=role)
            e = Embed(color=role.color)
            e.set_author(name="Role Created", icon_url=audit.user_avatar)
            e.set_thumbnail(url=audit.user_display_avatar)
            e.description = f"{audit.user_mention} made {role.mention}"
            self.add_to_queue(
                guild_id,
                "role_create",
                embed=e,
                actor=audit.user,
                target=role,
                target_label="Role",
                reason=audit.reason,
                details={
                    "🎨 Primary Color": role.color,
                    "🌈 Secondary Color": role.secondary_color,
                    "✨ Tertiary Color": role.tertiary_color,
                    "↕️ Position": role.position,
                    "📌 Displayed Separately": role.hoist,
                    "📣 Mentionable": role.mentionable,
                    "🖼️ Display Icon": role.display_icon,
                    "🤖 Managed": role.managed,
                    "🏷️ Tags": role.tags,
                    "🚩 Flags": role.flags,
                    "🔐 Enabled Permissions": sum(value for _, value in role.permissions),
                },
            )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        guild_id = str(role.guild.id)
        if guild_id in self.config:
            audit = await AuditLogSearch(role.guild, Action.role_delete, target=role)
            e = Embed(color=role.color)
            e.set_author(name="Role Deleted", icon_url=audit.user_avatar)
            e.set_thumbnail(url=audit.user_display_avatar)
            e.description = chain(
                f"**@{role.name}** was deleted\n"
                f"By {audit.user_mention}\n"
                f"Had `{len(role.members)}` members"
            )
            e.set_footer(text=f"Hex {role.color} | RGB {role.color.to_rgb()}")
            buffer = await self.member_export_file(
                role.guild,
                f"{role.name} - Member List",
                role.members,
            )
            self.add_to_queue(
                guild_id,
                "role_delete",
                embed=e,
                file=buffer,
                actor=audit.user,
                target=role,
                target_label="Deleted Role",
                reason=audit.reason,
                details={
                    "🎨 Primary Color": role.color,
                    "🌈 Secondary Color": role.secondary_color,
                    "✨ Tertiary Color": role.tertiary_color,
                    "↕️ Position": role.position,
                    "📌 Displayed Separately": role.hoist,
                    "📣 Mentionable": role.mentionable,
                    "🖼️ Display Icon": role.display_icon,
                    "🤖 Managed": role.managed,
                    "🏷️ Tags": role.tags,
                    "🚩 Flags": role.flags,
                    "👥 Members": len(role.members),
                },
            )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        guild_id = str(after.guild.id)
        if guild_id in self.config:
            audit = await AuditLogSearch(after.guild, Action.role_update, target=after)

            def make_embed(name: str):
                e = Embed(color=after.color)
                e.set_author(name=name, icon_url=audit.user_avatar)
                e.set_thumbnail(url=audit.user_display_avatar)
                return e

            if before.name != after.name:
                e = make_embed("Role Renamed")
                e.description = chain(
                    f"{audit.user_mention} renamed {after.mention}\n"
                    f"To `{after.name}`\n"
                    f"From `{before.name}`"
                )
                self.add_to_queue(
                    guild_id,
                    "role_rename",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Role",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.name,
                        "➡️ After": after.name,
                    },
                )

            if (
                before.color != after.color
                or before.secondary_color != after.secondary_color
                or before.tertiary_color != after.tertiary_color
            ):
                def generate_image():
                    card = Image.new("RGBA", (320, 64), color=(0, 0, 0, 0))

                    def paint_role_palette(role, y):
                        colors = [role.color.to_rgb()]
                        if role.secondary_color:
                            colors.append(role.secondary_color.to_rgb())
                        if role.tertiary_color:
                            colors.append(role.tertiary_color.to_rgb())
                        for x in range(card.width):
                            scaled = (x / max(1, card.width - 1)) * (len(colors) - 1)
                            left = min(int(scaled), len(colors) - 1)
                            right = min(left + 1, len(colors) - 1)
                            blend = scaled - left
                            rgb = tuple(
                                round(colors[left][index] * (1 - blend) + colors[right][index] * blend)
                                for index in range(3)
                            )
                            for row in range(y, y + 30):
                                card.putpixel((x, row), (*rgb, 255))

                    paint_role_palette(before, 0)
                    paint_role_palette(after, 34)
                    _buffer = BytesIO()
                    card.save(_buffer, format="png")
                    _buffer.seek(0)

                    return File(_buffer, filename="colors.png")

                buffer = await self.bot.loop.run_in_executor(None, generate_image)
                e = make_embed("Role Recolored")
                e.set_image(url="attachment://colors.png")
                e.description = chain(
                    f"{audit.user_mention} recolored {after.mention}\n"
                    f"From {before.color} to {after.color}\n"
                    f"From {before.color.to_rgb()} to {after.color.to_rgb()}"
                )
                self.add_to_queue(
                    guild_id,
                    "role_recolor",
                    embed=e,
                    file=buffer,
                    actor=audit.user,
                    target=after,
                    target_label="Role",
                    reason=audit.reason,
                    details={
                        "⬅️ Before Palette": (
                            f"{before.color} / {before.secondary_color or 'none'} / "
                            f"{before.tertiary_color or 'none'}"
                        ),
                        "➡️ After Palette": (
                            f"{after.color} / {after.secondary_color or 'none'} / "
                            f"{after.tertiary_color or 'none'}"
                        ),
                    },
                )

            if before.display_icon != after.display_icon:
                e = make_embed("Role Icon Updated")
                before_icon = before.display_icon or "None"
                after_icon = after.display_icon or "None"
                e.description = chain(
                    f"{audit.user_mention} updated {after.mention}'s display icon\n"
                    f"From {before_icon}\n"
                    f"To {after_icon}"
                )
                if hasattr(after_icon, "url"):
                    e.set_thumbnail(url=after_icon.url)
                self.add_to_queue(
                    guild_id,
                    "role_icon",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Role",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before_icon,
                        "➡️ After": after_icon,
                    },
                )

            if before.hoist != after.hoist:
                action = "show"
                if after.hoist is False:
                    action = "not show"
                e = make_embed("Role Visibility Changed")
                e.description = f"{audit.user_mention} set {after.mention} to {action} in the member list"
                self.add_to_queue(
                    guild_id,
                    "role_visibility",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Role",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.hoist,
                        "➡️ After": after.hoist,
                    },
                )

            if before.mentionable != after.mentionable:
                action = "to be mentionable"
                if not after.mentionable:
                    action = "to not be mentionable"
                e = make_embed("Role Mention-Ability Changed")
                e.description = f"{audit.user_mention} set {after.mention} to {action}"
                self.add_to_queue(
                    guild_id,
                    "role_mentionable",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Role",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.mentionable,
                        "➡️ After": after.mentionable,
                    },
                )

            if before.position != after.position and not self.role_moved_cd.check(after.guild.id):
                # old_roles = self.role_index[guild_id]
                # old_pos = old_roles.index(before)
                # if old_roles[old_pos+1] is after.guild.roles[after.position+1]:
                #     if old_roles[old_pos-1] is after.guild.roles[after.position-1]:
                #         return
                # self.role_index[guild_id] = [role for role in after.guild.roles]
                e = Embed(color=after.color)
                e.set_author(name="Role Moved", icon_url=audit.user_avatar)
                e.set_thumbnail(url=audit.user_display_avatar)
                action = "raised"
                if after.position < before.position:
                    action = "lowered"
                e.description = f"{before.mention} was {action}"
                self.add_to_queue(
                    guild_id,
                    "role_move",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Role",
                    reason=audit.reason,
                    details={
                        "⬅️ Before Position": before.position,
                        "➡️ After Position": after.position,
                    },
                )

                # before_roles = before.guild.roles
                # before_roles.pop(after.position)
                # before_roles.insert(before.position, before)
                #
                # before_above = before_roles[before.position+1].id
                # before_below = before_roles[before.position-1].id
                # after_above = after.guild.roles[after.position+1].id
                # after_below = after.guild.roles[after.position-1].id
                #
                # if before_above == after_above and before_below == after_below:
                #     print("Identical! EEEEEE")
                # else:
                #     self.queue[guild_id].append([em, 'updates', time()])

                #     e.add_field(
                #         name='Position Changed',
                #         value=f"**》Before** - {before.position}"
                #               f"\n{before_above}"
                #               f"\n{before.mention}"
                #               f"\n{before_below}"
                #               f"\n\n**》After** - {after.position}"
                #               f"\n{after_above}"
                #               f"\n{after.mention}"
                #               f"\n{after_below}",
                #         inline=False
                #     )
            if before.permissions != after.permissions:
                e = make_embed("Role Permissions Updated")
                changes = ""
                for i, (perm, value) in enumerate(iter(after.permissions)):
                    if value != list(before.permissions)[i][1]:
                        changes += f"\n{emojis.on if value else emojis.off} {perm}"
                e.description = f"{after.mention}'s perms updated\n" \
                                f"{emojis.reply} By {audit.user_mention}\n" \
                                f"{changes[:3800]}"
                changed_permissions = [
                    permission
                    for index, (permission, value) in enumerate(after.permissions)
                    if value != list(before.permissions)[index][1]
                ]
                self.add_to_queue(
                    guild_id,
                    "role_permissions",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Role",
                    reason=audit.reason,
                    details={"🔐 Changed Permissions": changed_permissions},
                )

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        guild_id = str(channel.guild.id)
        if guild_id in self.config:
            audit = await AuditLogSearch(
                channel.guild,
                Action.webhook_create,
                Action.webhook_delete,
                Action.webhook_update,
            )
            if audit.user and audit.user.bot and audit.user.id in self.config[guild_id]["ignored_bots"]:
                return

            match getattr(audit.action, "name", None):
                case "webhook_create":
                    action = "Created"
                case "webhook_delete":
                    action = "Deleted"
                case "webhook_update":
                    action = "Updated"
                case _other:
                    # The channel gateway event can beat audit-log delivery.
                    # A generic card is more useful than silently dropping it.
                    action = "Changed"

            webhook: Optional[Object] = audit.target
            if webhook and action != "Deleted":
                try:
                    webhook = await self.bot.fetch_webhook(webhook.id)
                except (NotFound, Forbidden, HTTPException, ClientOSError):
                    pass
            e = Embed(color=cyan)
            e.set_author(name=f"Webhook {action}", icon_url=audit.user_avatar)
            e.set_thumbnail(url=audit.user_display_avatar)
            name = f"**{webhook.name}**" if hasattr(webhook, "name") else "a webhook"
            e.description = chain(
                f"{audit.user_mention} {action.lower()} {name}\n"
                f"In {channel.mention}"
            )
            self.add_to_queue(
                guild_id,
                "webhook_update",
                embed=e,
                actor=audit.user,
                target=webhook,
                target_label="Webhook",
                channel=channel,
                reason=audit.reason,
                details={
                    "⚙️ Action": action,
                    "🧭 Webhook Type": getattr(webhook, "type", None),
                    "🖼️ Avatar": getattr(webhook, "avatar", None),
                    "🤖 Application ID": getattr(webhook, "application_id", None),
                    "👤 Owner": getattr(webhook, "user", None),
                    "🆔 Audit Entry": audit.entry_id,
                },
                links=[("Open channel", channel.jump_url, "↗️")],
                created_at=(audit.created_at.timestamp() if audit.created_at else None),
            )

    @commands.Cog.listener()
    async def on_member_join(self, member):
        guild_id = str(member.guild.id)
        if guild_id in self.config:
            if member.bot:
                audit = await AuditLogSearch(member.guild, Action.bot_add, target=member)
                e = Embed(color=light_grey)
                e.set_author(name="Bot Added", icon_url=audit.user_avatar)
                e.set_thumbnail(url=audit.user_display_avatar)
                inv = (
                    "https://discord.com/oauth2/authorize"
                    f"?client_id={member.id}&permissions=0&scope=bot%20applications.commands"
                )
                e.description = chain(
                    f"{member.mention} was added\n"
                    f"By {audit.user_mention}\n"
                    f"Account created {utils.format_dt(member.created_at, style='F')} "
                    f"({utils.format_dt(member.created_at, style='R')})\n"
                    f"[Invite URL]({inv})"
                )
                self.add_to_queue(
                    guild_id,
                    "bot_add",
                    embed=e,
                    actor=audit.user,
                    target=member,
                    target_label="Bot",
                    reason=audit.reason,
                    details={
                        "🆔 Audit Entry": audit.entry_id,
                    },
                    links=[("Open install page", inv, "🤖")],
                    created_at=(
                        audit.created_at.timestamp()
                        if audit.created_at
                        else member.joined_at.timestamp() if member.joined_at else None
                    ),
                )
                return

            used_invite = None
            if member.guild.me.guild_permissions.manage_guild:
                try:
                    invites = await member.guild.invites()
                except (Forbidden, HTTPException):
                    invites = []
                if guild_id not in self.invites:
                    self.invites[guild_id] = {}
                for current_invite in invites:
                    previous_uses = self.invites[guild_id].get(current_invite.url)
                    current_uses = current_invite.uses or 0
                    if previous_uses is not None and current_uses > previous_uses:
                        used_invite = current_invite
                    elif previous_uses is None and current_uses > 0 and used_invite is None:
                        used_invite = current_invite
                    self.invites[guild_id][current_invite.url] = current_uses

            e = Embed(color=lime_green)
            e.set_author(name="Member Joined", icon_url=get_avatar(member))
            e.set_thumbnail(url=member.display_avatar.url)

            if used_invite and used_invite.inviter:
                description = f"{member.mention} was invited by {used_invite.inviter.mention}\n" \
                              f"With Invite [{used_invite.code}]({used_invite.url})"
                _s = "s" if used_invite.uses != 1 else ""
                description += f" ({used_invite.uses} use{_s})"
            else:
                description = f"{member.mention} joined"

            description += (
                f"\nCreated {utils.format_dt(member.created_at, style='F')}"
                f" ({utils.format_dt(member.created_at, style='R')})"
            )

            e.description = chain(description)
            account_age = utcnow() - member.created_at
            details = {
                "🕒 Account Created": member.created_at,
                "⚠️ Account Age": format_date(seconds=int(account_age.total_seconds())),
            }
            if used_invite:
                details["⌛ Invite Expires"] = used_invite.expires_at
            self.add_to_queue(
                guild_id,
                "member_join",
                embed=e,
                actor=used_invite.inviter if used_invite else None,
                actor_label="Inviter",
                target=member,
                target_label="New Member",
                channel=used_invite.channel if used_invite else None,
                details=details,
                links=(
                    [("Open invite", used_invite.url, "✉️")]
                    if used_invite else []
                ),
                created_at=(member.joined_at.timestamp() if member.joined_at else None),
            )

    @commands.Cog.listener()
    async def on_member_remove(self, member: Member):
        guild_id = str(member.guild.id)
        if guild_id in self.config:
            now = time()
            self.recent_member_bans = {
                key: created_at
                for key, created_at in self.recent_member_bans.items()
                if now - created_at < 30
            }
            if (member.guild.id, member.id) in self.recent_member_bans:
                return

            e = Embed(color=red)
            e.set_author(name="Member Left", icon_url=get_avatar(member))
            e.set_thumbnail(url=member.display_avatar.url)
            description = [
                f"**{member}** left the server",
                f"Created {utils.format_dt(member.created_at, style='R')}",
            ]
            if member.joined_at:
                description.append(f"Joined {utils.format_dt(member.joined_at, style='R')}")
            if member.nick:
                description.append(f"Had the nick `{member.nick}`")
            if member.is_timed_out():
                description.append("Was in timeout")
            e.description = chain(description)

            kick, ban = await asyncio.gather(
                AuditLogSearch(member.guild, Action.kick, target=member),
                AuditLogSearch(member.guild, Action.ban, target=member),
            )
            audit = max(
                (item for item in (kick, ban) if item.action),
                key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc),
                default=None,
            )
            log_type = "member_leave"
            if audit and audit.action is Action.kick:
                log_type = "member_kick"
                e.set_author(name="Member Kicked", icon_url=member.display_avatar.url)
                if audit.user_display_avatar:
                    e.set_thumbnail(url=audit.user_display_avatar)
                summary = [f"{audit.user_mention} kicked **{member}**"]
                if audit.reason:
                    summary.append(f"Reason: `{audit.reason}`")
                e.description = chain(summary)
            elif audit and audit.action is Action.ban:
                log_type = "member_ban"
                self.recent_member_bans[(member.guild.id, member.id)] = now
                e.set_author(name="Member Banned", icon_url=member.display_avatar.url)
                if audit.user_display_avatar:
                    e.set_thumbnail(url=audit.user_display_avatar)
                summary = [f"{audit.user_mention} banned **{member}**"]
                if audit.reason:
                    summary.append(f"Reason: `{audit.reason}`")
                e.description = chain(summary)

            details = {
                "🕒 Account Created": member.created_at,
                "🚪 Joined Server": member.joined_at,
                "🏷️ Nickname": member.nick,
                "🛡️ Roles": [role for role in member.roles if not role.is_default()],
                "⏳ Was Timed Out": member.is_timed_out(),
            }
            self.add_to_queue(
                guild_id,
                log_type,
                embed=e,
                actor=audit.user if audit else None,
                target=member,
                target_label="Member",
                reason=audit.reason if audit else None,
                details=details,
                created_at=(audit.created_at.timestamp() if audit and audit.created_at else None),
            )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: Guild, user: User):
        guild_id = str(guild.id)
        if guild_id not in self.config:
            return
        key = (guild.id, user.id)
        now = time()
        if now - self.recent_member_bans.get(key, 0) < 30:
            return
        self.recent_member_bans[key] = now
        audit = await AuditLogSearch(guild, Action.ban, target=user)
        e = Embed(color=red)
        e.set_author(name="Member Banned", icon_url=get_avatar(user))
        e.set_thumbnail(url=audit.user_display_avatar or user.display_avatar.url)
        summary = [f"{user.mention} was banned"]
        if audit.user:
            summary.append(f"By {audit.user_mention}")
        if audit.reason:
            summary.append(f"Reason: `{audit.reason}`")
        e.description = chain(summary)
        self.add_to_queue(
            guild_id,
            "member_ban",
            embed=e,
            actor=audit.user,
            target=user,
            target_label="Member",
            reason=audit.reason,
            details={"🆔 Audit Entry": audit.entry_id},
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: Guild, user: User):
        guild_id = str(guild.id)
        if guild_id not in self.config:
            return
        audit = await AuditLogSearch(guild, Action.unban, target=user)
        e = Embed(color=lime_green)
        e.set_author(name="Member Unbanned", icon_url=get_avatar(user))
        e.set_thumbnail(url=audit.user_display_avatar or user.display_avatar.url)
        e.description = chain(
            f"{user.mention} was unbanned\n"
            f"By {audit.user_mention}"
        )
        self.add_to_queue(
            guild_id,
            "member_unban",
            embed=e,
            actor=audit.user,
            target=user,
            target_label="Member",
            reason=audit.reason,
            details={"🆔 Audit Entry": audit.entry_id},
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: Member, after: Member):
        guild_id = str(before.guild.id)
        if guild_id in self.config:
            if (
                before.guild_avatar != after.guild_avatar
                and await self.bot.get_privacy(after, "mod_logs")
            ):
                e = Embed(color=0xffb347)
                e.set_author(name="Server Avatar Changed", icon_url=get_avatar(before))
                e.set_thumbnail(url=before.display_avatar.url)
                e.description = f"{after.mention}'s server profile avatar was updated"
                av_to_get = after.guild_avatar
                if av_to_get is None:
                    e.set_author(name="Server Avatar Removed", icon_url=get_avatar(before))
                    e.set_thumbnail(url=after.display_avatar.url)
                    e.description = f"{after.mention}'s server profile avatar was removed"
                    av_to_get = before.guild_avatar
                fn = "avatar.png"
                if ".jpg" in av_to_get.url:
                    fn = "avatar.jpg"
                if ".gif" in av_to_get.url:
                    fn = "avatar.gif"
                e.set_image(url="attachment://" + fn)
                file = None
                with suppress(NotFound, Forbidden, HTTPException, ClientOSError):
                    file = await av_to_get.to_file(filename=fn)
                if file is None:
                    e.set_image(url=av_to_get.url)
                self.add_to_queue(
                    guild_id,
                    "new_server_avatar",
                    embed=e,
                    file=file,
                    target=after,
                    target_label="Member",
                    details={
                        "🖼️ New State": "Changed" if after.guild_avatar else "Removed",
                        "⬅️ Previous Asset": before.guild_avatar,
                        "➡️ Current Asset": after.guild_avatar,
                    },
                )

            if before.nick != after.nick:
                e = Embed(color=blue)
                e.set_author(name="Nick Changed", icon_url=after.display_avatar.url)
                if not before.nick:
                    e.description = f"{after.mention}'s nickname was set to `{after.nick}`"
                elif after.nick:
                    e.description = (
                        f"{after.mention}'s nickname changed from `{before.nick}` "
                        f"to `{after.nick}`"
                    )
                else:
                    e.description = f"{after.mention}'s nickname `{before.nick}` was removed"

                audit = await AuditLogSearch(after.guild, Action.member_update, target=after)
                if audit.user:
                    e.description += f" by {audit.user_mention}"
                else:
                    e.description += f" by {before.mention}"

                self.add_to_queue(
                    guild_id,
                    "nick_change",
                    embed=e,
                    actor=audit.user or after,
                    target=after,
                    target_label="Member",
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.nick,
                        "➡️ After": after.nick,
                    },
                    created_at=(audit.created_at.timestamp() if audit.created_at else None),
                )

            added_roles = [
                role for role in after.roles
                if role not in before.roles and role.id not in self.bot.suppressed
            ]
            removed_roles = [
                role for role in before.roles
                if role not in after.roles and role.id not in self.bot.suppressed
            ]
            if added_roles or removed_roles:
                e = Embed(color=blue)
                if added_roles and removed_roles:
                    action = "Updated"
                elif added_roles:
                    action = "Granted"
                else:
                    action = "Revoked"

                e.set_author(name=f"Roles {action}", icon_url=after.display_avatar.url)
                audit = await AuditLogSearch(
                    after.guild,
                    Action.member_role_update,
                    target=after,
                )
                actor_mention = audit.user_mention if audit.user else None
                changes = []
                if added_roles:
                    changes.append(
                        format_member_role_action(actor_mention, "added", added_roles)
                    )
                if removed_roles:
                    changes.append(
                        format_member_role_action(actor_mention, "removed", removed_roles)
                    )
                e.description = (
                    f"{after.mention}'s roles were updated\n" + "\n".join(changes)
                )

                self.add_to_queue(
                    guild_id,
                    "member_roles_update",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Member",
                    reason=audit.reason,
                    details={
                        "➕ Granted": added_roles or ["None"],
                        "➖ Revoked": removed_roles or ["None"],
                    },
                    created_at=(audit.created_at.timestamp() if audit.created_at else None),
                )

            if before.timed_out_until != after.timed_out_until:
                audit = await AuditLogSearch(after.guild, Action.member_update, target=after)
                timed_out_until = getattr(
                    audit.after,
                    "timed_out_until",
                    after.timed_out_until,
                )
                e = Embed(color=0x9eafe3)
                before_active = before.is_timed_out()
                after_active = after.is_timed_out()
                if after_active and not before_active:
                    title = "User Timed Out"
                    action = "was put in timeout"
                elif not after_active:
                    title = "Timeout Removed"
                    action = "was removed from timeout"
                elif after.timed_out_until > before.timed_out_until:
                    title = "Timeout Extended"
                    action = "had their timeout extended"
                else:
                    title = "Timeout Shortened"
                    action = "had their timeout shortened"
                e.set_author(
                    name=title,
                    icon_url=audit.target_display_avatar or after.display_avatar.url,
                )
                e.set_thumbnail(url=audit.user_display_avatar)
                user = audit.user_mention
                reason = audit.reason or "No reason provided"
                if reason.startswith("By ") and ": " in reason:
                    user, reason = reason.split(": ", 1)
                    user = f"`{user.replace('By ', '', 1)}`"
                description = f"{after.mention} {action}\nBy {user}"
                if timed_out_until:
                    description += (
                        f"\nUntil {utils.format_dt(timed_out_until, style='F')}"
                        f" ({utils.format_dt(timed_out_until, style='R')})"
                    )
                e.description = chain(description)
                self.add_to_queue(
                    guild_id,
                    "timeout",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Member",
                    reason=reason,
                    details={
                        "⬅️ Previous Expiry": before.timed_out_until,
                        "➡️ New Expiry": timed_out_until,
                        "🆔 Audit Entry": audit.entry_id,
                    },
                    created_at=(audit.created_at.timestamp() if audit.created_at else None),
                )

            if before.pending != after.pending:
                e = Embed(color=blue)
                state = "entered" if after.pending else "completed"
                e.set_author(name="Membership Screening Updated", icon_url=after.display_avatar.url)
                e.description = f"{after.mention} {state} membership screening"
                self.add_to_queue(
                    guild_id,
                    "member_screening",
                    embed=e,
                    target=after,
                    target_label="Member",
                    details={
                        "⬅️ Before": "Pending" if before.pending else "Complete",
                        "➡️ After": "Pending" if after.pending else "Complete",
                    },
                )

    @commands.Cog.listener()
    async def on_user_update(self, before: User, after: User):
        if not await self.bot.get_privacy(after, "mod_logs"):
            return
        if self.username_cd.check(after.id):
            return

        username_changed = (
            before.name != after.name
            or before.discriminator != after.discriminator
        )
        profile_changes = {}
        for attribute, label in (
            ("global_name", "🪪 Display Name"),
            ("avatar", "🖼️ Avatar"),
            ("banner", "🎨 Banner"),
            ("accent_color", "🌈 Accent Color"),
            ("avatar_decoration", "✨ Avatar Decoration"),
            ("primary_guild", "🏷️ Primary Guild Tag"),
        ):
            before_value = getattr(before, attribute, None)
            after_value = getattr(after, attribute, None)
            if profile_value_key(attribute, before_value) != profile_value_key(
                attribute, after_value
            ):
                profile_changes[label] = format_profile_change(
                    attribute,
                    before_value,
                    after_value,
                )

        if not username_changed and not profile_changes:
            return
        for guild in self.bot.guilds:
            await asyncio.sleep(0)
            member = guild.get_member(after.id)
            guild_id = str(guild.id)
            if member is None or guild_id not in self.config or len(guild.members) > 3000:
                continue

            if username_changed:
                e = Embed(color=0xb4f5f6)
                e.set_author(name="Username Changed", icon_url=get_avatar(after))
                e.set_thumbnail(url=member.display_avatar.url)
                e.description = chain(
                    f"{after.mention} updated their username\n"
                    f"To `{after}`\n"
                    f"From `{before}`"
                )
                self.add_to_queue(
                    guild_id,
                    "username_change",
                    embed=e,
                    actor=after,
                    actor_label="Changed By",
                    target=member,
                    target_label="Member",
                    details={
                        "⬅️ Before": str(before),
                        "➡️ After": str(after),
                    },
                )

            if profile_changes:
                e = Embed(color=0xb4f5f6)
                e.set_author(name="User Profile Updated", icon_url=get_avatar(after))
                e.set_thumbnail(url=member.display_avatar.url)
                e.description = f"{after.mention} updated their Discord profile"
                if before.avatar != after.avatar and after.avatar:
                    e.set_image(url=after.avatar.url)
                self.add_to_queue(
                    guild_id,
                    "user_profile_update",
                    embed=e,
                    actor=after,
                    actor_label="Changed By",
                    target=member,
                    target_label="Member",
                    details=profile_changes,
                )

    @commands.Cog.listener()
    async def on_thread_create(self, thread: Thread):
        guild_id = str(thread.guild.id)
        if guild_id in self.config:
            owner = thread.owner or thread.guild.get_member(thread.owner_id)
            e = Embed(color=dark_green)
            e.set_author(
                name="Thread Created",
                icon_url=get_avatar(owner) if owner else None,
            )
            if owner:
                e.set_thumbnail(url=owner.display_avatar.url)
            e.description = chain(
                f"{getattr(owner, 'mention', 'Unknown-User')} created a "
                f"{'private' if thread.is_private() else 'public'} thread\n"
                f"Named `{thread.name}`\n"
                f"In {getattr(thread.parent, 'mention', 'Unknown channel')}\n"
                f"**[Jump URL]({thread.jump_url})**"
            )
            self.add_to_queue(
                guild_id,
                "thread_create",
                embed=e,
                actor=owner,
                target=thread,
                target_label="Thread",
                channel=thread.parent,
                details={
                    "🧭 Type": "Private" if thread.is_private() else "Public",
                    "🏷️ Applied Tags": list(thread.applied_tags),
                    "🗄️ Auto Archive": f"{thread.auto_archive_duration} minutes",
                    "⏱️ Slowmode": f"{thread.slowmode_delay} seconds",
                    "🔒 Locked": thread.locked,
                },
                links=[("Open thread", thread.jump_url, "🧵")],
                created_at=thread.created_at.timestamp(),
            )

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: Thread):
        guild_id = str(thread.guild.id)
        if guild_id in self.config:
            audit = await AuditLogSearch(thread.guild, Action.thread_delete, target=thread)
            e = Embed(color=dark_green)
            e.set_author(name="Thread Deleted", icon_url=audit.user_avatar)
            e.set_thumbnail(url=audit.user_display_avatar)
            owner = thread.owner
            if not owner and thread.owner_id:
                with suppress(NotFound, HTTPException):
                    owner = await self.bot.fetch_user(thread.owner_id)
            e.description = chain(
                f"{audit.user_mention} deleted a {'private' if thread.is_private() else 'public'} thread\n"
                f"Named `{thread.name}`\n"
                f"Owned by {getattr(owner, 'mention', 'Unknown-User')}\n"
                f"In {getattr(thread.parent, 'mention', 'Unknown channel')}"
            )
            parent_url = getattr(thread.parent, "jump_url", None)
            self.add_to_queue(
                guild_id,
                "thread_delete",
                embed=e,
                actor=audit.user,
                target=thread,
                target_label="Deleted Thread",
                channel=thread.parent,
                reason=audit.reason,
                details={
                    "👑 Owner": owner,
                    "🧭 Type": "Private" if thread.is_private() else "Public",
                    "💬 Messages": thread.message_count,
                    "👥 Members": thread.member_count,
                    "🏷️ Applied Tags": list(thread.applied_tags),
                },
                links=[("Open parent", parent_url, "↗️")] if parent_url else [],
                created_at=(audit.created_at.timestamp() if audit.created_at else None),
            )

    @commands.Cog.listener()
    async def on_thread_update(self, before: Thread, after: Thread):
        guild_id = str(before.guild.id)
        if guild_id in self.config:
            audit = await AuditLogSearch(before.guild, Action.thread_update, target=after)

            if before.name != after.name:
                e = Embed(color=dark_green)
                e.set_author(name="Thread Renamed", icon_url=audit.user_avatar)
                e.set_thumbnail(url=audit.user_display_avatar)
                e.description = chain(
                    f"{audit.user_mention} renamed `{before.name}`\n"
                    f"To `{after.name}`\n"
                    f"In {getattr(after.parent, 'mention', 'Unknown channel')}"
                )
                self.add_to_queue(
                    guild_id,
                    "thread_update",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Thread",
                    channel=after.parent,
                    reason=audit.reason,
                    details={
                        "⬅️ Before": before.name,
                        "➡️ After": after.name,
                    },
                    links=[("Open thread", after.jump_url, "🧵")],
                    created_at=(audit.created_at.timestamp() if audit.created_at else None),
                )

            state_changes = {}
            for attribute, label in (
                ("archived", "🗄️ Archived"),
                ("locked", "🔒 Locked"),
                ("invitable", "✉️ Invitable"),
                ("auto_archive_duration", "⏳ Auto Archive"),
                ("slowmode_delay", "⏱️ Slowmode"),
            ):
                before_value = getattr(before, attribute, None)
                after_value = getattr(after, attribute, None)
                if before_value != after_value:
                    state_changes[label] = f"{before_value} → {after_value}"
            if state_changes:
                e = Embed(color=dark_green)
                if not before.archived and after.archived:
                    title = "Thread Archived"
                elif before.archived and not after.archived:
                    title = "Thread Reopened"
                elif not before.locked and after.locked:
                    title = "Thread Locked"
                elif before.locked and not after.locked:
                    title = "Thread Unlocked"
                else:
                    title = "Thread Settings Updated"
                e.set_author(name=title, icon_url=audit.user_avatar)
                e.set_thumbnail(url=audit.user_display_avatar)
                e.description = f"{audit.user_mention} updated {after.mention}"
                self.add_to_queue(
                    guild_id,
                    "thread_update",
                    embed=e,
                    actor=audit.user,
                    target=after,
                    target_label="Thread",
                    channel=after.parent,
                    reason=audit.reason,
                    details=state_changes,
                    links=[("Open thread", after.jump_url, "🧵")],
                    created_at=(audit.created_at.timestamp() if audit.created_at else None),
                )

            if isinstance(before.parent, ForumChannel):
                before_tags = {tag.id: tag for tag in before.applied_tags}
                after_tags = {tag.id: tag for tag in after.applied_tags}
                if before_tags != after_tags:
                    for tag_id in before_tags.keys() - after_tags.keys():
                        tag = before_tags[tag_id]
                        e = Embed(color=dark_green)
                        e.set_author(name="Tag Removed", icon_url=audit.user_avatar)
                        e.set_thumbnail(url=audit.user_display_avatar)
                        e.description = chain(
                            f"**{tag.name}** was removed\n"
                            f"From `{after.name}`\n"
                            f"By {audit.user_mention}\n"
                            f"**[Jump URL]({after.jump_url})**"
                        )
                        self.add_to_queue(
                            guild_id,
                            "thread_tagged",
                            embed=e,
                            actor=audit.user,
                            target=after,
                            target_label="Thread",
                            channel=after.parent,
                            reason=audit.reason,
                            details={
                                "➖ Removed Tag": tag.name,
                            },
                            links=[("Open thread", after.jump_url, "🧵")],
                        )
                    for tag_id in after_tags.keys() - before_tags.keys():
                        tag = after_tags[tag_id]
                        e = Embed(color=dark_green)
                        e.set_author(name="Tag Added", icon_url=audit.user_avatar)
                        e.set_thumbnail(url=audit.user_display_avatar)
                        e.description = chain(
                            f"**{tag.name}** was applied\n"
                            f"To `{after.name}`\n"
                            f"By {audit.user_mention}\n"
                            f"**[Jump URL]({after.jump_url})**"
                        )
                        self.add_to_queue(
                            guild_id,
                            "thread_tagged",
                            embed=e,
                            actor=audit.user,
                            target=after,
                            target_label="Thread",
                            channel=after.parent,
                            reason=audit.reason,
                            details={
                                "➕ Added Tag": tag.name,
                            },
                            links=[("Open thread", after.jump_url, "🧵")],
                        )

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild, before: Sequence[Emoji], after: Sequence[Emoji]):
        guild_id = str(guild.id)
        if guild_id in self.config:
            # Emoji rename
            index = {emoji.id: emoji for emoji in before}
            before_ids = set(index)
            after_ids = {emoji.id for emoji in after}
            for emoji in after:
                await asyncio.sleep(0)
                if emoji.id in index and emoji.name != index[emoji.id].name:
                    event_audit = await AuditLogSearch(
                        guild, Action.emoji_update, target=emoji
                    )
                    actor = self.pop_emoji_actor(guild.id, "rename", emoji.id, index[emoji.id].name)
                    actor_mention = actor.mention if actor else event_audit.user_mention
                    actor_avatar = get_avatar(actor) if actor else event_audit.user_avatar
                    actor_display_avatar = actor.display_avatar.url if actor else event_audit.user_display_avatar
                    e = Embed(color=orange)
                    e.set_author(name="Emoji Renamed", icon_url=actor_avatar)
                    e.set_thumbnail(url=actor_display_avatar)
                    e.description = chain(
                        f"{emoji} was renamed to `{emoji.name}`\n"
                        f"From {index[emoji.id].name}\n"
                        f"By {actor_mention}"
                    )
                    self.add_to_queue(
                        guild_id,
                        "emoji_rename",
                        embed=e,
                        actor=actor or event_audit.user,
                        target=emoji,
                        target_label="Emoji",
                        reason=event_audit.reason,
                        details={
                            "⬅️ Before": index[emoji.id].name,
                            "➡️ After": emoji.name,
                            "🎞️ Animated": emoji.animated,
                            "🔐 Role Restricted": list(emoji.roles),
                        },
                        links=[("Open emoji", emoji.url, "😀")],
                        created_at=(
                            event_audit.created_at.timestamp()
                            if event_audit.created_at else None
                        ),
                    )

            # Emoji delete
            for emoji in before:
                await asyncio.sleep(0)
                if emoji.id not in after_ids:
                    event_audit = await AuditLogSearch(
                        guild, Action.emoji_delete, target=emoji
                    )
                    actor = self.pop_emoji_actor(guild.id, "delete", emoji.id, emoji.name)
                    actor_mention = actor.mention if actor else event_audit.user_mention
                    actor_avatar = get_avatar(actor) if actor else event_audit.user_avatar
                    actor_display_avatar = actor.display_avatar.url if actor else event_audit.user_display_avatar
                    e = Embed(color=orange)
                    e.set_author(name="Emoji Deleted", icon_url=actor_avatar)
                    e.set_thumbnail(url=actor_display_avatar)
                    try:
                        fn = "emoji" + (".gif" if emoji.animated else ".png")
                        file = await emoji.to_file(filename=fn)
                        e.set_author(name="Emoji Deleted", icon_url="attachment://" + fn)
                    except (NotFound, Forbidden, HTTPException, ClientOSError):
                        file = None
                    e.description = chain(
                        f"**{emoji.name}** was deleted\n"
                        f"By {actor_mention}"
                    )
                    self.add_to_queue(
                        guild_id,
                        "emoji_delete",
                        embed=e,
                        file=file,
                        actor=actor or event_audit.user,
                        target=emoji,
                        target_label="Deleted Emoji",
                        reason=event_audit.reason,
                        details={
                            "🎞️ Animated": emoji.animated,
                            "🤖 Managed": emoji.managed,
                            "🔐 Role Restricted": list(emoji.roles),
                        },
                        created_at=(
                            event_audit.created_at.timestamp()
                            if event_audit.created_at else None
                        ),
                    )

            # Emoji create
            for emoji in after:
                await asyncio.sleep(0)
                if emoji.id not in before_ids:
                    event_audit = await AuditLogSearch(
                        guild, Action.emoji_create, target=emoji
                    )
                    actor = self.pop_emoji_actor(guild.id, "create", emoji.id, emoji.name)
                    actor_mention = actor.mention if actor else event_audit.user_mention
                    actor_avatar = get_avatar(actor) if actor else event_audit.user_avatar
                    actor_display_avatar = actor.display_avatar.url if actor else event_audit.user_display_avatar
                    e = Embed(color=orange)
                    e.set_author(name="Emoji Created", icon_url=actor_avatar)
                    e.set_thumbnail(url=actor_display_avatar)
                    e.description = chain(
                        f"{emoji} was created\n"
                        f"By {actor_mention}"
                    )
                    self.add_to_queue(
                        guild_id,
                        "emoji_create",
                        embed=e,
                        actor=actor or event_audit.user,
                        target=emoji,
                        target_label="Emoji",
                        reason=event_audit.reason,
                        details={
                            "🎞️ Animated": emoji.animated,
                            "🤖 Managed": emoji.managed,
                            "🔐 Role Restricted": list(emoji.roles),
                        },
                        links=[("Open emoji", emoji.url, "😀")],
                        created_at=(
                            event_audit.created_at.timestamp()
                            if event_audit.created_at else None
                        ),
                    )

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild, before: Sequence[Sticker], after: Sequence[Sticker]):
        guild_id = str(guild.id)
        if guild_id in self.config:
            # sticker rename
            index = {sticker.id: sticker for sticker in before}
            before_ids = set(index)
            after_ids = {sticker.id for sticker in after}
            for sticker in after:
                await asyncio.sleep(0)
                if sticker.id in index:
                    if sticker.name != index[sticker.id].name:
                        audit = await AuditLogSearch(
                            guild, Action.sticker_update, target=sticker
                        )
                        e = Embed(color=orange)
                        e.set_author(name="Sticker Renamed", icon_url=sticker.url)
                        e.set_thumbnail(url=audit.user_display_avatar)
                        e.set_image(url=sticker.url)
                        e.description = chain(
                            f"**{index[sticker.id].name}** was renamed to **{sticker.name}**\n"
                            f"From {index[sticker.id].name}\n"
                            f"By {audit.user_mention}"
                        )
                        self.add_to_queue(
                            guild_id,
                            "sticker_update",
                            embed=e,
                            actor=audit.user,
                            target=sticker,
                            target_label="Sticker",
                            reason=audit.reason,
                            details={
                                "⬅️ Before": index[sticker.id].name,
                                "➡️ After": sticker.name,
                                "🧩 Format": sticker.format,
                                "😀 Related Emoji": sticker.emoji,
                            },
                            links=[("Open sticker", sticker.url, "🏷️")],
                            created_at=(audit.created_at.timestamp() if audit.created_at else None),
                        )
                    if sticker.description != index[sticker.id].description:
                        audit = await AuditLogSearch(
                            guild, Action.sticker_update, target=sticker
                        )
                        e = Embed(color=orange)
                        e.set_author(name="New Sticker Description", icon_url=sticker.url)
                        e.set_thumbnail(url=audit.user_display_avatar)
                        e.set_image(url=sticker.url)
                        e.description = chain(
                            f"{audit.user_mention} updated **{sticker.name}**\n"
                            f"To `{sticker.description}`\n"
                            f"From `{index[sticker.id].description}`"
                        )
                        self.add_to_queue(
                            guild_id,
                            "sticker_update",
                            embed=e,
                            actor=audit.user,
                            target=sticker,
                            target_label="Sticker",
                            reason=audit.reason,
                            details={
                                "⬅️ Before": index[sticker.id].description,
                                "➡️ After": sticker.description,
                                "🧩 Format": sticker.format,
                                "😀 Related Emoji": sticker.emoji,
                            },
                            links=[("Open sticker", sticker.url, "🏷️")],
                            created_at=(audit.created_at.timestamp() if audit.created_at else None),
                        )

            # sticker delete
            for sticker in before:
                await asyncio.sleep(0)
                if sticker.id not in after_ids:
                    audit = await AuditLogSearch(
                        guild, Action.sticker_delete, target=sticker
                    )
                    e = Embed(color=orange)
                    e.set_author(name="Sticker Deleted", icon_url=audit.user_avatar)
                    e.set_thumbnail(url=audit.user_display_avatar)
                    try:
                        extension = {
                            "png": ".png",
                            "apng": ".png",
                            "lottie": ".json",
                            "gif": ".gif",
                        }.get(sticker.format.name, ".bin")
                        fn = "sticker" + extension
                        file = await sticker.to_file(filename=fn)
                        e.set_author(name="Sticker Deleted", icon_url="attachment://" + fn)
                    except (NotFound, Forbidden, HTTPException, ClientOSError):
                        file = None
                    e.description = chain(
                        f"**{sticker.name}** was deleted\n"
                        f"By {audit.user_mention}"
                    )
                    self.add_to_queue(
                        guild_id,
                        "sticker_delete",
                        embed=e,
                        file=file,
                        actor=audit.user,
                        target=sticker,
                        target_label="Deleted Sticker",
                        reason=audit.reason,
                        details={
                            "🧩 Format": sticker.format,
                            "😀 Related Emoji": sticker.emoji,
                            "📝 Description": sticker.description,
                            "✅ Available": sticker.available,
                        },
                        created_at=(audit.created_at.timestamp() if audit.created_at else None),
                    )

            # sticker create
            for sticker in after:
                await asyncio.sleep(0)
                if sticker.id not in before_ids:
                    audit = await AuditLogSearch(
                        guild, Action.sticker_create, target=sticker
                    )
                    e = Embed(color=orange)
                    e.set_author(name="Sticker Created", icon_url=sticker.url)
                    e.set_thumbnail(url=audit.user_display_avatar)
                    description = f"**{sticker.name}** was created\n" \
                                  f"By {audit.user_mention}"
                    if sticker.description:
                        e.add_field(name="Description", value=sticker.description)
                    e.description = chain(description)
                    self.add_to_queue(
                        guild_id,
                        "sticker_create",
                        embed=e,
                        actor=audit.user,
                        target=sticker,
                        target_label="Sticker",
                        reason=audit.reason,
                        details={
                            "🧩 Format": sticker.format,
                            "😀 Related Emoji": sticker.emoji,
                            "📝 Description": sticker.description,
                            "✅ Available": sticker.available,
                        },
                        links=[("Open sticker", sticker.url, "🏷️")],
                        created_at=(audit.created_at.timestamp() if audit.created_at else None),
                    )

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        if invite.guild:
            guild_id = str(invite.guild.id)
            if guild_id in self.config:
                e = Embed(color=lime_green)
                e.set_author(
                    name="Invite Created",
                    icon_url=get_avatar(invite.inviter) if invite.inviter else Images.create,
                )
                if invite.inviter:
                    e.set_thumbnail(url=invite.inviter.display_avatar.url)
                max_uses = f"{invite.max_uses} uses" if invite.max_uses else "unlimited uses"
                expires = "never"
                if invite.expires_at:
                    expires = (
                        f"{utils.format_dt(invite.expires_at, style='F')} "
                        f"({utils.format_dt(invite.expires_at, style='R')})"
                    )
                inviter = invite.inviter.mention if invite.inviter else "Unknown-User"
                e.description = chain(
                    f"{inviter} created [{invite.code}]({invite.url})\n"
                    f"In {invite.channel.mention}\n"
                    f"With {max_uses}\n"
                    f"Expires {expires}"
                )
                self.invites.setdefault(guild_id, {})[invite.url] = invite.uses or 0
                self.add_to_queue(
                    guild_id,
                    "invite_create",
                    embed=e,
                    actor=invite.inviter,
                    target=f"Invite `{invite.code}`",
                    target_label="Invite",
                    channel=invite.channel,
                    details={
                        "🔢 Max Uses": invite.max_uses or "Unlimited",
                        "⏳ Max Age": (
                            format_date(seconds=invite.max_age)
                            if invite.max_age else "Never"
                        ),
                        "⌛ Expires": invite.expires_at,
                        "👋 Temporary Membership": invite.temporary,
                        "🎯 Target": invite.target_user or invite.target_application,
                        "🗓️ Scheduled Event": invite.scheduled_event,
                    },
                    links=[("Open invite", invite.url, "✉️")],
                    created_at=(invite.created_at.timestamp() if invite.created_at else None),
                )

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        if not invite.guild:
            return
        guild_id = str(invite.guild.id)
        if guild_id not in self.config:
            return
        audit = await AuditLogSearch(invite.guild, Action.invite_delete)
        e = Embed(color=red)
        e.set_author(name="Invite Deleted", icon_url=audit.user_avatar or Images.trash)
        e.set_thumbnail(url=audit.user_display_avatar)
        e.description = chain(
            f"Invite `{invite.code}` was deleted\n"
            f"By {audit.user_mention}\n"
            f"In {getattr(invite.channel, 'mention', 'Unknown channel')}"
        )
        self.invites.setdefault(guild_id, {}).pop(invite.url, None)
        channel_url = getattr(invite.channel, "jump_url", None)
        self.add_to_queue(
            guild_id,
            "invite_delete",
            embed=e,
            actor=audit.user,
            target=f"Invite `{invite.code}`",
            target_label="Deleted Invite",
            channel=invite.channel,
            reason=audit.reason,
            details={
                "🔢 Uses": invite.uses,
                "🔢 Max Uses": invite.max_uses or "Unlimited",
                "⌛ Expired At": invite.expires_at,
                "👋 Temporary Membership": invite.temporary,
                "🆔 Audit Entry": audit.entry_id,
            },
            links=[("Open channel", channel_url, "↗️")] if channel_url else [],
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @staticmethod
    def automod_rule_details(rule) -> dict:
        trigger = rule.trigger
        actions = []
        for action in rule.actions:
            summary = action.type.name.replace("_", " ").title()
            if action.duration:
                summary += f" • {format_date(seconds=int(action.duration.total_seconds()))}"
            if action.channel_id:
                summary += f" • <#{action.channel_id}>"
            if action.custom_message:
                summary += f" • {utils.escape_markdown(action.custom_message)[:250]}"
            actions.append(summary)
        return {
            "⚡ Trigger": trigger.type,
            "🎬 Actions": actions or ["None"],
            "✅ Enabled": rule.enabled,
            "🔤 Keywords": len(trigger.keyword_filter),
            "🧪 Regex Patterns": len(trigger.regex_patterns),
            "✅ Allowed Terms": len(trigger.allow_list),
            "📣 Mention Limit": trigger.mention_limit or "Not set",
            "🚨 Raid Protection": trigger.mention_raid_protection,
            "🛡️ Exempt Roles": list(rule.exempt_roles),
            "📍 Exempt Channels": list(rule.exempt_channels),
        }

    async def log_automod_rule_change(
        self,
        rule,
        *,
        title: str,
        log_type: str,
        audit_action,
        color,
    ) -> None:
        guild_id = str(rule.guild.id)
        if guild_id not in self.config:
            return
        audit = await AuditLogSearch(rule.guild, audit_action, target=rule)
        actor = audit.user or rule.creator
        e = Embed(color=color)
        e.set_author(name=title, icon_url=get_avatar(actor) if actor else None)
        if actor:
            e.set_thumbnail(url=actor.display_avatar.url)
        e.description = chain(
            f"**{rule.name}** was {title.removeprefix('AutoMod Rule ').lower()}\n"
            f"By {getattr(actor, 'mention', 'Unknown-User')}"
        )
        self.add_to_queue(
            guild_id,
            log_type,
            embed=e,
            actor=actor,
            target=rule,
            target_label="AutoMod Rule",
            reason=audit.reason,
            details={
                **self.automod_rule_details(rule),
                "🆔 Audit Entry": audit.entry_id,
            },
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @commands.Cog.listener()
    async def on_automod_rule_create(self, rule):
        await self.log_automod_rule_change(
            rule,
            title="AutoMod Rule Created",
            log_type="automod_rule_create",
            audit_action=Action.automod_rule_create,
            color=lime_green,
        )

    @commands.Cog.listener()
    async def on_automod_rule_update(self, rule):
        await self.log_automod_rule_change(
            rule,
            title="AutoMod Rule Updated",
            log_type="automod_rule_update",
            audit_action=Action.automod_rule_update,
            color=orange,
        )

    @commands.Cog.listener()
    async def on_automod_rule_delete(self, rule):
        await self.log_automod_rule_change(
            rule,
            title="AutoMod Rule Deleted",
            log_type="automod_rule_delete",
            audit_action=Action.automod_rule_delete,
            color=red,
        )

    @commands.Cog.listener()
    async def on_automod_action(self, execution):
        guild = execution.guild
        guild_id = str(execution.guild_id)
        if guild is None or guild_id not in self.config:
            return
        member = execution.member or guild.get_member(execution.user_id)
        channel = execution.channel or guild.get_channel(execution.channel_id)
        try:
            rule = await execution.fetch_rule()
        except (NotFound, Forbidden, HTTPException):
            rule = None

        action_name = execution.action.type.name.replace("_", " ").title()
        title = f"AutoMod {action_name}"
        e = Embed(color=red if "Block" in action_name or "Timeout" in action_name else orange)
        e.set_author(name=title, icon_url=get_avatar(member) if member else None)
        if member:
            e.set_thumbnail(url=member.display_avatar.url)
        e.description = chain(
            f"Discord AutoMod acted on {getattr(member, 'mention', f'<@{execution.user_id}>')}\n"
            f"Action: **{action_name}**\n"
            f"Rule: **{getattr(rule, 'name', 'Unknown rule')}**\n"
            f"In {getattr(channel, 'mention', f'<#{execution.channel_id}>')}"
        )

        content = execution.content or ""
        matched = execution.matched_content or execution.matched_keyword
        file = None
        if len(content) > 1000:
            file = File(BytesIO(content.encode()), filename="automod-message.txt")
            content_summary = "Attached as `automod-message.txt`"
        else:
            content_summary = utils.escape_markdown(content) or "Unavailable"

        links = []
        if execution.message_id and execution.channel_id:
            message_url = (
                f"https://discord.com/channels/{execution.guild_id}/"
                f"{execution.channel_id}/{execution.message_id}"
            )
            links.append(("Open message", message_url, "↗️"))
        if execution.alert_system_message_id and execution.action.channel_id:
            alert_url = (
                f"https://discord.com/channels/{execution.guild_id}/"
                f"{execution.action.channel_id}/{execution.alert_system_message_id}"
            )
            links.append(("Open alert", alert_url, "🛡️"))

        details = {
            "🤖 AutoMod Action": action_name,
            "🛡️ Rule": getattr(rule, "name", f"Unknown (`{execution.rule_id}`)"),
            "⚡ Trigger": execution.rule_trigger_type,
            "🎯 Matched": utils.escape_markdown(str(matched)) if matched else "Unavailable",
            "💬 Message Content": content_summary,
            "🆔 Message ID": execution.message_id,
            "🆔 Alert Message ID": execution.alert_system_message_id,
        }
        if execution.action.duration:
            details["⏳ Timeout Duration"] = execution.action.duration
        if execution.action.custom_message:
            details["📝 Custom Response"] = utils.escape_markdown(execution.action.custom_message)

        self.add_to_queue(
            guild_id,
            "automod_action",
            embed=e,
            file=file,
            target=member or f"<@{execution.user_id}>",
            target_label="Member",
            channel=channel,
            details=details,
            links=links,
        )

    @staticmethod
    def scheduled_event_details(event) -> dict:
        return {
            "📅 Starts": event.start_time,
            "🏁 Ends": event.end_time,
            "📊 Status": event.status,
            "🧭 Event Type": event.entity_type,
            "📍 Location": event.channel or event.location,
            "👥 Interested": event.user_count,
            "🔐 Privacy": event.privacy_level,
            "📝 Description": event.description,
        }

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event):
        guild = event.guild or self.bot.get_guild(event.guild_id)
        guild_id = str(event.guild_id)
        if guild is None or guild_id not in self.config:
            return
        audit = await AuditLogSearch(guild, Action.scheduled_event_create, target=event)
        actor = audit.user or event.creator
        e = Embed(color=lime_green)
        e.set_author(name="Scheduled Event Created", icon_url=get_avatar(actor) if actor else None)
        if actor:
            e.set_thumbnail(url=actor.display_avatar.url)
        if event.cover_image:
            e.set_image(url=event.cover_image.url)
        e.description = chain(
            f"**{event.name}** was scheduled\n"
            f"By {getattr(actor, 'mention', 'Unknown-User')}\n"
            f"Starts {utils.format_dt(event.start_time, style='R')}"
        )
        self.add_to_queue(
            guild_id,
            "scheduled_event_create",
            embed=e,
            actor=actor,
            target=event,
            target_label="Scheduled Event",
            channel=event.channel,
            reason=audit.reason,
            details=self.scheduled_event_details(event),
            links=[("View event", event.url, "🗓️")],
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before, after):
        guild = after.guild or self.bot.get_guild(after.guild_id)
        guild_id = str(after.guild_id)
        if guild is None or guild_id not in self.config:
            return
        audit = await AuditLogSearch(guild, Action.scheduled_event_update, target=after)
        changes = {}
        for attribute, label in (
            ("name", "🪧 Name"),
            ("description", "📝 Description"),
            ("status", "📊 Status"),
            ("start_time", "📅 Starts"),
            ("end_time", "🏁 Ends"),
            ("channel_id", "📍 Channel"),
            ("location", "🌐 Location"),
            ("entity_type", "🧭 Event Type"),
            ("privacy_level", "🔐 Privacy"),
        ):
            before_value = getattr(before, attribute, None)
            after_value = getattr(after, attribute, None)
            if before_value != after_value:
                changes[label] = f"{before_value or 'Not set'} → {after_value or 'Not set'}"
        if not changes and before.cover_image == after.cover_image:
            return
        if before.cover_image != after.cover_image:
            changes["🖼️ Cover Image"] = "Updated"
        e = Embed(color=orange)
        e.set_author(name="Scheduled Event Updated", icon_url=audit.user_avatar)
        e.set_thumbnail(url=audit.user_display_avatar)
        if after.cover_image:
            e.set_image(url=after.cover_image.url)
        e.description = f"{audit.user_mention} updated **{after.name}**"
        self.add_to_queue(
            guild_id,
            "scheduled_event_update",
            embed=e,
            actor=audit.user,
            target=after,
            target_label="Scheduled Event",
            channel=after.channel,
            reason=audit.reason,
            details=changes,
            links=[("View event", after.url, "🗓️")],
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event):
        guild = event.guild or self.bot.get_guild(event.guild_id)
        guild_id = str(event.guild_id)
        if guild is None or guild_id not in self.config:
            return
        audit = await AuditLogSearch(guild, Action.scheduled_event_delete, target=event)
        e = Embed(color=red)
        e.set_author(name="Scheduled Event Deleted", icon_url=audit.user_avatar)
        e.set_thumbnail(url=audit.user_display_avatar)
        if event.cover_image:
            e.set_image(url=event.cover_image.url)
        e.description = chain(
            f"**{event.name}** was deleted\n"
            f"By {audit.user_mention}"
        )
        channel_url = getattr(event.channel, "jump_url", None)
        self.add_to_queue(
            guild_id,
            "scheduled_event_delete",
            embed=e,
            actor=audit.user,
            target=event,
            target_label="Deleted Event",
            channel=event.channel,
            reason=audit.reason,
            details=self.scheduled_event_details(event),
            links=[("Open channel", channel_url, "↗️")] if channel_url else [],
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @commands.Cog.listener()
    async def on_stage_instance_create(self, instance):
        guild_id = str(instance.guild.id)
        if guild_id not in self.config:
            return
        audit = await AuditLogSearch(
            instance.guild, Action.stage_instance_create, target=instance
        )
        e = Embed(color=lime_green)
        e.set_author(name="Stage Started", icon_url=audit.user_avatar)
        e.set_thumbnail(url=audit.user_display_avatar)
        e.description = chain(
            f"{audit.user_mention} started a Stage\n"
            f"Topic: **{instance.topic}**\n"
            f"In {getattr(instance.channel, 'mention', 'Unknown channel')}"
        )
        self.add_to_queue(
            guild_id,
            "stage_create",
            embed=e,
            actor=audit.user,
            target=instance.channel,
            target_label="Stage Channel",
            channel=instance.channel,
            reason=audit.reason,
            details={
                "📝 Topic": instance.topic,
                "🔐 Privacy": instance.privacy_level,
                "🗓️ Scheduled Event": instance.scheduled_event,
                "🔎 Discoverable": not instance.discoverable_disabled,
            },
            links=(
                [("Open Stage", instance.channel.jump_url, "🎙️")]
                if instance.channel else []
            ),
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @commands.Cog.listener()
    async def on_stage_instance_update(self, before, after):
        guild_id = str(after.guild.id)
        if guild_id not in self.config:
            return
        audit = await AuditLogSearch(
            after.guild, Action.stage_instance_update, target=after
        )
        changes = {}
        for attribute, label in (
            ("topic", "📝 Topic"),
            ("privacy_level", "🔐 Privacy"),
            ("discoverable_disabled", "🔎 Discoverability Disabled"),
            ("scheduled_event_id", "🗓️ Scheduled Event ID"),
        ):
            before_value = getattr(before, attribute, None)
            after_value = getattr(after, attribute, None)
            if before_value != after_value:
                changes[label] = f"{before_value or 'Not set'} → {after_value or 'Not set'}"
        if not changes:
            return
        e = Embed(color=orange)
        e.set_author(name="Stage Updated", icon_url=audit.user_avatar)
        e.set_thumbnail(url=audit.user_display_avatar)
        e.description = (
            f"{audit.user_mention} updated the Stage in "
            f"{getattr(after.channel, 'mention', 'Unknown channel')}"
        )
        self.add_to_queue(
            guild_id,
            "stage_update",
            embed=e,
            actor=audit.user,
            target=after.channel,
            target_label="Stage Channel",
            channel=after.channel,
            reason=audit.reason,
            details=changes,
            links=[("Open Stage", after.channel.jump_url, "🎙️")] if after.channel else [],
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @commands.Cog.listener()
    async def on_stage_instance_delete(self, instance):
        guild_id = str(instance.guild.id)
        if guild_id not in self.config:
            return
        audit = await AuditLogSearch(
            instance.guild, Action.stage_instance_delete, target=instance
        )
        e = Embed(color=red)
        e.set_author(name="Stage Ended", icon_url=audit.user_avatar)
        e.set_thumbnail(url=audit.user_display_avatar)
        e.description = chain(
            f"The Stage **{instance.topic}** ended\n"
            f"By {audit.user_mention}\n"
            f"In {getattr(instance.channel, 'mention', 'Unknown channel')}"
        )
        self.add_to_queue(
            guild_id,
            "stage_delete",
            embed=e,
            actor=audit.user,
            target=instance.channel,
            target_label="Stage Channel",
            channel=instance.channel,
            reason=audit.reason,
            details={
                "📝 Topic": instance.topic,
                "🗓️ Scheduled Event": instance.scheduled_event,
            },
            links=(
                [("Open channel", instance.channel.jump_url, "↗️")]
                if instance.channel else []
            ),
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @staticmethod
    def soundboard_details(sound) -> dict:
        return {
            "😀 Emoji": sound.emoji,
            "🔊 Volume": f"{sound.volume:.0%}",
            "✅ Available": sound.available,
            "⬆️ Uploaded By": sound.user,
        }

    @commands.Cog.listener()
    async def on_soundboard_sound_create(self, sound):
        guild_id = str(sound.guild.id)
        if guild_id not in self.config:
            return
        audit = await AuditLogSearch(
            sound.guild, Action.soundboard_sound_create, target=sound
        )
        e = Embed(color=lime_green)
        e.set_author(name="Soundboard Sound Created", icon_url=audit.user_avatar)
        e.set_thumbnail(url=audit.user_display_avatar)
        e.description = f"{audit.user_mention} added {sound.emoji or '🔊'} **{sound.name}**"
        self.add_to_queue(
            guild_id,
            "soundboard_create",
            embed=e,
            actor=audit.user or sound.user,
            target=sound,
            target_label="Sound",
            reason=audit.reason,
            details=self.soundboard_details(sound),
            links=[("Open sound", sound.url, "🔊")],
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @commands.Cog.listener()
    async def on_soundboard_sound_update(self, before, after):
        guild_id = str(after.guild.id)
        if guild_id not in self.config:
            return
        audit = await AuditLogSearch(
            after.guild, Action.soundboard_sound_update, target=after
        )
        changes = {}
        for attribute, label in (
            ("name", "🪧 Name"),
            ("emoji", "😀 Emoji"),
            ("volume", "🔊 Volume"),
            ("available", "✅ Available"),
        ):
            before_value = getattr(before, attribute, None)
            after_value = getattr(after, attribute, None)
            if before_value != after_value:
                if attribute == "volume":
                    changes[label] = f"{before_value:.0%} → {after_value:.0%}"
                else:
                    changes[label] = f"{before_value or 'Not set'} → {after_value or 'Not set'}"
        if not changes:
            return
        e = Embed(color=orange)
        e.set_author(name="Soundboard Sound Updated", icon_url=audit.user_avatar)
        e.set_thumbnail(url=audit.user_display_avatar)
        e.description = f"{audit.user_mention} updated {after.emoji or '🔊'} **{after.name}**"
        self.add_to_queue(
            guild_id,
            "soundboard_update",
            embed=e,
            actor=audit.user,
            target=after,
            target_label="Sound",
            reason=audit.reason,
            details=changes,
            links=[("Open sound", after.url, "🔊")],
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    @commands.Cog.listener()
    async def on_soundboard_sound_delete(self, sound):
        guild_id = str(sound.guild.id)
        if guild_id not in self.config:
            return
        audit = await AuditLogSearch(
            sound.guild, Action.soundboard_sound_delete, target=sound
        )
        e = Embed(color=red)
        e.set_author(name="Soundboard Sound Deleted", icon_url=audit.user_avatar)
        e.set_thumbnail(url=audit.user_display_avatar)
        e.description = f"{audit.user_mention} deleted {sound.emoji or '🔊'} **{sound.name}**"
        self.add_to_queue(
            guild_id,
            "soundboard_delete",
            embed=e,
            actor=audit.user,
            target=sound,
            target_label="Deleted Sound",
            reason=audit.reason,
            details=self.soundboard_details(sound),
            created_at=(audit.created_at.timestamp() if audit.created_at else None),
        )

    # Special Events

    async def on_message_filter(self, msg, flags):
        e = Embed(color=white)
        e.set_author(name="Message Filtered", icon_url=get_avatar(msg.author))
        e.set_thumbnail(url=msg.author.display_avatar.url)
        e.description = chain([
            f"{msg.author.mention}'s message matched the chat filter",
            f"Flags: `{', '.join(flags)}`",
            f"**[Jump URL]({msg.jump_url})**"
        ])
        e.description += f"\n\n>>> {utils.escape_markdown(msg.content)}"
        files = []
        for attachment in msg.attachments[:10]:
            if attachment.size > msg.guild.filesize_limit:
                continue
            with suppress(NotFound, Forbidden, HTTPException, ClientOSError):
                files.append(await attachment.to_file(use_cached=True))
        self.add_to_queue(
            str(msg.guild.id),
            "chat_filter",
            embed=e,
            files=files,
            target=msg.author,
            target_label="Member",
            channel=msg.channel,
            details={
                **self.message_log_details(msg),
                "🚩 Matched Flags": flags,
            },
            links=[("Open message", msg.jump_url, "↗️")],
            created_at=msg.created_at.timestamp(),
        )

    async def on_mute(self, ctx: Context, user: Union[User, Member], duration: str, reason: str):
        e = Embed(color=blue)
        e.set_author(name="User Muted", icon_url=get_avatar(user))
        e.set_thumbnail(url=user.display_avatar.url)
        summary = [
            f"{user.mention} was muted",
            f"By {ctx.author.mention}",
        ]
        if self.has_log_value(duration):
            summary.append(f"Duration: `{duration}`")
        if self.has_log_value(reason):
            summary.append(f"Reason: `{reason}`")
        summary.append(f"**[Jump URL]({ctx.message.jump_url})**")
        e.description = chain(summary)
        self.add_to_queue(
            str(ctx.guild.id),
            "mute",
            embed=e,
            actor=ctx.author,
            target=user,
            target_label="Member",
            channel=ctx.channel,
            reason=reason,
            links=[("Open command", ctx.message.jump_url, "↗️")],
            created_at=ctx.message.created_at.timestamp(),
        )

    async def on_unmute(self, ctx: Context, user: Union[User, Member]):
        e = Embed(color=blue)
        e.set_author(name="User Unmuted", icon_url=get_avatar(user))
        e.set_thumbnail(url=user.display_avatar.url)
        e.description = chain(
            f"{user.mention} was unmuted\n"
            f"By {ctx.author.mention}\n"
            f"**[Jump URL]({ctx.message.jump_url})**"
        )
        self.add_to_queue(
            str(ctx.guild.id),
            "unmute",
            embed=e,
            actor=ctx.author,
            target=user,
            target_label="Member",
            channel=ctx.channel,
            links=[("Open command", ctx.message.jump_url, "↗️")],
            created_at=ctx.message.created_at.timestamp(),
        )

    async def on_warn(self, ctx: Context, user: Union[User, Member], reason: str, total_warns: int):
        e = Embed(color=orange)
        e.set_author(name="User Warned", icon_url=get_avatar(user))
        e.set_thumbnail(url=user.display_avatar.url)
        summary = [
            f"{user.mention} was warned",
            f"By {ctx.author.mention}",
        ]
        if self.has_log_value(reason):
            summary.append(f"Reason: `{reason}`")
        if self.has_log_value(total_warns):
            summary.append(f"Total warnings: `{total_warns}`")
        e.description = chain(summary)
        self.add_to_queue(
            str(ctx.guild.id),
            "warn",
            embed=e,
            actor=ctx.author,
            target=user,
            target_label="Member",
            channel=ctx.channel,
            reason=reason,
            links=[("Open command", ctx.message.jump_url, "↗️")],
            created_at=ctx.message.created_at.timestamp(),
        )


async def setup(bot):
    await bot.add_cog(Logger(bot), override=True)
