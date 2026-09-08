"""
Tools
~~~~~~

A collection of utility functions

Classes:
    PersistentTasks
    TempConvo
    Cooldown
    OperationLock
    Formatting

Functions:
    split
    format_date
    bytes2human
    extract_time
    get_seconds
    get_images
    total_seconds
    get_user_rewrite
    get_time
    get_role
    update_msg

Misc variables:
    operators
    formulas

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import re
from copy import deepcopy
from datetime import datetime, timedelta
from time import time
from typing import List, Optional, Tuple, Union
from unicodedata import normalize

import discord

import botutils
from checks.exceptions import IgnoredExit
from .emojis import arrow


class PersistentTasks:
    """
    Keeps tasks running even after the bot restarts.
    This requires the callback to use a sleep_until(date) method instead of sleep(duration)
    """
    def __init__(self, bot, database, callback, identifier, debug=False):
        """
        :param bot:
        :param str database: the db name to use
        :param callable callback: the coro to call
        :param str identifier: the name of the param with the unique key
        :param bool debug: don't suppress errors if set to True
        """
        self.bot = bot
        self.callback = callback
        self.identifier = identifier
        self.debug = debug
        self.db = bot.utils.cache(database)
        self.tasks = {}
        if not bot.is_ready():
            for _key, kwargs in self.db.items():
                self.run(**kwargs)

    def run(self, **kwargs) -> asyncio.Task:
        if duration := kwargs.get("sleep_for", None):
            if isinstance(duration, int):
                duration = kwargs["sleep_for"] = datetime.now() + timedelta(seconds=duration)
        key = kwargs[self.identifier]
        self.db[key] = deepcopy(kwargs)
        if duration:
            kwargs["sleep_for"] = max(
                0, int((duration - datetime.now()).total_seconds())
            )
        if task := self.tasks.get(key):
            task.cancel()
        task = self.bot.loop.create_task(self._run(key, **kwargs))
        self.tasks[key] = task
        return task

    async def cancel(self, key: Union[int, str]) -> None:
        if task := self.tasks.pop(key, None):
            task.cancel()
        if key in self.db:
            await self.db.remove(key)

    async def _run(self, key: Union[int, str], **kwargs):
        finished = False
        try:
            await self.db.flush()
            try:
                await self.callback(**kwargs)
            except Exception as error:  # type: ignore
                self.bot.loop.call_exception_handler({
                    "message": f"{key} task failed",
                    "exception": error
                })
            finished = True
        finally:
            if self.tasks.get(key) is asyncio.current_task():
                self.tasks.pop(key, None)
                if finished and key in self.db:
                    await self.db.remove(key)


class TempConvo:
    def __init__(self, context):
        self.ctx = context
        self.sent = []

    def predicate(self, message):
        return message.author.id in [self.ctx.author.id, self.ctx.bot.user.id]

    async def __aenter__(self):
        return self

    async def __aexit__(self, _type, _tb, _exc):
        before = self.sent[len(self.sent) - 1]
        after = self.sent[0]
        msgs = [
            message
            async for message in self.ctx.channel.history(before=before, after=after)
        ]
        await self.ctx.channel.delete_messages([
            before, after, *[
                msg for msg in msgs if self.predicate(msg)
            ]
        ])

    async def send(self, *args, **kwargs):
        msg = await self.ctx.send(*args, **kwargs)
        self.sent.append(msg)


class Cooldown:
    def __init__(self, limit, timeframe, raise_error=False):
        self.limit = limit
        self.timeframe = timeframe
        self.raise_error = raise_error
        self.index = {}
        self.cleanup_task_is_running = False
        self._last_cleanup = time()

    def _cleanup(self, now):
        if now - self._last_cleanup < 60:
            return
        current_window = int(now / self.timeframe)
        self.index = {
            key: value for key, value in self.index.items()
            if value[0] == current_window
        }
        self._last_cleanup = now

    async def cleanup_task(self):
        self.cleanup_task_is_running = True
        try:
            await asyncio.sleep(60)
            self._cleanup(time())
        finally:
            self.cleanup_task_is_running = False

    def check(self, _id) -> bool:
        """ Returns whether the ID is on cooldown, or not """
        current_time = time()
        self._cleanup(current_time)
        now = int(current_time / self.timeframe)
        if _id not in self.index:
            self.index[_id] = [now, 0]
        if self.index[_id][0] == now:
            self.index[_id][1] += 1
        else:
            self.index[_id] = [now, 1]
        if self.index[_id][1] > self.limit:
            if self.raise_error:
                raise IgnoredExit
            return True
        return False


def cooldowns(rate_limits: List[Tuple]) -> List[Cooldown]:
    """
    Converts a list of rate limits into a list of Cooldown objects.
    E.g. [(limit, within_timeframe), ...]
    """
    return [
        Cooldown(limit, rate)
        for limit, rate in rate_limits
    ]


class OperationLock:
    bot = None
    def __init__(self, key):
        self.key = key

    def __enter__(self):
        if self.key in self.bot.operation_locks:
            raise IgnoredExit
        self.bot.operation_locks.append(self.key)

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        self.bot.operation_locks.remove(self.key)


class Formatting:
    def __init__(self, bot):
        self.bot = bot

    def format_dict(self, data: dict, emoji=None) -> str:
        if emoji is None:
            emoji = arrow() + " "
        elif emoji is False:
            emoji = ""
        result = ""
        for k, v in data.items():
            if v:
                result += f"\n{emoji}**{k}:** {v}"
            else:
                result += f"\n{emoji}{k}"
        if emoji == "<:enter:673955417994559539> ":
            lines = result.splitlines()
            result = []
            for i, line in enumerate(lines):
                if i + 1 == len(lines):
                    line = line.replace(emoji, botutils.emojis.reply)
                else:
                    line = line.replace(emoji, botutils.emojis.creply)
                result.append(line)
            result = "\n".join(result)
        return result

    def add_field(self, embed, name: str, value: dict, inline=True):
        embed.add_field(name=f"◈ {name}", value=self.format_dict(value), inline=inline)

    async def dump_json(self, data):
        """Save json in a different thread to prevent freezing the loop"""
        return await self.bot.dump(data)

    async def wait_for_msg(self, ctx, user=None):
        if not user:
            user = ctx.author

        def pred(m):
            return m.channel.id == ctx.channel.id and m.author.id == user.id

        try:
            msg = await self.bot.wait_for("message", check=pred, timeout=60)
        except asyncio.TimeoutError:
            await ctx.send("Timeout error")
            return False
        else:
            return msg


class TemporaryList(list):
    def __init__(self, keep_items_for=15, *args, **kwargs):
        self.keep_items_for = keep_items_for
        self._expiry_tasks = set()
        super().__init__(*args, **kwargs)

    async def waiter(self, value):
        await asyncio.sleep(self.keep_items_for)
        if value in self:
            self.remove(value)

    def append(self, value):
        if value not in self:
            super().append(value)
            task = asyncio.create_task(self.waiter(value))
            self._expiry_tasks.add(task)
            task.add_done_callback(self._expiry_tasks.discard)


def cln(string: str) -> str:
    """
    Strips a name of unicode and tries to replace with abcs.

    :returns: Normalized name, or "Invalid-User"
    """
    new = normalize("NFKD", string).encode('ascii', 'ignore').decode()
    if not new:
        new = "Invalid"
    return new


def s(var: int) -> str:
    """ Decides whether or not to use an s """
    var = int(var)
    if var == 0 or var > 1:
        return "s"
    return ""

def url_from(obj: Optional[discord.Asset]):
    """ Transforms an object with a possible .url attribute into something usable in embeds """
    return getattr(obj, "url", None)


def split(text, amount=2000) -> list:
    return [text[i : i + amount] for i in range(0, len(text), amount)]


def format_date(date=None, other_date=None, seconds=None) -> str:
    """
    Formats the time since the provided datetime object

    :param datetime or float date:
    :param datetime or float other_date:
    :param int or None seconds:
    """
    def space():
        """ Add a space to the start only if fmt has existing content """
        return " " if fmt else ""

    if isinstance(date, float):
        date = datetime.fromtimestamp(date)
    if other_date and isinstance(other_date, float):
        other_date = datetime.fromtimestamp(other_date, tz=date.tzinfo)
    if seconds is not None:
        date = datetime.now() + timedelta(seconds=int(seconds))
    if not date:
        return "unknown"
    date = date.replace(microsecond=0)

    fmt = ""
    now = other_date or datetime.now(tz=date.tzinfo)
    now = now.replace(microsecond=0)

    if now > date:
        date = now - date
    else:
        date = date - now
    remainder = date.total_seconds()

    if days := date.days:
        if years := int(date.days / 365):
            fmt += f"{years} year{s(years)}"
            days -= years * 365
            remainder -= 60 * 60 * 24 * 365 * years

        one_month = 60 * 60 * 24 * 30
        if remainder >= one_month:
            months = int(remainder / one_month)
            fmt += f"{space()}{months} month{s(months)}"
            days -= months * 30
            remainder -= one_month * months

        one_week = 60 * 60 * 24 * 7
        if remainder >= one_week:
            weeks = int(remainder / one_week)
            fmt += f"{space()}{weeks} week{s(weeks)}"
            days -= weeks * 7
            remainder -= one_week * weeks

        if days:
            fmt += f"{space()}{days} day{s(days)}"
            remainder -= 60 * 60 * 24 * days

    if hours := int(remainder / 60 / 60):
        fmt += f"{space()}{int(hours)} hour{s(hours)}"
        remainder -= 60 * 60 * hours

    if minutes := int(remainder / 60):
        fmt += f"{space()}{int(minutes)} minute{s(minutes)}"
        remainder -= 60 * minutes

    if remainder >= 1 or not fmt:
        fmt += f"{space()}{round(remainder, 0 if fmt else 1)}s"

    return " ".join(fmt.replace('.0', '').split()[:4])


def bytes2human(n):
    for symbol, size in reversed(_BYTE_UNITS):
        if n >= size:
            value = float(n) / size
            return "%.1f%s" % (value, symbol)
    return "%sB" % n


_BYTE_UNITS = tuple(
    (symbol, 1 << (index + 1) * 10)
    for index, symbol in enumerate(("KB", "MB", "GB", "TB", "PB", "E", "Z", "Y"))
)


operators = {
    'seconds': 's',
    'minutes': 'm',
    'hours': 'h',
    'days': 'd',
    'weeks': 'w',
    'months': 'M',
    'years': 'y'
}

formulas = {
    's': 1,        # seconds
    'm': 60,       # minutes
    'h': 3600,     # hours:  60 * 60
    'd': 86400,    # days:   60 * 60 * 24
    'w': 604800,   # weeks:  60 * 60 * 24 * 7
    'M': 2592000,  # months: 60 * 60 * 24 * 30
    'y': 31536000  # years:  60 * 60 * 24 * 365
}


def extract_time(string: str) -> Optional[int]:
    string = string.replace(" ", "")[:20]
    for human_form, operator in operators.items():
        string = string.replace(human_form, operator)
        string = string.replace(human_form.rstrip('s'), operator)
    timers = re.findall("[0-9]*[smhdwMy]", string[:8])
    if not timers:
        return None
    timeframe = 0
    for timer in timers:
        operator = ''.join(c for c in timer if not c.isdigit())
        num = timer.replace(operator, '')
        if not num:
            continue
        num = int(num) * formulas[operator]
        timeframe += num
    return timeframe


def get_seconds(minutes=None, hours=None, days=None):
    if minutes:
        return minutes * 60
    if hours:
        return hours * 60 * 60
    if days:
        return days * 60 * 60 * 24
    return 0


async def get_images(ctx) -> list:
    """ Gets the latest image(s) in the channel """

    def scrape(msg: discord.Message) -> list:
        """ Thoroughly checks a msg for images """
        image_links = []
        if msg.attachments:
            for attachment in msg.attachments:
                image_links.append(attachment.url)
        for embed in msg.embeds:
            embed_data = embed.to_dict()
            if "image" in embed_data:
                image_links.append(embed_data["image"]["url"])
        args = msg.content.split()
        if not args:
            args = [msg.content]
        for arg in args:
            if "https://cdn.discordapp.com/attachments/" in arg:
                image_links.append(arg)
        return image_links

    image_links = scrape(ctx.message)
    if image_links:
        return image_links
    async for msg in ctx.channel.history(limit=10):
        image_links = scrape(msg)
        if image_links:
            return image_links
    await ctx.send("No images found in the last 10 msgs")
    return image_links


def total_seconds(now, before):
    secs = str((now - before).total_seconds())
    return secs[: secs.find(".") + 2]


async def get_user_rewrite(ctx, target: str = None) -> Union[discord.User, discord.Member]:
    """ Grab a user by id, name, or username, and convert to Member if possible """
    if not target:
        user = ctx.author
    elif target.isdigit() or re.findall("<.@[0-9]*>", target):
        user_id = int("".join(c for c in target if c.isdigit()))
        user = await ctx.bot.fetch_user(user_id)
    elif ctx.guild is None:
        user = ctx.author
    else:
        for usr in ctx.bot.users:
            if str(usr) == target:
                user = usr
                break
        else:
            target = re.sub("#[0-9]{4}", "", target.lower())
            results = [
                member
                for member in ctx.guild.members
                if (
                    target in member.display_name.lower()
                    if not member.nick
                    else target in member.name.lower()
                )
            ]
            if len(results) == 1:
                user = results[0]  # type: discord.Member
            elif len(results) > 1:
                user = await ctx.bot.get_choice(ctx, *results, user=ctx.author)
            else:
                user = ctx.author
    if ctx.guild is not None and not isinstance(user, discord.Member):
        user = ctx.guild.get_member(user.id) or user
    return user


def get_time(seconds):
    return format_date(seconds=seconds)


async def get_role(ctx, name) -> Optional[discord.Role]:
    if name.startswith("<@"):
        role_id = "".join(char for char in name if char.isdigit())
        return ctx.guild.get_role(int(role_id)) if role_id else None
    else:
        roles = []
        for role in ctx.guild.roles:
            if name.lower() == role.name.lower():
                roles.append(role)
        if not roles:
            for role in ctx.guild.roles:
                if name.lower() in role.name.lower():
                    if role not in roles:
                        roles.append(role)
        if roles:
            if len(roles) == 1:
                return roles[0]

            index = {r.name: r for r in roles}
            choice = await botutils.GetChoice(ctx, index.keys(), placeholder="Select a role")
            return index[choice]


async def update_msg(msg, new) -> discord.Message:
    if len(msg.content) + len(new) + 2 >= 2000:
        msg = await msg.channel.send("Uploading emoji(s)")
    return await msg.edit(content=f"{msg.content}\n{new}")
