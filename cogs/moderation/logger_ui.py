"""Interactive control center for Fate's server activity logger."""

from __future__ import annotations

import asyncio
import math
import re
import traceback
from contextlib import suppress
from copy import deepcopy
from time import monotonic
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord import ui
from discord.ext import commands

from botutils import colors
from botutils.log_archive import (
    MAX_ATTACHMENT_BYTES,
    MAX_GUILD_BYTES,
    MAX_RETENTION_DAYS,
)

if TYPE_CHECKING:
    from cogs.moderation.logger import Logger


LOGGER_PAGES = {
    "overview": ("Overview", "🏠", "See the whole logger at a glance"),
    "setup": ("Setup", "⚙️", "Enable logging and choose its main channel"),
    "events": ("Events", "🧭", "Choose exactly which activity Fate records"),
    "routing": ("Routing", "↪️", "Send individual events to other channels"),
    "filters": ("Filters", "🔕", "Ignore noisy channels and bot accounts"),
    "appearance": ("Appearance", "🎨", "Choose colors for logger embeds"),
    "history": ("Local history", "🗄️", "Manage private searchable history"),
    "health": ("Health", "🩺", "Inspect delivery and worker health"),
    "commands": ("Commands", "⌨️", "Browse every logger command"),
}

THEMES = {
    "default": (None, "Default", "Use Fate's event colors", "🎨"),
    "role": ("Role Color", "Role color", "Use the related member's role color", "👤"),
    "rgb": ("RGB", "Changing colors", "Cycle through a rainbow", "🌈"),
    "solid": ("Solid Color", "Solid color", "Use one color for every event", "🟦"),
    "custom": ("Custom", "Per-event colors", "Choose colors event by event", "🖌️"),
}

COMMAND_PAGE_SIZE = 8
MAX_EVENT_SEARCH_RESULTS = 25


def friendly_event_name(event: str) -> str:
    """Turn a stored event key into a compact user-facing name."""
    return event.replace("_", " ").title()


def normalize_event_query(value: str) -> str:
    return re.sub(r"[\s-]+", "_", value.strip().casefold()).strip("_")


def search_logger_events(categories: dict[str, list[str]], query: str) -> list[str]:
    """Return logger event keys ordered by match quality."""
    normalized = normalize_event_query(query)
    words = query.strip().casefold()
    query_tokens = re.findall(r"[a-z0-9]+", words)
    if not normalized:
        return []

    def related(left: str, right: str) -> bool:
        return left == right or (
            min(len(left), len(right)) >= 4
            and (left.startswith(right) or right.startswith(left))
        )

    matches = []
    for category, events in categories.items():
        category_text = category.casefold()
        for event in events:
            friendly = friendly_event_name(event).casefold()
            if normalized == event:
                score = 0
            elif event.startswith(normalized):
                score = 1
            elif normalized in event:
                score = 2
            elif words and words in friendly:
                score = 3
            elif words and words in category_text:
                score = 4
            elif query_tokens:
                candidate_tokens = re.findall(
                    r"[a-z0-9]+",
                    f"{event} {friendly} {category_text}",
                )
                if all(
                    any(related(token, candidate) for candidate in candidate_tokens)
                    for token in query_tokens
                ):
                    score = 5
                else:
                    continue
            else:
                continue
            matches.append((score, event))
    return [event for _score, event in sorted(set(matches))]


def parse_logger_color(value: str) -> int:
    """Parse a Discord color from hex or RGB text."""
    raw = value.strip()
    if not raw:
        raise ValueError(
            "Enter a hex color such as #5865F2 or RGB such as 88, 101, 242."
        )

    rgb_match = re.fullmatch(
        r"(?:rgb\s*\()?\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*\)?",
        raw,
        flags=re.IGNORECASE,
    )
    if rgb_match:
        channels = tuple(int(channel) for channel in rgb_match.groups())
        if any(channel > 255 for channel in channels):
            raise ValueError("Each RGB value must be between 0 and 255.")
        return discord.Color.from_rgb(*channels).value

    compact = raw.removeprefix("#").removeprefix("0x").removeprefix("0X")
    if re.fullmatch(r"[0-9a-fA-F]{3}", compact):
        compact = "".join(character * 2 for character in compact)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", compact):
        raise ValueError("Use a 3- or 6-digit hex color, or three RGB values.")
    return int(compact, 16)


def logger_command_usage(command: commands.Command, prefix: str) -> str:
    invocation = f"{prefix}{command.qualified_name}"
    if command.signature:
        invocation += f" {command.signature}"
    return invocation


