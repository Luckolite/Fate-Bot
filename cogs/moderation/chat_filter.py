"""
cogs.moderation.chatfilter
~~~~~~~~~~~~~~~~~~~~~~~~~~~

A cog for filtering out messages containing filtered words

:copyright: (C) 2020-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import io
import json
import re
from contextlib import suppress
from functools import lru_cache
from string import printable
from time import time
from typing import Dict, Optional, Set, Tuple, Union
from unicodedata import normalize

import discord
from discord import NotFound, Forbidden, app_commands
from discord import ui, Message, Member
from discord.ext import commands, tasks

from botutils import colors, CancelButton, findall, GetConfirmation, find_links

try:
    from re import _parser as sre_parse
except ImportError:  # Python 3.10 compatibility.
    import sre_parse

aliases = {
    "a": ["@"],
    "i": ['1', 'l', r'\|', "!", "/", r"\*", ";"],
    "o": ["0", "@", "ø", "о"],
    "x": ["х"],
    "y": ["у"]
}
lang_aliases = {
    "x": ["х"],
    "y": ["у"]
}
esc = "\\"


presets = {
    "All Links (Needs Regex Enabled)": [
                r"((https?://)|(www\.)|(discord\.gg/))[a-zA-Z0-9./]+"
    ],
    "Advertising Links": [
        "youtube.com", "youtu.be", "discord.gg", "invite.gg", "discord.invite", "discord.com"
    ],
    "Offensive Slurs": [
        "nigger", "nigga", "fag", "faggot", "spic", "coon", "chink", "transvestite", "tranny", "retard"
    ]
}

DEFAULT_CONFIG = {
    "toggle": False,
    "blacklist": [],
    "whitelist": [],
    "ignored": [],
}

FILTER_OPTIONS = {
    "regex": ("Regex matching", "Catch obfuscated and pattern-based phrases"),
    "filter_nicks": ("Filter nicknames", "Reset nicknames containing blocked phrases"),
    "bots": ("Filter bots", "Apply the filter to messages sent by bots"),
    "webhooks": ("Resend cleaned messages", "Repost messages with blocked content masked"),
    "phishing": ("Phishing protection", "Block links on the phishing domain list"),
}


@lru_cache(maxsize=1024)
def safe_regex_query(pattern: str) -> Optional[str]:
    """Build the legacy wildcard query and reject costly regex structures."""
    query = pattern.replace("*", "{0,16}").replace("+", "{1,16}").replace("\n", "")
    for match in re.findall(r"{([0-9]+), ?([0-9]+)}", query):
        minimum, maximum = map(int, match)
        if minimum > maximum or maximum > 16:
            return None
    if re.search(r"\\[1-9]", query):
        return None
    try:
        parsed = sre_parse.parse(query)
    except re.error:
        return None

    repeat_ops = {
        value
        for name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")
        if (value := getattr(sre_parse, name, None)) is not None
    }
    forbidden_ops = {
        value
        for name in ("ASSERT", "ASSERT_NOT", "GROUPREF", "GROUPREF_EXISTS")
        if (value := getattr(sre_parse, name, None)) is not None
    }

    def walk(tokens, *, inside_repeat: bool = False) -> bool:
        for operation, argument in tokens:
            if operation in forbidden_ops:
                return False
            if operation in repeat_ops:
                minimum, maximum, child = argument
                if inside_repeat or maximum > 16 or minimum > maximum:
                    return False
                if not walk(child, inside_repeat=True):
                    return False
            elif operation is getattr(sre_parse, "SUBPATTERN", object()):
                if not walk(argument[-1], inside_repeat=inside_repeat):
                    return False
            elif operation is getattr(sre_parse, "BRANCH", object()):
                if any(
                    not walk(branch, inside_repeat=inside_repeat)
                    for branch in argument[1]
                ):
                    return False
        return True

    return query if walk(parsed) else None



class ChatFilter(commands.Cog):
    def __init__(self, bot):
        if not hasattr(bot, "filtered_messages"):
            bot.filtered_messages = {}
        self.bot = bot
        self.config = bot.utils.cache("chatfilter")
        self.chatfilter_usage = self._chatfilter
        self.webhooks: Dict[int, discord.Webhook] = {}

        self.phishing_urls: Set[str] = set()

    async def cog_load(self):
        changed = False
        for config in self.config.values():
            if self.normalize_config(config):
                changed = True
        if changed:
            await self.config.flush()
        self.update_urls_task.start()

    async def cog_unload(self):
        self.update_urls_task.cancel()
        self.webhooks.clear()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        self.webhooks.pop(channel.id, None)

    @tasks.loop(hours=1)
    async def update_urls_task(self):
        try:
            raw = await asyncio.wait_for(
                self.bot.get_resource(
                    "https://phish.sinking.yachts/v2/all",
                    label="phishing protection list",
                ),
                timeout=10,
            )
            domains = json.loads(raw)
            if not isinstance(domains, list):
                raise ValueError("phishing protection returned an invalid list")
            self.phishing_urls = {
                domain.casefold().rstrip(".")
                for domain in domains
                if isinstance(domain, str) and domain
            }
        except Exception as error:
            self.bot.log.warning(f"Couldn't refresh phishing protection: {error}")

    @update_urls_task.before_loop
    async def before_update_urls_task(self):
        await self.bot.wait_until_ready()

    def is_enabled(self, guild_id: int) -> bool:
        """ Denotes whether or not the modules enabled in a specific guild """
        config = self.config.get(guild_id)
        return bool(config and config.get("toggle"))

    @staticmethod
    def normalize_config(config: dict) -> bool:
        """Fill missing settings and discard malformed collection values."""
        changed = False
        if not isinstance(config.get("toggle"), bool):
            config["toggle"] = bool(config.get("toggle", True))
            changed = True
        for key in ("blacklist", "whitelist"):
            value = config.get(key)
            normalized = []
            if isinstance(value, list):
                for phrase in value:
                    if not isinstance(phrase, str) or not phrase.strip():
                        changed = True
                        continue
                    phrase = phrase.strip()
                    if phrase not in normalized:
                        normalized.append(phrase)
                    else:
                        changed = True
            if normalized != value:
                config[key] = normalized
                changed = True
        ignored = config.get("ignored")
        normalized_ignored = []
        if isinstance(ignored, list):
            for object_id in ignored:
                try:
                    object_id = int(object_id)
                except (TypeError, ValueError):
                    changed = True
                    continue
                if object_id not in normalized_ignored:
                    normalized_ignored.append(object_id)
                else:
                    changed = True
        if normalized_ignored != ignored:
            config["ignored"] = normalized_ignored
            changed = True
        return changed

    async def get_config(self, guild_id: int, *, create: bool = False):
        config = self.config.get(guild_id)
        if config is None and create:
            config = {
                key: value.copy() if isinstance(value, list) else value
                for key, value in DEFAULT_CONFIG.items()
            }
            self.config[guild_id] = config
            await self.config.flush()
        elif config is not None and self.normalize_config(config):
            await self.config.flush()
        return config

    async def set_enabled(self, guild: discord.Guild, enabled: bool) -> bool:
        config = await self.get_config(guild.id)
        created = config is None and enabled
        if created:
            config = await self.get_config(guild.id, create=True)
        if config is None:
            return False
        changed = created or config.get("toggle") is not enabled
        config["toggle"] = enabled
        await self.config.flush()
        if not changed:
            return False
        try:
            await self.bot.create_log(
                message=f"!{'on' if enabled else 'off'} **Chat Filter** - `{guild}`",
                channel="module_log",
                embedded=True,
                color=colors.green if enabled else colors.red,
            )
        except Exception as error:
            self.bot.log.critical(
                f"Chatfilter changed state, but its internal log failed: {error}"
            )
        return True

    async def clean_content(self, content: str, flag: str) -> str:
        """ Sanitizes content to be resent with the flag filtered out """
        flag = flag.rstrip(" ")
        for chunk in content.split():
            if flag.lower() in chunk.lower():
                filtered_word = f"{flag[0]}{f'{esc}*' * (len(chunk) - 1)}"
                content = content.lower().replace(chunk.lower(), filtered_word.lower())
        for line in content.split("\n"):
            for _ in range(50):
                await asyncio.sleep(0)
                if content.count(line) > 1:
                    content = content.replace(line + line, line)
                else:
                    break
        return content

    async def run_default_filter(self, guild_id: int, content: str) -> Tuple[Optional[str], Optional[list]]:
        """ Filters the content of a message without using regex to prevent false flags """
        if guild_id not in self.config:
            return None, None
        if self.normalize_config(self.config[guild_id]):
            await self.config.flush()
        content = "".join(c for c in content if c in printable)
        content = normalize('NFKD', content).encode('ascii', 'ignore').decode()
        for letter, alts in lang_aliases.items():
            await asyncio.sleep(0)
            for alias in alts:
                content = content.replace(alias, letter)
        if results := await findall(" +[a-zA-Z] +", content):
            for result in results:
                await asyncio.sleep(0)
                content = content.replace(result, result.replace(result, result.strip()))
        if esc in content:
            content = content.replace("\\", "")
        lowered_content = content.lower()
        content_words = set(lowered_content.replace("\n", "").split())
        allowed_phrases = {
            allowed.casefold() for allowed in self.config[guild_id]["whitelist"]
        }
        for phrase in self.config[guild_id]["blacklist"]:
            await asyncio.sleep(0)
            normalized_phrase = phrase.casefold()
            if normalized_phrase in allowed_phrases:
                continue
            if " " in phrase and phrase.lower() in lowered_content:
                return await self.clean_content(content, phrase), [phrase]
            if normalized_phrase in content_words:
                return await self.clean_content(content, phrase), [phrase]
        return None, None

    async def run_regex_filter(self, guild_id: int, content: str) -> Tuple[Optional[str], Optional[list]]:
        """ A more thorough filter to better flag bypasses """
        if guild_id not in self.config:
            return None, None
        if self.normalize_config(self.config[guild_id]):
            await self.config.flush()

        def run_regex(query: str) -> Optional[str]:
            result = re.search(query, search_content)
            if result:
                return result.group()
            return None

        illegal = ("\\", "`", "__", "||", "~~")
        content = str(content).lower()
        for char in illegal:
            content = content.replace(char, "")
        content = normalize('NFKD', content).encode('ascii', 'ignore').decode()
        content = "".join(c for c in content if c in printable)
        search_content = content.replace(" ", "")

        flags = []
        removed_bad_pattern = False
        allowed_phrases = {
            allowed.casefold() for allowed in self.config[guild_id]["whitelist"]
        }
        for pattern in list(self.config[guild_id]["blacklist"]):
            await asyncio.sleep(0)

            query = safe_regex_query(pattern)
            if query is None:
                self.bot.log.critical(f"Removing unsafe regex: {pattern}")
                with suppress(ValueError):
                    self.config[guild_id]["blacklist"].remove(pattern)
                    removed_bad_pattern = True
                continue

            try:
                if result := await asyncio.wait_for(
                    self.bot.loop.run_in_executor(None, run_regex, query), timeout=0.25
                ):
                    for token in content.split():
                        await asyncio.sleep(0)
                        if result in token and token.casefold() not in allowed_phrases:
                            flags.append(result)
                            break
            except (re.error, asyncio.TimeoutError):
                self.bot.log.critical(f"Removing invalid or slow regex: {pattern}")
                with suppress(ValueError):
                    self.config[guild_id]["blacklist"].remove(pattern)
                    removed_bad_pattern = True

        if removed_bad_pattern:
            await self.config.flush()

        if not flags:
            return None, flags

        for flag in flags:
            content = await self.clean_content(content, flag)

        return content, flags

    @commands.hybrid_group(
        name="chatfilter",
        fallback="view",
        invoke_without_command=True,
        description="Configures the server's message filter",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(embed_links=True, manage_messages=True)
    @app_commands.default_permissions(manage_messages=True)
    async def _chatfilter(self, ctx: commands.Context):
        """Open the interactive chat-filter dashboard."""
        await ctx.defer()
        await ChatFilterMenu(self, ctx).start()

    @_chatfilter.command(name="enable", description="Enables the module")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def _enable(self, ctx):
        config = await self.get_config(ctx.guild.id)
        if config and config["toggle"]:
            return await ctx.send("Chatfilter is already enabled")
        await self.set_enabled(ctx.guild, True)
        await ctx.send("Enabled chatfilter")

    @_chatfilter.command(name="disable", description="Disables the module")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def _disable(self, ctx):
        if ctx.guild.id not in self.config:
            return await ctx.send("Chatfilter is not enabled")
        if not self.config[ctx.guild.id].get("toggle"):
            return await ctx.send("Chatfilter is already disabled")
        await self.set_enabled(ctx.guild, False)
        await ctx.send("Disabled chatfilter")

    @_chatfilter.command(
        name="ignore",
        description="Has chatfilter ignore a channel, member, or role",
        with_app_command=False,
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def _ignore(self, ctx, *targets: Union[discord.User, discord.Role, discord.TextChannel]):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("Chatfilter isn't enabled")
        passed = []
        for object in targets:
            if object.id in self.config[guild_id]["ignored"]:
                await ctx.send(f"{object} is already ignored")
                continue
            self.config[guild_id]["ignored"].append(object.id)
            passed.append(object.mention)
        if passed:
            await ctx.send(f"I'll now ignore {', '.join(passed)}")
            await self.config.flush()

    @_chatfilter.command(
        name="unignore",
        description="Stops ignoring a channel, member, or role",
        with_app_command=False,
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def _unignore(self, ctx, *targets: Union[discord.User, discord.Role, discord.TextChannel]):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("This server has no ignored targets")
        passed = []
        for target in targets:
            if target.id not in self.config[guild_id]["ignored"]:
                await ctx.send(f"{target.mention} isn't ignored")
                continue
            self.config[guild_id]["ignored"].remove(target.id)
            passed.append(target.mention)
        if passed:
            await ctx.send(f"I'll no longer ignore {', '.join(passed)}")
            await self.config.flush()

    @_chatfilter.command(name="add", aliases=["blacklist"], description="Adds a word, or phrase to the filter")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def _add(self, ctx, *, phrases: str):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("Chatfilter isn't enabled")
        if len(phrases) > 256:
            return await ctx.send("That's too large to add")
        for phrase in phrases.split(", ")[:16]:
            if len(phrase) > 64:
                return await ctx.send("That's too large to add")
            if phrase in self.config[guild_id]["blacklist"]:
                await ctx.send(f"`{phrase}` is already blacklisted")
                continue
            self.config[guild_id]["blacklist"].append(phrase)
            await ctx.send(f"Added `{phrase}`")
            await asyncio.sleep(1)
        await self.config.flush()

    @_chatfilter.command(name="whitelist", description="Adds a word, or phrase to the whitelist")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def _whitelist(self, ctx, *, phrases: str):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("Chatfilter isn't enabled")
        if len(phrases) > 256:
            return await ctx.send("That's too large to add")
        for phrase in phrases.split(", ")[:16]:
            if len(phrase) > 64:
                return await ctx.send("That's too large to add")
            if phrase in self.config[guild_id]["whitelist"]:
                await ctx.send(f"`{phrase}` is already whitelisted")
                continue
            self.config[guild_id]["whitelist"].append(phrase)
            await ctx.send(f"Added `{phrase}`")
            await asyncio.sleep(1)
        await self.config.flush()

    @_chatfilter.command(name="unwhitelist", description="Removes a word, or phrase from the whitelist")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def _unwhitelist(self, ctx, *, phrases: str):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("Chatfilter isn't enabled")
        if len(phrases) > 256:
            return await ctx.send("That's too large to add")
        for phrase in phrases.split(", ")[:16]:
            if len(phrase) > 64:
                return await ctx.send("That's too large to add")
            if phrase not in self.config[guild_id]["whitelist"]:
                return await ctx.send(f"`{phrase}` isn't whitelisted")
            self.config[guild_id]["whitelist"].remove(phrase)
            await ctx.send(f"Removed `{phrase}`")
            await asyncio.sleep(1)
        await self.config.flush()

    @_chatfilter.command(name="remove", description="Removes a word, or phrase from the filter")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def _remove(self, ctx, *, phrase: str):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("Chatfilter isn't enabled")
        if phrase not in self.config[guild_id]["blacklist"] and not phrase.endswith("*") and not phrase.startswith("*"):
            return await ctx.send("Phrase/word not found")
        removed = []
        if phrase.endswith("*"):
            phrase = phrase.rstrip("*")
            for word in list(self.config[guild_id]["blacklist"]):
                _word = normalize('NFKD', word).encode('ascii', 'ignore').decode().lower()
                if _word.startswith(phrase):
                    self.config[guild_id]["blacklist"].remove(word)
                    removed.append(word)
            if not removed:
                return await ctx.send("No phrase/words found matching that")
            await ctx.send(f"Removed {', '.join(f'`{w}`' for w in removed)}")
            return await self.config.flush()
        if phrase.startswith("*"):
            phrase = phrase.lstrip("*")
            for word in list(self.config[guild_id]["blacklist"]):
                _word = normalize('NFKD', word).encode('ascii', 'ignore').decode().lower()
                if _word.endswith(phrase):
                    self.config[guild_id]["blacklist"].remove(word)
                    removed.append(word)
            if not removed:
                return await ctx.send("No phrase/words found matching that")
            await ctx.send(f"Removed {', '.join(f'`{w}`' for w in removed)}")
            return await self.config.flush()
        self.config[guild_id]["blacklist"].remove(phrase)
        await ctx.send(f"Removed `{phrase}`")
        await self.config.flush()

    @_chatfilter.command(name="clear", description="Removes every phrase from the filter")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def _clear(self, ctx):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("Chatfilter isn't enabled")
        blacklist = self.config[guild_id]["blacklist"]
        if not blacklist:
            return await ctx.send("Chatfilter's blacklist is already empty")
        if not await GetConfirmation(
            ctx, f"Remove all {len(blacklist)} phrases from chatfilter?"
        ):
            return await ctx.send("Cancelled")
        removed = len(blacklist)
        blacklist.clear()
        await self.config.flush()
        await ctx.send(f"Cleared {removed} phrase{'s' if removed != 1 else ''} from chatfilter")

    @_chatfilter.command(name="sanitize", description="Filters old/existing messages")
    @commands.cooldown(1, 120, commands.BucketType.user)
    @commands.has_permissions(administrator=True)
    @commands.cooldown(1, 60, commands.BucketType.guild)
    async def sanitize(self, ctx, amount: int):
        """ Clean up existing chat history """
        config = await self.get_config(ctx.guild.id)
        if not config or not config.get("toggle"):
            return await ctx.send("Enable chatfilter before sanitizing message history.")
        if amount < 1:
            return await ctx.send("Choose at least one message to scan.")
        view = CancelButton("manage_messages")
        message = await ctx.send("🖨️ **Sanitizing chat history**", view=view)
        if amount > 1000 and ctx.author.id not in self.bot.owner_ids:
            amount = 1000

        # Create a listener to let the code know if the original message was deleted
        task: asyncio.Task = self.bot.loop.create_task(self.bot.wait_for(
            "message_delete",
            check=lambda m: m.id == message.id,
            timeout=900
        ))
        owner_task = asyncio.current_task()
        if owner_task:
            owner_task.add_done_callback(
                lambda _finished: task.cancel() if not task.done() else None
            )

        method = self.run_default_filter
        if self.config[ctx.guild.id].get("regex"):
            method = self.run_regex_filter

        # Status variables
        scanned = 0
        deleted = []
        last_update = time()
        last_user = None

        async for msg in message.channel.history(limit=amount):
            await asyncio.sleep(0)
            if view.is_cancelled or not message or task.done():
                return
            scanned += 1

            # Check for flags
            _content, flags = await method(ctx.guild.id, msg.content)
            if flags:
                with suppress(NotFound, Forbidden):
                    await msg.delete()
                    date = msg.created_at.strftime("%m/%d/%Y %I:%M:%S%p")
                    newline = "" if last_user == msg.author.id else "\n"
                    deleted.append(f"{newline}{date} - {msg.author} - {msg.content}")
                    last_user = msg.author.id

            # Update the message every 5 seconds
            if time() - 5 > last_update:
                await message.edit(
                    content=f"🖨️ **Sanitizing chat history**\n"
                            f"{scanned} messages scanned\n"
                            f"{len(deleted)} messages deleted"
                )
                last_update = time()

        try:
            # Stop listening for button presses after scanning channel history
            view.stop()
            task.cancel()
            await message.edit(
                content=message.content.replace("Sanitizing", "Sanitized"),
                view=None
            )

            if deleted:
                report = io.BytesIO("\n".join(deleted).encode("utf-8"))
                await ctx.send(
                    "Operation finished; this report will be deleted in a minute.",
                    reference=message,
                    file=discord.File(report, filename="filtered-messages.txt"),
                    delete_after=60,
                )
        except (discord.errors.HTTPException, NotFound):
            await ctx.send("The message I was using got deleted, so I can't proceed")

    async def get_webhook(self, channel):
        if channel.id not in self.webhooks:
            webhooks = await channel.webhooks()
            for wh in webhooks:
                if wh.name == "Chatfilter":
                    webhook = wh
                    break
            else:
                webhook = await channel.create_webhook(name="Chatfilter")
            if channel.id not in self.webhooks:
                self.webhooks[channel.id] = webhook
            else:
                await webhook.delete()
                return self.webhooks[channel.id]
        return self.webhooks[channel.id]

    @commands.Cog.listener()
    async def on_message(self, msg: Message) -> None:
        if isinstance(msg.author, discord.Member) and msg.guild.id in self.config:
            guild_id = msg.guild.id
            if not self.config[guild_id]["toggle"]:
                return
            if str(msg.author).endswith("#0000"):
                return
            if self.bot.attrs.is_moderator(msg.author) and not msg.author.bot:
                return
            if msg.author.bot and not self.config[guild_id].get("bots"):
                return
            ignored = self.config[guild_id]["ignored"]
            if msg.channel.id in ignored or msg.author.id in ignored:
                return
            if any(r.id in ignored for r in msg.author.roles):
                return

            result = None
            flags = []
            if self.config[guild_id].get("phishing"):
                if links := await find_links(msg.content):
                    for link in links:
                        await asyncio.sleep(0)
                        domain = link.partition("/")[0].casefold().rstrip(".")
                        if domain in self.phishing_urls:
                            result = (result or msg.content).replace(
                                link, "**phishing-link**"
                            )
                            flags.append(link)

            if not result:
                if self.config[guild_id].get("regex"):
                    result, flags = await self.run_regex_filter(guild_id, msg.content)
                else:
                    result, flags = await self.run_default_filter(guild_id, msg.content)
                if not result:
                    return
            try:
                await msg.delete()
            except (NotFound, Forbidden, discord.HTTPException):
                return
            self.bot.telemetry.increment("chatfilter_triggers")

            # Mark a message_id as a filtered message
            self.bot.suppressed.append(msg.id)
            if guild_id not in self.bot.filtered_messages:
                self.bot.filtered_messages[guild_id] = {}
            self.bot.filtered_messages[guild_id][msg.id] = time()

            # Tell the logger module that a message was deleted by chatfilter.
            if logger := self.bot.get_cog("Logger"):
                try:
                    await logger.on_message_filter(msg, flags)
                except Exception as error:
                    self.bot.log.warning(f"Chatfilter couldn't notify Logger: {error}")

            # Resend the message with blocked content masked.
            if self.config[guild_id].get("webhooks"):
                if msg.channel.permissions_for(msg.guild.me).manage_webhooks:
                    try:
                        w = await self.get_webhook(msg.channel)
                        await w.send(
                            content=result,
                            avatar_url=msg.author.display_avatar.url,
                            username=msg.author.display_name,
                        )
                    except (Forbidden, discord.HTTPException) as error:
                        self.bot.log.warning(
                            f"Chatfilter couldn't resend in {msg.channel.id}: {error}"
                        )

            # Check their nickname independently of logger/webhook failures.
            with suppress(Forbidden, discord.HTTPException):
                await self.on_member_update(None, msg.author)

    @commands.Cog.listener()
    async def on_message_edit(self, _before: Message, after: Message) -> None:
        """ Check edited messages for added filtered words, or phrases """
        await self.on_message(after)

    @commands.Cog.listener()
    async def on_member_update(self, _before: Optional[Member], after: Member) -> None:
        """ Resets a members nickname if it has a filtered word, or phrase """
        if after.nick and after.guild.id in self.config:
            if (bot := after.guild.me) and bot.guild_permissions.manage_nicknames:
                if bot.top_role.position > after.top_role.position:
                    guild_id = after.guild.id
                    if not self.config[guild_id]["toggle"]:
                        return
                    if not self.config[guild_id].get("filter_nicks"):
                        return
                    if self.bot.attrs.is_moderator(after) and not after.bot:
                        return
                    if after.bot and not self.config[guild_id].get("bots"):
                        return
                    if self.config[guild_id].get("regex"):
                        result, flags = await self.run_regex_filter(guild_id, after.nick)
                    else:
                        result, flags = await self.run_default_filter(guild_id, after.nick)
                    if not result:
                        return
                    await after.edit(nick=None, reason=f"Chatfilter flagged their nick for '{', '.join(flags)}'")


class ChatFilterMenu(ui.View):
    def __init__(self, cog: ChatFilter, ctx: commands.Context):
        super().__init__(timeout=180)
        self.cog = cog
        self.ctx = ctx
        self.guild = ctx.guild
        self.user = ctx.author
        self.config = None
        self.message: Optional[discord.Message] = None
        self.notice: Optional[str] = None

    async def start(self):
        await self.refresh()
        self.message = await self.ctx.send(embed=self.embed, view=self)

    async def refresh(self):
        stored = await self.cog.get_config(self.guild.id)
        self.config = stored or {
            key: value.copy() if isinstance(value, list) else value
            for key, value in DEFAULT_CONFIG.items()
        }
        if stored is None:
            self.config["toggle"] = False
        self.embed = self.build_embed()
        self.clear_items()
        self.add_item(FilterSettingsSelect(self))
        if self.config["blacklist"]:
            self.add_item(RemovePhraseSelect(self, allowlist=False))
        if self.config["whitelist"]:
            self.add_item(RemovePhraseSelect(self, allowlist=True))
        self.add_item(OpenPhraseModalButton(self, allowlist=False))
        self.add_item(OpenPhraseModalButton(self, allowlist=True))
        self.add_item(OpenIgnoredTargetsButton(self))
        self.add_item(OpenImportButton(self))
        self.add_item(ToggleChatFilterButton(self))
        if self.config["blacklist"]:
            self.add_item(ClearBlockedPhrasesButton(self))
        self.add_item(CloseChatFilterMenu(self))

    async def refresh_interaction(self, interaction: discord.Interaction):
        await self.refresh()
        self.message = await interaction.edit_original_response(
            embed=self.embed,
            view=self,
        )

    async def refresh_message(self):
        await self.refresh()
        if self.message:
            await self.message.edit(embed=self.embed, view=self)

    def build_embed(self):
        enabled = bool(self.config.get("toggle"))
        embed = discord.Embed(
            title="Chat Filter",
            description=(
                "Block words and phrases, allow intentional exceptions, and choose "
                "where the filter applies. Use the controls below to update it live."
            ),
            color=colors.green if enabled else colors.fate,
        )
        if self.guild.icon:
            embed.set_thumbnail(url=self.guild.icon.url)
        embed.add_field(
            name="Status",
            value="🟢 **Enabled**" if enabled else "⚪ **Disabled**",
        )
        embed.add_field(
            name="Coverage",
            value=(
                f"{len(self.config['blacklist']):,} blocked · "
                f"{len(self.config['whitelist']):,} allowed · "
                f"{len(self.config['ignored']):,} ignored"
            ),
        )
        settings = [
            f"{'🟢' if self.config.get(key) else '⚫'} {label}"
            for key, (label, _description) in FILTER_OPTIONS.items()
        ]
        embed.add_field(name="Options", value="\n".join(settings), inline=False)
        embed.add_field(
            name=f"Blocked phrases ({len(self.config['blacklist']):,})",
            value=self._phrase_preview(self.config["blacklist"]),
            inline=False,
        )
        if self.config["whitelist"]:
            embed.add_field(
                name=f"Allowed exceptions ({len(self.config['whitelist']):,})",
                value=self._phrase_preview(self.config["whitelist"]),
                inline=False,
            )
        ignored = self._ignored_preview()
        if ignored:
            embed.add_field(name="Ignored targets", value=ignored, inline=False)
        embed.set_footer(
            text=self.notice or "Select an option or add a phrase to get started."
        )
        return embed

    @staticmethod
    def _phrase_preview(phrases: list) -> str:
        if not phrases:
            return "No phrases configured yet."
        visible = [
            f"`{discord.utils.escape_markdown(str(phrase))[:80]}`"
            for phrase in phrases[:8]
        ]
        if len(phrases) > len(visible):
            visible.append(f"*...and {len(phrases) - len(visible):,} more*.")
        return ", ".join(visible)

    def _ignored_preview(self) -> Optional[str]:
        visible = []
        for object_id in self.config["ignored"][:8]:
            target = (
                self.guild.get_channel(object_id)
                or self.guild.get_member(object_id)
                or self.guild.get_role(object_id)
            )
            visible.append(target.mention if target else f"Deleted target (`{object_id}`)")
        if len(self.config["ignored"]) > len(visible):
            visible.append(f"*...and {len(self.config['ignored']) - len(visible):,} more*.")
        return "\n".join(visible) if visible else None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "Only the moderator who opened this menu can use it.", ephemeral=True
            )
            return False
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "You need Manage Messages to configure chatfilter.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)


class FilterSettingsSelect(ui.Select):
    def __init__(self, menu: ChatFilterMenu):
        self.menu = menu
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                description=description,
            )
            for key, (label, description) in FILTER_OPTIONS.items()
        ]
        super().__init__(
            placeholder="Filter options · choose one to toggle",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        config = await self.menu.cog.get_config(self.menu.guild.id, create=True)
        if key == "regex" and not config.get("regex"):
            invalid = [
                phrase
                for phrase in config["blacklist"]
                if safe_regex_query(phrase) is None
            ]
            if invalid:
                return await interaction.response.send_message(
                    "Remove invalid patterns before enabling regex matching. "
                    f"First invalid entry: `{discord.utils.escape_markdown(invalid[0])}`",
                    ephemeral=True,
                )
        config[key] = not bool(config.get(key))
        await self.menu.cog.config.flush()
        label = FILTER_OPTIONS[key][0]
        self.menu.notice = f"{label} {'enabled' if config[key] else 'disabled'}."
        await interaction.response.defer()
        await self.menu.refresh_interaction(interaction)


class RemovePhraseSelect(ui.Select):
    def __init__(self, menu: ChatFilterMenu, *, allowlist: bool):
        self.menu = menu
        self.allowlist = allowlist
        key = "whitelist" if allowlist else "blacklist"
        self.phrases = menu.config[key][:25]
        options = [
            discord.SelectOption(
                label=str(phrase)[:100],
                value=str(index),
                description="Remove allowed exception" if allowlist else "Remove blocked phrase",
            )
            for index, phrase in enumerate(self.phrases)
        ]
        super().__init__(
            placeholder=(
                "Remove an allowed exception" if allowlist else "Remove a blocked phrase"
            ),
            options=options,
            row=2 if allowlist else 1,
        )

    async def callback(self, interaction: discord.Interaction):
        key = "whitelist" if self.allowlist else "blacklist"
        config = await self.menu.cog.get_config(self.menu.guild.id)
        phrase = self.phrases[int(self.values[0])]
        if config is None or phrase not in config[key]:
            return await interaction.response.send_message(
                "That phrase is no longer configured.", ephemeral=True
            )
        config[key].remove(phrase)
        await self.menu.cog.config.flush()
        self.menu.notice = f"Removed {phrase}."
        await interaction.response.defer()
        await self.menu.refresh_interaction(interaction)


class OpenPhraseModalButton(ui.Button):
    def __init__(self, menu: ChatFilterMenu, *, allowlist: bool):
        self.menu = menu
        self.allowlist = allowlist
        super().__init__(
            label="Allow exceptions" if allowlist else "Block phrases",
            emoji="✅" if allowlist else "🛡️",
            style=discord.ButtonStyle.success if allowlist else discord.ButtonStyle.primary,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PhraseModal(self.menu, self.allowlist))


class PhraseModal(ui.Modal):
    def __init__(self, menu: ChatFilterMenu, allowlist: bool):
        super().__init__(title="Add allowed exceptions" if allowlist else "Add blocked phrases")
        self.menu = menu
        self.allowlist = allowlist
        self.phrases = ui.TextInput(
            label="Words or phrases",
            placeholder="Separate multiple entries with commas or new lines",
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=1024,
        )
        self.add_item(self.phrases)

    async def on_submit(self, interaction: discord.Interaction):
        phrases = [
            phrase.strip()
            for line in self.phrases.value.splitlines()
            for phrase in line.split(",")
            if phrase.strip()
        ]
        phrases = list(dict.fromkeys(phrases))
        if not phrases:
            return await interaction.response.send_message(
                "Enter at least one word or phrase.", ephemeral=True
            )
        if len(phrases) > 16:
            return await interaction.response.send_message(
                "Add up to 16 phrases at a time.", ephemeral=True
            )
        if any(len(phrase) > 64 for phrase in phrases):
            return await interaction.response.send_message(
                "Each phrase must be 64 characters or shorter.", ephemeral=True
            )

        config = await self.menu.cog.get_config(self.menu.guild.id, create=True)
        if not self.allowlist and config.get("regex"):
            unsafe = [phrase for phrase in phrases if safe_regex_query(phrase) is None]
            if unsafe:
                return await interaction.response.send_message(
                    f"This pattern is unsafe or invalid: `{discord.utils.escape_markdown(unsafe[0])}`",
                    ephemeral=True,
                )
        key = "whitelist" if self.allowlist else "blacklist"
        added = [phrase for phrase in phrases if phrase not in config[key]]
        config[key].extend(added)
        await self.menu.cog.config.flush()
        noun = "allowed exception" if self.allowlist else "blocked phrase"
        self.menu.notice = f"Added {len(added):,} {noun}{'s' if len(added) != 1 else ''}."
        await interaction.response.defer()
        await self.menu.refresh_interaction(interaction)


class ToggleChatFilterButton(ui.Button):
    def __init__(self, menu: ChatFilterMenu):
        self.menu = menu
        enabled = bool(menu.config.get("toggle"))
        super().__init__(
            label="Disable filter" if enabled else "Enable filter",
            emoji="⏸️" if enabled else "▶️",
            style=discord.ButtonStyle.secondary if enabled else discord.ButtonStyle.success,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        enabled = not bool(self.menu.config.get("toggle"))
        await interaction.response.defer()
        await self.menu.cog.set_enabled(self.menu.guild, enabled)
        self.menu.notice = f"Chatfilter {'enabled' if enabled else 'disabled'}."
        await self.menu.refresh_interaction(interaction)


class ClearBlockedPhrasesButton(ui.Button):
    def __init__(self, menu: ChatFilterMenu):
        self.menu = menu
        super().__init__(
            label="Clear all",
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"Remove all {len(self.menu.config['blacklist']):,} blocked phrases?",
            view=ConfirmClearView(self.menu),
            ephemeral=True,
        )


class ConfirmClearView(ui.View):
    def __init__(self, menu: ChatFilterMenu):
        super().__init__(timeout=45)
        self.menu = menu

    @ui.button(label="Clear all", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: ui.Button):
        config = await self.menu.cog.get_config(self.menu.guild.id)
        removed = len(config["blacklist"]) if config else 0
        if config:
            config["blacklist"].clear()
            await self.menu.cog.config.flush()
        self.menu.notice = f"Cleared {removed:,} blocked phrase{'s' if removed != 1 else ''}."
        await self.menu.refresh_message()
        await interaction.response.edit_message(content=self.menu.notice, view=None)
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.menu.interaction_check(interaction)


class OpenIgnoredTargetsButton(ui.Button):
    def __init__(self, menu: ChatFilterMenu):
        self.menu = menu
        super().__init__(
            label="Ignored targets",
            emoji="🎯",
            style=discord.ButtonStyle.secondary,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Select channels, members, or roles to toggle whether chatfilter ignores them.",
            view=IgnoredTargetsView(self.menu),
            ephemeral=True,
        )


class IgnoredTargetsView(ui.View):
    def __init__(self, menu: ChatFilterMenu):
        super().__init__(timeout=90)
        self.menu = menu
        self.add_item(IgnoredChannelSelect(menu))
        self.add_item(IgnoredMentionableSelect(menu))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.menu.interaction_check(interaction)


async def toggle_ignored(menu: ChatFilterMenu, interaction: discord.Interaction, targets):
    config = await menu.cog.get_config(menu.guild.id, create=True)
    added = 0
    removed = 0
    for target in targets:
        if target.id in config["ignored"]:
            config["ignored"].remove(target.id)
            removed += 1
        else:
            config["ignored"].append(target.id)
            added += 1
    await menu.cog.config.flush()
    menu.notice = f"Ignored targets updated: {added:,} added, {removed:,} removed."
    await menu.refresh_message()
    await interaction.response.edit_message(content=menu.notice, view=IgnoredTargetsView(menu))


class IgnoredChannelSelect(ui.ChannelSelect):
    def __init__(self, menu: ChatFilterMenu):
        self.menu = menu
        super().__init__(
            placeholder="Toggle ignored channels",
            channel_types=[discord.ChannelType.text, discord.ChannelType.forum],
            min_values=1,
            max_values=25,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        await toggle_ignored(self.menu, interaction, self.values)


class IgnoredMentionableSelect(ui.MentionableSelect):
    def __init__(self, menu: ChatFilterMenu):
        self.menu = menu
        super().__init__(
            placeholder="Toggle ignored members or roles",
            min_values=1,
            max_values=25,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        await toggle_ignored(self.menu, interaction, self.values)


class OpenImportButton(ui.Button):
    def __init__(self, menu: ChatFilterMenu):
        self.menu = menu
        super().__init__(
            label="Import rules",
            emoji="📥",
            style=discord.ButtonStyle.secondary,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Choose a preset or another server to import blocked phrases from.",
            view=ModernImportView(self.menu, interaction.user),
            ephemeral=True,
        )


class ModernImportView(ui.View):
    def __init__(self, menu: ChatFilterMenu, user: discord.Member):
        super().__init__(timeout=90)
        self.menu = menu
        self.add_item(PresetImportSelect(menu))
        servers = [
            guild for guild in user.mutual_guilds
            if guild.id != menu.guild.id and guild.id in menu.cog.config
        ][:25]
        if servers:
            self.add_item(ServerImportSelect(menu, servers))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.menu.interaction_check(interaction)


class PresetImportSelect(ui.Select):
    def __init__(self, menu: ChatFilterMenu):
        self.menu = menu
        super().__init__(
            placeholder="Import a preset",
            options=[
                discord.SelectOption(label=name[:100], value=name)
                for name in presets
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        await import_phrases(self.menu, interaction, presets[self.values[0]])


class ServerImportSelect(ui.Select):
    def __init__(self, menu: ChatFilterMenu, servers: list):
        self.menu = menu
        super().__init__(
            placeholder="Import from another server",
            options=[
                discord.SelectOption(label=guild.name[:100], value=str(guild.id))
                for guild in servers
            ],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        guild_id = int(self.values[0])
        source_config = self.menu.cog.config.get(guild_id)
        if not source_config:
            return await interaction.response.send_message(
                "That server no longer has chatfilter configured.",
                ephemeral=True,
            )
        source = source_config.get("blacklist", [])
        await import_phrases(self.menu, interaction, source)


async def import_phrases(menu: ChatFilterMenu, interaction: discord.Interaction, phrases):
    config = await menu.cog.get_config(menu.guild.id, create=True)
    candidates = []
    for phrase in phrases:
        if not isinstance(phrase, str):
            continue
        phrase = phrase.strip()
        if not phrase or len(phrase) > 64 or phrase in candidates:
            continue
        if config.get("regex") and safe_regex_query(phrase) is None:
            continue
        candidates.append(phrase)
    added = [phrase for phrase in candidates if phrase not in config["blacklist"]]
    config["blacklist"].extend(added)
    await menu.cog.config.flush()
    menu.notice = f"Imported {len(added):,} new blocked phrase{'s' if len(added) != 1 else ''}."
    await menu.refresh_message()
    await interaction.response.edit_message(content=menu.notice, view=None)


class CloseChatFilterMenu(ui.Button):
    def __init__(self, menu: ChatFilterMenu):
        self.menu = menu
        super().__init__(
            label="Close",
            emoji="✖️",
            style=discord.ButtonStyle.secondary,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        self.menu.stop()
        await interaction.response.edit_message(view=None)


async def setup(bot):
    await bot.add_cog(ChatFilter(bot), override=True)
