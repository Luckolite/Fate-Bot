"""Focused checks for the active-server owner command."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from discord.ext import commands

from botutils.active_servers import ACTIVE_WINDOW_SECONDS
from cogs.core.statistics import Statistics


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = None
        self.parameters = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, query, parameters=None):
        self.query = query
        self.parameters = parameters

    async def fetchall(self):
        return self.rows


class StatisticsActiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_guilds_use_monthly_xp_and_pending_rows(self):
        cursor = FakeCursor([(101,), (202,)])
        ranking = SimpleNamespace(
            pending_monthly={
                (303, 1, 9_999_999): 5,
                (404, 1, 4_000): 5,
            }
        )
        bot = SimpleNamespace(
            utils=SimpleNamespace(cursor=lambda: cursor),
            get_cog=lambda name: ranking if name == "Ranking" else None,
        )
        cog = Statistics(bot)

        with patch("botutils.active_servers.time", return_value=10_000_000):
            active = await cog.active_guild_ids()

        self.assertEqual(active, {"101", "202", "303"})
        self.assertIn("select distinct guild_id from monthly_msg", cursor.query)
        self.assertEqual(cursor.parameters, (10_000_000 - ACTIVE_WINDOW_SECONDS,))

    async def test_active_command_sends_total(self):
        telemetry = SimpleNamespace(gauge=Mock())
        bot = SimpleNamespace(telemetry=telemetry)
        cog = Statistics(bot)
        cog.active_guild_ids = AsyncMock(return_value={str(i) for i in range(1_234)})
        ctx = SimpleNamespace(send=AsyncMock())

        await Statistics.active.callback(cog, ctx)

        ctx.send.assert_awaited_once_with(
            "1,234 active servers in the last 30 days."
        )
        telemetry.gauge.assert_called_once_with("active_servers", 1_234)

    async def test_statistics_command_has_active_module_page(self):
        bot = SimpleNamespace(
            config={"bot_owner_id": 1},
            cogs={},
            fetch_user=AsyncMock(
                return_value=SimpleNamespace(
                    display_avatar=SimpleNamespace(url="https://example.com/avatar.png")
                )
            ),
            telemetry=SimpleNamespace(gauge=Mock()),
            utils=SimpleNamespace(
                format_dict=lambda values: "\n".join(
                    f"{key}: {value}" for key, value in values.items()
                )
            ),
        )
        cog = Statistics(bot)
        bot.cogs = {
            name: SimpleNamespace(
                **{attribute: {"1": True, 2: True, "inactive": True}}
            )
            for name, attribute in cog.cogs.items()
        }
        cog.active_guild_ids = AsyncMock(return_value={"1", "2", "3"})
        ctx = SimpleNamespace()

        captured_pages = []

        async def capture_menu(_ctx, pages):
            captured_pages.extend(pages)

        with patch("cogs.core.statistics.Menu", side_effect=capture_menu):
            await Statistics.statistics.callback(cog, ctx)

        self.assertEqual(len(captured_pages), 2)
        self.assertEqual(captured_pages[0].footer.text, "Page 1 of 2")
        self.assertEqual(captured_pages[1].footer.text, "Page 2 of 2")
        self.assertEqual(
            captured_pages[1].author.name,
            "Active Module Statistics (30 Days)",
        )
        for module in cog.cogs:
            self.assertIn(f"{module}: 2", captured_pages[1].description)
        bot.telemetry.gauge.assert_not_called()

    async def test_active_module_ids_support_async_database_keys(self):
        class AsyncConfig:
            async def keys(self):
                for guild_id in (1, "2", 3):
                    yield guild_id

        self.assertEqual(
            await Statistics.configured_guild_ids(AsyncConfig()),
            {"1", "2", "3"},
        )

    def test_active_command_is_prefix_only_and_owner_only(self):
        command = Statistics.active

        self.assertIsInstance(command, commands.Command)
        self.assertNotIsInstance(command, commands.HybridCommand)
        self.assertTrue(command.hidden)
        self.assertTrue(command.checks)


if __name__ == "__main__":
    unittest.main()
