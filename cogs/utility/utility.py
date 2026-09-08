"""
cogs.utility.utility
~~~~~~~~~~~~~~~~~~~~~

A cog for general utility commands

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import json
import os
import platform
import random
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from time import time
from typing import *

import discord
from discord import NotFound, HTTPException
from discord import (
    User, Role, TextChannel, VoiceChannel, StageChannel, ForumChannel,
    CategoryChannel, Member, ui, SelectOption, Message, Embed, Interaction, Guild,
)
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Context

from botutils import colors, bytes2human, get_time, emojis, extract_time, \
    get_prefixes_async, format_date, sanitize, GetChoice, AuthorView, Cooldown, Menu, \
    findall


def resolve_audit_action(name: str):
    """Resolve an audit-log action without evaluating user-provided Python."""
    if not name or name.startswith("_"):
        return None
    action = getattr(discord.AuditLogAction, name, None)
    return action if isinstance(action, discord.AuditLogAction) else None


def resolve_invite_code(value: str):
    """Return a Discord invite code, ignoring malformed URL-like text."""
    try:
        resolved = discord.utils.resolve_invite(value)
    except (TypeError, ValueError):
        return None
    code = getattr(resolved, "code", resolved)
    if not isinstance(code, str) or not code.isascii() or not code.isalnum():
        return None
    return code


def webhook_name_error(name: str) -> Optional[str]:
    """Return a user-facing error for names Discord will reject."""
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        return "Webhook names must be between 1 and 80 characters."
    normalized = name.casefold()
    for reserved in ("clyde", "discord"):
        if reserved in normalized:
            return f'Webhook names cannot contain "{reserved}".'
    return None


def count_python_source_lines() -> int:
    """Count Fate's Python source lines without running filesystem I/O on the loop."""
    files = [Path("fate.py")]
    for directory in (Path("botutils"), Path("cogs")):
        files.extend(directory.rglob("*.py"))
    total = 0
    for source in files:
        with source.open("rb") as file:
            total += sum(1 for _line in file)
    return total


