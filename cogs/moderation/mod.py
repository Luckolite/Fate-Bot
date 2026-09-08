"""
cogs.moderation.mod
~~~~~~~~~~~~~~~~~~~~

A cog for general moderation commands

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import json
import re
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from os import path
from string import printable
from time import time as now
from typing import *
from unicodedata import normalize

import discord
from discord import Member, Role, TextChannel, User, ButtonStyle, ui, Interaction, SelectOption
from discord import NotFound, Forbidden, HTTPException
from discord.ext import commands
from discord.ext.commands import Greedy

from apps.Dashboard.dashboard.validation import MAX_PURGE_LIMIT
from botutils import colors, get_prefix, get_time, split, CancelButton, format_date, GetConfirmation, extract_time, \
    emojis, s
from checks.exceptions import IgnoredExit
from fate import Fate
from .case_manager import CaseManager
from .logger import Logger

cache = {}  # Keep track of what commands are still being ran
# This should empty out as quickly as it's filled


def _root_command(command: str, subcommands: Dict[str, List[str]]) -> str:
    """Resolve a subcommand to the permission group used by its parent command."""
    return next(
        (parent for parent, children in subcommands.items() if command in children),
        command,
    )


def check_if_running():
    """ Checks if the command is already in progress """

    async def predicate(ctx):
        # with open(fp, 'r') as f:
        #     cache = json.load(f)  # type: dict
        cmd = ctx.command.name
        if cmd not in cache:
            cache[cmd] = []
        check_result = ctx.guild.id not in cache[cmd]
        if not check_result:
            with suppress(Forbidden):
                await ctx.send("That command is already running >:(")
        return check_result

    return commands.check(predicate)


def has_required_permissions(**kwargs):
    """ Permission check with support for usermod, rolemod, and role specific cmd access """
    async def predicate(ctx):
        cls = globals()["cls"]  # type: Moderation
        config = cls.config
        if str(ctx.guild.id) not in config:
            cls.config[str(ctx.guild.id)] = cls.template
        config = config[str(ctx.guild.id)]  # type: dict
        cmd = _root_command(ctx.command.name, cls.subs)
        if cmd in config["commands"]:
            allowed = config["commands"][cmd]  # type: dict
            if ctx.author.id in allowed["users"]:
                return True
            if any(role.id in allowed["roles"] for role in ctx.author.roles):
                return True
        if ctx.author.id in config["usermod"]:
            return True
        if any(r.id in config["rolemod"] for r in ctx.author.roles):
            return True
        perms = ctx.author.guild_permissions
        return all(getattr(perms, perm) == value for perm, value in kwargs.items())

    return commands.check(predicate)


def has_warn_permission():
    async def predicate(ctx):
        cls = globals()["cls"]  # type: Moderation
        config = cls.template
        if not ctx.guild:
            return False
        guild_id = str(ctx.guild.id)
        if guild_id in cls.config:
            config = cls.config[guild_id]
        warn_access = config["commands"]["warn"]
        if ctx.author.id in warn_access["users"]:
            return True
        elif any(role.id in warn_access["roles"] for role in ctx.author.roles):
            return True
        elif ctx.author.id in config["usermod"]:
            return True
        elif any(r.id in config["rolemod"] for r in ctx.author.roles):
            return True
        elif ctx.author.guild_permissions.administrator:
            return True
        raise commands.CheckFailure("You lack administrator or usermod permissions to use this command")

    return commands.check(predicate)


def purge_confirmation_enabled(settings: dict) -> bool:
    """Default to the safer prompt unless the guild explicitly opts out."""
    value = settings.get("purge_confirmation", True)
    return value if type(value) is bool else True


class PurgeConfirmation(GetConfirmation):
    """Purge confirmation with a persistent server-level opt-out."""

    def __init__(self, ctx, question: str, settings_cog):
        self.settings_cog = settings_cog
        super().__init__(ctx, question)

    @ui.button(label="Confirm & don't ask again", style=ButtonStyle.secondary)
    async def confirm_and_disable(self, interaction, _button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(
                "This menu isn't for you", ephemeral=True
            )

        config = self.settings_cog.get_config(self.ctx.guild.id)
        config["purge_confirmation"] = False
        await self.settings_cog.save_config(self.ctx.guild.id, config)
        self.value = True
        await interaction.response.send_message(
            "Alright, I won't ask again. Run `.purge reset` to bring confirmation back",
            ephemeral=True,
        )
        self.stop()


class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)  # Set a timeout for the buttons
        self.confirmed = False

        # Define the Confirm button
        self.confirm_button = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.green)
        self.confirm_button.callback = self.confirm
        self.add_item(self.confirm_button)

        # Define the Cancel button
        self.cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.red)
        self.cancel_button.callback = self.cancel
        self.add_item(self.cancel_button)

    async def confirm(self, interaction: discord.Interaction):
        await interaction.response.send_message("Confirmation received. Starting the import.")
        self.confirm_button.disabled = True
        self.cancel_button.disabled = True
        await interaction.message.edit(view=self)
        self.confirmed = True
        self.stop()

    async def cancel(self, interaction: discord.Interaction):
        await interaction.response.send_message("Operation cancelled.")
        self.confirm_button.disabled = True
        self.cancel_button.disabled = True
        await interaction.message.edit(view=self)
        self.stop()


class Moderation(commands.Cog):
    config: Dict[str, Dict[str, Any]]
    tasks: Dict[str, Dict[str, Any]]
    subs: Dict[str, List[str]]

    def __init__(self, bot: Fate):
        self.bot = bot
        self.guild_last_executed = {}
        self.fp: str = "./static/mod-cache.json"
        self.path: str = "./data/userdata/moderation.json"
        self.config = {}
        self.tasks = {}
        if path.isfile(self.path):
            with open(self.path, "r") as f:
                self.config = json.load(f)  # type: dict
        self.timers = bot.utils.persistent_tasks(
            database="role_timers",
            callback=self.handle_timer,
            identifier="user_id"
        )

        # Add or remove any missing/unused key/values
        # this is for ease of updating json as it's developed
        template = self.template
        for guild_id, config in self.config.items():
            for key in template.keys() - config.keys():
                config[key] = deepcopy(template[key])
            for key in config.keys() - template.keys():
                del config[key]
            self.config[guild_id] = config

        self.subs = {"warn": ["delwarn", "clearwarns"], "mute": ["unmute"]}

        self.import_bans_usage: str = "Transfer bans from one server to another. Usage is just `.import-bans 1234` " \
                                 "with 1234 being the ID of the server you're importing from"
        self.roles_usage: str = "Formats the role list to show how many members each role has. Usage is just `.roles`"

    async def cog_unload(self) -> None:
        pending = {
            task
            for guild_tasks in self.tasks.values()
            for task in guild_tasks.values()
            if not task.done()
        }
        pending.update(
            task for task in self.timers.tasks.values() if not task.done()
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()
        self.timers.tasks.clear()

    @property
    def template(self):
        return {
            "usermod": [],  # Users with access to all mod commands
            "rolemod": [],  # Roles with access to all mod commands
            "commands": {
                "warn": {"users": [], "roles": []},
                "purge": {"users": [], "roles": []},
                "mute": {"users": [], "roles": []},
                "kick": {"users": [], "roles": []},
                "ban": {"users": [], "roles": []},
            },
            "warns": {},
            "warns_config": {},
            "mute_role": None,  # type: Optional[None, discord.Role.id]
            "timers": {},
            "mute_timers": {},
        }

    @property
    def cases(self) -> CaseManager:
        return self.bot.cogs["CaseManager"]  # type: ignore

    async def save_data(self):
        async with self.bot.utils.open(self.path, "w+") as f:
            await f.write(await self.bot.dump(self.config))

    async def cog_before_invoke(self, ctx):
        """ Index commands that are running """
        if not ctx.guild:
            raise commands.errors.NoPrivateMessage("This command can't be ran in a DM")
        cmd = ctx.command.name
        if cmd not in cache:
            cache[cmd] = []
        if ctx.guild.id not in cache[cmd]:
            cache[cmd].append(ctx.guild.id)
        if str(ctx.guild.id) not in self.config:
            self.config[str(ctx.guild.id)] = self.template
            await self.save_data()
        ctx.cls = self

    async def cog_after_invoke(self, ctx):
        """ Index commands that are running """
        for cmd, guild_ids in list(cache.items()):
            cache[cmd] = [guild_id for guild_id in guild_ids if self.bot.get_guild(guild_id)]
            if not cache[cmd]:
                del cache[cmd]
        if not ctx.guild:
            return
        cmd = ctx.command.name
        if cmd in cache and ctx.guild.id in cache[cmd]:
            cache[cmd].remove(ctx.guild.id)
            if not cache[cmd]:
                del cache[cmd]

    async def save_config(self, config):
        """ Save things like channel restrictions """
        self.bot.restricted = config
        async with self.bot.utils.open("./data/userdata/config.json", "w") as f:
            await f.write(await self.bot.dump(config))

    @commands.hybrid_command(
        name="mute-role",
        aliases=["muterole", "set-mute-role", "setmuterole", "set-mute", "setmute"],
        description="Sets the mute role to a specific role"
    )
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.has_permissions(manage_roles=True)
    async def mute_role(self, ctx, *, role):
        role = await self.bot.utils.get_role(ctx, role)
        if not role:
            return await ctx.send("Role not found")
        if (
            role.position >= ctx.author.top_role.position
            and not ctx.author.id == ctx.guild.owner.id
        ):
            return await ctx.send("That role's above your paygrade, take a seat.")
        self.config[str(ctx.guild.id)]["mute_role"] = role.id
        await ctx.send(f"Set the mute role to {role.name}")
        await self.save_data()

    @commands.hybrid_command(name="addmod", description="Gives a user or role access to mod commands")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(embed_links=True)
    async def addmod(self, ctx, target: Union[discord.Member, discord.Role] = None):
        if not target:
            return await ctx.send("User or role not found")
        if ctx.author.id != ctx.guild.owner.id:
            if isinstance(target, discord.Member):
                if target.top_role.position >= ctx.author.top_role.position:
                    return await ctx.send("That user is above your paygrade, take a seat")
            elif target.position >= ctx.author.top_role.position:
                return await ctx.send("That role is above your paygrade, take a seat")
        guild_id = str(ctx.guild.id)
        if isinstance(target, discord.Member):
            if target.id in self.config[guild_id]["usermod"]:
                return await ctx.send("That users already a mod")
            self.config[guild_id]["usermod"].append(target.id)
        else:
            if target.id in self.config[guild_id]["rolemod"]:
                return await ctx.send("That role's already a mod role")
            self.config[guild_id]["rolemod"].append(target.id)
        e = discord.Embed(color=colors.fate)
        e.description = f"Made {target.mention} a mod"
        await ctx.send(embed=e)
        await self.save_data()

    @commands.hybrid_command(
        name="delmod",
        aliases=["removemod", "del-mod", "remove-mod"],
        description="Removes a user or roles special mod command access"
    )
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(embed_links=True)
    async def delmod(self, ctx, target: Union[discord.Member, discord.Role] = None):
        if not target:
            return await ctx.send("User or role not found")
        if ctx.author.id != ctx.guild.owner.id:
            if isinstance(target, discord.Member):
                if target.top_role.position >= ctx.author.top_role.position:
                    return await ctx.send("That user is above your paygrade, take a seat")
            elif target.position >= ctx.author.top_role.position:
                return await ctx.send("That role is above your paygrade, take a seat")
        guild_id = str(ctx.guild.id)
        if isinstance(target, discord.Member):
            if target.id not in self.config[guild_id]["usermod"]:
                return await ctx.send("That user isn't a mod")
            self.config[guild_id]["usermod"].remove(target.id)
        else:
            if target.id not in self.config[guild_id]["rolemod"]:
                return await ctx.send("That role's isn't a mod role")
            self.config[guild_id]["rolemod"].remove(target.id)
        e = discord.Embed(color=colors.fate)
        e.description = f"Removed {target.mention} mod"
        await ctx.send(embed=e)
        await self.save_data()

    @commands.hybrid_command(
        name="mods",
        aliases=["usermods", "rolemods"],
        description="Lists everything that has user/role mod"
    )
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.channel)
    @commands.bot_has_permissions(embed_links=True)
    async def mods(self, ctx):
        config = self.config[str(ctx.guild.id)]
        if not config["usermod"] and not config["rolemod"]:
            return await ctx.send("There are no mod users or mod roles")
        e = discord.Embed(color=colors.fate)
        users = [user for uid in config["usermod"] if (user := self.bot.get_user(uid))]
        roles = [role for rid in config["rolemod"] if (role := ctx.guild.get_role(rid))]
        if not users and not roles:
            return await ctx.send(
                "There are no mod users or mod roles, the existing ones were removed"
            )
        if users:
            e.add_field(
                name="UserMods", value="\n".join(u.mention for u in users), inline=False
            )
        if roles:
            e.add_field(
                name="RoleMods", value="\n".join(r.mention for r in roles), inline=False
            )
        await ctx.send(embed=e)

    # @commands.is_owner()
    # async def test_purge(
    #         self, ctx,
    #         target: Optional[User],
    #         method: Optional[Literal[
    #             "images",
    #             "embeds",
    #             "stickers",
    #             "mentions",
    #             "users",
    #             "bots"
    #         ]],
    #         phrase: Optional[str],
    #         amount: int
    # ):
    #     await ctx.send(
    #         f"Target: {target}\n"
    #         f"Method: {method}\n"
    #         f"Phrase: {phrase}\n"
    #         f"Amount: {amount}"
    #     )

    @commands.hybrid_command(name="purge", aliases=["prune", "nuke", "clear"], description="Bulk deletes messages")
    @commands.cooldown(2, 5, commands.BucketType.user)
    @check_if_running()
    @has_required_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(self, ctx, *, args=None):
        args = args.split() if args else []
        await ctx.defer()
        settings_cog = self.bot.get_cog("Settings")
        if [str(arg).lower() for arg in args] == ["reset"]:
            if not settings_cog:
                return await ctx.send("Purge confirmation settings are unavailable right now.")
            settings = settings_cog.get_config(ctx.guild.id)
            settings["purge_confirmation"] = True
            await settings_cog.save_config(ctx.guild.id, settings)
            return await ctx.send("Purge confirmation is back on.")

        _help = discord.Embed(color=colors.fate)
        _help.description = (
            ".purge amount\n"
            ".purge @user amount\n"
            ".purge images amount\n"
            ".purge embeds amount\n"
            ".purge stickers amount\n"
            ".purge mentions amount\n"
            ".purge users amount\n"
            ".purge bots amount\n"
            ".purge word/phrase amount\n"
            ".purge 1h30m\n"
            ".purge reset"
        )
        after = None
        purge_limit = 1000
        purge_confirmation = True
        if settings_cog:
            settings = settings_cog.get_config(ctx.guild.id)
            purge_limit = max(1, min(MAX_PURGE_LIMIT, int(settings["purge_limit"])))
            purge_confirmation = purge_confirmation_enabled(settings)
        if ctx.guild.id == 523678393565315077:
            purge_limit = min(purge_limit, 50)
        if args and (duration := extract_time(args[0])):
            if duration > 60 * 60 * 24 * 14:
                return await ctx.send("That's too old")
            after = datetime.utcnow() - timedelta(seconds=duration)
            if len(args) > 1:
                return await ctx.send("Timed purges do not support additional args")
        elif not args or not args[-1].isdigit():
            return await ctx.send(embed=_help)

        args = [str(arg).lower() for arg in args]
        if ctx.message.reference:
            amount_to_purge = "1000"
            if args:
                amount_to_purge = args[len(args) - 1]
        else:
            amount_to_purge = args[len(args) - 1]
        if not after and not amount_to_purge.isdecimal():
            return await ctx.send(f"{amount_to_purge} isn't a proper number")
        if after:
            amount_to_purge = purge_limit
        else:
            amount_to_purge = int(amount_to_purge)
        ctx.counter = 0
        if amount_to_purge > purge_limit:
            return await ctx.send(
                f"This server has a purge limit of {purge_limit:,} messages"
            )
        check = None
        special_check = None
        msgs = []
        if len(args) > 1:
            if ctx.message.raw_mentions:
                special_check = lambda msg: msg.author.id in ctx.message.raw_mentions
            elif "image" in args or "images" in args:
                special_check = lambda msg: msg.attachments
            elif "embed" in args or "embeds" in args:
                special_check = lambda msg: msg.embeds
            elif "mentions" in args:
                special_check = lambda msg: msg.raw_mentions or (
                    msg.raw_channel_mentions or msg.raw_role_mentions
                )
            elif "user" in args or "users" in args:
                special_check = lambda msg: not msg.author.bot
            elif "bot" in args or "bots" in args:
                special_check = lambda msg: msg.author.bot
            elif "sticker" in args or "stickers" in args:
                special_check = lambda msg: msg.stickers
            else:
                phrase = " ".join(args[: len(args) - 1])
                special_check = lambda msg: phrase in str(msg.content).lower()
            old_amount = int(amount_to_purge)
            amount_to_purge = 250

            def check(msg):
                if ctx.counter == old_amount:
                    return False
                if special_check(msg):
                    ctx.counter += 1
                    return True
                return False

        reaction_purge = "reaction" in args or "reactions" in args
        target_amount = old_amount if len(args) > 1 else None
        preview = []
        history_kwargs = {
            "limit": amount_to_purge,
            "before": ctx.message,
            "after": after,
        }
        if ctx.message.reference:
            ref = ctx.message.reference
            if ref.cached_message:
                history_kwargs["after"] = ref.cached_message
            else:
                history_kwargs["after"] = await ctx.channel.fetch_message(ref.message_id)

        async for msg in ctx.channel.history(**history_kwargs):
            if reaction_purge:
                if not msg.reactions:
                    continue
            elif special_check and not special_check(msg):
                    continue
            preview.append(msg)
            if target_amount and len(preview) == target_amount:
                break

        if not preview:
            return await ctx.send("There are no matching messages to purge.")

        boundary = preview[-1]
        content = boundary.clean_content.strip()
        if not content:
            content = "an attachment, embed, sticker, or empty message"
        elif len(content) > 250:
            content = f"{content[:247]}..."
        if purge_confirmation:
            action = "clear reactions from" if reaction_purge else "delete"
            question = (
                f"This will {action} {len(preview):,} message"
                f"{'s' if len(preview) != 1 else ''}, up to this message from "
                f"**{boundary.author}**:\n> {content}\n{boundary.jump_url}\n"
                "Are you sure?"
            )
            confirmation = (
                PurgeConfirmation(ctx, question, settings_cog)
                if settings_cog
                else GetConfirmation(ctx, question)
            )
            confirmed = await confirmation
            if not confirmed:
                return

        if reaction_purge:
            for msg in preview:
                with suppress(Forbidden, NotFound):
                    await msg.clear_reactions()
                    msgs.append(msg)
        else:
            async def purge_task(coro):
                try:
                    messages = await coro
                except discord.errors.HTTPException:  # Msgs too old
                    try:
                        messages = []
                        async for msg in ctx.channel.history(before=ctx.message, limit=amount_to_purge):
                            with suppress(Forbidden, NotFound, asyncio.TimeoutError):
                                await msg.delete()
                                messages.append(msg)
                    except discord.errors.NotFound:
                        raise IgnoredExit
                return messages

            kwargs = {}
            if check:
                kwargs["check"] = check
            if ctx.message.reference:
                coro = ctx.channel.purge(
                    limit=amount_to_purge,
                    before=ctx.message,
                    after=history_kwargs["after"],
                    **kwargs
                )
            else:
                coro = ctx.channel.purge(
                    limit=amount_to_purge,
                    before=ctx.message,
                    after=after,
                    **kwargs
                )

            task = asyncio.create_task(purge_task(coro))
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except asyncio.TimeoutError:
                await ctx.send(
                    "It seems this purge is gonna take awhile..", delete_after=20
                )
            msgs = await task
        e = discord.Embed(
            description=f"♻ Cleared {len(msgs)} message{'s' if len(msgs) > 1 else ''}"
        )
        await ctx.send(embed=e, delete_after=5)
        if ctx.message:
            await ctx.message.delete(delay=5)

    async def finish_mute_timer(self, guild_id: str, user_id: str) -> None:
        guild_config = self.config.get(guild_id, {})
        mute_timers = guild_config.get("mute_timers", {})
        mute_timers.pop(user_id, None)
        await self.save_data()
        guild_tasks = self.tasks.get(guild_id)
        if guild_tasks is not None:
            guild_tasks.pop(user_id, None)
            if not guild_tasks:
                self.tasks.pop(guild_id, None)

    async def handle_mute_timer(self, guild_id: str, user_id: str, timer_info: dict):
        timer = timer_info["end_time"] - now()
        await asyncio.sleep(timer)  # Switch this to a task
        guild_config = self.config.get(guild_id)
        if guild_config and user_id in guild_config["mute_timers"]:
            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                await self.finish_mute_timer(guild_id, user_id)
                return
            user = guild.get_member(int(user_id))
            if not user:
                await self.finish_mute_timer(guild_id, user_id)
                return
            if "removed_roles" in timer_info and timer_info["removed_roles"]:
                for role_id in timer_info["removed_roles"]:
                    role = guild.get_role(role_id)
                    if not role:
                        continue
                    if role not in user.roles:
                        try:
                            await user.add_roles(role)
                        except discord.errors.Forbidden:
                            pass
            mute_role = guild.get_role(self.config[guild_id]["mute_role"])
            if not mute_role:
                self.config[guild_id]["mute_role"] = None
                await self.finish_mute_timer(guild_id, user_id)
                return
            if mute_role in user.roles:
                channel = self.bot.get_channel(timer_info["channel"])
                if channel:
                    try:
                        await user.remove_roles(mute_role)
                        await channel.send(f"**Unmuted:** {user.name}")
                    except discord.errors.Forbidden:
                        pass
            await self.finish_mute_timer(guild_id, user_id)

    @commands.hybrid_command(name="timeout", description="Puts a user in timeout for x duration")
    @commands.cooldown(2, 5, commands.BucketType.user)
    @commands.guild_only()
    @check_if_running()
    @has_required_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(self, ctx, members: Greedy[discord.Member], duration = None, *, reason = None):
        dur_error_msg = f"You forgot to set a duration! Example on putting a user in timeout " \
                        f"for 6 hours because of spamming: `{ctx.prefix}timeout @user 6h spamming`"
        if not duration:
            return await ctx.send(dur_error_msg)
        duration = extract_time(duration)
        if not duration:
            return await ctx.send(dur_error_msg)
        if duration > 60 * 60 * 24 * 14:
            return await ctx.send("You can't put someone in timeout for longer than 2 weeks")
        end_time = datetime.now(tz=timezone.utc) + timedelta(seconds=duration)
        human_end_time = format_date(end_time)
        if not reason:
            reason = "Unspecified"
        _reason = f"By {ctx.author}: {reason}"
        for member in members:
            if ctx.author.id != ctx.guild.owner.id:
                if member.top_role.position >= ctx.author.top_role.position:
                    return await ctx.send(f"Skipped **{member}** due to them being higher, or of equal status to you")
            if member.top_role.position >= ctx.guild.me.top_role.position:
                return await ctx.send(f"Skipped **{member}** due to them being higher, or of equal status to me")
            if member.id == ctx.author.id:
                return await ctx.send("You can't put yourself in timeout..")
            try:
                await member.timeout(end_time, reason=_reason)
            except Forbidden:
                await ctx.send(f"I'm missing permissions to put **{member}** in timeout")
            except NotFound:
                await ctx.send(f"Couldn't find and put **{member}** in timeout")
            else:
                case = await self.cases.add_case(
                    guild_id=ctx.guild.id,
                    user_id=member.id,
                    action="timeout",
                    reason=reason,
                    link=ctx.message.jump_url,
                    created_by=ctx.author.id
                )
                with suppress(Exception):
                    await member.send(
                        f"You've received a timeout in **{ctx.guild}** that will end in `{human_end_time}`"
                        f"\n**Reason** - \"{reason}\" \n[Case #{case}]"
                    )
                await ctx.send(
                    f"Put {member.mention} in timeout for {human_end_time}",
                    allowed_mentions=discord.AllowedMentions(users=True)
                )

    @commands.hybrid_command(
        name="mute",
        aliases=["shutup", "fuckoff", "shush", "shh", "shut", "oppress"],
        description="Prevents a user from being able to chat"
    )
    @commands.cooldown(2, 5, commands.BucketType.user)
    @check_if_running()
    @has_required_permissions(manage_roles=True)
    @commands.bot_has_permissions(embed_links=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def mute(self, ctx, members: Greedy[discord.Member], *, reason="Unspecified"):
        if not members:
            return await ctx.send("**Format:** `.mute {@user} {timer: 2m, 2h, or 2d}`")

        guild_id = str(ctx.guild.id)
        mute_role = None
        async with ctx.channel.typing():
            if self.config[guild_id]["mute_role"]:
                mute_role = ctx.guild.get_role(self.config[guild_id]["mute_role"])
            if not mute_role:
                mute_role = await self.bot.utils.get_role(ctx, "muted")
                if not mute_role:
                    perms = ctx.guild.me.guild_permissions
                    if not perms.manage_channels or not perms.manage_roles:
                        p = get_prefix(ctx)
                        return await ctx.send(
                            "No muted role found, and I'm missing manage_role and manage_channel permissions to set "
                            f"one up. You can set a mute role manually with `{p}mute-role @role` which doesn't "
                            f"have to be a role @mention, and can just be the roles name."
                        )
                    await ctx.send("Creating mute role..")
                    try:
                        mute_role = await ctx.guild.create_role(
                            name="Muted", color=discord.Color(colors.black)
                        )
                    except discord.errors.HTTPException:
                        return await ctx.send("Failed to create the mute role. Operation cancelled")

                    # Set the overwrites for the mute role
                    for i, channel in enumerate(ctx.guild.text_channels):
                        with suppress(discord.errors.Forbidden):
                            await channel.set_permissions(
                                mute_role, send_messages=False
                            )
                        if i + 1 >= len(
                            ctx.guild.text_channels
                        ):  # Prevent sleeping after the last
                            await asyncio.sleep(0.5)
                    for i, channel in enumerate(ctx.guild.voice_channels):
                        with suppress(discord.errors.Forbidden):
                            await channel.set_permissions(mute_role, speak=False)
                        if i + 1 >= len(
                            ctx.guild.voice_channels
                        ):  # Prevent sleeping after the last
                            await asyncio.sleep(0.5)

                if mute_role.position >= ctx.guild.me.top_role.position:
                    return await ctx.send(
                        "My current role's not high enough for me to give, or remove the mute role to, or from anyone"
                    )
                self.config[guild_id]["mute_role"] = mute_role.id

            # Setup the mute role in channels it's not in
            for i, channel in enumerate(ctx.guild.text_channels):
                if (
                    not channel.permissions_for(ctx.guild.me).manage_channels
                    or mute_role in channel.overwrites
                ):
                    continue
                if mute_role not in channel.overwrites:
                    with suppress(discord.errors.Forbidden):
                        await channel.set_permissions(mute_role, send_messages=False)
                    if i + 1 >= len(
                        ctx.guild.text_channels
                    ):  # Prevent sleeping after the last
                        await asyncio.sleep(0.5)
            for i, channel in enumerate(ctx.guild.voice_channels):
                if (
                    not channel.permissions_for(ctx.guild.me).manage_channels
                    or mute_role in channel.overwrites
                ):
                    continue
                if mute_role not in channel.overwrites:
                    with suppress(discord.errors.Forbidden):
                        await channel.set_permissions(mute_role, speak=False)
                    if i + 1 >= len(
                        ctx.guild.voice_channels
                    ):  # Prevent sleeping after the last
                        await asyncio.sleep(0.5)

            if mute_role.position >= ctx.guild.me.top_role.position:
                return await ctx.send("The mute role's above my highest role so I can't manage it")

            timers = []
            timer = expanded_timer = None
            for timer in [re.findall("[0-9]+[smhd]", arg) for arg in reason.split()]:
                timers = [*timers, *timer]
            if timers:
                time_to_sleep = [0, []]
                for timer in timers:
                    reason = str(reason.replace(timer, "")).lstrip(" ").rstrip(" ")
                    raw = "".join(x for x in list(timer) if x.isdigit())
                    if "d" in timer:
                        time = int(timer.replace("d", "")) * 60 * 60 * 24
                        _repr = "day"
                    elif "h" in timer:
                        time = int(timer.replace("h", "")) * 60 * 60
                        _repr = "hour"
                    elif "m" in timer:
                        time = int(timer.replace("m", "")) * 60
                        _repr = "minute"
                    else:  # 's' in timer
                        time = int(timer.replace("s", ""))
                        _repr = "second"
                    time_to_sleep[0] += time
                    time_to_sleep[1].append(
                        f"{raw} {_repr if raw == '1' else _repr + 's'}"
                    )
                timer, expanded_timer = time_to_sleep
                expanded_timer = ", ".join(expanded_timer)

        if not reason:
            reason = "Unspecified"
        for user in list(members):
            if ctx.author.id != ctx.guild.owner.id:
                if user.top_role.position >= ctx.author.top_role.position:
                    return await ctx.send("That user is above your paygrade, take a seat")
                if user.top_role.position >= ctx.guild.me.top_role.position:
                    return await ctx.send("That users top role is above mine, so I can't manage them")
            updated = False
            if mute_role in user.roles:
                user_id = str(user.id)
                if guild_id in self.tasks and user_id in self.tasks[guild_id]:
                    self.tasks[guild_id][user_id].cancel()
                    del self.tasks[guild_id][user_id]
                    updated = True
            removed_roles = []

            async def ensure_muted(user=user, removed_roles=removed_roles):
                if user.guild_permissions.administrator:
                    choice = await self.bot.utils.get_choice(
                        ctx,
                        "Remove admin roles",
                        "Leave as is",
                        name=f"{user.name} can still talk",
                        user=ctx.author,
                    )
                    if choice == "Remove admin roles":
                        for role in user.roles:
                            if role.permissions.administrator:
                                await user.remove_roles(role)
                                removed_roles.append(role.id)
                return None

            case = await self.cases.add_case(ctx.guild.id, user.id, "mute", reason, ctx.message.jump_url, ctx.author.id)
            additional = ""
            usr_additional = ""
            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    "select channel_id from modmail where guild_id = %s;",
                    (guild_id,),
                )
                result = await cur.fetchone()
            if result:
                usr_additional += f" [use .appeal {case} if this was a mistake]"

            if not timers:
                try:
                    await user.send(f"You've been muted in {ctx.guild} for {reason}" + usr_additional)
                except:
                    additional += " (Unable to notify user via DM)"
                self.bot.suppressed.append(mute_role.id)
                await user.add_roles(mute_role)
                logger: Optional[Logger] = self.bot.get_cog("Logger")  # type: ignore
                if logger:
                    await logger.on_mute(ctx, user, expanded_timer, reason)
                await ctx.send(
                    f"Muted {user.display_name} for {reason} [Case #{case}]" + additional,
                    view=MuteView(ctx, user, case, reason.replace("Unspecified", ""), timer)
                )
                await ensure_muted()
                continue

            if timer > 15552000:  # 6 months
                return await ctx.send(
                    "No way in hell I'm waiting that long to unmute. "
                    "You'll have to do it yourself >:("
                )
            self.bot.suppressed.append(mute_role.id)
            await user.add_roles(mute_role)
            logger: Optional[Logger] = self.bot.get_cog("Logger")  # type: ignore
            if logger:
                await logger.on_mute(ctx, user, expanded_timer, reason)
            await ensure_muted()
            timer_info = {
                "channel": ctx.channel.id,
                "user": user.id,
                "end_time": now() + timer,
                "mute_role": mute_role.id,
                "removed_roles": removed_roles,
            }
            try:
                await user.send(f"You've been muted in {ctx.guild} for {expanded_timer} for {reason}" + usr_additional)
            except:
                additional += " (Unable to notify user via DM)"
            if updated:
                await ctx.send(
                    f"Updated the mute for **{user.name}** to {expanded_timer} "
                    f"for {reason} [Case #{case}]" + additional
                )
            else:
                await ctx.send(
                    f"Muted **{user.name}** for {expanded_timer} "
                    f"for {reason} [Case #{case}]" + additional,
                    view=MuteView(ctx, user, case, reason.replace("Unspecified", ""), timer)
                )

            user_id = str(user.id)
            self.config[guild_id]["mute_timers"][user_id] = timer_info
            await self.save_data()
            task = self.bot.loop.create_task(
                self.handle_mute_timer(guild_id, user_id, timer_info)
            )
            if guild_id not in self.tasks:
                self.tasks[guild_id] = {}
            self.tasks[guild_id][user_id] = task

    @commands.Cog.listener()
    async def on_ready(self):
        for guild_id, tasks in list(self.tasks.items()):
            for user_id, task in list(tasks.items()):
                if not task.done():
                    continue
                if not task.cancelled() and (error := task.exception()) is not None:
                    self.bot.log.critical(f"A mute task errored: {error!r}")
                if self.tasks.get(guild_id, {}).get(user_id) is task:
                    del self.tasks[guild_id][user_id]
            if guild_id in self.tasks and not self.tasks[guild_id]:
                del self.tasks[guild_id]
        for guild_id, data in list(self.config.items()):
            for user_id, timer_info in data["mute_timers"].items():
                if guild_id not in self.tasks or user_id not in self.tasks[guild_id]:
                    task = self.bot.loop.create_task(
                        self.handle_mute_timer(guild_id, user_id, timer_info)
                    )
                    if guild_id not in self.tasks:
                        self.tasks[guild_id] = {}
                    self.tasks[guild_id][user_id] = task

    @commands.hybrid_command(
        name="unmute",
        aliases=["unshutup", "unfuckoff", "unshh", "unshush", "unshut", "unoppress"],
        description="Removes the mute role from a user"
    )
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    @has_required_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unmute(self, ctx, users: Greedy[discord.Member]):
        if not users:
            return await ctx.send("You need to specify who to unmute")
        for user in users:
            if not user:
                return await ctx.send("**Unmute Usage:**\n.unmute {@user}")
            if ctx.author.id != ctx.guild.owner.id:
                if user.top_role.position >= ctx.author.top_role.position:
                    return await ctx.send("That user is above your paygrade, take a seat")
            if user.is_timed_out():
                await user.edit(timed_out_until=None)
                await ctx.send("Removed their timeout")
                continue
            guild_id = str(ctx.guild.id)
            user_id = str(user.id)
            mute_role = None
            if self.config[guild_id]["mute_role"]:
                mute_role = ctx.guild.get_role(self.config[guild_id]["mute_role"])
                if not mute_role:
                    await ctx.send(
                        "The configured mute role was deleted, so I'll try to find another"
                    )
            if not mute_role:
                mute_role = await self.bot.utils.get_role(ctx, "muted")
            if not mute_role:
                p = get_prefix(ctx)
                return await ctx.send(
                    f"No mute role found? If it doesn't have `muted` in the name use `{p}mute-role @role` "
                    f"which doesn't need to be a role @mention, and you can just the roles name."
                )
            if mute_role not in user.roles:
                return await ctx.send(f"{user.display_name} is not muted")
            self.bot.suppressed.append(mute_role.id)
            await user.remove_roles(mute_role)
            logger: Optional[Logger] = self.bot.get_cog("Logger")  # type: ignore
            if logger:
                await logger.on_unmute(ctx, user)
            if user_id in self.config[guild_id]["mute_timers"]:
                del self.config[guild_id]["mute_timers"][user_id]
                await self.save_data()
            if guild_id in self.tasks and user_id in self.tasks[guild_id]:
                if not self.tasks[guild_id][user_id].done():
                    self.tasks[guild_id][user_id].cancel()
                del self.tasks[guild_id][user_id]
                if not self.tasks[guild_id]:
                    del self.tasks[guild_id]
            await ctx.send(f"Unmuted {user.name}")

    @commands.hybrid_command(name="kick", description="Kicks a user from the server")
    @commands.cooldown(2, 5, commands.BucketType.user)
    @check_if_running()
    @has_required_permissions(kick_members=True)
    @commands.bot_has_permissions(embed_links=True, kick_members=True)
    async def kick(self, ctx, members: Greedy[Union[discord.Member, discord.User]], *, reason="Unspecified"):
        if not members:
            return await ctx.send("You need to properly specify who to kick")
        e = discord.Embed(color=colors.fate)
        e.set_author(name=f"Kicking members", icon_url=ctx.author.display_avatar.url)
        msg = await ctx.send(embed=e)
        e.description = ""
        for i, member in enumerate(members):
            if isinstance(member, discord.User):  # Was kicked already
                continue
            if ctx.author.id != ctx.guild.owner.id:
                if member.top_role.position >= ctx.author.top_role.position:
                    e.description += f"\n❌ {member} is Higher Than You"
                    continue
            if member.top_role.position >= ctx.guild.me.top_role.position:
                e.description += f"❌ {member} is Higher Than Me"
                continue
            case = await self.cases.add_case(
                ctx.guild.id, member.id, "kick", reason, ctx.message.jump_url, ctx.author.id
            )
            content = f"You've been kicked in {ctx.guild} by {ctx.author} for {reason}"
            rows = await self.bot.rowcount(
                "select * from modmail where guild_id = %s;", (ctx.guild.id,)
            )
            if rows:
                content += f". Use `.appeal {case}` if you feel there's a mistake"
            else:
                content += f" [Case #{case}]"
            with suppress(NotFound, Forbidden, HTTPException):
                await member.send(content)
            await member.kick(
                reason=f"Kicked by {ctx.author} with ID: {ctx.author.id} for {reason}"
            )
            e.description += f"✅ {member} [Case #{case}]"
            if i % 2 == 0 and i != len(members) - 1:
                await msg.edit(embed=e)
        await msg.edit(embed=e)

    @commands.hybrid_command(name="ban", aliases=["yeet"], description="Bans a user from the server")
    @commands.cooldown(2, 10, commands.BucketType.guild)
    @check_if_running()
    @commands.guild_only()
    @has_required_permissions(ban_members=True)
    @commands.bot_has_permissions(embed_links=True, ban_members=True)
    async def ban(self, ctx, *, users: str):
        """ Ban cmd that supports more than just members """
        converter = commands.UserConverter()
        args: list[str] = users.split()
        users: list[User] = []

        for word in list(args):
            if "@" not in word and not (word.isdigit() and len(word) > 10):
                break
            try:
                user = await converter.convert(ctx, word)
                users.append(user)
                args.pop(0)
            except commands.UserNotFound:
                pass

        if not args:
            reason = "Unspecified"
        elif len(args) > 1:
            reason = " ".join(args)
        else:
            reason = args.pop(0)

        reason = reason[:128]
        users_to_ban = len(users)
        e = discord.Embed(color=colors.fate)
        if users_to_ban == 0:
            return await ctx.send("You need to specify who to ban")
        elif users_to_ban > 1:
            e.set_author(
                name=f"Banning {users_to_ban} user{'' if users_to_ban > 1 else ''}",
                icon_url=ctx.author.display_avatar.url,
            )
        e.set_thumbnail(
            url="https://cdn.discordapp.com/attachments/514213558549217330/514345278669848597/8yx98C.gif"
        )
        msg = await ctx.send(embed=e)
        failed = 0
        for user in users:
            member: Member = discord.utils.get(ctx.guild.members, id=user.id)
            if member:
                if ctx.author.id != ctx.guild.owner_id:
                    if member.top_role.position >= ctx.author.top_role.position:
                        failed += 1
                        if users_to_ban < 10:
                            e.add_field(
                                name=f"◈ Failed to ban {member}",
                                value="This users is above your paygrade",
                                inline=False,
                            )
                            await msg.edit(embed=e)
                            continue
                if member.top_role.position >= ctx.guild.me.top_role.position:
                    failed += 1
                    if users_to_ban < 10:
                        e.add_field(
                            name=f"◈ Failed to ban {member}",
                            value="I can't ban this user",
                            inline=False,
                        )
                        await msg.edit(embed=e)
                    continue
            case = await self.cases.add_case(
                ctx.guild.id, user.id, "ban", reason, ctx.message.jump_url, ctx.author.id
            )
            content = f"You've been banned in {ctx.guild} by {ctx.author} for {reason}"
            rows = await self.bot.rowcount(
                "select * from modmail where guild_id = %s;", (ctx.guild.id,)
            )
            if rows:
                content += f". Use `.appeal {case}` if you feel there's a mistake"
            else:
                content += f" [Case #{case}]"
            if f"@{user.id}>" in ctx.message.content:
                with suppress(NotFound, Forbidden, HTTPException):
                    await user.send(content)
            try:
                await ctx.guild.ban(
                    user, reason=f"{ctx.author}: {reason}"[:512], delete_message_days=0
                )
            except discord.errors.Forbidden:
                return await ctx.send("It seems I lost ban permissions. Operation cancelled")
            except discord.errors.HTTPException as e:
                if "Max number of bans for non-guild members" in str(e):
                    return await ctx.send(
                        "You've reached the limit of users you can "
                        "ban that aren't in the server. Try again later"
                    )
                return await ctx.send(e)
            if users_to_ban < 10:
                e.add_field(
                    name=f"◈ Banned {user} [Case #{case}]", value=f"Reason: {reason}", inline=False
                )
        if not e.fields:
            e.colour = colors.red
            e.set_author(name="Couldn't ban any of the specified user(s)")
        if users_to_ban > 10:
            e.description = f"Banned {users_to_ban - failed} users. `{failed}` failed"
        await msg.edit(embed=e)

    @commands.hybrid_command(
        name="get-ban",
        aliases=["getban", "searchban", "search-ban", "searchbans", "search-bans"],
        description="Gives extra information on a ban"
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    @check_if_running()
    @has_required_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True, view_audit_log=True)
    async def get_ban(self, ctx, user: discord.User):
        try:
            ban = await ctx.guild.fetch_ban(user)
        except NotFound:
            return await ctx.send(f"{user} isn't banned")

        action = discord.AuditLogAction.ban
        async for entry in ctx.guild.audit_logs(limit=2500, action=action):
            if entry.target.id == user.id:
                entry: discord.AuditLogEntry = entry
                break
        else:
            return await ctx.send("Couldn't find any results in the audit log. It's probably too old")

        e = discord.Embed(color=self.bot.config["theme_color"])
        e.set_author(name=f"Ban Entry for {user}", icon_url=user.display_avatar.url)
        e.description = f"> {ban.reason if ban.reason else 'No reason specified'}"
        e.add_field(
            name="◈ Banned by",
            value=str(entry.user)
        )
        e.add_field(
            name="Banned at",
            value=discord.utils.format_dt(entry.created_at, style="F"),
            inline=False
        )
        await ctx.send(embed=e)

    @commands.hybrid_command(
        name="archive",
        description="Archives a thread"
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    @check_if_running()
    @has_required_permissions(manage_threads=True)
    @commands.bot_has_permissions(manage_threads=True)
    async def archive(self, ctx):
        if not isinstance(ctx.channel, discord.Thread):
            return await ctx.send("You can only run this command in threads", ephemeral=True)
        await ctx.send("Archiving and locking the thread", ephemeral=True)
        await ctx.channel.edit(archived=True, locked=True)

    @commands.hybrid_command(
        name="import-bans",
        aliases=["importbans", "transfer-bans", "transferbans"],
        description="Copies the ban-list from another server"
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    @check_if_running()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(ban_members=True)
    @commands.max_concurrency(1, commands.BucketType.guild)
    @commands.max_concurrency(1, commands.BucketType.user)
    async def import_bans(self, ctx, server_id: int):
        guild = self.bot.get_guild(server_id)
        if not guild:
            return await ctx.send("Server not found. Maybe i'm not in it?")
        user = guild.get_member(ctx.author.id)
        if not user:
            return await ctx.send("It doesn't seem you're in that server")
        if not user.guild_permissions.ban_members:
            return await ctx.send("You need ban_member permission(s) in that server to import its ban list")
        bans = [ban async for ban in guild.bans(limit=None)]
        current_bans = [ban async for ban in ctx.guild.bans(limit=None)]
        current_banned_ids = {ban.user.id for ban in current_bans}
        last_executed = self.guild_last_executed.get(ctx.guild.id)
        if last_executed:
            # If it's been less than 24 hours, prevent the user from executing the command
            if datetime.utcnow() - last_executed < timedelta(hours=24):
                return await ctx.send("You can only import 1000 bans once every 24 hours. Please wait.")
        users_to_ban = [
            [entry.user, entry.reason]
            for entry in bans
            if entry.user.id not in current_banned_ids
            and not ctx.guild.get_member(entry.user.id)
        ]
        if not users_to_ban:
            return await ctx.send("No bans left to import")
        users_to_ban = users_to_ban[:1000]

        # Create and send a confirmation message
        view = ConfirmView()
        await ctx.send("Are you sure you want to import these bans?", view=view)

        await view.wait()

        if not view.confirmed:
            return

        msg = await ctx.send(f"Importing bans (0/{len(users_to_ban)})")
        try:
            for i, (user, reason) in enumerate(users_to_ban):
                to_ban = round(len(users_to_ban) / 5)
                if to_ban and i % to_ban == 0:
                    await msg.edit(content=f"Importing bans ({i + 1}/{len(users_to_ban)})")
                if not reason:
                    reason = f"{ctx.author} importing bans"
                await ctx.guild.ban(user, reason=reason, delete_message_days=0)

                await asyncio.sleep(0.2)  # Rate limit
        except Forbidden:
            with suppress(Exception):
                await ctx.send("I no longer have permission(s) to ban. Operation cancelled")
        except HTTPException as error:
            await ctx.send(error)
        else:
            await msg.edit(content=f"Importing bans ({len(users_to_ban)}/{len(users_to_ban)})")
            self.guild_last_executed[ctx.guild.id] = datetime.utcnow()
            await ctx.send("Finished importing bans")

    @commands.hybrid_command(name="unban", description="Removes the ban for a user")
    @commands.cooldown(2, 5, commands.BucketType.user)
    @check_if_running()
    @has_required_permissions(ban_members=True)
    @commands.bot_has_permissions(
        embed_links=True, ban_members=True, view_audit_log=True
    )
    async def unban(self, ctx, users: Greedy[discord.User], *, reason=":author:"):
        if not users:
            async for entry in ctx.guild.audit_logs(
                limit=1, action=discord.AuditLogAction.ban
            ):
                users = (entry.target,)
        if len(users) == 1:
            user = users[0]
            try:
                await ctx.guild.unban(
                    user, reason=reason.replace(":author:", str(ctx.author))
                )
            except discord.errors.NotFound:
                return await ctx.send("That user isn't banned")
            case = await self.cases.add_case(
                ctx.guild.id, user.id, "unban", str(ctx.author), ctx.message.jump_url, ctx.author.id
            )
            e = discord.Embed(color=colors.red)
            e.set_author(name=f"{user} unbanned [Case #{case}]", icon_url=user.display_avatar.url)
            await ctx.send(embed=e)
        else:
            e = discord.Embed(color=colors.green)
            e.set_author(
                name=f"Unbanning {len(users)} users", icon_url=ctx.author.display_avatar.url
            )
            e.description = ""
            msg = await ctx.send(embed=e)
            index = 1
            for user in users:
                try:
                    await ctx.guild.unban(
                        user, reason=reason.replace(":author:", str(ctx.author))
                    )
                except discord.errors.NotFound:
                    e.description += f"Couldn't unban {user}"
                    continue
                case = await self.cases.add_case(
                    ctx.guild.id, user.id, "unban", str(ctx.author), ctx.message.jump_url, ctx.author.id
                )
                e.description += f"✅ {user} [Case #{case}]"
                if index == 5:
                    await msg.edit(embed=e)
                    index = 1
                else:
                    index += 1
            await msg.edit(embed=e)

    @commands.hybrid_command(name="roles", description="Shows how many people have each role")
    @commands.cooldown(2, 5, commands.BucketType.user)
    @check_if_running()
    @commands.has_permissions(manage_roles=True)
    @commands.cooldown(1, 15, commands.BucketType.channel)
    @check_if_running()
    async def roles(self, ctx):
        """ Formats the role list to show how many members each role has """
        longest = sorted(ctx.guild.roles, key=lambda r: len(r.name), reverse=True)[0]
        length = len(longest.name) + 3
        lines = [f"Name:{' ' * (length - 5)}Members:"]
        for role in sorted(ctx.guild.roles, key=lambda r: r.position, reverse=True):
            name = normalize('NFKD', role.name).encode('ascii', 'ignore').decode()
            name = "".join(c for c in name if c in printable)
            lines.append(f"{name}{' ' * (length - len(name))}{len(role.members)}")
        roles = "\n".join(lines)
        for chunk in split(roles, 1900):
            chunk = chunk.lstrip("\n")
            await ctx.send(f"```\n{chunk}```")

    @commands.hybrid_command(name="mass-nick", aliases=["massnick"], description="Sets the nick of everyone in the server")
    @commands.cooldown(2, 5, commands.BucketType.user)
    @check_if_running()
    @has_required_permissions(manage_nicknames=True)
    @commands.bot_has_guild_permissions(manage_nicknames=True)
    async def mass_nick(self, ctx, *, nick=""):
        # Define the embed generator
        def gen_embed(iteration):
            e = discord.Embed(color=colors.fate)
            e.set_author(name="Mass Updating Nicknames", icon_url=ctx.author.display_avatar.url)
            e.description = (
                f"{iteration + 1}/{len(members)} complete"
                f"\n1 nick per 1.21 seconds"
                f"\nETA of {get_time(round((len(members) - (iteration + 1)) * 1.21))}"
            )
            return e

        # Check for nickname length
        if len(nick) > 32:
            await ctx.send("Nicknames cannot exceed 32 characters in length")
            return

        # Collect members to change
        members = [member for member in ctx.guild.members if member.top_role.position < ctx.author.top_role.position and member.top_role.position < ctx.guild.me.top_role.position and (member.nick if not nick else member.display_name) != nick]

        if not members:
            await ctx.send("There aren't any possible members I can nick")
            return

        view = CancelButton("manage_roles")

        # Send initial message
        msg = await ctx.send(embed=gen_embed(0), view=view)

        await self.cases.add_case(
            ctx.guild.id, ctx.author.id, "massnick", nick, msg.jump_url, ctx.author.id
        )

        last_updated = now()

        for i, member in enumerate(members[:3600]):
            await asyncio.sleep(1.21)

            if view.is_cancelled:
                view.disabled = True
                await msg.edit(content="Message Inactive: Operation Cancelled")
                return

            if now() - 5 > last_updated:  # try checking the bot's internal message cache instead
                await msg.edit(embed=gen_embed(i))
                last_updated = now()

            try:
                await member.edit(nick=nick)
            except discord.errors.NotFound:
                pass
            except discord.errors.Forbidden:
                if not ctx.guild.me.guild_permissions.manage_nicknames:
                    view.stop()
                    await msg.edit(content="Message Inactive: Missing Permissions", view=None)
                    await ctx.send("I'm missing permissions to manage nicknames. Canceling the operation :[")
                    return

        view.stop()
        await msg.edit(content="Operation Complete", embed=gen_embed(len(members) - 1), view=None)

    @commands.hybrid_command(name="mass-role", aliases=["massrole"], description="Give a role to everyone in the server")
    @commands.cooldown(2, 5, commands.BucketType.user)
    @check_if_running()
    @has_required_permissions(manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def mass_role(self, ctx, *, role=None):
        def gen_embed(iteration):
            e = discord.Embed(color=colors.fate)
            e.set_author(name=f"Mass {action} Roles", icon_url=ctx.author.display_avatar.url)
            e.description = (
                f"{iteration + 1}/{len(members)} complete"
                f"\n1 role per 1.21 seconds"
                f"\nETA of {get_time(round((len(members) - (iteration + 1)) * 1.21))}"
            )

            return e

        if not role:
            e = discord.Embed(color=colors.fate)
            e.set_author(name="MassRole Usages", icon_url=ctx.author.display_avatar.url)
            e.description = f"Add, or remove roles from members in mass"
            p = get_prefix(ctx)
            e.add_field(name=f"{p}massrole @Role", value="Mass adds roles")
            e.add_field(name=f"{p}massrole -@Role", value="Mass removes roles")
            e.add_field(
                name="Note",
                value="@Role can be replaced with role names, role mentions, or role ids",
                inline=False,
            )
            return await ctx.send(embed=e)

        role = role.lstrip("+")
        action = "Adding"
        if role.startswith("-"):
            action = "Removing"
            role = role.lstrip("-")
        role = await self.bot.utils.get_role(ctx, role)
        if not role:
            return
        if role.position >= ctx.guild.me.top_role.position:
            return await ctx.send("That role's higher than I can manage")
        members = []
        user_limited = 0
        bot_limited = 0
        already_has = 0
        for member in list(ctx.guild.members):
            await asyncio.sleep(0)
            if member.top_role.position < ctx.author.top_role.position:
                if member.top_role.position < ctx.guild.me.top_role.position:
                    if role not in member.roles if action == "Adding" else role in member.roles:
                        members.append(member)
                    else:
                        already_has += 1
                else:
                    bot_limited += 1
            else:
                user_limited += 1
        if not members:
            return await ctx.send(
                "There aren't any possible members I can give, or remove that role from. "
                f"{user_limited} that were above you, "
                f"{bot_limited} that were above me, "
                f"and {already_has} already have the role"
            )
        view = CancelButton("manage_roles")
        if len(members) > 3600:
            msg = await ctx.send(
                "Bruh.. you get ONE hour, but that's it.", embed=gen_embed(0), view=view
            )
        else:
            msg = await ctx.send(embed=gen_embed(0), view=view)
        await self.cases.add_case(
            ctx.guild.id, ctx.author.id, "massrole", role.mention, msg.jump_url, ctx.author.id
        )
        async with ctx.typing():
            last_updated = now()
            for i, member in enumerate(members[:3600]):
                await asyncio.sleep(1.21)
                if view.is_cancelled:
                    view.disabled = True
                    return await msg.edit(
                        content="Message Inactive: Operation Cancelled",
                        view=None
                    )
                if now() - 5 > last_updated:
                    await msg.edit(embed=gen_embed(i))
                    last_updated = now()
                try:
                    if action == "Adding":
                        await member.add_roles(role)
                    else:
                        await member.remove_roles(role)
                except discord.errors.NotFound:
                    pass
                except discord.errors.Forbidden:
                    if not ctx.guild.me.guild_permissions.manage_roles:
                        view.stop()
                        await msg.edit(content="Message Inactive: Missing Permissions", view=None)
                        return await ctx.send(
                            "I'm missing permissions to manage roles. Canceling the operation :["
                        )
            view.stop()
            await msg.edit(content="Operation Complete", embed=gen_embed(i), view=None)

    @commands.hybrid_command(name="nick", description="Shortcut command for setting a users nickname")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.has_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def nick(self, ctx, user, *, nick=""):
        user = await self.bot.utils.get_user(ctx, user)
        if not user:
            return await ctx.send("User not found")
        if ctx.author.id != ctx.guild.owner.id:
            if user.top_role.position >= ctx.author.top_role.position:
                return await ctx.send("That user is above your paygrade, take a seat")
            if user.top_role.position >= ctx.guild.me.top_role.position:
                return await ctx.send("I can't edit that users nick ;-;")
        if len(nick) > 32:
            return await ctx.send(
                "That nickname is too long! Must be `32` or fewer in length"
            )
        try:
            await user.edit(nick=nick)
            await ctx.message.add_reaction("👍")
        except commands.MissingPermissions:
            await ctx.send("Missing permissions to change their nick")

    @commands.hybrid_command(name="role", description="Shortcut command for giving a user a role")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def role(self, ctx, user, *, role):
        user = await self.bot.utils.get_user(ctx, user)
        if user:
            user = ctx.guild.get_member(user.id)
        if not user:
            return await ctx.send("User not found")
        timer = None
        if " " in role and (timer := extract_time(role.split()[::-1][0])):
            role = role.split()[0]
            if timer > 60 * 60 * 24 * 7:
                return await ctx.send(f"You can't set a role timer longer than a week")
            if timer < 60:
                return await ctx.send("Role timer's can't be shorter than a minute")
        converter = commands.RoleConverter()
        try:
            result = await converter.convert(ctx, role)
            role = result  # type: discord.Role
        except commands.BadArgument:
            self.bot.log.debug("Role converter missed; trying Fate's role lookup")
        if not isinstance(role, discord.Role):
            role = await self.bot.utils.get_role(ctx, role)
        if not role:
            return await ctx.send("Role not found")

        if ctx.author.id != ctx.guild.owner.id:
            if user.top_role.position >= ctx.author.top_role.position:
                return await ctx.send("This user is above your paygrade, take a seat")
            if role.position >= ctx.author.top_role.position:
                return await ctx.send("This role is above your paygrade, take a seat")
        sensitive = ["kick_members", "ban_members", "manage_roles", "manage_channels"]
        if any(getattr(role.permissions, perm) for perm in sensitive):
            if not await GetConfirmation(ctx, "This role has sensitive permissions, are you sure?"):
                return
        if role in user.roles:
            await user.remove_roles(role)
            msg = f"Removed **{role.name}** from @{user.name}"
            add = True
        else:
            await user.add_roles(role)
            msg = f"Gave **{role.name}** to **@{user.name}**"
            add = False
        if timer:
            msg += f" for {get_time(timer)}"
        await ctx.send(msg)
        if timer:
            if user.id in self.timers.db:
                return await ctx.send("Each user can only have one role timer at a time")
            self.timers.run(
                channel_id=ctx.channel.id,
                message_id=ctx.message.id,
                role_id=role.id,
                user_id=user.id,
                add=add,
                sleep_for=timer
            )

    @commands.hybrid_command(name="transfer", description="Transfers roles between two members")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(1, commands.BucketType.guild)
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def transfer(self, ctx, user1: discord.Member, user2: discord.Member):
        highest = user1.top_role.position
        if user2.top_role.position > highest:
            highest = user2.top_role.position
        if highest >= ctx.author.top_role.position:
            return await ctx.send("You can't modify someone higher, or of equal status to you")
        if highest >= ctx.guild.me.top_role.position:
            return await ctx.send("They're too high up for me to modify")
        await ctx.send("Beginning role transfer")
        roles = [role for role in user1.roles if role not in user2.roles]
        await user1.remove_roles(*roles)
        await user2.add_roles(*roles)
        await ctx.send(
            f"Transferred {len(roles)} roles to {user2.mention}",
            allowed_mentions=discord.AllowedMentions(users=True)
        )

    @commands.hybrid_command(name="tag", aliases=["tags"], description="Toggles tags on a forum post")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(1, commands.BucketType.guild)
    @commands.guild_only()
    @commands.has_permissions(manage_threads=True)
    @commands.bot_has_permissions(manage_threads=True)
    async def tag(self, ctx, *, tags: str):
        if not isinstance(ctx.channel, discord.Thread) or not isinstance(ctx.channel.parent, discord.ForumChannel):
            return await ctx.send("This can only be used in forum threads")

        available_tags = ctx.channel.parent.available_tags
        exact_matches = {tag.name.casefold(): tag for tag in available_tags}
        found_tags = []
        for requested in tags.split():
            normalized = requested.casefold()
            matches = [exact_matches[normalized]] if normalized in exact_matches else [
                tag for tag in available_tags if normalized in tag.name.casefold()
            ]
            for tag in matches:
                if tag not in found_tags:
                    found_tags.append(tag)
        if not found_tags:
            return await ctx.send(f"No tags found under those {s(tags.count(' '))}")

        # Replace tags with new set of tags
        if ctx.invoked_with.lower() == "tags":
            applied = []
        else:
            applied = list(ctx.channel.applied_tags)

        actions = []
        for tag in found_tags:
            name = f"{tag.emoji} {tag.name}" if tag.emoji else tag.name
            if tag in applied:
                applied.remove(tag)
                actions.append(f"{emojis.off} **{name}**")
            else:
                applied.append(tag)
                actions.append(f"{emojis.on} **{name}**")

        self.bot.log.info(str(applied))
        self.bot.log.info("\n".join(actions))
        await ctx.channel.edit(applied_tags=applied)
        await ctx.send("\n".join(actions))

    async def handle_timer(self, channel_id, message_id, role_id, user_id, add, sleep_for):
        await asyncio.sleep(sleep_for)
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return
        guild = channel.guild
        member = guild.get_member(user_id)
        role = guild.get_role(role_id)
        if not member or not role:
            return
        reference = None
        try:
            ref = await channel.fetch_message(message_id)
            reference = ref
        except (NotFound, Forbidden):
            pass
        with suppress(Exception):
            if add:
                if role in member.roles:
                    return
                await member.add_roles(role)
                msg = f"Gave **{role.name}** to **@{member.name}**"
            else:
                if role not in member.roles:
                    return
                await member.remove_roles(role)
                msg = f"Removed **{role.name}** from @{member.name}"
            await channel.send(msg, reference=reference)

    @commands.command(name="rename", description="Renames a user, role, or channel")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(administrator=True)
    async def rename(self, ctx, target: Union[Member, Role, TextChannel], *, new_name=""):
        if not isinstance(target, Member) and not new_name:
            return await ctx.send("You need to specify the new name after the target you're renaming")
        old_name = str(target.display_name if hasattr(target, "display_name") else target.name)
        new_name = new_name[:28]
        try:
            if isinstance(target, Member):
                if ctx.author.id != ctx.guild.owner.id:
                    if target.top_role.position >= ctx.author.top_role.position:
                        return await ctx.send("That user's above your paygrade, take a seat")
                await target.edit(nick=new_name)
            elif isinstance(target, Role):
                if ctx.author.id != ctx.guild.owner.id:
                    if target.position >= ctx.author.top_role.position:
                        return await ctx.send("This role is above your paygrade, take a seat")
                await target.edit(name=new_name)
            elif isinstance(target, TextChannel):
                if not target.permissions_for(ctx.author).manage_channels:
                    return await ctx.send("You don't have the required permissions to edit that channel")
                await target.edit(name=new_name)
        except Forbidden:
            await ctx.send("I'm missing permissions to change that targets name")
        else:
            await ctx.send(f"Renamed {target.mention} from {old_name}")

    async def warn_user(self, channel, user, reason, context):
        guild = channel.guild
        guild_id = str(guild.id)
        user_id = str(user.id)
        if guild_id not in self.config:
            self.config[guild_id] = self.template
        warns = self.config[guild_id]["warns"]
        if user_id not in warns:
            warns[user_id] = []
        if not isinstance(warns[user_id], list):
            warns[user_id] = []

        warns[user_id].append([reason, datetime.now(tz=timezone.utc).isoformat()])
        total_warns = len(warns[user_id])
        await self.save_data()

        logger: Optional[Logger] = self.bot.get_cog("Logger")  # type: ignore
        if logger:
            await logger.on_warn(context, user, reason, total_warns)

        e = discord.Embed(color=colors.fate)
        url = self.bot.user.display_avatar.url
        if user.display_avatar.url:
            url = user.display_avatar.url
        e.set_author(name=f"{user.name} has been warned [Warn #{total_warns}]", icon_url=url)
        e.description = f"\n\n  **Reason** - `{reason}`"
        case = await self.cases.add_case(
            int(guild_id), user.id, "warn", reason, context.message.jump_url, context.author.id
        )
        e.description += f"\n(Case #{case})"
        destination = channel
        if settings_cog := self.bot.get_cog("Settings"):
            settings = settings_cog.get_config(guild.id)
            if warning_channel := guild.get_channel(settings["warns_channel"] or 0):
                destination = warning_channel
        try:
            await destination.send(embed=e)
        except Forbidden:
            if destination.id == channel.id:
                raise
            destination = channel
            await channel.send(embed=e)
        if destination.id != channel.id:
            await channel.send(f"Warned {user.mention} • Details sent to {destination.mention}")
        try:
            await user.send(f"**You've received a warning in {channel.guild} for**;\n \"{reason}\"")
        except (Forbidden, HTTPException):
            self.bot.log.debug(f"Couldn't DM warning to user {user.id}")

    @commands.hybrid_command(name="warn", description="Dms and logs a warn on the user")
    @commands.guild_only()
    @commands.cooldown(2, 5, commands.BucketType.user)
    @check_if_running()
    @has_warn_permission()
    async def warn(self, ctx, users: Greedy[discord.Member], *, reason="Unspecified"):
        if not users:
            return await ctx.send("You need to specify who to warn")
        if len(users) > 1 and len(ctx.message.raw_mentions) < len(users):
            users = users[:1]
        for user in list(users):
            if user.bot:
                await ctx.send(f"You can't warn {user.mention} because they're a bot")
                continue
            if ctx.author.id != ctx.guild.owner.id:
                if user.top_role.position >= ctx.author.top_role.position:
                    await ctx.send(f"{user.name} is above your paygrade, take a seat")
                    continue
            await self.warn_user(ctx.channel, user, reason, ctx)

    @commands.hybrid_command(name="delwarn", aliases=["del-warn"], description="Removes a users warn")
    @commands.guild_only()
    @commands.cooldown(2, 5, commands.BucketType.user)
    @check_if_running()
    @has_warn_permission()
    @commands.bot_has_permissions(add_reactions=True)
    async def delwarn(self, ctx, user: Greedy[discord.Member], *, partial_reason):
        def check(reaction, reactor):
            return (
                reactor == ctx.author
                and reaction.message.id == msg.id
                and str(reaction.emoji) in ["✔", "❌"]
            )

        guild_id = str(ctx.guild.id)
        for member in list(set(user)):
            user_id = str(member.id)
            if user_id not in self.config[guild_id]["warns"]:
                await ctx.send(f"{member} has no warns")
                continue
            for reason, warn_time in self.config[guild_id]["warns"][user_id]:
                if partial_reason in reason:
                    e = discord.Embed(color=colors.fate)
                    e.set_author(name="Is this the right warn?")
                    e.description = reason
                    msg = await ctx.send(embed=e)
                    await msg.add_reaction("✔")
                    await asyncio.sleep(0.5)
                    await msg.add_reaction("❌")
                    try:
                        reaction, _reactor = await self.bot.wait_for(
                            "reaction_add", timeout=60.0, check=check
                        )
                    except asyncio.TimeoutError:
                        await msg.edit(
                            content="Inactive Message: timed out due to no response"
                        )
                        if ctx.channel.permissions_for(ctx.guild.me).manage_messages:
                            await msg.clear_reactions()
                        return
                    else:
                        with suppress(ValueError, NotFound, Forbidden):
                            if str(reaction.emoji) == "✔":
                                self.config[guild_id]["warns"][user_id].remove(
                                    [reason, warn_time]
                                )
                                await self.save_data()
                                await ctx.message.delete()
                                await msg.delete()
                            else:
                                await msg.delete()
                        break

    @commands.hybrid_command(name="clearwarns", aliases=["clear-warns"], description="Erases all of a users warns")
    @commands.guild_only()
    @commands.cooldown(2, 5, commands.BucketType.user)
    @check_if_running()
    @has_warn_permission()
    async def clear_warns(self, ctx, user: Greedy[discord.Member]):
        guild_id = str(ctx.guild.id)
        for member in list(set(user)):
            user_id = str(member.id)
            if user_id not in self.config[guild_id]["warns"]:
                await ctx.send(f"{member} has no warns")
                continue
            if ctx.author.id != ctx.guild.owner.id:
                if member.top_role.position >= ctx.author.top_role.position:
                    return await ctx.send(f"{member} is above your paygrade, take a seat")
            del self.config[guild_id]["warns"][user_id]
            await ctx.send(f"Cleared {member}'s warns")
            await self.save_data()

    @commands.hybrid_command(name="warns", description="Shows all a users warns")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def _warns(self, ctx, *, user=None):
        if not user:
            user = ctx.author
        else:
            user = await self.bot.utils.get_user(ctx, user)
        if not user:
            return await ctx.send("User not found")
        guild_id = str(ctx.guild.id)
        user_id = str(user.id)
        if user_id not in self.config[guild_id]["warns"]:
            self.config[guild_id]["warns"][user_id] = []
        expire_warns = self.config[guild_id].get("warns_config", {}).get(
            "expire", False
        ) in (True, 1, "true", "True")
        active_warns = []
        reasons = ""
        expired = False
        for reason, raw_time in list(self.config[guild_id]["warns"][user_id]):
            try:
                warned_at = datetime.fromisoformat(raw_time)
                comparison_now = datetime.now(tz=warned_at.tzinfo)
                is_expired = (comparison_now - warned_at).days > 30
            except (TypeError, ValueError):
                is_expired = False
            if expire_warns and is_expired:
                expired = True
                continue
            active_warns.append([reason, raw_time])
            reasons += f"\n• `{reason}`"
        if expired:
            self.config[guild_id]["warns"][user_id] = active_warns
            await self.save_data()
        e = discord.Embed(color=colors.fate)
        url = self.bot.user.display_avatar.url
        if user.display_avatar.url:
            url = user.display_avatar.url
        e.set_author(name=f"{user.name}'s Warns", icon_url=url)
        e.description = f"**Total Warns:** [`{len(active_warns)}`]" + reasons
        await ctx.send(embed=e)

    @commands.command(
        name="simplify-perms",
        aliases=["simplify-permissions"],
        description="Simplifies server channel and role permissions",
    )
    @commands.is_owner()
    async def fix_perms(self, ctx: commands.Context, access_role: discord.Role = None):
        special = [
            "administrator",
            "manage_guild",
            "manage_roles",
            "manage_messages",
            "manage_webhooks",
            "manage_reactions",
            "manage_threads",
            "manage_permissions"
        ]
        if not access_role:
            access_role = ctx.guild.default_role

        perms = discord.PermissionOverwrite()
        perms.update(read_messages=True)
        access_granted = {access_role: perms}

        perms = discord.PermissionOverwrite()
        perms.update(read_messages=True, send_messages=False)
        limited_access = {access_role: perms}

        for category in ctx.guild.categories:
            perms = category.permissions_for(access_role)
            if perms.read_messages and perms.send_messages:
                await category.edit(overwrites=access_granted)

            for channel in category.text_channels:
                perms = channel.permissions_for(access_role)
                if perms.read_messages and perms.send_messages:
                    await channel.edit(sync_permissions=True)
                else:
                    await channel.edit(overwrites=limited_access)

        for role in ctx.guild.roles:
            perms = discord.Permissions()
            if role.id == access_role.id:
                old = access_role.permissions
                perms.update(
                    embed_links=old.embed_links,
                    create_instant_invite=perms.create_instant_invite,
                    attach_files=old.attach_files,
                    change_nickname=perms.change_nickname,
                    send_messages=True,
                    add_reactions=True
                )
                await role.edit(permissions=perms)
            if not any(getattr(role.permissions, perm) for perm in special):
                await role.edit(permissions=perms)

        await ctx.send("Finished simplifying server permissions")


class MuteView(ui.View):
    def __init__(self, ctx, user: Union[User, Member], case: int, reason: Optional[str], timer: Optional[int]):
        self.ctx = ctx
        self.user = user
        self.case = case
        self.reason = reason
        self.timer = timer
        super().__init__(timeout=45)

        if timer and reason:
            self.stop()
            return

        if not reason:
            self.reason_button = ui.Button(label="Set Reason", style=ButtonStyle.blurple)
            self.reason_button.callback = self.set_reason
            self.add_item(self.reason_button)

        if not timer:
            self.timer_button = ui.Button(label="Set Timer", style=ButtonStyle.blurple)
            self.timer_button.callback = self.set_timer
            self.add_item(self.timer_button)

    async def on_error(self, interaction, error, item):
        if not isinstance(error, NotFound):
            raise error

    async def interaction_check(self, interaction):
        """ Ensure the interaction is from the user who initiated the view """
        if interaction.user.id != self.ctx.author.id:
            with suppress(Exception):
                await interaction.response.send_message(
                    "Only the user who initiated this command can interact", ephemeral=True
                )
            return False
        return True

    async def set_timer(self, interaction: Interaction):
        view = TimerView(self.ctx, self.case, self)
        self.remove_item(self.timer_button)
        await interaction.response.edit_message(view=view)

    async def set_reason(self, interaction: Interaction):
        view = ReasonView(self.ctx, self.case, self)
        self.remove_item(self.reason_button)
        await interaction.response.edit_message(view=view)


class TimerView(ui.View):
    class SelectOptions(ui.Select):
        timers: List[Tuple[Union[str, int]]] = [
            ("Cancel", 0),
            ("5 Minutes", 60 * 5),
            ("15 Minutes", 60 * 15),
            ("30 Minutes", 60 * 30),
            ("45 Minutes", 60 * 45),
            ("1 Hour", 60 * 60),
            ("2 Hours", 60 * 60 * 2),
            ("6 Hours", 60 * 60 * 6),
            ("12 Hours", 60 * 60 * 12),
            ("1 Day", 60 * 60 * 24),
            ("2 Days", 60 * 60 * 24 * 2),
            ("1 Week", 60 * 60 * 24 * 7)
        ]

        def __init__(self, ctx: commands.Context, case: int, home_view: MuteView):
            self.ctx = ctx
            self.bot = ctx.bot
            self.case = case
            self.home_view = home_view

            options = [
                SelectOption(label=label, emoji="⏰", value=str(timer))
                for label, timer in self.timers
            ]
            super().__init__(
                placeholder="Select from presets",
                options=options,
                min_values=1,
                max_values=1
            )

        async def callback(self, interaction: Interaction):
            if not await self.home_view.interaction_check(interaction):
                return
            duration = int(interaction.data["values"][0])
            if duration == 0:
                return await interaction.response.edit_message(view=self.home_view)

            mod: Moderation = self.bot.cogs["Moderation"]  # type: ignore
            guild_id = str(self.ctx.guild.id)

            user = self.home_view.user
            mute_role = await self.bot.attrs.get_mute_role(self.ctx.guild, upsert=True)
            await user.add_roles(mute_role)

            # Update the mute if one is already running
            if mute_role in user.roles:
                user_id = str(user.id)
                if guild_id in mod.tasks and user_id in mod.tasks[guild_id]:
                    mod.tasks[guild_id][user_id].cancel()
                    del mod.tasks[guild_id][user_id]

            timer_info = {
                "channel": self.ctx.channel.id,
                "user": user.id,
                "end_time": now() + duration,
                "mute_role": mute_role.id,
                "removed_roles": [],
            }
            mod.config[guild_id]["mute_timers"][str(user.id)] = timer_info
            await mod.save_data()
            task = self.bot.loop.create_task(
                mod.handle_mute_timer(guild_id, str(user.id), timer_info)
            )
            if guild_id not in mod.tasks:
                mod.tasks[guild_id] = {}
            mod.tasks[guild_id][str(user.id)] = task

            expanded_form = format_date(datetime.now() - timedelta(seconds=duration))
            await interaction.response.send_message(
                f"Updated the mute for {user.mention} to {expanded_form}",
                allowed_mentions=discord.AllowedMentions.all()
            )
            await interaction.message.edit(view=self.home_view)

    def __init__(self, ctx: commands.Context, case: int, home_view: MuteView):
        super().__init__(timeout=45)
        self.add_item(self.SelectOptions(ctx, case, home_view))

    async def on_error(self, interaction, error, item):
        if not isinstance(error, NotFound):
            raise error


class ReasonView(ui.View):
    class SelectOptions(ui.Select):
        preset: List[Tuple[Union[str, str]]] = [
            ("❎", "Cancel"),
            ("🔊", "Spamming"),
            ("⚔", "Raiding"),
            ("👊", "Harassment"),
            ("👮‍♂️", "Discord TOS")
        ]

        def __init__(self, ctx: commands.Context, case: int, home_view: MuteView):
            self.ctx = ctx
            self.case = case
            self.home_view = home_view

            options = [
                SelectOption(label=reason, emoji=emoji, value=reason)
                for emoji, reason in self.preset
            ]
            super().__init__(
                placeholder="Select from presets",
                options=options,
                min_values=1,
                max_values=1
            )

        async def callback(self, interaction: Interaction):
            if not await self.home_view.interaction_check(interaction):
                return
            reason = interaction.data["values"][0]
            if reason == "Cancel":
                return await interaction.response.edit_message(view=self.home_view)

            async with self.ctx.bot.utils.cursor() as cur:
                await cur.execute(
                    "update cases set reason = %s "
                    "where guild_id = %s and case_number = %s;",
                    (self.ctx.bot.encode(reason), self.ctx.guild.id, self.case),
                )

            await interaction.response.send_message(f"Updated the reason to {reason}", ephemeral=True)
            await interaction.message.edit(view=self.home_view)

    def __init__(self, ctx: commands.Context, case: int, home_view: MuteView):
        super().__init__(timeout=45)
        self.add_item(self.SelectOptions(ctx, case, home_view))

    async def on_error(self, interaction, error, item):
        if not isinstance(error, NotFound):
            raise error


async def setup(bot):
    cls = Moderation(bot)
    globals()["cls"] = cls
    await bot.add_cog(cls)
