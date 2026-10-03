"""Starboard checks for cheap subthreshold reactions and exact eligible votes."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.utility.starboard import Starboard
from botutils.starboard import emoji_key


class StarboardChecks(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_highlights_preserve_both_entries_without_copying_history(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        class History(dict):
            def __deepcopy__(self, memo):
                raise AssertionError("copied the full post history")

        class Posts(dict):
            flush = AsyncMock()

        class Channel:
            id = 20

            def is_nsfw(self):
                return False

            async def send(self, **kwargs):
                entered.set()
                await release.wait()
                return SimpleNamespace(id=200)

        destination = Channel()
        cog = object.__new__(Starboard)
        cog.posts = Posts({1: History()})
        cog._reaction_count = AsyncMock(return_value=3)
        cog._highlight_embed = Mock(return_value=None)
        guild = SimpleNamespace(id=1, get_channel=lambda channel_id: destination)
        board = {"channel_id": 20, "emoji_key": emoji_key("⭐"), "emoji": "⭐", "threshold": 3, "board_id": "board"}

        def message(message_id):
            return SimpleNamespace(id=message_id, guild=guild, channel=SimpleNamespace(id=10, mention="#source"), author=SimpleNamespace(bot=False))

        with patch("cogs.utility.starboard.discord.TextChannel", Channel):
            first = asyncio.create_task(cog._sync_board_message(message(100), board))
            await entered.wait()
            # A different event writes while the first request is in flight.
            cog.posts[1]["other:50"] = {"post_message_id": 99}
            second = asyncio.create_task(cog._sync_board_message(message(101), board))
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(set(cog.posts[1]), {"other:50", "board:100", "board:101"})

    async def test_source_deletion_preserves_posts_added_during_discord_request(self):
        class Posts(dict):
            flush = AsyncMock()

        cog = object.__new__(Starboard)
        cog.posts = Posts({1: {"board:100": {"destination_channel_id": 20, "post_message_id": 200}}})

        async def fetch(message_id):
            cog.posts[1]["board:101"] = {"post_message_id": 201}
            return SimpleNamespace(delete=AsyncMock())

        destination = SimpleNamespace(fetch_message=fetch)
        cog.bot = SimpleNamespace(get_guild=lambda guild_id: SimpleNamespace(get_channel=lambda channel_id: destination))
        await cog.on_raw_message_delete(SimpleNamespace(guild_id=1, message_id=100))
        self.assertEqual(cog.posts[1], {"board:101": {"post_message_id": 201}})

    async def test_subthreshold_reaction_does_not_fetch_reactors(self):
        reaction = SimpleNamespace(emoji="⭐", count=2, users=Mock(side_effect=AssertionError("network request")))
        message = SimpleNamespace(reactions=[reaction])
        count = await Starboard._reaction_count(None, message, emoji_key("⭐"), minimum=3)
        self.assertEqual(count, 2)
        reaction.users.assert_not_called()

    async def test_possible_highlight_still_excludes_bots_and_author(self):
        async def users():
            for user_id, bot in ((1, False), (2, True), (3, False), (4, False), (5, False)):
                yield SimpleNamespace(id=user_id, bot=bot)
        reaction = SimpleNamespace(emoji="⭐", count=5, users=Mock(return_value=users()))
        message = SimpleNamespace(reactions=[reaction], author=SimpleNamespace(id=1))
        count = await Starboard._reaction_count(None, message, emoji_key("⭐"), minimum=3)
        self.assertEqual(count, 3)
        reaction.users.assert_called_once_with(limit=None)

    async def test_subthreshold_event_removes_existing_highlight(self):
        class Channel:
            id = 20
            fetch_message = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))

            def is_nsfw(self):
                return False

        class Posts(dict):
            flush = AsyncMock()

        destination = Channel()
        cog = object.__new__(Starboard)
        cog.posts = Posts({1: {"board:100": {"destination_channel_id": 20, "post_message_id": 200}}})
        reaction = SimpleNamespace(emoji="⭐", count=2, users=Mock(side_effect=AssertionError("network request")))
        message = SimpleNamespace(
            id=100, guild=SimpleNamespace(id=1, get_channel=lambda channel_id: destination),
            channel=SimpleNamespace(id=10), author=SimpleNamespace(bot=False), reactions=[reaction],
        )
        with patch("cogs.utility.starboard.discord.TextChannel", Channel):
            await cog._sync_board_message(message, {"enabled": True, "channel_id": 20, "emoji_key": emoji_key("⭐"), "threshold": 3, "board_id": "board"})
        destination.fetch_message.return_value.delete.assert_awaited_once()
        self.assertEqual(cog.posts[1], {})
        cog.posts.flush.assert_awaited_once()
        reaction.users.assert_not_called()


if __name__ == "__main__":
    unittest.main()
