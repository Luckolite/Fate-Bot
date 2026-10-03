"""MySQL pool reuse and idle-recycle configuration checks."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from botutils.local_databases import mysql_settings
from cogs.core.tasks import Tasks
from fate import Fate


class PoolChecks(unittest.IsolatedAsyncioTestCase):
    async def test_idle_recycle_is_default_and_configurable_without_pool_clear(self):
        for configured, expected in (({}, 1800), ({"pool_recycle": 60}, 60), ({"pool_recycle": -1}, -1)):
            bot = SimpleNamespace(
                auth={"MySQL": {"host": "127.0.0.1", "port": 3307, "user": "test", "password": "", "db": "test"}},
                config={"mysql": configured}, pool=None, _pool_ready=asyncio.Event(),
                log=Mock(), loop=asyncio.get_running_loop(), telemetry=Mock(),
            )
            pool = Mock()
            with patch("fate.aiomysql.create_pool", AsyncMock(return_value=pool)) as create:
                await Fate.create_pool(bot)
            self.assertEqual(create.call_args.kwargs["pool_recycle"], expected)
            self.assertTrue(bot._pool_ready.is_set())
            pool.clear.assert_not_called()
        self.assertFalse(hasattr(Tasks, "cleanup_pool"))

    def test_config_can_override_encrypted_idle_recycle_setting(self):
        self.assertEqual(mysql_settings({"MySQL": {"pool_recycle": 30}}, {"mysql": {"pool_recycle": 90}})["pool_recycle"], 90)


if __name__ == "__main__":
    unittest.main()
