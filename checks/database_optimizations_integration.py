"""Opt-in checks against isolated local databases; never production settings.

Run with FATE_TEST_DATABASES=1 after starting apps/DevServices databases.
Each test owns a uniquely named collection/table and removes only that data.
"""

import asyncio
import json
import os
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from pymongo.errors import BulkWriteError
import aiomysql

from botutils.resources import Cache
from botutils.cache_rewrite import Cache as QueryCache
from botutils.mysql_indexes import ensure_xp_order_index
from botutils.resources import Cursor
from botutils.prefixes import save_prefix
from cogs.core.ranking import Ranking
from cogs.utility.utility import Utility


def local_mysql_settings():
    path = Path(__file__).resolve().parents[1] / ".fate-test-services" / "test-secrets.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    if settings["mysql_database"] != "fate_test":
        raise ValueError("Integration checks require the isolated fate_test database")
    return dict(
        host="127.0.0.1", port=3307, user=settings["mysql_user"],
        password=settings["mysql_password"], db="fate_test", autocommit=True,
    )


@unittest.skipUnless(os.environ.get("FATE_TEST_DATABASES") == "1", "isolated DB checks are opt-in")
class MongoCacheIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.name = "_optimization_" + uuid4().hex
        self.client = AsyncIOMotorClient("mongodb://127.0.0.1:27018", serverSelectionTimeoutMS=3000)
        self.sync_client = MongoClient("mongodb://127.0.0.1:27018", serverSelectionTimeoutMS=3000)
        self.database = self.client["fate_test"]
        self.collection = self.database[self.name]
        self.bot = SimpleNamespace(
            loop=asyncio.get_running_loop(), aio_mongo=self.database,
            mongo=self.sync_client["fate_test"],
        )

    async def asyncTearDown(self):
        await self.collection.drop()
        self.client.close()
        await asyncio.to_thread(self.sync_client.close)

    async def make_cache(self):
        return await asyncio.to_thread(Cache, self.bot, self.name)

    async def test_legacy_insert_replace_remove_round_trip(self):
        cache = await self.make_cache()
        for key in range(300):
            cache[key] = {"nested": {"value": key}}
        await cache.flush()
        self.assertEqual(await self.collection.count_documents({}), 300)
        cache[1]["nested"]["value"] = 500
        await cache.flush()
        self.assertEqual((await self.collection.find_one({"_id": 1}))["nested"]["value"], 500)
        await cache.remove_sub(1, "nested")
        self.assertEqual(await self.collection.find_one({"_id": 1}), {"_id": 1})
        await cache.remove(1)
        self.assertIsNone(await self.collection.find_one({"_id": 1}))

    async def test_duplicate_insert_preserves_external_document(self):
        cache = await self.make_cache()
        await self.collection.insert_one({"_id": 1, "value": "external"})
        cache[1], cache[2] = {"value": "local"}, {"value": "second"}
        await cache.flush()
        self.assertEqual((await self.collection.find_one({"_id": 1}))["value"], "external")
        self.assertEqual((await self.collection.find_one({"_id": 2}))["value"], "second")

    async def test_prefix_writes_coalesce_and_reset_real_documents(self):
        bot = SimpleNamespace(
            guild_prefixes={}, user_prefixes={}, aio_mongo={"GuildPrefixes": self.collection},
        )
        results = await asyncio.gather(*(
            save_prefix(bot, "GuildPrefixes", 1, {"prefix": "!", "override": True})
            for _ in range(20)
        ))
        self.assertEqual(sum(results), 1)
        self.assertEqual(await self.collection.find_one({"_id": 1}), {"_id": 1, "prefix": "!", "override": True})
        self.assertTrue(await save_prefix(bot, "GuildPrefixes", 1, {"prefix": "?", "override": False}))
        self.assertEqual((await self.collection.find_one({"_id": 1}))["prefix"], "?")
        self.assertTrue(await save_prefix(bot, "GuildPrefixes", 1, None))
        self.assertIsNone(await self.collection.find_one({"_id": 1}))
        self.assertEqual(bot.guild_prefixes, {})

    async def test_validation_failure_keeps_failed_document_dirty(self):
        await self.database.create_collection(self.name, validator={"value": {"$type": "int"}})
        cache = await self.make_cache()
        cache[1], cache[2] = {"value": 1}, {"value": "bad"}
        with self.assertRaises(BulkWriteError):
            await cache.flush()
        self.assertIn(1, cache._db_state)
        self.assertNotIn(2, cache._db_state)
        cache[2] = {"value": 2}
        await cache.flush()
        self.assertEqual(await self.collection.count_documents({}), 2)

    async def test_query_cache_bulk_replacements_and_partial_retry(self):
        await self.database.create_collection(self.name, validator={"value": {"$type": "int"}})
        cache = QueryCache(self.bot, self.name)
        cache.changes = {key: {"value": key} for key in range(300)}
        await cache.flush()
        self.assertEqual(await self.collection.count_documents({}), 300)
        cache.changes = {1: {"value": 1000}, 2: {"value": "bad"}}
        with self.assertRaises(BulkWriteError):
            await cache.flush()
        self.assertEqual(set(cache.changes), {1, 2})
        cache.changes[2] = {"value": 2000}
        await cache.flush()
        self.assertEqual((await self.collection.find_one({"_id": 1}))["value"], 1000)
        self.assertEqual((await self.collection.find_one({"_id": 2}))["value"], 2000)
        self.assertEqual(cache.changes, {})

    async def test_query_context_save_and_nested_list_edits_round_trip(self):
        await self.collection.insert_one({"_id": 1, "nested": {"values": [1]}})
        cache = QueryCache(self.bot, self.name)
        async with cache[1] as config:
            config["nested"]["values"].append(2)
        self.assertEqual((await self.collection.find_one({"_id": 1}))["nested"]["values"], [1, 2])
        async with cache[1] as config:
            self.assertEqual(config["nested"]["values"], [1, 2])
        self.assertEqual([key async for key in cache.keys()], [1])


