"""
cogs.moderation.verification
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Modern, component-driven member verification and configuration.

:copyright: (C) 2020-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
from contextlib import suppress
from typing import Optional

import discord
from discord import Forbidden, HTTPException, NotFound, app_commands, ui
from discord.ext import commands

from botutils import colors, extract_time, get_time
from fate import Fate

MIN_TIME_LIMIT = 30
MAX_TIME_LIMIT = 10 * 60
PANEL_CUSTOM_ID = "fate:verification:start"

DEFAULT_CONFIG = {
    "channel_id": None,
    "verified_role_id": None,
    "temp_role_id": None,
    "delete_after": True,
    "log_channel": None,
    "kick_on_fail": False,
    "auto_start": False,
    "time_limit": 45,
    "panel_message_id": None,
}


async def respond_ephemeral(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def channel_default(channel: Optional[discord.abc.GuildChannel]) -> list:
    return [channel] if channel else []


def role_default(role: Optional[discord.Role]) -> list:
    return [role] if role else []


class Verification(commands.Cog):
    """Give every server a clean, shared entry panel and private captcha flow."""

    def __init__(self, bot: Fate):
        self.bot = bot
        self.config = bot.utils.cache("verification")
        self.active_challenges: dict[tuple[int, int], asyncio.Task] = {}
        self.background_tasks: set[asyncio.Task] = set()
        self.entry_view = VerificationEntryView(self)
        self.panel_sync_task: Optional[asyncio.Task] = None

        changed = False
        for config in self.config.values():
            changed = self.normalize_config(config) or changed
        if changed:
            self.track_background(asyncio.create_task(self.config.flush()))

    async def cog_load(self) -> None:
        self.bot.add_view(self.entry_view)

    async def cog_unload(self) -> None:
        self.entry_view.stop()
        if self.panel_sync_task:
            self.panel_sync_task.cancel()
        for task in {*self.background_tasks, *self.active_challenges.values()}:
            task.cancel()

    def track_background(self, task: asyncio.Task) -> asyncio.Task:
        self.background_tasks.add(task)

        def finished(completed: asyncio.Task) -> None:
            self.background_tasks.discard(completed)
            if completed.cancelled():
                return
            exception = completed.exception()
            if exception:
                self.bot.loop.call_exception_handler(
                    {
                        "message": "Unhandled verification background task error",
                        "exception": exception,
                        "task": completed,
                    }
                )

        task.add_done_callback(finished)
        return task

    @staticmethod
    def normalize_config(config: dict) -> bool:
        changed = False
        for key, default in DEFAULT_CONFIG.items():
            if key not in config:
                config[key] = default
                changed = True

        for key in (
            "channel_id",
            "verified_role_id",
            "temp_role_id",
            "log_channel",
            "panel_message_id",
        ):
            value = config.get(key)
            if value is not None:
                try:
                    normalized = int(value)
                except (TypeError, ValueError):
                    normalized = None
                if normalized != value:
                    config[key] = normalized
                    changed = True

        try:
            limit = int(config.get("time_limit", DEFAULT_CONFIG["time_limit"]))
        except (TypeError, ValueError):
            limit = DEFAULT_CONFIG["time_limit"]
        limit = min(MAX_TIME_LIMIT, max(MIN_TIME_LIMIT, limit))
        if config.get("time_limit") != limit:
            config["time_limit"] = limit
            changed = True

        for key in ("delete_after", "kick_on_fail", "auto_start"):
            value = bool(config.get(key, DEFAULT_CONFIG[key]))
            if config.get(key) is not value:
                config[key] = value
                changed = True
        return changed

    def is_enabled(self, guild_id: int) -> bool:
        return guild_id in self.config

    def get_config(self, guild_id: int) -> Optional[dict]:
        config = self.config.get(guild_id)
        if config is not None:
            self.normalize_config(config)
        return config

    def is_entry_panel(self, message: discord.Message) -> bool:
        if not self.bot.user or message.author.id != self.bot.user.id:
            return False

        components = list(message.components)
        while components:
            component = components.pop()
            if getattr(component, "custom_id", None) == PANEL_CUSTOM_ID:
                return True
            components.extend(getattr(component, "children", None) or ())
        return False

    @staticmethod
    def validate_role(
        guild: discord.Guild,
        role: discord.Role,
        actor: Optional[discord.Member] = None,
    ) -> Optional[str]:
        if role.is_default():
            return "The everyone role cannot be used for verification."
        if role.managed:
            return "That role is managed by an integration and cannot be changed."
        if role.position >= guild.me.top_role.position:
            return "That role must be below Fate's highest role."
        if (
            actor
            and actor.id != guild.owner_id
            and role.position >= actor.top_role.position
        ):
            return "That role must be below your highest role."
        return None

    @staticmethod
    def channel_permission_error(channel: discord.TextChannel) -> Optional[str]:
        permissions = channel.permissions_for(channel.guild.me)
        required = (
            "view_channel",
            "send_messages",
            "attach_files",
            "read_message_history",
            "manage_messages",
        )
        missing = [
            permission.replace("_", " ").title()
            for permission in required
            if not getattr(permissions, permission)
        ]
        if missing:
            return f"Fate needs {', '.join(missing)} in {channel.mention}."
        return None

    @staticmethod
    def log_channel_permission_error(channel: discord.TextChannel) -> Optional[str]:
        permissions = channel.permissions_for(channel.guild.me)
        missing = [
            permission.replace("_", " ").title()
            for permission in ("view_channel", "send_messages", "embed_links")
            if not getattr(permissions, permission)
        ]
        if missing:
            return f"Fate needs {', '.join(missing)} in {channel.mention}."
        return None

    def configuration_warnings(self, guild: discord.Guild, config: dict) -> list[str]:
        warnings = []
        channel = guild.get_channel(config.get("channel_id") or 0)
        verified_role = guild.get_role(config.get("verified_role_id") or 0)
        temp_role = guild.get_role(config.get("temp_role_id") or 0)
        log_channel = guild.get_channel(config.get("log_channel") or 0)

        if not isinstance(channel, discord.TextChannel):
            warnings.append("Choose a valid verification channel.")
        elif error := self.channel_permission_error(channel):
            warnings.append(error)
        if not verified_role:
            warnings.append("Choose a valid verified role.")
        elif error := self.validate_role(guild, verified_role):
            warnings.append(error)
        if config.get("temp_role_id") and not temp_role:
            warnings.append(
                "The restricted role was deleted; choose another or clear it."
            )
        elif temp_role and temp_role.id == config.get("verified_role_id"):
            warnings.append("The restricted and verified roles must be different.")
        elif temp_role and (error := self.validate_role(guild, temp_role)):
            warnings.append(error)
        if config.get("log_channel") and not isinstance(
            log_channel, discord.TextChannel
        ):
            warnings.append(
                "The activity log channel was deleted; choose another or clear it."
            )
        elif isinstance(log_channel, discord.TextChannel) and (
            error := self.log_channel_permission_error(log_channel)
        ):
            warnings.append(error)
        if not guild.me.guild_permissions.manage_roles:
            warnings.append("Fate needs Manage Roles to finish verification.")
        if config.get("kick_on_fail") and not guild.me.guild_permissions.kick_members:
            warnings.append(
                "Kick on timeout is on, but Fate does not have Kick Members."
            )
        return warnings

    async def resolve_text_channel(
        self, guild: discord.Guild, channel_id: Optional[int]
    ) -> Optional[discord.TextChannel]:
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        with suppress(NotFound, Forbidden, HTTPException):
            fetched = await self.bot.fetch_channel(channel_id)
            if (
                isinstance(fetched, discord.TextChannel)
                and fetched.guild.id == guild.id
            ):
                return fetched
        return None

    async def publish_panel(self, guild: discord.Guild) -> str:
        config = self.get_config(guild.id)
        if not config:
            return "Verification is disabled."
        channel = await self.resolve_text_channel(guild, config.get("channel_id"))
        if not channel:
            return "The verification channel is missing."
        if error := self.channel_permission_error(channel):
            return error

        view = VerificationEntryView(self, guild)
        message = None
        panel_id = config.get("panel_message_id")
        if panel_id:
            with suppress(NotFound, Forbidden, HTTPException):
                message = await channel.fetch_message(panel_id)

        panels = []
        with suppress(Forbidden, HTTPException):
            panels.extend([
                candidate
                async for candidate in channel.history(limit=200)
                if self.is_entry_panel(candidate)
            ])

        if message is None and panels:
            message = panels[0]
        if message is not None:
            message = await message.edit(
                content=None,
                embeds=[],
                attachments=[],
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if message is None:
            message = await channel.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        for duplicate in panels:
            if duplicate.id != message.id:
                with suppress(NotFound, Forbidden, HTTPException):
                    await duplicate.delete()

        if config.get("panel_message_id") != message.id:
            config["panel_message_id"] = message.id
            await self.config.flush()
        return f"Verification panel ready in {channel.mention}."

    async def delete_panel(self, guild: discord.Guild, config: dict) -> None:
        channel = await self.resolve_text_channel(guild, config.get("channel_id"))
        panel_id = config.get("panel_message_id")
        if channel and panel_id:
            with suppress(NotFound, Forbidden, HTTPException):
                await channel.get_partial_message(panel_id).delete()

    async def disable_for_guild(self, guild: discord.Guild) -> bool:
        config = self.get_config(guild.id)
        if not config:
            return False
        snapshot = dict(config)
        for key, task in list(self.active_challenges.items()):
            if key[0] == guild.id:
                task.cancel()
                self.active_challenges.pop(key, None)
        await self.config.remove(guild.id)
        await self.delete_panel(guild, snapshot)
        await self.bot.create_log(
            message=f"!off **Verification** - `{guild}`",
            channel="module_log",
            embedded=True,
            color="red",
        )
        return True

    async def log_result(
        self,
        member: discord.Member,
        outcome: str,
        detail: Optional[str] = None,
    ) -> None:
        config = self.get_config(member.guild.id)
        if not config:
            return
        channel = await self.resolve_text_channel(
            member.guild, config.get("log_channel")
        )
        if not channel:
            return

        palette = {
            "verified": colors.green,
            "timed out": colors.red,
            "kicked": colors.red,
            "error": colors.orange,
        }
        embed = discord.Embed(
            title=f"Verification {outcome.title()}",
            description=member.mention,
            color=palette.get(outcome, colors.fate),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member", value=f"{member}\n`{member.id}`", inline=True)
        if detail:
            embed.add_field(name="Details", value=detail[:1024], inline=False)
        with suppress(Forbidden, HTTPException):
            await channel.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )

    async def start_verification(
        self,
        member: discord.Member,
        *,
        interaction: Optional[discord.Interaction] = None,
    ) -> Optional[bool]:
        config = self.get_config(member.guild.id)
        if not config:
            if interaction:
                await respond_ephemeral(
                    interaction, "Verification is not enabled here."
                )
            return None

        verified_role = member.guild.get_role(config.get("verified_role_id") or 0)
        if not verified_role:
            if interaction:
                await respond_ephemeral(
                    interaction,
                    "Verification needs moderator attention because its verified role is missing.",
                )
            return None
        if verified_role in member.roles:
            if interaction:
                await respond_ephemeral(interaction, "You're already verified.")
            return True

        key = (member.guild.id, member.id)
        active = self.active_challenges.get(key)
        if active and not active.done():
            if interaction:
                await respond_ephemeral(
                    interaction,
                    "You already have an active challenge. Finish it or wait for it to expire.",
                )
            return None

        current = asyncio.current_task()
        if current:
            self.active_challenges[key] = current
        try:
            return await self.verify_member(member, config, interaction=interaction)
        finally:
            if self.active_challenges.get(key) is current:
                self.active_challenges.pop(key, None)

    async def verify_member(
        self,
        member: discord.Member,
        config: dict,
        *,
        interaction: Optional[discord.Interaction] = None,
    ) -> Optional[bool]:
        channel = await self.resolve_text_channel(
            member.guild, config.get("channel_id")
        )
        if not channel:
            if interaction:
                await respond_ephemeral(
                    interaction,
                    "Verification needs moderator attention because its channel is missing.",
                )
            return None

        try:
            passed = await self.bot.utils.verify_user(
                channel=channel,
                user=member,
                delete_after=config["delete_after"],
                timeout=config["time_limit"],
                interaction=interaction,
            )
        except (Forbidden, HTTPException):
            if interaction:
                await respond_ephemeral(
                    interaction,
                    "I couldn't open the captcha. Please ask a moderator to check my channel permissions.",
                )
            return None

        # A moderator may disable or reconfigure the module while a challenge is open.
        current_config = self.get_config(member.guild.id)
        if not current_config:
            return None
        verified_role = member.guild.get_role(
            current_config.get("verified_role_id") or 0
        )
        if not verified_role:
            return None

        if passed:
            try:
                if verified_role not in member.roles:
                    await member.add_roles(
                        verified_role, reason="Completed Fate verification"
                    )
            except Forbidden:
                await self.log_result(
                    member, "error", "Fate could not assign the verified role."
                )
                if interaction:
                    await respond_ephemeral(
                        interaction,
                        "Your code was correct, but I couldn't assign the verified role. A moderator needs to fix my role permissions.",
                    )
                return False

            temp_role = member.guild.get_role(current_config.get("temp_role_id") or 0)
            if temp_role and temp_role in member.roles:
                with suppress(Forbidden, HTTPException):
                    await member.remove_roles(
                        temp_role, reason="Completed Fate verification"
                    )

            await self.log_result(member, "verified", "Captcha completed successfully.")
            welcome = self.bot.get_cog("Welcome")
            if welcome:
                welcome_config = welcome.config.get(member.guild.id)
                if welcome_config and welcome_config.get("wait_for_verify"):
                    await welcome.on_member_join(member, just_verified=True)
            self.bot.dispatch("member_verified", member)
            return True

        kicked = False
        if current_config.get("kick_on_fail"):
            try:
                await member.kick(reason="Fate verification timed out")
            except (Forbidden, HTTPException):
                await self.log_result(
                    member,
                    "error",
                    "Captcha expired, but Fate could not kick the member.",
                )
            else:
                kicked = True
        await self.log_result(
            member,
            "kicked" if kicked else "timed out",
            "The captcha expired before a correct code was submitted.",
        )
        return False

    def launch_join_challenge(self, member: discord.Member) -> None:
        self.track_background(asyncio.create_task(self.start_verification(member)))

    async def test_challenge(self, interaction: discord.Interaction) -> None:
        config = self.get_config(interaction.guild_id)
        if not config:
            return await respond_ephemeral(interaction, "Set up verification first.")
        await self.bot.utils.verify_user(
            channel=interaction.channel,
            user=interaction.user,
            timeout=config["time_limit"],
            delete_after=False,
            reason=f"{interaction.user.mention}, this is a test. No roles or moderation actions will change.",
            interaction=interaction,
        )

    @commands.hybrid_group(
        name="verification",
        fallback="view",
        invoke_without_command=True,
        description="Opens the verification control panel",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def verification(self, ctx: commands.Context):
        """Open the component-based verification control panel."""
        await VerificationDashboard(self, ctx).start()

    @verification.command(
        name="enable", description="Opens the guided verification setup"
    )
    @commands.has_permissions(manage_guild=True)
    async def _enable(self, ctx: commands.Context):
        menu = VerificationDashboard(self, ctx)
        if self.is_enabled(ctx.guild.id):
            menu.notice = "Verification is already enabled; edit it below."
        else:
            menu.notice = (
                "Select Set up verification to configure everything in one form."
            )
        await menu.start()

    @verification.command(
        name="disable", description="Disables verification and removes its panel"
    )
    @commands.has_permissions(manage_guild=True)
    async def _disable(self, ctx: commands.Context):
        if await self.disable_for_guild(ctx.guild):
            await ctx.send(
                "Verification is disabled and its entry panel was removed.",
                ephemeral=bool(ctx.interaction),
            )
        else:
            await ctx.send(
                "Verification is already disabled.", ephemeral=bool(ctx.interaction)
            )

    @verification.command(
        name="set-channel", description="Changes the verification channel"
    )
    @commands.has_permissions(manage_guild=True)
    async def _set_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        config = self.get_config(ctx.guild.id)
        if not config:
            return await ctx.send(
                "Set up verification first.", ephemeral=bool(ctx.interaction)
            )
        if error := self.channel_permission_error(channel):
            return await ctx.send(error, ephemeral=bool(ctx.interaction))
        old = dict(config)
        changed_channel = config.get("channel_id") != channel.id
        config["channel_id"] = channel.id
        if changed_channel:
            config["panel_message_id"] = None
        await self.config.flush()
        status = await self.publish_panel(ctx.guild)
        if changed_channel:
            await self.delete_panel(ctx.guild, old)
        await ctx.send(status, ephemeral=bool(ctx.interaction))

    @verification.command(
        name="set-limit", description="Changes how long a captcha stays open"
    )
    @commands.has_permissions(manage_guild=True)
    async def set_limit(self, ctx: commands.Context, limit: str):
        config = self.get_config(ctx.guild.id)
        if not config:
            return await ctx.send(
                "Set up verification first.", ephemeral=bool(ctx.interaction)
            )
        seconds = int(limit) if limit.isdecimal() else extract_time(limit)
        if not seconds or not MIN_TIME_LIMIT <= seconds <= MAX_TIME_LIMIT:
            return await ctx.send(
                f"Choose a time between {get_time(MIN_TIME_LIMIT)} and {get_time(MAX_TIME_LIMIT)}.",
                ephemeral=bool(ctx.interaction),
            )
        config["time_limit"] = seconds
        await self.config.flush()
        await ctx.send(
            f"Captchas now stay open for {get_time(seconds)}.",
            ephemeral=bool(ctx.interaction),
        )

    @verification.command(
        name="set-verified-role",
        description="Changes the role given after verification",
    )
    @commands.has_permissions(manage_guild=True)
    async def _set_verified_role(self, ctx: commands.Context, role: discord.Role):
        config = self.get_config(ctx.guild.id)
        if not config:
            return await ctx.send(
                "Set up verification first.", ephemeral=bool(ctx.interaction)
            )
        if error := self.validate_role(ctx.guild, role, ctx.author):
            return await ctx.send(error, ephemeral=bool(ctx.interaction))
        config["verified_role_id"] = role.id
        await self.config.flush()
        await ctx.send(
            f"Verified members will receive {role.mention}.",
            ephemeral=bool(ctx.interaction),
        )

    @verification.command(
        name="set-temp-role", description="Changes or clears the restricted role"
    )
    @commands.has_permissions(manage_guild=True)
    async def _set_temp_role(
        self, ctx: commands.Context, role: Optional[discord.Role] = None
    ):
        config = self.get_config(ctx.guild.id)
        if not config:
            return await ctx.send(
                "Set up verification first.", ephemeral=bool(ctx.interaction)
            )
        if role and (error := self.validate_role(ctx.guild, role, ctx.author)):
            return await ctx.send(error, ephemeral=bool(ctx.interaction))
        if role and role.id == config.get("verified_role_id"):
            return await ctx.send(
                "The restricted and verified roles must be different.",
                ephemeral=bool(ctx.interaction),
            )
        config["temp_role_id"] = role.id if role else None
        await self.config.flush()
        message = (
            f"New members will start with {role.mention}."
            if role
            else "The restricted role was cleared."
        )
        await ctx.send(message, ephemeral=bool(ctx.interaction))

    @verification.command(
        name="delete-after", description="Toggles deleting completed public captchas"
    )
    @commands.has_permissions(manage_guild=True)
    async def _delete_after(
        self, ctx: commands.Context, enabled: Optional[bool] = None
    ):
        await self.toggle_setting(
            ctx, "delete_after", enabled, "cleanup after completion"
        )

    @verification.command(
        name="kick", description="Toggles kicking members when a captcha expires"
    )
    @commands.has_permissions(manage_guild=True)
    async def _kick(self, ctx: commands.Context, enabled: Optional[bool] = None):
        await self.toggle_setting(ctx, "kick_on_fail", enabled, "kick on timeout")

    @verification.command(
        name="auto-start", description="Toggles opening a captcha when members join"
    )
    @commands.has_permissions(manage_guild=True)
    async def auto_start(self, ctx: commands.Context, enabled: Optional[bool] = None):
        await self.toggle_setting(ctx, "auto_start", enabled, "automatic challenges")

    @verification.command(
        name="log-channel",
        description="Changes or clears the verification activity log",
    )
    @commands.has_permissions(manage_guild=True)
    async def _log_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ):
        config = self.get_config(ctx.guild.id)
        if not config:
            return await ctx.send(
                "Set up verification first.", ephemeral=bool(ctx.interaction)
            )
        if channel and (error := self.log_channel_permission_error(channel)):
            return await ctx.send(error, ephemeral=bool(ctx.interaction))
        config["log_channel"] = channel.id if channel else None
        await self.config.flush()
        message = (
            f"Verification activity will be logged in {channel.mention}."
            if channel
            else "Verification activity logging is off."
        )
        await ctx.send(message, ephemeral=bool(ctx.interaction))

    async def toggle_setting(
        self,
        ctx: commands.Context,
        setting: str,
        enabled: Optional[bool],
        label: str,
    ) -> None:
        config = self.get_config(ctx.guild.id)
        if not config:
            return await ctx.send(
                "Set up verification first.", ephemeral=bool(ctx.interaction)
            )
        value = not config[setting] if enabled is None else enabled
        config[setting] = value
        await self.config.flush()
        await ctx.send(
            f"{label.title()} is now {'on' if value else 'off'}.",
            ephemeral=bool(ctx.interaction),
        )

    @commands.hybrid_command(
        name="verify", description="Opens your private verification challenge"
    )
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def verify(self, ctx: commands.Context):
        config = self.get_config(ctx.guild.id)
        if not config:
            return await ctx.send(
                "Verification isn't enabled here.", ephemeral=bool(ctx.interaction)
            )
        if ctx.interaction:
            return await self.start_verification(
                ctx.author, interaction=ctx.interaction
            )
        channel = await self.resolve_text_channel(ctx.guild, config.get("channel_id"))
        if channel and channel.id != ctx.channel.id:
            await ctx.send(
                f"Your challenge is opening in {channel.mention}.", delete_after=8
            )
        await self.start_verification(ctx.author)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.panel_sync_task and not self.panel_sync_task.done():
            return

        async def sync_panels():
            for guild_id in list(self.config.keys()):
                guild = self.bot.get_guild(guild_id)
                if guild:
                    with suppress(Forbidden, HTTPException):
                        await self.publish_panel(guild)

        self.panel_sync_task = self.track_background(asyncio.create_task(sync_panels()))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        config = self.get_config(message.guild.id)
        if not config or message.channel.id != config.get("channel_id"):
            return
        if (
            isinstance(message.author, discord.Member)
            and message.author.guild_permissions.manage_guild
        ):
            return
        await asyncio.sleep(4)
        with suppress(NotFound, Forbidden, HTTPException):
            await message.delete()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        config = self.get_config(member.guild.id)
        if not config:
            return
        temp_role = member.guild.get_role(config.get("temp_role_id") or 0)
        if temp_role and temp_role.id == config.get("verified_role_id"):
            temp_role = None
        if temp_role and temp_role not in member.roles:
            with suppress(Forbidden, HTTPException):
                await member.add_roles(temp_role, reason="Awaiting Fate verification")
        if config.get("auto_start"):
            self.launch_join_challenge(member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        task = self.active_challenges.pop((member.guild.id, member.id), None)
        if task:
            task.cancel()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        config = self.get_config(channel.guild.id)
        if not config:
            return
        changed = False
        if config.get("channel_id") == channel.id:
            config["channel_id"] = None
            config["panel_message_id"] = None
            changed = True
        if config.get("log_channel") == channel.id:
            config["log_channel"] = None
            changed = True
        if changed:
            await self.config.flush()

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        config = self.get_config(role.guild.id)
        if not config:
            return
        changed = False
        if config.get("verified_role_id") == role.id:
            config["verified_role_id"] = None
            changed = True
        if config.get("temp_role_id") == role.id:
            config["temp_role_id"] = None
            changed = True
        if changed:
            await self.config.flush()


class VerificationEntryView(ui.LayoutView):
    def __init__(self, cog: Verification, guild: Optional[discord.Guild] = None):
        super().__init__(timeout=None)
        self.cog = cog
        title = f"Welcome to {guild.name}" if guild else "Server verification"
        description = (
            "Verify once to unlock the rest of the server. Your captcha opens privately, "
            "so the channel stays clean."
        )
        children = []
        if guild and guild.icon:
            children.append(
                ui.Section(
                    ui.TextDisplay(f"## 🛡️ {title}"),
                    ui.TextDisplay(description),
                    accessory=ui.Thumbnail(
                        guild.icon.url, description=f"{guild.name} icon"
                    ),
                )
            )
        else:
            children.extend(
                (ui.TextDisplay(f"## 🛡️ {title}"), ui.TextDisplay(description))
            )
        children.extend(
            (
                ui.Separator(spacing=discord.SeparatorSpacing.small),
                ui.TextDisplay(
                    "Select **Verify me**, read the image, and enter its six-character code."
                ),
                ui.ActionRow(VerificationEntryButton(cog)),
            )
        )
        self.add_item(ui.Container(*children, accent_colour=colors.fate))


class VerificationEntryButton(ui.Button):
    def __init__(self, cog: Verification):
        super().__init__(
            label="Verify me",
            emoji="✨",
            style=discord.ButtonStyle.success,
            custom_id=PANEL_CUSTOM_ID,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            return await respond_ephemeral(
                interaction, "Verification only works inside a server."
            )
        await self.cog.start_verification(interaction.user, interaction=interaction)


class VerificationDashboard(ui.LayoutView):
    def __init__(self, cog: Verification, ctx: commands.Context):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.guild = ctx.guild
        self.user = ctx.author
        self.message: Optional[discord.Message] = None
        self.notice: Optional[str] = None

    async def start(self):
        self.rebuild()
        self.message = await self.ctx.send(
            view=self, ephemeral=bool(self.ctx.interaction)
        )

    def rebuild(self):
        self.clear_items()
        config = self.cog.get_config(self.guild.id)
        if not config:
            detail = (
                "Create a shared verification panel and private captcha flow. The setup form "
                "collects channels, roles, and recommended behavior in one place."
            )
            if self.notice:
                detail += f"\n\n> {self.notice}"
            container = ui.Container(
                ui.TextDisplay("## ✨ Verification setup"),
                ui.TextDisplay(detail),
                ui.Separator(),
                ui.ActionRow(
                    DashboardButton(
                        self,
                        "setup",
                        "Set up verification",
                        "🛠️",
                        discord.ButtonStyle.success,
                    )
                ),
                accent_colour=colors.fate,
            )
            self.add_item(container)
            return

        channel = self.guild.get_channel(config.get("channel_id") or 0)
        verified_role = self.guild.get_role(config.get("verified_role_id") or 0)
        temp_role = self.guild.get_role(config.get("temp_role_id") or 0)
        log_channel = self.guild.get_channel(config.get("log_channel") or 0)
        warnings = self.cog.configuration_warnings(self.guild, config)
        status = "Needs attention" if warnings else "Ready"
        summary = (
            f"**Status**  {'⚠️' if warnings else '✅'} {status}\n"
            f"**Verification channel**  {channel.mention if channel else 'Not set'}\n"
            f"**Verified role**  {verified_role.mention if verified_role else 'Not set'}\n"
            f"**Restricted role**  {temp_role.mention if temp_role else 'None'}\n"
            f"**Activity log**  {log_channel.mention if log_channel else 'Off'}\n\n"
            f"**Challenge behavior**\n"
            f"{get_time(config['time_limit'])} • "
            f"Auto-start {'on' if config['auto_start'] else 'off'} • "
            f"Cleanup {'on' if config['delete_after'] else 'off'} • "
            f"Kick on timeout {'on' if config['kick_on_fail'] else 'off'}"
        )
        if warnings:
            summary += "\n\n**Fix before using**\n" + "\n".join(
                f"- {warning}" for warning in warnings
            )
        if self.notice:
            summary += f"\n\n> {self.notice}"

        buttons = ui.ActionRow(
            DashboardButton(self, "destinations", "Channels & roles", "🧭"),
            DashboardButton(self, "behavior", "Behavior", "⚙️"),
            DashboardButton(self, "publish", "Refresh panel", "🔄"),
            DashboardButton(self, "test", "Test", "🧪"),
            DashboardButton(
                self, "disable", "Disable", "🗑️", discord.ButtonStyle.danger
            ),
        )
        self.add_item(
            ui.Container(
                ui.TextDisplay("## 🛡️ Verification control center"),
                ui.TextDisplay(summary),
                ui.Separator(),
                buttons,
                accent_colour=colors.orange if warnings else colors.fate,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "Only the moderator who opened this panel can use it.", ephemeral=True
            )
            return False
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server to change verification.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message:
            with suppress(HTTPException):
                await self.message.edit(view=self)


class DashboardButton(ui.Button):
    def __init__(
        self,
        dashboard: VerificationDashboard,
        action: str,
        label: str,
        emoji: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ):
        super().__init__(label=label, emoji=emoji, style=style)
        self.dashboard = dashboard
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        dashboard = self.dashboard
        if self.action in {"setup", "destinations"}:
            return await interaction.response.send_modal(DestinationsModal(dashboard))
        if self.action == "behavior":
            return await interaction.response.send_modal(BehaviorModal(dashboard))
        if self.action == "disable":
            return await interaction.response.send_modal(
                DisableVerificationModal(dashboard)
            )
        if self.action == "test":
            return await dashboard.cog.test_challenge(interaction)

        await interaction.response.defer()
        dashboard.notice = await dashboard.cog.publish_panel(dashboard.guild)
        dashboard.rebuild()
        await interaction.edit_original_response(view=dashboard)


class DestinationsModal(ui.Modal):
    def __init__(self, dashboard: VerificationDashboard):
        super().__init__(title="Verification channels and roles")
        self.dashboard = dashboard
        config = dashboard.cog.get_config(dashboard.guild.id) or DEFAULT_CONFIG
        guild = dashboard.guild

        self.channel = ui.ChannelSelect(
            custom_id="fate:verification:channel",
            channel_types=[discord.ChannelType.text],
            placeholder="Choose the verification channel",
            min_values=1,
            max_values=1,
            required=True,
            default_values=channel_default(
                guild.get_channel(config.get("channel_id") or 0)
            ),
        )
        self.verified_role = ui.RoleSelect(
            custom_id="fate:verification:verified-role",
            placeholder="Choose the role members unlock",
            min_values=1,
            max_values=1,
            required=True,
            default_values=role_default(
                guild.get_role(config.get("verified_role_id") or 0)
            ),
        )
        self.temp_role = ui.RoleSelect(
            custom_id="fate:verification:temp-role",
            placeholder="Optional restricted/unverified role",
            min_values=0,
            max_values=1,
            required=False,
            default_values=role_default(
                guild.get_role(config.get("temp_role_id") or 0)
            ),
        )
        self.log_channel = ui.ChannelSelect(
            custom_id="fate:verification:log-channel",
            channel_types=[discord.ChannelType.text],
            placeholder="Optional private activity log",
            min_values=0,
            max_values=1,
            required=False,
            default_values=channel_default(
                guild.get_channel(config.get("log_channel") or 0)
            ),
        )
        self.add_item(
            ui.Label(
                text="Verification channel",
                description="Fate keeps a shared entry panel here.",
                component=self.channel,
            )
        )
        self.add_item(
            ui.Label(
                text="Verified role",
                description="Added after a correct captcha.",
                component=self.verified_role,
            )
        )
        self.add_item(
            ui.Label(
                text="Restricted role",
                description="Optional: added on join and removed after verification.",
                component=self.temp_role,
            )
        )
        self.add_item(
            ui.Label(
                text="Activity log",
                description="Optional: records passes, timeouts, and errors.",
                component=self.log_channel,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        dashboard = self.dashboard
        cog = dashboard.cog
        guild = dashboard.guild
        channel = guild.get_channel(self.channel.values[0].id)
        verified_role = guild.get_role(self.verified_role.values[0].id)
        temp_role = (
            guild.get_role(self.temp_role.values[0].id)
            if self.temp_role.values
            else None
        )
        log_channel = (
            guild.get_channel(self.log_channel.values[0].id)
            if self.log_channel.values
            else None
        )

        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "Choose a server text channel.", ephemeral=True
            )
        if error := cog.channel_permission_error(channel):
            return await interaction.response.send_message(error, ephemeral=True)
        if not verified_role:
            return await interaction.response.send_message(
                "Choose a valid verified role.", ephemeral=True
            )
        if error := cog.validate_role(guild, verified_role, interaction.user):
            return await interaction.response.send_message(error, ephemeral=True)
        if temp_role and (
            error := cog.validate_role(guild, temp_role, interaction.user)
        ):
            return await interaction.response.send_message(error, ephemeral=True)
        if temp_role and temp_role.id == verified_role.id:
            return await interaction.response.send_message(
                "The restricted and verified roles must be different.", ephemeral=True
            )
        if log_channel and (error := cog.log_channel_permission_error(log_channel)):
            return await interaction.response.send_message(error, ephemeral=True)

        await interaction.response.defer()
        was_enabled = cog.is_enabled(guild.id)
        old = dict(cog.get_config(guild.id) or {})
        config = dict(DEFAULT_CONFIG)
        config.update(old)
        changed_channel = config.get("channel_id") != channel.id
        config.update(
            channel_id=channel.id,
            verified_role_id=verified_role.id,
            temp_role_id=temp_role.id if temp_role else None,
            log_channel=log_channel.id if log_channel else None,
        )
        if changed_channel:
            config["panel_message_id"] = None
        cog.config[guild.id] = config
        await cog.config.flush()

        dashboard.notice = await cog.publish_panel(guild)
        if changed_channel and old:
            await cog.delete_panel(guild, old)
        if not was_enabled:
            await cog.bot.create_log(
                message=f"!on **Verification** - `{guild}`",
                channel="module_log",
                embedded=True,
                color="green",
            )
        dashboard.rebuild()
        await interaction.edit_original_response(view=dashboard)


class BehaviorModal(ui.Modal):
    def __init__(self, dashboard: VerificationDashboard):
        super().__init__(title="Verification behavior")
        self.dashboard = dashboard
        config = dashboard.cog.get_config(dashboard.guild.id) or DEFAULT_CONFIG
        self.time_limit = ui.TextInput(
            custom_id="fate:verification:time-limit",
            placeholder="45s, 2m, or a number of seconds",
            default=f"{config['time_limit']}s",
            min_length=2,
            max_length=12,
        )
        options = [
            discord.CheckboxGroupOption(
                label="Start a captcha when a member joins",
                value="auto_start",
                description="Otherwise members use the shared panel.",
                default=config["auto_start"],
            ),
            discord.CheckboxGroupOption(
                label="Delete completed public captchas",
                value="delete_after",
                description="Private challenges always become a result card.",
                default=config["delete_after"],
            ),
            discord.CheckboxGroupOption(
                label="Kick when the captcha expires",
                value="kick_on_fail",
                description="Only applies after the full time limit passes.",
                default=config["kick_on_fail"],
            ),
        ]
        self.behavior = ui.CheckboxGroup(
            custom_id="fate:verification:behavior",
            required=False,
            min_values=0,
            max_values=3,
            options=options,
        )
        self.add_item(
            ui.Label(
                text="Challenge time limit",
                description=f"Between {get_time(MIN_TIME_LIMIT)} and {get_time(MAX_TIME_LIMIT)}.",
                component=self.time_limit,
            )
        )
        self.add_item(ui.Label(text="Options", component=self.behavior))

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.time_limit.value.strip()
        seconds = int(raw) if raw.isdecimal() else extract_time(raw)
        if not seconds or not MIN_TIME_LIMIT <= seconds <= MAX_TIME_LIMIT:
            return await interaction.response.send_message(
                f"Choose a time between {get_time(MIN_TIME_LIMIT)} and {get_time(MAX_TIME_LIMIT)}.",
                ephemeral=True,
            )
        config = self.dashboard.cog.get_config(self.dashboard.guild.id)
        if not config:
            return await interaction.response.send_message(
                "Set up verification first.", ephemeral=True
            )

        selected = set(self.behavior.values)
        config["time_limit"] = seconds
        for key in ("auto_start", "delete_after", "kick_on_fail"):
            config[key] = key in selected
        await self.dashboard.cog.config.flush()
        self.dashboard.notice = "Challenge behavior saved."
        self.dashboard.rebuild()
        await interaction.response.edit_message(view=self.dashboard)


class DisableVerificationModal(ui.Modal):
    def __init__(self, dashboard: VerificationDashboard):
        super().__init__(title="Disable verification")
        self.dashboard = dashboard
        self.confirm = ui.Checkbox(custom_id="fate:verification:disable-confirm")
        self.add_item(
            ui.Label(
                text="Confirm disable",
                description="This removes the saved configuration and shared panel.",
                component=self.confirm,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        if not self.confirm.value:
            return await interaction.response.send_message(
                "Nothing changed — select the confirmation box to disable verification.",
                ephemeral=True,
            )
        await interaction.response.defer()
        await self.dashboard.cog.disable_for_guild(self.dashboard.guild)
        self.dashboard.notice = (
            "Verification was disabled and the shared panel was removed."
        )
        self.dashboard.rebuild()
        await interaction.edit_original_response(view=self.dashboard)


async def setup(bot):
    await bot.add_cog(Verification(bot), override=True)
