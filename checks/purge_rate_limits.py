"""Purge selection, old-message pacing, and cancellation regressions."""

import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from yarl import URL

from botutils.discord_rate_limits import DiscordRateLimitTracker
from cogs.moderation.mod import Moderation, delete_purge_messages


def message(number, *, days=30, error=None):
    created = discord.utils.utcnow() - timedelta(days=days)
    return SimpleNamespace(
        id=discord.utils.time_snowflake(created) + number,
        delete=AsyncMock(side_effect=error),
        type=discord.MessageType.default,
        content="match", clean_content="match", author="User", jump_url="https://example.com",
    )


def http_error(code, *, status=400):
    response = SimpleNamespace(status=status, reason="test error")
    cls = discord.NotFound if status == 404 else discord.Forbidden if status == 403 else discord.HTTPException
    return cls(response, {"code": code, "message": "test error"})


class OldDeletePacingTests(unittest.IsolatedAsyncioTestCase):
    def params(self, *, channel=12345, days=30, method="DELETE"):
        msg = message(1, days=days)
        return SimpleNamespace(
            method=method,
            url=URL(f"https://discord.com/api/v10/channels/{channel}/messages/{msg.id}"),
            headers={"Authorization": "Bot test"},
        )

    async def asyncSetUp(self):
        self.current = 100.0
        self.waits = []

        async def sleep(delay):
            self.waits.append(delay)
            self.current += delay

        self.patches = [
            patch("botutils.discord_rate_limits.monotonic", side_effect=lambda: self.current),
            patch("botutils.discord_rate_limits.asyncio.sleep", side_effect=sleep),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.tracker = DiscordRateLimitTracker(None)

    async def send(self, params):
        await self.tracker._before_request_start(None, None, params)
        return self.current

    async def test_concurrent_old_deletes_and_retries_are_spaced_per_channel(self):
        params = self.params()
        sends = await asyncio.gather(*(self.send(params) for _ in range(8)))
        self.assertTrue(all(b - a >= 1.0999 for a, b in zip(sends, sends[1:])))

    async def test_new_messages_and_other_channels_keep_global_pacing(self):
        start = await self.send(self.params())
        for params in (self.params(days=1), self.params(channel=67890), self.params(method="GET")):
            self.assertLess(await self.send(params) - start, 0.1)

    async def test_retry_after_extends_only_affected_channel_even_without_telemetry(self):
        params = self.params()
        await self.send(params)
        params.response = SimpleNamespace(status=429, headers={"Retry-After": "8", "Via": "discord"})
        await self.tracker._on_request_end(None, None, params)
        self.assertLess(await self.send(self.params(channel=67890)), 100.1)
        self.assertGreaterEqual(await self.send(params), 108.1)

    async def test_exhausted_server_bucket_is_respected(self):
        params = self.params()
        await self.send(params)
        params.response = SimpleNamespace(status=204, headers={
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "120",
        })
        await self.tracker._on_request_end(None, None, params)
        self.assertGreaterEqual(await self.send(params), 220.1)

    async def test_expired_channel_deadlines_are_removed(self):
        await self.send(self.params())
        self.current += 10
        await self.send(self.params(method="GET"))
        self.assertEqual(self.tracker._old_delete_deadlines, {})

    async def test_channel_wait_releases_global_lock_and_is_cancellable(self):
        params = self.params()
        await self.send(params)
        self.current += 0.1
        entered = asyncio.Event()

        async def hold(delay):
            entered.set()
            await asyncio.Event().wait()

        with patch("botutils.discord_rate_limits.asyncio.sleep", side_effect=hold):
            waiting = asyncio.create_task(self.send(params))
            await entered.wait()
            try:
                self.assertFalse(self.tracker._request_lock.locked())
                self.assertEqual(await self.send(self.params(channel=67890)), self.current)
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
        self.current += 2
        await self.send(params)


class PurgeDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_ages_use_bulk_only_for_recent_selected_messages(self):
        recent = [message(i, days=1) for i in range(3)]
        old = message(4)
        channel = SimpleNamespace(delete_messages=AsyncMock())
        result = await delete_purge_messages(channel, [*recent, old, old])
        channel.delete_messages.assert_awaited_once_with(recent)
        old.delete.assert_awaited_once()
        for msg in recent:
            msg.delete.assert_not_awaited()
        self.assertEqual(result, [*recent, old])

    async def test_large_recent_selection_is_batched_to_100(self):
        selected = [message(i, days=1) for i in range(205)]
        channel = SimpleNamespace(delete_messages=AsyncMock())
        self.assertEqual(await delete_purge_messages(channel, selected), selected)
        self.assertEqual([len(call.args[0]) for call in channel.delete_messages.await_args_list], [100, 100, 5])

    async def test_age_error_falls_back_only_to_the_selected_batch(self):
        selected = [message(i, days=1) for i in range(3)]
        channel = SimpleNamespace(delete_messages=AsyncMock(side_effect=http_error(50034)))
        self.assertEqual(await delete_purge_messages(channel, selected), selected)
        for msg in selected:
            msg.delete.assert_awaited_once()

    async def test_forbidden_and_server_errors_do_not_start_single_delete_storm(self):
        for error in (http_error(50013, status=403), http_error(0, status=500)):
            selected = [message(i, days=1) for i in range(3)]
            channel = SimpleNamespace(delete_messages=AsyncMock(side_effect=error))
            with self.assertRaises(discord.HTTPException):
                await delete_purge_messages(channel, selected)
            for msg in selected:
                msg.delete.assert_not_awaited()

    async def test_deleted_message_is_skipped_but_missing_channel_stops(self):
        missing = message(1, error=http_error(10008, status=404))
        present = message(2)
        self.assertEqual(await delete_purge_messages(None, [missing, present]), [present])
        missing.delete.side_effect = http_error(10003, status=404)
        present.delete.reset_mock()
        with self.assertRaises(discord.NotFound):
            await delete_purge_messages(None, [missing, present])
        present.delete.assert_not_awaited()

    async def test_cancellation_stops_before_next_message(self):
        first, second = message(1), message(2)
        entered = asyncio.Event()

        async def block():
            entered.set()
            await asyncio.Event().wait()

        first.delete.side_effect = block
        task = asyncio.create_task(delete_purge_messages(None, [first, second]))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        second.delete.assert_not_awaited()


class PurgeCommandTests(unittest.IsolatedAsyncioTestCase):
    def context(self, selected):
        scans = []

        async def history(**kwargs):
            scans.append(kwargs)
            for msg in selected:
                yield msg

        settings = SimpleNamespace(get_config=lambda _: {"purge_limit": 1000, "purge_confirmation": False})
        moderation = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: settings), _purge_tasks=set())
        channel = SimpleNamespace(history=history, delete_messages=AsyncMock())
        ctx = SimpleNamespace(
            channel=channel, guild=SimpleNamespace(id=1), defer=AsyncMock(), send=AsyncMock(),
            message=SimpleNamespace(reference=None, raw_mentions=[], delete=AsyncMock()),
        )
        return moderation, ctx, scans

    async def test_filtered_purge_deletes_only_preview_and_never_rescans(self):
        first, excluded, last = message(1), message(2), message(3)
        excluded.content = "unrelated"
        moderation, ctx, scans = self.context([first, excluded, last])
        await Moderation.purge.callback(moderation, ctx, args="match 2")
        self.assertEqual(len(scans), 1)
        first.delete.assert_awaited_once()
        last.delete.assert_awaited_once()
        excluded.delete.assert_not_awaited()
        self.assertEqual(moderation._purge_tasks, set())

    async def test_command_cancellation_joins_background_deletion(self):
        first, second = message(1), message(2)
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def block():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        first.delete.side_effect = block
        moderation, ctx, _ = self.context([first, second])
        task = asyncio.create_task(Moderation.purge.callback(moderation, ctx, args="2"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(moderation._purge_tasks, set())
        second.delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
