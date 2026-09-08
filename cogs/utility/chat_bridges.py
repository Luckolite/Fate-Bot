"""Reliable, persistent chat bridges between Discord text channels.

The bridge topology is stored in the existing ``chatbridges`` Mongo collection.
Each document is keyed by the host channel ID and retains the legacy shape so
deployed bridge records do not need a migration::

    {
        "guild_id": 123,
        "webhook_url": "https://discord.com/api/webhooks/...",
        "channels": {"456": "https://discord.com/api/webhooks/..."},
        "blocked": [789],
        "warnings": false,             # optional; presence means disabled
        "additional_channels": 2,      # optional
    }

Runtime state has one owner: :class:`ChatBridges`.  A channel index makes relay
lookups constant-time, one bounded queue serializes each bridge, and exact
webhook message IDs are retained for edit/delete propagation.
"""

from __future__ import annotations

import asyncio
import io
import re
import traceback
from collections import OrderedDict, defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, Iterable, Mapping

import aiohttp
import discord
from discord.ext import commands

if TYPE_CHECKING:
    from fate import Fate


BASE_CHANNEL_LIMIT = 3
BRIDGES_PER_GUILD_LIMIT = 3
LINK_TIMEOUT_SECONDS = 120
MUTE_SECONDS = 60
QUEUE_SIZE = 25
MAX_RELAY_CONTENT = 2_000
MAX_TRACKED_MESSAGES = 1_000
MAX_BUFFERED_ATTACHMENT_BYTES = 32 * 1024 * 1024
MAX_ATTACHMENTS = 10

RELAY_MENTIONS = discord.AllowedMentions(
    everyone=False,
    roles=False,
    users=True,
    replied_user=False,
)
NO_MENTIONS = discord.AllowedMentions.none()
WEBHOOK_ID_PATTERN = re.compile(r"/webhooks/(?P<id>[0-9]+)(?:/|$)")
REPEATED_CHARACTER_PATTERN = re.compile(r"(.)\1{19,}", re.DOTALL)
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:[^:>]+:[0-9]+>")


@dataclass(frozen=True)
class Bridge:
    """A validated, immutable view of one persisted bridge record."""

    bridge_id: int
    guild_id: int
    host_webhook_url: str
    channels: dict[int, str]
    blocked_users: frozenset[int]
    warnings_enabled: bool
    additional_channels: int

    @classmethod
    def from_record(
        cls,
        bridge_id: int | str,
        record: Mapping[str, Any],
    ) -> "Bridge":
        host_id = _positive_int(bridge_id, "bridge ID")
        guild_id = _positive_int(record.get("guild_id"), "host guild ID")
        host_webhook = _webhook_url(record.get("webhook_url"))

        raw_channels = record.get("channels", {})
        if not isinstance(raw_channels, Mapping):
            raise ValueError("channels must be a mapping")
        channels: dict[int, str] = {}
        for channel_id, webhook_url in raw_channels.items():
            parsed_id = _positive_int(channel_id, "channel ID")
            if parsed_id == host_id:
                raise ValueError("the host channel cannot also be a satellite")
            channels[parsed_id] = _webhook_url(webhook_url)

        raw_blocked = record.get("blocked", [])
        if not isinstance(raw_blocked, (list, tuple, set, frozenset)):
            raise ValueError("blocked must be a sequence")
        blocked = frozenset(
            _positive_int(user_id, "blocked user ID") for user_id in raw_blocked
        )

        additional = record.get("additional_channels", 0)
        if isinstance(additional, bool):
            raise ValueError("additional_channels must be an integer")
        try:
            additional = int(additional)
        except (TypeError, ValueError) as error:
            raise ValueError("additional_channels must be an integer") from error

        return cls(
            bridge_id=host_id,
            guild_id=guild_id,
            host_webhook_url=host_webhook,
            channels=channels,
            blocked_users=blocked,
            # Legacy records used the presence of this key as the disabled flag.
            warnings_enabled="warnings" not in record,
            additional_channels=max(0, additional),
        )

    @property
    def channel_ids(self) -> tuple[int, ...]:
        return (self.bridge_id, *self.channels)

    @property
    def total_channels(self) -> int:
        return 1 + len(self.channels)

    @property
    def channel_limit(self) -> int:
        return BASE_CHANNEL_LIMIT + self.additional_channels

    @property
    def webhook_urls(self) -> dict[int, str]:
        return {self.bridge_id: self.host_webhook_url, **self.channels}

    @property
    def webhook_ids(self) -> frozenset[int]:
        return frozenset(
            webhook_id
            for url in self.webhook_urls.values()
            if (webhook_id := webhook_id_from_url(url)) is not None
        )

    def targets_for(self, source_channel_id: int) -> tuple["RelayTarget", ...]:
        return tuple(
            RelayTarget(channel_id=channel_id, webhook_url=webhook_url)
            for channel_id, webhook_url in self.webhook_urls.items()
            if channel_id != source_channel_id
        )


@dataclass(frozen=True)
class RelayTarget:
    channel_id: int
    webhook_url: str


@dataclass(frozen=True)
class RelayEnvelope:
    bridge_id: int
    message: discord.Message
    targets: tuple[RelayTarget, ...]


