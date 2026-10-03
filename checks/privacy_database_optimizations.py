"""Privacy saves write only changed settings with bounded SQL round trips."""

import unittest
from copy import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.core.core import Core, default_privacy_settings
from checks.custom_command_optimizations import Cursor


class PrivacyDatabaseChecks(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, rows, changes):
        cursor = Cursor(rows)
        cog = object.__new__(Core)
        cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=lambda: cursor))

        class View:
            def __init__(self, options, user):
                self.result = copy(options)

            async def wait(view, message):
                self.assertEqual(cursor.active, 0)
                view.result.update(changes)

        async def send(*args, **kwargs):
            self.assertEqual(cursor.active, 0)

        ctx = SimpleNamespace(author=SimpleNamespace(id=1), send=AsyncMock(side_effect=send), reply=AsyncMock(side_effect=send))
        with patch("cogs.core.core.Toggles", View):
            await Core.privacy.callback(cog, ctx)
        return cursor, ctx

    async def test_unchanged_dialog_writes_nothing(self):
        cursor, ctx = await self.exercise([], {})
        cursor.execute.assert_awaited_once()
        cursor.executemany.assert_not_awaited()
        ctx.reply.assert_not_awaited()

    async def test_overrides_and_default_resets_each_use_one_batch(self):
        cursor, ctx = await self.exercise([("xp", False)], {"xp": True, "fun_commands": False, "activity_info": True})
        self.assertEqual(cursor.execute.await_count, 1)
        self.assertEqual(cursor.executemany.await_count, 2)
        delete, upsert = cursor.executemany.call_args_list
        self.assertTrue(delete.args[0].startswith("delete from privacy"))
        self.assertEqual(delete.args[1], [(1, "xp")])
        self.assertIn("on duplicate key update", upsert.args[0])
        self.assertEqual(upsert.args[1], [(1, "fun_commands", False), (1, "activity_info", True)])
        ctx.reply.assert_awaited_once_with("Updated your privacy settings")

    async def test_all_nondefault_settings_use_one_upsert(self):
        changes = {item: not value for item, value in default_privacy_settings.items()}
        cursor, _ctx = await self.exercise([], changes)
        cursor.executemany.assert_awaited_once()
        self.assertEqual(len(cursor.executemany.call_args.args[1]), len(default_privacy_settings))


if __name__ == "__main__":
    unittest.main()
