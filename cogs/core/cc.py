"""
cogs.core.cc
~~~~~~~~~~~~~

A module for configuring per-server custom commands

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import random
from contextlib import suppress
from time import time
from typing import *

from discord import Message, Embed, AllowedMentions, ui, utils, Interaction, Guild, Thread
from discord import NotFound, Forbidden
from discord.ext import commands, tasks

from botutils import get_prefixes_async, GetChoice, Cooldown
from fate import Fate

m = AllowedMentions.none()


class CustomCommands(commands.Cog):
    """ Cog class for handling custom commands """
    cache: Dict[int, Dict[str, List[Union[Optional[str], float]]]] = {}
    guilds: List[int] = []

    def __init__(self, bot: Fate) -> None:
        self.bot = bot
        self.cd = Cooldown(1, 10)
        self.cleanup_task.start()

    async def cog_load(self) -> None:
        if self.bot.is_ready():
            await self.on_ready()

    async def cog_unload(self) -> None:
        self.cleanup_task.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """ Index the servers with custom commands enabled """
        async with self.bot.utils.cursor() as cur:
            await cur.execute("select guild_id from cc;")
            for (guild_id,) in set(await cur.fetchall()):
                if guild_id not in self.guilds:
                    self.guilds.append(guild_id)

    @tasks.loop(minutes=1)
    async def cleanup_task(self) -> None:
        """ Uncache custom commands that haven't been used in awhile """
        for guild_id, custom_commands in list(self.cache.items()):
            for command, (_resp, cached_at) in list(custom_commands.items()):
                await asyncio.sleep(0)
                if time() - cached_at > 60 * 10:
                    del self.cache[guild_id][command]
            if not self.cache[guild_id]:
                del self.cache[guild_id]

    @commands.group(name="cc", description="Shows help for custom commands")
    @commands.guild_only()
    @commands.cooldown(2, 5, commands.BucketType.channel)
    async def cc(self, ctx) -> None:
        """ Shows help for custom commands """
        if not ctx.invoked_subcommand:
            e = Embed(color=self.bot.config["theme_color"])
            e.set_author(name="Custom Commands", icon_url=self.bot.user.display_avatar.url)
            e.description = "Create commands with custom responses"
            p: str = ctx.prefix
            e.add_field(
                name="◈ Usage",
                value=f"{p}cc add [command] [the cmds response]\n"
                      f"{p}cc remove [command, or pick which after running the command]"
            )

            # Add a button for viewing the current custom commands
            view = None
            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    "select command, response from cc where guild_id = %s;",
                    (ctx.guild.id,),
                )
                if cur.rowcount:
                    custom_commands = [(c, r) for c, r in await cur.fetchall()]
                    view = View(self.bot, custom_commands)  # type: ignore

            msg = await ctx.send(embed=e, view=view)
            if view:
                await view.wait()
                view.button.disabled = True
                await msg.edit(view=view)

    @cc.group(name="add", description="Creates a custom command")
    async def add(self, ctx, command, *, response) -> Optional[Message]:
        """ Creates a custom command """
        if not self.bot.attrs.is_moderator(ctx.author):
            return await ctx.send("You need to be a moderator to manage custom commands")
        if not response:
            return await ctx.send(f"You need to include a response for when the command's ran")
        if len(command) > 16:
            return await ctx.send("Command names can't be longer than 16 characters")
        if not all(c.lower() != c.upper() for c in command):
            return await ctx.send("Commands can only have abc characters")
        if self.bot.get_command(command):
            return await ctx.send(f"Command `{command}` already exists")

        # Configure options
        # convo = Conversation(ctx=ctx, delay=2)
        # reply = await convo.ask(
        #     "Should I delete the initial command after its been used?",
        #     use_buttons=True
        # )
        # if reply == "yes":
        #     ...
        # await convo.end()

        command = command.lower()
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select * from cc where guild_id = %s and command = %s;",
                (ctx.guild.id, command),
            )
            if cur.rowcount:
                return await ctx.send(
                    f"There's already a registered command under that. "
                    f"You can remove it via `{ctx.prefix}cc remove {command}`"
                )
            await cur.execute(
                "insert into cc values (%s, %s, %s);",
                (ctx.guild.id, command, response),
            )
        if ctx.guild.id not in self.guilds:
            self.guilds.append(ctx.guild.id)
        if ctx.guild.id in self.cache:
            del self.cache[ctx.guild.id]
        await ctx.send(f"Added {command} as a custom command")

    @cc.command(name="remove", description="Deletes a custom command")
    async def remove(self, ctx, command = None) -> Optional[Message]:
        """ Deletes a custom command """
        if not self.bot.attrs.is_moderator(ctx.author):
            return await ctx.send("You need to be a moderator to manage custom commands")
        async with self.bot.utils.cursor() as cur:
            if not command:
                await cur.execute(
                    "select command from cc where guild_id = %s;", (ctx.guild.id,)
                )
                if not cur.rowcount:
                    return await ctx.send("This server has no custom commands")
                results = [result[0] for result in await cur.fetchall()]
                choices = await GetChoice(ctx, results, limit=len(results))
                for choice in choices:
                    await cur.execute(
                        "delete from cc where guild_id = %s and command = %s;",
                        (ctx.guild.id, choice),
                    )
                    await ctx.send(f"Removed `{choice}`")
            else:
                await cur.execute(
                    "select * from cc where guild_id = %s and command = %s;",
                    (ctx.guild.id, command),
                )
                if not cur.rowcount:
                    return await ctx.send(f"`{command}` isn't registered as a custom command")
                await cur.execute(
                    "delete from cc where guild_id = %s and command = %s;",
                    (ctx.guild.id, command),
                )
                await ctx.send(f"Removed `{command}`")
        if ctx.guild.id in self.cache:
            del self.cache[ctx.guild.id]

    def ensure_cached(self, guild: Guild) -> None:
        """ Shortcut for ensuring the guild_id key exists """
        if guild.id not in self.cache:
            self.cache[guild.id] = {}

    async def process_command(self, msg: Message, command: str, response: str) -> None:
        """ Handles the cooldowns and sends the response """
        if isinstance(msg.channel, Thread):
            msg.channel = msg.channel.parent
        if self.cd.check(msg.guild.id) or self.cd.check(msg.author.id):
            if msg.channel.permissions_for(msg.guild.me).add_reactions:
                with suppress(Exception):
                    await msg.add_reaction("⏳")
            return
        self.ensure_cached(msg.guild)
        self.cache[msg.guild.id][command][1] = time()
        if msg.channel.permissions_for(msg.guild.me).send_messages:
            # Check whether to use random responses
            if (lines := response.split("\n")) and len(lines) > 1:
                if all("https://" in line for line in lines):
                    response = random.choice(lines)
            await msg.channel.send(response)

    @commands.Cog.listener()
    async def on_message(self, msg: Message) -> None:
        """ Check for custom commands, cache them, and process the command """
        if not msg.guild or not msg.guild.owner:
            return
        if msg.guild.id not in self.guilds:
            return
        if len(msg.content.split()) > 1:
            return
        p: list = await get_prefixes_async(self.bot, msg)
        if not msg.content.startswith(p[2]):
            return

        command: str = msg.content.lstrip(p[2]).lower()  # Remove the prefix
        if self.bot.get_command(command):
            return

        self.ensure_cached(msg.guild)
        if command in self.cache[msg.guild.id]:
            response = self.cache[msg.guild.id][command][0]
            if response:
                return await self.process_command(msg, command, response)

        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select response from cc where guild_id = %s "
                "and command = %s limit 1;",
                (msg.guild.id, command),
            )
            if cur.rowcount:
                response, *_ = await cur.fetchone()  # type: str
                self.ensure_cached(msg.guild)
                self.cache[msg.guild.id][command] = [response, time()]
                if response:
                    await self.process_command(msg, command, response)
            else:
                self.ensure_cached(msg.guild)
                self.cache[msg.guild.id][command] = [None, time()]


