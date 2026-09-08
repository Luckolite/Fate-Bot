"""
cogs.core.error_handler
~~~~~~~~~~~~~~~~~~~~~~~~

A cog for handling exceptions raised within commands

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import sys
import traceback
from contextlib import suppress
from io import BytesIO
from typing import *

import aiohttp
import discord
from aiohttp import ClientConnectorError, ClientOSError, ServerDisconnectedError
from discord.ext import commands
from discord.http import DiscordServerError
from pymongo.errors import DuplicateKeyError

import fate
from botutils import colors, Cooldown
from botutils.log_paths import (
    LAST_ERROR_LOG_PATH,
    ensure_logging_directory,
)
from checks import checks
from checks.exceptions import IgnoredExit


def safe_console_print(*values, sep=" ", end="\n", file=None) -> None:
    """Write diagnostics even when the Windows console cannot encode Unicode."""
    stream = file or sys.stdout
    text = sep.join(str(value) for value in values) + end
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        escaped = text.encode(encoding, errors="backslashreplace").decode(encoding)
        stream.write(escaped)


def strip_url_prefix(value: object) -> str:
    """Remove an actual leading URL prefix without stripping unrelated letters."""
    message = str(value)
    for prefix in ("https://", "http://", "www."):
        message = message.removeprefix(prefix)
    return message


def unwrap_original(error: Exception) -> Exception:
    """Return the underlying exception from nested command wrappers."""
    seen = {id(error)}
    while isinstance(original := getattr(error, "original", None), Exception):
        if id(original) in seen:
            break
        error = original
        seen.add(id(error))
    return error


class ErrorHandler(commands.Cog):
    notification_limit = 500
    ignored = (
        IgnoredExit,
        aiohttp.ClientOSError,
        aiohttp.ClientConnectorError,
        asyncio.exceptions.TimeoutError,
        discord.DiscordServerError,
        discord.errors.NotFound,
        commands.CommandNotFound,
        commands.NoPrivateMessage,
        discord.DiscordServerError,
    )

    def __init__(self, bot: fate.Fate):
        self.bot = bot
        ensure_logging_directory()
        self.notifs: Dict[int, int] = {}  # Error message ID -> affected user ID
        self._report_tasks: set[asyncio.Task] = set()
        self.cd = Cooldown(1, 5)
        self.response_cooldown = Cooldown(1, 10)
        self.reaction_cooldown = Cooldown(1, 4)

        self._previous_exception_handler = bot.loop.get_exception_handler()
        bot.loop.set_exception_handler(self.task_exception_handler)

    def cog_unload(self) -> None:
        handler = self.bot.loop.get_exception_handler()
        if getattr(handler, "__self__", None) is self:
            self.bot.loop.set_exception_handler(self._previous_exception_handler)
        for task in tuple(self._report_tasks):
            task.cancel()
        self._report_tasks.clear()

    def _track_report_task(self, task: asyncio.Task) -> None:
        """Keep fire-and-forget error reports alive until Discord accepts them."""
        self._report_tasks.add(task)

        def finished(completed: asyncio.Task) -> None:
            self._report_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                self.bot.loop.default_exception_handler(
                    {"message": "Failed to deliver an error report", "exception": error}
                )

        task.add_done_callback(finished)

    def task_exception_handler(self, loop: asyncio.AbstractEventLoop, context: dict):
        """ Suppress ignored exceptions within tasks """
        error: Exception = context.get("exception")
        if error and isinstance(error, self.ignored):
            return

        if error:
            if channel := self.bot.get_channel(self.bot.config["event_errors"]):
                message = context.get("message", None)
                stack = ''.join(traceback.format_tb(error.__traceback__))
                trace = f"```python\n{message}\n{stack}" \
                        f"{type(error).__name__}: {''.join([str(x) for x in error.args])}```"
                self._track_report_task(loop.create_task(channel.send(trace)))
            else:
                safe_console_print(f"uncaught error, {error}")
                with LAST_ERROR_LOG_PATH.open("w", encoding="utf-8") as f:
                    f.write(str(error))
                loop.default_exception_handler(context)

    # @commands.Cog.listener()
    async def on_error(self, _event_method, *_args, **_kwargs):
        """ Suppress ignored exceptions within events """
        err = sys.exc_info()[1]
        if err is None:
            return
        error = unwrap_original(err)
        if not isinstance(error, self.ignored):
            with LAST_ERROR_LOG_PATH.open("w", encoding="utf-8") as f:
                f.write(str(error))
            raise error

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error, ignore_if_handler = True):
        # Ensure the servers `currently running commands` index gets updated
        if (
            ctx.command
            and ctx.command.cog
            and ctx.command.cog.__class__.__name__ == "Moderation"
        ):
            module = self.bot.cogs["Moderation"]
            await module.cog_after_invoke(ctx)
        if ignore_if_handler and ctx.cog and ctx.cog.has_error_handler():
            return

        # Suppress spammy, or intentional errors
        error = unwrap_original(error)
        if isinstance(error, self.ignored) or not (error_to_send := str(error)):
            return
        # Sometimes the actual exception class can be nested in the error message
        if any(exception.__name__ in error_to_send for exception in self.ignored):
            return
        if "Unknown Message" in error_to_send:
            return
        if "Too Many Requests" in error_to_send:
            return

        # Don't bother if completely missing access
        if not isinstance(ctx.channel, discord.DMChannel):
            if not ctx.guild or not ctx.guild.me or not ctx.channel:
                return
            perms = ctx.channel.permissions_for(ctx.guild.me)
            if not perms.send_messages:
                return

        # Cooldown the guild instead of author if available
        _id = getattr(ctx.guild, "id", ctx.author.id)
        if self.cd.check(_id):
            return

        # Format the full traceback
        full_tb = "\n".join(traceback.format_tb(error.__traceback__))
        formatted_tb = f"```python\n{full_tb}\n{type(error).__name__}: {error}```"

        try:
            # Disabled globally in the code
            if isinstance(error, RuntimeError):
                return await ctx.send("Oop, I had an internal error. Rerun the command")
            elif isinstance(error, commands.DisabledCommand):
                return await ctx.send(f"`{ctx.command}` is disabled.")

            # Unaccepted, or improperly used arg was passed
            elif isinstance(error, commands.ExpectedClosingQuoteError):
                return await ctx.send("You can't include a `\"` in that argument")
            elif isinstance(error, (commands.BadArgument, commands.errors.BadUnionArgument)):
                return await ctx.send(strip_url_prefix(error))

            elif isinstance(error, DuplicateKeyError):
                self.bot.log.critical(full_tb)
                return await ctx.send("Error saving changes because to a duplicate entry, it's likely you ran the command twice at once")

            elif isinstance(error, commands.MaxConcurrencyReached):
                return await ctx.send(error)

            # Too fast, sMh
            elif isinstance(error, commands.CommandOnCooldown):
                user_id = ctx.author.id
                if self.response_cooldown.check(user_id):
                    if not self.reaction_cooldown.check(user_id):
                        await ctx.message.add_reaction("⏳")
                else:
                    await ctx.send(error)
                return

            # User forgot to pass a required argument
            elif isinstance(error, commands.MissingRequiredArgument):
                return await ctx.send(error)

            # Failed a decorator check
            elif isinstance(error, commands.CheckFailure):
                if not checks.command_is_enabled(ctx):
                    return await ctx.send(f"{ctx.command} is disabled here")
                elif "check functions" in str(error):
                    return await ctx.message.add_reaction("🚫")
                elif "You do not own this bot" in str(error):
                    return
                else:
                    return await ctx.send(error)

            # The bot tried to perform an action on a non existent or removed object
            elif isinstance(error, discord.errors.NotFound):
                try:
                    await ctx.send(
                        f"Something I tried to do an operation on was removed or doesn't exist",
                        reference=ctx.message,
                        delete_after=5
                    )
                except discord.errors.HTTPException:
                    await ctx.send(
                        f"Something I tried to do an operation on was removed or doesn't exist",
                        delete_after=5
                    )
                return

            # An action by the bot failed due to missing access or lack of required permissions
            elif isinstance(error, discord.errors.Forbidden):
                if not ctx.guild:
                    return
                if ctx.channel.permissions_for(ctx.guild.me).send_messages:
                    return await ctx.send(error)
                if ctx.channel.permissions_for(ctx.guild.me).add_reactions:
                    return await ctx.message.add_reaction("⚠")
                return await ctx.author.send(
                    f"I don't have permission to reply to you in {ctx.guild.name}"
                )

            # The bot attempted to complete an invalid action
            elif isinstance(error, discord.errors.HTTPException):
                if "Maximum number of guild roles reached" in str(error):
                    return await ctx.send("Can't operate due to this server reaching the max number of roles")

            # Discord shit the bed
            elif isinstance(error, DiscordServerError):
                return await ctx.send(
                        "Oop-\nDiscord shit in the bed\nIt's not my fault, it's theirs"
                    )

            # Failed while parsing an argument that contains a "'"
            elif isinstance(error, commands.UnexpectedQuoteError):
                return await ctx.send("You can't use quotes in that argument")

            # bAd cOdE, requires fix if occurs
            elif isinstance(error, KeyError):
                error_to_send = f"No Data: {error}"[:64]

            # Send a user-friendly error and state that it'll be fixed soon
            if not isinstance(error, discord.errors.NotFound):
                e = discord.Embed(color=colors.red)
                e.description = f"[{error_to_send[:64]}](https://www.youtube.com/watch?v=t3otBjVZzT0)"
                e.set_footer(text="This error has been logged, and will be fixed soon")
                await ctx.send(embed=e)

            # Temporarily can't connect to discord
            if isinstance(error, (ClientConnectorError, ClientOSError, ServerDisconnectedError)):
                await ctx.send(
                    "Temporarily failed to connect to discord; please re-run your command"
                )
                return
        except (discord.errors.Forbidden, discord.errors.NotFound):
            return

        # Print everything to console to get the full traceback
        safe_console_print(
            "Ignoring exception in command {}:".format(ctx.command),
            file=sys.stderr,
        )
        safe_console_print(
            "".join(traceback.format_exception(type(error), error, error.__traceback__)),
            end="",
            file=sys.stderr,
        )

        # Prepare to log the error to a dedicated error channel
        channel = self.bot.get_channel(self.bot.config["command_errors"])
        if channel is None:
            safe_console_print(
                "Unable to deliver command error: configured command_errors channel "
                f"{self.bot.config['command_errors']} is unavailable",
                file=sys.stderr,
            )
            return
        e = discord.Embed(color=colors.red)
        e.description = f"[{ctx.message.content}]({ctx.message.jump_url})"
        e.set_author(
            name=f"| Fatal Error | in {ctx.command}", icon_url=ctx.author.display_avatar.url
        )
        if ctx.guild and ctx.guild.icon:
            e.set_thumbnail(url=ctx.guild.icon.url)
        file = None
        if len(formatted_tb) > 3000:
            file = discord.File(BytesIO(
                formatted_tb.strip("`\n").replace("python", "", 1).encode()),
                filename="error.txt"
            )
        else:
            e.description += f"\n\n{formatted_tb}"

        # Check to make sure the error isn't already logged
        async for msg in channel.history(limit=16):
            for embed in msg.embeds:
                if not embed.author or not embed.author.name:
                    continue
                if embed.author.name == e.author.name:
                    return

        # Send the logged error out
        message = await channel.send(embed=e, file=file)
        await message.add_reaction("✔")
        self.notifs[message.id] = ctx.author.id
        while len(self.notifs) > self.notification_limit:
            self.notifs.pop(next(iter(self.notifs)))

        if ctx.author.id in self.bot.owner_ids:
            e = discord.Embed(color=colors.fate)
            e.set_author(
                name=f"Here's the full traceback:", icon_url=ctx.author.display_avatar.url
            )
            e.set_thumbnail(url=self.bot.user.display_avatar.url)
            e.description = full_tb
            await ctx.send(embed=e)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, data):
        """ Dismisses an error whence fixed """
        if str(data.emoji) == "✔" and data.channel_id == self.bot.config["command_errors"]:
            if (user := self.bot.get_user(data.user_id)) and not user.bot:
                delay = None
                try:
                    # Fetch the message object
                    channel = self.bot.get_channel(data.channel_id)
                    msg = await channel.fetch_message(data.message_id)  # type: discord.Message

                    # Check the embeds for exception embeds
                    for embed in msg.embeds:
                        dump_channel = self.bot.get_channel(self.bot.config["dump_channel"])
                        await dump_channel.send("Error Dismissed", embed=embed)

                        # If possible tell the author it was fixed
                        if author := self.bot.get_user(self.notifs.get(msg.id, None)):
                            e = discord.Embed(color=colors.green)
                            description = embed.description
                            if len(description) > 128:
                                description = description[:128] + "..."
                            e.description = f"**Command you used:** {description}"
                            with suppress(Exception):
                                await author.send(f"A problem you encountered was fixed", embed=e)
                                await channel.send(
                                    "DM'd the user that the problem was fixed",
                                    reference=msg,
                                    delete_after=5
                                )
                                delay = 5
                        self.notifs.pop(msg.id, None)
                except (AttributeError, discord.errors.NotFound):
                    pass
                else:
                    await msg.delete(delay=delay)

    async def suppress_key_error(self, ctx, error) -> None:
        """ Handle KeyError's from modifying menus """
        error = unwrap_original(error)
        if isinstance(error, KeyError):
            await ctx.send("It seems something we're operating on was deleted")
        else:
            await self.on_command_error(ctx, error, ignore_if_handler=False)  # type: ignore


async def setup(bot: fate.Fate):
    await bot.add_cog(ErrorHandler(bot), override=True)
