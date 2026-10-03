"""XP index reuse, cross-instance races, and readiness failure checks."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pymysql.err import OperationalError

from botutils.mysql_indexes import ensure_xp_order_index
from cogs.core.ranking import Ranking


def index_row(column, name="existing", visible="YES"):
    return ("global_msg", 1, name, 1, column, "A", 100, None, None, "", "BTREE", "", "", visible)


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        pass


class IndexChecks(unittest.IsolatedAsyncioTestCase):
    def make_pool(self, rows):
        cursor = SimpleNamespace(execute=AsyncMock(), fetchall=AsyncMock(return_value=rows))
        pool = SimpleNamespace(acquire=lambda: Context(SimpleNamespace(cursor=lambda: Context(cursor))))
        return pool, cursor

    async def test_existing_leading_xp_index_is_reused_by_definition(self):
        pool, cursor = self.make_pool([index_row("xp", "custom_index")])
        self.assertFalse(await ensure_xp_order_index(pool))
        cursor.execute.assert_awaited_once_with("SHOW INDEX FROM `global_msg`")

    async def test_missing_or_invisible_index_requires_online_ddl(self):
        for rows in ([], [index_row("user_id")], [index_row("xp", visible="NO")]):
            pool, cursor = self.make_pool(rows)
            self.assertTrue(await ensure_xp_order_index(pool))
            self.assertIn("ALGORITHM=INPLACE, LOCK=NONE", cursor.execute.call_args.args[0])

    async def test_duplicate_index_race_requires_verifying_the_actual_index(self):
        pool, cursor = self.make_pool([])
        cursor.execute.side_effect = [None, OperationalError(1061, "duplicate index"), None]
        cursor.fetchall.side_effect = [[], [index_row("xp")]]
        self.assertFalse(await ensure_xp_order_index(pool))
        self.assertEqual(cursor.fetchall.await_count, 2)

    async def test_permission_failure_is_propagated(self):
        pool, cursor = self.make_pool([])
        cursor.execute.side_effect = [None, OperationalError(1142, "ALTER denied")]
        with self.assertRaises(OperationalError):
            await ensure_xp_order_index(pool)

    async def test_bot_readiness_coalesces_index_setup_and_logs_failure(self):
        cog = object.__new__(Ranking)
        cog.index_lock, cog.index_ready = asyncio.Lock(), False
        cog.bot = SimpleNamespace(wait_for_pool=AsyncMock(return_value=object()), log=Mock())
        with patch("cogs.core.ranking.ensure_xp_order_index", AsyncMock(return_value=True)) as ensure:
            await asyncio.gather(cog.on_ready(), cog.on_ready())
            ensure.assert_awaited_once()
        cog.index_ready = False
        with patch("cogs.core.ranking.ensure_xp_order_index", AsyncMock(side_effect=OperationalError(1142, "denied"))):
            await cog.on_ready()
        self.assertFalse(cog.index_ready)
        cog.bot.log.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
