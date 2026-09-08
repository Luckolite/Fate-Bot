"""
cogs.misc.global_chat
~~~~~~~~~~~~~

A cog to add functionality for a channel interconnected between multiple others

:copyright: (C) 2020-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import json
import traceback
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta
from os import path
from random import choice
from typing import Optional, Union

import discord
from discord import NotFound, Forbidden
from discord.ext import commands, tasks

from botutils import get_prefixes_async, colors, Cooldown, emojis, GetChoice
from fate import Fate


class Assets:
    rules = "1. No spamming\n" \
            "2. No NSFW content of any kind\n" \
            "3. No harassment or bullying\n" \
            "4. No content that may trigger epilepsy. This includes emojis\n" \
            "5. No using bot commands in the global channel\n" \
            "6. No advertising of any kind\n" \
            "7. No absurdly long, or spam-ish names\n" \
            "8. Only speak in English\n" \
            "9. No slurs regardless of your race, identity, or orientation\n" \
            "10. Most importantly abide by discords TOS\n" \
            "**Breaking any of these rules results in being blocked from using the channel**"

    ban_hammers = [
        "https://media1.tenor.com/images/1e46ced92e2521749ca6f72602765c1a/tenor.gif?itemid=18219363"
    ]

    blocked = [
        "The council does not approve",
        "No hablo inglés",
        "Nay 🙅‍♂️",
        "Naur 😩",
        "I think the fawk not",
        "Try againnnnnnn.. next year",
        "Hold on. Call me back later, I'm already OTP",
        "Busy rn. If it's an emergency you can reach me on onlybots.com",
        "Acccess grantedn't",
        "Access denied 🙅‍♂️"
    ]

    forbidden = [
        "kys",
        "fag",
        "nigg",
        "cp",
        "porn",
        "spic",
        "retard",
        "kill yourself",
        "unalive yourself",
        "boob",
        "vagina",
        "penis",
        "orgasm",
        "tranny",
        "should die",
        "pedo",
        "loli",
        "shota",
        "dox",
        "full name",
        "discord.gg"
    ]


class GlobalChat(commands.Cog):
    """
    Attributes
    -----------
    bot : fate.Fate
    polls : Dict[int, Dict[str, List[int]]]
    cache : Dict[int, discord.Channel]
    _queue : Dict[int, Union[discord.Embed, bool, discord.Message]]
    last_id : Optional[int]
    config : Dict[str, List[int]]
    """
    def __init__(self, bot: Fate):
        self.bot = bot
        self.polls = {}
        self.cache = {}
        self.messages = []
        self.msg_cache = []
        self.msg_chunks = []
        self.names = {}
        self.name_expiry_tasks = {}
        self.cache_task = self.bot.loop.create_task(self.cache_channels())
        self.handle_queue.start()
        self._queue = []
        self.last_id = None
        self.last = False
        self.last_message = None
        self.config = {
            "blocked": [],
            "icon_blocked": [],
            "images_blocked": []
        }
        self.ignore = []
        if path.isfile("./data/gcb.json"):
            with open("./data/gcb.json") as f:
                self.config = json.load(f)
        self.cd = Cooldown(2, 5)

    async def save_blacklist(self):
        async with self.bot.utils.open("./data/gcb.json", "w") as f:
            await f.write(await self.bot.dump(self.config))

    async def cog_unload(self):
        self.handle_queue.cancel()
        pending = [self.cache_task, *self.name_expiry_tasks.values()]
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.name_expiry_tasks.clear()

    async def expire_cached_name(self, user_id: int) -> None:
        try:
            await asyncio.sleep(60 * 60 * 12)
            self.names.pop(user_id, None)
        finally:
            if self.name_expiry_tasks.get(user_id) is asyncio.current_task():
                self.name_expiry_tasks.pop(user_id, None)

    @property
    def queue(self) -> list:
        self._queue = self._queue[-5:]
        return self._queue

    @property
    def blocked(self):
        return [*self.config["blocked"], *self.bot.blocked]

    async def return_coro(self, coro):
        return await coro

    @tasks.loop(seconds=0.21)
    async def handle_queue(self):
        try:
            queued_to_send = list(self.queue)
            if len(queued_to_send) > 3 and all(
                entry[2].guild.id == queued_to_send[0][2].author.id
                for entry in queued_to_send
            ):
                self.config["blocked"].append(queued_to_send[0][2].guild.id)
                await queued_to_send[0][2].channel.send("Andddddddd blocked")
                self._queue = []
                return

            for (content, files), requires_edit, author_msg in queued_to_send:
                with suppress(ValueError, IndexError):
                    self._queue.remove([(content, files), requires_edit, author_msg])

                if author_msg.stickers:
                    content += f"\n{author_msg.stickers[0].url}"

                self.msg_cache = []
                chunk = {}
                tasks = []
                username = author_msg.author.name
                if author_msg.author.id in self.names:
                    username = self.names[author_msg.author.id]
                for guild_id, webhook in list(self.cache.items()):
                    if not webhook:
                        print(f"{guild_id} lacked a valid webhook")
                        continue
                    with suppress(AttributeError):
                        if author_msg.channel.id == webhook.channel.id and author_msg.attachments:
                            continue
                    with suppress(NotFound, Forbidden, AttributeError):
                        if webhook.channel.permissions_for(webhook.guild.me).manage_messages:
                            if not webhook.channel.permissions_for(webhook.guild.me).manage_webhooks:
                                with suppress(Exception):
                                    await webhook.channel.send("I need manage_webhook permissions to show messages from global chat")
                                continue
                            _files = deepcopy(files)
                            tasks.append(asyncio.create_task(self.return_coro(
                                webhook.send(
                                    content,
                                    embeds=author_msg.embeds,
                                    files=_files,
                                    username=username,
                                    avatar_url=author_msg.author.display_avatar.url,
                                    allowed_mentions=discord.AllowedMentions.none()
                                )
                            )))
                for task in tasks:
                    with suppress(NotFound, Forbidden, AttributeError):
                        await task
                        msg = task.result()
                        self.msg_cache.append(msg)
                        chunk[msg.channel.id] = msg.id
                self.msg_chunks.append(chunk)
                with suppress(AttributeError, NotFound, Forbidden):
                    if author_msg.attachments:
                        if author_msg.channel.permissions_for(author_msg.guild.me).add_reactions:
                            await author_msg.add_reaction("✅")
                    else:
                        if author_msg.author.id != self.bot.user.id:
                            self.bot.suppressed.append(author_msg.id)
                            await author_msg.delete()
        except:
            print(traceback.format_exc())

    async def cache_channels(self):
        try:
            if not self.bot.is_ready():
                await self.bot.wait_until_ready()
            while True:
                if not self.bot.pool:
                    await asyncio.sleep(1)
                    continue
                break
            async with self.bot.utils.cursor() as cur:
                await cur.execute("select guild_id, channel_id from global_chat;")
                ids = await cur.fetchall()
            for guild_id, channel_id in ids:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except (NotFound, Forbidden):
                    async with self.bot.utils.cursor() as cur:
                        await cur.execute(
                            "delete from global_chat where guild_id = %s;",
                            (guild_id,),
                        )
                else:
                    async with self.bot.utils.cursor() as cur:
                        await cur.execute(
                            "select * from blocked where user_id in (%s, %s);",
                            (guild_id, channel.guild.owner.id),
                        )
                        if cur.rowcount:
                            if guild_id in self.blocked:
                                self.bot.log.info(f"Removing {channel.guild} from global chat. The guild is blocked")
                            else:
                                self.bot.log.info(f"Removing {channel.guild} from global chat. The guild owner ({channel.guild.owner}) is blocked")
                            async with self.bot.utils.cursor() as cur:
                                await cur.execute(
                                    "delete from global_chat where guild_id = %s;",
                                    (guild_id,),
                                )
                            if guild_id in self.cache:
                                del self.cache[guild_id]
                            continue
                    try:
                        webhooks = await channel.webhooks()
                        for webhook in webhooks:
                            if webhook.name == "Global Chat":
                                webhook = webhook
                                break
                        else:
                            webhook = await channel.create_webhook(
                                name="Global Chat"
                            )
                        self.cache[guild_id] = webhook
                    except (NotFound, Forbidden):
                        async with self.bot.utils.cursor() as cur:
                            await cur.execute(
                                "delete from global_chat where guild_id = %s;",
                                (guild_id,),
                            )
        except Exception as e:
            print(traceback.format_exc())
            print(e)

    @commands.command(name="gc-cleanup", description="Cleans stale global-chat data")
    @commands.is_owner()
    async def clean_gc(self, ctx):
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select guild_id from global_activity where last_used < %s;",
                (datetime.now() - timedelta(days=30),),
            )
            r = await cur.fetchall()
            for guild_id, in r:
                await cur.execute(
                    "select channel_id from global_chat where guild_id = %s;",
                    (guild_id,),
                )
                if cur.rowcount:
                    r = await cur.fetchone()
                    if c := self.bot.get_channel(int(r[0])):
                        with suppress(Exception):
                            await c.send("Disabled global chat due to inactivity")
                            await ctx.send(f"sent msg into {c.guild}")
                await cur.execute(
                    "delete from global_chat where guild_id = %s;", (guild_id,)
                )
                await cur.execute(
                    "delete from global_activity where guild_id = %s;", (guild_id,)
                )
                if guild_id in self.cache:
                    del self.cache[guild_id]

        await ctx.send("done")

    @commands.group(
        name="gc",
        aliases=["global-chat", "globalchat", "global_chat"],
        description="Manages and uses the global chat network",
    )
    @commands.guild_only()
    async def _gc(self, ctx):
        if not ctx.invoked_subcommand:
            e = discord.Embed(color=self.bot.config["theme_color"])
            e.set_author(name="Global Chat", icon_url=self.bot.user.display_avatar.url)
            e.description = "Link a channel into my global channel. " \
                            "Msgs sent into it will be forwarded to other " \
                            "configured channels alongside the same in reverse"
            p = await get_prefixes_async(self.bot, ctx.message)
            p = p[2]  # type: str
            e.add_field(
                name="Usage",
                value=f"{p}gc enable\n"
                      f"{p}gc disable\n"
                      f"{p}gc rules\n"
                      f"{p}gc servers\n"
                      f"{p}gc poll [your question]",
                inline=False
            )
            async with self.bot.utils.cursor() as cur:
                await cur.execute("select channel_id from global_chat;")
                channel_count = cur.rowcount
                await cur.execute("select user_id from global_users;")
                user_count = cur.rowcount
            ban_count = len(self.config["blocked"])
            e.set_footer(text=f"{channel_count} Channels | {user_count} Users | {ban_count} Bans")
            await ctx.send(embed=e)

    @_gc.command(name="servers", description="Lists servers connected to global chat")
    async def _servers(self, ctx):
        e = discord.Embed()
        e.description = ""
        for guild_id in self.cache.keys():
            if g := self.bot.get_guild(guild_id):
                e.description += f"\n• {g}"
        await ctx.send(embed=e)

    @_gc.command(name="mod", description="Grants global-chat moderator access")
    @commands.is_owner()
    async def _mod(self, ctx, user: discord.User):
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select status from global_users "
                "where user_id = %s and status = 'moderator';",
                (user.id,),
            )
            if cur.rowcount:
                await cur.execute(
                    "update global_users set status = 'verified' where user_id = %s;",
                    (user.id,),
                )
                await ctx.send(f"Removed {user} as a mod")
            else:
                await cur.execute(
                    "insert into global_users values (%s, 'moderator') "
                    "on duplicate key update status = 'moderator';",
                    (user.id,),
                )
                await ctx.send(f"Added {user} as a mod")

    @_gc.command(name="ban", description="Bans a user or server from global chat")
    async def _ban(self, ctx, target: Optional[Union[discord.User, discord.Guild]], *, reason = None):
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select status from global_users "
                "where user_id = %s and status = 'moderator';",
                (ctx.author.id,),
            )
            if not cur.rowcount:
                return await ctx.send("Only global chat moderators can use this command")
        if not target:
            choice = await GetChoice(
                ctx=ctx,
                choices={v: str(k) for k, v in self.names.items()},
                placeholder="Choose who to ban"
            )
            target = self.bot.get_user(int(choice))
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select status from global_users "
                "where user_id = %s and status = 'moderator';",
                (target.id,),
            )
            if cur.rowcount:
                return await ctx.send("You can't ban global chat moderators")
        if target.id in self.config["blocked"]:
            return await ctx.send(f"{target} is already blocked")
        self.config["blocked"].append(target.id)
        await ctx.send(f"Blocked {target}")
        await self.save_blacklist()
        self.bot.log.info(f"{target} was banned from global chat for `{reason}`")
        self._queue.append([(f"{target} was banned from global-chat for `{reason}`", []), False, ctx.message])
        await self.cache_channels()

        # Forward the change into each global chat channel
        # e = discord.Embed(color=colors.red)
        # if isinstance(target, discord.User):
        #     icon_url = target.display_avatar.url
        # else:
        #     icon_url = target.icon.url
        # e.set_author(name=f"{target} was banned", icon_url=icon_url)
        # e.description = reason
        # self._queue.append([e, False, None])
        # self.last_id = None

    @_gc.command(name="unban", description="Unbans a user or server from global chat")
    async def _unban(self, ctx, *, target: Union[discord.User, discord.Guild]):
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select status from global_users "
                "where user_id = %s and status = 'moderator';",
                (ctx.author.id,),
            )
            if not cur.rowcount:
                return await ctx.send("Only global chat moderators can use this command")
        if target.id not in self.config["blocked"]:
            return await ctx.send(f"{target} isn't blocked")
        self.config["blocked"].remove(target.id)
        await ctx.send(f"Unblocked {target}")
        await self.save_blacklist()

    @_gc.command(name="ban-icon", description="Blocks a server icon in global chat")
    async def _ban_icon(self, ctx, guild_id: int):
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select status from global_users "
                "where user_id = %s and status = 'moderator';",
                (ctx.author.id,),
            )
            if not cur.rowcount:
                return await ctx.send("Only global chat moderators can use this command")
        if guild_id in self.config["icon_blocked"]:
            return await ctx.send("That servers icon is already blacklisted")
        self.config["icon_blocked"].append(guild_id)
        await ctx.send(f"Blacklisted {self.bot.get_guild(guild_id)}s icon")
        await self.save_blacklist()

    @_gc.command(name="unban-icon", description="Unblocks a server icon in global chat")
    async def _unban_icon(self, ctx, guild_id: int):
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select status from global_users "
                "where user_id = %s and status = 'moderator';",
                (ctx.author.id,),
            )
            if not cur.rowcount:
                return await ctx.send("Only global chat moderators can use this command")
        if guild_id not in self.config["icon_blocked"]:
            return await ctx.send("That servers icon isn't blacklisted")
        self.config["icon_blocked"].remove(guild_id)
        await ctx.send(f"Un-blacklisted {self.bot.get_guild(guild_id)}s icon")
        await self.save_blacklist()

    @_gc.command(name="ban-images", description="Blocks global-chat image access")
    async def _ban_images(self, ctx, target: Union[discord.User, discord.Guild]):
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select status from global_users "
                "where user_id = %s and status = 'moderator';",
                (ctx.author.id,),
            )
            if not cur.rowcount:
                return await ctx.send("Only global chat moderators can use this command")
        if target.id in self.config["images_blocked"]:
            return await ctx.send(f"{target} is already blacklisted")
        self.config["images_blocked"].append(target.id)
        await ctx.send(f"Blacklisted images from {target}")
        await self.save_blacklist()

    @_gc.command(name="unban-images", description="Restores global-chat image access")
    async def _unban_images(self, ctx, target: Union[discord.User, discord.Guild]):
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select status from global_users "
                "where user_id = %s and status = 'moderator';",
                (ctx.author.id,),
            )
            if not cur.rowcount:
                return await ctx.send("Only global chat moderators can use this command")
        if target.id not in self.config["images_blocked"]:
            return await ctx.send(f"{target} isn't blacklisted")
        self.config["images_blocked"].remove(target.id)
        await ctx.send(f"Un-blacklisted images from {target}")
        await self.save_blacklist()

    @_gc.command(name="rules", description="Shows the global-chat rules")
    async def rules(self, ctx):
        e = discord.Embed(color=self.bot.config["theme_color"])
        e.description = Assets.rules
        await ctx.send(embed=e)

    @_gc.command(name="enable", description="Enables global chat in this server")
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_webhooks=True, attach_files=True, embed_links=True)
    async def _enable(self, ctx):
        if ctx.author.id in self.blocked:
            self.bot.log.info(f"Prevented {ctx.author} from enabling global chat. They are blocked")
            return await ctx.send(choice(Assets.blocked))
        if ctx.guild.owner.id in self.blocked:
            self.bot.log.info(f"Prevented {ctx.guild} from enabling global chat. The guild is blocked")
            return await ctx.send(choice(Assets.blocked))
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "insert into global_chat values (%s, %s) "
                "on duplicate key update channel_id = %s;",
                (ctx.guild.id, ctx.channel.id, ctx.channel.id),
            )

        self.cache[ctx.guild.id] = await ctx.channel.create_webhook(name="Global Chat")
        await ctx.send("Enabled global chat")

    @_gc.command(name="disable", description="Disables global chat in this server")
    @commands.has_permissions(administrator=True)
    async def _disable(self, ctx):
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select * from global_chat where guild_id = %s;", (ctx.guild.id,)
            )
            if not cur.rowcount:
                return await ctx.send("Global chat isn't enabled")
            if ctx.guild.id in self.cache:
                del self.cache[ctx.guild.id]
            await cur.execute(
                "delete from global_chat where guild_id = %s;", (ctx.guild.id,)
            )
        await ctx.send("Disabled global chat")

    @_gc.command(name="verify", description="Verifies a server for global chat")
    @commands.cooldown(1, 60, commands.BucketType.user)
    @commands.cooldown(3, 60, commands.BucketType.guild)
    @commands.guild_only()
    async def verify(self, ctx):
        if ctx.author.id in self.config["blocked"]:
            return await ctx.send(choice(Assets.blocked))

        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select status from global_users where user_id = %s;",
                (ctx.author.id,),
            )
            if cur.rowcount:
                return await ctx.send("You're already registered")
        channel = self.bot.get_channel(self.bot.config["gc_verify_channel"])
        async for msg in channel.history(limit=15):  # type: ignore
            if not msg.content:
                for embed in msg.embeds:
                    if str(ctx.author.id) in str(embed.to_dict()):
                        return await ctx.send("You already have an application waiting")

        await ctx.send(
            "Are you aware that the global-chat channel is independent of, and has nothing "
            "to do with the server you're using it in? Reply with `yes` to confirm you understand. "
            "You can reply with `cancel`, or anything else to stop the verification process"
        )
        reply = await self.bot.utils.get_message(ctx)
        if "yes" not in reply.content.lower():
            return await ctx.send("Alright, stopped the verification process. You can redo at any point in time")

        await ctx.send("What's your reason for wanting access to global chat. Send `cancel` to stop the process")
        reason = await self.bot.utils.get_message(ctx)
        if "cancel" in reason.content:
            with suppress(Forbidden, NotFound):
                await reason.add_reaction("👍")
            return
        if not reason.content:
            return await ctx.send("That's not a valid response. Rerun the command")
        e = discord.Embed(color=self.bot.config["theme_color"])
        e.description = Assets.rules
        msg = await ctx.send(
            "Do you agree to **all** of the stated rules in this embed?",
            embed=e
        )
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")
        reaction, _user = await self.bot.utils.get_reaction(ctx)
        if reaction.message.id != msg.id:
            return await ctx.send("Why.. would you do this. Rerun the cmd")
        if str(reaction.emoji) != "👍":
            return await ctx.send("Ok")

        e = discord.Embed(color=ctx.author.color)
        e.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
        age = discord.utils.format_dt(ctx.author.created_at, style="R")
        joined = discord.utils.format_dt(ctx.author.joined_at, style="R")
        e.description = f"🆔 | {ctx.author.id}\n" \
                        f"📬 | {len(ctx.author.mutual_guilds)} Mutual Servers\n" \
                        f"⏰ | Created {age}\n" \
                        f"⏱ | Joined {joined}"
        e.add_field(name="Reason", value=reason.content)
        e.set_footer(text=ctx.guild.name, icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
        msg = await channel.send(embed=e)  # type: ignore
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")
        await ctx.send("Sent your application")

    @_gc.command(name="poll", description="Creates a poll in global chat")
    @commands.cooldown(1, 120, commands.BucketType.channel)
    async def poll(self, ctx, *, poll):
        active = [m.channel.id for m in list(self.cache.values()) if m]
        if ctx.channel.id not in active:
            return await ctx.send("Global chat isn't active")
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select status from global_users where user_id = %s "
                "and status in ('verified', 'moderator');",
                (ctx.author.id,),
            )
            if not cur.rowcount:
                return await ctx.send("You aren't verified into global chat")
        e = discord.Embed()
        e.set_author(name=f"Poll by {ctx.author} 📊", icon_url=ctx.author.display_avatar.url)
        e.description = poll
        e.set_footer(text="👍 0 | 👎 0")
        self.polls[ctx.author.id] = {
            "messages": [],
            "👍": [],
            "👎": []
        }
        try:
            for guild_id, webhook in list(self.cache.items()):
                with suppress(NotFound, Forbidden):
                    msg = await webhook.channel.send(embed=e)
                    await msg.add_reaction("👍")
                    await msg.add_reaction("👎")
                    self.polls[ctx.author.id]["messages"].append(msg)
            self.last_id = None
        except Exception as e:
            await ctx.send(e)
        with suppress(NotFound, Forbidden):
            await ctx.message.delete()

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        if not user.bot and reaction.emoji in ["👍", "👎"]:
            active = [m.channel.id for m in list(self.cache.values()) if m and m.channel]
            if reaction.message.channel.id in active:
                for user_id, data in list(self.polls.items()):
                    await asyncio.sleep(0)
                    if any(reaction.message.id == m.id for m in data["messages"] if m):
                        if user.id not in data[reaction.emoji]:
                            self.polls[user_id][reaction.emoji].append(user.id)
                            emoji = "👍" if reaction.emoji == "👎" else "👎"
                            if user.id in data[emoji]:
                                self.polls[user_id][emoji].remove(user.id)
                            if not self.last:
                                self.last = True
                                await asyncio.sleep(3)
                                self.last = False
                                data = self.polls[user_id]
                                for message in [m for m in data["messages"] if m and m.embeds]:
                                    with suppress(Exception):
                                        e = message.embeds[0]
                                        e.set_footer(text=f"👍 {len(data['👍'])} | 👎 {len(data['👎'])}")
                                        await message.edit(embed=e)

                        with suppress(Exception):
                            await reaction.message.remove_reaction(reaction.emoji, user)

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        try:
            if msg.content.startswith("."):
                return
            if len(msg.content) == 1 and msg.content.upper() != msg.content.lower():
                return
            active = [m.channel.id for m in list(self.cache.values()) if m and m.channel]
            if not msg.author.bot and msg.channel.id in active:
                await asyncio.sleep(0.21)
                if msg.author.id in self.ignore:
                    if msg.channel.permissions_for(msg.guild.me).add_reactions:
                        await msg.add_reaction("⏳")
                    return
                if self.cd.check(msg.author.id):
                    self.ignore.append(msg.author.id)
                    if msg.channel.permissions_for(msg.guild.me).add_reactions:
                        await msg.add_reaction("⏳")
                    await asyncio.sleep(25)
                    self.ignore.remove(msg.author.id)
                    return

                if not msg.channel.permissions_for(msg.guild.me).manage_webhooks:
                    async with self.bot.utils.cursor() as cur:
                        await cur.execute(
                            "delete from global_chat where guild_id = %s;",
                            (msg.guild.id,),
                        )
                    return await msg.channel.send(
                        "I need manage_webhooks permission for global chat to function. "
                        "You'll have to re-enable it whence I'm given the permission"
                    )

                # Duplicate messages
                # if msg.content and any(msg.content == m.content for m in self.msg_cache):
                #     return
                if msg.guild.id in self.config["blocked"] or msg.author.id in self.config["blocked"]:
                    return

                # Missing permissions to moderate global chat
                perms = msg.channel.permissions_for(msg.guild.me)
                if not perms.send_messages or not perms.embed_links or not perms.manage_messages:
                    async with self.bot.utils.cursor() as cur:
                        await cur.execute(
                            "delete from global_chat where guild_id = %s;",
                            (msg.guild.id,),
                        )
                    del self.cache[msg.guild.id]
                    with suppress(Exception):
                        return await msg.channel.send(
                            "Disabled global chat due to missing permissions"
                        )

                async with self.bot.utils.cursor() as cur:
                    await cur.execute(
                        "select status from global_users where user_id = %s;",
                        (msg.author.id,),
                    )
                    if not cur.rowcount:
                        return await msg.channel.send("You're not verified into using this channel. Run `.gc verify` in a different channel")
                    await cur.execute(
                        "select status from global_users "
                        "where user_id = %s and status = 'blocked';",
                        (msg.author.id,),
                    )
                    if cur.rowcount:
                        return await msg.channel.send("You're blocked from using global chat")
                    await cur.execute(
                        "select status from global_users "
                        "where user_id = %s and status = 'moderator';",
                        (msg.author.id,),
                    )
                    if msg.author.id not in self.names:
                        user_id = msg.author.id
                        self.names[user_id] = str(msg.author.name)
                        task = self.bot.loop.create_task(
                            self.expire_cached_name(user_id)
                        )
                        self.name_expiry_tasks[user_id] = task

                    # Update the last use for this server
                    used_at = datetime.now()
                    await cur.execute(
                        "insert into global_activity values (%s, %s) "
                        "on duplicate key update last_used = %s;",
                        (msg.guild.id, used_at, used_at),
                    )

                msg.embeds = []
                if msg.reference:
                    e = discord.Embed()
                    m = msg.reference.cached_message
                    if not m:
                        m = await msg.channel.fetch_message(msg.reference.message_id)
                    e.set_author(name=f"Replying to @{m.author.name}", icon_url=m.author.display_avatar.url)
                    if m.content:
                        old_content = "\n".join("> " + line for line in m.content.split("\n"))
                        e.description = f"> {emojis.reply} *[{old_content.lstrip('> ')}]({m.jump_url})*\n" \
                                        f"{emojis.reply} {msg.content[:self.bot.content_limit]}"
                    else:
                        reply = None
                        if m.embeds and m.embeds[0].author:
                            em: discord.Embed = m.embeds[0]
                            if em.author.name.startswith("Replying to"):
                                # e.set_author(name=em.author.name, icon_url=em.author.icon_url)
                                reply = "> [" + "\n".join(em.description.splitlines()[1:]) + f"]({m.jump_url})"
                        if m.embeds:
                            text = f"> **Replying to [EMBED]({m.jump_url})**"
                        elif m.attachments:
                            text = f"> **Replying to [ATTACHMENT]({m.jump_url})**"
                        else:
                            text = f"> **Replying to [@{m.author.name}]({m.jump_url})**"

                        e.description = f"{reply or text}\n{emojis.reply}{msg.content[:self.bot.content_limit]}"
                    msg.content = ""
                    # e.set_thumbnail(url=m.author.display_avatar.url)
                    # if m.content:
                    #     old_content = "\n".join("> " + line for line in m.content.split("\n"))
                    #     e.description = f"> **Replying to [@{m.author.name}]({m.jump_url})**\n" \
                    #                     f"> {emojis.reply} *{old_content.lstrip('> ')}*\n" \
                    #                     f"↳ {msg.content[:self.bot.content_limit]}"
                    # else:
                    #     e.description = f"> **Replying to [@{m.author.name}]({m.jump_url})**" \
                    #                     f"\n{emojis.reply}{msg.content[:self.bot.content_limit]}"
                    msg.content = ""
                    msg.embeds.append(e)

                if msg.content.count("@") > 2:
                    self.ignore.append(msg.author.id)
                    await msg.channel.send(
                        "You've been temporarily muted from global-chat for 15mins for sending too many @'s",
                        reference=msg
                    )
                    await asyncio.sleep(900)
                    self.ignore.remove(msg.author.id)
                    return
                lowered_content = msg.content.lower()
                if any(
                    lowered_content.startswith(phrase) or lowered_content.endswith(phrase)
                    for phrase in Assets.forbidden
                ):
                    self.ignore.append(msg.author.id)
                    await msg.channel.send(
                        "You've been temporarily muted from global-chat for 15mins for sending a filtered word",
                        reference=msg
                    )
                    await asyncio.sleep(900)
                    self.ignore.remove(msg.author.id)
                    return
                if msg.content and msg.content == self.last_message:
                    self.ignore.append(msg.author.id)
                    await msg.channel.send(
                        "You've been temporarily muted from global-chat for 15mins for repeating the last message",
                        reference=msg
                    )
                    await asyncio.sleep(900)
                    self.ignore.remove(msg.author.id)
                    return
                self.last_message = str(msg.content)
                if "chat" in msg.content:
                    if "died" in msg.content or "dead" in msg.content:
                        return await msg.add_reaction("🤦‍♂️")
                if msg.content and len(msg.content) > 5 and any(msg.content == old_msg.content for old_msg in self.messages[-5:]):
                    self.ignore.append(msg.author.id)
                    await msg.channel.send(
                        "You've been temporarily muted from global-chat for 15mins for trying to send a duplicate message",
                        reference=msg
                    )
                    await asyncio.sleep(900)
                    self.ignore.remove(msg.author.id)
                    return

                # Convert mentions to nicknames so everyone can read them
                ctx = await self.bot.get_context(msg)
                converter = commands.clean_content(use_nicknames=True)
                msg.content = await converter.convert(ctx, msg.content)
                lowered_content = msg.content.lower()

                # Filter from special blacklist
                word_filter = ["loli", "slut", "rape"]
                for word in word_filter:
                    if word in lowered_content:
                        return

                # Send a new msg
                files = []
                if msg.author.id not in self.config["images_blocked"]:
                    if msg.guild.id not in self.config["images_blocked"]:
                        files = await asyncio.gather(
                            *(attachment.to_file() for attachment in msg.attachments)
                        )
                # if msg.stickers:
                #     e.set_image(url=msg.stickers[0].url)
                self._queue.append([(msg.content, files), False, msg])
                self.messages = [*self.messages[-20:], msg]
        except:
            print(traceback.format_exc())

    @commands.Cog.listener("on_raw_reaction_add")
    async def on_message_delete(self, payload):
        if payload.channel_id != 709035348629520425 or payload.emoji != "🚩":
            return
        channel = self.bot.get_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        if msg.author.bot:
            for webhook in list(self.cache.values()):
                with suppress(Exception):
                    async for m in webhook.channel.history(limit=50):
                        if msg.content and m.content == msg.content:
                            await m.delete()
                        if msg.attachments and m.attachments[0].size == msg.attachments[0].size:
                            await m.delete()
                        if msg.embeds and m.embeds and msg.embeds[0] == m.embeds[0]:
                            await m.delete()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.channel_id == self.bot.config["gc_verify_channel"]:
            if payload.user_id == self.bot.user.id:
                return
            channel = self.bot.get_channel(payload.channel_id)
            msg = await channel.fetch_message(payload.message_id)  # type: ignore
            if not msg.embeds or "🆔" not in msg.embeds[0].description:
                return
            user_id = int(msg.embeds[0].description.split("\n")[0].split(" | ")[1])

            user = await self.bot.fetch_user(user_id)
            u = self.bot.get_user(payload.user_id)
            e = discord.Embed(color=colors.green)
            if str(payload.emoji) == "👍":
                async with self.bot.utils.cursor() as cur:
                    await cur.execute(
                        "insert into global_users values (%s, 'verified');",
                        (user_id,),
                    )
                e.set_author(name=f"{user} was verified", icon_url=user.display_avatar.url)

                self._queue.append([(f"{user} was verified", []), False, msg])
                await msg.edit(content=f"Application accepted by {u}")
            else:
                with suppress(NotFound, Forbidden):
                    await user.send("Your verification into global-chat was denied.")
                await msg.edit(content=f"Application denied by {u}")
            await msg.clear_reactions()


async def setup(bot):
    await bot.add_cog(GlobalChat(bot), override=True)