@dataclass(frozen=True)
class BufferedAttachment:
    filename: str
    url: str
    description: str | None
    spoiler: bool
    data: bytes | None


@dataclass(frozen=True)
class MirroredMessage:
    channel_id: int
    message_id: int
    webhook_url: str


@dataclass(frozen=True)
class PendingLink:
    channel_id: int
    guild_id: int
    created_at: float


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a positive integer") from error
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


def _webhook_url(value: Any) -> str:
    if not isinstance(value, str) or webhook_id_from_url(value) is None:
        raise ValueError("invalid Discord webhook URL")
    return value


def webhook_id_from_url(url: str) -> int | None:
    """Return the Discord webhook ID embedded in *url*, if one is present."""
    if not isinstance(url, str):
        return None
    match = WEBHOOK_ID_PATTERN.search(url)
    return int(match.group("id")) if match else None


def spam_reason(content: str) -> str | None:
    """Return a stable rejection reason for obvious disruptive message spam."""
    if len(content) > 1_000:
        return "messages are limited to 1,000 characters"
    if "\n\n\n\n" in content:
        return "messages cannot contain large blank sections"

    letters = [character for character in content if character.isalpha()]
    if len(letters) >= 30:
        uppercase = sum(character.isupper() for character in letters)
        if uppercase / len(letters) >= 0.75:
            return "messages cannot be mostly uppercase"

    if REPEATED_CHARACTER_PATTERN.search(content):
        return "messages cannot repeat one character that many times"

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) >= 4 and len(set(lines)) == 1:
        return "messages cannot repeat the same line"

    words = [word.casefold() for word in content.split() if len(word) >= 4]
    if words:
        most_repeated = max(words.count(word) for word in set(words))
        threshold = 4 if len(content) <= 50 else 11
        if most_repeated >= threshold:
            return "messages cannot repeat the same word that many times"

    if len(CUSTOM_EMOJI_PATTERN.findall(content)) > 10:
        return "messages cannot contain that many custom emoji"
    if len(content) > 50 and not any(character.isalnum() for character in content):
        return "messages cannot be only symbols or emoji"
    return None


def quoted_reply(message: discord.Message) -> str:
    """Build a compact, mention-safe quote for a Discord reply."""
    reference = message.reference
    replied_to = reference.cached_message if reference else None
    if replied_to is None:
        return ""

    author = discord.utils.escape_markdown(
        discord.utils.escape_mentions(replied_to.author.display_name)
    ).removeprefix("@")
    if replied_to.content:
        preview = replied_to.clean_content.strip().replace("\n", " ")
        preview = discord.utils.escape_mentions(preview)
        preview = discord.utils.escape_markdown(preview)[:300]
    elif replied_to.attachments:
        preview = "[attachment]"
    elif replied_to.embeds:
        preview = "[embed]"
    else:
        preview = "[message]"
    return f"> **Replying to @{author}:** {preview}\n"


def relay_content(
    message: discord.Message,
    *,
    attachment_links: Iterable[str] = (),
) -> str | None:
    """Return relay content without mutating the source ``Message`` object."""
    parts = [quoted_reply(message), message.content]
    parts.extend(sticker.url for sticker in message.stickers)
    parts.extend(attachment_links)
    content = "\n".join(part for part in parts if part).strip()
    if not content:
        return None
    if len(content) <= MAX_RELAY_CONTENT:
        return content
    return content[: MAX_RELAY_CONTENT - 1].rstrip() + "…"


