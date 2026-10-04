"""
Fetch-User Helper
~~~~~~~~~~~~~~~~~~

Helper class for fetching a user via most possible means

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio

import discord
from discord import NotFound, Forbidden, HTTPException
from discord.ext import commands

import botutils
from checks.exceptions import IgnoredExit


class GetUser:
    """Resolve user IDs and names, prompting when several matches remain."""

    def __init__(self, bot, *args, **kwargs):
        self.bot = bot
        self.multi = False

        # Configurable arguments
        self.ctx = None
        self.channel = None
        self.guild = None
        self.user_ids = []
        self.names = []

        self.__parse(*args, **kwargs)

    def all(self):
        self.multi = True
        return self

    def __await__(self):
        return self.get_user().__await__()

    def __parse(self, *args, **kwargs):
        for arg in args:
            if isinstance(arg, commands.Context):
                self.ctx = arg
                if arg.guild:
                    self.guild = arg.guild
                self.channel = arg.channel
            elif isinstance(arg, discord.Message):
                self.channel = arg.channel
                self.guild = arg.guild
                self.user_ids.extend(arg.raw_mentions)
            elif isinstance(arg, discord.TextChannel):
                self.channel = arg
            elif isinstance(arg, int) or (isinstance(arg, str) and arg.isdigit()):
                self.user_ids.append(int(arg))
            elif isinstance(arg, str):
                for word in arg.split():
                    if "@" in word and any(c.isdigit() for c in word):
                        try:
                            self.user_ids.append(int("".join([c for c in word if c.isdigit()])))
                        except ValueError:
                            pass
                    elif "@" in word:
                        self.names.append(word.lstrip("@"))
                    else:
                        self.names.append(word)
        if "ctx" in kwargs:
            self.ctx = kwargs["ctx"]
            self.channel = self.ctx.channel
            if self.ctx.guild:
                self.guild = self.ctx.guild
        elif "context" in kwargs:
            self.ctx = kwargs["context"]
            self.channel = self.ctx.channel
            if self.ctx.guild:
                self.guild = self.ctx.guild
        if "channel" in kwargs:
            self.channel = kwargs["channel"]
            self.guild = self.channel.guild
        if "name" in kwargs:
            self.names.append(kwargs["name"])
        if "names" in kwargs:
            self.names.extend(kwargs["names"])
        if "guild" in kwargs:
            self.guild = kwargs["guild"]
        if "user_id" in kwargs:
            self.user_ids.append(kwargs["user_id"])
        if "user_ids" in kwargs:
            self.user_ids.extend(kwargs["user_ids"])

    async def _validate_id(self, user_id):
        if user_id > 9223372036854775807:
            err = f"'{user_id}' isn't a proper UserID"
            if self.ctx:
                raise commands.BadArgument(err)
            if self.channel:
                await self.channel.send(err)
            return False
        return True

    async def get_user(self):
        users = await self._get_guild_users() if self.guild else await self._get_global_users()
        if not users:
            if self.ctx:
                await self.ctx.send("Couldn't find any users going by that")
                raise IgnoredExit
            return None
        if self.multi:
            return users
        if len(users) > 1:
            return await self._choose_user(users)
        return users[0]

    async def _get_guild_users(self):
        """Find cached members by ID and partial name without fetching Discord."""
        users = []
        for user_id in self.user_ids:
            if not await self._validate_id(user_id):
                continue
            member = self.guild.get_member(user_id)
            if member:
                users.append(member)

        matches = []
        for name in self.names:
            for member in self.guild.members:
                await asyncio.sleep(0)
                if name.lower() in str(member).lower():
                    matches.append(member)
        if not matches:
            return users
        if len(matches) == 1:
            users.append(matches[0])
        elif self.multi:
            users.extend(matches)
        elif not self.channel:
            users.append(matches[0])
        else:
            users.append(await self._choose_user(matches, unique_names=True))
        return users

    async def _get_global_users(self):
        """Use cached users first, then Discord ID fetching and name conversion."""
        users = []
        for user_id in self.user_ids:
            if not await self._validate_id(user_id):
                continue
            user = self.bot.get_user(user_id)
            if not user:
                try:
                    user = await self.bot.fetch_user(user_id)
                except (NotFound, Forbidden, HTTPException):
                    continue
            users.append(user)
        if self.names and not self.ctx:
            raise TypeError("To get users by name 'ctx' is required")
        converter = commands.UserConverter()
        for name in self.names:
            try:
                user = await converter.convert(self.ctx, name)
                users.append(user)
            except commands.CommandError:
                pass
        return users

    async def _choose_user(self, users, *, unique_names=False):
        """Keep displayed choices aligned with the users they resolve to."""
        if self.ctx and unique_names:
            users_by_name = {}
            for user in users:
                users_by_name.setdefault(str(user), user)
            choices = list(users_by_name)
            users = list(users_by_name.values())
        else:
            choices = [str(user) if self.ctx else user.mention for user in users]
        if self.ctx:
            choice = await botutils.GetChoice(self.ctx, choices)
        else:
            choice = await self.bot.utils.get_choice(self.ctx, choices, name="Which user")
        if not choice:
            raise IgnoredExit
        return users[choices.index(choice)]

