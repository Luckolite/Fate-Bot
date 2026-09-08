"""
cogs.utility.vc_log
~~~~~~~~~~~~~~~~~~~~

A cog to log vc events to a dedicated channel

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
from time import time

import discord
from discord.ext import commands, tasks
from discord.http import DiscordServerError

from botutils import colors


class VcLog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.utils.cache("vclog")
        self.join_cd = {}
        self.leave_cd = {}
        self.move_cd = {}
        self.cooldown_cleanup_task.start()

    def cog_unload(self):
        self.cooldown_cleanup_task.cancel()

    @staticmethod
    def claim_cooldown(cache, guild_id, user_id, duration=10):
        """Claim a cooldown before yielding control to Discord."""
        current = time()
        users = cache.setdefault(guild_id, {})
        if users.get(user_id, 0) >= current:
            return False
        users[user_id] = current + duration
        return True

    @tasks.loop(minutes=1)
    async def cooldown_cleanup_task(self):
        current = time()
        for cache in (self.join_cd, self.leave_cd):
            for guild_id, users in list(cache.items()):
                cache[guild_id] = {
                    user_id: expires_at
                    for user_id, expires_at in users.items()
                    if expires_at > current
                }
                if not cache[guild_id]:
                    del cache[guild_id]

        current_bucket = int(current / 10)
        for guild_id, users in list(self.move_cd.items()):
            self.move_cd[guild_id] = {
                user_id: state
                for user_id, state in users.items()
                if state and state[0] >= current_bucket - 1
            }
            if not self.move_cd[guild_id]:
                del self.move_cd[guild_id]

    @commands.group(name="vc-log", aliases=["vclog"], description="Shows how to use the module")
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def _vclog(self, ctx):
        if not ctx.invoked_subcommand:
            e = discord.Embed(color=colors.fate)
            e.set_author(name="Vc Logger", icon_url=ctx.author.display_avatar.url)
            if ctx.guild.icon:
                e.set_thumbnail(url=ctx.guild.icon.url)
            else:
                e.set_thumbnail(url=self.bot.user.display_avatar.url)
            e.description = "Logs actions in vc to a dedicated channel"
            e.add_field(
                name="◈ Usage ◈", value=".vclog enable\n.vclog disable", inline=False
            )
            toggle = "Enabled" if ctx.guild.id in self.config else "Disabled"
            e.set_footer(text=f"Current Status: {toggle}")
            await ctx.send(embed=e)

    @_vclog.command(name="enable", description="Enables logging vc events to a channel")
    @commands.has_permissions(manage_channels=True)
    async def _enable(self, ctx):
        await ctx.send("Mention the channel I should use")
        msg = await self.bot.utils.get_message(ctx)
        if not msg.channel_mentions:
            return await ctx.send("That isn't a channel mention")
        channel = msg.channel_mentions[0]
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages:
            return await ctx.send("I don't have access to that channel")
        await ctx.send("Would you like me to delete all non vc-log messages?")
        msg = await self.bot.utils.get_message(ctx)
        keep_clean = True if "yes" in msg.content.lower() else False
        if keep_clean and not perms.manage_messages:
            return await ctx.send("I'm missing manage_message permissions in the channel")
        if keep_clean:
            await ctx.send("Aight, i'll make sure it stays clean .-.")
        self.config[ctx.guild.id] = {
            "channel": channel.id,
            "keep_clean": keep_clean
        }
        await self.config.flush()
        await ctx.send("Enabled VcLog")

    @_vclog.command(name="disable", description="Disables the module")
    @commands.has_permissions(manage_channels=True)
    async def _disable(self, ctx):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("VcLog isn't enabled")
        self.config.remove(guild_id)
        await ctx.send("Disabled VcLog")

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.guild and msg.author.id != self.bot.user.id:
            guild_id = msg.guild.id
            if guild_id in self.config and self.config[guild_id]["keep_clean"]:
                if msg.channel.id == self.config[guild_id]["channel"]:
                    if not msg.channel.permissions_for(msg.guild.me).manage_messages:
                        self.config.remove(guild_id)
                        return await self.config.flush()
                    await asyncio.sleep(20)
                    if not msg.channel.permissions_for(msg.guild.me).manage_messages:
                        return self.config.remove(guild_id)
                    ignored = (
                        discord.errors.NotFound,
                        discord.errors.Forbidden,
                        discord.errors.HTTPException
                    )
                    try:
                        await msg.delete()
                    except ignored:
                        pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not before.channel and not after.channel:
            return

        guild_id = member.guild.id
        if guild_id in self.config:
            channel = self.bot.get_channel(self.config[guild_id]["channel"])
            if not channel:
                ignored = (
                    discord.errors.HTTPException,
                    DiscordServerError
                )
                handled = (
                    discord.errors.NotFound,
                    discord.errors.Forbidden
                )
                try:
                    channel = await self.bot.fetch_channel(self.config[guild_id]["channel"])
                except ignored:
                    return
                except handled:
                    self.config.remove(guild_id)
                    return await self.config.flush()
            if not channel.permissions_for(member.guild.me).send_messages:
                self.config.remove(guild_id)
                return await self.config.flush()

            user_id = member.id
            if not before.channel:
                if self.claim_cooldown(self.join_cd, guild_id, user_id):
                    await channel.send(
                        f"<:plus:548465119462424595> **{member.display_name} joined {after.channel.name}**"
                    )
                    return

            elif not after.channel:
                if self.claim_cooldown(self.leave_cd, guild_id, user_id):
                    await channel.send(
                        f"❌ **{member.display_name} left {before.channel.name}**"
                    )
                    return

            elif before.channel.id != after.channel.id:
                now = int(time() / 10)
                if guild_id not in self.move_cd:
                    self.move_cd[guild_id] = {}
                if user_id not in self.move_cd[guild_id]:
                    self.move_cd[guild_id][user_id] = [now, 0]
                if self.move_cd[guild_id][user_id][0] == now:
                    self.move_cd[guild_id][user_id][1] += 1
                else:
                    self.move_cd[guild_id][user_id] = [now, 0]
                if self.move_cd[guild_id][user_id][1] > 2:
                    return
                return await channel.send(
                    f"🚸 **{member.display_name} moved to {after.channel.name}**"
                )

            elif before.mute is False and after.mute is True:
                return await channel.send(f"🔈 **{member.display_name} was muted**")
            elif before.mute is True and after.mute is False:
                return await channel.send(
                    f"🔊 **{member.display_name} was unmuted**"
                )
            elif before.deaf is False and after.deaf is True:
                return await channel.send(
                    f"🎧 **{member.display_name} was deafened**"
                )
            elif before.deaf is True and after.deaf is False:
                await channel.send(f"🎤 **{member.display_name} was undeafened**")
            elif not before.self_stream and after.self_stream:
                await channel.send(f"🖥 **{member.display_name} started streaming**")
            elif not before.self_video and after.self_video:
                await channel.send(f"📷 **{member.display_name} turned their camera on**")


async def setup(bot):
    await bot.add_cog(VcLog(bot), override=True)