class ChatBridges(commands.Cog):
    """Create and operate persistent channel-to-channel chat bridges."""

    link_usage = (
        "Run `.link` as an administrator in each channel within two minutes. "
        "A bridge supports three channels by default."
    )

    def __init__(self, bot: Fate):
        self.bot = bot
        self.config = bot.utils.cache("chatbridges")
        self.queues: dict[int, asyncio.Queue[RelayEnvelope]] = {}
        self.workers: dict[int, asyncio.Task] = {}
        self.channel_index: dict[int, int] = {}
        self._config_keys: dict[int, int | str] = {}
        self.pending_links: dict[int, PendingLink] = {}
        self.muted_until: dict[int, float] = {}
        self.message_activity: dict[int, deque[float]] = defaultdict(deque)
        self.ping_activity: dict[int, deque[float]] = defaultdict(deque)
        self.ban_cache: dict[int, tuple[float, frozenset[int]]] = {}
        self.webhook_cache: dict[str, discord.Webhook] = {}
        self.copies_by_origin: OrderedDict[int, list[MirroredMessage]] = OrderedDict()
        self.origin_by_copy: dict[int, int] = {}
        self.local_notice_ids: set[int] = set()
        self._mutation_lock = asyncio.Lock()
        self._closing = False
        self.session: aiohttp.ClientSession | None = None

        self._rebuild_channel_index()
        # Keep the legacy task monitor useful without creating a second owner.
        bot.tasks["bridges"] = self.workers

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        for bridge_id in self._config_keys:
            if self._bridge(bridge_id) is not None:
                self._ensure_worker(bridge_id)

    async def cog_unload(self) -> None:
        self._closing = True
        tasks = list(self.workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
        self.queues.clear()
        if self.session and not self.session.closed:
            await self.session.close()
        if self.bot.tasks.get("bridges") is self.workers:
            self.bot.tasks.pop("bridges", None)

    def _log_failure(self, summary: str) -> None:
        self.bot.log.critical(f"{summary}\n{traceback.format_exc()}")

    def _rebuild_channel_index(self) -> None:
        index: dict[int, int] = {}
        keys: dict[int, int | str] = {}
        for raw_bridge_id, record in self.config.items():
            try:
                bridge = Bridge.from_record(raw_bridge_id, record)
            except (TypeError, ValueError) as error:
                self.bot.log.warning(
                    f"Skipping invalid chat bridge {raw_bridge_id!r}: {error}"
                )
                continue
            if bridge.bridge_id in keys:
                self.bot.log.warning(
                    f"Skipping duplicate chat bridge ID {bridge.bridge_id}"
                )
                continue
            keys[bridge.bridge_id] = raw_bridge_id
            for channel_id in bridge.channel_ids:
                if channel_id in index:
                    self.bot.log.warning(
                        f"Channel {channel_id} appears in multiple chat bridges; "
                        f"keeping bridge {index[channel_id]}"
                    )
                    continue
                index[channel_id] = bridge.bridge_id
        self.channel_index = index
        self._config_keys = keys

    def _bridge(self, bridge_id: int) -> Bridge | None:
        key = self._config_keys.get(bridge_id)
        if key is None:
            return None
        try:
            return Bridge.from_record(bridge_id, self.config[key])
        except (KeyError, TypeError, ValueError) as error:
            self.bot.log.warning(f"Invalid chat bridge {bridge_id}: {error}")
            return None

    def _record(self, bridge_id: int) -> Mapping[str, Any]:
        return self.config[self._config_keys[bridge_id]]

    def _ensure_worker(self, bridge_id: int) -> asyncio.Task:
        queue = self.queues.setdefault(bridge_id, asyncio.Queue(maxsize=QUEUE_SIZE))
        task = self.workers.get(bridge_id)
        if task and not task.done():
            return task
        task = asyncio.create_task(
            self._relay_worker(bridge_id, queue),
            name=f"chatbridge-{bridge_id}",
        )
        self.workers[bridge_id] = task
        task.add_done_callback(
            lambda finished, current_id=bridge_id: self._worker_finished(
                current_id, finished
            )
        )
        return task

    def _worker_finished(self, bridge_id: int, task: asyncio.Task) -> None:
        if self.workers.get(bridge_id) is task:
            self.workers.pop(bridge_id, None)
        if task.cancelled() or self._closing:
            return
        error = task.exception()
        if error is not None:
            self.bot.log.critical(
                f"Chat bridge {bridge_id} worker stopped: "
                f"{type(error).__name__}: {error}"
            )
        if self._bridge(bridge_id) is not None:
            self._ensure_worker(bridge_id)

    async def _relay_worker(
        self,
        bridge_id: int,
        queue: asyncio.Queue[RelayEnvelope],
    ) -> None:
        await self.bot.wait_until_ready()
        while not self._closing and self._bridge(bridge_id) is not None:
            envelope = await queue.get()
            try:
                await self._relay(envelope)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log_failure(f"Chat bridge {bridge_id} relay failed")
                await self._send_local_notice(
                    envelope.message.channel,
                    "I couldn't relay that message because of an internal error.",
                )
            finally:
                queue.task_done()

    async def _relay(self, envelope: RelayEnvelope) -> None:
        max_upload = max(
            (
                getattr(
                    getattr(self.bot.get_channel(target.channel_id), "guild", None),
                    "filesize_limit",
                    8 * 1024 * 1024,
                )
                for target in envelope.targets
            ),
            default=8 * 1024 * 1024,
        )
        attachments = await self._buffer_attachments(
            envelope.message,
            min(max_upload, MAX_BUFFERED_ATTACHMENT_BYTES),
        )

        send_failed = False
        for target in envelope.targets:
            bridge = self._bridge(envelope.bridge_id)
            if bridge is None or target.channel_id not in bridge.channel_ids:
                continue
            if not await self._destination_accepts(target.channel_id, envelope.message):
                continue
            try:
                mirrored = await self._send_to_webhook(
                    target,
                    envelope.message,
                    attachments,
                )
            except (discord.NotFound, discord.Forbidden):
                await self._handle_dead_webhook(
                    envelope.bridge_id,
                    target.webhook_url,
                )
                continue
            except (discord.HTTPException, aiohttp.ClientError):
                send_failed = True
                self._log_failure(
                    f"Chat bridge {envelope.bridge_id} could not reach "
                    f"channel {target.channel_id}"
                )
                continue
            if mirrored is not None:
                self._remember_copy(
                    envelope.message.id,
                    MirroredMessage(
                        channel_id=target.channel_id,
                        message_id=mirrored.id,
                        webhook_url=target.webhook_url,
                    ),
                )

        if send_failed:
            await self._send_local_notice(
                envelope.message.channel,
                "One or more linked channels could not receive that message.",
            )

    async def _buffer_attachments(
        self,
        message: discord.Message,
        byte_budget: int,
    ) -> tuple[BufferedAttachment, ...]:
        buffered: list[BufferedAttachment] = []
        remaining = byte_budget
        for attachment in message.attachments[:MAX_ATTACHMENTS]:
            data = None
            if attachment.size <= remaining:
                with suppress(
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                    aiohttp.ClientError,
                ):
                    data = await attachment.read(use_cached=True)
                    remaining -= len(data)
            buffered.append(
                BufferedAttachment(
                    filename=attachment.filename,
                    url=attachment.url,
                    description=attachment.description,
                    spoiler=attachment.is_spoiler(),
                    data=data,
                )
            )
        return tuple(buffered)

    async def _send_to_webhook(
        self,
        target: RelayTarget,
        message: discord.Message,
        attachments: tuple[BufferedAttachment, ...],
    ) -> discord.WebhookMessage | None:
        channel = self.bot.get_channel(target.channel_id)
        upload_limit = getattr(
            getattr(channel, "guild", None),
            "filesize_limit",
            8 * 1024 * 1024,
        )
        links = [
            f"[{attachment.filename}]({attachment.url})"
            for attachment in attachments
            if attachment.data is None or len(attachment.data) > upload_limit
        ]
        files = [
            discord.File(
                io.BytesIO(attachment.data),
                filename=attachment.filename,
                description=attachment.description,
                spoiler=attachment.spoiler,
            )
            for attachment in attachments
            if attachment.data is not None and len(attachment.data) <= upload_limit
        ]
        try:
            webhook = self._webhook(target.webhook_url)
            result = await webhook.send(
                content=relay_content(message, attachment_links=links),
                embeds=list(message.embeds[:10]),
                files=files,
                username=(message.author.display_name or message.author.name)[:80],
                avatar_url=message.author.display_avatar.url,
                allowed_mentions=RELAY_MENTIONS,
                wait=True,
            )
            return result if isinstance(result, discord.WebhookMessage) else None
        finally:
            for file in files:
                file.close()

    def _webhook(self, url: str) -> discord.Webhook:
        webhook = self.webhook_cache.get(url)
        if webhook is None:
            if self.session is None:
                raise RuntimeError("chat bridge HTTP session is not available")
            webhook = discord.Webhook.from_url(url, session=self.session)
            self.webhook_cache[url] = webhook
        return webhook

    async def _destination_accepts(
        self,
        channel_id: int,
        message: discord.Message,
    ) -> bool:
        channel = self.bot.get_channel(channel_id)
        if channel is None or getattr(channel, "guild", None) is None:
            return True
        guild = channel.guild
        member = guild.get_member(message.author.id)
        if member is not None:
            mute_role = await self.bot.attrs.get_mute_role(guild, upsert=False)
            if mute_role is not None and mute_role in member.roles:
                return False
        return not await self._is_banned(guild, message.author.id)

    async def _is_banned(self, guild: discord.Guild, user_id: int) -> bool:
        me = guild.me
        if me is None or not me.guild_permissions.ban_members:
            return False
        now = monotonic()
        cached_at, banned = self.ban_cache.get(guild.id, (0.0, frozenset()))
        if now - cached_at >= 60:
            try:
                banned = frozenset(entry.user.id async for entry in guild.bans())
            except (discord.Forbidden, discord.HTTPException):
                return False
            self.ban_cache[guild.id] = (now, banned)
        return user_id in banned

    def _remember_copy(self, origin_id: int, copy: MirroredMessage) -> None:
        copies = self.copies_by_origin.setdefault(origin_id, [])
        copies.append(copy)
        self.origin_by_copy[copy.message_id] = origin_id
        self.copies_by_origin.move_to_end(origin_id)
        while len(self.copies_by_origin) > MAX_TRACKED_MESSAGES:
            _old_origin, old_copies = self.copies_by_origin.popitem(last=False)
            for old_copy in old_copies:
                self.origin_by_copy.pop(old_copy.message_id, None)

    def _forget_origin(self, origin_id: int) -> list[MirroredMessage]:
        copies = self.copies_by_origin.pop(origin_id, [])
        for copy in copies:
            self.origin_by_copy.pop(copy.message_id, None)
        return copies

    def _forget_copy(self, copy_id: int) -> None:
        origin_id = self.origin_by_copy.pop(copy_id, None)
        if origin_id is None:
            return
        copies = self.copies_by_origin.get(origin_id, [])
        remaining = [copy for copy in copies if copy.message_id != copy_id]
        if remaining:
            self.copies_by_origin[origin_id] = remaining
        else:
            self.copies_by_origin.pop(origin_id, None)

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self,
        payload: discord.RawMessageDeleteEvent,
    ) -> None:
        copies = self._forget_origin(payload.message_id)
        if not copies:
            self._forget_copy(payload.message_id)
            return
        for copy in copies:
            with suppress(
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
                aiohttp.ClientError,
            ):
                await self._webhook(copy.webhook_url).delete_message(copy.message_id)

    @commands.Cog.listener()
    async def on_message_edit(
        self,
        before: discord.Message,
        after: discord.Message,
    ) -> None:
        if before.content == after.content and before.embeds == after.embeds:
            return
        copies = list(self.copies_by_origin.get(after.id, ()))
        for copy in copies:
            try:
                await self._webhook(copy.webhook_url).edit_message(
                    copy.message_id,
                    content=relay_content(after),
                    embeds=list(after.embeds[:10]),
                    allowed_mentions=RELAY_MENTIONS,
                )
            except discord.NotFound:
                self._forget_copy(copy.message_id)
            except (discord.Forbidden, discord.HTTPException, aiohttp.ClientError):
                self._log_failure(
                    f"Could not edit mirrored chat bridge message {copy.message_id}"
                )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.is_system():
            return
        if message.id in self.local_notice_ids:
            self.local_notice_ids.discard(message.id)
            return

        bridge_id = self.channel_index.get(message.channel.id)
        if bridge_id is None:
            return
        bridge = self._bridge(bridge_id)
        if bridge is None:
            return
        if message.webhook_id in bridge.webhook_ids:
            return
        if message.author.id in bridge.blocked_users:
            return

        context = await self.bot.get_context(message)
        if context.valid and context.command and context.command.cog is self:
            return

        now = monotonic()
        muted_until = self.muted_until.get(message.author.id, 0.0)
        if muted_until > now:
            return
        self.muted_until.pop(message.author.id, None)

        if reason := spam_reason(message.content):
            await self._reject(message, bridge, reason)
            return
        if await self._rate_limited(message, bridge, now):
            return
        if await self._ping_limited(message, bridge, now):
            return
        if not (
            message.content or message.embeds or message.attachments or message.stickers
        ):
            return

        envelope = RelayEnvelope(
            bridge_id=bridge_id,
            message=message,
            targets=bridge.targets_for(message.channel.id),
        )
        if not envelope.targets:
            return
        queue = self.queues.setdefault(
            bridge_id,
            asyncio.Queue(maxsize=QUEUE_SIZE),
        )
        self._ensure_worker(bridge_id)
        try:
            queue.put_nowait(envelope)
        except asyncio.QueueFull:
            await self._safe_reaction(message, "\u23f3")

    async def _rate_limited(
        self,
        message: discord.Message,
        bridge: Bridge,
        now: float,
    ) -> bool:
        activity = self.message_activity[message.author.id]
        activity.append(now)
        while activity and activity[0] < now - 10:
            activity.popleft()
        if len(activity) >= 8:
            self.muted_until[message.author.id] = now + MUTE_SECONDS
            activity.clear()
            await self._reject(
                message,
                bridge,
                "too many messages; bridge access is paused for one minute",
                notify=True,
            )
            return True
        if sum(timestamp >= now - 4 for timestamp in activity) >= 5:
            await self._safe_reaction(message, "\u23f3")
            return True
        return False

    async def _ping_limited(
        self,
        message: discord.Message,
        bridge: Bridge,
        now: float,
    ) -> bool:
        ping_count = (
            len(set(message.raw_mentions))
            + len(set(message.raw_role_mentions))
            + int(message.mention_everyone)
        )
        if ping_count > 3:
            self.muted_until[message.author.id] = now + MUTE_SECONDS
            await self._reject(
                message,
                bridge,
                "too many mentions; bridge access is paused for one minute",
                notify=True,
            )
            return True
        if ping_count:
            activity = self.ping_activity[message.author.id]
            activity.append(now)
            while activity and activity[0] < now - 10:
                activity.popleft()
            if len(activity) > 3:
                self.muted_until[message.author.id] = now + MUTE_SECONDS
                activity.clear()
                await self._reject(
                    message,
                    bridge,
                    "too many mention-heavy messages; bridge access is paused "
                    "for one minute",
                    notify=True,
                )
                return True
        return False

    async def _reject(
        self,
        message: discord.Message,
        bridge: Bridge,
        reason: str,
        *,
        notify: bool = False,
    ) -> None:
        if not bridge.warnings_enabled:
            return
        if notify:
            await self._send_local_notice(message.channel, f"Bridge: {reason}.")
        else:
            await self._safe_reaction(message, "\u274c")

    async def _safe_reaction(self, message: discord.Message, emoji: str) -> None:
        with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            await message.add_reaction(emoji)

    async def _send_local_notice(self, channel: Any, content: str) -> None:
        with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            notice = await channel.send(content, allowed_mentions=NO_MENTIONS)
            self.local_notice_ids.add(notice.id)
            if len(self.local_notice_ids) > 100:
                self.local_notice_ids.pop()

    async def _handle_dead_webhook(self, bridge_id: int, url: str) -> None:
        bridge = self._bridge(bridge_id)
        if bridge is None:
            return
        if url == bridge.host_webhook_url:
            await self._destroy_bridge(
                bridge_id,
                "The host webhook was deleted, so this chat bridge was disabled.",
                skip_urls={url},
            )
            return

        dead_channel = next(
            (
                channel_id
                for channel_id, webhook_url in bridge.channels.items()
                if webhook_url == url
            ),
            None,
        )
        if dead_channel is None:
            return
        async with self._mutation_lock:
            current = self._bridge(bridge_id)
            if current is None or current.channels.get(dead_channel) != url:
                return
            record = self._record(bridge_id)
            record["channels"] = {
                str(channel_id): webhook_url
                for channel_id, webhook_url in current.channels.items()
                if channel_id != dead_channel
            }
            await self.config.flush()
            self._rebuild_channel_index()
        current = self._bridge(bridge_id)
        if current is None:
            return
        if not current.channels:
            await self._destroy_bridge(
                bridge_id,
                "The last linked webhook was deleted, so this bridge was disabled.",
                skip_urls={url},
            )
        else:
            await self._broadcast_system(
                bridge_id,
                f"Channel {dead_channel} left because its webhook was deleted.",
            )

    async def _destroy_bridge(
        self,
        bridge_id: int,
        reason: str,
        *,
        skip_urls: set[str] | None = None,
    ) -> None:
        skip_urls = skip_urls or set()
        async with self._mutation_lock:
            bridge = self._bridge(bridge_id)
            if bridge is None:
                return
            urls = tuple(bridge.webhook_urls.values())
            key = self._config_keys[bridge_id]
            await self.config.remove(key)
            self._rebuild_channel_index()

        worker = self.workers.get(bridge_id)
        if worker and worker is not asyncio.current_task():
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        self.workers.pop(bridge_id, None)
        self.queues.pop(bridge_id, None)

        for url in urls:
            if url in skip_urls:
                continue
            webhook = self._webhook(url)
            with suppress(
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
                aiohttp.ClientError,
            ):
                await webhook.send(reason, allowed_mentions=NO_MENTIONS)
            with suppress(
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
                aiohttp.ClientError,
            ):
                await webhook.delete(reason="Chat bridge disabled")
            self.webhook_cache.pop(url, None)

    async def _broadcast_system(self, bridge_id: int, content: str) -> None:
        bridge = self._bridge(bridge_id)
        if bridge is None:
            return
        for url in bridge.webhook_urls.values():
            with suppress(
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
                aiohttp.ClientError,
            ):
                await self._webhook(url).send(content, allowed_mentions=NO_MENTIONS)

    def _prune_pending_links(self) -> None:
        cutoff = monotonic() - LINK_TIMEOUT_SECONDS
        self.pending_links = {
            user_id: pending
            for user_id, pending in self.pending_links.items()
            if pending.created_at >= cutoff
        }

    def _bridge_ids_for_guild(self, guild: discord.Guild) -> set[int]:
        return {
            bridge_id
            for channel in guild.text_channels
            if (bridge_id := self.channel_index.get(channel.id)) is not None
        }

    @commands.group(
        name="link",
        aliases=["chatbridge", "bridge"],
        description="Links this channel with another channel",
        invoke_without_command=True,
    )
    @commands.cooldown(2, 10, commands.BucketType.user)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_webhooks=True)
    async def link(self, ctx: commands.Context) -> None:
        """Run this command in each channel to create or extend a bridge."""
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("Chat bridges can only use server text channels.")
            return
        self._prune_pending_links()
        pending = self.pending_links.pop(ctx.author.id, None)
        if pending is None:
            self.pending_links[ctx.author.id] = PendingLink(
                channel_id=ctx.channel.id,
                guild_id=ctx.guild.id,
                created_at=monotonic(),
            )
            await ctx.send(
                "Ready to link this channel. Run `.link` as an administrator "
                "in the other channel within two minutes.",
                allowed_mentions=NO_MENTIONS,
            )
            return
        if pending.channel_id == ctx.channel.id:
            self.pending_links[ctx.author.id] = PendingLink(
                channel_id=ctx.channel.id,
                guild_id=ctx.guild.id,
                created_at=monotonic(),
            )
            await ctx.send("Still waiting for `.link` in another channel.")
            return

        first = self.bot.get_channel(pending.channel_id)
        if not isinstance(first, discord.TextChannel):
            await ctx.send("The first channel is no longer available. Start again.")
            return
        await self._connect_channels(ctx, first, ctx.channel)

    async def _connect_channels(
        self,
        ctx: commands.Context,
        first: discord.TextChannel,
        second: discord.TextChannel,
    ) -> None:
        first_bridge = self.channel_index.get(first.id)
        second_bridge = self.channel_index.get(second.id)
        if first_bridge is not None and first_bridge == second_bridge:
            await ctx.send("Those channels are already in the same bridge.")
            return
        if first_bridge is not None and second_bridge is not None:
            await ctx.send("Two existing bridges cannot be merged.")
            return

        existing_id = first_bridge if first_bridge is not None else second_bridge
        if existing_id is not None:
            new_channel = second if first_bridge is not None else first
            await self._add_channel(ctx, existing_id, new_channel)
            return
        await self._create_bridge(ctx, first, second)

    async def _create_bridge(
        self,
        ctx: commands.Context,
        host: discord.TextChannel,
        satellite: discord.TextChannel,
    ) -> None:
        for guild in {host.guild, satellite.guild}:
            if len(self._bridge_ids_for_guild(guild)) >= BRIDGES_PER_GUILD_LIMIT:
                await ctx.send(
                    f"{guild.name} already uses the maximum of "
                    f"{BRIDGES_PER_GUILD_LIMIT} chat bridges."
                )
                return

        created: list[discord.Webhook] = []
        record_created = False
        try:
            host_webhook = await host.create_webhook(
                name="Fate ChatBridge",
                reason=f"Chat bridge created by {ctx.author}",
            )
            created.append(host_webhook)
            satellite_webhook = await satellite.create_webhook(
                name="Fate ChatBridge",
                reason=f"Chat bridge created by {ctx.author}",
            )
            created.append(satellite_webhook)
            async with self._mutation_lock:
                self.config[host.id] = {
                    "guild_id": host.guild.id,
                    "webhook_url": host_webhook.url,
                    "channels": {str(satellite.id): satellite_webhook.url},
                    "blocked": [],
                }
                record_created = True
                await self.config.flush()
                self._rebuild_channel_index()
            self._ensure_worker(host.id)
        except Exception:
            if record_created:
                with suppress(Exception):
                    await self.config.remove(host.id)
                self._rebuild_channel_index()
            for webhook in created:
                with suppress(
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    await webhook.delete(reason="Chat bridge creation rolled back")
            raise

        await self._broadcast_system(
            host.id,
            f"Linked #{host.name} in {host.guild.name} with "
            f"#{satellite.name} in {satellite.guild.name}.",
        )
        await ctx.send("Chat bridge created successfully.")

    async def _add_channel(
        self,
        ctx: commands.Context,
        bridge_id: int,
        channel: discord.TextChannel,
    ) -> None:
        bridge = self._bridge(bridge_id)
        if bridge is None:
            await ctx.send("That bridge is no longer available. Start again.")
            return
        if bridge.total_channels >= bridge.channel_limit:
            await ctx.send(
                f"This bridge already uses all {bridge.channel_limit} channel slots."
            )
            return
        guild_bridges = self._bridge_ids_for_guild(channel.guild)
        if (
            bridge_id not in guild_bridges
            and len(guild_bridges) >= BRIDGES_PER_GUILD_LIMIT
        ):
            await ctx.send(
                f"{channel.guild.name} already uses the maximum of "
                f"{BRIDGES_PER_GUILD_LIMIT} chat bridges."
            )
            return

        webhook = await channel.create_webhook(
            name="Fate ChatBridge",
            reason=f"Chat bridge extended by {ctx.author}",
        )
        previous_channels = dict(bridge.channels)
        try:
            async with self._mutation_lock:
                current = self._bridge(bridge_id)
                if current is None or current.total_channels >= current.channel_limit:
                    raise RuntimeError("the bridge changed while the channel was added")
                record = self._record(bridge_id)
                record["channels"] = {
                    **{
                        str(channel_id): url
                        for channel_id, url in current.channels.items()
                    },
                    str(channel.id): webhook.url,
                }
                await self.config.flush()
                self._rebuild_channel_index()
        except Exception:
            current = self._bridge(bridge_id)
            if current is not None:
                record = self._record(bridge_id)
                record["channels"] = {
                    str(channel_id): url
                    for channel_id, url in previous_channels.items()
                }
                with suppress(Exception):
                    await self.config.flush()
                self._rebuild_channel_index()
            with suppress(
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                await webhook.delete(reason="Chat bridge extension rolled back")
            raise

        self._ensure_worker(bridge_id)
        await self._broadcast_system(
            bridge_id,
            f"Linked #{channel.name} in {channel.guild.name} to this bridge.",
        )
        await ctx.send("Channel added to the chat bridge.")

    @link.command(name="cancel", description="Cancels your pending link")
    async def cancel_link(self, ctx: commands.Context) -> None:
        if self.pending_links.pop(ctx.author.id, None) is None:
            await ctx.send("You do not have a pending chat bridge link.")
        else:
            await ctx.send("Cancelled the pending chat bridge link.")

    @commands.command(
        name="unlink",
        description="Removes this channel from its chat bridge",
    )
    @commands.cooldown(2, 10, commands.BucketType.user)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def unlink(self, ctx: commands.Context) -> None:
        bridge_id = self.channel_index.get(ctx.channel.id)
        bridge = self._bridge(bridge_id) if bridge_id is not None else None
        if bridge is None:
            await ctx.send("This channel is not linked.")
            return
        if ctx.channel.id == bridge.bridge_id:
            await ctx.send("The host channel disabled this chat bridge.")
            await self._destroy_bridge(
                bridge.bridge_id,
                "The host channel disabled this chat bridge.",
            )
            return

        webhook_url = bridge.channels[ctx.channel.id]
        with suppress(
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            aiohttp.ClientError,
        ):
            await self._webhook(webhook_url).delete(
                reason=f"Chat bridge channel removed by {ctx.author}"
            )
        self.webhook_cache.pop(webhook_url, None)

        async with self._mutation_lock:
            current = self._bridge(bridge.bridge_id)
            if current is None:
                return
            record = self._record(bridge.bridge_id)
            record["channels"] = {
                str(channel_id): url
                for channel_id, url in current.channels.items()
                if channel_id != ctx.channel.id
            }
            await self.config.flush()
            self._rebuild_channel_index()

        current = self._bridge(bridge.bridge_id)
        if current is None or not current.channels:
            await self._destroy_bridge(
                bridge.bridge_id,
                "The final linked channel left, so this chat bridge was disabled.",
            )
        else:
            await self._broadcast_system(
                bridge.bridge_id,
                f"#{ctx.channel.name} in {ctx.guild.name} left this bridge.",
            )
        await ctx.send("Channel removed from the chat bridge.")

    @link.command(
        name="toggle-warnings",
        aliases=["warnings"],
        description="Toggles local bridge moderation warnings",
    )
    async def toggle_warnings(self, ctx: commands.Context) -> None:
        bridge_id = self.channel_index.get(ctx.channel.id)
        bridge = self._bridge(bridge_id) if bridge_id is not None else None
        if bridge is None:
            await ctx.send("This channel is not linked.")
            return
        record = self._record(bridge.bridge_id)
        if bridge.warnings_enabled:
            record["warnings"] = False
            state = "disabled"
        else:
            await self.config.remove_sub(
                self._config_keys[bridge.bridge_id],
                "warnings",
            )
            state = "enabled"
        await self.config.flush()
        await ctx.send(f"Bridge moderation warnings {state}.")

    @link.command(
        name="grant",
        description="Grants additional channel slots to a bridge",
    )
    @commands.is_owner()
    async def grant_slots(
        self,
        ctx: commands.Context,
        channel_id: int,
        amount: int,
    ) -> None:
        bridge_id = self.channel_index.get(channel_id)
        bridge = self._bridge(bridge_id) if bridge_id is not None else None
        if bridge is None:
            await ctx.send("That channel is not part of a chat bridge.")
            return
        if amount <= 0:
            await ctx.send("The number of additional slots must be positive.")
            return
        record = self._record(bridge.bridge_id)
        record["additional_channels"] = bridge.additional_channels + amount
        await self.config.flush()
        await ctx.send(
            f"Bridge {bridge.bridge_id} now has "
            f"{record['additional_channels']} additional channel slots."
        )

    @link.command(
        name="remove-grant",
        description="Removes all additional channel slots from a bridge",
    )
    @commands.is_owner()
    async def remove_granted_slots(
        self,
        ctx: commands.Context,
        channel_id: int,
    ) -> None:
        bridge_id = self.channel_index.get(channel_id)
        bridge = self._bridge(bridge_id) if bridge_id is not None else None
        if bridge is None:
            await ctx.send("That channel is not part of a chat bridge.")
            return
        if not bridge.additional_channels:
            await ctx.send("That bridge has no additional channel slots.")
            return
        await self.config.remove_sub(
            self._config_keys[bridge.bridge_id],
            "additional_channels",
        )
        await self.config.flush()
        await ctx.send("Removed the bridge's additional channel slots.")

    @link.command(name="block", description="Blocks users from this bridge")
    async def block_users(
        self,
        ctx: commands.Context,
        users: commands.Greedy[discord.User],
    ) -> None:
        bridge_id = self.channel_index.get(ctx.channel.id)
        bridge = self._bridge(bridge_id) if bridge_id is not None else None
        if bridge is None:
            await ctx.send("This channel is not linked.")
            return
        if not users:
            await ctx.send("Mention or provide at least one user to block.")
            return
        blocked = set(bridge.blocked_users)
        before = len(blocked)
        blocked.update(user.id for user in users)
        record = self._record(bridge.bridge_id)
        record["blocked"] = sorted(blocked)
        await self.config.flush()
        await ctx.send(f"Blocked {len(blocked) - before} new user(s) from the bridge.")

    @link.command(name="unblock", description="Unblocks a user from this bridge")
    async def unblock_user(
        self,
        ctx: commands.Context,
        user: discord.User,
    ) -> None:
        bridge_id = self.channel_index.get(ctx.channel.id)
        bridge = self._bridge(bridge_id) if bridge_id is not None else None
        if bridge is None:
            await ctx.send("This channel is not linked.")
            return
        blocked = set(bridge.blocked_users)
        if user.id not in blocked:
            await ctx.send(f"{user} is not blocked from this bridge.")
            return
        blocked.remove(user.id)
        record = self._record(bridge.bridge_id)
        record["blocked"] = sorted(blocked)
        await self.config.flush()
        await ctx.send(f"Unblocked {user} from the bridge.")

    @commands.command(
        name="bridges",
        aliases=["chatbridges"],
        description="Lists chat bridges used by this server",
    )
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def chatbridges(self, ctx: commands.Context) -> None:
        bridge_ids = sorted(self._bridge_ids_for_guild(ctx.guild))
        embed = discord.Embed(
            title="Chat bridges",
            color=self.bot.config["theme_color"],
        )
        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)
        if not bridge_ids:
            embed.description = "This server does not use any chat bridges."
            await ctx.send(embed=embed)
            return

        for bridge_id in bridge_ids:
            bridge = self._bridge(bridge_id)
            if bridge is None:
                continue
            locations = []
            for channel_id in bridge.channel_ids:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    locations.append(f"Unavailable channel ({channel_id})")
                else:
                    locations.append(f"{channel.guild.name} / #{channel.name}")
            embed.add_field(
                name=f"Bridge {bridge_id}",
                value="\n".join(f"• {location}" for location in locations)[:1024],
                inline=False,
            )
        await ctx.send(embed=embed)


async def setup(bot: Fate) -> None:
    await bot.add_cog(ChatBridges(bot))
