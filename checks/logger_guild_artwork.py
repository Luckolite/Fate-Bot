"""Guild artwork logs keep their cards, recovered files, and archive context."""

import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.moderation.logger import Logger


class Guild(SimpleNamespace):
    def __getattr__(self, name):
        return None


def artwork(url):
    return SimpleNamespace(
        url=url, to_file=AsyncMock(return_value=discord.File(BytesIO(b"old artwork"))),
    )


class GuildArtworkChecks(unittest.IsolatedAsyncioTestCase):
    def make_logger(self):
        logger = Logger.__new__(Logger)
        logger.config = {"1": {}}
        logger.add_to_queue = Mock()
        return logger

    def make_audit(self):
        return SimpleNamespace(
            user=SimpleNamespace(id=2),
            user_mention="<@2>",
            user_display_avatar="https://example.test/actor.png",
            reason="Updated artwork",
        )

    async def dispatch(self, before, after):
        logger = self.make_logger()
        audit = self.make_audit()
        with patch("cogs.moderation.logger.AuditLogSearch", AsyncMock(return_value=audit)):
            await logger.on_guild_update(before, after)
        return logger, audit

    async def test_replacement_preserves_previous_animation_and_archive_context(self):
        previous = artwork("https://example.test/old.gif")
        replacement = artwork("https://example.test/new.png")
        before = Guild(id=1, name="Server", banner=previous)
        after = Guild(id=1, name="Server", banner=replacement)

        logger, audit = await self.dispatch(before, after)

        call = logger.add_to_queue.call_args
        self.assertEqual(call.args, ("1", "new_server_banner"))
        self.assertEqual(call.kwargs["embed"].image.url, replacement.url)
        self.assertEqual(call.kwargs["embed"].thumbnail.url, "attachment://before.gif")
        previous.to_file.assert_awaited_once_with(filename="before.gif")
        self.assertEqual(call.kwargs["file"].fp.read(), b"old artwork")
        self.assertIs(call.kwargs["actor"], audit.user)
        self.assertIs(call.kwargs["target"], after)
        self.assertEqual(call.kwargs["target_label"], "Server")
        self.assertEqual(call.kwargs["reason"], audit.reason)
        self.assertEqual(call.kwargs["details"], {"🖼️ New State": "Changed"})

    async def test_removal_keeps_previous_images_and_banner_before_splash_order(self):
        before = Guild(
            id=1, name="Server",
            banner=artwork("https://example.test/banner.png"),
            splash=artwork("https://example.test/splash.png"),
        )
        after = Guild(id=1, name="Server")

        logger, _ = await self.dispatch(before, after)

        self.assertEqual(
            [call.args[1] for call in logger.add_to_queue.call_args_list],
            ["new_server_banner", "new_server_splash"],
        )
        for call in logger.add_to_queue.call_args_list:
            self.assertEqual(call.kwargs["embed"].image.url, "attachment://before.png")
            self.assertIn("removed the server", call.kwargs["embed"].description)
            self.assertEqual(call.kwargs["details"], {"🖼️ New State": "Removed"})

    async def test_unavailable_previous_artwork_still_logs_new_image(self):
        previous = artwork("https://example.test/old.png")
        previous.to_file.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Expired artwork",
        )
        replacement = artwork("https://example.test/new.png")

        logger, _ = await self.dispatch(
            Guild(id=1, name="Server", splash=previous),
            Guild(id=1, name="Server", splash=replacement),
        )

        call = logger.add_to_queue.call_args
        self.assertEqual(call.kwargs["embed"].image.url, replacement.url)
        self.assertIsNone(call.kwargs["file"])
        self.assertIsNone(call.kwargs["embed"].thumbnail.url)

    async def test_same_urls_do_not_download_or_log_artwork(self):
        previous = artwork("https://example.test/same.png")
        replacement = artwork(previous.url)

        logger, _ = await self.dispatch(
            Guild(id=1, name="Server", banner=previous),
            Guild(id=1, name="Server", banner=replacement),
        )

        logger.add_to_queue.assert_not_called()
        previous.to_file.assert_not_awaited()
        replacement.to_file.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
