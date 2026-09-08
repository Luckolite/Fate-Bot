"""Adaptive, explainable spam prevention and moderation controls."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict, deque
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import time
from typing import Any, Deque, Iterable, Mapping, MutableMapping, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from botutils import Cooldown, colors
from botutils.antispam import (
    DETECTION_PARENT,
    MODULE_DEFAULTS,
    MODULE_INFO,
    MODULE_ORDER,
    cadence_is_automated,
    effective_action,
    evasion_signals,
    extract_urls,
    format_action,
    format_duration,
    module_enabled,
    module_has_detectors,
    normalize_action,
    normalize_config,
    normalize_content,
    recommended_config,
    repeated_segment_score,
    set_module_enabled,
)
from fate import Fate

# Public compatibility alias used by the web dashboard adapter.
defaults = recommended_config()
default_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
MAX_STATE_KEYS = 20_000
MAX_FINGERPRINT_EVIDENCE = 250


@dataclass(slots=True)
class MessageRecord:
    message: discord.Message
    timestamp: float
    fingerprint: str
    urls: tuple[str, ...]
    images: tuple[tuple[Any, ...], ...]
    stickers: tuple[int, ...]

    @property
    def id(self) -> int:
        return self.message.id

    @property
    def guild_id(self) -> int:
        return self.message.guild.id

    @property
    def channel_id(self) -> int:
        return self.message.channel.id

    @property
    def author_id(self) -> int:
        return self.message.author.id


@dataclass(slots=True)
class SpamSignal:
    module: str
    reason: str
    score: int
    records: list[MessageRecord] = field(default_factory=list)


def attachment_fingerprint(
    attachment: discord.Attachment,
) -> Optional[tuple[Any, ...]]:
    """Return a short-lived image fingerprint without downloading the upload."""
    content_type = (attachment.content_type or "").lower()
    filename = attachment.filename.lower()
    if not content_type.startswith("image/") and not filename.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif")
    ):
        return None
    return (
        int(attachment.size),
        int(attachment.width or 0),
        int(attachment.height or 0),
        content_type.split(";", 1)[0],
    )


class AntiSpam(commands.Cog):
    """Detect spam signals, combine their evidence, and enforce one policy."""

    def __init__(self, bot: Fate) -> None:
        self.bot = bot
        self.config = bot.utils.cache("AntiSpam")
        self.notice_cooldown = Cooldown(2, 12)
        self.started_at = time()

        self.member_records: dict[tuple[int, int], Deque[MessageRecord]] = defaultdict(
            lambda: deque(maxlen=100)
        )
        self.rate_windows: dict[
            tuple[int, int, int, int], Deque[float]
        ] = defaultdict(lambda: deque(maxlen=100))
        self.mention_windows: dict[
            tuple[int, int], Deque[tuple[float, int, int]]
        ] = defaultdict(lambda: deque(maxlen=100))
        self.macro_windows: dict[tuple[int, int], Deque[float]] = defaultdict(
            lambda: deque(maxlen=30)
        )
        self.typing: dict[tuple[int, int], Deque[datetime]] = defaultdict(
            lambda: deque(maxlen=30)
        )
        self.link_records: dict[
            tuple[int, str], Deque[MessageRecord]
        ] = defaultdict(lambda: deque(maxlen=MAX_FINGERPRINT_EVIDENCE))
        self.image_records: dict[
            tuple[int, tuple[Any, ...]], Deque[MessageRecord]
        ] = defaultdict(lambda: deque(maxlen=MAX_FINGERPRINT_EVIDENCE))
        self.campaign_records: dict[
            tuple[int, str], Deque[MessageRecord]
        ] = defaultdict(lambda: deque(maxlen=MAX_FINGERPRINT_EVIDENCE))
        self.edit_windows: dict[tuple[int, int], Deque[float]] = defaultdict(
            lambda: deque(maxlen=25)
        )
        self.recent_deletes: dict[tuple[int, int, int, str], float] = {}
        self.incident_history: dict[tuple[int, int], Deque[float]] = defaultdict(
            lambda: deque(maxlen=16)
        )
        self.incident_cooldowns: dict[tuple[int, int, str], float] = {}

        self.stats: dict[int, dict[str, Any]] = {}
        self.recent_incidents: dict[int, Deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=25)
        )
        self.background_tasks: set[asyncio.Task] = set()

        changed = False
        for _guild_id, guild_config in list(self.config.items()):
            if isinstance(guild_config, MutableMapping):
                changed = normalize_config(guild_config) or changed
        if changed:
            self.track_task(self.bot.loop.create_task(self.config.flush()))
        self.cache_cleanup.start()

    def track_task(self, task: asyncio.Task) -> asyncio.Task:
        self.background_tasks.add(task)
        task.add_done_callback(self._background_task_done)
        return task

    def _background_task_done(self, task: asyncio.Task) -> None:
        self.background_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            self.bot.log.error(
                "AntiSpam background task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def cog_unload(self) -> None:
        self.cache_cleanup.cancel()
        for task in tuple(self.background_tasks):
            task.cancel()
        self.background_tasks.clear()

    def _stats(self, guild_id: int) -> dict[str, Any]:
        if guild_id not in self.stats:
            self.stats[guild_id] = {
                "started_at": getattr(self, "started_at", time()),
                "processed": 0,
                "signals": 0,
                "incidents": 0,
                "deleted": 0,
                "warnings": 0,
                "timeouts": 0,
                "kicks": 0,
                "bans": 0,
                "observed": 0,
                "failures": 0,
                "modules": Counter(),
                "users": set(),
            }
        return self.stats[guild_id]

    def get_config(
        self, guild_id: int, *, create: bool = False
    ) -> Optional[dict[str, Any]]:
        config = self.config.get(guild_id)
        if config is None and create:
            config = recommended_config()
            config["enabled"] = False
            self.config[guild_id] = config
        if (
            config is not None
            and config.get("schema_version") != defaults["schema_version"]
            and normalize_config(config)
        ):
            self.track_task(self.bot.loop.create_task(self.config.flush()))
        return config

    async def enable_for_guild(
        self, guild: discord.Guild
    ) -> tuple[dict[str, Any], bool]:
        config = self.get_config(guild.id)
        created = config is None
        if config is None:
            config = recommended_config()
            self.config[guild.id] = config
        else:
            was_enabled = bool(config.get("enabled"))
            config["enabled"] = True
            if not was_enabled:
                self.clear_runtime_for_guild(
                    guild.id, clear_telemetry=False
                )
        await self.config.flush()
        if created:
            await self.bot.create_log(
                message=f"!on **AntiSpam** - {guild}",
                channel="module_log",
                embedded=True,
                color=colors.green,
            )
        return config, created

    async def set_enabled(
        self, guild: discord.Guild, enabled: bool
    ) -> dict[str, Any]:
        if enabled:
            config, _created = await self.enable_for_guild(guild)
            return config
        config = self.get_config(guild.id, create=True)
        was_enabled = bool(config.get("enabled"))
        config["enabled"] = False
        if was_enabled:
            self.clear_runtime_for_guild(guild.id, clear_telemetry=False)
        await self.config.flush()
        await self.bot.create_log(
            message=f"!off **AntiSpam** - {guild}",
            channel="module_log",
            embedded=True,
            color=colors.red,
        )
        return config

    async def set_module(
        self,
        guild_id: int,
        module: str,
        enabled: bool,
        *,
        enable_system: bool = False,
    ) -> dict[str, Any]:
        config = self.get_config(guild_id, create=True)
        before = (
            deepcopy(config.get(module)),
            tuple(config.get("disabled_modules", [])),
            bool(config.get("enabled")),
        )
        was_enabled = bool(config.get("enabled"))
        set_module_enabled(config, module, enabled)
        if enabled and enable_system:
            config["enabled"] = True
        if not was_enabled and config.get("enabled"):
            self.clear_runtime_for_guild(
                guild_id, clear_telemetry=False
            )
        after = (
            config.get(module),
            tuple(config.get("disabled_modules", [])),
            bool(config.get("enabled")),
        )
        if before != after and (was_enabled or not config.get("enabled")):
            self.clear_runtime_for_guild(
                guild_id, clear_telemetry=False
            )
        await self.config.flush()
        return config

    async def reset_module(
        self, guild_id: int, module: str
    ) -> dict[str, Any]:
        config = self.get_config(guild_id, create=True)
        config[module] = deepcopy(MODULE_DEFAULTS[module])
        set_module_enabled(config, module, True)
        self.clear_runtime_for_guild(guild_id, clear_telemetry=False)
        await self.config.flush()
        return config

    def is_enabled(self, guild_id: int) -> bool:
        return bool((self.get_config(guild_id) or {}).get("enabled"))

    @staticmethod
    def _scope_ids(channel: discord.abc.GuildChannel) -> set[int]:
        ids = {channel.id}
        parent = getattr(channel, "parent", None)
        category = getattr(channel, "category", None) or getattr(
            parent, "category", None
        )
        if parent:
            ids.add(parent.id)
        if category:
            ids.add(category.id)
        return ids

    @staticmethod
    def _bounded_bucket(
        mapping: MutableMapping[Any, Deque[Any]],
        key: Any,
    ) -> Deque[Any]:
        """Return a state bucket while bounding adversarial key growth."""
        if key not in mapping and len(mapping) >= MAX_STATE_KEYS:
            mapping.pop(next(iter(mapping)))
        return mapping[key]

    def is_exempt(
        self,
        member: discord.Member,
        channel: discord.abc.GuildChannel,
        config: Mapping[str, Any],
    ) -> bool:
        if self._scope_ids(channel).intersection(config.get("ignored", [])):
            return True
        if member.id in config.get("trusted_members", []):
            return True
        trusted_roles = set(config.get("trusted_roles", []))
        if any(role.id in trusted_roles for role in member.roles):
            return True
        permissions = member.guild_permissions
        if (
            permissions.administrator
            or permissions.manage_guild
            or permissions.manage_messages
            or permissions.moderate_members
        ):
            return True
        try:
            return bool(self.bot.attrs.is_moderator(member))
        except (AttributeError, KeyError, TypeError):
            # AntiSpam must remain operational while the Moderation cog is
            # absent/reloading. Native Discord moderation permissions above
            # remain a conservative staff exemption in that state.
            return False

    def configuration_warnings(
        self,
        guild: discord.Guild,
        config: Optional[Mapping[str, Any]] = None,
    ) -> list[str]:
        config = config or self.get_config(guild.id) or {}
        warnings = []
        bot_member = guild.me
        if not bot_member:
            return [
                "Fate's server member is not cached yet; permission health is unavailable."
            ]
        permissions = bot_member.guild_permissions
        checks = (
            ("manage_messages", "Manage Messages", "delete detected spam"),
            (
                "read_message_history",
                "Read Message History",
                "collect duplicate evidence",
            ),
            ("moderate_members", "Moderate Members", "apply timeouts"),
            ("send_messages", "Send Messages", "post incident notices"),
            ("embed_links", "Embed Links", "show ghost-ping evidence"),
            (
                "view_audit_log",
                "View Audit Log",
                "distinguish ghost pings from moderator deletes",
            ),
            ("manage_threads", "Manage Threads", "remove excess thread creation"),
        )
        for permission, label, effect in checks:
            if not getattr(permissions, permission, False):
                warnings.append(f"Missing **{label}** — Fate cannot {effect}.")
        active = [
            module
            for module in MODULE_ORDER
            if module_enabled(config, module)
            and module_has_detectors(config, module)
        ]
        if config.get("enabled") and not active:
            warnings.append("No protection modules are active.")
        warnings.extend(
            f"**{MODULE_INFO[module]['label']}** is enabled but has no "
            "active checks; edit it or disable the module."
            for module in MODULE_ORDER
            if module_enabled(config, module) and not module_has_detectors(
                config, module
            )
        )
        threshold_groups = (
            ("Flood control", config.get("rate_limit", [])),
            (
                "Mention bursts",
                config.get("mass_pings", {}).get("thresholds", []),
            ),
            (
                "Repeated text",
                config.get("duplicates", {}).get("thresholds", []),
            ),
        )
        for label, rules in threshold_groups:
            if len(rules) > 25:
                warnings.append(
                    f"**{label}** retains {len(rules)} legacy rolling rules; "
                    "the editor requires reducing that list to 25 or fewer on save."
                )
        stale = [
            channel_id
            for channel_id in config.get("ignored", [])
            if not guild.get_channel(channel_id)
            and not guild.get_thread(channel_id)
        ]
        if stale:
            warnings.append(
                f"{len(stale)} ignored channel target(s) no longer exist."
            )
        if guild.id in config.get("trusted_roles", []):
            warnings.append(
                "The **@everyone** role is trusted, so every member bypasses AntiSpam."
            )
        return warnings

    def coverage_snapshot(
        self,
        guild: discord.Guild,
        config: Mapping[str, Any],
    ) -> dict[str, int]:
        """Summarize visible/in-scope message-channel coverage."""
        channels = {
            channel.id: channel
            for channel in (
                *getattr(guild, "text_channels", ()),
                *getattr(guild, "forums", ()),
            )
        }.values()
        snapshot = {
            "total": 0,
            "protected": 0,
            "delete_ready": 0,
            "ignored": 0,
            "inaccessible": 0,
        }
        bot_member = guild.me
        for channel in channels:
            snapshot["total"] += 1
            if self._scope_ids(channel).intersection(config.get("ignored", [])):
                snapshot["ignored"] += 1
                continue
            if not bot_member:
                snapshot["inaccessible"] += 1
                continue
            permissions = channel.permissions_for(bot_member)
            if not permissions.view_channel:
                snapshot["inaccessible"] += 1
                continue
            snapshot["protected"] += 1
            if permissions.manage_messages and permissions.read_message_history:
                snapshot["delete_ready"] += 1
        return snapshot

    def runtime_snapshot(self, guild_id: int) -> dict[str, Any]:
        stats = self._stats(guild_id)
        return {
            **{
                key: value
                for key, value in stats.items()
                if key not in {"modules", "users"}
            },
            "modules": Counter(stats["modules"]),
            "unique_users": len(stats["users"]),
            "recent": list(self.recent_incidents[guild_id]),
        }

    def clear_runtime_for_guild(
        self, guild_id: int, *, clear_telemetry: bool = True
    ) -> None:
        """Clear one guild's bounded detector state, optionally including stats."""
        mappings = (
            self.member_records,
            self.rate_windows,
            self.mention_windows,
            self.macro_windows,
            self.typing,
            self.link_records,
            self.image_records,
            self.campaign_records,
            self.edit_windows,
            self.recent_deletes,
            self.incident_history,
            self.incident_cooldowns,
        )
        for mapping in mappings:
            for key in list(mapping):
                if isinstance(key, tuple) and key and key[0] == guild_id:
                    del mapping[key]
        if clear_telemetry:
            self.stats.pop(guild_id, None)
            self.recent_incidents.pop(guild_id, None)

    @tasks.loop(seconds=15)
    async def cache_cleanup(self) -> None:
        now = time()
        record_mappings = (
            (self.member_records, now - 3600),
            (self.link_records, now - 3600),
            (self.image_records, now - 3600),
            (self.campaign_records, now - 300),
        )
        for mapping, cutoff in record_mappings:
            for key, records in list(mapping.items()):
                while records and records[0].timestamp < cutoff:
                    records.popleft()
                if not records:
                    del mapping[key]

        for mapping, cutoff in (
            (self.rate_windows, now - 3600),
            # Thirty samples at the maximum one-hour interval can span 30h.
            (self.macro_windows, now - 108_000),
        ):
            for key, timestamps in list(mapping.items()):
                while timestamps and timestamps[0] < cutoff:
                    timestamps.popleft()
                if not timestamps:
                    del mapping[key]

        for key, events in list(self.mention_windows.items()):
            while events and events[0][0] < now - 3600:
                events.popleft()
            if not events:
                del self.mention_windows[key]

        typing_cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        for key, timestamps in list(self.typing.items()):
            while timestamps and timestamps[0] < typing_cutoff:
                timestamps.popleft()
            if not timestamps:
                del self.typing[key]

        for message_key, timestamps in list(self.edit_windows.items()):
            while timestamps and timestamps[0] < now - 300:
                timestamps.popleft()
            if not timestamps:
                del self.edit_windows[message_key]

        self.recent_deletes = {
            key: timestamp
            for key, timestamp in self.recent_deletes.items()
            if timestamp >= now - 300
        }
        self.incident_cooldowns = {
            key: timestamp
            for key, timestamp in self.incident_cooldowns.items()
            if timestamp >= now - 60
        }
        for key, timestamps in list(self.incident_history.items()):
            while timestamps and timestamps[0] < now - 3600:
                timestamps.popleft()
            if not timestamps:
                del self.incident_history[key]

    @cache_cleanup.before_loop
    async def before_cache_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_group(
        name="anti-spam",
        aliases=["antispam"],
        fallback="view",
        invoke_without_command=True,
        description="Opens the anti-spam control center",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def anti_spam(self, ctx: commands.Context) -> None:
        """Open the complete anti-spam dashboard."""
        from cogs.moderation.antispam_ui import AntiSpamDashboard

        await AntiSpamDashboard(self, ctx).start()

    @anti_spam.command(
        name="configure",
        aliases=["config"],
        description="Opens anti-spam configuration",
    )
    @commands.has_permissions(manage_guild=True)
    async def configure(self, ctx: commands.Context) -> None:
        from cogs.moderation.antispam_ui import AntiSpamDashboard

        dashboard = AntiSpamDashboard(self, ctx)
        dashboard.page = "protections"
        await dashboard.start()

    @anti_spam.group(
        name="enable", description="Enables AntiSpam or one protection module"
    )
    @commands.has_permissions(manage_guild=True)
    async def enable(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand:
            return
        config, created = await self.enable_for_guild(ctx.guild)
        active = sum(module_enabled(config, module) for module in MODULE_ORDER)
        profile = "recommended" if created else "saved"
        await ctx.send(
            f"AntiSpam enabled with the {profile} profile and {active} active "
            f"protection{'s' if active != 1 else ''}.",
            ephemeral=bool(ctx.interaction),
        )

    @anti_spam.group(
        name="disable", description="Disables AntiSpam or one protection module"
    )
    @commands.has_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand:
            return
        await self.set_enabled(ctx.guild, False)
        await ctx.send(
            "AntiSpam is disabled. Its settings remain saved until it is enabled again.",
            ephemeral=bool(ctx.interaction),
        )

    async def _command_set_module(
        self, ctx: commands.Context, module: str, enabled: bool
    ) -> None:
        await self.set_module(
            ctx.guild.id,
            module,
            enabled,
            enable_system=enabled,
        )
        label = MODULE_INFO[module]["label"]
        await ctx.send(
            f"{label} is now {'enabled' if enabled else 'disabled'}.",
            ephemeral=bool(ctx.interaction),
        )

    @enable.command(name="rate-limit", description="Enables rolling flood limits")
    async def enable_rate_limit(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "rate_limit", True)

    @enable.command(
        name="mass-pings", description="Enables mention and ghost-ping protection"
    )
    async def enable_mass_pings(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "mass_pings", True)

    @enable.command(
        name="duplicates", description="Enables repeat, link, and media protection"
    )
    async def enable_duplicates(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "duplicates", True)

    @enable.command(
        name="inhuman", description="Enables malformed-message protection"
    )
    async def enable_inhuman(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "inhuman", True)

    @enable.command(
        name="anti-macro", description="Enables automated-cadence protection"
    )
    async def enable_anti_macro(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "anti_macro", True)

    @enable.command(
        name="evasion", description="Enables Unicode and edit-evasion protection"
    )
    async def enable_evasion(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "evasion", True)

    @enable.command(
        name="coordinated", description="Enables multi-account campaign protection"
    )
    async def enable_coordinated(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "coordinated", True)

    @disable.command(name="rate-limit", description="Disables rolling flood limits")
    async def disable_rate_limit(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "rate_limit", False)

    @disable.command(name="mass-pings", description="Disables mention protection")
    async def disable_mass_pings(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "mass_pings", False)

    @disable.command(name="duplicates", description="Disables repeat protection")
    async def disable_duplicates(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "duplicates", False)

    @disable.command(
        name="inhuman", description="Disables malformed-message protection"
    )
    async def disable_inhuman(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "inhuman", False)

    @disable.command(
        name="anti-macro", description="Disables automated-cadence protection"
    )
    async def disable_anti_macro(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "anti_macro", False)

    @disable.command(
        name="evasion", description="Disables Unicode and edit-evasion protection"
    )
    async def disable_evasion(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "evasion", False)

    @disable.command(
        name="coordinated", description="Disables campaign protection"
    )
    async def disable_coordinated(self, ctx: commands.Context) -> None:
        await self._command_set_module(ctx, "coordinated", False)

    @anti_spam.command(
        name="ignore", description="Adds a channel exclusion"
    )
    @commands.has_permissions(manage_guild=True)
    async def ignore(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        config = self.get_config(ctx.guild.id, create=True)
        if channel.id in config["ignored"]:
            return await ctx.send(
                f"{channel.mention} is already ignored.",
                ephemeral=bool(ctx.interaction),
            )
        config["ignored"].append(channel.id)
        self.clear_runtime_for_guild(ctx.guild.id, clear_telemetry=False)
        await self.config.flush()
        await ctx.send(
            f"AntiSpam will ignore {channel.mention} and its threads.",
            ephemeral=bool(ctx.interaction),
        )

    @anti_spam.command(
        name="unignore", description="Removes a channel exclusion"
    )
    @commands.has_permissions(manage_guild=True)
    async def unignore(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        config = self.get_config(ctx.guild.id)
        if not config or channel.id not in config["ignored"]:
            return await ctx.send(
                f"{channel.mention} is not ignored.",
                ephemeral=bool(ctx.interaction),
            )
        config["ignored"].remove(channel.id)
        self.clear_runtime_for_guild(ctx.guild.id, clear_telemetry=False)
        await self.config.flush()
        await ctx.send(
            f"AntiSpam will protect {channel.mention} again.",
            ephemeral=bool(ctx.interaction),
        )

    @anti_spam.command(
        name="punishments", description="Opens the enforcement policy editor"
    )
    @commands.has_permissions(manage_guild=True)
    async def punishments(self, ctx: commands.Context) -> None:
        from cogs.moderation.antispam_ui import AntiSpamDashboard

        dashboard = AntiSpamDashboard(self, ctx)
        dashboard.page = "enforcement"
        await dashboard.start()

    def _make_record(self, message: discord.Message) -> MessageRecord:
        urls = tuple(extract_urls(message.content or ""))
        images = tuple(
            fingerprint
            for attachment in message.attachments
            if (fingerprint := attachment_fingerprint(attachment)) is not None
        )
        return MessageRecord(
            message=message,
            timestamp=(message.edited_at or message.created_at).timestamp(),
            fingerprint=normalize_content(message.content or "", urls=urls),
            urls=urls,
            images=images,
            stickers=tuple(sticker.id for sticker in message.stickers),
        )

    @staticmethod
    def _prune_records(
        records: Deque[MessageRecord], cutoff: float
    ) -> None:
        while records and records[0].timestamp < cutoff:
            records.popleft()

    @staticmethod
    def _replace_record(
        records: Deque[MessageRecord], record: MessageRecord
    ) -> None:
        for existing in tuple(records):
            if existing.id == record.id:
                records.remove(existing)
        records.append(record)

    def _remember_member_record(self, record: MessageRecord) -> None:
        records = self._bounded_bucket(
            self.member_records, (record.guild_id, record.author_id)
        )
        self._replace_record(records, record)

    def _remove_secondary_indexes(self, record: MessageRecord) -> None:
        """Remove an edited message from indexes keyed by its old content."""
        keys = (
            *(
                (self.link_records, (record.guild_id, url))
                for url in record.urls
            ),
            *(
                (self.image_records, (record.guild_id, image))
                for image in record.images
            ),
            (self.campaign_records, (record.guild_id, record.fingerprint)),
        )
        for mapping, key in keys:
            records = mapping.get(key)
            if not records:
                continue
            for existing in tuple(records):
                if existing.id == record.id:
                    records.remove(existing)
            if not records:
                mapping.pop(key, None)

    def _remove_deleted_record(self, message: discord.Message) -> None:
        """Remove a self-deleted message from content and mention indexes."""
        member_key = (message.guild.id, message.author.id)
        member_records = self.member_records.get(member_key)
        if member_records:
            for record in tuple(member_records):
                if record.id == message.id:
                    self._remove_secondary_indexes(record)
                    member_records.remove(record)
            if not member_records:
                self.member_records.pop(member_key, None)
        events = self.mention_windows.get(member_key)
        if events:
            for event in tuple(events):
                if event[2] == message.id:
                    events.remove(event)
            if not events:
                self.mention_windows.pop(member_key, None)
        self.edit_windows.pop((message.guild.id, message.id), None)

    def _rate_signals(
        self,
        record: MessageRecord,
        config: Mapping[str, Any],
        *,
        is_edit: bool,
    ) -> list[SpamSignal]:
        if is_edit or not module_enabled(config, "rate_limit"):
            return []
        signals = []
        for rule in config["rate_limit"]:
            window = int(rule["timespan"])
            threshold = int(rule["threshold"])
            key = (record.guild_id, record.author_id, window, threshold)
            timestamps = self._bounded_bucket(self.rate_windows, key)
            cutoff = record.timestamp - window
            while timestamps and timestamps[0] < cutoff:
                timestamps.popleft()
            timestamps.append(record.timestamp)
            if len(timestamps) >= threshold:
                evidence = [
                    item
                    for item in self.member_records[
                        (record.guild_id, record.author_id)
                    ]
                    if item.timestamp >= cutoff
                ]
                signals.append(
                    SpamSignal(
                        "rate_limit",
                        f"{len(timestamps)} messages inside a rolling "
                        f"{format_duration(window)} window",
                        5,
                        evidence,
                    )
                )
        return signals

    def _mention_signals(
        self, record: MessageRecord, config: Mapping[str, Any]
    ) -> list[SpamSignal]:
        if not module_enabled(config, "mass_pings"):
            return []
        message = record.message
        module = config["mass_pings"]
        role_weight = len(message.raw_role_mentions) * 2
        everyone_weight = 4 if message.mention_everyone else 0
        weighted_mentions = (
            len(message.raw_mentions) + role_weight + everyone_weight
        )
        signals = []
        per_message = int(module.get("per_message", 0) or 0)
        if per_message and weighted_mentions >= per_message:
            signals.append(
                SpamSignal(
                    "mass_pings_per_msg",
                    f"mention weight {weighted_mentions} reached the "
                    f"per-message limit {per_message}",
                    6,
                    [record],
                )
            )
        events = self._bounded_bucket(
            self.mention_windows, (record.guild_id, record.author_id)
        )
        for event in tuple(events):
            if event[2] == record.id:
                events.remove(event)
        if not weighted_mentions:
            return signals
        events.append((record.timestamp, weighted_mentions, record.id))
        largest = max(
            (
                int(rule["timespan"])
                for rule in module.get("thresholds", [])
            ),
            default=0,
        )
        while events and events[0][0] < record.timestamp - largest:
            events.popleft()
        for rule in module.get("thresholds", []):
            window = int(rule["timespan"])
            threshold = int(rule["threshold"])
            recent = [
                event
                for event in events
                if event[0] >= record.timestamp - window
            ]
            if len(recent) < threshold:
                continue
            evidence = [
                item
                for item in self.member_records[
                    (record.guild_id, record.author_id)
                ]
                if item.timestamp >= record.timestamp - window
                and (
                    item.message.raw_mentions
                    or item.message.raw_role_mentions
                    or item.message.mention_everyone
                )
            ]
            distinct = {
                target
                for item in evidence
                for target in (
                    *item.message.raw_mentions,
                    *item.message.raw_role_mentions,
                )
            }
            signals.append(
                SpamSignal(
                    "mass_pings",
                    f"{len(recent)} pinging messages / "
                    f"{format_duration(window)} across "
                    f"{len(distinct)} distinct targets",
                    5,
                    evidence,
                )
            )
        return signals

    def _inhuman_signals(
        self, record: MessageRecord, config: Mapping[str, Any]
    ) -> list[SpamSignal]:
        if (
            not module_enabled(config, "inhuman")
            or not record.message.content
        ):
            return []
        module = config["inhuman"]
        content = record.message.content
        lowered = content.casefold()
        lines = lowered.splitlines()
        alphabetic = sum(character.isalpha() for character in lowered)
        spaces = max(
            1, sum(character.isspace() for character in lowered)
        )
        signals = []
        if (
            module.get("non_abc")
            and len(content) > 256
            and alphabetic == 0
        ):
            signals.append(
                SpamSignal(
                    "non_abc",
                    "a long message with no letters",
                    4,
                    [record],
                )
            )
        if module.get("tall_messages") and (
            (
                len(lines) > 8
                and sum(len(line) for line in lines if line) < 21
            )
            or (len(lines) > 5 and alphabetic == 0)
        ):
            signals.append(
                SpamSignal(
                    "tall_messages",
                    f"vertical spam across {len(lines)} lines",
                    4,
                    [record],
                )
            )
        if module.get("empty_lines"):
            small = sum(not line or len(line) < 3 for line in lines)
            large = sum(len(line) > 2 for line in lines)
            if len(lines) > 8 and small > large:
                signals.append(
                    SpamSignal(
                        "empty_lines",
                        f"{small} empty or tiny lines",
                        4,
                        [record],
                    )
                )
        if (
            module.get("unknown_chars")
            and len(content) > (256 if ":" in content else 128)
        ):
            ratio = len(content) / max(1, alphabetic)
            if ratio > 3 and not (
                "http" in lowered and len(content) < 512
            ):
                signals.append(
                    SpamSignal(
                        "unknown_chars",
                        "text is mostly symbols or non-letter controls",
                        3,
                        [record],
                    )
                )
        if (
            module.get("ascii")
            and len(content) > 256
            and len(content) / spaces > 10
        ):
            signals.append(
                SpamSignal(
                    "ascii",
                    "dense character or ASCII wall",
                    3,
                    [record],
                )
            )
        if (
            module.get("copy_paste")
            and len(content) > (600 if "http" in lowered else 800)
        ):
            key = (record.guild_id, record.author_id)
            typed_recently = any(
                (
                    datetime.now(timezone.utc) - timestamp
                ).total_seconds()
                < 45
                for timestamp in self.typing.get(key, ())
            )
            if not typed_recently:
                # Typing is only a weak risk signal; it never acts alone.
                signals.append(
                    SpamSignal(
                        "copy_paste",
                        "large paste with no recent typing signal",
                        2,
                        [record],
                    )
                )
        return signals

    def _evasion_signals(
        self,
        record: MessageRecord,
        config: Mapping[str, Any],
        *,
        is_edit: bool,
    ) -> list[SpamSignal]:
        if not module_enabled(config, "evasion"):
            return []
        module = config["evasion"]
        signals = [
            SpamSignal(name, reason, 5, [record])
            for name, reason in evasion_signals(
                record.message.content or "", module
            )
        ]
        if is_edit:
            edits = self._bounded_bucket(
                self.edit_windows, (record.guild_id, record.id)
            )
            now = time()
            edits.append(now)
            window = int(module.get("edit_window", 20) or 20)
            while edits and edits[0] < now - window:
                edits.popleft()
            maximum = int(module.get("max_edits", 4) or 4)
            if maximum and len(edits) >= maximum:
                signals.append(
                    SpamSignal(
                        "edit_churn",
                        f"{len(edits)} content edits inside "
                        f"{format_duration(window)}",
                        5,
                        [record],
                    )
                )
        resend_window = int(module.get("resend_window", 30) or 30)
        deleted_at = self.recent_deletes.get(
            (
                record.guild_id,
                record.author_id,
                record.channel_id,
                record.fingerprint,
            )
        )
        if (
            record.fingerprint
            and deleted_at
            and time() - deleted_at <= resend_window
        ):
            signals.append(
                SpamSignal(
                    "delete_resend",
                    "deleted and resent the same normalized content in this "
                    f"channel within {format_duration(resend_window)}",
                    2,
                    [record],
                )
            )
        return signals

    def _duplicate_signals(
        self, record: MessageRecord, config: Mapping[str, Any]
    ) -> list[SpamSignal]:
        if not module_enabled(config, "duplicates"):
            return []
        module = config["duplicates"]
        member_records = self.member_records[
            (record.guild_id, record.author_id)
        ]
        signals = []

        per_message = int(module.get("per_message", 0) or 0)
        if (
            per_message
            and repeated_segment_score(
                record.message.content or "",
                normalized=record.fingerprint,
            )
            >= per_message
        ):
            signals.append(
                SpamSignal(
                    "duplicate_segments",
                    f"a word or line repeated at least {per_message} "
                    "times in one message",
                    4,
                    [record],
                )
            )

        if len(record.fingerprint) >= 6:
            for rule in module.get("thresholds", []):
                window = int(rule["timespan"])
                threshold = int(rule["threshold"])
                matches = [
                    item
                    for item in member_records
                    if item.fingerprint == record.fingerprint
                    and item.timestamp >= record.timestamp - window
                ]
                if len(matches) >= threshold:
                    signals.append(
                        SpamSignal(
                            "duplicates",
                            f"{len(matches)} normalized repeats inside "
                            f"{format_duration(window)}",
                            5,
                            matches,
                        )
                    )

        link_window = int(module.get("same_link", 0) or 0)
        for url in record.urls:
            key = (record.guild_id, url)
            matches = self._bounded_bucket(self.link_records, key)
            self._prune_records(
                matches, record.timestamp - max(1, link_window)
            )
            prior = [item for item in matches if item.id != record.id]
            if link_window and prior:
                same_author = any(
                    item.author_id == record.author_id for item in prior
                )
                signals.append(
                    SpamSignal(
                        "same_link",
                        (
                            "same member repeated a canonical destination inside "
                            if same_author
                            else "canonical destination appeared across members inside "
                        )
                        + f"{format_duration(link_window)}",
                        5 if same_author else 2,
                        [*prior, record],
                    )
                )
            self._replace_record(matches, record)

        image_window = int(module.get("same_image", 0) or 0)
        for image in record.images:
            key = (record.guild_id, image)
            matches = self._bounded_bucket(self.image_records, key)
            self._prune_records(
                matches, record.timestamp - max(1, image_window)
            )
            prior = [item for item in matches if item.id != record.id]
            if image_window and prior:
                same_author = any(
                    item.author_id == record.author_id for item in prior
                )
                signals.append(
                    SpamSignal(
                        "same_image",
                        (
                            "same member repeated an image fingerprint inside "
                            if same_author
                            else "image fingerprint appeared across members inside "
                        )
                        + f"{format_duration(image_window)}",
                        5 if same_author else 2,
                        [*prior, record],
                    )
                )
            self._replace_record(matches, record)

        if record.stickers:
            sticker_window = int(module.get("sticker", 0) or 0)
            same_window = int(module.get("same_sticker", 0) or 0)
            prior_stickers = [
                item
                for item in member_records
                if item.id != record.id and item.stickers
            ]
            if sticker_window and any(
                item.timestamp >= record.timestamp - sticker_window
                for item in prior_stickers
            ):
                signals.append(
                    SpamSignal(
                        "sticker",
                        "multiple sticker messages inside "
                        f"{format_duration(sticker_window)}",
                        4,
                        [*prior_stickers[-3:], record],
                    )
                )
            same = [
                item
                for item in prior_stickers
                if item.timestamp >= record.timestamp - same_window
                and set(item.stickers).intersection(record.stickers)
            ]
            if same_window and same:
                signals.append(
                    SpamSignal(
                        "same_sticker",
                        "the same sticker repeated inside "
                        f"{format_duration(same_window)}",
                        5,
                        [*same, record],
                    )
                )
        return signals

    def _coordinated_signals(
        self, record: MessageRecord, config: Mapping[str, Any]
    ) -> list[SpamSignal]:
        if (
            not module_enabled(config, "coordinated")
            or not record.fingerprint
        ):
            return []
        module = config["coordinated"]
        minimum = int(module.get("min_length", 18))
        if len(record.fingerprint) < minimum and not record.urls:
            return []
        window = int(module.get("window", 20))
        required = int(module.get("unique_users", 3))
        key = (record.guild_id, record.fingerprint)
        records = self._bounded_bucket(self.campaign_records, key)
        self._prune_records(records, record.timestamp - window)
        self._replace_record(records, record)
        authors = {item.author_id for item in records}
        if len(authors) < required:
            return []
        channels = {item.channel_id for item in records}
        return [
            SpamSignal(
                "coordinated",
                f"the same normalized payload from {len(authors)} "
                f"members in {len(channels)} "
                f"channel{'s' if len(channels) != 1 else ''} / "
                f"{format_duration(window)}",
                7,
                list(records),
            )
        ]

    async def _macro_signals(
        self,
        record: MessageRecord,
        config: Mapping[str, Any],
        *,
        is_edit: bool,
    ) -> list[SpamSignal]:
        if is_edit or not module_enabled(config, "anti_macro"):
            return []
        module = config["anti_macro"]
        timestamps = self._bounded_bucket(
            self.macro_windows, (record.guild_id, record.author_id)
        )
        timestamps.append(record.timestamp)
        automated = cadence_is_automated(
            list(timestamps),
            samples=int(module.get("samples", 14)),
            tolerance_percent=int(module.get("tolerance", 8)),
            min_interval=int(module.get("min_interval", 1)),
            max_interval=int(module.get("max_interval", 45)),
        )
        if not automated:
            return []
        with suppress(discord.HTTPException):
            if (await self.bot.get_context(record.message)).command:
                return []
        samples = int(module.get("samples", 14))
        evidence = list(
            self.member_records[(record.guild_id, record.author_id)]
        )[-samples:]
        return [
            SpamSignal(
                "anti_macro",
                f"{samples} messages followed an unusually regular cadence",
                5,
                evidence,
            )
        ]

    @staticmethod
    def _trust_boost(
        member: discord.Member,
    ) -> tuple[int, list[str]]:
        now = datetime.now(timezone.utc)
        boost = 0
        evidence = []
        if member.created_at > now - timedelta(days=7):
            boost += 1
            evidence.append("account younger than 7 days")
        if (
            member.joined_at
            and member.joined_at > now - timedelta(hours=24)
        ):
            boost += 1
            evidence.append("joined this server within 24 hours")
        return boost, evidence

    async def process_message(
        self, message: discord.Message, *, is_edit: bool = False
    ) -> None:
        if (
            not message.guild
            or message.author.bot
            or not isinstance(message.author, discord.Member)
        ):
            return
        config = self.get_config(message.guild.id)
        if not config or not config.get("enabled"):
            return
        if self.is_exempt(message.author, message.channel, config):
            return

        stats = self._stats(message.guild.id)
        stats["processed"] += 1
        record = self._make_record(message)
        if is_edit:
            previous = next(
                (
                    item
                    for item in self.member_records.get(
                        (record.guild_id, record.author_id), ()
                    )
                    if item.id == record.id
                ),
                None,
            )
            if previous:
                self._remove_secondary_indexes(previous)
        self._remember_member_record(record)

        signals = []
        signals.extend(
            self._rate_signals(record, config, is_edit=is_edit)
        )
        signals.extend(self._mention_signals(record, config))
        signals.extend(self._inhuman_signals(record, config))
        signals.extend(
            self._evasion_signals(record, config, is_edit=is_edit)
        )
        signals.extend(self._duplicate_signals(record, config))
        signals.extend(self._coordinated_signals(record, config))
        signals.extend(
            await self._macro_signals(record, config, is_edit=is_edit)
        )
        if not signals:
            return

        # One strongest signal per rule keeps telemetry and scoring legible.
        unique: dict[str, SpamSignal] = {}
        for signal in signals:
            current = unique.get(signal.module)
            if current is None or (
                signal.score,
                len(signal.records),
            ) > (current.score, len(current.records)):
                unique[signal.module] = signal
        signals = list(unique.values())
        stats["signals"] += len(signals)
        stats["modules"].update(signal.module for signal in signals)

        trust_boost, trust_evidence = self._trust_boost(
            message.author
        )
        primary = max(signals, key=lambda signal: signal.score)
        combined_score = sum(signal.score for signal in signals)
        # Account/member age can strengthen real evidence, but cannot act alone.
        combined_score += trust_boost
        if primary.score < 4 and combined_score < 5:
            return
        if trust_evidence:
            primary.reason += (
                f"; age-risk context: {', '.join(trust_evidence)}"
            )
        await self._handle_incident(
            message, primary, signals, combined_score
        )

    async def _handle_incident(
        self,
        message: discord.Message,
        primary: SpamSignal,
        signals: Iterable[SpamSignal],
        score: int,
    ) -> None:
        config = self.get_config(message.guild.id)
        if not config or not config.get("enabled"):
            return
        signals = list(signals)
        stats = self._stats(message.guild.id)
        action = effective_action(config, primary.module)
        mode = config.get("mode", "enforce")
        now = time()
        parent = DETECTION_PARENT.get(primary.module, primary.module)
        cooldown_key = (
            message.guild.id,
            message.author.id,
            parent,
        )
        repeated = (
            now - self.incident_cooldowns.get(cooldown_key, 0) < 3
        )
        if (
            cooldown_key not in self.incident_cooldowns
            and len(self.incident_cooldowns) >= MAX_STATE_KEYS
        ):
            self.incident_cooldowns.pop(next(iter(self.incident_cooldowns)))
        self.incident_cooldowns[cooldown_key] = now

        incident = {
            "timestamp": now,
            "module": primary.module,
            "reason": primary.reason,
            "score": score,
            "user_id": message.author.id,
            "channel_id": message.channel.id,
            "mode": mode,
            "action": (
                "Observed"
                if mode == "observe" or action == "observe"
                else "Delete only"
                if mode == "delete"
                else format_action(action)
            ),
            "signals": [signal.module for signal in signals],
        }
        self.recent_incidents[message.guild.id].appendleft(incident)
        stats["incidents"] += 1
        stats["users"].add(message.author.id)
        self.bot.telemetry.increment("antispam_triggers")

        if mode == "observe" or action == "observe":
            stats["observed"] += 1
            return

        records = []
        for signal in signals:
            if signal.module == "coordinated":
                # Campaign containment intentionally removes the matched wave.
                records.extend(signal.records)
            else:
                # A weak cross-user URL/image match may support the current
                # incident, but must never delete another member's message.
                records.extend(
                    record
                    for record in signal.records
                    if record.author_id == message.author.id
                )
        if not records:
            records = [self._make_record(message)]
        deleted = await self._delete_evidence(records)
        stats["deleted"] += deleted

        if mode == "delete" or action == "delete" or repeated:
            return
        if action == "warn":
            stats["warnings"] += 1
            await self._announce_action(
                message, primary, "Warned and removed the spam"
            )
            return

        member = message.author
        bot_member = message.guild.me
        if (
            not bot_member
            or member.top_role >= bot_member.top_role
            or member.guild_permissions.administrator
        ):
            stats["failures"] += 1
            await self._announce_action(
                message,
                primary,
                "Spam removed; Fate cannot moderate this member's role",
            )
            return

        permissions = bot_member.guild_permissions
        try:
            if action == "kick":
                if not permissions.kick_members:
                    return await self._moderation_failure(
                        message, primary, action, "Missing Kick Members"
                    )
                await member.kick(reason=f"AntiSpam: {primary.reason}")
                stats["kicks"] += 1
                await self._announce_action(message, primary, "Kicked")
                return
            if action == "ban":
                if not permissions.ban_members:
                    return await self._moderation_failure(
                        message, primary, action, "Missing Ban Members"
                    )
                await member.ban(reason=f"AntiSpam: {primary.reason}")
                stats["bans"] += 1
                await self._announce_action(message, primary, "Banned")
                return

            channel_permissions = message.channel.permissions_for(bot_member)
            if not channel_permissions.moderate_members:
                return await self._moderation_failure(
                    message,
                    primary,
                    action,
                    "Missing Moderate Members",
                )
            if action.startswith("timeout:"):
                seconds = int(action.split(":", 1)[1])
                history = None
            else:
                history = self._bounded_bucket(
                    self.incident_history, (message.guild.id, member.id)
                )
                while history and history[0] < now - 3600:
                    history.popleft()
                seconds = min(86400, 300 * (2 ** len(history)))
            until = datetime.now(timezone.utc) + timedelta(
                seconds=seconds
            )
            await member.timeout(
                until, reason=f"AntiSpam: {primary.reason}"
            )
            if history is not None:
                history.append(now)
            stats["timeouts"] += 1
            with suppress(discord.HTTPException, discord.Forbidden):
                await member.send(
                    f"You were timed out in **{message.guild.name}** for "
                    f"{format_duration(seconds)}. "
                    f"Reason: {primary.reason}"
                )
            await self._announce_action(
                message,
                primary,
                f"Timed out for {format_duration(seconds)}",
            )
        except (
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException,
        ) as error:
            await self._moderation_failure(
                message, primary, action, str(error)
            )

    async def _moderation_failure(
        self,
        message: discord.Message,
        signal: SpamSignal,
        action: str,
        reason: str,
    ) -> None:
        self._stats(message.guild.id)["failures"] += 1
        log_message = (
            f"AntiSpam could not apply {action} to {message.author.id} "
            f"in {message.guild.id}: {reason}"
        )
        if reason == "Missing Moderate Members":
            channels = [
                channel
                for channel in message.guild.channels
                if not isinstance(channel, discord.CategoryChannel)
                and hasattr(channel, "permissions_for")
            ]
            can_timeout_somewhere = any(
                channel.permissions_for(message.guild.me).moderate_members
                for channel in channels
            )
            if not can_timeout_somewhere:
                config = self.get_config(message.guild.id)
                if config:
                    parent = DETECTION_PARENT.get(signal.module, signal.module)
                    punishments = config.setdefault("punishments", {})
                    affected = [
                        target
                        for target in DETECTION_PARENT
                        if DETECTION_PARENT[target] == parent
                    ]
                    changed = punishments.get(parent) != "delete"
                    punishments[parent] = "delete"
                    for target in affected:
                        configured = normalize_action(punishments.get(target))
                        if configured == "adaptive" or (
                            configured and configured.startswith("timeout:")
                        ):
                            punishments[target] = "delete"
                            changed = True
                    if changed:
                        await self.config.flush()
                    self.bot.log.debug(
                        f"{log_message}; disabled timeout enforcement for "
                        f"the {parent} module because Fate lacks Moderate "
                        "Members in every message channel"
                    )
                else:
                    self.bot.log.warning(log_message)
            else:
                self.bot.log.debug(log_message)
        else:
            self.bot.log.warning(log_message)
        await self._announce_action(
            message,
            signal,
            f"Spam removed; {format_action(action)} failed",
        )

    async def _delete_evidence(
        self, records: Iterable[MessageRecord]
    ) -> int:
        unique = {record.id: record for record in records}
        grouped: dict[int, list[discord.Message]] = defaultdict(list)
        for record in unique.values():
            grouped[record.channel_id].append(record.message)

        deleted = 0
        for messages in grouped.values():
            channel = messages[0].channel
            bot_member = messages[0].guild.me
            if (
                not bot_member
                or not channel.permissions_for(
                    bot_member
                ).manage_messages
            ):
                continue
            for offset in range(0, len(messages), 100):
                chunk = messages[offset : offset + 100]
                filtered = self.bot.filtered_messages.setdefault(
                    chunk[0].guild.id, {}
                )
                if len(chunk) > 1 and hasattr(channel, "delete_messages"):
                    for candidate in chunk:
                        filtered[candidate.id] = time()
                    try:
                        await channel.delete_messages(chunk)
                    except (
                        discord.Forbidden,
                        discord.NotFound,
                        discord.HTTPException,
                    ):
                        for candidate in chunk:
                            filtered.pop(candidate.id, None)
                    else:
                        deleted += len(chunk)
                        continue
                for candidate in chunk:
                    filtered[candidate.id] = time()
                    try:
                        await candidate.delete()
                    except (
                        discord.Forbidden,
                        discord.NotFound,
                        discord.HTTPException,
                    ):
                        filtered.pop(candidate.id, None)
                    else:
                        deleted += 1
        return deleted

    async def _announce_action(
        self,
        message: discord.Message,
        signal: SpamSignal,
        action: str,
    ) -> None:
        if self.notice_cooldown.check(message.channel.id):
            return
        bot_member = message.guild.me
        if (
            not bot_member
            or not message.channel.permissions_for(
                bot_member
            ).send_messages
        ):
            return
        parent = DETECTION_PARENT.get(signal.module, signal.module)
        embed = discord.Embed(
            title="🛡️ Spam contained",
            description=(
                f"**Member:** {message.author.mention}\n"
                f"**Signal:** {MODULE_INFO[parent]['label']}\n"
                f"**Evidence:** {signal.reason}\n"
                f"**Response:** {action}"
            ),
            color=colors.fate,
            timestamp=datetime.now(timezone.utc),
        )
        with suppress(
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException,
        ):
            await message.channel.send(
                embed=embed,
                allowed_mentions=default_mentions,
                delete_after=12,
            )

    @commands.Cog.listener()
    async def on_typing(
        self,
        channel: discord.abc.Messageable,
        user: discord.User | discord.Member,
        when: datetime,
    ) -> None:
        guild = getattr(channel, "guild", None)
        if not guild or user.bot:
            return
        config = self.get_config(guild.id)
        if (
            config
            and module_enabled(config, "inhuman")
            and config["inhuman"].get("copy_paste")
        ):
            timestamp = (
                when
                if when.tzinfo
                else when.replace(tzinfo=timezone.utc)
            )
            self._bounded_bucket(
                self.typing, (guild.id, user.id)
            ).append(timestamp)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.process_message(message)

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        if (
            before.content == after.content
            and [item.id for item in before.attachments]
            == [item.id for item in after.attachments]
        ):
            return
        removed_targets = self._mention_target_ids(
            before
        ) - self._mention_target_ids(after)
        if removed_targets:
            await self._check_ghost_ping(
                before,
                from_edit=True,
                targets=removed_targets,
            )
            if before.id in self.bot.filtered_messages.get(
                before.guild.id, {}
            ):
                return
        await self.process_message(after, is_edit=True)

    @commands.Cog.listener()
    async def on_message_delete(
        self, message: discord.Message
    ) -> None:
        if (
            not message.guild
            or message.author.bot
            or not isinstance(message.author, discord.Member)
        ):
            return
        config = self.get_config(message.guild.id)
        if (
            not config
            or not config.get("enabled")
            or self.is_exempt(
                message.author, message.channel, config
            )
        ):
            return
        self._remove_deleted_record(message)
        if message.id in self.bot.filtered_messages.get(message.guild.id, {}):
            return
        fingerprint = normalize_content(message.content or "")
        if fingerprint and module_enabled(config, "evasion"):
            key = (
                message.guild.id,
                message.author.id,
                message.channel.id,
                fingerprint,
            )
            if (
                key not in self.recent_deletes
                and len(self.recent_deletes) >= MAX_STATE_KEYS
            ):
                self.recent_deletes.pop(next(iter(self.recent_deletes)))
            self.recent_deletes[key] = time()
        await self._check_ghost_ping(message)

    @staticmethod
    def _mention_target_ids(message: discord.Message) -> set[int]:
        targets = set(message.raw_mentions) | set(message.raw_role_mentions)
        targets.update(member.id for member in message.mentions)
        if message.mention_everyone:
            targets.add(0)
        return targets

    async def _check_ghost_ping(
        self,
        message: discord.Message,
        *,
        from_edit: bool = False,
        targets: Optional[set[int]] = None,
    ) -> None:
        if (
            not message.guild
            or message.author.bot
            or not isinstance(message.author, discord.Member)
        ):
            return
        config = self.get_config(message.guild.id)
        if (
            not config
            or not module_enabled(config, "mass_pings")
            or not config["mass_pings"].get("ghost_pings")
            or self.is_exempt(
                message.author, message.channel, config
            )
            or message.id in self.bot.suppressed
        ):
            return
        raw_targets = (
            set(targets)
            if targets is not None
            else self._mention_target_ids(message)
        )
        if not raw_targets:
            return
        if from_edit:
            timing = "by editing the message"
        else:
            age = (
                datetime.now(timezone.utc) - message.created_at
            ).total_seconds()
            if age > 120:
                return
            bot_member = message.guild.me
            if (
                not bot_member
                or not bot_member.guild_permissions.view_audit_log
            ):
                return
            # Audit entries can trail gateway deletion events briefly.  Waiting
            # here prevents a moderator delete from becoming a false incident.
            await asyncio.sleep(0.5)
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=10)
            try:
                async for entry in message.guild.audit_logs(
                    limit=6,
                    action=discord.AuditLogAction.message_delete,
                ):
                    extra_channel = getattr(
                        getattr(entry, "extra", None), "channel", None
                    )
                    if (
                        entry.created_at >= cutoff
                        and getattr(entry.target, "id", None)
                        == message.author.id
                        and (
                            not extra_channel
                            or extra_channel.id == message.channel.id
                        )
                    ):
                        return
                async for entry in message.guild.audit_logs(
                    limit=3,
                    action=discord.AuditLogAction.message_bulk_delete,
                ):
                    extra_channel = getattr(
                        getattr(entry, "extra", None), "channel", None
                    )
                    if (
                        entry.created_at >= cutoff
                        and (
                            not extra_channel
                            or extra_channel.id == message.channel.id
                        )
                    ):
                        return
            except (discord.Forbidden, discord.HTTPException):
                return
            timing = f"after {format_duration(max(1, round(age)))}"

        record = self._make_record(message)
        signal = SpamSignal(
            "ghost_pings",
            f"removed {len(raw_targets)} mention "
            f"target{'s' if len(raw_targets) != 1 else ''} {timing}",
            4,
            [record],
        )
        await self._handle_incident(
            message, signal, [signal], signal.score
        )

    @commands.Cog.listener()
    async def on_thread_create(
        self, thread: discord.Thread
    ) -> None:
        guild = thread.guild
        config = self.get_config(guild.id)
        if (
            not config
            or not module_enabled(config, "duplicates")
        ):
            return
        owner = thread.owner or guild.get_member(
            thread.owner_id or 0
        )
        if (
            not owner
            or owner.bot
            or self.is_exempt(owner, thread, config)
        ):
            return
        limit = int(
            config["duplicates"].get("max_open_threads", 0)
            or 0
        )
        if not limit or not thread.parent:
            return
        open_threads = [
            candidate
            for candidate in thread.parent.threads
            if candidate.owner_id == owner.id
            and not candidate.archived
        ]
        if len(open_threads) <= limit:
            return

        stats = self._stats(guild.id)
        stats["signals"] += 1
        stats["incidents"] += 1
        stats["modules"].update(["max_open_threads"])
        stats["users"].add(owner.id)
        mode = config.get("mode", "enforce")
        action = effective_action(config, "max_open_threads")
        observed = mode == "observe" or action == "observe"
        self.recent_incidents[guild.id].appendleft(
            {
                "timestamp": time(),
                "module": "max_open_threads",
                "reason": (
                    f"opened {len(open_threads)} threads with "
                    f"a limit of {limit}"
                ),
                "score": 4,
                "user_id": owner.id,
                "channel_id": thread.parent.id,
                "mode": mode,
                "action": (
                    "Observed"
                    if observed
                    else "Deleted thread"
                ),
                "signals": ["max_open_threads"],
            }
        )
        if observed:
            stats["observed"] += 1
            return
        bot_member = guild.me
        if (
            not bot_member
            or not thread.parent.permissions_for(
                bot_member
            ).manage_threads
        ):
            stats["failures"] += 1
            return
        try:
            await thread.delete(
                reason=(
                    f"AntiSpam: more than {limit} open threads"
                )
            )
        except (
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException,
        ):
            stats["failures"] += 1
            return
        stats["deleted"] += 1
        parent_send = getattr(thread.parent, "send", None)
        if (
            callable(parent_send)
            and thread.parent.permissions_for(bot_member).send_messages
        ):
            with suppress(
                discord.Forbidden, discord.HTTPException
            ):
                await parent_send(
                    f"{owner.mention}, you can have up to {limit} "
                    f"open thread{'s' if limit != 1 else ''} here.",
                    allowed_mentions=default_mentions,
                    delete_after=10,
                )

    # End of AntiSpam.


async def setup(bot: Fate) -> None:
    await bot.add_cog(AntiSpam(bot), override=True)
