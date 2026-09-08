"""Regression checks for application-command publishing during reloads."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cogs.dev.reload import ExtensionResult, Reload


class AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def make_context():
    return SimpleNamespace(
        author=SimpleNamespace(
            display_name="Owner",
            display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
        ),
        send=AsyncMock(),
        typing=lambda: AsyncContext(),
    )


class ReloadSyncScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_module_reload_does_not_publish_commands(self):
        bot = SimpleNamespace(
            config={"extensions": {}},
            sync_application_commands=AsyncMock(return_value=[]),
        )
        cog = Reload(bot)
        cog._reload_extensions = AsyncMock(
            return_value=[
                ExtensionResult(
                    requested="statistics",
                    extension="cogs.core.statistics",
                    action="reloaded",
                )
            ]
        )
        ctx = make_context()

        await Reload.reload.callback(cog, ctx, "statistics")

        bot.sync_application_commands.assert_not_awaited()
        ctx.send.assert_awaited_once()
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertNotIn(
            "Application commands", {field.name for field in embed.fields}
        )

    async def test_full_reload_still_publishes_commands(self):
        bot = SimpleNamespace(
            config={"extensions": {"core": ["statistics"]}},
            sync_application_commands=AsyncMock(return_value=[object()]),
        )
        cog = Reload(bot)
        cog._reload_extensions = AsyncMock(
            return_value=[
                ExtensionResult(
                    requested="core.statistics",
                    extension="cogs.core.statistics",
                    action="reloaded",
                )
            ]
        )
        cog._refresh_website = Mock(return_value="refreshed")
        ctx = make_context()

        await Reload.reload.callback(cog, ctx)

        bot.sync_application_commands.assert_awaited_once_with(force=True)
        cog._refresh_website.assert_called_once_with()
        embed = ctx.send.await_args.kwargs["embed"]
        application_commands = next(
            field for field in embed.fields if field.name == "Application commands"
        )
        self.assertEqual(
            application_commands.value, "Published 1 global commands."
        )


if __name__ == "__main__":
    unittest.main()
