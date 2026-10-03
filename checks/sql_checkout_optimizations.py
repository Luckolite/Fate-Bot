"""SQL connections must be returned before Discord network waits."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.moderation.mod_mail import ModMail
from cogs.core.ranking import Ranking
from cogs.core.user import User
from cogs.core.core import Core
from cogs.moderation.case_manager import CaseManager
from checks.custom_command_optimizations import Cursor


class ModMailChecks(unittest.IsolatedAsyncioTestCase):
    async def test_mod_reply_returns_connection_before_dm_and_channel_updates(self):
        for row in ([], [(5,)]):
            cursor = Cursor(rows=row)

            async def send(*args, **kwargs):
                self.assertEqual(cursor.active, 0)
            user = SimpleNamespace(send=AsyncMock(side_effect=send))
            cog = object.__new__(ModMail)
            cog.bot = SimpleNamespace(
                utils=SimpleNamespace(cursor=lambda: cursor),
                get_user=lambda user_id: user, config={"theme_color": 123},
            )
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=1, icon=None), send=AsyncMock(side_effect=send),
                message=SimpleNamespace(
                    channel=SimpleNamespace(name="Case 2", send=AsyncMock(side_effect=send)),
                    author="Mod", content="hello", attachments=[], delete=AsyncMock(side_effect=send),
                ),
            )
            await cog.mod_reply(ctx)
            if row:
                user.send.assert_awaited_once()
                ctx.message.delete.assert_awaited_once()
            else:
                user.send.assert_not_awaited()
                ctx.send.assert_awaited_once_with("Can't find the case for this thread")


class RankingCleanupChecks(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_releases_connection_between_batches(self):
        cursor = Cursor()
        counts = iter((5000, 5000, 3))

        async def execute(*args):
            cursor.rowcount = next(counts)
        cursor.execute.side_effect = execute
        cog = object.__new__(Ranking)
        cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=lambda: cursor))

        async def pause(*args):
            self.assertEqual(cursor.active, 0)
        with patch("cogs.core.ranking.asyncio.sleep", AsyncMock(side_effect=pause)) as sleep:
            removed = await cog._delete_batches("DELETE FROM sample LIMIT 5000", (123,))
        self.assertEqual(removed, 10003)
        self.assertEqual(cursor.execute.await_count, 3)
        self.assertEqual(sleep.await_count, 2)


class AdminCheckoutChecks(unittest.IsolatedAsyncioTestCase):
    async def test_block_batches_users_and_releases_before_confirmations(self):
        cursor = Cursor()

        async def send(*args, **kwargs):
            self.assertEqual(cursor.active, 0)

        cog = object.__new__(User)
        cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=lambda: cursor))
        users = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        ctx = SimpleNamespace(send=AsyncMock(side_effect=send))
        await User.block.callback(cog, ctx, users, reason="spam")
        cursor.executemany.assert_awaited_once()
        self.assertEqual(cursor.executemany.call_args.args[1], [(user.id, str(user), "spam", user.id) for user in users])
        self.assertEqual(ctx.send.await_count, 2)

    async def test_missing_and_deleted_case_use_single_query_before_reply(self):
        for count in (0, 1):
            cursor = Cursor(rows=[None] * count)

            async def send(*args, **kwargs):
                self.assertEqual(cursor.active, 0)

            cog = object.__new__(CaseManager)
            cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=lambda: cursor))
            ctx = SimpleNamespace(guild=SimpleNamespace(id=1), send=AsyncMock(side_effect=send))
            await CaseManager.del_log.callback(cog, ctx, 42)
            cursor.execute.assert_awaited_once()
            self.assertTrue(cursor.execute.call_args.args[0].startswith("delete from cases"))
            ctx.send.assert_awaited_once_with("Deleted case #42" if count else "There is no case by that number")

    async def test_blocked_guild_returns_connection_before_leaving(self):
        cursor = Cursor(rows=[(1,)])

        async def leave():
            self.assertEqual(cursor.active, 0)

        cog = object.__new__(Core)
        cog.join_dates = {}
        cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=lambda: cursor), is_ready=lambda: True, log=lambda message: None)
        guild = SimpleNamespace(id=1, owner=SimpleNamespace(id=2), me=SimpleNamespace(joined_at=None), leave=AsyncMock(side_effect=leave))
        await cog.on_guild_join(guild)
        guild.leave.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
