import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from cogs.core.error_handler import ErrorHandler
from cogs.moderation.logger import overwrite_target_name
from cogs.utility.self_roles import resolve_component_emoji
from cogs.utility.utility import Utility, resolve_invite_code, webhook_name_error


class RuntimeErrorRegressionTests(unittest.TestCase):
    def test_malformed_invite_text_is_ignored(self):
        self.assertIsNone(resolve_invite_code("discord.gg/test)."))
        self.assertIsNone(resolve_invite_code("discord.gg/tést"))
        self.assertEqual(resolve_invite_code("discord.gg/AbC123"), "AbC123")

    def test_webhook_name_validation_matches_discord_constraints(self):
        self.assertIsNone(webhook_name_error("a"))
        self.assertIsNone(webhook_name_error("a" * 80))
        self.assertEqual(
            webhook_name_error(""),
            "Webhook names must be between 1 and 80 characters.",
        )
        self.assertEqual(
            webhook_name_error("a" * 81),
            "Webhook names must be between 1 and 80 characters.",
        )
        self.assertEqual(
            webhook_name_error("My DiScOrD Relay"),
            'Webhook names cannot contain "discord".',
        )
        self.assertEqual(
            webhook_name_error("Clyde's Relay"),
            'Webhook names cannot contain "clyde".',
        )

    def test_uncached_overwrite_target_gets_stable_label(self):
        guild = SimpleNamespace(
            get_role=MagicMock(return_value=None),
            get_member=MagicMock(return_value=None),
        )
        target = discord.Object(id=123456)

        self.assertEqual(
            overwrite_target_name(guild, target), "Unknown target (123456)"
        )

    def test_stale_custom_component_emoji_is_omitted(self):
        bot = SimpleNamespace(get_emoji=MagicMock(return_value=None))

        self.assertEqual(resolve_component_emoji(bot, "👍"), "👍")
        self.assertIsNone(
            resolve_component_emoji(bot, "<:deleted:123456789012345678>")
        )

        available = object()
        bot.get_emoji.return_value = available
        self.assertIs(
            resolve_component_emoji(bot, "<a:available:123456789012345678>"),
            available,
        )

    def test_task_error_reports_exception_without_inspecting_view_store(self):
        loop = SimpleNamespace(default_exception_handler=MagicMock())
        handler = ErrorHandler.__new__(ErrorHandler)
        handler.bot = SimpleNamespace(
            config={"event_errors": 0},
            get_channel=MagicMock(return_value=None),
        )
        error = RuntimeError("test failure")

        with tempfile.TemporaryDirectory() as directory:
            error_path = Path(directory) / "last-error.log"
            with patch("cogs.core.error_handler.LAST_ERROR_LOG_PATH", error_path):
                handler.task_exception_handler(
                    loop, {"message": "Task exception", "exception": error}
                )
            self.assertEqual(error_path.read_text(encoding="utf-8"), str(error))

        loop.default_exception_handler.assert_called_once()


class RuntimeErrorAsyncRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_webhook_name_does_not_call_discord(self):
        utility = object.__new__(Utility)
        ctx = SimpleNamespace(
            send=AsyncMock(),
            channel=SimpleNamespace(create_webhook=AsyncMock()),
            message=SimpleNamespace(attachments=[]),
        )

        await Utility.create_webhook.callback(
            utility, ctx, name="Unofficial Discord Relay"
        )

        ctx.send.assert_awaited_once_with(
            'Webhook names cannot contain "discord".'
        )
        ctx.channel.create_webhook.assert_not_awaited()

    async def test_channel_less_invite_payload_is_ignored(self):
        bot = SimpleNamespace(
            fetch_invite=AsyncMock(side_effect=KeyError("channel")),
            get_privacy=AsyncMock(return_value=False),
        )
        utility = object.__new__(Utility)
        utility.bot = bot
        utility.afk = {}
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            raw_mentions=[],
            content="discord.gg/AbC123",
        )

        with patch("cogs.utility.utility.asyncio.sleep", new=AsyncMock()), patch(
            "cogs.utility.utility.findall",
            new=AsyncMock(return_value=["discord.gg/AbC123"]),
        ):
            await utility.on_message(message)

        bot.fetch_invite.assert_awaited_once_with("AbC123", with_counts=True)


if __name__ == "__main__":
    unittest.main()
