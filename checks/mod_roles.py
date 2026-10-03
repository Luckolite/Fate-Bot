import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord.ext import commands

from cogs.moderation.mod import Moderation


class RoleListingTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_role_cache_returns_actionable_response(self):
        ctx = SimpleNamespace(guild=SimpleNamespace(roles=[]), send=AsyncMock())

        await Moderation.roles.callback(None, ctx)

        ctx.send.assert_awaited_once_with(
            "I don't have this server's role data yet. "
            "Make sure Fate is added to the server, then try again."
        )

    async def test_role_listing_preserves_order_alignment_and_member_counts(self):
        ctx = SimpleNamespace(
            guild=SimpleNamespace(roles=[
                SimpleNamespace(name="@everyone", position=0, members=[1, 2, 3]),
                SimpleNamespace(name="Admin", position=2, members=[1]),
                SimpleNamespace(name="Member", position=1, members=[2, 3]),
            ]),
            send=AsyncMock(),
        )

        await Moderation.roles.callback(None, ctx)

        ctx.send.assert_awaited_once_with(
            "```\nName:       Members:\n"
            "Admin       1\nMember      2\n@everyone   3```"
        )

    async def test_prefix_roles_rejects_private_context_before_server_checks(self):
        ctx = SimpleNamespace(guild=None)
        with self.assertRaises(commands.NoPrivateMessage):
            await Moderation.roles.checks[0](ctx)

    def test_slash_roles_is_guild_only(self):
        self.assertTrue(Moderation.roles.app_command.guild_only)


if __name__ == "__main__":
    unittest.main()
