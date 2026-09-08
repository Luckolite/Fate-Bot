"""
cogs.moderation.chatlock
~~~~~~~~~~~~~~~~~~~~~~~~~

A cog for locking a channel from users sending messages

:copyright: (C) 2020-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import json
from os.path import isfile

import discord
from discord.ext import commands

from botutils import colors
from fate import Fate


class ChatLock(commands.Cog):
    path = "./data/userdata/chatlock.json"

    def __init__(self, bot: Fate):
        self.bot = bot
        self.toggle: dict[str, list[int]] = {}
        if isfile(self.path):
            with open(self.path, encoding="utf-8") as file:
                self.toggle = json.load(file)

    async def save_data(self) -> None:
        await self.bot.utils.save_json(self.path, self.toggle)

    def is_enabled(self, guild_id: int) -> bool:
        return str(guild_id) in self.toggle

    def channel_names(self, guild_id: str) -> str:
        channels = (
            self.bot.get_channel(channel_id)
            for channel_id in self.toggle.get(guild_id, [])
        )
        return ", ".join(channel.name for channel in channels if channel)

    @commands.group(
        name="chatlock",
        description="Deletes messages by users without the manage_messages permission",
    )
    @commands.cooldown(1, 3, commands.BucketType.channel)
    @commands.bot_has_permissions(embed_links=True)
    async def _chatlock(self, ctx: commands.Context):
        if ctx.invoked_subcommand:
            return

        guild_id = str(ctx.guild.id)
        channel_names = self.channel_names(guild_id)

        embed = discord.Embed(color=colors.fate)
        embed.set_author(name="| Chatlock", icon_url=ctx.author.display_avatar.url)
        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)
        embed.description = "Deletes messages by users without the manage_messages permission"
        if channel_names:
            embed.set_footer(text=f"| Toggle: enabled | Channels: {channel_names}")
        await ctx.send(embed=embed)

    @_chatlock.command(name="enable", description="Enables chatlock in this channel")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def _enable(self, ctx: commands.Context):
        guild_id = str(ctx.guild.id)
        channels = self.toggle.setdefault(guild_id, [])
        if ctx.channel.id in channels:
            return await ctx.send("Chatlock is already enabled")

        channels.append(ctx.channel.id)
        await ctx.message.add_reaction("👍")
        await self.save_data()

    @_chatlock.command(name="disable", description="Disables chatlock in this channel")
    @commands.has_permissions(manage_messages=True)
    async def _disable(self, ctx: commands.Context):
        guild_id = str(ctx.guild.id)
        channels = self.toggle.get(guild_id)
        if not channels:
            return await ctx.send("Chatlock isn't enabled")
        if ctx.channel.id not in channels:
            return await ctx.send("Chatlock isn't enabled in this channel")

        channels.remove(ctx.channel.id)
        if not channels:
            del self.toggle[guild_id]
        await ctx.send("Disabled chatlock")
        await self.save_data()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return

        channels = self.toggle.get(str(message.guild.id), [])
        if message.channel.id not in channels:
            return
        if message.author.guild_permissions.manage_messages:
            return

        await message.delete()


async def setup(bot: Fate) -> None:
    await bot.add_cog(ChatLock(bot), override=True)
