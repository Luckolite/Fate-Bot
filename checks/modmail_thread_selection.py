"""Modmail selects the requested case without matching another case's digits."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.moderation.mod_mail import ModMail


class Cursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, *args):
        return None

    async def fetchall(self):
        return [(42, 1, "warn", None, "https://example.test/case", 0)]


class ModmailThreadChecks(unittest.IsolatedAsyncioTestCase):
    def make_reply(self, *, archived=(), active=()):
        async def archived_threads():
            for thread in archived:
                yield thread

        guild = SimpleNamespace(id=42, active_threads=AsyncMock(return_value=list(active)))
        channel = SimpleNamespace(
            name="modmail", archived_threads=archived_threads,
            guild=SimpleNamespace(me=SimpleNamespace(guild_permissions=SimpleNamespace(view_audit_log=True))),
            send=AsyncMock(), create_thread=AsyncMock(),
        )
        compose = AsyncMock(side_effect=AssertionError("Unexpected compose prompt"))
        cog = ModMail.__new__(ModMail)
        cog.bot = SimpleNamespace(
            utils=SimpleNamespace(cursor=Cursor, get_message=compose),
            get_guild=lambda identifier: guild, get_channel=lambda identifier: channel,
            config={"theme_color": 0},
        )
        cog.config = {42: {"blocked": [], "channel_id": 4}}
        cog.reference = AsyncMock()
        ctx = SimpleNamespace(
            guild=None, author=SimpleNamespace(id=2, display_avatar=SimpleNamespace(url="https://example.test/avatar.png")),
            message=SimpleNamespace(attachments=[]), send=AsyncMock(),
        )
        return cog, ctx, channel, compose

    async def test_closed_case_ten_does_not_block_reply_to_case_one(self):
        closed = SimpleNamespace(name="Case 10 - User (Closed)", send=AsyncMock())
        requested = SimpleNamespace(name="Case 1 - User", send=AsyncMock())
        cog, ctx, channel, _ = self.make_reply(archived=[closed], active=[requested])

        await ModMail.reply.callback(cog, ctx, case_number="1 Hello")

        requested.send.assert_awaited_once()
        self.assertEqual(requested.send.call_args.kwargs["embed"].description, "Hello")
        closed.send.assert_not_awaited()
        channel.create_thread.assert_not_awaited()
        ctx.send.assert_awaited_once_with("Replied to your thread 👍")
        cog.reference.assert_awaited_once()

    async def test_closed_requested_case_stops_before_composing_or_sending(self):
        closed = SimpleNamespace(name="Case 1 - User (Closed)", send=AsyncMock())
        cog, ctx, channel, compose = self.make_reply(archived=[closed])

        await ModMail.reply.callback(cog, ctx, case_number="1")

        ctx.send.assert_awaited_once_with("That thread is currently closed. Try again another time")
        compose.assert_not_awaited()
        channel.create_thread.assert_not_awaited()
        closed.send.assert_not_awaited()
        cog.reference.assert_not_awaited()

    async def test_archived_requested_thread_is_reused(self):
        unrelated = SimpleNamespace(name="Case 10 - User", send=AsyncMock())
        requested = SimpleNamespace(name="Case 1 - User", send=AsyncMock())
        cog, ctx, channel, _ = self.make_reply(archived=[unrelated, requested])

        await ModMail.reply.callback(cog, ctx, case_number="1 Hello")

        requested.send.assert_awaited_once()
        unrelated.send.assert_not_awaited()
        channel.create_thread.assert_not_awaited()
        ctx.send.assert_awaited_once_with("Replied to your thread 👍")


if __name__ == "__main__":
    unittest.main()