class View(ui.View):
    """ Creates a button to view the existing custom commands """
    def __init__(self, bot, custom_commands: List[Tuple[str]]) -> None:
        self.bot = bot
        self.cd = Cooldown(1, 45)

        # Format the button response
        if len(custom_commands) > 32:
            self.commands = ", ".join(f"`{c}`" for c in custom_commands)
        else:
            self.commands = ""
            for command, response in custom_commands:
                if response:
                    response = utils.escape_markdown(response[:100])\
                        .replace("\n", " ")\
                        .replace("https://", "")\
                        .replace("http://", "")\
                        .replace("www.", "")
                self.commands += f"🗨 **{command}:**\n*{response}*\n\n"

        super().__init__(timeout=45)

    async def on_error(
        self, _interaction: Interaction, error: Exception, _item: ui.Item
    ) -> None:
        """ Ignores interaction failed exceptions """
        if not isinstance(error, (NotFound, Forbidden)):
            raise error

    @ui.button(label="Show Commands")
    async def button(self, interaction: Interaction, _button) -> None:
        """ Processes interactions """
        if self.cd.check(interaction.user.id):
            return
        await interaction.response.send_message(self.commands, ephemeral=True)


async def setup(bot: Fate):
    await bot.add_cog(CustomCommands(bot))
