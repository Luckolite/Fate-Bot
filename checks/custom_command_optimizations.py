"""Custom-command query caching and short MySQL checkout checks."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.core.cc import CustomCommands


class Cursor:
    def __init__(self, rows=()):
        self.active = 0
        self.rows = rows
        self.rowcount = len(rows)
        self.execute = AsyncMock()
        self.executemany = AsyncMock()

    async def __aenter__(self):
        self.active += 1
        return self

    async def __aexit__(self, *args):
        self.active -= 1

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return self.rows


class CustomCommandChecks(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, cursor):
        cog = object.__new__(CustomCommands)
        cog.cache = {}
        cog.guilds = {1}
        cog.bot = SimpleNamespace(
            utils=SimpleNamespace(cursor=lambda: cursor),
            get_command=lambda command: None,
            attrs=SimpleNamespace(is_moderator=lambda author: True),
        )
        return cog

    async def test_missing_command_is_queried_once_until_cleanup(self):
        cursor = Cursor()
        cog = self.make_cog(cursor)
        msg = SimpleNamespace(guild=SimpleNamespace(id=1, owner=object()), content=".missing")
        with patch("cogs.core.cc.get_prefixes_async", AsyncMock(return_value=["<@1>", "<@!1>", "."])):
            for _ in range(20):
                await cog.on_message(msg)
            cursor.execute.assert_awaited_once()
            cog.cache[1]["missing"][1] = 0
            await CustomCommands.cleanup_task.coro(cog)
            await cog.on_message(msg)
        self.assertEqual(cursor.execute.await_count, 2)

    async def test_connection_is_released_before_custom_response(self):
        cursor = Cursor(rows=[("hello",)])
        cog = self.make_cog(cursor)

        async def respond(*args):
            self.assertEqual(cursor.active, 0)
        cog.process_command = AsyncMock(side_effect=respond)
        msg = SimpleNamespace(guild=SimpleNamespace(id=1, owner=object()), content=".hello")
        with patch("cogs.core.cc.get_prefixes_async", AsyncMock(return_value=["<@1>", "<@!1>", "."])):
            await cog.on_message(msg)
        cog.process_command.assert_awaited_once_with(msg, "hello", "hello")

    async def test_connection_is_released_while_removal_menu_waits(self):
        cursor = Cursor(rows=[("hello",), ("bye",)])
        cog = self.make_cog(cursor)
        cog.cache[1] = {"hello": ["response", 0]}

        async def choose(*args, **kwargs):
            self.assertEqual(cursor.active, 0)
            await asyncio.sleep(0)
            return ["hello", "bye"]

        async def send(*args):
            self.assertEqual(cursor.active, 0)
        ctx = SimpleNamespace(author=object(), guild=SimpleNamespace(id=1), send=AsyncMock(side_effect=send))
        with patch("cogs.core.cc.GetChoice", choose):
            await CustomCommands.remove.callback(cog, ctx)
        cursor.executemany.assert_awaited_once_with(
            "delete from cc where guild_id = %s and command = %s;",
            [(1, "hello"), (1, "bye")],
        )
        self.assertNotIn(1, cog.cache)
        self.assertEqual(ctx.send.await_count, 2)


if __name__ == "__main__":
    unittest.main()
