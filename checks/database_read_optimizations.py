"""Fresh privacy reads, bounded moderation results, and prefix acknowledgements."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from pymongo.errors import AutoReconnect

from botutils.prefixes import save_prefix
from checks.custom_command_optimizations import Cursor
from cogs.core.core import Core, default_privacy_settings
from cogs.moderation.case_manager import CaseManager
from cogs.utility.utility import InfoView


class PrivacyReadChecks(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, rows=()):
        cursor = Cursor(rows)
        cog = object.__new__(Core)
        cog.privacy_reads = {}
        cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=lambda: cursor))
        return cog, cursor

    async def test_simultaneous_settings_share_one_read_and_release_it(self):
        cog, cursor = self.make_cog([("nicks", False), ("activity_info", True)])
        user = SimpleNamespace(id=1)
        values = await asyncio.gather(
            *(cog.get_privacy(user, item) for item in default_privacy_settings)
        )
        expected = {**default_privacy_settings, "nicks": False, "activity_info": True}
        self.assertEqual(values, list(expected.values()))
        cursor.execute.assert_awaited_once()
        self.assertEqual(cursor.active, 0)
        self.assertEqual(cog.privacy_reads, {})

    async def test_next_event_sees_changed_settings_without_ttl_delay(self):
        cog, cursor = self.make_cog([("activity_info", True)])
        user = SimpleNamespace(id=1)
        self.assertTrue(await cog.get_privacy(user, "activity_info"))
        cursor.rows = [("activity_info", False)]
        self.assertFalse(await cog.get_privacy(user, "activity_info"))
        self.assertEqual(cursor.execute.await_count, 2)

    async def test_different_users_do_not_share_results(self):
        cog, cursor = self.make_cog()
        await asyncio.gather(*(cog.get_privacy(SimpleNamespace(id=i), "nicks") for i in (1, 2)))
        self.assertEqual(cursor.execute.await_count, 2)
        self.assertEqual({call.args[1] for call in cursor.execute.call_args_list}, {(1,), (2,)})

    async def test_one_cancelled_waiter_does_not_cancel_shared_read(self):
        cog, cursor = self.make_cog([("nicks", False)])
        started, proceed = asyncio.Event(), asyncio.Event()

        async def execute(*args):
            started.set()
            await proceed.wait()
        cursor.execute.side_effect = execute
        user = SimpleNamespace(id=1)
        first = asyncio.create_task(cog.get_privacy(user, "nicks"))
        await started.wait()
        second = asyncio.create_task(cog.get_privacy(user, "nicks"))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        proceed.set()
        self.assertFalse(await second)
        cursor.execute.assert_awaited_once()
        self.assertEqual(cog.privacy_reads, {})

    async def test_failed_read_is_not_cached_and_can_retry(self):
        cog, cursor = self.make_cog()
        cursor.execute.side_effect = RuntimeError("database unavailable")
        with self.assertRaises(RuntimeError):
            await cog.get_privacy(SimpleNamespace(id=1), "nicks")
        self.assertEqual(cog.privacy_reads, {})
        cursor.execute.side_effect = None
        self.assertTrue(await cog.get_privacy(SimpleNamespace(id=1), "nicks"))


class PrefixWriteChecks(unittest.IsolatedAsyncioTestCase):
    def make_bot(self):
        collection = SimpleNamespace(update_one=AsyncMock(), delete_one=AsyncMock())
        bot = SimpleNamespace(guild_prefixes={}, user_prefixes={}, aio_mongo={
            "GuildPrefixes": collection, "UserPrefixes": collection,
        })
        return bot, collection

    async def test_new_and_unchanged_prefixes_use_one_upsert(self):
        for name in ("GuildPrefixes", "UserPrefixes"):
            bot, collection = self.make_bot()
            value = {"prefix": "!"}
            self.assertTrue(await save_prefix(bot, name, 1, value))
            self.assertFalse(await save_prefix(bot, name, 1, value))
            collection.update_one.assert_awaited_once_with({"_id": 1}, {"$set": value}, upsert=True)

    async def test_failed_write_does_not_change_runtime_prefix(self):
        bot, collection = self.make_bot()
        bot.guild_prefixes[1] = {"prefix": "!"}
        collection.update_one.side_effect = AutoReconnect("uncertain acknowledgement")
        with self.assertRaises(AutoReconnect):
            await save_prefix(bot, "GuildPrefixes", 1, {"prefix": "?"})
        self.assertEqual(bot.guild_prefixes[1], {"prefix": "!"})
        # An unacknowledged write may have reached Mongo. Reassert even the
        # previously cached prefix rather than treating it as a no-op.
        collection.update_one.side_effect = None
        self.assertTrue(await save_prefix(bot, "GuildPrefixes", 1, {"prefix": "!"}))
        self.assertEqual(collection.update_one.await_count, 2)
        self.assertEqual(bot._prefix_uncertain_writes, set())

    async def test_concurrent_equal_edits_write_once(self):
        bot, collection = self.make_bot()
        self.assertEqual(await asyncio.gather(
            *(save_prefix(bot, "GuildPrefixes", 1, {"prefix": "!"}) for _ in range(20))
        ), [True] + [False] * 19)
        collection.update_one.assert_awaited_once()
        self.assertEqual(len(bot._prefix_write_locks), 0)

    async def test_reset_waits_for_pending_write_before_deleting(self):
        bot, collection = self.make_bot()
        started, proceed = asyncio.Event(), asyncio.Event()

        async def write(*args, **kwargs):
            started.set()
            await proceed.wait()
        collection.update_one.side_effect = write
        write_task = asyncio.create_task(save_prefix(bot, "GuildPrefixes", 1, {"prefix": "!"}))
        await started.wait()
        reset = asyncio.create_task(save_prefix(bot, "GuildPrefixes", 1, None))
        await asyncio.sleep(0)
        collection.delete_one.assert_not_awaited()
        proceed.set()
        self.assertEqual(await asyncio.gather(write_task, reset), [True, True])
        self.assertNotIn(1, bot.guild_prefixes)
        self.assertFalse(await save_prefix(bot, "GuildPrefixes", 1, None))
        collection.delete_one.assert_awaited_once_with({"_id": 1})

    async def test_failed_reset_preserves_runtime_prefix(self):
        bot, collection = self.make_bot()
        bot.guild_prefixes[1] = {"prefix": "!"}
        collection.delete_one.side_effect = AutoReconnect("uncertain delete")
        with self.assertRaises(AutoReconnect):
            await save_prefix(bot, "GuildPrefixes", 1, None)
        self.assertEqual(bot.guild_prefixes[1], {"prefix": "!"})


class BoundedQueryChecks(unittest.IsolatedAsyncioTestCase):
    async def test_all_mod_log_filters_limit_rows_before_reply(self):
        for args, mentions in ((None, []), ("42", []), (None, [42]), ("spam", [])):
            cursor = Cursor()
            cog = object.__new__(CaseManager)
            cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=lambda: cursor))

            async def send(*args, **kwargs):
                self.assertEqual(cursor.active, 0)
            ctx = SimpleNamespace(guild=SimpleNamespace(id=1), message=SimpleNamespace(raw_mentions=mentions), send=AsyncMock(side_effect=send))
            await CaseManager.mod_logs.callback(cog, ctx, args=args)
            self.assertIn("order by case_number desc limit 16;", cursor.execute.call_args.args[0])
            ctx.send.assert_awaited_once()

    async def test_missing_invite_releases_connection_and_ends_generator(self):
        cursor = Cursor()
        view = object.__new__(InfoView)

        async def send(*args, **kwargs):
            self.assertEqual(cursor.active, 0)
        view.ctx = SimpleNamespace(message=SimpleNamespace(content="discord.gg/missing"), send=AsyncMock(side_effect=send))
        view.bot = SimpleNamespace(
            user=SimpleNamespace(display_avatar=SimpleNamespace(url="https://example.test/avatar.png")),
            utils=SimpleNamespace(cursor=lambda: cursor), encode=lambda value: value,
            fetch_invite=AsyncMock(side_effect=discord.NotFound(Mock(status=404), "missing")),
        )
        self.assertEqual([result async for result in view.invite_info()], [])
        view.ctx.send.assert_awaited_once_with("Failed to query that invite")


if __name__ == "__main__":
    unittest.main()
