"""Check payload release without changing Discord edit/serialization behavior."""

import asyncio
import gc
import unittest
import weakref
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from weakref import WeakValueDictionary

import discord

from cogs.moderation.logger import Logger
from cogs.moderation import logger as logger_module
from cogs.fun.reactions import Reactions
from cogs.utility.self_roles import SelfRoles
from cogs.utility.welcome import Welcome


class Payload:
    pass


class MenuCache(dict):
    def __init__(self):
        super().__init__()
        self.flush = AsyncMock()

    async def remove(self, key):
        self.pop(key, None)
        await self.flush()


class WelcomeRetentionChecks(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog = object.__new__(Welcome)
        partial = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(get_partial_message=Mock(return_value=partial))
        cog.bot = SimpleNamespace(get_partial_messageable=Mock(return_value=channel))
        cog.msgs = {1: {}}
        return cog, partial, channel

    async def test_only_edit_address_and_text_survive_and_departure_edits(self):
        cog, partial, channel = self.make_cog()
        payload = Payload()
        ref = weakref.ref(payload)
        message = SimpleNamespace(id=3, channel=SimpleNamespace(id=4),
                                  content="Welcome <@2>", embeds=[payload])
        cog.remember_message(1, 2, message)
        del message, payload
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(cog.msgs, {1: {2: (4, 3, "Welcome <@2>")}})
        await cog.on_member_remove(SimpleNamespace(id=2, guild=SimpleNamespace(id=1)))
        cog.bot.get_partial_messageable.assert_called_once_with(4, guild_id=1)
        channel.get_partial_message.assert_called_once_with(3)
        partial.edit.assert_awaited_once_with(content="~~Welcome <@2>~~")
        self.assertEqual(cog.msgs, {})

    async def test_deleted_or_inaccessible_message_releases_snapshot(self):
        for error_type in (discord.NotFound, discord.Forbidden):
            cog, partial, _channel = self.make_cog()
            cog.msgs = {1: {2: (4, 3, "Welcome")}}
            partial.edit.side_effect = error_type(
                SimpleNamespace(status=404, reason="gone"), "gone"
            )
            await cog.on_member_remove(SimpleNamespace(id=2, guild=SimpleNamespace(id=1)))
            self.assertEqual(cog.msgs, {})

    async def test_guild_removal_releases_only_its_snapshots(self):
        cog, _partial, _channel = self.make_cog()
        cog.msgs = {1: {2: (4, 3, "Welcome")}, 5: {6: (7, 8, "Hi")}}
        await cog.on_guild_remove(SimpleNamespace(id=1))
        self.assertEqual(cog.msgs, {5: {6: (7, 8, "Hi")}})


class LoggerRetentionChecks(unittest.IsolatedAsyncioTestCase):
    async def test_idle_cleanup_expires_only_stale_log_and_audit_payloads(self):
        cog = object.__new__(Logger)
        now = 100_000
        stale = SimpleNamespace(created_at=datetime.fromtimestamp(now - 31, timezone.utc))
        fresh = SimpleNamespace(created_at=datetime.fromtimestamp(now - 10, timezone.utc))
        cog.recent_logs = {"1": [["old", now - 86_401], ["recent", now - 1]]}
        cog.message_fetch_rate_limit_alerts = {3: now - 61, 4: now - 1}
        with patch.dict(logger_module.audit_entry_cache, {1: [stale, fresh], 2: [stale]}, clear=True), \
             patch.dict(logger_module.audit_rest_cache, {1: (999, [stale]), 2: (1001, [fresh])}, clear=True), \
             patch("cogs.moderation.logger.monotonic", return_value=1000):
            await cog.cleanup_runtime_state(now)
            self.assertEqual(logger_module.audit_entry_cache, {1: [fresh]})
            self.assertEqual(logger_module.audit_rest_cache, {2: (1001, [fresh])})
        self.assertEqual(cog.recent_logs, {"1": [["recent", now - 1]]})
        self.assertEqual(cog.message_fetch_rate_limit_alerts, {4: now - 1})

    async def test_guild_departure_stops_worker_and_rejoin_recreates_queue(self):
        cog = object.__new__(Logger)
        cog.config = {"1": {"saved": True}}
        payload = Payload()
        ref = weakref.ref(payload)
        for name in ("queue", "health", "recent_logs", "invites", "cycle", "colors", "queue_full_alerts"):
            setattr(cog, name, {"1": payload})
        del payload
        cog.last_log_deliveries = {}
        cog.pool = {}
        cog.permission_queue_watchdogs = {}
        cog.unavailable_channel_notified = {"1"}
        cog.typing = {4: {2: 1}, 5: {3: 1}}
        cog.message_fetch_rate_limit_alerts = {4: 1, 5: 1}
        worker = asyncio.create_task(asyncio.Event().wait())
        cog.bot = SimpleNamespace(tasks={"logger": {"1": worker}})
        guild = SimpleNamespace(id=1, channels=[SimpleNamespace(id=4)], threads=[])
        with patch.dict(logger_module.audit_entry_cache, {1: ["entry"]}, clear=True), \
             patch.dict(logger_module.audit_rest_cache, {1: (1, ["entry"])}, clear=True):
            await cog.on_guild_remove(guild)
            self.assertEqual(logger_module.audit_entry_cache, {})
            self.assertEqual(logger_module.audit_rest_cache, {})
        gc.collect()
        self.assertIsNone(ref())
        self.assertTrue(worker.cancelled())
        self.assertEqual(cog.bot.tasks["logger"], {})
        self.assertEqual(cog.config, {"1": {"saved": True}})
        self.assertEqual(cog.typing, {5: {3: 1}})
        cog.start_worker = Mock()
        await cog.on_guild_join(guild)
        self.assertIsInstance(cog.queue["1"], asyncio.Queue)
        cog.start_worker.assert_called_once_with("1")

    async def test_waiters_share_lock_and_idle_lock_is_released(self):
        cog = object.__new__(Logger)
        cog.message_fetch_locks = WeakValueDictionary()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fetch(message_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            return message_id

        channel = SimpleNamespace(id=4, fetch_message=fetch)
        with patch("cogs.moderation.logger.asyncio.sleep", AsyncMock()):
            first = asyncio.create_task(cog.fetch_message_for_log(channel, 1))
            await entered.wait()
            lock_ref = weakref.ref(cog.message_fetch_locks[4])
            second = asyncio.create_task(cog.fetch_message_for_log(channel, 2))
            ready = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(ready.set_result, None)
            await ready
            self.assertEqual(calls, 1)
            self.assertIs(cog.message_fetch_locks[4], lock_ref())
            release.set()
            self.assertEqual(await asyncio.gather(first, second), [1, 2])
        gc.collect()
        self.assertEqual(dict(cog.message_fetch_locks), {})
        self.assertIsNone(lock_ref())

    async def test_delivery_references_release_primary_and_mirror_payloads(self):
        cog = object.__new__(Logger)
        cog.last_log_deliveries = {}
        channel = SimpleNamespace(id=4, type=discord.ChannelType.text,
                                  _state=SimpleNamespace(), guild=SimpleNamespace(id=1))
        payload = Payload()
        payload_ref = weakref.ref(payload)
        primary = object.__new__(discord.Message)
        primary.id, primary.channel, primary.embeds = 3, channel, [payload]
        mirror = object.__new__(discord.Message)
        mirror.id, mirror.channel, mirror.embeds = 5, channel, [payload]
        cog.remember_log_delivery("1", channel, SimpleNamespace(collapse_signature="same"),
                                  primary, [mirror])
        del primary, mirror, payload
        gc.collect()
        self.assertIsNone(payload_ref())
        delivery = cog.last_log_deliveries["1"][4]
        self.assertIsInstance(delivery["primary"], discord.PartialMessage)
        self.assertEqual(delivery["primary"].id, 3)
        self.assertEqual(delivery["mirrors"][0].id, 5)
        with patch.object(discord.PartialMessage, "edit", AsyncMock()) as edit:
            self.assertTrue(await cog.update_collapsed_delivery(delivery, 2))
        self.assertEqual(edit.await_count, 2)


class ReactionRetentionChecks(unittest.IsolatedAsyncioTestCase):
    async def test_expired_selection_removes_empty_registries(self):
        cog = Reactions(SimpleNamespace())
        cog.sent = {"hug": {1: {"a.gif": Mock()}, 2: {"b.gif": Mock()}}}
        cog.expire_selection(1, "hug", "a.gif")
        self.assertEqual(tuple(cog.sent["hug"]), (2,))
        cog.expire_selection(2, "hug", "b.gif")
        cog.expire_selection(2, "hug", "b.gif")
        self.assertEqual(cog.sent, {})

    async def test_unload_cancels_all_timers(self):
        cog = Reactions(SimpleNamespace())
        handles = [Mock(), Mock()]
        cog.sent = {"hug": {1: {"a": handles[0]}, 2: {"b": handles[1]}}}
        cog.cog_unload()
        for handle in handles:
            handle.cancel.assert_called_once()
        self.assertEqual(cog.sent, {})

    async def test_departure_releases_only_departed_guild(self):
        cog = Reactions(SimpleNamespace())
        first, second = Mock(), Mock()
        cog.sent = {"hug": {1: {"a": first}, 2: {"b": second}}}
        cog.webhook = {3: SimpleNamespace(guild=SimpleNamespace(id=1)),
                       4: SimpleNamespace(guild=SimpleNamespace(id=2))}
        await cog.on_guild_remove(SimpleNamespace(id=1))
        first.cancel.assert_called_once()
        second.cancel.assert_not_called()
        self.assertEqual(tuple(cog.sent["hug"]), (2,))
        self.assertEqual(tuple(cog.webhook), (4,))
        await cog.on_guild_channel_delete(SimpleNamespace(id=4))
        self.assertEqual(cog.webhook, {})


class SelfRoleRetentionChecks(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog = object.__new__(SelfRoles)
        cog.config = MenuCache()
        view = SimpleNamespace(config=cog.config, stop=Mock())
        cog.bot = SimpleNamespace(views={1: {"2": view}})
        return cog, view

    async def test_raw_deleted_message_stops_view_even_without_settings(self):
        cog, view = self.make_cog()
        await cog.on_raw_message_delete(SimpleNamespace(guild_id=1, message_id=2))
        view.stop.assert_called_once()
        self.assertEqual(cog.bot.views, {})

    async def test_bulk_deleted_messages_stop_only_the_deleted_views(self):
        cog, view = self.make_cog()
        remaining = SimpleNamespace(config=cog.config, stop=Mock())
        cog.bot.views[1]["3"] = remaining
        await cog.on_raw_bulk_message_delete(SimpleNamespace(guild_id=1, message_ids={2, 4}))
        view.stop.assert_called_once()
        remaining.stop.assert_not_called()
        self.assertEqual(cog.bot.views, {1: {"3": remaining}})

    async def test_bulk_delete_saves_menu_removals_once(self):
        cog, _view = self.make_cog()
        cog.config[1] = {"2": {}, "3": {}, "4": {}}
        await cog.on_raw_bulk_message_delete(SimpleNamespace(guild_id=1, message_ids={2, 3}))
        self.assertEqual(cog.config[1], {"4": {}})
        cog.config.flush.assert_awaited_once()
        await cog.on_raw_bulk_message_delete(SimpleNamespace(guild_id=1, message_ids={4}))
        self.assertNotIn(1, cog.config)
        self.assertEqual(cog.config.flush.await_count, 2)

    async def test_departure_stops_views(self):
        cog, view = self.make_cog()
        await cog.on_guild_remove(SimpleNamespace(id=1))
        view.stop.assert_called_once()
        self.assertEqual(cog.bot.views, {})

    async def test_unload_preserves_successor_cog_views(self):
        cog, view = self.make_cog()
        successor = SimpleNamespace(config={}, stop=Mock())
        cog.bot.views[1]["3"] = successor
        cog.cog_unload()
        view.stop.assert_called_once()
        successor.stop.assert_not_called()
        self.assertEqual(cog.bot.views, {1: {"3": successor}})


if __name__ == "__main__":
    unittest.main()