@unittest.skipUnless(os.environ.get("FATE_TEST_DATABASES") == "1", "isolated DB checks are opt-in")
class MySQLPoolIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_shared_leaderboards_execute_full_and_guild_only_queries(self):
        prefix = "_optimization_" + uuid4().hex
        tables = {name: prefix + "_" + name for name in ("msg", "global_msg", "monthly_msg", "global_monthly")}
        pool = await aiomysql.create_pool(**local_mysql_settings(), minsize=1, maxsize=1)
        queries = []

        class MappedCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            async def execute(self, query, args):
                queries.append(query)
                for name, table in tables.items():
                    query = query.replace("from " + name + " ", f"from `{table}` ")
                await self.cursor.execute(query, args)

            async def fetchall(self):
                return await self.cursor.fetchall()

        @asynccontextmanager
        async def cursor_factory():
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    yield MappedCursor(cursor)

        try:
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    schemas = {
                        "msg": "guild_id BIGINT, user_id BIGINT, xp BIGINT",
                        "global_msg": "user_id BIGINT, xp BIGINT",
                        "monthly_msg": "guild_id BIGINT, user_id BIGINT, xp BIGINT, msg_time BIGINT",
                        "global_monthly": "user_id BIGINT, xp BIGINT, timeframe BIGINT",
                    }
                    for name, schema in schemas.items():
                        await cursor.execute(f"CREATE TABLE `{tables[name]}` ({schema})")
                    await cursor.execute(f"INSERT INTO `{tables['msg']}` VALUES (7, 1, 10), (8, 2, 15)")
                    await cursor.execute(f"INSERT INTO `{tables['global_msg']}` VALUES (3, 20), (4, 25)")
                    await cursor.execute(f"INSERT INTO `{tables['monthly_msg']}` VALUES (7, 1, 5, 2000000000), (7, 1, 3, 2000000000), (7, 9, 100, 1), (8, 2, 4, 2000000000)")
                    await cursor.execute(f"INSERT INTO `{tables['global_monthly']}` VALUES (3, 6, 2000000000), (3, 2, 2000000000), (9, 100, 1)")
            from weakref import WeakValueDictionary
            from unittest.mock import patch
            cog = object.__new__(Ranking)
            cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=cursor_factory), log=SimpleNamespace(debug=lambda message: None))
            cog.leaderboard_cache = {}
            cog.leaderboard_locks = WeakValueDictionary()
            with patch("cogs.core.ranking.time", return_value=2_000_000_000):
                first = await cog.get_leaderboard_data(7)
                second = await cog.get_leaderboard_data(8)
            self.assertEqual(first["Msg Leaderboard"], [(1, 10)])
            self.assertEqual(first["Monthly Msg Leaderboard"], [(1, 8)])
            self.assertEqual(second["Msg Leaderboard"], [(2, 15)])
            self.assertEqual(second["Monthly Msg Leaderboard"], [(2, 4)])
            for result in (first, second):
                self.assertEqual(result["Global Msg Leaderboard"], [(4, 25), (3, 20)])
                self.assertEqual(result["Global Monthly Leaderboard"], [(3, 8)])
            self.assertEqual(len(queries), 2)
            self.assertIn("global_monthly", queries[0])
            self.assertNotIn("global_monthly", queries[1])
        finally:
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    for table in tables.values():
                        await cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
            pool.close()
            await pool.wait_closed()

    async def test_history_cleanup_preserves_recent_null_and_unknown_activity(self):
        prefix = "_optimization_" + uuid4().hex
        tables = {name: prefix + "_" + name for name in ("invites", "usernames", "activity")}
        pool = await aiomysql.create_pool(**local_mysql_settings(), minsize=1, maxsize=1)
        now = 2_000_000_000
        month, year = now - 30 * 86400, now - 365 * 86400

        class MappedCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            @property
            def rowcount(self):
                return self.cursor.rowcount

            async def execute(self, query, args):
                for name, table in tables.items():
                    query = query.replace("from " + name + " ", f"from `{table}` ")
                await self.cursor.execute(query, args)

        @asynccontextmanager
        async def cursor_factory():
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    yield MappedCursor(cursor)

        try:
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(f"CREATE TABLE `{tables['invites']}` (id INT PRIMARY KEY, created_at DOUBLE, deleted_at DOUBLE)")
                    await cursor.executemany(f"INSERT INTO `{tables['invites']}` VALUES (%s, %s, %s)", [(1, month - 1, None), (2, now, month - 1), (3, now, None), (4, month, month), (5, None, None)])
                    await cursor.execute(f"CREATE TABLE `{tables['usernames']}` (id INT PRIMARY KEY, changed_at DOUBLE)")
                    await cursor.executemany(f"INSERT INTO `{tables['usernames']}` VALUES (%s, %s)", [(1, month - 1), (2, now), (3, month)])
                    await cursor.execute(f"CREATE TABLE `{tables['activity']}` (id INT PRIMARY KEY, last_online VARCHAR(64), last_message VARCHAR(64))")
                    await cursor.executemany(f"INSERT INTO `{tables['activity']}` VALUES (%s, %s, %s)", [(1, year - 1, year - 1), (2, now, year - 1), (3, None, year - 1), (4, year - 1, None), (5, None, None), (6, '2026-10-02 12:00:00', year - 1), (7, 'unknown', year - 1), (8, year, year)])
            cog = object.__new__(Utility)
            cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=cursor_factory), log=SimpleNamespace(info=lambda message: None))
            from unittest.mock import patch
            with patch("cogs.utility.utility.time", return_value=now):
                await cog._cleanup_history()
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    for name, expected in (("invites", [3, 4, 5]), ("usernames", [2, 3]), ("activity", [2, 5, 6, 7, 8])):
                        await cursor.execute(f"SELECT id FROM `{tables[name]}` ORDER BY id")
                        self.assertEqual([row[0] for row in await cursor.fetchall()], expected)
        finally:
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    for table in tables.values():
                        await cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
            pool.close()
            await pool.wait_closed()

    async def test_warm_connection_reuse_and_expired_idle_replacement(self):
        pool = await aiomysql.create_pool(
            **local_mysql_settings(), minsize=1, maxsize=1, pool_recycle=1800,
        )
        try:
            async with pool.acquire() as first:
                async with first.cursor() as cursor:
                    await cursor.execute("SELECT CONNECTION_ID()")
                    first_id = (await cursor.fetchone())[0]
            async with pool.acquire() as second:
                self.assertIs(first, second)
                # Advance the connection's idle age without a 30-minute wait.
                second._last_usage -= 1801
            async with pool.acquire() as third:
                self.assertIsNot(first, third)
                async with third.cursor() as cursor:
                    await cursor.execute("SELECT CONNECTION_ID()")
                    self.assertNotEqual((await cursor.fetchone())[0], first_id)
        finally:
            pool.close()
            await pool.wait_closed()

    async def test_xp_index_removes_filesort_and_preserves_top_results(self):
        table = "_optimization_" + uuid4().hex
        pool = await aiomysql.create_pool(**local_mysql_settings(), minsize=1, maxsize=2)
        try:
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(f"CREATE TABLE `{table}` (user_id BIGINT PRIMARY KEY, xp BIGINT NOT NULL) ENGINE=InnoDB")
                    await cursor.executemany(
                        f"INSERT INTO `{table}` VALUES (%s, %s)",
                        [(index, index * 7919 % 100000) for index in range(10000)],
                    )
                    query = f"SELECT user_id, xp FROM `{table}` ORDER BY xp DESC LIMIT 256"
                    await cursor.execute("EXPLAIN FORMAT=JSON " + query)
                    before_plan = json.loads((await cursor.fetchone())[0])
                    self.assertIn('"using_filesort": true', json.dumps(before_plan))
                    await cursor.execute(query)
                    expected = await cursor.fetchall()
            self.assertTrue(await ensure_xp_order_index(pool, table))
            self.assertFalse(await ensure_xp_order_index(pool, table))
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute("EXPLAIN FORMAT=JSON " + query)
                    after_plan = json.loads((await cursor.fetchone())[0])
                    self.assertNotIn('"using_filesort": true', json.dumps(after_plan))
                    self.assertIn("fate_xp_order_idx", json.dumps(after_plan))
                    await cursor.execute(query)
                    self.assertEqual(await cursor.fetchall(), expected)
                    await cursor.execute("EXPLAIN ANALYZE " + query)
                    print("Indexed synthetic XP plan:", (await cursor.fetchone())[0].strip())
        finally:
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
            pool.close()
            await pool.wait_closed()

    async def test_ranking_cleanup_deletes_all_expired_rows_in_bounded_batches(self):
        table = "_optimization_" + uuid4().hex
        pool = await aiomysql.create_pool(**local_mysql_settings(), minsize=1, maxsize=1)
        try:
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(f"CREATE TABLE `{table}` (id INT PRIMARY KEY, stamp DOUBLE NOT NULL) ENGINE=InnoDB")
                    await cursor.executemany(
                        f"INSERT INTO `{table}` VALUES (%s, %s)",
                        [(index, 0) for index in range(10003)] + [(10003, 10000)],
                    )
            cog = object.__new__(Ranking)
            bot = SimpleNamespace(pool=pool)
            bot.utils = SimpleNamespace(cursor=lambda: Cursor(bot))
            cog.bot = bot
            removed = await cog._delete_batches(f"DELETE FROM `{table}` WHERE stamp < %s LIMIT 5000", (5,))
            self.assertEqual(removed, 10003)
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(f"SELECT id FROM `{table}`")
                    self.assertEqual(await cursor.fetchall(), ((10003,),))
        finally:
            async with pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
            pool.close()
            await pool.wait_closed()


if __name__ == "__main__":
    unittest.main()
