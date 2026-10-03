"""Snipe SQL checkouts finish before Discord responses."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.fun.fun import Fun
from checks.custom_command_optimizations import Cursor


class SnipeDatabaseChecks(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_and_missing_history_responses_release_connection(self):
        cases = [
            ("snipe enable", False, True, "Enabled sniping", 2),
            ("snipe enable", True, True, "Sniping is already enabled", 1),
            ("snipe disable", True, True, "Disabled sniping", 2),
            ("snipe disable", False, True, "Sniping isn't enabled", 1),
            ("snipe enable", False, False, "Only administrators can enable this", 0),
            ("snipe", False, True, "Snipe requires being enabled by an administrator. Use `.snipe enable`", 1),
            ("snipe", True, True, "Nothing to snipe", 1),
        ]
        for content, enabled, admin, reply, queries in cases:
            with self.subTest(content=content, enabled=enabled, admin=admin):
                cursor = Cursor([(1,)] if enabled else [])
                cog = object.__new__(Fun)
                cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=lambda: cursor))
                cog.dat = {}

                async def send(*args, **kwargs):
                    self.assertEqual(cursor.active, 0)

                ctx = SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2), prefix=".", message=SimpleNamespace(content=content), author=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=admin)), send=AsyncMock(side_effect=send))
                await Fun.snipe.callback(cog, ctx)
                ctx.send.assert_awaited_once_with(reply)
                self.assertEqual(cursor.execute.await_count, queries)


if __name__ == "__main__":
    unittest.main()
