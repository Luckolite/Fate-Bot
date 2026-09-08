"""
cogs.utility.autorole
~~~~~~~~~~~~~~~~~~~~~~

Assigns configured roles to members when they join, immediately or after a delay.

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
from contextlib import suppress
from typing import Optional

import discord
from discord import Forbidden, NotFound, app_commands, ui
from discord.ext import commands

from botutils import GetChoice, colors, extract_time, get_time
from botutils import cache_rewrite

MAX_DELAY = 60 * 60 * 24 * 365
MAX_ROLES = 25


class AutoRole(commands.Cog):
    default_config = {
        "wait_for_verify": False,
        "roles": {},
    }

    def __init__(self, bot):
        self.bot = bot
        self.config = cache_rewrite.Cache(bot, "autorole")
        self.timers = bot.utils.persistent_tasks(
            database="autorole_timers",
            callback=self.give_delayed_role,
            identifier="task_id",
        )

    @staticmethod
    def normalize_config(config) -> bool:
        """Migrate the old role ID list into role ID -> delay settings."""
        changed = False
        roles = config.get("roles", {})
        if isinstance(roles, list):
            roles = {str(role_id): 0 for role_id in roles}
            changed = True
        elif not isinstance(roles, dict):
            roles = {}
            changed = True

        normalized = {}
        for role_id, delay in roles.items():
            try:
                normalized[str(int(role_id))] = max(0, int(delay or 0))
            except (TypeError, ValueError):
                changed = True
        if normalized != roles:
            changed = True
        config["roles"] = normalized

        if "wait_for_verify" not in config:
            config["wait_for_verify"] = False
            changed = True
        return changed

    async def get_config(self, guild_id: int, *, fresh=False):
        config = (
            await self.config.fetch(guild_id)
            if fresh else await self.config[guild_id]
        )
        existed = bool(config)
        changed = self.normalize_config(config)
        if existed and changed:
            await config.save()
        return config

    async def is_enabled(self, guild_id: int):
        config = await self.config[guild_id]
        return bool(config and config.get("roles"))

    @staticmethod
    def validate_role(guild: discord.Guild, actor: discord.Member, role: discord.Role):
        if role.is_default():
            return "The everyone role cannot be assigned."
        if role.managed:
            return "That role is managed by an integration."
        if role.position >= guild.me.top_role.position:
            return "That role is higher than the bot's highest role."
        if actor.id != guild.owner_id and role.position >= actor.top_role.position:
            return "That role is above your highest role."

    async def set_role(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        role: discord.Role,
        delay: int,
    ) -> Optional[str]:
        if error := self.validate_role(guild, actor, role):
            return error

        config = await self.get_config(guild.id, fresh=True)
        roles = config["roles"]
        is_new = str(role.id) not in roles
        if is_new and len(roles) >= MAX_ROLES:
            return f"Auto role supports up to {MAX_ROLES} configured roles."

        was_enabled = bool(roles)
        roles[str(role.id)] = delay
        await config.save()
        if not was_enabled:
            await self.bot.create_log(
                message=f"!on **Auto Role** - `{guild}`",
                channel="module_log",
                embedded=True,
                color="green",
            )
        return None

    async def cancel_role_timers(self, guild_id: int, role_id: int) -> None:
        keys = [
            key for key, data in list(self.timers.db.items())
            if data.get("guild_id") == guild_id and data.get("role_id") == role_id
        ]
        for key in keys:
            await self.timers.cancel(key)

    async def remove_role(self, guild: discord.Guild, role_id: int) -> bool:
        config = await self.get_config(guild.id, fresh=True)
        if str(role_id) not in config["roles"]:
            return False

        del config["roles"][str(role_id)]
        await self.cancel_role_timers(guild.id, role_id)
        if config["roles"]:
            await config.save()
        else:
            await config.delete()
            await self.bot.create_log(
                message=f"!off **Auto Role** - `{guild}`",
                channel="module_log",
                embedded=True,
                color="red",
            )
        return True

    @commands.hybrid_group(
        name="auto-role",
        aliases=["autorole", "auto_role"],
        fallback="view",
        invoke_without_command=True,
        description="Configures roles given to new members",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(embed_links=True, manage_roles=True)
    @app_commands.default_permissions(manage_roles=True)
    async def auto_role(self, ctx: commands.Context):
        """Open the interactive auto-role dashboard."""
        await ctx.defer()
        await AutoRoleMenu(self, ctx).start()

    @auto_role.command(name="add", description="Adds or updates an auto role")
    @app_commands.describe(role="Role to assign", delay="Wait time, such as 30m or 7d")
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def _add(
        self,
        ctx: commands.Context,
        role: discord.Role,
        delay: Optional[str] = None,
    ):
        await ctx.defer()
        seconds = 0
        if delay and delay.casefold() not in {"0", "none", "instant", "immediate"}:
            seconds = extract_time(delay) or 0
            if not seconds:
                return await ctx.send("Use a delay like `30m`, `2h`, or `7d`.")
            if seconds < 60:
                return await ctx.send("The delay must be at least one minute.")
            if seconds > MAX_DELAY:
                return await ctx.send("The delay cannot be longer than one year.")

        if error := await self.set_role(ctx.guild, ctx.author, role, seconds):
            return await ctx.send(error)
        timing = f"after {get_time(seconds)}" if seconds else "immediately"
        await ctx.send(f"New members will receive {role.mention} {timing}.")

    @auto_role.command(name="remove", description="Removes configured auto roles")
    @app_commands.describe(role="Configured role to remove")
    @commands.has_permissions(manage_roles=True)
    async def _remove(
        self,
        ctx: commands.Context,
        role: Optional[discord.Role] = None,
    ):
        await ctx.defer()
        config = await self.get_config(ctx.guild.id, fresh=True)
        configured = {
            f"{found.name} ({found.id})": found
            for role_id in config["roles"]
            if (found := ctx.guild.get_role(int(role_id)))
        }
        if not configured:
            return await ctx.send("Auto role is not configured.")

        roles = [role] if role else []
        if not roles:
            choices = await GetChoice(
                ctx,
                configured.keys(),
                limit=None,
                placeholder="Choose auto roles to remove",
            )
            roles = [configured[choice] for choice in choices]

        removed = 0
        for selected in roles:
            if selected and await self.remove_role(ctx.guild, selected.id):
                removed += 1
        await ctx.send(f"Removed {removed:,} auto role{'s' if removed != 1 else ''}.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not self.bot.is_ready():
            return
        config = await self.config[member.guild.id]
        if not config or not config.get("roles"):
            return
        if self.normalize_config(config):
            await config.save()

        guild = member.guild
        bot = guild.me
        if not bot.guild_permissions.manage_roles:
            with suppress(Forbidden, AttributeError):
                await guild.owner.send(
                    f"**[Auto Role - {guild}]** I need Manage Roles to assign roles."
                )
            return

        immediate = []
        invalid = []
        for role_id, delay in config["roles"].items():
            role = guild.get_role(int(role_id))
            if not role or role.position >= bot.top_role.position:
                invalid.append(role_id)
                continue
            if delay:
                self.timers.run(
                    task_id=f"{guild.id}:{member.id}:{role.id}",
                    guild_id=guild.id,
                    user_id=member.id,
                    role_id=role.id,
                    sleep_for=delay,
                )
            else:
                immediate.append(role)

        if immediate:
            with suppress(Forbidden, NotFound, discord.HTTPException):
                await member.add_roles(*immediate, reason="Configured auto roles")
                self.record_activity(len(immediate))
        if invalid:
            for role_id in invalid:
                del config["roles"][role_id]
            await config.save()

    async def give_delayed_role(
        self,
        task_id: str,
        guild_id: int,
        user_id: int,
        role_id: int,
        sleep_for: int,
    ):
        await asyncio.sleep(sleep_for)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        member = guild.get_member(user_id)
        role = guild.get_role(role_id)
        config = await self.config[guild_id]
        if not member or not role or not config:
            return
        self.normalize_config(config)
        if str(role_id) not in config["roles"]:
            return
        if role in member.roles or role.position >= guild.me.top_role.position:
            return
        with suppress(Forbidden, NotFound, discord.HTTPException):
            await member.add_roles(role, reason="Delayed auto role")
            self.record_activity()

    def record_activity(self, amount: int = 1) -> None:
        telemetry = getattr(self.bot, "telemetry", None)
        if telemetry is not None and amount > 0:
            telemetry.increment("autorole_activity", amount=amount)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        keys = [
            key for key, data in list(self.timers.db.items())
            if data.get("guild_id") == member.guild.id
            and data.get("user_id") == member.id
        ]
        for key in keys:
            await self.timers.cancel(key)

    @commands.Cog.listener()
    async def on_role_delete(self, role: discord.Role):
        config = await self.config[role.guild.id]
        if not config:
            return
        self.normalize_config(config)
        if str(role.id) in config["roles"]:
            await self.remove_role(role.guild, role.id)


class AutoRoleMenu(ui.View):
    def __init__(self, cog: AutoRole, ctx: commands.Context):
        super().__init__(timeout=180)
        self.cog = cog
        self.ctx = ctx
        self.guild = ctx.guild
        self.user = ctx.author
        self.config = None
        self.message: Optional[discord.Message] = None
        self.notice = None

    async def start(self):
        await self.refresh()
        self.message = await self.ctx.send(embed=self.embed, view=self)

    async def refresh(self):
        self.config = await self.cog.get_config(self.guild.id, fresh=True)
        self.embed = self.build_embed()
        self.clear_items()
        self.add_item(AutoRoleSelect(self))
        if self.config["roles"]:
            self.add_item(ConfiguredRoleSelect(self))
        self.add_item(CloseAutoRoleMenu(self))

    async def refresh_interaction(self, interaction: discord.Interaction):
        """Rebuild and edit the menu attached to a component or modal interaction."""
        await self.refresh()
        self.message = await interaction.edit_original_response(
            embed=self.embed,
            view=self,
        )

    def build_embed(self):
        embed = discord.Embed(
            title="Auto Role",
            description=(
                "Choose a role below to add it or change when it is assigned. "
                "Each role can be immediate or have its own delay."
            ),
            color=colors.fate,
        )
        if self.guild.icon:
            embed.set_thumbnail(url=self.guild.icon.url)

        configured = []
        for role_id, delay in self.config["roles"].items():
            role = self.guild.get_role(int(role_id))
            name = role.mention if role else f"Deleted role (`{role_id}`)"
            timing = f"After {get_time(delay)}" if delay else "Immediately"
            configured.append(f"{name} - {timing}")
        visible = configured[:15]
        if len(configured) > len(visible):
            visible.append(f"*...and {len(configured) - len(visible)} more*")
        embed.add_field(
            name=f"Configured roles ({len(configured)})",
            value="\n".join(visible) if visible else "No roles configured yet.",
            inline=False,
        )

        pending = sum(
            data.get("guild_id") == self.guild.id
            for data in self.cog.timers.db.values()
        )
        embed.add_field(name="Pending assignments", value=f"{pending:,}")
        if self.notice:
            embed.set_footer(text=self.notice)
        else:
            embed.set_footer(text="Select a role to configure its delay.")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "Only the moderator who opened this menu can use it.", ephemeral=True
            )
            return False
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "You need Manage Roles to configure auto roles.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)


class AutoRoleSelect(ui.RoleSelect):
    def __init__(self, menu: AutoRoleMenu):
        self.menu = menu
        super().__init__(placeholder="Add or edit an auto role", row=0)

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        if error := self.menu.cog.validate_role(
            self.menu.guild, interaction.user, role
        ):
            return await interaction.response.send_message(error, ephemeral=True)
        current = self.menu.config["roles"].get(str(role.id), 0)
        await interaction.response.send_modal(
            AutoRoleDelayModal(self.menu, role, current)
        )


class ConfiguredRoleSelect(ui.Select):
    def __init__(self, menu: AutoRoleMenu):
        self.menu = menu
        options = []
        for role_id, delay in list(menu.config["roles"].items())[:25]:
            role = menu.guild.get_role(int(role_id))
            options.append(discord.SelectOption(
                label=role.name[:100] if role else f"Deleted role {role_id}",
                value=role_id,
                description=f"Remove - {'immediate' if not delay else get_time(delay)}"[:100],
            ))
        super().__init__(
            placeholder="Remove a configured role",
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        role_id = int(self.values[0])
        role = self.menu.guild.get_role(role_id)
        await interaction.response.defer()
        await self.menu.cog.remove_role(self.menu.guild, role_id)
        self.menu.notice = f"Removed {role.name if role else role_id}."
        await self.menu.refresh_interaction(interaction)


class CloseAutoRoleMenu(ui.Button):
    def __init__(self, menu: AutoRoleMenu):
        self.menu = menu
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction):
        self.menu.stop()
        await interaction.response.edit_message(view=None)


class AutoRoleDelayModal(ui.Modal):
    def __init__(self, menu: AutoRoleMenu, role: discord.Role, current: int):
        super().__init__(title=f"Configure {role.name}"[:45])
        self.menu = menu
        self.role = role
        self.delay = ui.TextInput(
            label="Delay before assigning",
            placeholder="immediate, 30m, 2h, or 7d",
            default=get_time(current) if current else "immediate",
            min_length=1,
            max_length=20,
        )
        self.add_item(self.delay)

    async def on_submit(self, interaction: discord.Interaction):
        value = self.delay.value.strip()
        if value.casefold() in {"0", "none", "instant", "immediate"}:
            seconds = 0
        else:
            seconds = extract_time(value) or 0
            if not seconds:
                return await interaction.response.send_message(
                    "Use a delay like `30m`, `2h`, or `7d`.", ephemeral=True
                )
            if seconds < 60:
                return await interaction.response.send_message(
                    "The delay must be at least one minute.", ephemeral=True
                )
            if seconds > MAX_DELAY:
                return await interaction.response.send_message(
                    "The delay cannot be longer than one year.", ephemeral=True
                )

        await interaction.response.defer()
        if error := await self.menu.cog.set_role(
            self.menu.guild, interaction.user, self.role, seconds
        ):
            return await interaction.followup.send(error, ephemeral=True)

        timing = f"after {get_time(seconds)}" if seconds else "immediately"
        self.menu.notice = f"{self.role.name} will be assigned {timing}."
        await self.menu.refresh_interaction(interaction)


async def setup(bot):
    await bot.add_cog(AutoRole(bot), override=True)
