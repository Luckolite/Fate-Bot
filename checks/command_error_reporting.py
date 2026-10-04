"""Command error reporting keeps replies, deduplication, files, and owner details."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.core.error_handler import ErrorHandler


class CommandErrorReportingChecks(unittest.IsolatedAsyncioTestCase):
    def make_handler(self, *, previous=(), owner=False, channel_available=True):
        async def history(*, limit):
            self.assertEqual(limit, 16)
            for message in previous:
                yield message

        report_message = SimpleNamespace(id=7, add_reaction=AsyncMock())
        channel = SimpleNamespace(history=history, send=AsyncMock(return_value=report_message))
        avatar = SimpleNamespace(url="https://example.test/avatar.png")
        ctx = SimpleNamespace(
            command=SimpleNamespace(cog=None), cog=None,
            guild=SimpleNamespace(id=1, me=object(), icon=None),
            channel=SimpleNamespace(permissions_for=lambda member: SimpleNamespace(send_messages=True)),
            author=SimpleNamespace(id=2, display_avatar=avatar),
            message=SimpleNamespace(content=".broken", jump_url="https://example.test/message"),
            send=AsyncMock(),
        )
        handler = ErrorHandler.__new__(ErrorHandler)
        handler.bot = SimpleNamespace(
            config={"command_errors": 4}, owner_ids={2} if owner else set(),
            user=SimpleNamespace(display_avatar=avatar),
            get_channel=Mock(return_value=channel if channel_available else None),
        )
        handler.cd = SimpleNamespace(check=Mock(return_value=False))
        handler.notifs = {}
        return handler, ctx, channel, report_message

    async def dispatch(self, handler, ctx, error):
        with patch("cogs.core.error_handler.safe_console_print") as console:
            await handler.on_command_error(ctx, error)
        return console

    async def test_unhandled_error_replies_and_tracks_report_notification(self):
        handler, ctx, channel, report = self.make_handler()

        await self.dispatch(handler, ctx, ValueError("Specific failure"))

        ctx.send.assert_awaited_once()
        self.assertIn("Specific failure", ctx.send.call_args.kwargs["embed"].description)
        channel.send.assert_awaited_once()
        self.assertIn("ValueError: Specific failure", channel.send.call_args.kwargs["embed"].description)
        report.add_reaction.assert_awaited_once_with("✔")
        self.assertEqual(handler.notifs, {7: 2})

    async def test_matching_previous_report_suppresses_duplicate_channel_send(self):
        handler, ctx, channel, _ = self.make_handler()
        embed = discord.Embed()
        embed.set_author(name=f"| Fatal Error | in {ctx.command}")
        channel.history = self.make_history(SimpleNamespace(embeds=[embed]))

        await self.dispatch(handler, ctx, ValueError("Specific failure"))

        ctx.send.assert_awaited_once()
        channel.send.assert_not_awaited()
        self.assertEqual(handler.notifs, {})

    def make_history(self, message):
        async def history(*, limit):
            yield message
        return history

    async def test_missing_report_channel_keeps_user_reply_and_console_diagnostic(self):
        handler, ctx, channel, _ = self.make_handler(channel_available=False)

        console = await self.dispatch(handler, ctx, ValueError("Specific failure"))

        ctx.send.assert_awaited_once()
        channel.send.assert_not_awaited()
        self.assertEqual(handler.notifs, {})
        self.assertIn("is unavailable", console.call_args.args[0])

    async def test_long_error_report_uses_file_and_owner_receives_traceback(self):
        handler, ctx, channel, _ = self.make_handler(owner=True)
        error = ValueError("Long failure " + "x" * 4000)

        with patch("cogs.core.error_handler.traceback.format_tb", return_value=["Known traceback"]):
            await self.dispatch(handler, ctx, error)

        report_file = channel.send.call_args.kwargs["file"]
        self.assertEqual(report_file.filename, "error.txt")
        self.assertIn(b"Known traceback", report_file.fp.read())
        self.assertEqual(ctx.send.await_count, 2)
        self.assertEqual(ctx.send.call_args.kwargs["embed"].description, "Known traceback")

    async def test_known_error_reply_does_not_enter_reporting(self):
        handler, ctx, channel, _ = self.make_handler()

        await self.dispatch(handler, ctx, RuntimeError("Internal error"))

        ctx.send.assert_awaited_once_with("Oop, I had an internal error. Rerun the command")
        channel.send.assert_not_awaited()

    async def test_notifications_evict_oldest_entry_at_limit(self):
        handler, ctx, _, _ = self.make_handler()
        handler.notification_limit = 2
        handler.notifs = {5: 2, 6: 3}

        await self.dispatch(handler, ctx, ValueError("Specific failure"))

        self.assertEqual(handler.notifs, {6: 3, 7: 2})


if __name__ == "__main__":
    unittest.main()
