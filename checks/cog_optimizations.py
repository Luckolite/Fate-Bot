import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from botutils import emojis
from cogs.core.cc import CustomCommands
from cogs.fun.factions import Factions
from cogs.misc.global_chat import GlobalChat
from cogs.moderation.chat_filter import ChatFilter, safe_regex_query
from cogs.moderation.logger import chain
from cogs.utility.inv_manager import InviteManager
from cogs.utility.self_roles import RoleView


class CogOptimizationChecks(unittest.TestCase):
    def test_custom_command_indexes_are_instance_local_sets(self):
        bot = SimpleNamespace()
        with patch("discord.ext.tasks.Loop.start"):
            first = CustomCommands(bot)
            second = CustomCommands(bot)

        first.guilds.add(123)
        first.cache[123] = {"hello": ["world", time.time()]}

        self.assertIsInstance(first.guilds, set)
        self.assertEqual(second.guilds, set())
        self.assertEqual(second.cache, {})

    def test_logger_chain_preserves_tree_formatting(self):
        self.assertEqual(
            chain(["root\nbranch", "leaf"]),
            f"root\n{emojis.creply} branch\n{emojis.reply} leaf",
        )
        self.assertEqual(
            chain(["root", "leaf"], skip_first=False),
            f"\n{emojis.creply} root\n{emojis.reply} leaf",
        )

    def test_safe_regex_validation_is_cached_by_pattern(self):
        safe_regex_query.cache_clear()

        self.assertEqual(safe_regex_query("blocked*"), "blocked{0,16}")
        first = safe_regex_query.cache_info()
        self.assertEqual(safe_regex_query("blocked*"), "blocked{0,16}")
        second = safe_regex_query.cache_info()

        self.assertEqual(second.hits, first.hits + 1)
        self.assertEqual(second.misses, first.misses)

    def test_regex_filter_reuses_normalized_search_content(self):
        async def run_filter():
            cog = object.__new__(ChatFilter)
            cog.config = {
                1: {
                    "toggle": True,
                    "blacklist": ["b[a@]d"],
                    "whitelist": [],
                    "ignored": [],
                }
            }
            cog.bot = SimpleNamespace(
                loop=asyncio.get_running_loop(),
                log=SimpleNamespace(critical=Mock()),
            )
            return await cog.run_regex_filter(1, "B@D")

        _content, flags = asyncio.run(run_filter())

        self.assertEqual(flags, ["b@d"])

    def test_collect_claims_indexes_guards_and_cleans_missing_channels(self):
        class FakeTextChannel:
            def __init__(self, channel_id, position):
                self.id = channel_id
                self.position = position

        cog = object.__new__(Factions)
        cog.factions = {
            "1": {
                "Alpha": {
                    "balance": 100,
                    "claims": [10, 11],
                }
            }
        }
        cog.boosts = {
            "land-guard": {"1": {"Alpha": time.time() + 60}},
        }
        channel = FakeTextChannel(10, 3)
        cog.bot = SimpleNamespace(
            get_channel=lambda channel_id: channel if channel_id == 10 else None
        )

        async def collect():
            with patch("cogs.fun.factions.discord.TextChannel", FakeTextChannel):
                return await cog.collect_claims("1")

        claims = asyncio.run(collect())

        self.assertEqual(
            claims,
            {10: {"faction": "Alpha", "guarded": True, "position": 3}},
        )
        self.assertEqual(cog.factions["1"]["Alpha"]["claims"], [10])
        self.assertEqual(cog.factions["1"]["Alpha"]["balance"], 350)

    def test_self_role_select_batches_additions_and_removals(self):
        class Role:
            def __init__(self, role_id):
                self.id = role_id
                self.position = role_id

            def __hash__(self):
                return self.id

        selected = Role(10)
        removed = Role(20)
        roles = {selected.id: selected, removed.id: removed}
        member = SimpleNamespace(
            roles=[removed],
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
            get_member=lambda _user_id: member,
            get_role=roles.get,
        )
        telemetry = SimpleNamespace(increment=Mock())
        view = SimpleNamespace(
            bot=SimpleNamespace(get_guild=lambda _guild_id: guild, telemetry=telemetry),
            guild_id=1,
            message_id=99,
            config={1: {"99": {"show_percentage": False}}},
            index=lambda: {selected: {}, removed: {}},
        )
        interaction = SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=5),
            data={"values": [str(selected.id)]},
            message=SimpleNamespace(id=99),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        asyncio.run(RoleView.select_callback(view, interaction))

        member.add_roles.assert_awaited_once_with(selected, atomic=False)
        member.remove_roles.assert_awaited_once_with(removed, atomic=False)
        self.assertEqual(telemetry.increment.call_count, 2)


class AsyncCursor:
    def __init__(self, row=None):
        self.row = row
        self.rowcount = int(row is not None)
        self.execute = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchone(self):
        return self.row


class AsyncIndex(dict):
    def __init__(self, data):
        super().__init__(data)
        self.flush = AsyncMock()


class AsyncOptimizationChecks(unittest.IsolatedAsyncioTestCase):
    async def test_global_chat_status_uses_one_query(self):
        cursor = AsyncCursor(("verified",))
        cog = object.__new__(GlobalChat)
        cog.bot = SimpleNamespace(
            utils=SimpleNamespace(cursor=lambda: cursor),
        )

        self.assertEqual(await cog.get_user_status(42), "verified")
        cursor.execute.assert_awaited_once_with(
            "select status from global_users where user_id = %s;",
            (42,),
        )

    async def test_idle_global_chat_queue_returns_without_scanning_cache(self):
        cog = object.__new__(GlobalChat)
        cog._queue = []

        await GlobalChat.handle_queue.coro(cog)

    async def test_global_chat_channel_index_tracks_webhook_cache(self):
        cog = object.__new__(GlobalChat)
        cog.cache = {}
        cog.active_channel_ids = set()
        webhook = SimpleNamespace(channel=SimpleNamespace(id=55))

        cog.cache_webhook(1, webhook)
        self.assertTrue(cog.is_active_channel(55))

        cog.remove_cached_webhook(1)
        self.assertFalse(cog.is_active_channel(55))

    async def test_invite_resync_skips_flush_when_state_is_unchanged(self):
        invite = SimpleNamespace(code="abc", uses=2)
        index = AsyncIndex(
            {1: {"abc": {"joins": [], "leaves": [], "uses": 2}}}
        )
        cog = object.__new__(InviteManager)
        cog.index = index
        cog.suppressed = ()
        guild = SimpleNamespace(
            id=1,
            invites=AsyncMock(return_value=[invite]),
            get_member=lambda _user_id: None,
        )

        await cog.re_sync(guild)

        index.flush.assert_not_awaited()

    async def test_invite_remove_flushes_only_when_a_record_changes(self):
        index = AsyncIndex(
            {1: {"abc": {"joins": [], "leaves": [], "uses": 2}}}
        )
        cog = object.__new__(InviteManager)
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=999))
        cog.index = index
        member = SimpleNamespace(id=42, guild=SimpleNamespace(id=1))

        await cog.on_member_remove(member)

        index.flush.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
