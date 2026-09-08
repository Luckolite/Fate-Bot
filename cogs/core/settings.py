"""
cogs.core.settings
~~~~~~~~~~~~~~~~~~

A modern dashboard for common server settings.

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import json
from copy import deepcopy
from typing import Optional

import discord
from discord import app_commands, ui
from discord.ext import commands

from apps.Dashboard.dashboard.module_validation import validate_module_settings
from apps.Dashboard.dashboard.validation import MAX_PURGE_LIMIT, ValidationError
from botutils import colors
from botutils.localization import (
    LANGUAGES,
    LANGUAGE_EMOJIS,
    normalize_language,
    ranked_language_codes,
)
from botutils.log_archive import (
    MAX_ATTACHMENT_BYTES,
    MAX_GUILD_BYTES,
    MAX_MESSAGE_ATTACHMENT_BYTES,
)

DEFAULT_SETTINGS = {
    "purge_limit": 1000,
    "purge_confirmation": True,
    "warns_channel": None,
    "disabled_channels": [],
    "language": "en",
}

LOCAL_HISTORY_DEFAULTS = {
    "enabled": False,
    "retention_days": 30,
    "cache_messages": True,
    "store_attachments": False,
    "attachment_size_limit_mb": MAX_ATTACHMENT_BYTES // (1024 ** 2),
}

CATEGORIES = {
    "overview": ("Overview", "A summary of the server's bot configuration", "🏠"),
    "general": ("General", "Prefix, purge, warnings, and command channels", "⚙️"),
    "language": ("Language", "Choose the language Fate uses in this server", "🌐"),
    "ranking": ("Ranking", "XP gain and leveling configuration", "🏆"),
    "messages": ("Messages", "Level-up and moderation message routing", "💬"),
    "logging": ("Logging", "Activity records, delivery, security, and local history", "📋"),
}


def channel_text(guild: discord.Guild, value, *, current="Current channel") -> str:
    if value is True:
        return current
    if not value:
        return "Disabled"
    channel = guild.get_channel(int(value))
    return channel.mention if channel else f"Missing channel (`{value}`)"


def local_history_config(config: dict) -> dict:
    """Return display-safe local history settings for legacy configurations."""
    archive = config.get("local_archive")
    if not isinstance(archive, dict):
        archive = {}
    normalized = deepcopy(LOCAL_HISTORY_DEFAULTS)
    normalized.update({key: archive[key] for key in normalized if key in archive})
    try:
        normalized["retention_days"] = int(normalized["retention_days"])
    except (TypeError, ValueError):
        normalized["retention_days"] = LOCAL_HISTORY_DEFAULTS["retention_days"]
    normalized["retention_days"] = min(max(normalized["retention_days"], 1), 365)
    try:
        attachment_limit_value = normalized["attachment_size_limit_mb"]
        if isinstance(attachment_limit_value, bool) or (
            isinstance(attachment_limit_value, float)
            and not attachment_limit_value.is_integer()
        ):
            raise ValueError
        attachment_limit = int(attachment_limit_value)
    except (TypeError, ValueError):
        attachment_limit = LOCAL_HISTORY_DEFAULTS["attachment_size_limit_mb"]
    normalized["attachment_size_limit_mb"] = min(
        max(attachment_limit, 1),
        MAX_ATTACHMENT_BYTES // (1024 ** 2),
    )
    for key in ("enabled", "cache_messages", "store_attachments"):
        if type(normalized[key]) is not bool:
            normalized[key] = LOCAL_HISTORY_DEFAULTS[key]
    return normalized


def editable_logging_config(config: dict) -> dict:
    """Include newly added Logging settings in JSON shown for old guilds."""
    editable = deepcopy(config)
    editable["local_archive"] = local_history_config(config)
    return editable


class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.utils.cache("settings", auto_sync=True)

    def get_config(self, guild_id: int) -> dict:
        config = self.config.get(guild_id)
        if config is None:
            config = deepcopy(DEFAULT_SETTINGS)
            self.config[guild_id] = config
        else:
            for key, value in DEFAULT_SETTINGS.items():
                config.setdefault(key, deepcopy(value))
        return config

    async def save_config(self, guild_id: int, config: dict) -> None:
        self.config[guild_id] = config
        await self.config.flush()

    def get_language(self, guild_id: int) -> str:
        config = self.config.get(guild_id, DEFAULT_SETTINGS)
        return normalize_language(config.get("language"))

    async def bot_check(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            return True
        config = self.config.get(ctx.guild.id, DEFAULT_SETTINGS)
        disabled = {
            int(channel_id)
            for channel_id in config.get("disabled_channels", [])
            if str(channel_id).isdigit()
        }
        parent_id = getattr(ctx.channel, "parent_id", None)
        return ctx.channel.id not in disabled and parent_id not in disabled

    @commands.hybrid_command(name="settings", description="General bot settings")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @app_commands.default_permissions(administrator=True)
    async def settings(self, ctx: commands.Context):
        await self.open_menu(ctx)

    async def open_menu(
        self,
        ctx: commands.Context,
        *,
        category: str = "overview"
    ) -> "SettingsMenu":
        """Open the shared settings UI at a specific category."""
        if category not in CATEGORIES:
            raise ValueError(f"Unknown settings category: {category}")
        menu = SettingsMenu(self, ctx, category=category)
        await menu.start()
        return menu


class SettingsMenu(ui.View):
    def __init__(
        self,
        cog: Settings,
        ctx: commands.Context,
        *,
        category: str = "overview"
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.bot = cog.bot
        self.ctx = ctx
        self.guild = ctx.guild
        self.user = ctx.author
        self.category = category
        self.message: Optional[discord.Message] = None

    @property
    def general(self) -> dict:
        return self.cog.get_config(self.guild.id)

    @property
    def ranking_cog(self):
        return self.bot.get_cog("Ranking")

    @property
    def messages_cog(self):
        return self.bot.get_cog("Messages")

    @property
    def logger_cog(self):
        return self.bot.get_cog("Logger")

    def prefix_config(self) -> dict:
        return self.bot.guild_prefixes.get(
            self.guild.id,
            {"prefix": ".", "override": False}
        )

    async def ranking_config(self):
        cog = self.ranking_cog
        if not cog:
            return None
        return await cog.config[self.guild.id] or deepcopy(cog.default_config)

    def messages_config(self) -> Optional[dict]:
        cog = self.messages_cog
        if not cog:
            return None
        config = cog.config.get(self.guild.id)
        if config is None:
            config = deepcopy(cog.default_modules)
        else:
            for key, value in cog.default_modules.items():
                config.setdefault(key, deepcopy(value))
        return config

    def logger_config(self) -> Optional[dict]:
        cog = self.logger_cog
        if not cog:
            return None
        return cog.config.get(str(self.guild.id))

    async def start(self) -> None:
        await self.refresh()
        self.message = await self.ctx.send(embed=self.embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "Only the administrator who opened this menu can use it.",
                ephemeral=True
            )
            return False
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "You need Administrator permission to change these settings.",
                ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: ui.Item
    ) -> None:
        self.bot.log(f"Settings menu error in guild {self.guild.id}\n{error}")
        message = "I couldn't save that setting. The error has been logged."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def refresh_message(self) -> None:
        await self.refresh()
        if self.message:
            await self.message.edit(embed=self.embed, view=self)

    async def refresh(self) -> None:
        self.embed = await self.build_embed()
        self.clear_items()
        self.add_item(CategorySelect(self))

        if self.category == "general":
            self.add_general_items()
        elif self.category == "language":
            self.add_language_items()
        elif self.category == "ranking":
            await self.add_ranking_items()
        elif self.category == "messages":
            self.add_message_items()
        elif self.category == "logging":
            self.add_logging_items()

        self.add_item(SettingsButton(self, "home", "Home", "🏠", row=4))
        self.add_item(SettingsButton(self, "refresh", "Refresh", "🔄", row=4))
        self.add_item(SettingsButton(
            self, "close", "Close", "✖️",
            style=discord.ButtonStyle.danger,
            row=4
        ))

    def add_general_items(self) -> None:
        warning = self.guild.get_channel(int(self.general["warns_channel"] or 0))
        self.add_item(SettingsChannelSelect(
            self,
            "warns_channel",
            placeholder="Choose the warning output channel",
            row=1,
            defaults=[warning] if warning else []
        ))
        disabled = [
            channel for channel_id in self.general["disabled_channels"]
            if (channel := self.guild.get_channel(int(channel_id)))
        ][:25]
        self.add_item(SettingsChannelSelect(
            self,
            "disabled_channels",
            placeholder="Choose channels where commands are disabled",
            row=2,
            max_values=25,
            defaults=disabled
        ))
        self.add_item(SettingsButton(
            self, "edit_general", "Edit prefix & purge limit", "✏️",
            style=discord.ButtonStyle.primary,
            row=3
        ))
        self.add_item(SettingsButton(
            self, "toggle_personal_prefixes", "Toggle personal prefixes", "🔀",
            row=3
        ))
        self.add_item(SettingsButton(
            self,
            "toggle_purge_confirmation",
            f"Purge confirmation: {'On' if self.general['purge_confirmation'] else 'Off'}",
            "✅" if self.general["purge_confirmation"] else "⚡",
            row=3,
        ))

    def add_language_items(self) -> None:
        self.add_item(LanguageSelect(self))

    async def add_ranking_items(self) -> None:
        self.add_item(SettingsButton(
            self, "edit_ranking", "Edit XP settings", "✏️",
            style=discord.ButtonStyle.primary,
            row=1,
            disabled=self.ranking_cog is None
        ))
        config = await self.ranking_config()
        if config is None:
            return
        disabled = [
            channel for channel_id in config.get("disabled_channels", [])
            if (channel := self.guild.get_channel(int(channel_id)))
        ][:25]
        self.add_item(SettingsChannelSelect(
            self,
            "xp_disabled_channels",
            placeholder="Choose channels where XP gain is disabled",
            row=2,
            max_values=25,
            defaults=disabled,
        ))

    def add_message_items(self) -> None:
        config = self.messages_config()
        if config is None:
            return
        level_value = config["level_up_messages"]
        mod_value = config["redirect_mod_commands"]
        level_channel = self.guild.get_channel(
            level_value
            if isinstance(level_value, int) and not isinstance(level_value, bool)
            else 0
        )
        mod_channel = self.guild.get_channel(
            mod_value
            if isinstance(mod_value, int) and not isinstance(mod_value, bool)
            else 0
        )
        self.add_item(SettingsChannelSelect(
            self,
            "level_up_messages",
            placeholder="Choose the level-up channel",
            row=1,
            defaults=[level_channel] if level_channel else []
        ))
        self.add_item(SettingsChannelSelect(
            self,
            "redirect_mod_commands",
            placeholder="Choose the moderation response channel",
            row=2,
            defaults=[mod_channel] if mod_channel else []
        ))
        self.add_item(SettingsButton(
            self, "level_current", "Level-ups in current channel", "📍", row=3
        ))
        self.add_item(SettingsButton(
            self, "level_disable", "Disable level-ups", "🔕", row=3
        ))
        self.add_item(SettingsButton(
            self, "mod_disable", "Disable mod redirect", "🚫", row=3
        ))

    def add_logging_items(self) -> None:
        if self.logger_cog is None:
            return
        config = self.logger_config()
        if config is None:
            self.add_item(SettingsButton(
                self,
                "enable_logger",
                "Enable logger",
                "✅",
                row=1,
                style=discord.ButtonStyle.success
            ))
            return
        log_channel = self.guild.get_channel(config.get("channel", 0))
        self.add_item(SettingsChannelSelect(
            self,
            "logger_channel",
            placeholder=(
                f"Current: #{log_channel.name}"
                if log_channel
                else "Choose the primary logging channel"
            ),
            row=1,
            defaults=[log_channel] if log_channel else []
        ))
        ignored = [
            channel for channel_id in config.get("ignored_channels", [])
            if (channel := self.guild.get_channel(int(channel_id)))
        ][:25]
        self.add_item(SettingsChannelSelect(
            self,
            "logger_ignored_channels",
            placeholder=(
                f"Currently ignoring {len(ignored)} channel(s)"
                if ignored
                else "No logging channels are currently ignored"
            ),
            row=2,
            max_values=25,
            defaults=ignored
        ))
        self.add_item(SettingsButton(
            self, "toggle_logger_security", "Toggle security", "🔐", row=3
        ))
        self.add_item(SettingsButton(
            self, "edit_logger_json", "Edit and save JSON", None, row=3
        ))
        self.add_item(SettingsButton(
            self,
            "disable_logger",
            "Disable logger",
            "🛑",
            row=3,
            style=discord.ButtonStyle.danger
        ))

    async def build_embed(self) -> discord.Embed:
        title, description, emoji = CATEGORIES[self.category]
        embed = discord.Embed(
            title=f"{emoji} {title} Settings",
            description=description,
            color=self.bot.config.get("theme_color", colors.fate)
        )
        embed.set_author(
            name=self.guild.name,
            icon_url=self.guild.icon.url if self.guild.icon else None
        )

        if self.category == "overview":
            await self.build_overview(embed)
        elif self.category == "general":
            self.build_general(embed)
        elif self.category == "language":
            self.build_language(embed)
        elif self.category == "ranking":
            await self.build_ranking(embed)
        elif self.category == "messages":
            self.build_messages(embed)
        elif self.category == "logging":
            self.build_logging(embed)

        embed.set_footer(text="Changes save immediately • Menu expires after 3 minutes")
        return embed

    async def build_overview(self, embed: discord.Embed) -> None:
        prefix = self.prefix_config()
        general = self.general
        embed.add_field(
            name="General",
            value=(
                f"Prefix: `{prefix['prefix']}`\n"
                f"Language: `{LANGUAGES[self.cog.get_language(self.guild.id)]}`\n"
                f"Purge limit: `{general['purge_limit']}`\n"
                "Purge confirmation: "
                f"`{'Enabled' if general['purge_confirmation'] else 'Disabled'}`\n"
                f"Disabled prefix-command channels: "
                f"`{len(general['disabled_channels'])}`"
            ),
            inline=True
        )

        ranking = await self.ranking_config()
        if ranking:
            embed.add_field(
                name="Ranking",
                value=(
                    f"XP per message: `{ranking['min_xp_per_msg']}`–"
                    f"`{ranking['max_xp_per_msg']}`\n"
                    f"First level: `{ranking['first_lvl_xp_req']} XP`\n"
                    "XP-disabled channels: "
                    f"`{len(ranking.get('disabled_channels', []))}`"
                ),
                inline=True
            )
        else:
            embed.add_field(name="Ranking", value="Module unavailable", inline=True)

        messages = self.messages_config()
        if messages:
            embed.add_field(
                name="Messages",
                value=(
                    "Level-ups: "
                    f"{channel_text(self.guild, messages['level_up_messages'])}\n"
                    "Mod redirect: "
                    f"{channel_text(self.guild, messages['redirect_mod_commands'])}"
                ),
                inline=False
            )
        else:
            embed.add_field(name="Messages", value="Module unavailable", inline=False)

        logger = self.logger_config()
        if logger:
            embed.add_field(
                name="Logging",
                value=(
                    f"Primary channel: {channel_text(self.guild, logger['channel'])}\n"
                    f"Security: `{'Enabled' if logger['secure'] else 'Disabled'}`"
                ),
                inline=False
            )
        else:
            embed.add_field(
                name="Logging",
                value="Not configured — use the logger setup command first",
                inline=False
            )

    def build_general(self, embed: discord.Embed) -> None:
        prefix = self.prefix_config()
        config = self.general
        disabled = [
            channel.mention for channel_id in config["disabled_channels"]
            if (channel := self.guild.get_channel(int(channel_id)))
        ]
        embed.add_field(name="Prefix", value=f"`{prefix['prefix']}`", inline=True)
        embed.add_field(
            name="Personal prefixes",
            value="Disabled" if prefix["override"] else "Allowed",
            inline=True
        )
        embed.add_field(
            name="Purge limit",
            value=f"`{config['purge_limit']}` messages",
            inline=True
        )
        embed.add_field(
            name="Purge confirmation",
            value="Enabled" if config["purge_confirmation"] else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Warning output",
            value=channel_text(self.guild, config["warns_channel"]),
            inline=False
        )
        embed.add_field(
            name="Prefix commands disabled in",
            value=", ".join(disabled) if disabled else "No channels",
            inline=False
        )

    def build_language(self, embed: discord.Embed) -> None:
        language = self.cog.get_language(self.guild.id)
        embed.add_field(
            name="Bot language",
            value=f"**{LANGUAGES[language]}**",
            inline=False,
        )
        embed.add_field(
            name="How translation works",
            value=(
                "Fate automatically translates its outgoing messages and interface "
                "text. Mentions, links, code, commands, and Discord IDs stay unchanged. "
                "Translations are cached so the same English text is not translated twice."
            ),
            inline=False,
        )

    async def build_ranking(self, embed: discord.Embed) -> None:
        config = await self.ranking_config()
        if not config:
            embed.description = "The Ranking module is not currently loaded."
            return
        embed.add_field(
            name="XP per eligible message",
            value=f"`{config['min_xp_per_msg']}`–`{config['max_xp_per_msg']}`",
            inline=True
        )
        embed.add_field(
            name="First level requirement",
            value=f"`{config['first_lvl_xp_req']} XP`",
            inline=True
        )
        embed.add_field(
            name="Rate limit",
            value=(
                f"`{config['msgs_within_timeframe']}` message(s) every "
                f"`{config['timeframe']}` seconds"
            ),
            inline=False
        )
        disabled = [
            channel.mention for channel_id in config.get("disabled_channels", [])
            if (channel := self.guild.get_channel(int(channel_id)))
        ]
        missing = [
            f"Missing channel (`{channel_id}`)"
            for channel_id in config.get("disabled_channels", [])
            if not self.guild.get_channel(int(channel_id))
        ]
        embed.add_field(
            name="XP gain disabled in",
            value=", ".join((*disabled, *missing)) if disabled or missing else "No channels",
            inline=False,
        )

    def build_messages(self, embed: discord.Embed) -> None:
        config = self.messages_config()
        if config is None:
            embed.description = "The Messages module is not currently loaded."
            return
        embed.add_field(
            name="Level-up messages",
            value=channel_text(self.guild, config["level_up_messages"]),
            inline=False
        )
        embed.add_field(
            name="Moderation command redirect",
            value=channel_text(self.guild, config["redirect_mod_commands"]),
            inline=False
        )

    def build_logging(self, embed: discord.Embed) -> None:
        if self.logger_cog is None:
            embed.description = "The Logger module is currently unavailable."
            return
        config = self.logger_config()
        if config is None:
            embed.description = "Logging is currently disabled for this server."
            embed.add_field(
                name="Status",
                value="Disabled",
                inline=True
            )
            embed.add_field(
                name="Enable logging",
                value=(
                    "Use the **Enable logger** button below. Fate will create "
                    "a new `discord-log` channel for this server."
                ),
                inline=False
            )
            return
        embed.add_field(name="Status", value="Enabled", inline=True)
        ignored = [
            channel.mention for channel_id in config.get("ignored_channels", [])
            if (channel := self.guild.get_channel(int(channel_id)))
        ]
        embed.add_field(
            name="Primary channel",
            value=channel_text(self.guild, config["channel"]),
            inline=False
        )
        embed.add_field(
            name="Security",
            value="Enabled" if config["secure"] else "Disabled",
            inline=True
        )
        embed.add_field(
            name="Disabled events",
            value=f"`{len(config.get('disabled', []))}`",
            inline=True
        )
        embed.add_field(
            name="Ignored channels",
            value=", ".join(ignored) if ignored else "No channels",
            inline=False
        )
        local_history = local_history_config(config)
        local_history_enabled = bool(local_history["enabled"])
        message_recovery_enabled = bool(
            local_history_enabled and local_history["cache_messages"]
        )
        attachment_storage_enabled = bool(
            local_history_enabled and local_history["store_attachments"]
        )
        storage_limit_gb = MAX_GUILD_BYTES / (1024 ** 3)
        message_attachment_limit_mb = MAX_MESSAGE_ATTACHMENT_BYTES / (1024 ** 2)
        embed.add_field(
            name="Searchable local history",
            value=(
                f"Status: **{'On' if local_history_enabled else 'Off'}**\n"
                f"Keep records for: `{local_history['retention_days']} days`\n"
                f"Recover uncached edits and deletions: "
                f"`{'On' if message_recovery_enabled else 'Off'}`\n"
                f"Storage limit: `{storage_limit_gb:g} GB per server` "
                "(oldest records are removed first)\n"
                "Attachment files: "
                f"`{'Stored' if attachment_storage_enabled else 'Metadata only'}`\n"
                f"Per-file limit: `{local_history['attachment_size_limit_mb']} MB`\n"
                f"Per-message safety limit: `{message_attachment_limit_mb:g} MB`\n"
                "Use **Edit and save JSON** below to change these options. "
                "Turning local history off stops new saves but does not erase "
                "records already kept; use `log archive clear` when you want "
                "to remove them."
            ),
            inline=False,
        )
        routes = config.get("channels", {})
        enabled_events = max(
            0,
            len(self.logger_cog.log_types) - len(config.get("disabled", [])),
        )
        embed.add_field(
            name="Current logging setup",
            value=(
                f"Theme: `{config.get('theme') or 'Colors by activity'}`\n"
                f"Events enabled: `{enabled_events}/{len(self.logger_cog.log_types)}`\n"
                f"Custom destinations: `{len(routes)}`"
            ),
            inline=False,
        )
        json_preview = json.dumps(
            editable_logging_config(config),
            indent=2,
            ensure_ascii=False,
        )
        if len(json_preview) > 850:
            json_preview = f"{json_preview[:847].rstrip()}â€¦"
        embed.add_field(
            name="Current JSON preview",
            value=f"```json\n{json_preview}\n```",
            inline=False,
        )

    async def save_prefix(self, prefix: str, override: bool) -> None:
        await self.bot.aio_mongo["GuildPrefixes"].update_one(
            {"_id": self.guild.id},
            {"$set": {"prefix": prefix, "override": override}},
            upsert=True
        )
        self.bot.guild_prefixes[self.guild.id] = {
            "prefix": prefix,
            "override": override
        }

    async def save_messages(self, key: str, value) -> None:
        cog = self.messages_cog
        if not cog:
            return
        config = self.messages_config()
        config[key] = value
        cog.config[self.guild.id] = config
        await cog.config.flush()

    async def save_logger(self, key: str, value) -> None:
        cog = self.logger_cog
        config = self.logger_config()
        if not cog or config is None:
            return
        config[key] = value
        await cog.save_data()

    async def handle_channel_select(
        self,
        setting: str,
        channels: list,
        interaction: discord.Interaction
    ) -> None:
        channel_ids = [channel.id for channel in channels]
        destinations = {
            "warns_channel",
            "level_up_messages",
            "redirect_mod_commands",
            "logger_channel",
        }
        if setting in destinations and channel_ids:
            channel = self.guild.get_channel(channel_ids[0])
            permissions = channel.permissions_for(self.guild.me) if channel else None
            if not permissions or not permissions.send_messages:
                return await interaction.response.send_message(
                    "I can't send messages in that channel.",
                    ephemeral=True
                )
        if setting.startswith("logger_"):
            logger = self.logger_config()
            if (
                logger
                and logger.get("secure")
                and interaction.user.id != self.guild.owner_id
            ):
                return await interaction.response.send_message(
                    "Only the server owner can edit a secured logger.",
                    ephemeral=True
                )
        if setting in {"warns_channel", "disabled_channels"}:
            config = self.general
            config[setting] = channel_ids if setting == "disabled_channels" else (
                channel_ids[0] if channel_ids else None
            )
            await self.cog.save_config(self.guild.id, config)
        elif setting in {"level_up_messages", "redirect_mod_commands"}:
            await self.save_messages(setting, channel_ids[0] if channel_ids else False)
        elif setting == "xp_disabled_channels":
            ranking = await self.ranking_config()
            if ranking is not None:
                ranking["disabled_channels"] = channel_ids
                await ranking.save()
        elif setting == "logger_channel" and channel_ids:
            await self.save_logger("channel", channel_ids[0])
        elif setting == "logger_ignored_channels":
            await self.save_logger("ignored_channels", channel_ids)

        await self.refresh()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def handle_action(
        self,
        action: str,
        interaction: discord.Interaction
    ) -> None:
        if action == "close":
            self.stop()
            return await interaction.response.edit_message(embed=self.embed, view=None)
        if action == "home":
            self.category = "overview"
        elif action == "refresh":
            pass
        elif action == "edit_general":
            return await interaction.response.send_modal(GeneralSettingsModal(self))
        elif action == "edit_ranking":
            config = await self.ranking_config()
            if config:
                return await interaction.response.send_modal(
                    RankingSettingsModal(self, config)
                )
        elif action == "toggle_personal_prefixes":
            config = self.prefix_config()
            await self.save_prefix(config["prefix"], not config["override"])
        elif action == "toggle_purge_confirmation":
            config = self.general
            config["purge_confirmation"] = not config["purge_confirmation"]
            await self.cog.save_config(self.guild.id, config)
        elif action == "level_current":
            await self.save_messages("level_up_messages", True)
        elif action == "level_disable":
            await self.save_messages("level_up_messages", False)
        elif action == "mod_disable":
            await self.save_messages("redirect_mod_commands", False)
        elif action in {"enable_logger", "disable_logger"}:
            return await self.toggle_logger(action == "enable_logger", interaction)
        elif action == "edit_logger_json":
            config = self.logger_config()
            if config:
                try:
                    modal = LoggingJsonModal(self, config)
                except ValueError:
                    return await interaction.response.send_message(
                        "This Logging configuration is too large for Discord's editor. Use the web dashboard instead.",
                        ephemeral=True,
                    )
                return await interaction.response.send_modal(modal)
        elif action == "toggle_logger_security":
            if interaction.user.id != self.guild.owner_id:
                return await interaction.response.send_message(
                    "Only the server owner can change logger security.",
                    ephemeral=True
                )
            config = self.logger_config()
            if config:
                await self.save_logger("secure", not config["secure"])

        await self.refresh()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def toggle_logger(
        self,
        enable: bool,
        interaction: discord.Interaction
    ) -> None:
        logger = self.logger_cog
        if logger is None:
            return await interaction.response.send_message(
                "The Logger module is currently unavailable.",
                ephemeral=True
            )

        config = self.logger_config()
        if enable and config is not None:
            return await interaction.response.send_message(
                "Logger is already enabled.", ephemeral=True
            )
        if not enable and config is None:
            return await interaction.response.send_message(
                "Logger is already disabled.", ephemeral=True
            )
        if (
            not enable
            and config.get("secure")
            and interaction.user.id != self.guild.owner_id
        ):
            return await interaction.response.send_message(
                "Only the server owner can disable a secured logger.",
                ephemeral=True
            )

        await interaction.response.defer()
        if enable:
            try:
                channel = await self.guild.create_text_channel(
                    name="discord-log",
                    reason=f"Logger enabled by {interaction.user}"
                )
            except discord.Forbidden:
                return await interaction.followup.send(
                    "I need Manage Channels permission to enable the logger.",
                    ephemeral=True
                )
            except discord.HTTPException:
                return await interaction.followup.send(
                    "I couldn't create the logger channel. Please try again.",
                    ephemeral=True
                )

            permissions = channel.permissions_for(self.guild.me)
            missing = [
                name.replace("_", " ").title()
                for name in ("send_messages", "embed_links", "attach_files")
                if not getattr(permissions, name)
            ]
            if missing:
                return await interaction.followup.send(
                    f"I created {channel.mention}, but I still need: "
                    f"{', '.join(missing)}.",
                    ephemeral=True
                )
            await logger.enable_for_guild(self.guild.id, channel.id)
            status = f"Logger enabled in {channel.mention}."
            audit_message = f"!on **Logger** - `{self.guild}`"
            audit_color = colors.green
        else:
            await logger.disable_for_guild(self.guild.id)
            status = "Logger disabled. Its log channels were left intact."
            audit_message = f"!off **Logger** - `{self.guild}`"
            audit_color = colors.red

        await self.bot.create_log(
            message=audit_message,
            channel="module_log",
            embedded=True,
            color=audit_color
        )
        await self.refresh()
        await interaction.edit_original_response(embed=self.embed, view=self)
        await interaction.followup.send(status, ephemeral=True)


class CategorySelect(ui.Select):
    def __init__(self, menu: SettingsMenu):
        self.menu = menu
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                description=description,
                emoji=emoji,
                default=value == menu.category
            )
            for value, (label, description, emoji) in CATEGORIES.items()
        ]
        super().__init__(
            placeholder="Choose a settings category",
            options=options,
            row=0
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.menu.category = self.values[0]
        await self.menu.refresh()
        await interaction.response.edit_message(
            embed=self.menu.embed,
            view=self.menu
        )


class LanguageSelect(ui.Select):
    def __init__(self, menu: SettingsMenu):
        self.menu = menu
        current = menu.cog.get_language(menu.guild.id)
        options = [
            discord.SelectOption(
                label=LANGUAGES[language],
                value=language,
                emoji=LANGUAGE_EMOJIS[language],
                default=language == current,
            )
            for language in ranked_language_codes(menu.bot.guilds)
        ]
        super().__init__(
            placeholder="Choose Fate's language for this server",
            options=options,
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        # Acknowledge immediately; the first translation for a language may need
        # a network request before the edited settings message is ready.
        await interaction.response.defer()
        config = self.menu.general
        config["language"] = normalize_language(self.values[0])
        await self.menu.cog.save_config(self.menu.guild.id, config)
        await self.menu.refresh()
        await interaction.edit_original_response(
            embed=self.menu.embed,
            view=self.menu,
        )


class SettingsChannelSelect(ui.ChannelSelect):
    def __init__(
        self,
        menu: SettingsMenu,
        setting: str,
        *,
        placeholder: str,
        row: int,
        max_values: int = 1,
        defaults: Optional[list] = None
    ):
        self.menu = menu
        self.setting = setting
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=placeholder,
            min_values=0,
            max_values=max_values,
            default_values=defaults or [],
            row=row
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu.handle_channel_select(
            self.setting,
            list(self.values),
            interaction
        )


class SettingsButton(ui.Button):
    def __init__(
        self,
        menu: SettingsMenu,
        action: str,
        label: str,
        emoji: str,
        *,
        row: int,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False
    ):
        self.menu = menu
        self.action = action
        super().__init__(
            label=label,
            emoji=emoji,
            row=row,
            style=style,
            disabled=disabled
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu.handle_action(self.action, interaction)


class LoggingJsonModal(ui.Modal, title="Edit and save Logging JSON"):
    def __init__(self, menu: SettingsMenu, config: dict):
        self.menu = menu
        editable = editable_logging_config(config)
        serialized = json.dumps(editable, indent=2, ensure_ascii=False)
        if len(serialized) > 4000:
            serialized = json.dumps(editable, separators=(",", ":"), ensure_ascii=False)
        if len(serialized) > 4000:
            raise ValueError("This Logging configuration is too large for a Discord editor.")
        self.configuration = ui.TextInput(
            label="Current Logging JSON",
            style=discord.TextStyle.paragraph,
            default=serialized,
            min_length=2,
            max_length=4000,
        )
        super().__init__()
        self.add_item(self.configuration)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            raw = json.loads(self.configuration.value)
            settings = validate_module_settings(
                "logger",
                {"enabled": True, "configuration": raw},
            )
            config = settings["configuration"]
        except (json.JSONDecodeError, ValidationError) as error:
            return await interaction.response.send_message(
                f"That Logging JSON cannot be saved: {error}",
                ephemeral=True,
            )

        logger = self.menu.logger_cog
        current = self.menu.logger_config()
        if logger is None or current is None:
            return await interaction.response.send_message(
                "Logging is no longer available for this server.",
                ephemeral=True,
            )
        if (
            interaction.user.id != self.menu.guild.owner_id
            and (current.get("secure") or config.get("secure"))
        ):
            return await interaction.response.send_message(
                "Only the server owner can edit secured Logging settings.",
                ephemeral=True,
            )

        known_events = set(logger.log_types)
        configured_events = (
            set(config["channels"])
            | set(config["disabled"])
            | set(config.get("colors", {}))
        )
        unknown = configured_events - known_events
        if unknown:
            return await interaction.response.send_message(
                f"That Logging event is not available: {sorted(unknown)[0]}",
                ephemeral=True,
            )

        primary = self.menu.guild.get_channel(config["channel"])
        if primary is None:
            return await interaction.response.send_message(
                "The primary Logging channel is not available in this server.",
                ephemeral=True,
            )
        primary_permissions = primary.permissions_for(self.menu.guild.me)
        if not all(
            getattr(primary_permissions, permission, False)
            for permission in ("send_messages", "embed_links", "attach_files")
        ):
            return await interaction.response.send_message(
                "Fate needs Send Messages, Embed Links, and Attach Files in the primary Logging channel.",
                ephemeral=True,
            )

        old_routes = current.get("channels", {})
        for event, destination_id in config["channels"].items():
            if old_routes.get(event) == destination_id:
                continue
            destination = self.menu.guild.get_channel(destination_id)
            if destination is None:
                destination = self.menu.guild.get_thread(destination_id)
            if destination is None:
                return await interaction.response.send_message(
                    f"The destination for {event} is not an available channel or thread.",
                    ephemeral=True,
                )
            if not destination.permissions_for(self.menu.guild.me).send_messages:
                return await interaction.response.send_message(
                    f"Fate cannot send {event} records to that destination.",
                    ephemeral=True,
                )

        old_history = local_history_config(current)
        new_history = local_history_config(config)
        logger.config[str(self.menu.guild.id)] = config
        await logger.save_data()
        prune_warning = None
        if new_history["retention_days"] < old_history["retention_days"]:
            local_archive = getattr(logger, "local_archive", None)
            if local_archive is not None:
                try:
                    await local_archive.prune_guild(
                        str(self.menu.guild.id),
                        new_history["retention_days"],
                    )
                except Exception as error:
                    logger.bot.log.critical(
                        "Failed to apply a lowered local Logging history "
                        f"retention for guild {self.menu.guild.id}: {error}"
                    )
                    prune_warning = (
                        " The new retention is saved, but Fate could not remove "
                        "older local records yet."
                    )
        await interaction.response.defer()
        await self.menu.refresh_message()
        await interaction.followup.send(
            f"Logging JSON saved.{prune_warning or ''}",
            ephemeral=True,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        await self.menu.on_error(interaction, error, self)


class GeneralSettingsModal(ui.Modal, title="General bot settings"):
    prefix = ui.TextInput(
        label="Server prefix",
        min_length=1,
        max_length=5
    )
    purge_limit = ui.TextInput(
        label="Maximum purge amount",
        min_length=1,
        max_length=4
    )

    def __init__(self, menu: SettingsMenu):
        self.menu = menu
        super().__init__()
        self.prefix.default = menu.prefix_config()["prefix"]
        self.purge_limit.default = str(menu.general["purge_limit"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        prefix = self.prefix.value.strip()
        if not prefix or any(character.isspace() for character in prefix):
            return await interaction.response.send_message(
                "The prefix must be 1–5 characters and cannot contain spaces.",
                ephemeral=True
            )
        try:
            purge_limit = int(self.purge_limit.value)
        except ValueError:
            purge_limit = 0
        if not 1 <= purge_limit <= MAX_PURGE_LIMIT:
            return await interaction.response.send_message(
                "The purge limit must be between 1 and 3,000.",
                ephemeral=True
            )

        prefix_config = self.menu.prefix_config()
        await self.menu.save_prefix(prefix, prefix_config["override"])
        config = self.menu.general
        config["purge_limit"] = purge_limit
        await self.menu.cog.save_config(self.menu.guild.id, config)
        await interaction.response.defer()
        await self.menu.refresh_message()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception
    ) -> None:
        await self.menu.on_error(interaction, error, self)


class RankingSettingsModal(ui.Modal, title="Ranking settings"):
    min_xp = ui.TextInput(label="Minimum XP per message", max_length=3)
    max_xp = ui.TextInput(label="Maximum XP per message", max_length=3)
    first_level = ui.TextInput(label="XP required for the first level", max_length=4)
    timeframe = ui.TextInput(label="Rate-limit timeframe in seconds", max_length=4)
    message_limit = ui.TextInput(label="Messages allowed in that timeframe", max_length=4)

    def __init__(self, menu: SettingsMenu, config: dict):
        self.menu = menu
        super().__init__()
        self.min_xp.default = str(config["min_xp_per_msg"])
        self.max_xp.default = str(config["max_xp_per_msg"])
        self.first_level.default = str(config["first_lvl_xp_req"])
        self.timeframe.default = str(config["timeframe"])
        self.message_limit.default = str(config["msgs_within_timeframe"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            minimum = int(self.min_xp.value)
            maximum = int(self.max_xp.value)
            first_level = int(self.first_level.value)
            timeframe = int(self.timeframe.value)
            message_limit = int(self.message_limit.value)
        except ValueError:
            return await interaction.response.send_message(
                "Every ranking setting must be a whole number.",
                ephemeral=True
            )

        if not 0 <= minimum <= maximum <= 100:
            return await interaction.response.send_message(
                "XP must satisfy `0 ≤ minimum ≤ maximum ≤ 100`.",
                ephemeral=True
            )
        if not 100 <= first_level <= 2500:
            return await interaction.response.send_message(
                "First-level XP must be between 100 and 2,500.",
                ephemeral=True
            )
        if not 1 <= timeframe <= 3600 or not 1 <= message_limit <= 3600:
            return await interaction.response.send_message(
                "The timeframe and message limit must be between 1 and 3,600.",
                ephemeral=True
            )

        cog = self.menu.ranking_cog
        config = await cog.config[self.menu.guild.id] or deepcopy(cog.default_config)
        config.update({
            "min_xp_per_msg": minimum,
            "max_xp_per_msg": maximum,
            "first_lvl_xp_req": first_level,
            "timeframe": timeframe,
            "msgs_within_timeframe": message_limit,
        })
        await config.save()
        await interaction.response.defer()
        await self.menu.refresh_message()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception
    ) -> None:
        await self.menu.on_error(interaction, error, self)


async def setup(bot):
    await bot.add_cog(Settings(bot), override=True)