class Utility(commands.Cog):
    info_app = app_commands.Group(
        name="info",
        description="Explore detailed information about Discord objects",
        guild_only=True,
    )

    def __init__(self, bot):
        self.bot = bot
        self.settings = bot.utils.cache("settings", auto_sync=True)
        self.find = {}
        self.afk = {}
        self.timer_path = "./data/userdata/timers.json"
        self.timers = {}
        if os.path.isfile(self.timer_path):
            with open(self.timer_path, "r") as f:
                self.timers = json.load(f)
        if "timers" not in bot.tasks:
            bot.tasks["timers"] = {}
        self.database_cleanup_task.start()

    async def cog_load(self):
        if self.bot.is_ready():
            await self.resume_timers()

    async def cog_unload(self):
        self.database_cleanup_task.cancel()
        timer_tasks = list(self.bot.tasks.get("timers", {}).values())
        for task in timer_tasks:
            if not task.done():
                task.cancel()
        if timer_tasks:
            await asyncio.gather(*timer_tasks, return_exceptions=True)
        self.bot.tasks.get("timers", {}).clear()

    @tasks.loop(hours=6)
    async def database_cleanup_task(self):
        if not self.bot.is_ready():
            await self.bot.wait_until_ready()
        for _iteration in range(6):
            if self.bot.pool:
                break
            await asyncio.sleep(10)
        else:
            self.bot.log.critical("Can't connect to the DB to cleanup invites")
            return
        lmt = time() + 60 * 60 * 24 * 30
        set_to_remove = 0
        async with self.bot.utils.cursor() as cur:
            # Invites
            await cur.execute("select * from invites where created_at > %s;", (lmt,))
            set_to_remove += cur.rowcount
            await cur.execute("select * from invites where deleted_at > %s;", (lmt,))
            set_to_remove += cur.rowcount
            if set_to_remove:
                self.bot.log.info(f"Removing {set_to_remove} old invites")
            await cur.execute("delete from invites where created_at > %s;", (lmt,))
            await cur.execute("delete from invites where deleted_at > %s;", (lmt,))

            # Usernames
            lmt = 60 * 60 * 24 * 30
            await cur.execute("delete from usernames where changed_at > %s;", (lmt,))

            # Activity
            lmt = 60 * 60 * 24 * 365
            await cur.execute(
                "delete from activity where last_online > %s and last_message > %s;",
                (lmt, lmt),
            )

    async def save_timers(self):
        await self.bot.utils.save_json(self.timer_path, self.timers)


    @staticmethod
    async def wait_for_dismissal(ctx, msg):
        def pred(m):
            return m.channel.id == ctx.channel.id and m.content.lower().startswith("k")

        try:
            reply = await ctx.bot.wait_for("message", check=pred, timeout=25)
        except asyncio.TimeoutError:
            pass
        else:
            await asyncio.sleep(0.21)
            await ctx.message.delete()
            await asyncio.sleep(0.21)
            await msg.delete()
            await asyncio.sleep(0.21)
            await reply.delete()

    @commands.command(name="delete-data", description="Deletes your saved info data", enabled=False)
    async def delete_data(self, ctx):
        # user_id = str(ctx.author.id)
        # if user_id not in self.user_logs:
        #     return await ctx.send("You have no user data saved")
        # del self.user_logs[user_id]
        await ctx.send("Removed your data from .info")

    @commands.command(name="info", description="Shows info on users, roles, channels, etc")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.guild_only()
    async def info(
        self,
        ctx,
        *,
        target: Union[
            Member, User, Role, TextChannel, VoiceChannel,
            StageChannel, ForumChannel, CategoryChannel, str,
        ] = None,
    ):
        await self._show_info(ctx, target)

    async def _show_info(self, ctx, target=None):
        view = await InfoView(ctx, target)
        await view.wait()
        await self._clear_info_view(view.message)

    @staticmethod
    async def _clear_info_view(message):
        try:
            await message.edit(view=None)
        except HTTPException as error:
            # The information response is already complete. If its thread was
            # archived while the view was open, Discord forbids this cosmetic
            # component cleanup and reports code 50083.
            if error.code != 50083:
                raise

    async def _show_slash_info(self, interaction: Interaction, target=None):
        await interaction.response.defer()
        ctx = await Context.from_interaction(interaction)
        await self._show_info(ctx, target)

    @info_app.command(name="user", description="Show a user's profile and server details")
    @app_commands.describe(user="The user to inspect; defaults to you")
    async def info_app_user(
        self,
        interaction: Interaction,
        user: Optional[Member] = None,
    ):
        await self._show_slash_info(interaction, user or interaction.user)

    @info_app.command(name="channel", description="Show channel access and configuration")
    @app_commands.describe(channel="The channel to inspect; defaults to this channel")
    async def info_app_channel(
        self,
        interaction: Interaction,
        channel: Optional[
            Union[
                TextChannel,
                VoiceChannel,
                StageChannel,
                ForumChannel,
                CategoryChannel,
            ]
        ] = None,
    ):
        target = channel or interaction.channel
        if isinstance(target, discord.Thread):
            target = target.parent
        supported = (
            TextChannel,
            VoiceChannel,
            StageChannel,
            ForumChannel,
            CategoryChannel,
        )
        if not isinstance(target, supported):
            return await interaction.response.send_message(
                "I couldn't resolve a supported channel here.", ephemeral=True
            )
        await self._show_slash_info(interaction, target)

    @info_app.command(name="server", description="Show server statistics and configuration")
    async def info_app_server(self, interaction: Interaction):
        await self._show_slash_info(interaction, interaction.guild)

    @info_app.command(name="role", description="Show role membership and permissions")
    @app_commands.describe(role="The role to inspect")
    async def info_app_role(self, interaction: Interaction, role: Role):
        await self._show_slash_info(interaction, role)

    @info_app.command(name="invite", description="Show details about a Discord invite")
    @app_commands.describe(invite="A discord.gg invite URL or code")
    async def info_app_invite(self, interaction: Interaction, invite: str):
        await self._show_slash_info(interaction, invite)

    @info_app.command(name="bot", description="Show Fate statistics and runtime details")
    async def info_app_bot(self, interaction: Interaction):
        await self._show_slash_info(interaction)

    def collect_invite_info(self, inv):
        info = {
            "code": self.bot.encode(inv.id),
            "guild_id": None,
            "guild_name": None,
            "channel_id": None,
            "channel_name": None,
            "inviter": None,
            "uses": "0"
        }
        guild_types = (discord.Guild, discord.PartialInviteGuild)
        guild = inv.guild
        if not isinstance(inv.guild, guild_types) and hasattr(inv.guild, "id"):
            tmp = self.bot.get_guild(inv.guild.id)
            if isinstance(tmp, discord.Guild):
                guild = tmp

        if guild.id and guild.name:
            info["guild_id"] = guild.id
            info["guild_name"] = self.bot.encode(guild.name)

        if inv.inviter:
            info["inviter"] = inv.inviter.id

        if inv.channel:
            info["channel_id"] = inv.channel.id
            if hasattr(inv.channel, "name"):
                info["channel_name"] = self.bot.encode(inv.channel.name)

        if inv.uses:
            info["uses"] = inv.uses

        return info

    async def invite_to_sql(self, invite):
        info = self.collect_invite_info(invite)
        now = time()
        async with self.bot.utils.cursor() as cur:
            await cur.execute("select * from invites where code = %s limit 1;", (info["code"],))
            if cur.rowcount:
                await cur.execute(
                    "update invites set "
                    "guild_id = coalesce(%s, guild_id), "
                    "guild_name = coalesce(%s, guild_name), "
                    "channel_id = coalesce(%s, channel_id), "
                    "channel_name = coalesce(%s, channel_name), "
                    "inviter = coalesce(%s, inviter), "
                    "uses = case when uses is null then 0 "
                    "when %s is not null and %s > uses then %s else uses end, "
                    "created_at = coalesce(created_at, %s) "
                    "where code = %s limit 1;",
                    (
                        info["guild_id"],
                        info["guild_name"],
                        info["channel_id"],
                        info["channel_name"],
                        info["inviter"],
                        info["uses"],
                        info["uses"],
                        info["uses"],
                        now,
                        info["code"],
                    ),
                )
            else:
                await cur.execute(
                    "insert into invites values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        info["code"],
                        info["guild_id"],
                        info["guild_name"],
                        info["channel_id"],
                        info["channel_name"],
                        info["inviter"],
                        info["uses"],
                        now,
                        None,
                    ),
                )

    @commands.Cog.listener()
    async def on_message(self, msg):
        # AFK Command
        if msg.author.bot:
            return

        for user_id in list(set(msg.raw_mentions)):
            if user_id in self.afk and msg.author.id != user_id:
                if not msg.channel.permissions_for(msg.guild.me).send_messages:
                    break
                replies = ["shh", "shush", "shush child", "nO"]
                choice = random.choice(replies)
                await msg.channel.send(f"{choice} they're {self.afk[user_id]}")
                return

        # Keep track of their last message time
        if await self.bot.get_privacy(msg.author, "activity_info"):
            await self.bot.execute(
                "insert into activity values (%s, null, %s) "
                "on duplicate key update last_message = %s;",
                (msg.author.id, datetime.now(tz=timezone.utc), time()),
            )

        # Check for invites and log their current state
        if "discord.gg" in msg.content:
            await asyncio.sleep(1)
            invites = await findall("discord.gg/.{4,8}", msg.content)
            invites = invites if invites else []
            for invite in invites:
                code = resolve_invite_code(invite)
                if not code:
                    continue

                try:
                    invite = await self.bot.fetch_invite(code, with_counts=True)
                except NotFound:
                    return await self.bot.execute(
                        "update invites set deleted_at = %s "
                        "where code = %s and deleted_at is null;",
                        (time(), self.bot.encode(code)),
                    )
                except HTTPException:
                    return
                except KeyError as error:
                    # discord.py 2.7.1 assumes fetched invites always include
                    # channel data. Friend and other non-trackable invite
                    # payloads can omit it, so ignore only that known parser
                    # failure and keep processing any other links.
                    if error.args != ("channel",):
                        raise
                    continue

                guild = self.bot.get_guild(invite.guild.id)
                if guild and guild.me.guild_permissions.administrator:
                    invites = await guild.invites()
                    for _invite in invites:
                        await asyncio.sleep(0)
                        if invite.id == _invite.id:
                            invite = _invite
                            break
                await self.invite_to_sql(invite)

    @commands.Cog.listener()
    async def on_user_update(self, before, after):
        if before and after and str(before) != str(after):
            if await self.bot.get_privacy(after, "username_history"):
                async with self.bot.utils.cursor() as cur:
                    await cur.execute(
                        "select * from usernames where user_id = %s and username = %s;",
                        (after.id, self.bot.encode(str(before))),
                    )
                    if not cur.rowcount:
                        await cur.execute(
                            "insert into usernames values (%s, %s, %s);",
                            (after.id, self.bot.encode(str(before)), time()),
                        )

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.status != after.status and await self.bot.get_privacy(after, "activity_info"):
            status = discord.Status
            if before.status != status.offline and after.status == status.offline:
                await self.bot.execute(
                    "insert into activity values (%s, %s, null) "
                    "on duplicate key update last_online = %s;",
                    (before.id, datetime.now(tz=timezone.utc), time()),
                )

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        await self.invite_to_sql(invite)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        await self.bot.execute(
            "update invites set deleted_at = %s where code = %s;",
            (time(), self.bot.encode(invite.code)),
        )

    @commands.hybrid_command(name="userinfo", aliases=["uinfo"], description="Shows extra info on a user")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def user_info(self, ctx, *, user: Union[Member, User] = None):
        if not user:
            user = ctx.author
        await self._show_info(ctx, user)

    @commands.command(name="serverinfo", aliases=["sinfo"], description="Shows extra info on the server")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def server_info(self, ctx):
        await self._show_info(ctx, ctx.guild)

    @commands.command(name="servericon", aliases=["icon"], description="Shows the server icon")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def servericon(self, ctx):
        if not ctx.guild.icon or not str(ctx.guild.icon.url):
            return await ctx.send("This server has no icon")
        e = discord.Embed(color=0x80B0FF)
        e.set_image(url=ctx.guild.icon.url)
        e.description = "Server Icon"
        await ctx.send(embed=e)

    @commands.hybrid_command(name="members", aliases=["membercount"], description="Shows the member counts")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    @commands.max_concurrency(2, commands.BucketType.guild)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def members(self, ctx, *, role = None):
        if role:  # returns a list of members that have the role
            if ctx.message.role_mentions:
                role = ctx.message.role_mentions[0]
            else:
                role = await self.bot.utils.get_role(ctx, role)
                if not isinstance(role, discord.Role):
                    return

            if not role.members:
                return await ctx.send("That role has no members")

            members = []
            dat = [(m, m.top_role.position) for m in role.members]
            for member, _position in sorted(dat, key=lambda kv: kv[1], reverse=True):
                members.append(member)

            embeds = []
            e = None
            for i, member in enumerate(members):
                if not e:
                    e = discord.Embed(color=role.color)
                    e.set_author(name=role.name, icon_url=ctx.author.display_avatar.url)
                    if ctx.guild.icon:
                        e.set_thumbnail(url=ctx.guild.icon.url)
                    e.description = ""
                e.description += f"\n{emojis.creply} {member}"
                if i and i % 15 == 0:
                    embeds.append(e)
                    e = None
            if e:
                embeds.append(e)
            for e in embeds:
                lines = e.description.split("\n")
                last_line = e.description.count("\n")
                lines[last_line] = lines[last_line].replace(emojis.creply, emojis.reply)
                e.description = "\n".join(lines)

            if len(embeds) == 1:
                return await ctx.send(embed=embeds[0])

            for i, embed in enumerate(embeds):
                embed.set_footer(text=f"Page {i + 1}/{len(embeds)} 👥{len(role.members)}")

            await Menu(ctx, embeds)

        else:  # return the servers member count
            status_list = [
                discord.Status.online,
                discord.Status.idle,
                discord.Status.dnd,
            ]
            humans = len([m for m in ctx.guild.members if not m.bot])
            bots = len([m for m in ctx.guild.members if m.bot])
            online = len([m for m in ctx.guild.members if m.status in status_list])
            e = discord.Embed(color=colors.fate)
            e.set_author(name=f"Member Count", icon_url=ctx.guild.owner.display_avatar.url)
            if ctx.guild.icon:
                e.set_thumbnail(url=ctx.guild.icon.url)
            e.description = (
                f"**Total:** [`{ctx.guild.member_count or len(ctx.guild.members)}`]\n"
                f"**Online:** [`{online}`]\n"
                f"**Humans:** [`{humans}`]\n"
                f"**Bots:** [`{bots}`]"
            )
            await ctx.send(embed=e)

    @commands.command(name="permissions", aliases=["perms", "perm"], description="Checks what has a specific permission")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def permissions(self, ctx, permission=None, channel: discord.TextChannel = None):
        perms = [perm[0] for perm in [perm for perm in discord.Permissions()]]
        if not permission:
            return await ctx.send(f'Perms: {", ".join(perms)}')
        permission = permission.lower()
        if permission not in perms:
            return await ctx.send("Unknown perm")
        e = discord.Embed(color=colors.fate)
        e.set_author(name=f"Things with {permission}", icon_url=ctx.author.display_avatar.url)
        if ctx.guild.icon:
            e.set_thumbnail(url=ctx.guild.icon.url)
        members = ""
        for member in ctx.guild.members:
            p = member.guild_permissions
            if channel:
                p = channel.permissions_for(member)
            if getattr(p, permission, None):
                members += f"{member.mention}\n"
        if members:
            e.add_field(name="Members", value=members[:1000])
        roles = ""
        for role in ctx.guild.roles:
            if getattr(role.permissions, permission, False):
                roles += f"{role.mention}\n"
        if roles:
            e.add_field(name="Roles", value=roles[:1000])
        await ctx.send(embed=e)

    @commands.hybrid_command(name="avatar", aliases=["av", "pfp"], description="Shows a users avatar")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.cooldown(2, 45, commands.BucketType.user)
    @commands.bot_has_permissions(embed_links=True)
    async def avatar(self, ctx, *, user: Union[discord.Member, discord.User] = None):
        if not user:
            user = ctx.author
        name = await sanitize(user.display_name)
        display_av = discord.Embed(color=colors.fate)
        display_av.set_image(url=str(user.display_avatar.url))
        display_av.set_author(name=f"{name}'s avatar")
        if user.avatar and (user.avatar != user.display_avatar):
            profile_av = discord.Embed(color=colors.fate)
            profile_av.set_author(name=f"{name}'s Main Avatar", icon_url=user.display_avatar.url)
            profile_av.set_image(url=user.avatar.url)
            display_av.set_author(name=f"{name}'s Server Avatar", icon_url=user.avatar.url)
            return await Menu(ctx, [profile_av, display_av])
        await ctx.send(embed=display_av)

    @commands.command(name="banner", description="Shows a user or servers banner")
    @commands.cooldown(2, 45, commands.BucketType.user)
    @commands.bot_has_permissions(embed_links=True)
    async def banner(self, ctx, *, target: Union[discord.User, discord.Guild, discord.Invite] = None):
        pages = []
        guild = ctx.guild
        if isinstance(target, (discord.Guild, discord.Invite)):
            guild = target
            if isinstance(target, discord.Invite):
                guild = target.guild
            if not guild.splash and not guild.banner:
                return await ctx.send("That server doesn't have a banner")
        else:
            target_user = target or ctx.author
            target_user = await self.bot.fetch_user(target_user.id)
            if target_user.banner:
                e = discord.Embed(color=colors.fate)
                e.set_author(
                    name=f"{await sanitize(target_user.display_name)}'s Banner",
                    icon_url=target_user.display_avatar.url
                )
                e.set_image(url=str(target_user.banner.url))
                if target:
                    return await ctx.send(embed=e)
                pages.append(e)
            if target and not target_user.banner:
                return await ctx.send("That user doesn't have a banner")
        if guild and (guild.splash or guild.banner):
            if guild.banner:
                e = discord.Embed(color=colors.fate)
                e.set_author(name=f"Server Banner", icon_url=guild.icon.url if guild.icon else None)
                e.set_image(url=guild.banner.url)
                pages.append(e)
            if guild.splash:
                e = discord.Embed(color=colors.fate)
                e.set_author(name=f"Server Invite Splash", icon_url=guild.icon.url if guild.icon else None)
                e.set_image(url=guild.splash.url)
                pages.append(e)
        if len(pages) == 1:
            return await ctx.send(embed=pages[0])
        await Menu(ctx, pages)

    @commands.command(name="owner", description="Shows who owns the server")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def owner(self, ctx):
        e = discord.Embed(color=colors.fate)
        e.description = f"**Server Owner:** {ctx.guild.owner.mention}"
        await ctx.send(embed=e)

    @commands.command(name="color", aliases=["setcolor", "changecolor"], description="Shows or generates a random color")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    async def color(self, ctx, *args):
        if len(args) == 0:
            color = colors.random()
            e = discord.Embed(color=color)
            e.set_author(name=f"#{color}", icon_url=ctx.author.display_avatar.url)
            return await ctx.send(embed=e)
        if len(args) == 1:
            _hex = args[0].strip("#")
            try:
                color = int("0x" + _hex, 0)
            except ValueError:
                return await ctx.send("That's not a real hex")
            if color > 16777215:
                return await ctx.send("That hex value is too large")
            e = discord.Embed(color=color)
            e.description = f"#{_hex}"
            return await ctx.send(embed=e)
        if not ctx.author.guild_permissions.manage_roles:
            return await ctx.send("You need manage roles permissions to use this")
        if "<@" in args[0]:
            target = "".join(x for x in args[0] if x.isdigit())
            if not target:
                return await ctx.send("Wtf man.. wHo")
            role = ctx.guild.get_role(int(target))
        else:
            role = await self.bot.utils.get_role(ctx, args[0])
        if not role:
            return await ctx.send("Unknown role")
        try:
            color = int("0x" + args[1].strip("#").strip("0x"), 0)
            if color > 16777215:
                return await ctx.send("That hex value is too large")
            _hex = discord.Color(color)
        except:
            return await ctx.send("Invalid Hex")
        if role.position >= ctx.author.top_role.position:
            return await ctx.send("That roles above your paygrade, take a seat")
        previous_color = role.color
        await role.edit(color=_hex)
        await ctx.send(f"Changed {role.name}'s color from {previous_color} to {_hex}")

    def start_timer_task(self, user_id, message, data):
        task_id = f"timer-{data['timer']}"
        task = self.bot.loop.create_task(self.remind(user_id, message, data))
        self.bot.tasks["timers"][task_id] = task

        def cleanup(finished):
            if self.bot.tasks.get("timers", {}).get(task_id) is finished:
                self.bot.tasks["timers"].pop(task_id, None)
            if not finished.cancelled() and (error := finished.exception()) is not None:
                self.bot.loop.call_exception_handler({
                    "message": f"Reminder task {task_id} failed",
                    "exception": error,
                    "task": finished,
                })

        task.add_done_callback(cleanup)
        return task

    @staticmethod
    def timer_datetime(data) -> datetime:
        timestamp = data.get("unix_timestamp")
        if timestamp is not None:
            return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        # Legacy reminders stored local wall-clock time without a timezone.
        # Keep them naive so dates beyond the Windows Unix timestamp range
        # remain usable and retain their original local-time meaning.
        return datetime.fromisoformat(data["timer"])

    @staticmethod
    async def sleep_until_timer(end_time: datetime) -> None:
        # Recalculate daily so clock and daylight-saving changes are respected.
        # Bounded sleeps also avoid platform timer limits on centuries-long
        # legacy reminders.
        while True:
            remaining = (
                end_time - datetime.now(tz=end_time.tzinfo)
            ).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 60 * 60 * 24))

    async def remind(self, user_id, msg, dat):
        end_time = self.timer_datetime(dat)
        await self.sleep_until_timer(end_time)
        channel = self.bot.get_channel(dat["channel"])
        if channel is None:
            with suppress(discord.HTTPException):
                channel = await self.bot.fetch_channel(dat["channel"])
        user = self.bot.get_user(int(user_id))
        if user is None:
            with suppress(discord.HTTPException):
                user = await self.bot.fetch_user(int(user_id))

        reminder_embed = discord.Embed(
            title=(
                f"Set <t:{dat['set_timestamp']}:R>"
                if dat.get("set_timestamp")
                else "Reminder"
            ),
            color=discord.Color.blue(),
        )
        if user is not None:
            reminder_embed.set_author(
                name=user.name, icon_url=user.display_avatar.url
            )
        reminder_embed.add_field(name="Message", value=msg, inline=False)
        if dat.get("message_link"):
            reminder_embed.add_field(
                name="Link", value=dat["message_link"], inline=False
            )
        reminder_message = f"{dat['mention']}, here's your reminder!"
        delivered = False
        for destination in (channel, user):
            if destination is None:
                continue
            try:
                await destination.send(
                    content=reminder_message,
                    embed=reminder_embed,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False, users=True
                    ),
                )
                delivered = True
                break
            except discord.HTTPException:
                continue
        if not delivered:
            self.bot.loop.call_exception_handler(
                {"message": f"Could not deliver reminder for user {user_id}"}
            )
            return

        user_timers = self.timers.get(user_id, {})
        user_timers.pop(msg, None)
        if not user_timers:
            self.timers.pop(user_id, None)
        await self.save_timers()

    @commands.command(name="reminder", aliases=["timer", "remindme", "remind"], description="Creates a reminder")
    @commands.cooldown(2, 15, commands.BucketType.user)
    async def timer(self, ctx, timer, *, reminder):
        prefix = await get_prefixes_async(self.bot, ctx.message)
        prefix = prefix[2]

        usage = (
            f"Usage: `{prefix}reminder [30s|5m|1h|2d] your reminder here`\n"
            f"Example: `{prefix}reminder 1h take out the trash`"
        )
        timer = extract_time(timer)

        error_messages = {
            'timer': usage,
            'too_short': "That timer is too short. Please set a timer longer than 30 seconds.",
            'too_long': "You can't set a timer longer than a year.",
            'links': "You can't include links in reminders.",
            'short_reminder': "That's a very short reminder message! Please provide more detail.",
            'reminder_exists': "You already have a reminder set for that.",
            'too_future': "That's a bit too far into the future."
        }

        if not timer:
            return await ctx.send(error_messages['timer'])

        if timer < 30:
            return await ctx.send(error_messages['too_short'])

        if timer > 60 * 60 * 24 * 365:
            return await ctx.send(error_messages['too_long'])

        if "http" in reminder.casefold():
            return await ctx.send(error_messages['links'])

        if len(reminder) <= 1:
            return await ctx.send(error_messages['short_reminder'])

        reminder = reminder[:200]

        user_id = str(ctx.author.id)
        self.timers[user_id] = self.timers.get(user_id, {})
        if reminder in self.timers[user_id]:
            return await ctx.send(error_messages['reminder_exists'])

        try:
            now = datetime.now(tz=timezone.utc)
            reminder_time = now + timedelta(seconds=timer)
            message_link = ctx.message.jump_url
            self.timers[user_id][reminder] = {
                "timer": reminder_time.isoformat(),
                "channel": ctx.channel.id,
                "mention": ctx.author.mention,
                "unix_timestamp": int(reminder_time.timestamp()),
                "set_timestamp": int(now.timestamp()),
                "message_link": message_link
            }
        except OverflowError:
            return await ctx.send(error_messages['too_future'])

        await ctx.send(f"Reminder set for <t:{self.timers[user_id][reminder]['unix_timestamp']}:F> \n\"{reminder}\"")

        self.start_timer_task(user_id, reminder, self.timers[user_id][reminder])
        await self.save_timers()

    @commands.command(name="timers", aliases=["reminders"], description="Shows your timers")
    @commands.cooldown(2, 5, commands.BucketType.user)
    async def timers(self, ctx):
        user_id = str(ctx.author.id)
        if user_id not in self.timers:
            return await ctx.send("You currently have no timers")
        if not self.timers[user_id]:
            return await ctx.send("You currently have no timers")
        e = discord.Embed(color=colors.fate)
        for msg, dat in list(self.timers[user_id].items()):
            end_time = self.timer_datetime(dat)
            if datetime.now(tz=end_time.tzinfo) > end_time:
                del self.timers[user_id][msg]
                await self.save_timers()
                continue
            expanded_time = format_date(end_time)
            channel = self.bot.get_channel(dat["channel"])
            if not channel:
                with suppress(discord.HTTPException):
                    channel = await self.bot.fetch_channel(dat["channel"])
            location = getattr(channel, "mention", "Direct message")
            e.add_field(
                name=f"Ending in {expanded_time}",
                value=f"{location} - `{msg}`",
                inline=False,
            )
        if not e.fields:
            return await ctx.send("You currently have no timers")
        await ctx.send(embed=e)

    @commands.command(name="delete-timer", aliases=["remove-timer", "clear-timers"], description="Removes a timer")
    async def delete_timers(self, ctx):
        user_id = str(ctx.author.id)
        if user_id not in self.timers or not self.timers[user_id]:
            return await ctx.send("You have no timers")
        if len(self.timers[user_id]) == 1:
            for key, dat in list(self.timers[user_id].items()):
                with suppress(KeyError):
                    self.bot.tasks["timers"][f"timer-{dat['timer']}"].cancel()
                    del self.bot.tasks["timers"][f"timer-{dat['timer']}"]
                del self.timers[user_id]
            await self.save_timers()
            return await ctx.send("Removed your only timer")
        choice = await GetChoice(ctx, self.timers[user_id].keys())
        if choice is None:
            return await ctx.send("Timer deletion cancelled")
        dat = self.timers[user_id][choice]
        with suppress(KeyError):
            self.bot.tasks["timers"][f"timer-{dat['timer']}"].cancel()
            del self.bot.tasks["timers"][f"timer-{dat['timer']}"]
        del self.timers[user_id][choice]
        if not self.timers[user_id]:
            del self.timers[user_id]
        await self.save_timers()
        await ctx.send(f"Deleted 👍")

    @commands.Cog.listener("on_ready")
    async def resume_timers(self):
        if "timers" not in self.bot.tasks:
            self.bot.tasks["timers"] = {}
        for user_id, timers in self.timers.items():
            for timer, dat in timers.items():
                task_id = f"timer-{dat['timer']}"
                current = self.bot.tasks["timers"].get(task_id)
                if current is not None and not current.done():
                    continue
                self.bot.tasks["timers"].pop(task_id, None)
                self.start_timer_task(user_id, timer, dat)

    @commands.command(name="findmsg", description="Finds a message in the channel history")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def _findmsg(self, ctx, *, content=None):
        if content is None:
            e = discord.Embed(color=colors.fate)
            e.set_author(name="Error ⚠", icon_url=ctx.author.display_avatar.url)
            if ctx.guild.icon:
                e.set_thumbnail(url=ctx.guild.icon.url)
            e.description = (
                "Content is a required argument\n"
                "Usage: `.find {content}`\n"
                "Limit: 16,000"
            )
            e.set_footer(text="Searches for a message")
            return await ctx.send(embed=e)
        async with ctx.typing():
            channel_id = str(ctx.channel.id)
            if channel_id in self.find:
                return await ctx.send("I'm already searching")
            self.find[channel_id] = True
            async for msg in ctx.channel.history(limit=1000):
                if ctx.message.id != msg.id:
                    if content.lower() in msg.content.lower():
                        e = discord.Embed(color=colors.fate)
                        e.set_author(
                            name="Message Found 🔍", icon_url=ctx.author.display_avatar.url
                        )
                        if ctx.guild.icon:
                            e.set_thumbnail(url=ctx.guild.icon.url)
                        e.description = (
                            f"**Author:** `{msg.author}`\n"
                            f"[Jump to MSG]({msg.jump_url})"
                        )
                        if msg.content != "":
                            e.add_field(name="Full Content:", value=msg.content)
                        if len(msg.attachments) > 0:
                            for attachment in msg.attachments:
                                e.set_image(url=attachment.url)
                        await ctx.send(embed=e)
                        del self.find[channel_id]
                        return await ctx.message.delete()
        await ctx.send("Nothing found")
        del self.find[channel_id]

    @commands.command(name="last-entry", description="Shows the last audit log entry")
    @commands.cooldown(2, 5, commands.BucketType.user)
    @commands.has_permissions(view_audit_log=True)
    @commands.bot_has_permissions(view_audit_log=True)
    async def last_entry(self, ctx, user: Optional[discord.User], action=None):
        """ Gets the last entry for a specific action """
        last_entry = None
        if not user and not action:
            return await ctx.send("You need to specify a user, or an audit log action")
        elif user:
            async for entry in ctx.guild.audit_logs(limit=250):
                if (
                    entry.target and entry.target.id == user.id
                ) or entry.user.id == user.id:
                    last_entry = entry
                    break
        else:
            action_name = action
            action = resolve_audit_action(action_name)
            if action is None:
                return await ctx.send(f"`{action_name}` isn't an audit log action")
            async for entry in ctx.guild.audit_logs(limit=1, action=action):
                last_entry = entry
        if not last_entry:
            return await ctx.send(f"I couldn't find anything")
        e = discord.Embed(color=colors.fate)
        e.description = self.bot.utils.format_dict(
            {
                "Action": last_entry.action.name,
                "User": last_entry.user,
                "Target": last_entry.target,
                "Reason": last_entry.reason if last_entry.reason else "None Specified",
                "When": last_entry.created_at,
            }
        )
        await ctx.send(embed=e)

    @commands.command(name="id", description="Shows a users ID")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def id(self, ctx, *, user=None):
        if user:
            user = await self.bot.utils.get_user(ctx, user)
            if not user:
                return await ctx.send("User not found")
            return await ctx.send(user.id)
        for user in ctx.message.mentions:
            return await ctx.send(user.id)
        for channel in ctx.message.channel_mentions:
            return await ctx.send(channel.id)
        e = discord.Embed(color=colors.fate)
        e.description = (
            f"{ctx.author.mention}: {ctx.author.id}\n"
            f"{ctx.channel.mention}: {ctx.channel.id}"
        )
        await ctx.send(embed=e)

    @commands.command(name="estimate-inactives", description="Estimates how many would get kicked from pruning")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(kick_members=True)
    async def estimate_inactives(self, ctx, days: int):
        if days > 30:
            return await ctx.send("That's too big of a number to check with")
        elif days < 1:
            return await ctx.send("The number of days must at least be 1")
        inactive_count = await ctx.guild.estimate_pruned_members(days=days)
        e = discord.Embed(color=colors.fate)
        e.description = f"Inactive Users: {inactive_count}"
        await ctx.send(embed=e)

    @commands.command(name="create-webhook", aliases=["createwebhook"], description="Creates and sends a webhook url")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.has_permissions(manage_webhooks=True)
    @commands.bot_has_permissions(
        manage_webhooks=True, embed_links=True, manage_messages=True
    )
    async def create_webhook(self, ctx, *, name=None):
        if not name:
            return await ctx.send(
                'Usage: "`.create-webhook name`"\nYou can attach a file for its avatar'
            )
        if error := webhook_name_error(name):
            return await ctx.send(error)
        avatar = None
        if ctx.message.attachments:
            avatar = await ctx.message.attachments[0].read()
        webhook = await ctx.channel.create_webhook(name=name, avatar=avatar)
        e = discord.Embed(color=colors.fate)
        e.set_author(name=f"Webhook: {webhook.name}", icon_url=webhook.url)
        if ctx.guild.icon:
            e.set_thumbnail(url=ctx.guild.icon.url)
        e.description = webhook.url
        try:
            await ctx.author.send(embed=e)
            await ctx.send("Sent the webhook url to dm 👍")
        except:
            await ctx.send("Failed to dm you the webhook url", embed=e)

    @commands.command(name="webhooks", description="Lists the servers webhooks")
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_webhooks=True)
    async def webhooks(self, ctx, channel: discord.TextChannel = None):
        """ Return all the servers webhooks """
        e = discord.Embed(color=colors.fate)
        e.set_author(name="Webhooks", icon_url=ctx.author.display_avatar.url)
        if ctx.guild.icon:
            e.set_thumbnail(url=ctx.guild.icon.url)
        if channel:
            if not channel.permissions_for(ctx.guild.me).manage_webhooks:
                return await ctx.send(
                    "I need manage webhook(s) permissions in that channel"
                )
            webhooks = await channel.webhooks()
            e.description = "\n".join([f"• {webhook.name}" for webhook in webhooks])
            await ctx.send(embed=e)
        else:
            for channel in ctx.guild.text_channels:
                if channel.permissions_for(ctx.guild.me).manage_webhooks:
                    webhooks = await channel.webhooks()
                    if webhooks:
                        e.add_field(
                            name=f"◈ {channel}",
                            value="\n".join(
                                [f"• {webhook.name}" for webhook in webhooks]
                            ),
                            inline=False,
                        )
            await ctx.send(embed=e)

    @commands.command(name="move", aliases=["mv"], description="Moves messages to a different channel")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_webhooks=True)
    async def move(self, ctx, amount: int, channel: discord.TextChannel):
        """ Moves a conversation to another channel """

        if ctx.channel.id == channel.id:
            return await ctx.send("Hey! that's illegal >:(")
        if amount > 250:
            return await ctx.send("That's too many :[")
        cooldown = 1
        if amount > 50:
            await ctx.send("That's a lot.. ima do this a lil slow then")
            cooldown *= cooldown

        webhook = await channel.create_webhook(name="Chat Transfer")
        msgs = [msg async for msg in ctx.channel.history(limit=amount + 1)]

        e = discord.Embed()
        e.set_author(name=f"Progress: 0/{amount}", icon_url=ctx.author.display_avatar.url)
        e.set_footer(text=f"Moving to #{channel.name}")
        transfer_msg = await ctx.send(embed=e)

        em = discord.Embed()
        em.set_author(name=f"Progress: 0/{amount}", icon_url=ctx.author.display_avatar.url)
        em.set_footer(text=f"Moving from #{channel.name}")
        channel_msg = await channel.send(embed=em)

        await ctx.message.delete()

        index = 1
        for iteration, msg in enumerate(msgs[::-1]):
            if ctx.message.id == msg.id:
                continue
            avatar = msg.author.display_avatar.url
            embed = None
            if msg.embeds:
                embed = msg.embeds[0]

            files = []
            file_paths = []
            for attachment in msg.attachments:
                fp = os.path.join("static", attachment.filename)
                await attachment.save(fp)
                files.append(discord.File(fp))
                file_paths.append(fp)

            if not msg.content and not files and not embed:
                continue
            await webhook.send(
                msg.content,
                username=msg.author.display_name,
                avatar_url=avatar,
                files=files,
                embed=embed,
            )
            for fp in file_paths:
                os.remove(fp)
            if index == 5:
                e.set_author(
                    name=f"Progress: {iteration+1}/{amount}",
                    icon_url=ctx.author.display_avatar.url,
                )
                em.set_author(
                    name=f"Progress: {iteration+1}/{amount}",
                    icon_url=ctx.author.display_avatar.url,
                )
                await transfer_msg.edit(embed=e)
                await channel_msg.edit(embed=em)
                index = 1
            else:
                index += 1
            await msg.delete()
            await asyncio.sleep(cooldown)

        await webhook.delete()
        result = f"Progress: {amount}/{amount}"
        e.set_author(name=result, icon_url=ctx.author.display_avatar.url)
        em.set_author(name=result, icon_url=ctx.author.display_avatar.url)
        await transfer_msg.edit(embed=e)
        await channel_msg.edit(embed=em)

    @commands.command(name="afk", description="Tells users you're afk when they ping you")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def afk(self, ctx, *, reason="afk"):
        if ctx.message.mentions or ctx.message.role_mentions:
            return await ctx.send("nO -_-\nMentions are forbidden in afk messages")
        if ctx.author.id in self.afk:
            return
        e = discord.Embed(color=colors.fate)
        if len(reason) > 64:
            return await ctx.send("Your afk message can't be greater than 64 characters")
        e.set_author(name="You are now afk", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=e, delete_after=5)
        reason = await sanitize(reason)
        self.afk[ctx.author.id] = reason
        await asyncio.sleep(5)
        await ctx.message.delete()

    @commands.Cog.listener("on_message")
    async def remove_afk(self, msg):
        if msg.author.id in self.afk:
            del self.afk[msg.author.id]
            if msg.channel.permissions_for(msg.guild.me).send_messages:
                await msg.channel.send("Removed your afk")

    @commands.command(name="export-bans", description="Sends a txt of the ban list")
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(ban_members=True, attach_files=True)
    async def export_bans(self, ctx):
        bans = [ban async for ban in ctx.guild.bans(limit=None)]
        members = "\n".join([
            f"{u.id}, {u}, {ban.reason}"
            for ban in bans if (u := ban.user)
        ])
        buffer = BytesIO()
        buffer.write(members.encode())
        buffer.seek(0)
        await ctx.send(file=discord.File(buffer, filename="bans.txt"))


