"""
cogs.core.toggles
~~~~~~~~~~~~~~~~~~

A cog for adding the functionality of `.enable module` instead of `.module enable`

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

from discord.ext import commands

from botutils import get_prefix
from fate import Fate


class Toggles(commands.Cog):
    def __init__(self, bot: Fate):
        self.bot = bot

    async def run_command(self, ctx, module, key) -> None:
        for _module, commands in self.bot.toggles.items():
            if _module.lower() == module.lower():
                enable, disable = commands
                break
        else:
            p = get_prefix(ctx)  # type: str
            return await ctx.send(
                f"Module not found. If you're trying to {key} "
                f"a command, use `{p}{key}-command your_command`"
            )
        if key == "enable":
            await enable.invoke(ctx)
        else:
            await disable.invoke(ctx)

    @commands.command(name="enable", description="Enables a bot module in this server")
    @commands.guild_only()
    async def enable_module(self, ctx, module):
        await self.run_command(ctx, module=module, key="enable")

    @commands.command(name="disable", description="Disables a bot module in this server")
    @commands.guild_only()
    async def disable_module(self, ctx, module):
        await self.run_command(ctx, module=module, key="disable")


async def setup(bot: Fate):
    await bot.add_cog(Toggles(bot), override=True)
