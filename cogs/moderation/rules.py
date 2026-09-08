"""
cogs.moderation.rules
~~~~~~~~~~~~~~~~~~~~~~

A module for adding in a rules command to display the servers rules quickly

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

from discord.ext import commands
import discord


class Mod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.rules = bot.utils.cache("rules")

    @commands.group(name="rules", description="Manages the server rules")
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.guild_only()
    async def rules(self, ctx):
        if not ctx.invoked_subcommand:
            if ctx.guild.id in self.rules:
                e = discord.Embed(color=0xFF0000)
                e.description = self.rules[ctx.guild.id]["content"]
                return await ctx.send(embed=e)
            await ctx.send(
                "This server doesnt have any rules set, try using .rules help"
            )

    @rules.command(name="help", description="Shows help for configuring server rules")
    async def _help(self, ctx):
        await ctx.send("**Rules Usage:**\n" ".rules set {rules}")

    @rules.command(name="set", description="Sets the server rules message")
    @commands.has_permissions(manage_guild=True)
    async def _set(self, ctx, *, new_rules):
        self.rules[ctx.guild.id] = {
            "content": new_rules
        }
        await ctx.send("Successfully set the rules 👍")
        await self.rules.flush()


async def setup(bot):
    await bot.add_cog(Mod(bot), override=True)
