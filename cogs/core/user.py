"""
cogs.core.user
~~~~~~~~~~~~~~~

An owner only cog for managing the bot user

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio

import discord
from discord.ext import commands

from botutils import colors

filter = [
    "child porn",
    " rape "
]


class User(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        if self.bot.is_ready():
            await self.filter_servers()

    @commands.Cog.listener("on_ready")
    async def filter_servers(self):
        for guild in list(self.bot.guilds):
            await asyncio.sleep(0)
            if any(phrase in guild.name.lower() for phrase in filter):
                try:
                    self.bot.log(f"Left `{guild}` for having an offensive name")
                    await guild.leave()
                except (AttributeError, discord.errors.NotFound):
                    pass

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        if any(phrase in guild.name.lower() for phrase in filter):
            self.bot.log(f"Left `{guild}` for having an offensive name")
            await guild.leave()


    @commands.command(
        name="set-status",
        aliases=["change-presence"],
        description="Changes the bot presence",
    )
    @commands.is_owner()
    async def change_presence(self, ctx, *, new_status):
        await self.bot.change_presence(
            activity=discord.Game(name=new_status), status=discord.Status.online
        )
        await ctx.send("Done")

    @commands.command(name="block", description="Blocks users from using the bot")
    @commands.is_owner()
    async def block(self, ctx, users: commands.Greedy[discord.User], *, reason="unspecified"):
        async with self.bot.utils.cursor() as cur:
            for user in users:
                await cur.execute(
                    "insert into blocked values (%s, %s, %s) "
                    "on duplicate key update user_id = %s;",
                    (user.id, str(user), reason, user.id)
                )
                await ctx.send(f"Blocked {user}")

    @commands.command(name="unblock", description="Unblocks a user from using the bot")
    @commands.is_owner()
    async def unblock(self, ctx, user: discord.User):
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "delete from blocked where user_id = %s;", (user.id,)
            )
        await ctx.send(f"Unblocked {user}")

    @commands.command(name="blocked", description="Lists users blocked from using the bot")
    @commands.is_owner()
    async def blocked(self, ctx):
        e = discord.Embed(color=colors.fate)
        e.set_author(name="Blocked Users", icon_url=self.bot.user.display_avatar.url)
        e.description = ""
        async with self.bot.utils.cursor() as cur:
            await cur.execute("select user_id, name, reason from blocked;")
            blocked = await cur.fetchall()
        for user_id, name, reason in blocked:
            try:
                user = await self.bot.fetch_user(user_id)
            except:
                user = name
            e.add_field(name=str(user), value=reason)
        await ctx.send(embed=e)


async def setup(bot):
    await bot.add_cog(User(bot), override=True)