# Method index
pages: Dict[Type, str] = {
    None: "bot_info",
    str: "invite_info",
    User: "user_info",
    Member: "user_info",
    TextChannel: "channel_info",
    VoiceChannel: "channel_info",
    StageChannel: "channel_info",
    ForumChannel: "channel_info",
    CategoryChannel: "channel_info",
    Role: "role_info",
    Guild: "server_info"
}

INFO_ASSET_ROOT = Path(__file__).resolve().parents[2] / "assets" / "info"
INFO_PAGE_BANNERS = {
    "server_info": "server-banner.png",
    "channel_info": "channel-banner.png",
    "user_info": "user-banner.png",
}


def _pretty_name(value: Any) -> str:
    return str(value).replace("_", " ").replace("-", " ").title()


def _compact_list(values: Iterable[str], limit: int = 8) -> str:
    items = [str(value) for value in values if value]
    if not items:
        return "None"
    visible = items[:limit]
    suffix = f"  +{len(items) - limit} more" if len(items) > limit else ""
    return " • ".join(visible) + suffix


class RefreshInfoButton(ui.Button):
    def __init__(self, parent: "InfoView") -> None:
        self.info_view = parent
        super().__init__(
            label="Refresh",
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            row=1,
        )

    async def callback(self, interaction: Interaction) -> None:
        await interaction.response.defer()
        await self.info_view.render_page(self.info_view.current_page, interaction)


