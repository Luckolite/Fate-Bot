"""
Event Listeners
~~~~~~~~~~~~~~~~

Contains classes and functions for easily awaiting events

Classes:
    CheckError
    Conversation
    Listener

Methods:
    parse_check

:copyright: (C) 2020-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
from contextlib import suppress

from discord import User, Member, Message
from discord.ext.commands import Context

from checks.exceptions import IgnoredExit


class CheckError(Exception):
    pass


class Conversation:
    """Emulate a realistic conversation process with typing"""
    def __init__(self, ctx: Context, delay: int = 2, delete_after: bool = False):
        self.ctx = ctx
        self.bot = ctx.bot
        self.delay = delay
        self.delete_after = delete_after
        self.msgs = []

    async def send(self, *args, **kwargs):
        """Sleep then return the object to send"""
        await asyncio.sleep(max(0, self.delay))

        if self.delete_after:
            kwargs["delete_after"] = 25
            msg = await self.ctx.send(*args, **kwargs)
            self.msgs.append(msg)
        else:
            msg = await self.ctx.send(*args, **kwargs)
        return msg

    async def ask(self, *args, **kwargs):
        """Get the recipients reply"""
        def predicate(m):
            return m.author.id == self.ctx.author.id and m.channel.id == self.ctx.channel.id

        buttons = False
        if "use_buttons" in kwargs:
            buttons = True
            del kwargs["use_buttons"]

        m = await self.send(*args, **kwargs)
        if buttons:
            return await self.bot.utils.get_answer(m)

        msg = await self.bot.utils.get_message(predicate)
        if self.delete_after:
            self.msgs.append(msg)

        if "cancel" in msg.content:
            msg = await self.ctx.send("Alright, operation cancelled")
            if self.delete_after:
                self.msgs.append(msg)
                await self.end()
            raise IgnoredExit

        return msg

    async def end(self):
        with suppress(Exception):
            msgs = [msg for msg in self.msgs if msg]
            await self.ctx.channel.delete_messages(msgs)


def parse_check(check) -> int:
    if callable(check):
        return check
    ids = []
    if isinstance(check, Context):
        ids.append(check.author.id)
    elif isinstance(check, (User, Member, Message)):
        ids.append(check.id)
    elif isinstance(check, int):
        ids.append(check)
    else:
        raise CheckError(f"Check of type '{type(check)}' isn't implemented")
    return ids[0]


class Listener:
    def __init__(self, bot):
        self.bot = bot

    async def get_reaction(self, check, timeout=60, ignore_timeout=True, duel=False):
        target_id = parse_check(check)  # type: int

        def predicate(r, u):
            return r.message.id == target_id or u.id == target_id

        if callable(target_id):
            predicate = target_id

        coro = self.bot.wait_for("reaction_add", check=predicate, timeout=timeout)

        if duel:
            async def wait_for(event):
                try:
                    return await self.bot.wait_for(event, check=predicate, timeout=timeout)
                except asyncio.TimeoutError:
                    return None

            tasks = {
                asyncio.create_task(wait_for("reaction_add")),
                asyncio.create_task(wait_for("reaction_remove")),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
            result = done.pop().result()
            if result is None:
                raise IgnoredExit from None
            return result

        if ignore_timeout:
            try:
                reaction, user = await coro
            except asyncio.TimeoutError:
                raise IgnoredExit from None
        else:
            reaction, user = await coro

        return reaction, user

    async def get_message(self, check, timeout=60, ignore_timeout=True):
        target = parse_check(check)

        def predicate(m):
            return m.author.id == target

        if callable(target):
            predicate = target

        coro = self.bot.wait_for("message", check=predicate, timeout=timeout)
        if ignore_timeout:
            try:
                msg = await coro
            except asyncio.TimeoutError:
                raise IgnoredExit from None
        else:
            msg = await coro

        return msg
