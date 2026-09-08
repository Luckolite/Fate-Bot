"""
cogs.core.core.py
~~~~~~~~~~~~~~~~~~

Core bot commands such as prefix, invite, and ping

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
from contextlib import suppress
from copy import copy
from datetime import datetime, timezone
from importlib import reload
from time import monotonic
from typing import *

import discord
from discord import ui, SelectOption, Interaction, User
from discord.ext import commands, tasks

import botutils
from botutils import colors, get_prefixes_async, emojis, Conversation, \
    url_from, format_date, sanitize, Cooldown, GetChoice

reload(botutils)


join_channels = ["bot", "general", "main"]
join_message = """
🪐 **Thank you for inviting Fate**
Here's a few things to know

> **Basic Usage**
- Command Prefix: `.`
- For a list of commands, send `.help`, and navigate through the dropdown
- Changed the bots prefix? My ping/mention works as a secondary prefix
- For an overview of enabled/disabled modules, send `.config`

> **Suggestions**
- If you use Fate for moderation; try out `.modmail`.
  This lets users create threads to discuss infractions with the moderators

> **Extra**
- Interactive games and utilities
- Factions economy game via .f

> **Community & Support**
https://discord.gg/wtjuznh
"""


default_privacy_settings = {
    "xp": True,
    "fun_commands": True,
    "nicks": True,
    "username_history": True,
    "activity_info": False,
    "mod_logs": True
}
descriptions = {
    "xp": "The leveling system",
    "fun_commands": "reactions, .gay, etc",
    "nicks": "Shows all nicknames in .info",
    "username_history": "Shows past usernames in .info",
    "activity_info": "Shows activity info in .info",
    "mod_logs": "ghost typing, profile changes"
}


class Core(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last = {}
        self.spam_cd = {}

        # if not hasattr(bot, "get_privacy"):
        async def get_privacy(user: Union[discord.User, discord.Member], item: str):
            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    "select value from privacy where user_id = %s "
                    "and item = %s limit 1;",
                    (user.id, item)
                )
                if cur.rowcount:
                    value, = await cur.fetchone()
                else:
                    value = default_privacy_settings[item]
                return value
        bot.get_privacy = get_privacy

        self.config = bot.utils.cache("disabled")
        self.join_dates = {
            guild.id: guild.me.joined_at for guild in bot.guilds
        }
        self.guild_cooldown = Cooldown(8, 10)
        self.user_cooldown = Cooldown(6, 10)
        self.rate_limit_events: Dict[int, List[float]] = {}
        self.ignored_until: Dict[int, float] = {}
        self.owned_ignored_locations: Set[int] = set()
        self.rate_limit_cleanup_task.start()

    async def cog_unload(self):
        self.rate_limit_cleanup_task.cancel()
        for location_id in self.owned_ignored_locations:
            with suppress(ValueError):
                self.bot.ignored_locations.remove(location_id)
        self.owned_ignored_locations.clear()

    @tasks.loop(seconds=15)
    async def rate_limit_cleanup_task(self):
        current = monotonic()
        expired = [
            location_id for location_id, deadline in self.ignored_until.items()
            if deadline <= current
        ]
        for location_id in expired:
            self.ignored_until.pop(location_id, None)
            self.owned_ignored_locations.discard(location_id)
            with suppress(ValueError):
                self.bot.ignored_locations.remove(location_id)

        strike_cutoff = current - (60 * 60 * 24)
        for user_id, timestamps in list(self.rate_limit_events.items()):
            active = [timestamp for timestamp in timestamps if timestamp > strike_cutoff]
            if active:
                self.rate_limit_events[user_id] = active
            else:
                self.rate_limit_events.pop(user_id, None)


    @commands.Cog.listener()
    async def on_dbl_test(self, data):
        if self.bot.debug_mode:
            return
        channel = self.bot.get_channel(self.bot.config["log_channel"])
        user = self.bot.get_user(int(data["user"]))
        if not user:
            user = await self.bot.fetch_user(int(data["user"]))
        e = discord.Embed(color=colors.pink)
        e.set_author(name=f"{user} Upvoted The Bot", icon_url=user.display_avatar.url)
        await channel.send(embed=e)

    @commands.Cog.listener()
    async def on_dbl_vote(self, data):
        if self.bot.debug_mode:
            return
        channel = self.bot.get_channel(self.bot.config["log_channel"])
        user: discord.User = self.bot.get_user(int(data["user"]))
        if not user:
            user = await self.bot.fetch_user(int(data["user"]))
        with suppress(discord.HTTPException):
            await user.send(
                "Thanks for voting! Run `.f vote` in your faction's server "
                "to redeem $250 for your faction."
            )
        e = discord.Embed(color=colors.pink)
        e.set_author(name=f"{user} Upvoted The Bot", icon_url=user.display_avatar.url)
        e.description = f"📬 | {len(user.mutual_guilds)} mutual servers"
        if channel is not None:
            with suppress(discord.HTTPException):
                await channel.send(embed=e)
        # The authenticated HTTP receiver commits the vote before dispatching
        # this notification. Discord logging must never gate reward delivery.
        await asyncio.sleep(60 * 60 * 12)
        with suppress(Exception):
            await user.send(
                "Your vote timer has refreshed, and you can vote again at https://vote.fatebot.xyz/"
            )

    @commands.Cog.listener()
    async def on_ready(self):
        self.join_dates = {
            guild.id: guild.me.joined_at for guild in self.bot.guilds if guild
        }

    @commands.Cog.listener()
    async def on_command(self, ctx):
        user_on_cd = self.user_cooldown.check(ctx.author.id)
        guild_on_cd = self.guild_cooldown.check(ctx.channel.id)
        if user_on_cd or guild_on_cd:
            target = ctx.guild
            if user_on_cd:
                target = ctx.author
            if target.id not in self.bot.ignored_locations:
                self.bot.ignored_locations.append(target.id)
                self.owned_ignored_locations.add(target.id)
                self.bot.log.info(f"Rate limited {target} for spamming {ctx.command}")
                duration = 15
                if user_on_cd:
                    duration += 120
                    current = monotonic()
                    cutoff = current - (60 * 60 * 24)
                    strikes = [
                        timestamp
                        for timestamp in self.rate_limit_events.get(target.id, [])
                        if timestamp > cutoff
                    ]
                    strikes.append(current)
                    if len(strikes) >= 3:
                        duration += 60 * 60 * 24
                        strikes.clear()
                    self.rate_limit_events[target.id] = strikes
                self.ignored_until[target.id] = monotonic() + duration

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        self.join_dates[guild.id] = guild.me.joined_at
        if not self.bot.is_ready() or not guild or not guild.owner:
            return

        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select * from blocked where user_id in (%s, %s);",
                (guild.owner.id, guild.id),
            )
            if cur.rowcount:
                await guild.leave()
                return self.bot.log(f"Left {guild} due to the server being blacklisted")

        sent_join_message = False
        for channel in guild.text_channels:
            if any(name in channel.name for name in join_channels):
                if channel.permissions_for(guild.me).send_messages:
                    try:
                        await channel.send(join_message)
                        sent_join_message = True
                        break
                    except (discord.errors.NotFound, discord.errors.Forbidden):
                        pass

        channel = self.bot.get_channel(self.bot.config["server_log"])
        e = discord.Embed(color=colors.green)
        e.set_author(name=guild.name, icon_url=url_from(guild.icon))
        if guild.splash:
            e.set_thumbnail(url=guild.splash.url)
        if guild.banner:
            e.set_image(url=guild.banner.url)
        e.description = f"👥 | {len(guild.members)} Members"
        if sent_join_message:
            e.description += "\n⁉| Sent join message"
        if guild.me.guild_permissions.view_audit_log:
            try:
                async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=1):
                    e.set_footer(text=str(entry.user), icon_url=entry.user.display_avatar.url)
            except (discord.errors.Forbidden, discord.errors.NotFound, discord.errors.HTTPException):
                pass
        await channel.send(embed=e)  # type: ignore

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        if not self.bot.is_ready():
            return
        channel = self.bot.get_channel(self.bot.config["server_log"])
        e = discord.Embed(color=colors.red)
        e.set_author(name=guild.name, icon_url=url_from(guild.icon))
        if guild.splash:
            e.set_thumbnail(url=guild.splash.url)
        if guild.banner:
            e.set_image(url=guild.banner.url)
        e.description = f"👥 | {len(guild.members)} Members"
        if guild.id in self.join_dates:
            join_duration = format_date(self.join_dates[guild.id])
            del self.join_dates[guild.id]
            e.description += f"\n⏰ | {join_duration}"
        await channel.send(embed=e)  # type: ignore

    @commands.command(name="votes", description="Links to Fate's Top.gg vote page")
    @commands.is_owner()
    async def votes(self, ctx):
        if not self.bot.vote_url:
            return await ctx.send("Vote handling is disabled on this secondary instance.")
        await ctx.send(f"Vote for Fate: {self.bot.vote_url}")

    # @commands.command(name="setup", enabled=False, description="Helps you set the bot up via conversation")
    # @commands.cooldown(1, 5, commands.BucketType.user)
    # @commands.guild_only()
    # @commands.has_permissions(administrator=True)
    # @commands.cooldown(1, 10, commands.BucketType.guild)
    async def setup(self, ctx):
        await ctx.send("Note this cmd doesn't actually do anything", delete_after=5)
        convo = Conversation(ctx)
        await convo.send(
            "To follow with setup just reply with `yes/no` for whether or not you want "
            "to use the given module. To stop just send `cancel`."
        )

        # Set the prefix
        reply = await convo.ask("To start, what's the command prefix you want me to have")
        await self.bot.get_command("prefix")(ctx, prefix=reply.content)

        # Anti Spam
        reply = await convo.ask("Do you want to use AntiSpam to mute any spammers I detect?", use_buttons=True)
        if reply:
            await convo.send("Alright, you can customize this more with `.antispam configure`")
            await self.bot.get_command("antispam enable")(ctx)

        # Logger
        reply = await convo.ask(
            "Do you want a logs channel set to show things like deleted messages? if so #mention the channel"
        )
        if reply.channel_mentions:
            channel = reply.channel_mentions[0]
            await self.bot.get_command("log enable")(ctx, channel=channel)

        # Verification
        reply = await convo.ask("Do you want users to verify via a captcha when they join the server?", use_buttons=True)
        if reply:
            await self.bot.get_command("verification enable")(ctx)

        await convo.send("Setup complete")

    @commands.command(name="topguilds", description="Displays the top 8 servers based on highest member count")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.bot_has_permissions(embed_links=True)
    async def topguilds(self, ctx):
        e = discord.Embed(color=0x80B0FF)
        e.title = "Top Guilds"
        e.description = ""
        rank = 1
        items = [[g.name, g.member_count] for g in self.bot.guilds if g]
        for guild, count in sorted(items, key=lambda k: k[1], reverse=True, )[:8]:
            e.description += f"**{rank}.** {guild}: `{count}`\n"
            rank += 1
        await ctx.send(embed=e)

    @commands.hybrid_command(
        name="invite",
        aliases=["links", "support"],
        description="Gives links to install Fate for yourself or a server"
    )
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def invite(self, ctx):
        user_install_url = getattr(
            self.bot,
            "user_install_url",
            (
                "https://discord.com/oauth2/authorize"
                f"?client_id={self.bot.user.id}"
                "&integration_type=1&scope=applications.commands"
            ),
        )
        embed = discord.Embed(color=0x80B0FF)
        embed.set_author(
            name=f"| Links | 📚",
            icon_url="https://images-ext-1.discordapp.net/external/kgeJxDOsmMoy2gdBr44IFpg5hpYzqxTkOUqwjYZbPtI/%3Fsize%3D1024/https/cdn.discordapp.com/avatars/506735111543193601/689cf49cf2435163ca420996bcb723a5.webp",
        )
        embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/501871950260469790/513636736492896271/mail-open-solid.png")
        embed.description = (
            f"[Add to My Apps]({user_install_url}) 📲\n"
            f"[Add to a Server]({self.bot.invite_url}) 📥\n"
            f"[Support](https://discord.gg/wtjuznh/) 📧\n"
            f"[Discord](https://discord.gg/wtjuznh/) <:discord:513634338487795732>\n"
            f"[Donate](https://ko-fi.com/fatebot) ☕\n"
            f"[Vote](https://vote.fatebot.xyz/) ⬆"
        )
        if not self.bot.vote_url:
            embed.description = embed.description.rsplit("\n", 1)[0]
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="vote", description="Sends the link to vote for the bot on top.gg")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def vote(self, ctx):
        if not self.bot.vote_url:
            return await ctx.send("Vote rewards are only available on the primary Fate bot.")
        await ctx.send(self.bot.vote_url)

    @commands.command(name="say", description="Sends a message as the bot")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(attach_files=True)
    async def say(self, ctx, *, content):
        if not ctx.channel.permissions_for(ctx.guild.me).manage_messages:
            if len(str(content).split("\n")) > 4:
                await ctx.send(f"{ctx.author.mention} too many lines")
                with suppress(Exception):
                    await ctx.message.delete()
                return
            if len(str(content)) > 100:
                return await ctx.send("That's too long")
            content = await sanitize(content)
        await ctx.send(content, allowed_mentions=discord.AllowedMentions.none())
        if not ctx.message.mentions and not ctx.message.role_mentions and ctx.message:
            await ctx.message.delete()

    @commands.hybrid_command(name="prefix", description="Changes the servers prefix")
    @commands.cooldown(2, 5, commands.BucketType.user)
    @commands.guild_only()
    async def prefix(self, ctx, *, prefix: str = None):
        if not prefix or ctx.message.mentions or (len(prefix) > 10 and prefix.isdigit()):
            user = ctx.author
            if ctx.message.mentions:
                user = ctx.message.mentions[0]
            elif prefix and prefix.isdigit():
                user = self.bot.get_user(int(prefix))
                if not user:
                    return await ctx.send("I can't find a user with that ID")
            ctx.message = copy(ctx.message)
            ctx.message.author = user
            prefixes = await get_prefixes_async(self.bot, ctx.message)
            formatted = "\n".join(prefixes[1::])
            e = discord.Embed(color=colors.fate)
            e.set_author(name="Prefixes", icon_url=user.display_avatar.url)
            e.description = formatted
            return await ctx.send(embed=e)
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.send(f"You need manage_server permission(s) to use this")
        if not isinstance(ctx.guild, discord.Guild):
            return await ctx.send("This command can't be used in dm")
        if len(prefix) > 5:
            return await ctx.send("That prefix is too long")
        opts = ["yes", "no"]
        choice = await self.bot.utils.get_choice(ctx, opts, name="Allow personal prefixes?")
        override = True if choice == "no" else False
        if ctx.guild.id in self.bot.guild_prefixes:
            if not override and prefix == ".":
                await self.bot.aio_mongo["GuildPrefixes"].delete_one({
                    "_id": ctx.guild.id
                })
                del self.bot.guild_prefixes[ctx.guild.id]
                return await ctx.send("Reset the prefix to default")
            else:
                await self.bot.aio_mongo["GuildPrefixes"].update_one(
                    filter={"_id": ctx.guild.id},
                    update={"$set": {"prefix": prefix, "override": override}}
                )
        else:
            if not override and prefix == ".":
                return await ctx.send("tHaT's tHe sAmE aS thE cuRreNt cOnfiG")
            await self.bot.aio_mongo["GuildPrefixes"].insert_one({
                "_id": ctx.guild.id,
                "prefix": prefix,
                "override": override
            })
        self.bot.guild_prefixes[ctx.guild.id] = {
            "prefix": prefix,
            "override": override
        }
        await ctx.send(f"Changed the servers prefix to `{prefix}`")

    @commands.hybrid_command(name="personal-prefix", aliases=["pp"], description="Sets a different prefix for only you")
    @commands.cooldown(2, 5, commands.BucketType.user)
    async def personal_prefix(self, ctx, *, prefix: str = ""):
        if prefix.startswith('"') and prefix.endswith('"') and len(prefix) > 2:
            prefix = prefix.strip('"')
        prefix = prefix.strip("'\"")
        if len(prefix) > 8:
            return await ctx.send("Your prefix can't be more than 8 chars long")
        if ctx.author.id in self.bot.user_prefixes:
            await self.bot.aio_mongo["UserPrefixes"].update_one(
                filter={"_id": ctx.author.id},
                update={"$set": {"prefix": prefix}}
            )
        else:
            await self.bot.aio_mongo["UserPrefixes"].insert_one({
                "_id": ctx.author.id,
                "prefix": prefix
            })
        self.bot.user_prefixes[ctx.author.id] = {"prefix": prefix}
        nothing = "something that doesn't exist"
        await ctx.send(
            f"Set your personal prefix as `{prefix if prefix else nothing}`\n"
            f"Note you can still use my mention as a sub-prefix"
        )

    @commands.hybrid_command(name="enable-command", aliases=["enablecommand"], description="Enables a disabled command")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(embed_links=True)
    async def enable_command(self, ctx, *, command: str):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("This server has no disabled commands")
        if not self.bot.get_command(command):
            return await ctx.send("That's not a command")
        command = self.bot.get_command(command).name
        for key, value in list(self.config[guild_id].items()):
            await asyncio.sleep(0)
            if command in value:
                break
        else:
            return await ctx.send(f"`{command}` isn't disabled anywhere")
        locations = [
            "enable globally", "enable in category", "enable in this channel"
        ]
        choice = await self.bot.utils.get_choice(ctx, locations, user=ctx.author)
        if not choice:
            return
        if choice == "enable globally":
            for key, value in list(self.config[guild_id].items()):
                await asyncio.sleep(0)
                if command in value:
                    self.config[guild_id][key].remove(command)
                    if not self.config[guild_id][key]:
                        await self.config.remove_sub(guild_id, key)
            await ctx.send(f"Enabled `{command}` in all channels")
        elif choice == "enable in category":
            if not ctx.channel.category:
                return await ctx.send("This channel has no category")
            for channel in ctx.channel.category.text_channels:
                await asyncio.sleep(0)
                channel_id = str(channel.id)
                if channel_id in self.config[guild_id]:
                    if command in self.config[guild_id][channel_id]:
                        self.config[guild_id][channel_id].remove(command)
                        if not self.config[guild_id][channel_id]:
                            await self.config.remove_sub(guild_id, channel_id)
            await ctx.send(f"Enabled `{command}` in all of {ctx.channel.category}'s channels")
        elif choice == "enable in this channel":
            channel_id = str(ctx.channel.id)
            if channel_id not in self.config[guild_id]:
                return await ctx.send("This channel has no disabled commands")
            if command not in self.config[guild_id][channel_id]:
                return await ctx.send(f"`{command}` isn't disabled in this channel")
            self.config[guild_id][channel_id].remove(command)
            if not self.config[guild_id][channel_id]:
                await self.config.remove_sub(guild_id, channel_id)
            await ctx.send(f"Enabled `{command}` in this channel")
        if guild_id in self.config and not self.config[guild_id]:
            await self.config.remove(guild_id)
        else:
            await self.config.flush()

    @commands.hybrid_command(
        name="disable-command",
        aliases=["disablecommand"],
        description="Disables a command in a channel, category or globally"
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(embed_links=True)
    async def disable_command(self, ctx, *, command: str = ""):
        if not command.strip():
            embed = discord.Embed(
                title="Disable Command Usage",
                description="`.disable-command <command>`\n\nExample: `.disable-command ping`",
                color=discord.Color.blue()
            )
            return await ctx.send(embed=embed)

        if "enable" in command.lower() or "disable" in command.lower():
            return await ctx.send("You can't disable commands with 'enable' or 'disable' in them")
        guild_id = ctx.guild.id
        if not self.bot.get_command(command):
            return await ctx.send("That's not a command")

        command = self.bot.get_command(command).name

        if guild_id not in self.config:
            self.config[guild_id] = {}

        choices = {
            "Globally within server": "globally",
            "Within category": "category",
            "In this channel": "channel"
        }
        choice_menu = GetChoice(ctx, choices)
        choice = await choice_menu

        if not choice:
            return

        channels = []
        if choice == "globally":
            channels = ctx.guild.text_channels
        elif choice == "category":
            if not ctx.channel.category:
                return await ctx.send("This channel has no category")
            channels = ctx.channel.category.text_channels
        elif choice == "channel":
            channels = [ctx.channel]

        for channel in channels:
            channel_id = str(channel.id)
            if channel.guild.id != guild_id:
                continue
            self.config[guild_id].setdefault(channel_id, [])
            if command in self.config[guild_id][channel_id]:
                return await ctx.send(f"The command `{command}` is already disabled {choice}.")
            self.config[guild_id][channel_id].append(command)

        await ctx.send(f"`{command}` disabled {choice}.")
        await self.config.flush()

    @commands.hybrid_command(
        name="disabled",
        aliases=["disabled-commands", "disabledcommands"],
        description="Shows the list of disabled commands"
    )
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    @commands.has_permissions(administrator=True)
    async def disabled(self, ctx):
        """ Lists the guilds disabled commands """
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("This server has no disabled commands")
        e = discord.Embed(color=discord.Color.red())
        for channel_id, commands in self.config[guild_id].items():
            await asyncio.sleep(0)
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                continue
            e.add_field(name=channel.name, value=", ".join([f"`{c}`" for c in commands]))
            if len(e) > 5800:
                await ctx.send(embed=e)
                e = discord.Embed(color=discord.Color.red())
        if len(e):
            await ctx.send(embed=e)

    @commands.hybrid_command(name="restrict", description="Prevents non mods from running commands in a channel")
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.has_permissions(administrator=True)
    async def restrict(self, ctx, args: str = None):
        if not args:
            e = discord.Embed(color=colors.fate)
            e.set_author(name="Channel Restricting")
            e.description = "Prevents everyone except mods from using commands"
            e.add_field(
                name="Usage",
                value=".restrict #channel_mention / @user_mention / @role_mention\n"
                ".unrestrict #channel_mention / @user_mention / @role_mention\n.restricted | Returns a list of restricted channels/users/roles",
            )
            return await ctx.send(embed=e)
        guild_id = ctx.guild.id
        if guild_id not in self.bot.restricted:
            self.bot.restricted[guild_id] = {"channels": [], "users": [], "roles": []}
        restricted = "**Restricted:**"
        dat = self.bot.restricted[guild_id]
        if "roles" not in dat:
            self.bot.restricted[guild_id]["roles"] = []

        if "everyone" == args.lstrip("@") or args == ctx.guild.default_role.mention:
            self.bot.restricted[guild_id]["roles"].append(ctx.guild.default_role.id)
            restricted += f"\n@everyone"

        for channel in ctx.message.channel_mentions:
            if channel.id in dat["channels"]:
                continue
            self.bot.restricted[guild_id]["channels"].append(channel.id)
            restricted += f"\n{channel.mention}"
        for member in ctx.message.mentions:
            if member.id in dat["users"]:
                continue
            self.bot.restricted[guild_id]["users"].append(member.id)
            restricted += f"\n{member.mention}"
        e = discord.Embed(color=colors.fate, description=restricted)
        await ctx.send("Do you want this to effect moderators too? Reply with `yes` or `no`")
        reply = await self.bot.utils.get_message(ctx)
        if "yes" in reply.content.lower():
            self.bot.restricted[guild_id]["effect_mods"] = True
            await ctx.send("Alright, I'll restrict moderators too")
        else:
            if "effect_mods" in self.bot.restricted[guild_id]:
                self.bot.restricted.remove_sub(guild_id, "effect_mods")
        await self.bot.restricted.flush()
        await ctx.send(embed=e)

    @commands.hybrid_command(name="unrestrict", description="Allows everyone to use commands in a channel")
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.has_permissions(administrator=True)
    async def unrestrict(self, ctx, *, args: str = None):
        guild_id = ctx.guild.id
        unrestricted = "**Unrestricted:**"
        if guild_id not in self.bot.restricted:
            return await ctx.send("Nothing's currently restricted")
        dat = self.bot.restricted[guild_id]
        if "roles" not in dat:
            self.bot.restricted[guild_id]["roles"] = []

        if "everyone" == args.lstrip("@") or args == ctx.guild.default_role.mention:
            if ctx.guild.default_role.id not in dat["roles"]:
                return await ctx.send("Everyone isn't restricted")
            self.bot.restricted[guild_id]["roles"].remove(ctx.guild.default_role.id)
            unrestricted += f"\n@everyone"

        for channel in ctx.message.channel_mentions:
            if channel.id in dat["channels"]:
                self.bot.restricted[guild_id]["channels"].remove(channel.id)
                unrestricted += f"\n{channel.mention}"
        for member in ctx.message.mentions:
            if member.id in dat["users"]:
                self.bot.restricted[guild_id]["users"].remove(member.id)
                unrestricted += f"\n{member.mention}"
        await self.bot.restricted.flush()
        e = discord.Embed(color=colors.fate, description=unrestricted)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="restricted", description="Shows the channels only mods can use commands in")
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.has_permissions(administrator=True)
    async def restricted(self, ctx):
        guild_id = ctx.guild.id
        if guild_id not in self.bot.restricted:
            return await ctx.send("This server doesn't have anything restricted")
        dat = self.bot.restricted[guild_id]
        e = discord.Embed(color=colors.fate)
        e.set_author(name="Restricted:", icon_url=ctx.author.display_avatar.url)
        e.description = ""
        if dat["channels"]:
            changelog = ""
            for channel_id in dat["channels"]:
                channel = self.bot.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel):
                    self.bot.restricted[guild_id]["channels"].remove(channel_id)
                    await self.bot.restricted.flush()
                else:
                    changelog += "\n" + channel.mention
            if changelog:
                e.description += changelog
        if dat["users"]:
            changelog = ""
            for user_id in dat["users"]:
                user = self.bot.get_user(user_id)
                if not isinstance(user, discord.User):
                    self.bot.restricted[guild_id]["users"].remove(user_id)
                    await self.bot.restricted.flush()
                else:
                    changelog += "\n" + user.mention
            if changelog:
                e.description += changelog
        await ctx.send(embed=e)

    @commands.hybrid_command(name="ping", description="Measures how long it takes for the bot to interact")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.bot_has_permissions(embed_links=True)
    async def ping(self, ctx):
        e = discord.Embed(color=colors.fate)
        e.set_author(name="Measuring ping")

        before = monotonic()
        msg = await ctx.send(embed=e)
        our_latency = round(self.bot.latency * 1000)
        send_ping = f"<:fate:1181442026185703434> **Mine:** `{round(self.bot.latency * 1000)}ms`\n" \
                    f"{emojis.discord} **API Send:** `{round(((monotonic() - before) * 1000) - our_latency)}ms`\n"

        response_time = (datetime.now(tz=timezone.utc) - ctx.message.created_at).total_seconds() * 1000
        response_ping = f"{emojis.discord} **API Receive:** `{round(response_time - our_latency)}ms`\n"
        imgs = [
            "https://cdn.discordapp.com/emojis/562592256939393035.png?v=1",
            "https://cdn.discordapp.com/emojis/562592178204049408.png?v=1",
            "https://cdn.discordapp.com/emojis/562592177692213248.png?v=1",
            "https://cdn.discordapp.com/emojis/562592176463151105.png?v=1",
            "https://cdn.discordapp.com/emojis/562592175880405003.png?v=1",
            "https://cdn.discordapp.com/emojis/562592175192539146.png?v=1"
        ]
        for i, limit in enumerate(range(175, 175 * 6, 175)):
            if response_time < limit:
                img = imgs[i]
                break
        else:
            img = imgs[5]

        shard_ping = ""
        for shard, latency in self.bot.latencies:
            try:
                ping = str(round(latency * 1000)) + "ms"
            except OverflowError:
                ping = "unknown"
            extra = ""
            if shard == ctx.guild.shard_id:
                extra = " (yours)"
            shard_ping += f"{emojis.boost} **Shard {shard}:** `{ping}`{extra}\n"

        e.set_author(name=f"Bots Latency", icon_url=self.bot.user.display_avatar.url)
        e.set_thumbnail(url=img)
        e.description = send_ping + response_ping + shard_ping
        await msg.edit(embed=e)

    @commands.hybrid_command(name="privacy", description="Manages your privacy preferences")
    @commands.max_concurrency(1, commands.BucketType.user)
    async def privacy(self, ctx):
        options = copy(default_privacy_settings)
        async with self.bot.utils.cursor() as cur:
            await cur.execute("select item, value from privacy where user_id = %s;", (ctx.author.id))
            if cur.rowcount:
                for item, value in await cur.fetchall():
                    options[item] = value
        original = copy(options)
        view = Toggles(options, ctx.author)
        msg = await ctx.send("Set your privacy settings", view=view)
        await view.wait(msg)

        async with self.bot.utils.cursor() as cur:
            for item, value in view.result.items():
                if value == default_privacy_settings[item]:
                    await cur.execute(
                        "delete from privacy"
                        " where user_id = %s"
                        " and item = %s",
                        (ctx.author.id, item)
                    )
                else:
                    await cur.execute(
                        "select * from privacy where user_id = %s "
                        "and item = %s limit 1;",
                        (ctx.author.id, item),
                    )
                    if cur.rowcount:
                        await cur.execute(
                            "update privacy set value = %s "
                            "where user_id = %s and item = %s",
                            (value, ctx.author.id, item)
                        )
                    else:
                        await cur.execute(
                            "insert into privacy values (%s, %s, %s);",
                            (ctx.author.id, item, value)
                        )
        if original != view.result:
            await ctx.reply(f"Updated your privacy settings")


async def setup(bot):
    await bot.add_cog(Core(bot), override=True)


class Toggles(ui.View):
    class Dropdown(ui.Select):
        def __init__(self, toggles: dict, user: User, view: ui.View):
            self.user = user
            self.toggles = toggles
            self.toggle_view = view
            self.stopped = False
            super().__init__(
                placeholder="Tap to toggle on/off",
                options=self.get_options(),
                min_values=1,
                max_values=1
            )

        def get_options(self) -> List[SelectOption]:
            done = SelectOption(emoji=emojis.yes, label="Save and Exit", value="done")
            return [
                done, *[
                    SelectOption(
                        emoji=emojis.on if value else emojis.off,
                        label=name.replace("_", " ").title(),
                        description=descriptions[name],
                        value=name
                    ) for name, value in self.toggles.items()
                ]
            ]

        async def callback(self, interaction: Interaction):
            if self.user.id != interaction.user.id:
                return await interaction.response.send_message(
                    "This isn't your menu to interact with",
                    ephemeral=True
                )
            for name in interaction.data["values"]:
                if name != "done":
                    self.toggles[name] = not self.toggles[name]
                self.toggle_view.result = self.toggles
            if "done" in interaction.data["values"]:
                self.disabled = True
                self.toggle_view.stop()
            self.options = self.get_options()
            await interaction.response.edit_message(
                view=self.view
            )

    def __init__(self, options: Union[list, dict], user: User, timeout: int = 60):
        """ Initializes the View to send and wait on """
        if isinstance(options, list):
            options = {
                option: False for option in options
            }
        self.result = options
        super().__init__(timeout=timeout)
        self.dropdown = self.Dropdown(options, user, self)
        self.add_item(self.dropdown)

    async def wait(self, msg: discord.Message = None):
        await super().wait()
        self.dropdown.disabled = True
        if msg:
            await msg.edit(view=self)

