"""
cogs.utility.inv_manager
~~~~~~~~~~~~~~~~~~~~~~~~~

Less api spammy invite manager that relies on high uptime

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
from contextlib import suppress
from time import monotonic

import discord
from discord import HTTPException, NotFound, Forbidden
from discord.ext import commands

from checks.exceptions import IgnoredExit


class InviteManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if hasattr(bot, "invite_manager"):
            del bot.invite_manager
        bot.invite_manager = self
        self.index = bot.utils.cache("invites")
        self.suppressed = (HTTPException, NotFound, Forbidden)

    async def cog_load(self):
        if self.bot.is_ready():
            await self.re_sync_all()

    def cog_unload(self):
        if getattr(self.bot, "invite_manager", None) is self:
            del self.bot.invite_manager

    async def re_sync_all(self):
        for guild_id in list(self.index):
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                await self.index.remove(guild_id)
                continue
            with suppress(IgnoredExit):
                await self.re_sync(guild)

    @commands.Cog.listener()
    async def on_ready(self):
        await self.re_sync_all()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        if guild.id in self.index:
            await self.disable(guild)

    def invite_to_dict(self, invite) -> dict:
        return {
            "joins": [],
            "leaves": [],
            "temporary": invite.temporary,
            "user_id": getattr(invite.inviter, "id", None),
            "uses": invite.uses or 0,
        }

    async def init(self, guild):
        if not isinstance(guild, discord.Guild):
            return
        if guild.id in self.index:
            return
        try:
            invites = await guild.invites()
        except self.suppressed:
            raise commands.BadArgument("Missing permissions to fetch server invites")
        self.index[guild.id] = {
            inv.code: self.invite_to_dict(inv) for inv in invites
        }
        await self.index.flush()

    async def re_sync(self, guild):
        """Update the index with any un-logged changes"""
        try:
            invites = await guild.invites()
        except self.suppressed:
            raise IgnoredExit
        for invite in invites:
            await asyncio.sleep(0)
            uses = invite.uses or 0
            if invite.code in self.index[guild.id]:
                if uses != self.index[guild.id][invite.code]["uses"]:
                    self.index[guild.id][invite.code]["uses"] = uses
            else:
                self.index[guild.id][invite.code] = self.invite_to_dict(invite)
        for code, data in list(self.index[guild.id].items()):
            for user_id in list(data["joins"]):
                await asyncio.sleep(0)
                if not guild.get_member(user_id):
                    if user_id not in self.index[guild.id][code]["leaves"]:
                        self.index[guild.id][code]["leaves"].append(user_id)
                    self.index[guild.id][code]["joins"].remove(user_id)
        await self.index.flush()

    async def disable(self, guild):
        if isinstance(guild, discord.Guild):
            guild_id = guild.id
        else:
            guild_id = int(guild)
        await self.index.remove(guild_id)

    async def get_inviter(self, guild, member, timeout=2):
        if guild.id not in self.index:
            raise KeyError(f"{guild.id} not in index")
        deadline = monotonic() + max(0, timeout)
        while monotonic() <= deadline:
            for invite in list(self.index[guild.id].values()):
                if member.id in invite["joins"]:
                    inviter_id = invite.get("user_id")
                    if inviter_id is None:
                        return "unknown"
                    user = self.bot.get_user(inviter_id)
                    if not user:
                        with suppress(*self.suppressed):
                            user = await self.bot.fetch_user(inviter_id)
                    return str(user) if user else "unknown"
            await asyncio.sleep(0.05)
        return "unknown"

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if not member.guild or member.guild.id not in self.index:
            return
        guild = member.guild
        with suppress(*self.suppressed):
            invites = await guild.invites()
            discrepancies = []
            for invite in invites:
                uses = invite.uses or 0
                if invite.code not in self.index[guild.id]:
                    self.index[guild.id][invite.code] = self.invite_to_dict(invite)
                    if uses != 0:
                        discrepancies.append(invite)
                elif uses != self.index[guild.id][invite.code]["uses"]:
                    discrepancies.append(invite)
            if len(discrepancies) == 1:
                inv = discrepancies[0]
                if member.id not in self.index[guild.id][inv.code]["joins"]:
                    self.index[guild.id][inv.code]["joins"].append(member.id)
                if member.id in self.index[guild.id][inv.code]["leaves"]:
                    self.index[guild.id][inv.code]["leaves"].remove(member.id)
            for invite in invites:
                self.index[guild.id][invite.code]["uses"] = invite.uses or 0
        await self.index.flush()

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if member.id == self.bot.user.id:
            return
        if not member.guild or member.guild.id not in self.index:
            return
        guild = member.guild
        for code, data in list(self.index[guild.id].items()):
            await asyncio.sleep(0)
            if member.id in data["joins"]:
                self.index[guild.id][code]["joins"].remove(member.id)
                self.index[guild.id][code]["leaves"].append(member.id)
        await self.index.flush()

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        if invite.guild.id in self.index:
            self.index[invite.guild.id][invite.code] = self.invite_to_dict(invite)
        await self.index.flush()


async def setup(bot):
    await bot.add_cog(InviteManager(bot), override=True)
