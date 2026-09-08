"""
cogs.core.config
~~~~~~~~~~~~~~~~~

Commands for displaying and editing server configuration.

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
from typing import Optional

import discord
from discord.ext import commands

from botutils import colors, emojis, get_prefixes_async


class Config(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(
        name="config",
        aliases=["conf"],
        description="Shows and manages the server configuration",
    )
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.bot_has_permissions(embed_links=True)
    async def _config(self, ctx):
        if ctx.invoked_subcommand:
            return

        embed = discord.Embed(color=colors.fate)
        embed.set_author(
            name="Server Config", icon_url=ctx.guild.owner.display_avatar.url
        )
        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)
        prefixes = await get_prefixes_async(self.bot, ctx.message)
        embed.description = f"**Prefix:** [`{prefixes[2]}`]\n"

        module_lines = []
        for name, cog in self.bot.cogs.items():
            await asyncio.sleep(0)
            if not hasattr(cog, "is_enabled"):
                continue
            enabled = cog.is_enabled(ctx.guild.id)
            if hasattr(enabled, "__await__"):
                enabled = await enabled
            module_lines.append(
                f"{emojis.online if enabled else emojis.dnd} **{name}**"
            )

        module_chunks = []
        current_chunk = ""
        for line in module_lines:
            candidate = f"{current_chunk}\n{line}" if current_chunk else line
            if len(candidate) > 1000:
                module_chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk = candidate
        if current_chunk or not module_chunks:
            module_chunks.append(current_chunk or "None")
        for index, chunk in enumerate(module_chunks):
            embed.add_field(
                name="Modules" if index == 0 else "Modules (continued)",
                value=chunk,
                inline=False,
            )
        embed.add_field(
            name="Editable Configs",
            value=f"{prefixes[2]}config warns [true|false]",
            inline=False,
        )
        await ctx.send(embed=embed)

    @_config.command(
        name="warns",
        description="Shows or changes whether warnings expire after 30 days",
    )
    @commands.has_permissions(manage_guild=True)
    async def _warns(self, ctx, expire: Optional[bool] = None):
        moderation = self.bot.get_cog("Moderation")
        if moderation is None:
            return await ctx.send("The Moderation module is currently unavailable.")

        guild_id = str(ctx.guild.id)
        guild_config = moderation.config.setdefault(guild_id, moderation.template)
        warn_config = guild_config.get("warns_config")
        if not isinstance(warn_config, dict):
            warn_config = {}
            guild_config["warns_config"] = warn_config
        current = warn_config.get("expire", False) in (True, 1, "true", "True")

        if expire is None:
            state = "enabled" if current else "disabled"
            return await ctx.send(
                f"30-day warning expiration is currently **{state}**. "
                f"Use `{ctx.clean_prefix}config warns true` or `false` to change it."
            )

        warn_config["expire"] = expire
        await moderation.save_data()
        state = "enabled" if expire else "disabled"
        await ctx.send(f"30-day warning expiration is now **{state}**.")


async def setup(bot):
    await bot.add_cog(Config(bot), override=True)
