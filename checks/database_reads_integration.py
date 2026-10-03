"""Opt-in bot query checks using uniquely owned tables in local fate_test."""

import asyncio
import os
import re
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import aiomysql

from checks.database_optimizations_integration import local_mysql_settings
from cogs.core.core import Core
from cogs.moderation.case_manager import CaseManager
from cogs.moderation.mod_mail import ModMail
from cogs.utility.utility import Utility
from apps.Dashboard.dashboard.ranking_store import BotRankingStore


@unittest.skipUnless(os.environ.get("FATE_TEST_DATABASES") == "1", "isolated DB checks are opt-in")
class DatabaseReadIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await aiomysql.create_pool(**local_mysql_settings(), minsize=1, maxsize=1)
        self.tables = {name: "_optimization_" + uuid4().hex for name in ("cases", "privacy", "invites", "msg", "global_msg")}
        self.queries, self.rows = [], []
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                for name, table in self.tables.items():
                    await cursor.execute(f"CREATE TABLE `{table}` LIKE `{name}`")

        owner = self

        class MappedCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            async def execute(self, query, args=None):
                query = re.sub(r"\b(cases|privacy|invites|msg|global_msg)\b", lambda match: f"`{owner.tables[match[0]]}`", query)
                owner.queries.append((query, args))
                await self.cursor.execute(query, args)

            async def fetchall(self):
                rows = await self.cursor.fetchall()
                owner.rows.append(rows)
                return rows

            def __getattr__(self, name):
                return getattr(self.cursor, name)

        @asynccontextmanager
        async def cursor_factory():
            async with self.pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    yield MappedCursor(cursor)

        self.bot = SimpleNamespace(utils=SimpleNamespace(cursor=cursor_factory))

    async def asyncTearDown(self):
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                for table in self.tables.values():
                    await cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
        self.pool.close()
        await self.pool.wait_closed()

    async def test_concurrent_privacy_reads_share_real_query_and_refresh(self):
        cog = object.__new__(Core)
        cog.bot, cog.privacy_reads = self.bot, {}
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(f"INSERT INTO `{self.tables['privacy']}` VALUES (1, 'nicks', '0')")
        user = SimpleNamespace(id=1)
        values = await asyncio.gather(*(cog.get_privacy(user, "nicks") for _ in range(20)))
        self.assertEqual(values, ["0"] * 20)
        self.assertEqual(len(self.queries), 1)
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(f"UPDATE `{self.tables['privacy']}` SET value = '1'")
        self.assertEqual(await cog.get_privacy(user, "nicks"), "1")
        self.assertEqual(len(self.queries), 2)

    async def test_mod_log_results_are_bounded_and_user_index_is_usable(self):
        table = self.tables["cases"]
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.executemany(
                    f"INSERT INTO `{table}` VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [(1, "42" if index <= 30 else str(1000 + index), "warn", "spam",
                      "https://example.test/case", index, "7", 1) for index in range(1, 6001)],
                )
                await cursor.execute(f"ANALYZE TABLE `{table}`")

        cog = object.__new__(CaseManager)
        cog.bot = SimpleNamespace(
            utils=self.bot.utils, theme_color=1, get_user=lambda uid: "User", decode=lambda value: value,
            wait_for=AsyncMock(side_effect=asyncio.TimeoutError),
        )
        message = SimpleNamespace(id=1, add_reaction=AsyncMock(), clear_reactions=AsyncMock())
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, icon=None), message=SimpleNamespace(raw_mentions=[]),
            send=AsyncMock(return_value=message),
        )
        with patch("cogs.moderation.case_manager.asyncio.sleep", AsyncMock()):
            for args, expected in ((None, list(range(6000, 5984, -1))), ("42", list(range(30, 14, -1))), ("spam", list(range(6000, 5984, -1)))):
                await CaseManager.mod_logs.callback(cog, ctx, args=args)
                self.assertEqual([row[4] for row in self.rows[-1]], expected)
        user_query, args = self.queries[1]
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("EXPLAIN " + user_query, args)
                indexed = await cursor.fetchone()
                await cursor.execute("EXPLAIN " + user_query, (1, 42))
                numeric = await cursor.fetchone()
        self.assertIn("cases_user_idx", indexed[6].split(","))
        self.assertNotIn("cases_user_idx", (numeric[6] or "").split(","))
        print(f"Moderation user lookup estimated rows: numeric={numeric[9]}, string={indexed[9]}")

    async def test_invite_upsert_keeps_metadata_counts_and_timestamps(self):
        cog = object.__new__(Utility)
        cog.bot = self.bot
        info = dict(code="test", guild_id=1, guild_name="Guild", channel_id=2, channel_name="Channel", inviter=3, uses=10)
        cog.collect_invite_info = lambda invite: dict(info)
        with patch("cogs.utility.utility.time", return_value=1000):
            await cog.invite_to_sql(None)
        self.assertEqual(len(self.queries), 1)
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(f"UPDATE `{self.tables['invites']}` SET deleted_at = 1500")
        info.update(guild_name=None, channel_id=None, channel_name=None, inviter=None, uses=2)
        with patch("cogs.utility.utility.time", return_value=2000):
            await asyncio.gather(*(cog.invite_to_sql(None) for _ in range(3)))
        info["uses"] = 12
        await cog.invite_to_sql(None)
        self.assertEqual(len(self.queries), 5)
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(f"SELECT * FROM `{self.tables['invites']}`")
                self.assertEqual(await cursor.fetchall(), (("test", 1, "Guild", 2, "Channel", 3, 12, 1000, 1500),))
        # Incomplete invites can still update an existing required-guild row.
        info.update(guild_id=None, uses=15)
        await cog.invite_to_sql(None)
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(f"SELECT guild_id, uses FROM `{self.tables['invites']}`")
                self.assertEqual(await cursor.fetchone(), (1, 15))

    async def test_dashboard_profile_union_keeps_tied_ranks_and_missing_defaults(self):
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.executemany(f"INSERT INTO `{self.tables['msg']}` VALUES (%s, %s, %s)",
                                         [(7, 1, 250), (7, 2, 250), (7, 3, 300), (8, 4, 1000)])
                await cursor.executemany(f"INSERT INTO `{self.tables['global_msg']}` VALUES (%s, %s)",
                                         [(1, 500), (2, 500), (3, 700)])
        self.bot.wait_for_pool = AsyncMock()
        store = BotRankingStore(self.bot)
        result = await store.profile(2, 7, {"first_lvl_xp_req": 250})
        self.assertEqual((result["server"]["xp"], result["server"]["rank"]), (250, 2))
        self.assertEqual((result["global"]["xp"], result["global"]["rank"]), (500, 2))
        self.assertEqual(len(self.queries), 1)
        result = await store.profile(2, 8, {"first_lvl_xp_req": 250})
        self.assertEqual((result["server"]["xp"], result["server"]["rank"]), (0, None))
        self.assertEqual(result["global"]["rank"], 2)
        result = await store.profile(9, 7, {"first_lvl_xp_req": 250})
        self.assertEqual((result["server"]["xp"], result["global"]["xp"]), (0, 0))

    async def test_modmail_chooser_preserves_first_five_links_and_exact_snowflake(self):
        user_id = 264838866480005122
        links = ["z", "a", "Z", "A", "9", "10", "b", "B"]
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.executemany(
                    f"INSERT INTO `{self.tables['cases']}` VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [(1, str(user_id), "warn", "reason", "https://example.test/" + link, index, "7", 1999)
                     for index, link in enumerate(links, 1)]
                    + [(1, str(user_id + 1), "warn", "other user's case", "https://example.test/0", 9, "7", 1999)],
                )
        self.bot.utils.get_choice = AsyncMock(return_value=None)
        self.bot.get_guild = lambda guild_id: "Guild"
        self.bot.decode = lambda value: value
        cog = object.__new__(ModMail)
        cog.bot = self.bot
        ctx = SimpleNamespace(guild=None, author=SimpleNamespace(id=user_id),
                              message=SimpleNamespace(attachments=[]), send=AsyncMock())
        with patch("cogs.moderation.mod_mail.time", return_value=2000):
            await ModMail.reply.callback(cog, ctx)
        expected = sorted(range(1, 9), key=lambda index: links[index - 1])[:5]
        self.assertEqual([row[1] for row in self.rows[-1]], expected)
        self.assertEqual(len(self.bot.utils.get_choice.call_args.args[1:]), 5)
        self.assertEqual(self.queries[-1][1][0], str(user_id))


if __name__ == "__main__":
    unittest.main()
