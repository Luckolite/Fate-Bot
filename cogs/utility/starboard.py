"""Configurable, multi-board message highlights."""

from __future__ import annotations

import asyncio
import secrets
import weakref
from contextlib import suppress
from copy import deepcopy
from typing import Any

import discord
from discord.ext import commands

from botutils.starboard import (
    BOARD_ID_RE,
    DEFAULT_EMOJI,
    DEFAULT_THRESHOLD,
    MAX_BOARD_NAME,
    MAX_BOARDS,
    custom_emoji_id,
    emoji_key,
    public_board,
)


class Starboard(commands.Cog):
    """Promote messages when they receive a configured reaction."""

    def __init__(self, bot):
        self.bot = bot
        self.config = bot.utils.cache("starboard", auto_sync=True)
        self.posts = bot.utils.cache("starboard_posts", auto_sync=True)
        self._locks: weakref.WeakValueDictionary[
            tuple[int, str, int], asyncio.Lock
        ] = weakref.WeakValueDictionary()

    def boards_for(self, guild_id: int) -> list[dict[str, Any]]:
        config = self.config.get(guild_id, {})
        boards = config.get("boards", []) if isinstance(config, dict) else []
        return [deepcopy(board) for board in boards if isinstance(board, dict)]

    def public_config(self, guild_id: int) -> dict[str, Any]:
        boards = [public_board(board) for board in self.boards_for(guild_id)]
        return {"enabled": any(board["enabled"] for board in boards), "boards": boards}

    def _prepare_boards(self, guild: discord.Guild, boards) -> list[dict[str, Any]]:
        if not isinstance(boards, list) or len(boards) > MAX_BOARDS:
            raise ValueError(f"Starboard supports at most {MAX_BOARDS} boards per server.")
        prepared = []
        names: set[str] = set()
        emojis: set[str] = set()
        identifiers: set[str] = set()
        for raw in boards:
            if not isinstance(raw, dict):
                raise ValueError("Each starboard must be a settings object.")
            name = str(raw.get("name") or "").strip()
            if not name:
                raise ValueError("Give every starboard a name.")
            if len(name) > MAX_BOARD_NAME:
                raise ValueError(f"Starboard names must be {MAX_BOARD_NAME} characters or fewer.")
            name_key = name.casefold()
            if name_key in names:
                raise ValueError("Starboard names must be unique in this server.")
            names.add(name_key)

            board_id = str(raw.get("board_id") or secrets.token_hex(6))
            if not BOARD_ID_RE.fullmatch(board_id) or board_id in identifiers:
                raise ValueError("That starboard identifier is invalid or duplicated.")
            identifiers.add(board_id)

            try:
                channel_id = int(raw.get("channel_id"))
            except (TypeError, ValueError) as error:
                raise ValueError(f"Choose a destination channel for {name}.") from error
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                raise ValueError(f"The destination for {name} is not an available text channel.")
            permissions = channel.permissions_for(guild.me)
            if not (
                permissions.send_messages
                and permissions.embed_links
                and permissions.read_message_history
            ):
                raise ValueError(
                    f"Fate needs Send Messages, Embed Links, and Read Message History in the {name} destination."
                )

            display_emoji = str(raw.get("emoji") or DEFAULT_EMOJI).strip()
            key = emoji_key(display_emoji)
            if key in emojis:
                raise ValueError("Each starboard must use a different reaction emoji.")
            emojis.add(key)
            if emoji_id := custom_emoji_id(display_emoji):
                resolved = guild.get_emoji(emoji_id) or self.bot.get_emoji(emoji_id)
                if resolved is None:
                    raise ValueError(f"Fate cannot access the custom emoji configured for {name}.")
                display_emoji = str(resolved)

            try:
                threshold = int(raw.get("threshold", DEFAULT_THRESHOLD))
            except (TypeError, ValueError) as error:
                raise ValueError(f"The reaction threshold for {name} must be a whole number.") from error
            if not 1 <= threshold <= 100:
                raise ValueError(f"The reaction threshold for {name} must be between 1 and 100.")

            prepared.append(
                {
                    "board_id": board_id,
                    "name": name,
                    "channel_id": channel.id,
                    "emoji": display_emoji,
                    "emoji_key": key,
                    "threshold": threshold,
                    "enabled": bool(raw.get("enabled", True)),
                }
            )
        return prepared

    async def replace_boards(self, guild: discord.Guild, boards) -> None:
        previous = {board["board_id"]: board for board in self.boards_for(guild.id)}
        prepared = self._prepare_boards(guild, boards)
        self.config[guild.id] = {"boards": prepared}
        await self.config.flush()

        # A destination or emoji change starts a new stream of highlights. Old
        # highlights stay in Discord as history, but are no longer edited.
        active = {board["board_id"]: board for board in prepared}
        stale_ids = {
            board_id
            for board_id, old in previous.items()
            if board_id not in active
            or old.get("channel_id") != active[board_id].get("channel_id")
            or old.get("emoji_key") != active[board_id].get("emoji_key")
        }
        if stale_ids:
            posts = deepcopy(self.posts.get(guild.id, {}))
            posts = {
                key: value
                for key, value in posts.items()
                if key.split(":", 1)[0] not in stale_ids
            }
            self.posts[guild.id] = posts
            await self.posts.flush()

    def _resolve_board(self, guild_id: int, query: str) -> dict[str, Any] | None:
        lookup = query.strip().casefold()
        for board in self.boards_for(guild_id):
            if board.get("board_id", "").casefold() == lookup or board.get("name", "").casefold() == lookup:
                return board
        return None

    @commands.hybrid_group(
        name="starboard",
        aliases=["starboards"],
        description="Manage reaction-powered message highlight boards",
    )
    @commands.guild_only()
    async def starboard(self, ctx: commands.Context):
        if ctx.invoked_subcommand:
            return
        boards = self.public_config(ctx.guild.id)["boards"]
        embed = discord.Embed(
            title="Starboards",
            color=self.bot.config.get("theme_color", 0x5865F2),
        )
        if not boards:
            embed.description = (
                "No starboards are configured. Use `starboard create` or open "
                "Starboards in the web dashboard."
            )
        else:
            embed.description = "\n".join(
                f"{board['emoji']} **{board['name']}** • <#{board['channel_id']}> • "
                f"{board['threshold']} reaction{'s' if board['threshold'] != 1 else ''} • "
                f"{'on' if board['enabled'] else 'paused'}"
                for board in boards
            )
        await ctx.send(embed=embed)

    @starboard.command(name="create", aliases=["add"], description="Create a starboard")
    @commands.has_permissions(manage_guild=True)
    async def create_board(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        threshold: int = DEFAULT_THRESHOLD,
        emoji: str = DEFAULT_EMOJI,
        *,
        name: str = "Starboard",
    ):
        boards = self.boards_for(ctx.guild.id)
        boards.append(
            {
                "name": name,
                "channel_id": channel.id,
                "emoji": emoji,
                "threshold": threshold,
                "enabled": True,
            }
        )
        try:
            await self.replace_boards(ctx.guild, boards)
        except ValueError as error:
            return await ctx.send(str(error))
        await ctx.send(f"Created **{name.strip()}** in {channel.mention} using {emoji}.")

    async def _edit_board(self, ctx, query: str, **changes) -> bool:
        boards = self.boards_for(ctx.guild.id)
        target = self._resolve_board(ctx.guild.id, query)
        if target is None:
            await ctx.send("I couldn't find that starboard. Use `starboard` to list the configured names.")
            return False
        for board in boards:
            if board["board_id"] == target["board_id"]:
                board.update(changes)
                break
        try:
            await self.replace_boards(ctx.guild, boards)
        except ValueError as error:
            await ctx.send(str(error))
            return False
        return True

    @starboard.command(name="rename", description="Rename a starboard")
    @commands.has_permissions(manage_guild=True)
    async def rename_board(self, ctx: commands.Context, board: str, *, name: str):
        if await self._edit_board(ctx, board, name=name):
            await ctx.send(f"Renamed that starboard to **{name.strip()}**.")

    @starboard.command(name="emoji", description="Change a starboard reaction emoji")
    @commands.has_permissions(manage_guild=True)
    async def change_emoji(self, ctx: commands.Context, board: str, emoji: str):
        if await self._edit_board(ctx, board, emoji=emoji):
            await ctx.send(f"That starboard now watches {emoji} reactions.")

    @starboard.command(name="threshold", description="Change the reactions needed")
    @commands.has_permissions(manage_guild=True)
    async def change_threshold(self, ctx: commands.Context, board: str, threshold: int):
        if await self._edit_board(ctx, board, threshold=threshold):
            await ctx.send(f"That starboard now requires **{threshold}** reactions.")

    @starboard.command(name="channel", description="Move future highlights to another channel")
    @commands.has_permissions(manage_guild=True)
    async def change_channel(self, ctx: commands.Context, board: str, channel: discord.TextChannel):
        if await self._edit_board(ctx, board, channel_id=channel.id):
            await ctx.send(f"Future highlights for that starboard will appear in {channel.mention}.")

    @starboard.command(name="toggle", description="Pause or resume a starboard")
    @commands.has_permissions(manage_guild=True)
    async def toggle_board(self, ctx: commands.Context, *, board: str):
        target = self._resolve_board(ctx.guild.id, board)
        if target is None:
            return await ctx.send("I couldn't find that starboard.")
        enabled = not target.get("enabled", True)
        if await self._edit_board(ctx, board, enabled=enabled):
            await ctx.send(f"**{target['name']}** is now {'active' if enabled else 'paused'}.")

    @starboard.command(name="delete", aliases=["remove"], description="Delete a starboard configuration")
    @commands.has_permissions(manage_guild=True)
    async def delete_board(self, ctx: commands.Context, *, board: str):
        target = self._resolve_board(ctx.guild.id, board)
        if target is None:
            return await ctx.send("I couldn't find that starboard.")
        boards = [item for item in self.boards_for(ctx.guild.id) if item["board_id"] != target["board_id"]]
        await self.replace_boards(ctx.guild, boards)
        await ctx.send(f"Deleted the **{target['name']}** starboard configuration. Existing highlights were kept.")

    @staticmethod
    def _post_key(board_id: str, message_id: int) -> str:
        return f"{board_id}:{message_id}"

    async def _reaction_count(self, message: discord.Message, key: str) -> int:
        for reaction in message.reactions:
            if emoji_key(reaction.emoji) != key:
                continue
            count = 0
            async for user in reaction.users(limit=None):
                if not user.bot and user.id != message.author.id:
                    count += 1
            return count
        return 0

    def _highlight_embed(self, message: discord.Message, board: dict[str, Any]) -> discord.Embed:
        description = message.content.strip() or "*No text content*"
        embed = discord.Embed(
            description=description[:4096],
            color=self.bot.config.get("theme_color", 0x5865F2),
            timestamp=message.created_at,
        )
        embed.set_author(
            name=message.author.display_name,
            icon_url=message.author.display_avatar.url,
        )
        embed.add_field(name="Original message", value=f"[Jump to message]({message.jump_url})", inline=False)
        for attachment in message.attachments:
            content_type = attachment.content_type or ""
            if content_type.startswith("image/"):
                embed.set_image(url=attachment.url)
                break
        else:
            for source_embed in message.embeds:
                image_url = getattr(source_embed.image, "url", None)
                if image_url:
                    embed.set_image(url=image_url)
                    break
        embed.set_footer(text=f"{board['name']} • Source {message.id}")
        return embed

    async def _sync_board_message(self, message: discord.Message, board: dict[str, Any]) -> None:
        if not board.get("enabled", True) or message.author.bot:
            return
        destination = message.guild.get_channel(int(board["channel_id"]))
        if not isinstance(destination, discord.TextChannel) or destination.id == message.channel.id:
            return
        source_is_nsfw = getattr(message.channel, "is_nsfw", lambda: False)()
        if source_is_nsfw and not destination.is_nsfw():
            return
        count = await self._reaction_count(message, board["emoji_key"])
        key = self._post_key(board["board_id"], message.id)
        posts = deepcopy(self.posts.get(message.guild.id, {}))
        saved = posts.get(key)
        output = None
        if saved:
            saved_channel = message.guild.get_channel(int(saved.get("destination_channel_id", 0)))
            if saved_channel:
                with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                    output = await saved_channel.fetch_message(int(saved["post_message_id"]))

        if count < int(board["threshold"]):
            if output:
                with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                    await output.delete()
            if key in posts:
                posts.pop(key, None)
                self.posts[message.guild.id] = posts
                await self.posts.flush()
            return

        content = f"{board['emoji']} **{count}** · {message.channel.mention}"
        embed = self._highlight_embed(message, board)
        if output:
            await output.edit(content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        else:
            output = await destination.send(
                content=content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        posts[key] = {
            "source_channel_id": message.channel.id,
            "destination_channel_id": destination.id,
            "post_message_id": output.id,
        }
        self.posts[message.guild.id] = posts
        await self.posts.flush()

    async def _sync_source(self, guild_id: int, channel_id: int, message_id: int, reaction=None) -> None:
        # Gateway events arrive for every server, including servers without a
        # starboard. Reject unrelated events before making any Discord request.
        requested_key = emoji_key(reaction) if reaction is not None else None
        posts = self.posts.get(guild_id, {})
        boards = [
            board for board in self.boards_for(guild_id)
            if board.get("enabled", True)
            and int(board["channel_id"]) != channel_id
            and (
                board.get("emoji_key") == requested_key
                if requested_key is not None
                else self._post_key(board["board_id"], message_id) in posts
            )
        ]
        # Edits only refresh existing highlights; reactions create new ones.
        if not boards:
            return
        guild = self.bot.get_guild(guild_id)
        channel = None
        if guild:
            get_channel = getattr(guild, "get_channel_or_thread", guild.get_channel)
            channel = get_channel(channel_id)
        if not guild or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        if message.author.bot:
            return
        for board in boards:
            lock_key = (guild_id, board["board_id"], message_id)
            lock = self._locks.setdefault(lock_key, asyncio.Lock())
            try:
                async with lock:
                    await self._sync_board_message(message, board)
            except (discord.Forbidden, discord.HTTPException):
                continue

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.member is not None and payload.member.bot:
            return
        if payload.guild_id and payload.user_id != self.bot.user.id:
            await self._sync_source(payload.guild_id, payload.channel_id, payload.message_id, payload.emoji)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id and payload.user_id != self.bot.user.id:
            await self._sync_source(payload.guild_id, payload.channel_id, payload.message_id, payload.emoji)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.data.get("author", {}).get("bot"):
            return
        if not any(field in payload.data for field in ("content", "attachments", "embeds")):
            return
        if payload.guild_id:
            await self._sync_source(payload.guild_id, payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if not payload.guild_id:
            return
        posts = deepcopy(self.posts.get(payload.guild_id, {}))
        changed = False
        guild = self.bot.get_guild(payload.guild_id)
        for key, saved in list(posts.items()):
            if int(saved.get("post_message_id", 0)) == payload.message_id:
                posts.pop(key, None)
                changed = True
                continue
            if not key.endswith(f":{payload.message_id}"):
                continue
            destination = guild.get_channel(int(saved.get("destination_channel_id", 0))) if guild else None
            if destination:
                with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                    output = await destination.fetch_message(int(saved["post_message_id"]))
                    await output.delete()
            posts.pop(key, None)
            changed = True
        if changed:
            self.posts[payload.guild_id] = posts
            await self.posts.flush()


async def setup(bot):
    await bot.add_cog(Starboard(bot), override=True)