class InfoView(AuthorView):
    """ An awaitable for an interactive info menu """
    message: Message = None

    def __init__(
        self,
        ctx: Context,
        target: Union[
            None, str, User, Member, TextChannel, VoiceChannel,
            StageChannel, ForumChannel, CategoryChannel, Role,
        ],
    ) -> None:
        self.cd = Cooldown(1, 3)

        self.ctx = ctx
        self.bot = ctx.bot
        self.target = target
        if isinstance(target, str) and "discord.gg" not in target:
            self.target = target = None

        self.current_page = pages[type(target) if target else target]

        super().__init__(timeout=300)
        self.add_item(SelectMenu(parent=self))
        self.add_item(RefreshInfoButton(parent=self))
        permissions = discord.Permissions(**self.bot.config["bot_invite_permissions"])
        invite_url = discord.utils.oauth_url(
            self.bot.user.id,
            permissions=permissions,
            scopes=("bot", "applications.commands"),
        )
        user_install_url = getattr(
            self.bot,
            "user_install_url",
            (
                "https://discord.com/oauth2/authorize"
                f"?client_id={self.bot.user.id}"
                "&integration_type=1&scope=applications.commands"
            ),
        )
        self.add_item(ui.Button(
            label="Invite Fate",
            emoji="➕",
            style=discord.ButtonStyle.link,
            url=invite_url,
            row=1,
        ))
        self.add_item(ui.Button(
            label="Add to My Apps",
            emoji="📲",
            style=discord.ButtonStyle.link,
            url=user_install_url,
            row=1,
        ))
        self.add_item(ui.Button(
            label="Support",
            emoji="💬",
            style=discord.ButtonStyle.link,
            url=self.bot.config["support_server"],
            row=1,
        ))
        if self.bot.vote_url:
            self.add_item(ui.Button(
                label="Vote",
                emoji="⬆️",
                style=discord.ButtonStyle.link,
                url=self.bot.vote_url,
                row=1,
            ))

    def __await__(self) -> Generator[None, None, "InfoView"]:
        """ Return the coro function for initializing the message """
        return self._await().__await__()

    async def _await(self) -> "InfoView":
        """ Initialize the info message """
        async for embed in getattr(self, self.current_page)():
            attachments = self.attachments_for(embed)
            if self.message:
                await self.message.edit(embed=embed, attachments=attachments)
            else:
                kwargs = {"embed": embed, "view": self}
                if attachments:
                    kwargs["files"] = attachments
                self.message = await self.ctx.send(**kwargs)
        return self

    def set_page_banner(
        self,
        embed: Embed,
        page: str,
        native_url: Optional[str] = None,
    ) -> None:
        """Prefer informative Discord artwork, with generated art as fallback."""
        if native_url:
            embed.set_image(url=native_url)
            return
        guild = getattr(self.ctx, "guild", None)
        channel = getattr(self.ctx, "channel", None)
        if guild and channel:
            bot_member = getattr(guild, "me", None)
            if bot_member and not channel.permissions_for(bot_member).attach_files:
                return
        filename = INFO_PAGE_BANNERS.get(page)
        if filename and (INFO_ASSET_ROOT / filename).is_file():
            embed.set_image(url=f"attachment://{filename}")

    @staticmethod
    def attachments_for(embed: Embed) -> List[discord.File]:
        image_url = getattr(embed.image, "url", None)
        if not image_url or not image_url.startswith("attachment://"):
            return []
        filename = image_url.removeprefix("attachment://")
        path = INFO_ASSET_ROOT / filename
        return [discord.File(path, filename=filename)] if path.is_file() else []

    async def render_page(
        self,
        page: str,
        interaction: Optional[Interaction] = None,
    ) -> None:
        self.current_page = page
        for item in self.children:
            if isinstance(item, SelectMenu):
                for option in item.options:
                    option.default = option.value == page
        async for embed in getattr(self, page)():
            attachments = self.attachments_for(embed)
            if interaction:
                await interaction.edit_original_response(
                    embed=embed,
                    view=self,
                    attachments=attachments,
                )
            elif self.message:
                await self.message.edit(
                    embed=embed,
                    view=self,
                    attachments=attachments,
                )

    async def on_error(
        self, interaction: Interaction, error: Exception, item: ui.Item
    ) -> None:
        """ Ignore errors related to altered permissions or deletions mid-process """
        if not isinstance(error, NotFound):
            raise error

    async def bot_info(self) -> Generator[Embed, None, Embed]:
        e = Embed(
            color=colors.fate,
            description="### Collecting Fate's latest statistics…",
        )
        e.set_thumbnail(url=self.bot.user.display_avatar.url)
        yield e
        now = datetime.now(tz=timezone.utc)
        e = Embed(
            title="Fate",
            description="A multipurpose Discord bot for moderation, utility, games, and communities.",
            color=colors.fate,
            timestamp=now,
        )
        e.set_author(
            name="Bot Overview",
            icon_url=self.bot.user.display_avatar.url,
        )

        cutoff = int((
            datetime.now(tz=timezone.utc) - timedelta(days=30)
        ).timestamp())
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select coalesce(sum(total), 0) from commands "
                "where ran_at >= %s;",
                (cutoff,)
            )
            commands_used = (await cur.fetchone())[0]

        lines = await asyncio.to_thread(count_python_source_lines)

        guilds = len(list(self.bot.guilds))
        users = 0
        for guild in list(self.bot.guilds):
            await asyncio.sleep(0)
            if guild and guild.member_count:
                users += guild.member_count
        uptime_percentage = self.bot.uptime_tracker.percentage(now)

        e.set_thumbnail(url=self.bot.user.display_avatar.url)
        e.add_field(
            name="📊 Reach",
            value=f"**Servers**  {guilds:,}"
                  f"\n**Members**  {users:,}"
                  f"\n**Commands**  {len(self.bot.commands):,}"
                  f"\n**Modules**  {len(self.bot.extensions):,}",
            inline=True,
        )
        e.add_field(
            name="⚡ Activity",
            value=f"**Commands (30d)**  {commands_used:,}"
                  f"\n**Gateway**  {round(self.bot.latency * 1000):,} ms"
                  f"\n**Uptime (30d)**  {uptime_percentage:.2f}%"
                  f"\n**Shards**  {self.bot.shard_count or 1:,}"
                  f"\n**Source**  {lines:,} lines",
            inline=True,
        )
        e.add_field(
            name="🕒 Online Since",
            value=(
                f"{discord.utils.format_dt(self.bot.start_time, style='F')}\n"
                f"{discord.utils.format_dt(self.bot.start_time, style='R')}"
            ),
            inline=False,
        )
        e.add_field(
            name="👥 Credits",
            value="• **Villagers654** ~ Owner, Developer"
                  "\n• **Luck** ~ Creator",
            inline=False,
        )
        e.set_footer(
            text=f"Python {platform.python_version()} • discord.py {discord.__version__}",
        )
        yield e


    async def user_info(self) -> Generator[None, None, Embed]:
        user: Union[Member, User] = (
            self.target
            if isinstance(self.target, (Member, User))
            else self.ctx.author
        )
        profile: User = user
        with suppress(HTTPException):
            profile = await self.bot.fetch_user(user.id)

        accent = getattr(profile, "accent_color", None)
        if not accent and isinstance(user, Member) and user.color.value:
            accent = user.color
        embed = Embed(
            title=f"👤 {user.display_name}",
            description=f"{user.mention}  •  `{user.id}`",
            color=accent or colors.fate,
            timestamp=datetime.now(tz=timezone.utc),
        )
        embed.set_author(
            name=f"User dossier • {user}",
            icon_url=user.display_avatar.url,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        banner = getattr(profile, "banner", None)
        self.set_page_banner(
            embed,
            "user_info",
            str(banner.url) if banner else None,
        )

        flags = [
            _pretty_name(name)
            for name, enabled in getattr(profile, "public_flags", [])
            if enabled
        ]
        account_lines = [
            f"**Username**  `{user}`",
            f"**Created**  {discord.utils.format_dt(user.created_at, style='R')}",
            f"**Type**  {'Bot account' if user.bot else 'User account'}",
        ]
        if flags:
            account_lines.append(f"**Badges**  {_compact_list(flags, 6)}")
        if getattr(profile, "avatar_decoration", None):
            account_lines.append("**Avatar decoration**  Equipped")
        embed.add_field(name="🪪 Account", value="\n".join(account_lines), inline=True)

        guilds = list(getattr(user, "mutual_guilds", []))
        nicknames: List[str] = []
        if await self.bot.get_privacy(user, "nicks") and user.id != self.bot.user.id:
            for guild in guilds:
                member = guild.get_member(user.id)
                if member and member.display_name not in {user.display_name, *nicknames}:
                    nicknames.append(member.display_name)

        if isinstance(user, Member):
            roles = [role.mention for role in reversed(user.roles[1:])]
            server_lines = [
                f"**Joined**  {discord.utils.format_dt(user.joined_at, style='R') if user.joined_at else 'Unknown'}",
                f"**Top role**  {user.top_role.mention}",
                f"**Roles**  {len(user.roles) - 1:,}",
            ]
            if user.premium_since:
                server_lines.append(
                    f"**Server boost**  {discord.utils.format_dt(user.premium_since, style='R')}"
                )
            if user.pending:
                server_lines.append("**Screening**  Pending")
            if user.timed_out_until:
                server_lines.append(
                    f"**Timed out until**  {discord.utils.format_dt(user.timed_out_until, style='R')}"
                )
            embed.add_field(name="🛰 Server Presence", value="\n".join(server_lines), inline=True)
            if roles:
                embed.add_field(
                    name="🎨 Role Constellation",
                    value=_compact_list(roles, 10)[:1024],
                    inline=False,
                )

            text_access = sum(
                channel.permissions_for(user).view_channel
                for channel in self.ctx.guild.text_channels
            )
            voice_access = sum(
                channel.permissions_for(user).view_channel
                for channel in self.ctx.guild.voice_channels
            )
            notable = {
                "administrator", "view_audit_log", "manage_guild", "manage_roles",
                "manage_channels", "manage_emojis", "moderate_members", "kick_members",
                "ban_members", "manage_messages", "mention_everyone",
            }
            permissions = [
                _pretty_name(name)
                for name, enabled in user.guild_permissions
                if enabled and name in notable
            ]
            if user.guild_permissions.administrator:
                permissions = ["Administrator"]
            access_lines = [
                f"**Visible channels**  {text_access:,} text • {voice_access:,} voice"
            ]
            if permissions:
                access_lines.append(
                    f"**Notable permissions**  {_compact_list(permissions, 7)}"
                )
            embed.add_field(
                name="🔐 Access Matrix",
                value="\n".join(access_lines),
                inline=False,
            )

            statuses = []
            for label, value in (
                ("Desktop", getattr(user, "desktop_status", discord.Status.offline)),
                ("Mobile", getattr(user, "mobile_status", discord.Status.offline)),
                ("Web", getattr(user, "web_status", discord.Status.offline)),
            ):
                if value is not discord.Status.offline:
                    statuses.append(f"{label}: {_pretty_name(value)}")
            activities = list(dict.fromkeys(
                activity.name for activity in user.activities if activity.name
            ))
            activity_lines = []
            if user.status is not discord.Status.offline:
                activity_lines.append(f"**Status**  {_pretty_name(user.status)}")
            if statuses:
                activity_lines.append(f"**Devices**  {_compact_list(statuses, 3)}")
            if activities:
                activity_lines.append(f"**Activities**  {_compact_list(activities, 5)}")
            if user.bot and self.ctx.guild.me.guild_permissions.view_audit_log:
                inviter = None
                with suppress(HTTPException):
                    async for entry in self.ctx.guild.audit_logs(
                        limit=100,
                        action=discord.AuditLogAction.bot_add,
                    ):
                        if entry.target and entry.target.id == user.id:
                            inviter = entry.user
                            break
                if inviter:
                    activity_lines.append(f"**Added by**  {inviter.mention}")
            if activity_lines:
                embed.add_field(
                    name="📡 Live Activity",
                    value="\n".join(activity_lines),
                    inline=False,
                )

        history_lines = []
        if guilds:
            history_lines.append(f"**Shared servers**  {len(guilds):,}")
        if nicknames:
            history_lines.append(f"**Known nicknames**  {_compact_list(nicknames, 5)}")
        if await self.bot.get_privacy(user, "activity_info"):
            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    "select last_online, last_message from activity where user_id = %s limit 1;",
                    (user.id,),
                )
                row = await cur.fetchone()
            if row:
                for label, raw_value in (("Last online", row[0]), ("Last message", row[1])):
                    if raw_value is None:
                        continue
                    try:
                        elapsed = max(0, round(time() - float(str(raw_value).replace(",", ""))))
                    except (TypeError, ValueError):
                        continue
                    history_lines.append(f"**{label}**  {get_time(elapsed)} ago")

        if await self.bot.get_privacy(user, "username_history"):
            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    "select username from usernames where user_id = %s order by changed_at desc;",
                    (user.id,),
                )
                results = await cur.fetchall()
            names = []
            for result in results:
                decoded = self.bot.decode(result[0])
                if decoded != str(user) and decoded not in names:
                    names.append(decoded)
            if names:
                history_lines.append(f"**Past usernames**  {_compact_list(names, 5)}")

        if len(history_lines) == 1:
            account_lines.extend(history_lines)
            account_field = embed.fields[0]
            embed.set_field_at(
                0,
                name=account_field.name,
                value="\n".join(account_lines)[:1024],
                inline=account_field.inline,
            )
        elif history_lines:
            embed.add_field(
                name="🧭 History & Reach",
                value="\n".join(history_lines)[:1024],
                inline=False,
            )
        embed.set_footer(
            text=f"User ID {user.id} • Requested by {self.ctx.author}",
            icon_url=self.ctx.author.display_avatar.url,
        )
        yield embed


    async def channel_info(self) -> Generator[None, None, Embed]:
        supported_channels = (
            TextChannel, VoiceChannel, StageChannel, ForumChannel, CategoryChannel,
        )
        channel = self.ctx.channel
        if isinstance(self.target, supported_channels):
            channel = self.target
        guild = channel.guild
        guild_icon = str(guild.icon.url) if guild.icon else self.bot.user.display_avatar.url

        embed = Embed(
            title=f"#️⃣ {channel.name}",
            description=channel.mention,
            color=colors.fate,
            timestamp=datetime.now(tz=timezone.utc),
        )
        embed.set_author(
            name=f"Channel telemetry • {guild.name}",
            icon_url=guild_icon,
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        overview = [
            f"**Type**  {_pretty_name(channel.type)}",
            f"**Position**  {channel.position + 1:,}",
            f"**Created**  {discord.utils.format_dt(channel.created_at, style='R')}",
        ]
        if channel.category:
            overview.insert(1, f"**Category**  {channel.category.mention}")
        embed.add_field(name="🧭 Coordinates", value="\n".join(overview), inline=True)

        overwrites = channel.overwrites
        role_overwrites = sum(isinstance(target, Role) for target in overwrites)
        member_overwrites = sum(isinstance(target, Member) for target in overwrites)
        visible_members = len(getattr(channel, "members", []))
        is_nsfw = channel.is_nsfw() if hasattr(channel, "is_nsfw") else False
        access = [
            f"**Visible members**  {visible_members:,}",
            f"**Permission sync**  {'Synced' if channel.permissions_synced else 'Customized'}",
        ]
        if role_overwrites or member_overwrites:
            access.append(
                f"**Overrides**  {role_overwrites:,} roles • {member_overwrites:,} members"
            )
        if is_nsfw:
            access.append("**NSFW**  Yes")
        embed.add_field(name="🔐 Access", value="\n".join(access), inline=True)

        slowmode = getattr(channel, "slowmode_delay", 0)
        settings = []
        if slowmode:
            settings.append(f"**Slowmode**  {format_date(seconds=slowmode)}")
        if isinstance(channel, (TextChannel, ForumChannel)):
            thread_slowmode = channel.default_thread_slowmode_delay
            if thread_slowmode:
                settings.append(
                    f"**Thread slowmode**  {format_date(seconds=thread_slowmode)}"
                )
            if channel.default_auto_archive_duration:
                settings.append(
                    f"**Thread archive**  {channel.default_auto_archive_duration:,} minutes"
                )
        if isinstance(channel, TextChannel) and channel.is_news():
            settings.append("**Announcement channel**  Yes")
        if isinstance(channel, (VoiceChannel, StageChannel)):
            settings.extend((
                f"**Bitrate**  {channel.bitrate // 1000:,} kbps",
                f"**Video quality**  {_pretty_name(channel.video_quality_mode)}",
            ))
            if channel.user_limit:
                settings.append(f"**User limit**  {channel.user_limit:,}")
            if channel.rtc_region:
                settings.append(f"**Region**  {channel.rtc_region}")
        if isinstance(channel, ForumChannel):
            if channel.available_tags:
                settings.append(f"**Available tags**  {len(channel.available_tags):,}")
            if channel.default_layout:
                settings.append(
                    f"**Default layout**  {_pretty_name(channel.default_layout)}"
                )
            if channel.default_sort_order:
                settings.append(
                    f"**Default sort**  {_pretty_name(channel.default_sort_order)}"
                )
        if settings:
            embed.add_field(
                name="⚙️ Transmission Settings",
                value="\n".join(settings),
                inline=False,
            )

        topic = getattr(channel, "topic", None)
        if topic:
            embed.add_field(name="📝 Topic", value=topic[:1024], inline=False)

        activity = []
        last_message_id = getattr(channel, "last_message_id", None)
        if last_message_id:
            last_message_at = discord.utils.snowflake_time(last_message_id)
            activity.append(
                f"**Last message**  {discord.utils.format_dt(last_message_at, style='R')} • "
                f"`{last_message_id}`"
            )
        threads = list(getattr(channel, "threads", []))
        if threads:
            activity.append(f"**Active threads**  {len(threads):,}")
        if channel.permissions_for(guild.me).manage_webhooks:
            activity.append("**Webhooks**  Manageable")
        if activity:
            embed.add_field(
                name="📡 Activity",
                value="\n".join(activity),
                inline=False,
            )

        bot_permissions = channel.permissions_for(guild.me)
        notable = (
            "view_channel", "send_messages", "embed_links", "attach_files",
            "manage_channels", "manage_messages", "manage_threads", "manage_webhooks",
        )
        granted = [
            _pretty_name(name)
            for name in notable
            if getattr(bot_permissions, name, False)
        ]
        if granted:
            embed.add_field(
                name="🤖 Fate's Channel Access",
                value=_compact_list(granted, 8),
                inline=False,
            )
        embed.set_footer(
            text=f"Channel ID {channel.id} • Requested by {self.ctx.author}",
            icon_url=self.ctx.author.display_avatar.url,
        )
        yield embed

    async def role_info(self) -> Generator[None, None, Embed]:
        e = discord.Embed(color=colors.fate)
        e.set_author(
            name="Alright, here's what I got..", icon_url= self.bot.user.display_avatar.url
        )
        role = self.target  # type: discord.Role

        core = {
            "Name": role.name,
            "Mention": role.mention,
            "ID": role.id,
            "Created at": discord.utils.format_dt(role.created_at, style="F"),
        }
        if role.members:
            core["Members"] = len(role.members)

        extra = {
            "Mentionable": str(role.mentionable),
            "HEX Color": role.color,
            "RGB Color": role.colour.to_rgb(),
        }
        if role.hoist:
            extra["**Shows Above Other Roles**"] = None
        if role.managed:
            extra["**And Is An Integrated Role**"] = None

        e.add_field(
            name="◈ Role Information",
            value= self.bot.utils.format_dict(core),
            inline=False,
        )
        e.add_field(
            name="◈ Extra", value= self.bot.utils.format_dict(extra), inline=False
        )
        e.set_footer(text="React With ♻ For History")
        yield e

    async def invite_info(self) -> Generator[None, None, Embed]:
        e = discord.Embed(color=colors.fate)
        e.set_author(
            name="Alright, here's what I got..", icon_url=self.bot.user.display_avatar.url
        )
        data = {}
        inv = [arg for arg in self.ctx.message.content.split() if "discord.gg" in arg][0]
        code = discord.utils.resolve_invite(inv)
        try:
            invite = await self.bot.fetch_invite(code)
            e.set_author(
                name="Alright, here's what I got..",
                icon_url=invite.guild.icon.url if invite.guild.icon else None,
            )
            if invite.guild.splash:
                e.set_thumbnail(url=invite.guild.splash.url)
            if invite.guild.banner:
                e.set_image(url=invite.guild.banner.url)
            data = {
                "Guild": invite.guild.name,
                "GuildID": invite.guild.id,
                "channel_name": invite.channel.name,
                "channel_id": invite.channel.id
            }
        except (discord.errors.NotFound, discord.errors.Forbidden):
            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    "select guild_id, guild_name, channel_id, channel_name "
                    "from invites where code = %s;",
                    (self.bot.encode(code),),
                )
                if not cur.rowcount:
                    await self.ctx.send("Failed to query that invite")
                    raise StopIteration()
                results = await cur.fetchone()
                data = {
                    "guild_name": self.bot.decode(results[1]),
                    "guild_id": results[0],
                    "channel_name": self.bot.decode(results[3]),
                    "channel_id": results[2],
                }
                e.set_footer(text="⚠ From Cache ⚠")

        inviters = []

        e.add_field(
            name="◈ Invite Information",
            value=self.bot.utils.format_dict(data),
            inline=False,
        )
        if inviters:
            e.add_field(
                name="◈ Inviters", value=", ".join(inviters[:16]), inline=False
            )
        yield e


    async def server_info(self):
        guild: Guild = self.ctx.guild
        owner = guild.owner
        icon_url = (
            str(guild.icon.url)
            if guild.icon
            else owner.display_avatar.url if owner else self.bot.user.display_avatar.url
        )
        embed = Embed(
            title=f"🪐 {guild.name}",
            description=guild.description or None,
            color=colors.fate,
            timestamp=datetime.now(tz=timezone.utc),
        )
        embed.set_author(
            name=f"Server telemetry • {guild.name}",
            icon_url=icon_url,
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        members = list(guild.members)
        bots = sum(member.bot for member in members)
        online = sum(member.status is not discord.Status.offline for member in members)
        population = [
            f"**Members**  {(guild.member_count or len(members)):,}",
            f"**Humans / bots**  {max(0, len(members) - bots):,} / {bots:,}",
            f"**Online now**  {online:,}",
        ]
        if guild.premium_subscribers:
            population.append(f"**Boosters**  {len(guild.premium_subscribers):,}")
        embed.add_field(name="👥 Population", value="\n".join(population), inline=True)

        identity = [
            f"**Owner**  {owner.mention if owner else 'Unknown'}",
            f"**Created**  {discord.utils.format_dt(guild.created_at, style='R')}",
            f"**Locale**  {_pretty_name(guild.preferred_locale)}",
            f"**Shard**  {guild.shard_id:,}",
        ]
        if guild.vanity_url_code:
            identity.append(f"**Vanity**  `discord.gg/{guild.vanity_url_code}`")
        embed.add_field(name="🧭 Identity", value="\n".join(identity), inline=True)

        forums = sum(isinstance(channel, discord.ForumChannel) for channel in guild.channels)
        channels = [
            f"**Text**  {len(guild.text_channels):,}",
            f"**Voice / stage**  {len(guild.voice_channels):,} / {len(guild.stage_channels):,}",
            f"**Forums / categories**  {forums:,} / {len(guild.categories):,}",
            f"**Active threads**  {len(guild.threads):,}",
        ]
        embed.add_field(name="📡 Channel Network", value="\n".join(channels), inline=True)

        feature_names = {
            "ANIMATED_BANNER": "Animated banner",
            "ANIMATED_ICON": "Animated icon",
            "AUTO_MODERATION": "AutoMod",
            "COMMUNITY": "Community",
            "DISCOVERABLE": "Discovery",
            "INVITES_DISABLED": "Invites paused",
            "MEMBER_VERIFICATION_GATE_ENABLED": "Membership screening",
            "MONETIZATION_ENABLED": "Monetization",
            "MORE_STICKERS": "Expanded stickers",
            "NEWS": "Announcement channels",
            "PARTNERED": "Partnered",
            "PREVIEW_ENABLED": "Server preview",
            "ROLE_ICONS": "Role icons",
            "VANITY_URL": "Vanity URL",
            "VERIFIED": "Verified",
        }
        features = [feature_names.get(feature, _pretty_name(feature)) for feature in guild.features]
        community = [f"**Roles**  {len(guild.roles):,}"]
        if features:
            community.insert(0, f"**Features**  {_compact_list(features, 9)}")
        if guild.scheduled_events:
            community.append(f"**Events**  {len(guild.scheduled_events):,}")
        if guild.emojis or guild.stickers:
            community.append(
                f"**Emoji / stickers**  {len(guild.emojis):,} / {len(guild.stickers):,}"
            )
        embed.add_field(name="✨ Community Systems", value="\n".join(community)[:1024], inline=False)

        security = [
            f"**Verification**  {_pretty_name(guild.verification_level)}",
            f"**Content filter**  {_pretty_name(guild.explicit_content_filter)}",
            f"**Moderator 2FA**  {'Required' if guild.mfa_level else 'Not required'}",
            f"**Age restriction**  {_pretty_name(guild.nsfw_level)}",
        ]
        if guild.safety_alerts_channel:
            security.append(f"**Safety alerts**  {guild.safety_alerts_channel.mention}")
        embed.add_field(name="🛡️ Safety Deck", value="\n".join(security), inline=True)

        system = []
        if guild.system_channel:
            system.append(f"**System**  {guild.system_channel.mention}")
        if guild.rules_channel:
            system.append(f"**Rules**  {guild.rules_channel.mention}")
        if guild.public_updates_channel:
            system.append(f"**Updates**  {guild.public_updates_channel.mention}")
        if guild.afk_channel:
            system.append(
                f"**AFK**  {guild.afk_channel.mention} • "
                f"{format_date(seconds=guild.afk_timeout)}"
            )
        if system:
            embed.add_field(name="⚙️ Operations", value="\n".join(system), inline=True)

        capacity = [
            f"**Emoji / sticker slots**  {guild.emoji_limit:,} / {guild.sticker_limit:,}",
            f"**Voice bitrate**  {bytes2human(guild.bitrate_limit).replace('.0', '')}",
            f"**Upload ceiling**  {bytes2human(guild.filesize_limit).replace('.0', '')}",
        ]
        if guild.premium_tier or guild.premium_subscription_count:
            capacity.insert(
                0,
                f"**Boost tier**  {guild.premium_tier} • "
                f"{guild.premium_subscription_count:,} boosts",
            )
        embed.add_field(name="🚀 Capacity", value="\n".join(capacity), inline=False)
        embed.set_footer(
            text=f"Server ID {guild.id} • Requested by {self.ctx.author}",
            icon_url=self.ctx.author.display_avatar.url,
        )
        yield embed


class SelectMenu(ui.Select):
    """ Select options for the parent view """
    def __init__(self, parent: InfoView) -> None:
        self.info_view = parent
        options = [
            SelectOption(
                label="User Info", value="user_info", emoji="👤",
                description="Profile, roles, activity, and account details",
                default=parent.current_page == "user_info",
            ),
            SelectOption(
                label="Bot Info", value="bot_info", emoji="🤖",
                description="Fate statistics, uptime, and credits",
                default=parent.current_page == "bot_info",
            ),
            SelectOption(
                label="Server Info", value="server_info", emoji="🏠",
                description="Members, security, boosts, and creation date",
                default=parent.current_page == "server_info",
            ),
            SelectOption(
                label="Channel Info", value="channel_info", emoji="#️⃣",
                description="Channel properties, access, and creation date",
                default=parent.current_page == "channel_info",
            ),
        ]
        if isinstance(self.info_view.target, str):
            options.append(SelectOption(
                label="Invite Info", value="invite_info", emoji="✉️",
                description="Details about the supplied Discord invite",
                default=parent.current_page == "invite_info",
            ))
        if isinstance(self.info_view.target, Role):
            options.append(SelectOption(
                label="Role Info", value="role_info", emoji="🎨",
                description="Role members, color, and integration details",
                default=parent.current_page == "role_info",
            ))
        super().__init__(
            min_values=1,
            max_values=1,
            placeholder="Choose an information page",
            options=options,
            row=0,
        )

    async def callback(self, interaction: Interaction) -> None:
        """ Call the parent classes info function """
        await interaction.response.defer()
        choice: str = interaction.data["values"][0]
        await self.info_view.render_page(choice, interaction)


async def setup(bot):
    await bot.add_cog(Utility(bot), override=True)