class LoggerMenu(ui.View):
    """A readable, navigable, and fully interactive logger dashboard."""

    auto_refresh_interval = 5

    def __init__(
        self,
        cog: "Logger",
        ctx: commands.Context,
        *,
        initial_page: str = "overview",
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.bot = cog.bot
        self.ctx = ctx
        self.guild = ctx.guild
        self.user = ctx.author
        self.message: Optional[discord.Message] = None
        self.page = initial_page if initial_page in LOGGER_PAGES else "overview"
        self.notice: Optional[str] = None

        self.event_category = next(iter(cog.categories))
        self.selected_event = cog.categories[self.event_category][0]
        self.event_search_results: Optional[list[str]] = None

        self.command_page = 0
        self.selected_command: Optional[commands.Command] = None
        self.command_results: Optional[list[commands.Command]] = None
        self.command_query: Optional[str] = None
        self.history_stats: Optional[dict[str, Any]] = None
        self.embed = discord.Embed()
        self._rendered_state = None
        self._edit_lock = asyncio.Lock()
        self._refresh_task: Optional[asyncio.Task] = None
        self._last_activity = monotonic()

    @property
    def guild_id(self) -> str:
        return str(self.guild.id)

    @property
    def config(self) -> Optional[dict]:
        return self.cog.config.get(self.guild_id)

    @property
    def enabled(self) -> bool:
        return self.config is not None

    @property
    def prefix(self) -> str:
        return str(
            getattr(self.ctx, "clean_prefix", None)
            or getattr(self.ctx, "prefix", None)
            or "."
        )

    @property
    def can_manage(self) -> bool:
        permissions = getattr(self.user, "guild_permissions", None)
        if not getattr(permissions, "manage_channels", False):
            return False
        config = self.config
        return (
            not config
            or not config.get("secure")
            or self.user.id == self.guild.owner_id
        )

    @property
    def is_owner(self) -> bool:
        return self.user.id == self.guild.owner_id

    @property
    def theme_color(self) -> discord.Color:
        value = self.bot.config.get("theme_color", colors.fate)
        return value if isinstance(value, discord.Color) else discord.Color(value)

    @staticmethod
    def snowflake_id(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def color_label(value: Any) -> str:
        try:
            color = int(value)
        except (TypeError, ValueError):
            return "Invalid saved color"
        if not 0 <= color <= 0xFFFFFF:
            return "Invalid saved color"
        return f"`#{color:06X}`"

    def channel_for(self, channel_id: Any):
        channel_id = self.snowflake_id(channel_id)
        if channel_id is None:
            return None
        return self.guild.get_channel(channel_id) or self.guild.get_thread(channel_id)

    def resolve_selected_channel(self, channel: Any):
        if callable(getattr(channel, "permissions_for", None)):
            return channel
        return self.channel_for(getattr(channel, "id", channel))

    def user_for(self, user_id: Any):
        user_id = self.snowflake_id(user_id)
        if user_id is None:
            return None
        return self.guild.get_member(user_id) or self.bot.get_user(user_id)

    def channel_label(
        self, channel_id: Any, *, fallback: str = "Not configured"
    ) -> str:
        if not channel_id:
            return fallback
        channel = self.channel_for(channel_id)
        return channel.mention if channel else f"Missing channel (`{channel_id}`)"

    def event_category_for(self, event: str) -> str:
        for category, events in self.cog.categories.items():
            if event in events:
                return category
        return "Other"

    def event_destination(self, event: str) -> str:
        config = self.config or {}
        destination = config.get("channels", {}).get(event)
        if destination:
            return self.channel_label(destination, fallback="Missing route")
        return self.channel_label(config.get("channel"), fallback="Primary channel")

    def current_events(self) -> list[str]:
        if self.event_search_results is not None:
            return self.event_search_results
        return list(self.cog.categories[self.event_category])

    def logger_commands(self) -> list[commands.Command]:
        command_group = self.cog.logger
        return sorted(
            command_group.walk_commands(), key=lambda command: command.qualified_name
        )

    def visible_commands(self) -> list[commands.Command]:
        return (
            self.command_results
            if self.command_results is not None
            else self.logger_commands()
        )

    @property
    def command_page_count(self) -> int:
        return max(1, math.ceil(len(self.visible_commands()) / COMMAND_PAGE_SIZE))

    def current_commands(self) -> list[commands.Command]:
        start = self.command_page * COMMAND_PAGE_SIZE
        return self.visible_commands()[start : start + COMMAND_PAGE_SIZE]

    def search_commands(self, query: str) -> list[commands.Command]:
        normalized = query.strip().casefold()
        prefix = self.prefix.casefold()
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :].strip()
        if not normalized:
            return []
        matches = []
        for command in self.logger_commands():
            name = command.qualified_name.casefold()
            aliases = [alias.casefold() for alias in command.aliases]
            description = (command.description or command.short_doc or "").casefold()
            short_name = command.name.casefold()
            if normalized == name or normalized == short_name or normalized in aliases:
                score = 0
            elif name.startswith(normalized) or short_name.startswith(normalized):
                score = 1
            elif normalized in name:
                score = 2
            elif any(normalized in alias for alias in aliases):
                score = 3
            elif normalized in description:
                score = 4
            else:
                continue
            matches.append((score, name, command))
        return [command for _score, _name, command in sorted(matches)]

    async def start(self) -> "LoggerMenu":
        await self.refresh()
        self.message = await self.ctx.send(embed=self.embed, view=self)
        self._last_activity = monotonic()
        self._refresh_task = asyncio.create_task(self.auto_refresh())
        return self

    def stop(self) -> None:
        super().stop()
        if self._refresh_task and not self._refresh_task.done():
            if self._refresh_task is not asyncio.current_task():
                self._refresh_task.cancel()

    async def auto_refresh(self) -> None:
        while not self.is_finished():
            await asyncio.sleep(self.auto_refresh_interval)
            # Message edits re-register Discord views and reset their timeout.
            # Automatic updates must not keep an abandoned menu alive forever.
            if (
                self.timeout is not None
                and monotonic() - self._last_activity >= self.timeout
            ):
                await self.on_timeout()
                return
            if self.bot.get_cog(self.cog.qualified_name) is not self.cog:
                self.stop()
                return
            try:
                await self.refresh_message_if_changed()
            except (discord.NotFound, discord.Forbidden):
                self.stop()
                return
            except Exception as error:
                self.cog.bot.log.critical(
                    f"Could not auto-refresh logger menu for guild {self.guild_id}: {error}"
                )

    async def refresh_message_if_changed(self) -> None:
        async with self._edit_lock:
            if self.is_finished() or self.message is None:
                return
            if await self.refresh(only_if_changed=True):
                try:
                    await self.message.edit(embed=self.embed, view=self)
                except Exception:
                    # Retry the same state if Discord did not accept the edit.
                    self._rendered_state = None
                    raise

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user.id:
            self._last_activity = monotonic()
            return True
        await interaction.response.send_message(
            "This logger menu belongs to someone else.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        self.stop()
        async with self._edit_lock:
            if self.message:
                with suppress(discord.HTTPException):
                    await self.message.edit(view=None)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: ui.Item,
    ) -> None:
        self.cog.bot.log.critical(
            "Logger menu error:\n"
            f"{''.join(traceback.format_exception(type(error), error, error.__traceback__))}"
        )
        await self.send_ephemeral(
            interaction,
            "I couldn't update the logger menu. The error has been recorded.",
        )

    async def send_ephemeral(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def defer_interaction(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()

    async def authorize(
        self,
        interaction: discord.Interaction,
        *,
        owner_only: bool = False,
    ) -> bool:
        if owner_only and interaction.user.id != self.guild.owner_id:
            await self.send_ephemeral(
                interaction,
                "Only the server owner can change that logger setting.",
            )
            return False
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not getattr(permissions, "manage_channels", False):
            await self.send_ephemeral(
                interaction,
                "You need Manage Channels permission to change logger settings.",
            )
            return False
        config = self.config
        if (
            config
            and config.get("secure")
            and interaction.user.id != self.guild.owner_id
        ):
            await self.send_ephemeral(
                interaction,
                "Only the server owner can change this logger while security is enabled.",
            )
            return False
        return True

    async def refresh(self, *, only_if_changed: bool = False) -> bool:
        self.history_stats = None
        if self.page == "history" and self.enabled:
            try:
                await self.cog.local_archive.flush()
                self.history_stats = await self.cog.local_archive.stats(self.guild_id)
            except Exception as error:
                self.cog.bot.log.critical(
                    f"Could not read local logger history stats for guild {self.guild_id}: {error}"
                )
        embed = self.build_embed()
        state = (self.config, embed.to_dict(), self.can_manage)
        if only_if_changed and state == self._rendered_state:
            return False
        self.rebuild()
        self._rendered_state = deepcopy(state)
        return True

    async def redraw(self, interaction: discord.Interaction) -> None:
        await self.defer_interaction(interaction)
        self._last_activity = monotonic()
        async with self._edit_lock:
            if self.is_finished():
                return
            await self.refresh()
            try:
                await interaction.edit_original_response(embed=self.embed, view=self)
            except Exception:
                self._rendered_state = None
                raise

    def base_embed(self, title: str, description: str) -> discord.Embed:
        embed = discord.Embed(
            title=title, description=description, color=self.theme_color
        )
        icon = getattr(self.guild, "icon", None)
        bot_user = getattr(self.bot, "user", None)
        bot_avatar = getattr(getattr(bot_user, "display_avatar", None), "url", None)
        embed.set_author(
            name=self.guild.name,
            icon_url=str(icon.url) if icon else bot_avatar,
        )
        if bot_avatar:
            embed.set_thumbnail(url=bot_avatar)
        if self.notice:
            embed.add_field(name="✨ Updated", value=self.notice[:1024], inline=False)
        return embed

    def build_overview_embed(self) -> discord.Embed:
        embed = self.base_embed(
            "Fate Logger",
            (
                f"Browse **{len(self.cog.log_types)} event types** and configure each part "
                "of logging without leaving this menu."
            ),
        )
        config = self.config
        if config:
            snapshot = self.cog.get_worker_snapshot(self.guild_id)
            status_icons = {
                "healthy": "🟢",
                "degraded": "🟠",
                "blocked": "🟡",
                "down": "🔴",
            }
            disabled = set(config.get("disabled", []))
            enabled_events = sum(event not in disabled for event in self.cog.log_types)
            summary = (
                f"{status_icons.get(snapshot['status'], '⚪')} **{snapshot['status'].title()}**\n"
                f"> Destination: {self.channel_label(config.get('channel'))}\n"
                f"> Event Types: `{enabled_events}/{len(self.cog.log_types)}` enabled\n"
                f"> Channel Redirects: `{len(config.get('channels', {}))}`\n"
                f"> Security: `{'On' if config.get('secure') else 'Off'}` · "
                f"Local history: `{'On' if self.cog.archive_enabled(self.guild_id) else 'Off'}`"
            )
        else:
            summary = (
                "⚪ **Not configured**\n"
                "> Open **Setup** to choose an existing channel or let Fate create one."
            )
        embed.add_field(name="Current setup", value=summary, inline=False)
        embed.set_footer(
            text="Choose a section below · Changes save immediately"
        )
        return embed

    def build_setup_embed(self) -> discord.Embed:
        config = self.config
        embed = self.base_embed(
            "Logger › Setup",
            "Turn the logger on, choose its main destination, and protect its settings.",
        )
        if not config:
            embed.add_field(name="Status", value="⚪ Disabled", inline=True)
            embed.add_field(name="Primary channel", value="Not configured", inline=True)
            embed.add_field(name="Security", value="Available after setup", inline=True)
            embed.add_field(
                name="Get started",
                value=(
                    "Choose a channel below to enable logging there, or select "
                    "**Create channel & enable**. Fate needs Send Messages, Embed Links, "
                    "and Attach Files in the destination."
                ),
                inline=False,
            )
        else:
            snapshot = self.cog.get_worker_snapshot(self.guild_id)
            embed.add_field(
                name="Status",
                value=f"🟢 Enabled · `{snapshot['status'].title()}`",
                inline=True,
            )
            embed.add_field(
                name="Primary channel",
                value=self.channel_label(config.get("channel")),
                inline=True,
            )
            embed.add_field(
                name="Security",
                value="🔒 Owner only" if config.get("secure") else "🔓 Manage Channels",
                inline=True,
            )
            embed.add_field(
                name="What happens when disabled?",
                value=(
                    "Fate stops the worker and leaves Discord channels intact. "
                    "Locally saved history is not erased; use Local history to clear it first."
                ),
                inline=False,
            )
        embed.set_footer(
            text="Choose a primary channel below · Changes save immediately"
        )
        return embed

    def build_events_embed(self) -> discord.Embed:
        if self.event_search_results is not None:
            heading = "Search results"
            description = (
                f"Showing {len(self.event_search_results)} match(es). Choose a category "
                "to leave search results."
            )
        else:
            heading = self.event_category
            description = (
                "Enable or disable one event—or update the whole category at once."
            )
        embed = self.base_embed(f"Logger › Events › {heading}", description)
        config = self.config
        disabled = set(config.get("disabled", [])) if config else set()
        lines = []
        for event in self.current_events():
            state = "🟢" if config and event not in disabled else "⚪"
            route = config and config.get("channels", {}).get(event)
            suffix = f" · ↪ {self.channel_label(route)}" if route else ""
            lines.append(f"{state} `{event}` — {friendly_event_name(event)}{suffix}")
        embed.add_field(
            name=f"{heading} ({len(lines)})",
            value="\n".join(lines) or "No events matched.",
            inline=False,
        )
        selected = self.selected_event
        if selected in self.current_events():
            selected_state = (
                "Enabled" if config and selected not in disabled else "Disabled"
            )
            embed.add_field(
                name=f"Selected · {friendly_event_name(selected)}",
                value=(
                    f"Key: `{selected}`\n"
                    f"Status: **{selected_state}**\n"
                    f"Destination: {self.event_destination(selected) if config else 'Logger disabled'}"
                ),
                inline=False,
            )
        if not config:
            embed.add_field(
                name="Logger disabled",
                value="You can browse events now; open Setup before changing them.",
                inline=False,
            )
        embed.set_footer(
            text="Green events are enabled · Search accepts names or categories"
        )
        return embed

    def build_routing_embed(self) -> discord.Embed:
        embed = self.base_embed(
            f"Logger › Routing › {self.event_category}",
            "Choose an event, then choose where Fate should send it.",
        )
        config = self.config
        if not config:
            embed.add_field(
                name="Logger disabled",
                value="Open Setup and choose a primary channel before adding routes.",
                inline=False,
            )
            return embed
        routes = config.get("channels", {})
        lines = []
        for event in self.cog.categories[self.event_category]:
            destination = routes.get(event)
            label = (
                self.channel_label(destination) if destination else "Primary channel"
            )
            marker = "↪️" if destination else "•"
            lines.append(f"{marker} `{event}` → {label}")
        embed.add_field(
            name=f"{self.event_category} destinations",
            value="\n".join(lines),
            inline=False,
        )
        embed.add_field(
            name=f"Selected · {friendly_event_name(self.selected_event)}",
            value=(
                f"Current destination: {self.event_destination(self.selected_event)}\n"
                f"Primary fallback: {self.channel_label(config.get('channel'))}"
            ),
            inline=False,
        )
        embed.set_footer(
            text=f"{len(routes)} custom route(s) · Choosing the primary channel clears a custom route"
        )
        return embed

    def build_filters_embed(self) -> discord.Embed:
        embed = self.base_embed(
            "Logger › Filters",
            "Reduce routine message noise without disabling important server events.",
        )
        config = self.config
        if not config:
            embed.add_field(
                name="Logger disabled",
                value="Open Setup before adding ignored channels or bots.",
                inline=False,
            )
            return embed
        channel_ids = config.get("ignored_channels", [])
        channels = [self.channel_label(channel_id) for channel_id in channel_ids]
        bot_labels = []
        for user_id in config.get("ignored_bots", []):
            member = self.user_for(user_id)
            bot_labels.append(
                member.mention if member else f"Unavailable bot (`{user_id}`)"
            )
        embed.add_field(
            name=f"Ignored channels · {len(channel_ids)}",
            value="\n".join(channels[:15]) if channels else "None",
            inline=True,
        )
        embed.add_field(
            name=f"Ignored bots · {len(bot_labels)}",
            value="\n".join(bot_labels[:15]) if bot_labels else "None",
            inline=True,
        )
        embed.add_field(
            name="What is filtered?",
            value=(
                "Channel filters suppress chat-heavy records such as edits, deletions, "
                "reactions, typing, and message snapshots. Bot filters suppress records "
                "created by the selected bot accounts."
            ),
            inline=False,
        )
        embed.set_footer(
            text="Select up to 25 entries at once · Clear all removes every filter"
        )
        return embed

    def build_appearance_embed(self) -> discord.Embed:
        embed = self.base_embed(
            "Logger › Appearance",
            "Choose a consistent visual theme or give individual events their own colors.",
        )
        config = self.config
        if not config:
            embed.add_field(
                name="Logger disabled",
                value="Open Setup before changing logger colors.",
                inline=False,
            )
            return embed
        theme = config.get("theme")
        current = next(
            (
                label
                for value, label, _description, _emoji in THEMES.values()
                if value == theme
            ),
            str(theme or "Default"),
        )
        color_value = config.get("color")
        solid = self.color_label(color_value) if color_value is not None else "Not set"
        custom = config.get("colors", {})
        embed.add_field(name="Current theme", value=f"**{current}**", inline=True)
        embed.add_field(name="Solid color", value=solid, inline=True)
        embed.add_field(
            name="Custom event colors", value=f"`{len(custom)}`", inline=True
        )
        explanations = "\n".join(
            f"{emoji} **{label}** — {description}"
            for _value, label, description, emoji in THEMES.values()
        )
        embed.add_field(name="Themes", value=explanations, inline=False)
        if custom:
            custom_lines = [
                f"`{event}` → {self.color_label(value)}"
                for event, value in sorted(custom.items())[:15]
            ]
            embed.add_field(
                name="Saved per-event colors",
                value="\n".join(custom_lines),
                inline=False,
            )
        embed.set_footer(
            text="Selecting Solid color opens a color editor · Saved colors are reusable"
        )
        return embed

    def build_history_embed(self) -> discord.Embed:
        embed = self.base_embed(
            "Logger › Local history",
            (
                "Keep an optional private, searchable copy on Fate's host for recovery and "
                "moderation review. Nothing is stored here until you turn it on."
            ),
        )
        config = self.config
        if not config:
            embed.add_field(
                name="Logger disabled",
                value="Open Setup before configuring local history.",
                inline=False,
            )
            return embed
        archive = self.cog.archive_config(self.guild_id)
        embed.add_field(
            name="Collection",
            value=(
                f"Status: **{'On' if archive.get('enabled') else 'Off'}**\n"
                f"Keep records: `{archive.get('retention_days')} days`\n"
                f"Recover uncached messages: `{'On' if archive.get('cache_messages') else 'Off'}`\n"
                f"Store attachment files: `{'On' if archive.get('store_attachments') else 'Off'}`\n"
                f"Per-file limit: `{archive.get('attachment_size_limit_mb')} MB`"
            ),
            inline=True,
        )
        if self.history_stats:
            used = self.history_stats.get("stored_bytes", 0)
            embed.add_field(
                name="Stored now",
                value=(
                    f"Events: `{self.history_stats.get('log_count', 0)}`\n"
                    f"Message snapshots: `{self.history_stats.get('message_count', 0)}`\n"
                    f"Files: `{self.history_stats.get('file_count', 0)}`\n"
                    f"Space: `{self.cog.format_storage_size(used)} / "
                    f"{self.cog.format_storage_size(MAX_GUILD_BYTES)}`"
                ),
                inline=True,
            )
        else:
            embed.add_field(
                name="Stored now",
                value="Statistics are temporarily unavailable.",
                inline=True,
            )
        embed.add_field(
            name="Retention behavior",
            value=(
                "Turning collection off preserves existing history. Lowering retention prunes "
                "older records immediately. At the storage limit, the oldest data is removed first."
            ),
            inline=False,
        )
        embed.set_footer(
            text="Search results are sent by DM · Clear requires typed confirmation"
        )
        return embed

    def build_health_embed(self) -> discord.Embed:
        embed = self.base_embed(
            "Logger › Health",
            "A live view of this server's delivery worker and queue.",
        )
        if not self.enabled:
            embed.add_field(name="Status", value="⚪ Logger disabled", inline=False)
            embed.set_footer(text="Open Setup to enable the logger")
            return embed
        snapshot = self.cog.get_worker_snapshot(self.guild_id)
        icons = {"healthy": "🟢", "degraded": "🟠", "blocked": "🟡", "down": "🔴"}
        embed.color = {
            "healthy": colors.green,
            "degraded": colors.orange,
            "blocked": colors.yellow,
            "down": colors.red,
        }.get(snapshot["status"], self.theme_color)
        embed.add_field(
            name="Status",
            value=f"{icons.get(snapshot['status'], '⚪')} **{snapshot['status'].title()}**",
            inline=True,
        )
        embed.add_field(
            name="Queue",
            value=(
                f"`{snapshot['queue_size']}/{snapshot['queue_capacity']}` queued\n"
                f"`{snapshot['queue_ratio']:.0%}` full"
            ),
            inline=True,
        )
        embed.add_field(
            name="Worker",
            value=(
                f"`{'Running' if snapshot['worker_running'] else 'Stopped'}`\n"
                f"Restarts: `{snapshot['restarts']}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Delivery",
            value=(
                f"Sent: `{snapshot['sent']}`\n"
                f"Failed: `{snapshot['failed']}`\n"
                f"Dropped: `{snapshot['dropped']}`"
            ),
            inline=True,
        )
        blocked = ", ".join(snapshot["blocked_permissions"]) or "None"
        embed.add_field(
            name="Waiting for permissions", value=f"`{blocked}`", inline=True
        )
        embed.add_field(
            name="Destination",
            value=self.channel_label(self.config.get("channel")),
            inline=True,
        )
        embed.add_field(
            name="Recent activity",
            value=(
                f"Worker started: {self.cog.health_timestamp(snapshot['worker_started_at'])}\n"
                f"Last delivered: {self.cog.health_timestamp(snapshot['last_success'])}\n"
                f"Last failure: {self.cog.health_timestamp(snapshot['last_error_at'])}"
            ),
            inline=False,
        )
        if snapshot["last_error"]:
            safe_error = str(snapshot["last_error"]).replace("`", "ˋ")
            embed.add_field(name="Last error", value=safe_error[:1024], inline=False)
        embed.set_footer(
            text="Use Refresh for a new snapshot · Counters reset when the cog reloads"
        )
        return embed

    def command_aliases(self, command: commands.Command) -> str:
        if not command.aliases:
            return "None"
        parent = f"{command.parent.qualified_name} " if command.parent else ""
        return ", ".join(f"`{self.prefix}{parent}{alias}`" for alias in command.aliases)

    def build_commands_embed(self) -> discord.Embed:
        embed = self.base_embed(
            "Logger › Commands",
            "Browse command shortcuts or search by name and purpose.",
        )
        if self.selected_command:
            command = self.selected_command
            embed.title = f"{self.prefix}{command.qualified_name}"
            embed.description = (
                command.description or command.short_doc or "Logger command"
            )
            embed.add_field(
                name="Usage",
                value=f"`{logger_command_usage(command, self.prefix)}`"[:1024],
                inline=False,
            )
            embed.add_field(
                name="Aliases",
                value=self.command_aliases(command)[:1024],
                inline=False,
            )
            embed.set_footer(text="Use Back to return to the command list")
            return embed

        command_list = self.current_commands()
        embed.description = (
            f"Search results for **{self.command_query}**"
            if self.command_query
            else f"All {len(self.logger_commands())} logger commands"
        )
        embed.add_field(
            name="Commands",
            value="\n".join(
                f"`{self.prefix}{command.qualified_name}`\n"
                f"{command.description or command.short_doc or 'Logger command'}"
                for command in command_list
            )
            or "No commands matched.",
            inline=False,
        )
        embed.set_footer(
            text=f"Page {self.command_page + 1}/{self.command_page_count} · Select a command for details"
        )
        return embed

    def build_embed(self) -> discord.Embed:
        builders = {
            "overview": self.build_overview_embed,
            "setup": self.build_setup_embed,
            "events": self.build_events_embed,
            "routing": self.build_routing_embed,
            "filters": self.build_filters_embed,
            "appearance": self.build_appearance_embed,
            "history": self.build_history_embed,
            "health": self.build_health_embed,
            "commands": self.build_commands_embed,
        }
        return builders[self.page]()

    def add_navigation(self, *extra: ui.Button) -> None:
        for item in extra:
            self.add_item(item)
        self.add_item(
            LoggerButton(
                self,
                "home",
                "Home",
                "🏠",
                row=4,
                disabled=self.page == "overview",
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "close",
                "Close",
                "✖️",
                row=4,
                style=discord.ButtonStyle.danger,
            )
        )

    def rebuild(self) -> None:
        self.embed = self.build_embed()
        self.clear_items()
        self.add_item(LoggerPageSelect(self))

        if self.page == "setup":
            self.add_setup_items()
        elif self.page == "events":
            self.add_event_items()
        elif self.page == "routing":
            self.add_routing_items()
        elif self.page == "filters":
            self.add_filter_items()
        elif self.page == "appearance":
            self.add_appearance_items()
        elif self.page == "history":
            self.add_history_items()
        elif self.page == "commands":
            self.add_command_items()

        if self.page not in {"routing"}:
            self.add_navigation()

    def add_setup_items(self) -> None:
        self.add_item(LoggerPrimaryChannelSelect(self, disabled=not self.can_manage))
        if not self.enabled:
            self.add_item(
                LoggerButton(
                    self,
                    "create_logger_channel",
                    "Create channel & enable",
                    "✨",
                    row=2,
                    style=discord.ButtonStyle.success,
                    disabled=not self.can_manage,
                )
            )
        else:
            self.add_item(
                LoggerButton(
                    self,
                    "toggle_security",
                    "Disable security"
                    if self.config.get("secure")
                    else "Enable security",
                    "🔓" if self.config.get("secure") else "🔒",
                    row=2,
                    disabled=not self.is_owner,
                )
            )
            self.add_item(
                LoggerButton(
                    self,
                    "disable_logger",
                    "Disable logger",
                    "🛑",
                    row=2,
                    style=discord.ButtonStyle.danger,
                    disabled=not self.can_manage,
                )
            )

    def add_event_items(self) -> None:
        self.add_item(LoggerEventCategorySelect(self, page="events", row=1))
        self.add_item(LoggerEventSelect(self, row=2))
        self.add_item(
            LoggerButton(
                self,
                "toggle_event",
                "Toggle selected",
                "🔁",
                row=3,
                style=discord.ButtonStyle.primary,
                disabled=not self.enabled or not self.can_manage,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "enable_category",
                "Enable category",
                "✅",
                row=3,
                disabled=not self.enabled
                or not self.can_manage
                or self.event_search_results is not None,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "disable_category",
                "Disable category",
                "🚫",
                row=3,
                disabled=not self.enabled
                or not self.can_manage
                or self.event_search_results is not None,
            )
        )
        self.add_item(LoggerButton(self, "search_events", "Search", "🔎", row=3))

    def add_routing_items(self) -> None:
        self.add_item(LoggerEventCategorySelect(self, page="routing", row=1))
        self.add_item(LoggerEventSelect(self, row=2))
        self.add_item(
            LoggerRouteChannelSelect(
                self,
                disabled=not self.enabled or not self.can_manage,
            )
        )
        self.add_navigation(
            LoggerButton(
                self,
                "reset_route",
                "Use primary",
                "↩️",
                row=4,
                disabled=(
                    not self.enabled
                    or not self.can_manage
                    or self.selected_event not in self.config.get("channels", {})
                ),
            )
        )

    def add_filter_items(self) -> None:
        self.add_item(
            LoggerIgnoredChannelSelect(
                self,
                disabled=not self.enabled or not self.can_manage,
            )
        )
        self.add_item(
            LoggerIgnoredBotSelect(
                self,
                disabled=not self.enabled or not self.can_manage,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "clear_filters",
                "Clear all filters",
                "🧹",
                row=3,
                disabled=(
                    not self.enabled
                    or not self.can_manage
                    or not (
                        self.config.get("ignored_channels")
                        or self.config.get("ignored_bots")
                    )
                ),
            )
        )

    def add_appearance_items(self) -> None:
        self.add_item(
            LoggerThemeSelect(
                self,
                disabled=not self.enabled or not self.can_manage,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "change_solid_color",
                "Change solid color",
                "🟦",
                row=2,
                disabled=not self.enabled or not self.can_manage,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "set_event_color",
                "Set event color",
                "🖌️",
                row=2,
                disabled=not self.enabled or not self.can_manage,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "clear_custom_colors",
                "Clear event colors",
                "🧹",
                row=2,
                disabled=(
                    not self.enabled
                    or not self.can_manage
                    or not self.config.get("colors")
                ),
            )
        )

    def add_history_items(self) -> None:
        archive = self.cog.archive_config(self.guild_id) if self.enabled else {}
        disabled = not self.enabled or not self.can_manage
        self.add_item(
            LoggerButton(
                self,
                "toggle_history",
                "Turn history off" if archive.get("enabled") else "Turn history on",
                "⏹️" if archive.get("enabled") else "▶️",
                row=1,
                style=discord.ButtonStyle.danger
                if archive.get("enabled")
                else discord.ButtonStyle.success,
                disabled=disabled,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "toggle_recovery",
                "Message recovery",
                "💬",
                row=1,
                style=discord.ButtonStyle.primary
                if archive.get("cache_messages")
                else discord.ButtonStyle.secondary,
                disabled=disabled,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "toggle_files",
                "Attachment files",
                "📎",
                row=1,
                style=discord.ButtonStyle.primary
                if archive.get("store_attachments")
                else discord.ButtonStyle.secondary,
                disabled=disabled,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "history_limits",
                "Retention & limits",
                "✏️",
                row=2,
                disabled=disabled,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "search_history",
                "Search history",
                "🔎",
                row=2,
                disabled=disabled,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "clear_history",
                "Clear history",
                "🗑️",
                row=2,
                style=discord.ButtonStyle.danger,
                disabled=disabled
                or not self.history_stats
                or not (
                    self.history_stats.get("log_count")
                    or self.history_stats.get("message_count")
                    or self.history_stats.get("file_count")
                ),
            )
        )

    def add_command_items(self) -> None:
        if not self.selected_command and self.current_commands():
            self.add_item(LoggerCommandSelect(self))
        self.add_item(
            LoggerButton(
                self,
                "command_back",
                "Back",
                "↩️",
                row=2,
                disabled=self.selected_command is None,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "command_previous",
                "Previous",
                "◀️",
                row=2,
                disabled=self.selected_command is not None or self.command_page <= 0,
            )
        )
        self.add_item(
            LoggerButton(
                self,
                "command_next",
                "Next",
                "▶️",
                row=2,
                disabled=(
                    self.selected_command is not None
                    or self.command_page >= self.command_page_count - 1
                ),
            )
        )
        self.add_item(LoggerButton(self, "search_commands", "Search", "🔎", row=2))
        self.add_item(
            LoggerButton(
                self,
                "all_commands",
                "All",
                "📚",
                row=2,
                disabled=self.command_results is None,
            )
        )

    async def change_page(self, page: str, interaction: discord.Interaction) -> None:
        if page not in LOGGER_PAGES:
            return await self.send_ephemeral(
                interaction, "That logger section is unavailable."
            )
        self.page = page
        self.notice = None
        if page != "commands":
            self.selected_command = None
        await self.redraw(interaction)

    async def choose_event_category(
        self,
        category: str,
        interaction: discord.Interaction,
    ) -> None:
        if category not in self.cog.categories:
            return await self.send_ephemeral(
                interaction, "That event category is unavailable."
            )
        self.event_category = category
        self.event_search_results = None
        self.selected_event = self.cog.categories[category][0]
        await self.redraw(interaction)

    async def choose_event(self, event: str, interaction: discord.Interaction) -> None:
        if event not in self.cog.log_types:
            return await self.send_ephemeral(
                interaction, "That logger event is unavailable."
            )
        self.selected_event = event
        await self.redraw(interaction)

    async def set_primary_channel(
        self,
        channel: Any,
        interaction: discord.Interaction,
    ) -> None:
        if not await self.authorize(interaction):
            return
        channel = self.resolve_selected_channel(channel)
        if channel is None:
            return await self.send_ephemeral(
                interaction, "That channel is no longer available. Please choose another."
            )
        missing = self.missing_channel_permissions(channel)
        if missing:
            return await self.send_ephemeral(
                interaction,
                f"Fate still needs {', '.join(missing)} in {channel.mention}.",
            )
        await interaction.response.defer()
        was_enabled = self.enabled
        if was_enabled:
            self.config["channel"] = channel.id
            await self.cog.save_data()
            self.notice = f"Primary logging moved to {channel.mention}."
        else:
            await self.cog.enable_for_guild(self.guild.id, channel.id)
            await self.audit_module_state(True)
            self.notice = f"Logger enabled in {channel.mention}."
        await self.redraw(interaction)

    def missing_channel_permissions(self, channel) -> list[str]:
        member = getattr(self.guild, "me", None)
        if member is None:
            return ["bot membership information"]
        permissions = channel.permissions_for(member)
        labels = {
            "send_messages": "Send Messages",
            "embed_links": "Embed Links",
            "attach_files": "Attach Files",
        }
        return [
            label
            for permission, label in labels.items()
            if not getattr(permissions, permission, False)
        ]

    async def audit_module_state(self, enabled: bool) -> None:
        try:
            await self.bot.create_log(
                message=f"!{'on' if enabled else 'off'} **Logger** - `{self.guild}`",
                channel="module_log",
                embedded=True,
                color=colors.green if enabled else colors.red,
            )
        except Exception as error:
            self.cog.bot.log.critical(
                f"Could not publish logger module state for guild {self.guild_id}: {error}"
            )

    async def create_logger_channel(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if self.enabled:
            return await self.send_ephemeral(interaction, "Logger is already enabled.")
        await interaction.response.defer()
        try:
            channel = await self.guild.create_text_channel(
                name="discord-log",
                reason=f"Logger enabled by {interaction.user}",
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "Fate needs Manage Channels permission to create the log channel.",
                ephemeral=True,
            )
        except discord.HTTPException:
            return await interaction.followup.send(
                "Fate couldn't create the log channel. Please try again.",
                ephemeral=True,
            )
        missing = self.missing_channel_permissions(channel)
        if missing:
            return await interaction.followup.send(
                f"I created {channel.mention}, but Fate still needs "
                f"{', '.join(missing)} there before logging can be enabled.",
                ephemeral=True,
            )
        await self.cog.enable_for_guild(self.guild.id, channel.id)
        await self.audit_module_state(True)
        self.notice = f"Logger enabled in {channel.mention}."
        await self.redraw(interaction)

    async def disable_logger(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Logger is already disabled.")
        await interaction.response.defer()
        await self.cog.disable_for_guild(self.guild.id)
        await self.audit_module_state(False)
        self.notice = "Logger disabled. Discord channels and saved local history were left intact."
        await self.redraw(interaction)

    async def toggle_security(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction, owner_only=True):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        await self.defer_interaction(interaction)
        self.config["secure"] = not self.config.get("secure", False)
        await self.cog.save_data()
        self.notice = (
            "Security enabled; only the server owner can change logger settings."
            if self.config["secure"]
            else "Security disabled; members with Manage Channels can configure the logger."
        )
        await self.redraw(interaction)

    async def update_events(
        self, action: str, interaction: discord.Interaction
    ) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        await self.defer_interaction(interaction)
        disabled = self.config.setdefault("disabled", [])
        if action == "toggle_event":
            if self.selected_event in disabled:
                self.config["disabled"] = [
                    event for event in disabled if event != self.selected_event
                ]
                state = "enabled"
            else:
                disabled.append(self.selected_event)
                state = "disabled"
            self.notice = f"{friendly_event_name(self.selected_event)} is now {state}."
        else:
            events = set(self.cog.categories[self.event_category])
            if action == "enable_category":
                self.config["disabled"] = [
                    event for event in disabled if event not in events
                ]
                state = "enabled"
            else:
                for event in self.cog.categories[self.event_category]:
                    if event not in disabled:
                        disabled.append(event)
                state = "disabled"
            self.notice = f"{self.event_category} events are now {state}."
        await self.cog.save_data()
        await self.redraw(interaction)

    async def set_route(
        self,
        channel: Any,
        interaction: discord.Interaction,
    ) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        channel = self.resolve_selected_channel(channel)
        if channel is None:
            return await self.send_ephemeral(
                interaction, "That channel is no longer available. Please choose another."
            )
        missing = self.missing_channel_permissions(channel)
        if missing:
            return await self.send_ephemeral(
                interaction,
                f"Fate still needs {', '.join(missing)} in {channel.mention}.",
            )
        await self.defer_interaction(interaction)
        routes = self.config.setdefault("channels", {})
        if channel.id == self.config.get("channel"):
            routes.pop(self.selected_event, None)
            self.notice = f"{friendly_event_name(self.selected_event)} now uses the primary channel."
        else:
            routes[self.selected_event] = channel.id
            self.notice = f"{friendly_event_name(self.selected_event)} now goes to {channel.mention}."
        await self.cog.save_data()
        await self.redraw(interaction)

    async def reset_route(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        await self.defer_interaction(interaction)
        self.config.setdefault("channels", {}).pop(self.selected_event, None)
        await self.cog.save_data()
        self.notice = (
            f"{friendly_event_name(self.selected_event)} now uses the primary channel."
        )
        await self.redraw(interaction)

    async def set_ignored_channels(
        self,
        channels: list[discord.abc.GuildChannel],
        interaction: discord.Interaction,
    ) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        await self.defer_interaction(interaction)
        current = list(self.config.get("ignored_channels", []))
        represented = {channel.id for channel in self.default_ignored_channels()}
        preserved = [
            channel_id
            for channel_id in current
            if self.snowflake_id(channel_id) not in represented
        ]
        self.config["ignored_channels"] = list(
            dict.fromkeys(
                [
                    *preserved,
                    *(channel.id for channel in channels),
                ]
            )
        )
        await self.cog.save_data()
        self.notice = f"Now ignoring {len(self.config['ignored_channels'])} channel(s)."
        await self.redraw(interaction)

    def default_ignored_channels(self) -> list:
        if not self.enabled:
            return []
        channels = []
        for channel_id in self.config.get("ignored_channels", []):
            if channel := self.channel_for(channel_id):
                channels.append(channel)
            if len(channels) == 25:
                break
        return channels

    def default_ignored_bots(self) -> list:
        if not self.enabled:
            return []
        users = []
        for user_id in self.config.get("ignored_bots", []):
            user = self.user_for(user_id)
            if user:
                users.append(user)
            if len(users) == 25:
                break
        return users

    async def set_ignored_bots(
        self,
        users: list[discord.User],
        interaction: discord.Interaction,
    ) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        non_bots = [user for user in users if not getattr(user, "bot", False)]
        if non_bots:
            return await self.send_ephemeral(
                interaction,
                "Only bot accounts can be added to the ignored-bots filter.",
            )
        await self.defer_interaction(interaction)
        current = list(self.config.get("ignored_bots", []))
        represented = {user.id for user in self.default_ignored_bots()}
        preserved = [
            user_id
            for user_id in current
            if self.snowflake_id(user_id) not in represented
        ]
        self.config["ignored_bots"] = list(
            dict.fromkeys(
                [
                    *preserved,
                    *(user.id for user in users),
                ]
            )
        )
        await self.cog.save_data()
        self.notice = f"Now ignoring {len(self.config['ignored_bots'])} bot account(s)."
        await self.redraw(interaction)

    async def clear_filters(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        await self.defer_interaction(interaction)
        self.config["ignored_channels"] = []
        self.config["ignored_bots"] = []
        await self.cog.save_data()
        self.notice = "All logger channel and bot filters were cleared."
        await self.redraw(interaction)

    async def choose_theme(self, key: str, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        if key not in THEMES:
            return await self.send_ephemeral(
                interaction, "That logger theme is unavailable."
            )
        value, label, _description, _emoji = THEMES[key]
        if value == "Solid Color":
            return await interaction.response.send_modal(SolidColorModal(self))
        await self.defer_interaction(interaction)
        self.config["theme"] = value
        if value == "Custom":
            self.config.setdefault("colors", {})
        await self.cog.save_data()
        self.notice = f"Logger theme changed to {label}."
        await self.redraw(interaction)

    async def save_solid_color(
        self,
        interaction: discord.Interaction,
        raw_color: str,
    ) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        try:
            value = parse_logger_color(raw_color)
        except ValueError as error:
            return await self.send_ephemeral(interaction, str(error))
        await self.defer_interaction(interaction)
        self.config["theme"] = "Solid Color"
        self.config["color"] = value
        await self.cog.save_data()
        self.notice = f"Every logger event now uses `#{value:06X}`."
        await self.redraw(interaction)

    async def save_event_color(
        self,
        interaction: discord.Interaction,
        raw_event: str,
        raw_color: str,
    ) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        event = normalize_event_query(raw_event)
        if event not in self.cog.log_types:
            return await self.send_ephemeral(
                interaction,
                f"`{event or raw_event}` is not a logger event. Use the Events search first.",
            )
        try:
            value = parse_logger_color(raw_color)
        except ValueError as error:
            return await self.send_ephemeral(interaction, str(error))
        await self.defer_interaction(interaction)
        self.config["theme"] = "Custom"
        self.config.setdefault("colors", {})[event] = value
        await self.cog.save_data()
        self.notice = f"{friendly_event_name(event)} now uses `#{value:06X}`."
        await self.redraw(interaction)

    async def clear_custom_colors(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        await self.defer_interaction(interaction)
        self.config["colors"] = {}
        await self.cog.save_data()
        self.notice = "All saved per-event colors were cleared."
        await self.redraw(interaction)

    def history_config(self) -> dict:
        config = self.config
        archive = self.cog.archive_config(self.guild_id)
        if config is not None:
            config["local_archive"] = archive
        return archive

    async def toggle_history_option(
        self,
        option: str,
        interaction: discord.Interaction,
    ) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        await self.defer_interaction(interaction)
        archive = self.history_config()
        labels = {
            "enabled": "Local history collection",
            "cache_messages": "Uncached message recovery",
            "store_attachments": "Attachment file storage",
        }
        archive[option] = not archive.get(option, False)
        await self.cog.save_data()
        self.notice = f"{labels[option]} is now {'on' if archive[option] else 'off'}."
        await self.redraw(interaction)

    async def save_history_limits(
        self,
        interaction: discord.Interaction,
        retention_raw: str,
        file_limit_raw: str,
    ) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        try:
            retention = int(retention_raw.strip())
            file_limit = int(file_limit_raw.strip())
        except ValueError:
            return await self.send_ephemeral(
                interaction,
                "Retention and file size must both be whole numbers.",
            )
        max_file_mb = MAX_ATTACHMENT_BYTES // 1024**2
        if not 1 <= retention <= MAX_RETENTION_DAYS:
            return await self.send_ephemeral(
                interaction,
                f"Retention must be between 1 and {MAX_RETENTION_DAYS} days.",
            )
        if not 1 <= file_limit <= max_file_mb:
            return await self.send_ephemeral(
                interaction,
                f"The per-file limit must be between 1 and {max_file_mb} MB.",
            )
        await self.defer_interaction(interaction)
        archive = self.history_config()
        old_retention = int(archive.get("retention_days", MAX_RETENTION_DAYS))
        archive["retention_days"] = retention
        archive["attachment_size_limit_mb"] = file_limit
        await self.cog.save_data()
        prune_warning = ""
        if retention < old_retention:
            try:
                await self.cog.local_archive.prune_guild(self.guild_id, retention)
            except Exception as error:
                self.cog.bot.log.critical(
                    f"Could not prune local logger history for guild {self.guild_id}: {error}"
                )
                prune_warning = " Older records could not be pruned yet."
        self.notice = (
            f"History keeps {retention} days with a {file_limit} MB per-file limit."
            f"{prune_warning}"
        )
        await self.redraw(interaction)

    async def search_history_results(
        self,
        interaction: discord.Interaction,
        query: Optional[str],
    ) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        await interaction.response.defer(ephemeral=True)
        embed = await self.cog.build_archive_search_embed(self.guild, query=query)
        if embed is None:
            return await interaction.followup.send(
                "No saved logger events matched that search.",
                ephemeral=True,
            )
        try:
            await interaction.user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.followup.send(
                "I couldn't send the results privately. Enable direct messages and try again.",
                ephemeral=True,
            )
        await interaction.followup.send(
            "I sent the matching logger history to your direct messages.",
            ephemeral=True,
        )

    async def clear_history_data(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if not self.enabled:
            return await self.send_ephemeral(interaction, "Enable Logger first.")
        await self.defer_interaction(interaction)
        archive = self.history_config()
        was_enabled = bool(archive.get("enabled"))
        archive["enabled"] = False
        await self.cog.save_data()
        try:
            await self.cog.local_archive.clear_guild(self.guild_id)
        finally:
            archive["enabled"] = was_enabled
            await self.cog.save_data()
        self.notice = "This server's locally saved logger history was cleared."
        await self.redraw(interaction)

    async def handle_action(
        self, action: str, interaction: discord.Interaction
    ) -> None:
        if action == "close":
            self.stop()
            async with self._edit_lock:
                return await interaction.response.edit_message(embed=self.embed, view=None)
        if action == "home":
            self.page = "overview"
            self.notice = None
            return await self.redraw(interaction)
        if action == "create_logger_channel":
            return await self.create_logger_channel(interaction)
        if action == "disable_logger":
            return await self.disable_logger(interaction)
        if action == "toggle_security":
            return await self.toggle_security(interaction)
        if action in {"toggle_event", "enable_category", "disable_category"}:
            return await self.update_events(action, interaction)
        if action == "search_events":
            return await interaction.response.send_modal(EventSearchModal(self))
        if action == "reset_route":
            return await self.reset_route(interaction)
        if action == "clear_filters":
            return await self.clear_filters(interaction)
        if action == "change_solid_color":
            if not await self.authorize(interaction):
                return
            return await interaction.response.send_modal(SolidColorModal(self))
        if action == "set_event_color":
            if not await self.authorize(interaction):
                return
            return await interaction.response.send_modal(EventColorModal(self))
        if action == "clear_custom_colors":
            return await self.clear_custom_colors(interaction)
        if action == "toggle_history":
            return await self.toggle_history_option("enabled", interaction)
        if action == "toggle_recovery":
            return await self.toggle_history_option("cache_messages", interaction)
        if action == "toggle_files":
            return await self.toggle_history_option("store_attachments", interaction)
        if action == "history_limits":
            if not await self.authorize(interaction):
                return
            return await interaction.response.send_modal(HistoryLimitsModal(self))
        if action == "search_history":
            if not await self.authorize(interaction):
                return
            return await interaction.response.send_modal(HistorySearchModal(self))
        if action == "clear_history":
            if not await self.authorize(interaction):
                return
            return await interaction.response.send_modal(HistoryClearModal(self))
        if action == "command_back":
            self.selected_command = None
            return await self.redraw(interaction)
        if action == "command_previous" and self.command_page > 0:
            self.command_page -= 1
            return await self.redraw(interaction)
        if action == "command_next" and self.command_page < self.command_page_count - 1:
            self.command_page += 1
            return await self.redraw(interaction)
        if action == "search_commands":
            return await interaction.response.send_modal(CommandSearchModal(self))
        if action == "all_commands":
            self.command_results = None
            self.command_query = None
            self.selected_command = None
            self.command_page = 0
            return await self.redraw(interaction)


class LoggerPageSelect(ui.Select):
    def __init__(self, menu: LoggerMenu) -> None:
        self.menu = menu
        super().__init__(
            placeholder="Choose a logger section",
            options=[
                discord.SelectOption(
                    label=label,
                    value=page,
                    emoji=emoji,
                    description=description,
                    default=page == menu.page,
                )
                for page, (label, emoji, description) in LOGGER_PAGES.items()
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu.change_page(self.values[0], interaction)


class LoggerEventCategorySelect(ui.Select):
    def __init__(self, menu: LoggerMenu, *, page: str, row: int) -> None:
        self.menu = menu
        self.page = page
        super().__init__(
            placeholder="Choose an event category",
            options=[
                discord.SelectOption(
                    label=category,
                    value=category,
                    emoji=menu.cog.category_icons.get(category, "📁"),
                    description=f"{len(events)} event{'s' if len(events) != 1 else ''}",
                    default=(
                        menu.event_search_results is None
                        and category == menu.event_category
                    ),
                )
                for category, events in menu.cog.categories.items()
            ],
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu.choose_event_category(self.values[0], interaction)


class LoggerEventSelect(ui.Select):
    def __init__(self, menu: LoggerMenu, *, row: int) -> None:
        self.menu = menu
        config = menu.config or {}
        disabled_events = set(config.get("disabled", []))
        options = [
            discord.SelectOption(
                label=friendly_event_name(event)[:100],
                value=event,
                emoji="🟢" if menu.enabled and event not in disabled_events else "⚪",
                description=(
                    f"{menu.event_category_for(event)} · "
                    f"{'Enabled' if menu.enabled and event not in disabled_events else 'Disabled'}"
                )[:100],
                default=event == menu.selected_event,
            )
            for event in menu.current_events()[:25]
        ]
        super().__init__(
            placeholder="Choose an event",
            options=options,
            row=row,
            disabled=not options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu.choose_event(self.values[0], interaction)


class LoggerPrimaryChannelSelect(ui.ChannelSelect):
    def __init__(self, menu: LoggerMenu, *, disabled: bool) -> None:
        self.menu = menu
        current = menu.channel_for(menu.config.get("channel")) if menu.enabled else None
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=(
                f"Current: #{current.name}"
                if current
                else "Choose the primary logging channel"
            ),
            min_values=1,
            max_values=1,
            default_values=[current] if current else [],
            row=1,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu.set_primary_channel(self.values[0], interaction)


class LoggerRouteChannelSelect(ui.ChannelSelect):
    def __init__(self, menu: LoggerMenu, *, disabled: bool) -> None:
        self.menu = menu
        route = (
            menu.channel_for(menu.config.get("channels", {}).get(menu.selected_event))
            if menu.enabled
            else None
        )
        super().__init__(
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
            ],
            placeholder=(
                f"Current route: #{route.name}"
                if route
                else "Choose a destination for the selected event"
            ),
            min_values=1,
            max_values=1,
            default_values=[route] if route else [],
            row=3,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu.set_route(self.values[0], interaction)


class LoggerIgnoredChannelSelect(ui.ChannelSelect):
    def __init__(self, menu: LoggerMenu, *, disabled: bool) -> None:
        self.menu = menu
        defaults = menu.default_ignored_channels()
        super().__init__(
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
            ],
            placeholder=(
                f"Ignoring {len(menu.config.get('ignored_channels', []))} channel(s)"
                if menu.enabled and menu.config.get("ignored_channels")
                else "Choose channels to ignore"
            ),
            min_values=0,
            max_values=25,
            default_values=defaults,
            row=1,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu.set_ignored_channels(list(self.values), interaction)


class LoggerIgnoredBotSelect(ui.UserSelect):
    def __init__(self, menu: LoggerMenu, *, disabled: bool) -> None:
        self.menu = menu
        defaults = menu.default_ignored_bots()
        super().__init__(
            placeholder=(
                f"Ignoring {len(menu.config.get('ignored_bots', []))} bot account(s)"
                if menu.enabled and menu.config.get("ignored_bots")
                else "Choose bot accounts to ignore"
            ),
            min_values=0,
            max_values=25,
            default_values=defaults,
            row=2,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu.set_ignored_bots(list(self.values), interaction)


class LoggerThemeSelect(ui.Select):
    def __init__(self, menu: LoggerMenu, *, disabled: bool) -> None:
        self.menu = menu
        current = menu.config.get("theme") if menu.enabled else None
        super().__init__(
            placeholder="Choose a logger color theme",
            options=[
                discord.SelectOption(
                    label=label,
                    value=key,
                    description=description,
                    emoji=emoji,
                    default=value == current,
                )
                for key, (value, label, description, emoji) in THEMES.items()
            ],
            row=1,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu.choose_theme(self.values[0], interaction)


class LoggerCommandSelect(ui.Select):
    def __init__(self, menu: LoggerMenu) -> None:
        self.menu = menu
        super().__init__(
            placeholder="Choose a command",
            options=[
                discord.SelectOption(
                    label=f"{menu.prefix}{command.qualified_name}"[:100],
                    value=command.qualified_name,
                    description=(
                        command.description or command.short_doc or "Logger command"
                    )[:100],
                    emoji="⌨️",
                )
                for command in menu.current_commands()
            ],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        command = self.menu.bot.get_command(self.values[0])
        if not command:
            return await self.menu.send_ephemeral(
                interaction,
                "That logger command is no longer available.",
            )
        self.menu.selected_command = command
        await self.menu.redraw(interaction)


class LoggerButton(ui.Button):
    def __init__(
        self,
        menu: LoggerMenu,
        action: str,
        label: str,
        emoji: str,
        *,
        row: int,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> None:
        self.menu = menu
        self.action = action
        super().__init__(
            label=label,
            emoji=emoji,
            row=row,
            style=style,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu.handle_action(self.action, interaction)


class LoggerModal(ui.Modal):
    menu: LoggerMenu

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        await self.menu.on_error(interaction, error, self)


class EventSearchModal(LoggerModal, title="Search logger events"):
    query = ui.TextInput(
        label="Event name or category",
        placeholder="Try: deleted messages, roles, invites...",
        min_length=1,
        max_length=100,
    )

    def __init__(self, menu: LoggerMenu) -> None:
        self.menu = menu
        super().__init__()

    async def on_submit(self, interaction: discord.Interaction) -> None:
        results = search_logger_events(self.menu.cog.categories, self.query.value)
        if not results:
            return await self.menu.send_ephemeral(
                interaction,
                f"No logger events matched `{self.query.value}`.",
            )
        self.menu.page = "events"
        self.menu.event_search_results = results[:MAX_EVENT_SEARCH_RESULTS]
        self.menu.selected_event = self.menu.event_search_results[0]
        self.menu.notice = (
            f"Showing the first {MAX_EVENT_SEARCH_RESULTS} matches."
            if len(results) > MAX_EVENT_SEARCH_RESULTS
            else None
        )
        await self.menu.redraw(interaction)


class CommandSearchModal(LoggerModal, title="Search logger commands"):
    query = ui.TextInput(
        label="Command or keyword",
        placeholder="Try: channel, history, color...",
        min_length=1,
        max_length=100,
    )

    def __init__(self, menu: LoggerMenu) -> None:
        self.menu = menu
        super().__init__()

    async def on_submit(self, interaction: discord.Interaction) -> None:
        results = self.menu.search_commands(self.query.value)
        if not results:
            return await self.menu.send_ephemeral(
                interaction,
                f"No logger commands matched `{self.query.value}`.",
            )
        self.menu.command_results = results
        self.menu.command_query = self.query.value.strip()[:80]
        self.menu.command_page = 0
        normalized = normalize_event_query(self.query.value).replace("_", " ")
        exact = next(
            (
                command
                for command in results
                if normalized
                in {
                    command.name.casefold(),
                    command.qualified_name.casefold(),
                    *(alias.casefold() for alias in command.aliases),
                }
            ),
            None,
        )
        self.menu.selected_command = exact
        await self.menu.redraw(interaction)


class SolidColorModal(LoggerModal, title="Set one logger color"):
    def __init__(self, menu: LoggerMenu) -> None:
        self.menu = menu
        current = menu.config.get("color") if menu.enabled else None
        super().__init__()
        self.color = ui.TextInput(
            label="Hex or RGB color",
            placeholder="#5865F2 or 88, 101, 242",
            default=f"#{current:06X}" if isinstance(current, int) else None,
            min_length=3,
            max_length=32,
        )
        self.add_item(self.color)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.menu.save_solid_color(interaction, self.color.value)


class EventColorModal(LoggerModal, title="Set an event color"):
    def __init__(self, menu: LoggerMenu) -> None:
        self.menu = menu
        super().__init__()
        self.event = ui.TextInput(
            label="Logger event",
            placeholder="message_delete",
            default=menu.selected_event,
            min_length=1,
            max_length=50,
        )
        self.color = ui.TextInput(
            label="Hex or RGB color",
            placeholder="#ED4245 or 237, 66, 69",
            min_length=3,
            max_length=32,
        )
        self.add_item(self.event)
        self.add_item(self.color)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.menu.save_event_color(
            interaction,
            self.event.value,
            self.color.value,
        )


class HistoryLimitsModal(LoggerModal, title="Local history limits"):
    def __init__(self, menu: LoggerMenu) -> None:
        self.menu = menu
        archive = menu.history_config()
        self.retention = ui.TextInput(
            label=f"Retention days (1-{MAX_RETENTION_DAYS})",
            default=str(archive.get("retention_days", 30)),
            min_length=1,
            max_length=3,
        )
        max_file_mb = MAX_ATTACHMENT_BYTES // 1024**2
        self.file_limit = ui.TextInput(
            label=f"Per-file limit in MB (1-{max_file_mb})",
            default=str(archive.get("attachment_size_limit_mb", max_file_mb)),
            min_length=1,
            max_length=3,
        )
        super().__init__()
        self.add_item(self.retention)
        self.add_item(self.file_limit)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.menu.save_history_limits(
            interaction,
            self.retention.value,
            self.file_limit.value,
        )


class HistorySearchModal(LoggerModal, title="Search local logger history"):
    query = ui.TextInput(
        label="Words to find (optional)",
        placeholder="Leave blank for the newest saved events",
        required=False,
        max_length=200,
    )

    def __init__(self, menu: LoggerMenu) -> None:
        self.menu = menu
        super().__init__()

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = self.query.value.strip() or None
        await self.menu.search_history_results(interaction, query)


class HistoryClearModal(LoggerModal, title="Clear local logger history"):
    confirmation = ui.TextInput(
        label="Type CLEAR to permanently delete it",
        placeholder="CLEAR",
        min_length=5,
        max_length=5,
    )

    def __init__(self, menu: LoggerMenu) -> None:
        self.menu = menu
        super().__init__()

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.confirmation.value.strip().upper() != "CLEAR":
            return await self.menu.send_ephemeral(
                interaction,
                "Nothing was deleted. Type CLEAR exactly to confirm.",
            )
        await self.menu.clear_history_data(interaction)
