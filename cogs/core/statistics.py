"""
cogs.core.statistics
~~~~~~~~~~~~~~~~~~~~~

A cog for showing the number of servers using each module

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import discord
from discord.ext import commands

from botutils import Menu, colors
from botutils.active_servers import active_guild_ids


class Statistics(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cogs = {  #  CogName: variable
            "Logger": "config",
            "SelfRoles": "config",
            "AutoRole": "config",
            "ChatFilter": "config",
            "AntiSpam": "config",
            "ModMail": "config",
            "Verification": "config",
            "Giveaways": "data",
            "RestoreRoles": "guilds",
            "Welcome": "config",
            "Leave": "toggle",
            "Suggestions": "config"
        }

    async def active_guild_ids(self) -> set[str]:
        return await active_guild_ids(self.bot)

    @staticmethod
    async def configured_guild_ids(config) -> set[str]:
        """Return normalized guild IDs from both in-memory and async module stores."""
        keys = getattr(config, "keys", None)
        source = keys() if callable(keys) else config
        if hasattr(source, "__aiter__"):
            return {str(guild_id) async for guild_id in source}
        try:
            return {str(guild_id) for guild_id in source}
        except TypeError:
            return set()

    @commands.hybrid_command(name="statistics", aliases=["stats"], description="Shows the number of servers using each module")
    async def statistics(self, ctx):
        owner = await self.bot.fetch_user(self.bot.config["bot_owner_id"])

        modules = discord.Embed(color=colors.fate)
        modules.set_author(name="Module Statistics", icon_url=owner.display_avatar.url)
        description = {}
        configs = {}
        for key, value in self.cogs.items():
            config = getattr(self.bot.cogs[key], value)
            configs[key] = config
            if hasattr(config, "__len__"):
                count = len(config)
            else:
                count = await config.count()
            description[key] = count
        modules.description = self.bot.utils.format_dict(description)
        modules.set_footer(text="Page 1 of 2")

        guild_ids = await self.active_guild_ids()
        active_modules = {}
        for key, config in configs.items():
            configured = await self.configured_guild_ids(config)
            active_modules[key] = len(configured & guild_ids)

        activity = discord.Embed(color=colors.fate)
        activity.set_author(
            name="Active Module Statistics (30 Days)",
            icon_url=owner.display_avatar.url,
        )
        activity.description = self.bot.utils.format_dict(active_modules)
        activity.set_footer(text="Page 2 of 2")

        await Menu(ctx, [modules, activity])

    @commands.command(
        name="active",
        description="Shows the number of servers active in the last 30 days",
        hidden=True,
    )
    @commands.is_owner()
    async def active(self, ctx):
        guild_ids = await self.active_guild_ids()
        self.bot.telemetry.gauge("active_servers", len(guild_ids))
        await ctx.send(f"{len(guild_ids):,} active servers in the last 30 days.")


async def setup(bot):
    await bot.add_cog(Statistics(bot), override=True)
