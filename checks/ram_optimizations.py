"""Regression checks for bounded Discord-bot runtime state."""

import asyncio
import gc
import json
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from weakref import WeakValueDictionary

from botutils.custom_logging import Logging
from botutils.resources import AsyncFileManager
from botutils.stack import Stack
from cogs.fun.factions_rewrite import FactionsRewrite
from cogs.misc.global_chat import GlobalChat
from cogs.utility.giveaways import GiveawayEntryView, Giveaways
from cogs.utility.restore_roles import RestoreRoles
from fate import MAX_DEFERRED_LOGS, MAX_DISCORD_MESSAGE_CACHE


ROOT = Path(__file__).resolve().parents[1]


class RamOptimizationChecks(unittest.TestCase):
    def test_discord_message_cache_is_capped_in_bot_profiles(self):
        self.assertEqual(MAX_DISCORD_MESSAGE_CACHE, 16_000)
        for filename in ("config.json", "config.test.json"):
            config = json.loads((ROOT / "data" / filename).read_text(encoding="utf-8"))
            self.assertLessEqual(
                int(config["max_cached_messages"]), MAX_DISCORD_MESSAGE_CACHE
            )

    def test_deferred_logging_queue_drops_oldest_entries_at_the_limit(self):
        with patch("discord.ext.tasks.Loop.start"):
            logging = Logging(SimpleNamespace())
        for index in range(MAX_DEFERRED_LOGS + 5):
            logging.queue.append(str(index))

        self.assertEqual(len(logging.queue), MAX_DEFERRED_LOGS)
        self.assertEqual(logging.queue[0], "5")

    def test_file_lock_registry_does_not_own_unused_locks(self):
        bot = SimpleNamespace(file_locks=WeakValueDictionary())
        first = AsyncFileManager(bot, "same-path")
        second = AsyncFileManager(bot, "same-path")
        lock_ref = weakref.ref(first._file_lock)

        self.assertIs(first._file_lock, second._file_lock)
        del first
        del second
        gc.collect()

        self.assertIsNone(lock_ref())
        self.assertEqual(dict(bot.file_locks), {})

    def test_stack_discards_inactive_keys(self):
        stack = Stack(interval=60, timeout=900, max_stack=3)
        stack.index = {"old": 1, "recent": 950}
        stack._last_cleanup = 0

        stack.cleanup(now=1_000)

        self.assertEqual(stack.index, {"recent": 950})

    def test_ended_giveaway_view_is_not_persistent(self):
        cog = SimpleNamespace(get_giveaway=lambda *_args: {"entrants": []})

        active = GiveawayEntryView(cog, "1", "2")
        ended = GiveawayEntryView(cog, "1", "2", ended=True)

        self.assertIsNone(active.timeout)
        self.assertEqual(ended.timeout, 1)


class AsyncRamOptimizationChecks(unittest.IsolatedAsyncioTestCase):
    async def test_global_chat_polls_have_a_hard_limit(self):
        cog = object.__new__(GlobalChat)
        cog.bot = SimpleNamespace(loop=asyncio.get_running_loop())
        cog.polls = {}
        cog.poll_expiry_tasks = {}
        cog.max_active_polls = 2
        cog.poll_retention_seconds = 3600

        cog.start_poll(1)
        cog.start_poll(2)
        cog.start_poll(3)

        self.assertEqual(tuple(cog.polls), (2, 3))
        tasks = tuple(cog.poll_expiry_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_factions_pending_channel_counters_are_bounded(self):
        cog = object.__new__(FactionsRewrite)
        cog.ready = asyncio.Event()
        cog.ready.set()
        cog.initialization_error = None
        cog.claim_counter = {}
        cog.repo = SimpleNamespace(one=AsyncMock())

        with patch("cogs.fun.factions_rewrite.MAX_PENDING_CLAIM_CHANNELS", 2):
            for channel_id in (10, 11, 12):
                message = SimpleNamespace(
                    guild=SimpleNamespace(id=1),
                    channel=SimpleNamespace(id=channel_id),
                    author=SimpleNamespace(bot=False),
                )
                await cog.on_message(message)

        self.assertEqual(len(cog.claim_counter), 2)
        self.assertNotIn((1, 10), cog.claim_counter)
        cog.repo.one.assert_not_awaited()

    async def test_role_restore_snapshot_is_released_after_rejoin(self):
        cog = object.__new__(RestoreRoles)
        cog.guilds = ["1"]
        cog.cache = {"1": {"2": [7]}}
        role = SimpleNamespace(position=1)
        member = SimpleNamespace(
            id=2,
            guild=SimpleNamespace(
                id=1,
                me=SimpleNamespace(top_role=SimpleNamespace(position=10)),
                get_role=lambda role_id: role if role_id == 7 else None,
            ),
            add_roles=AsyncMock(),
        )

        with patch("cogs.utility.restore_roles.discord.Role", SimpleNamespace):
            await cog.on_member_join(member)

        self.assertEqual(cog.cache, {})
        member.add_roles.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
