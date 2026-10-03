"""Battle statistics count in MySQL and return connections before replies."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.fun.fun import Fun
from checks.custom_command_optimizations import Cursor


class BattleDatabaseChecks(unittest.IsolatedAsyncioTestCase):
    async def test_stats_use_one_count_query_and_release_before_reply(self):
        for opponent in (None, SimpleNamespace(id=2)):
            cursor = Cursor([(10000,), (5000,)] + ([(3,), (4,)] if opponent else []))
            cog = object.__new__(Fun)
            cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=lambda: cursor), config={"theme_color": 123})

            async def send(**kwargs):
                self.assertEqual(cursor.active, 0)

            ctx = SimpleNamespace(author=SimpleNamespace(id=1), send=AsyncMock(side_effect=send))
            await Fun.battle.callback(cog, ctx, "stats", opponent)
            cursor.execute.assert_awaited_once()
            query, args = cursor.execute.call_args.args
            self.assertNotIn("select *", query)
            self.assertEqual(query.count("count(*)"), 4 if opponent else 2)
            self.assertEqual(args, (2, 2, 1, 2, 2, 1) if opponent else (1, 1))
            description = ctx.send.call_args.kwargs["embed"].description
            self.assertEqual(description, "**10000** wins and **5000** losses" + (". You've won 3 times against them and lost 4 times" if opponent else ""))


if __name__ == "__main__":
    unittest.main()
