import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from discord import Embed, Member

from apps.Dashboard.dashboard.validation import (
    MAX_PURGE_LIMIT,
    ValidationError,
    validate_settings,
)
from cogs.moderation.logger import Log, Logger


def unknown_delete(message_id: int, reference: str) -> Log:
    embed = Embed(
        description=(
            f"ID: `{message_id}`\n"
            "In <#123456789012345678>\n"
            "Recovery: No cached or local copy was available"
        )
    )
    embed.set_author(name="Unknown Message Deleted")
    embed.set_footer(text=f"Log {reference}")
    log = Log(
        "message_delete",
        embed=embed,
        links=[("Open channel", "https://discord.com/channels/1/2", "↗️")],
    )
    log.collapse_signature = Logger.collapse_signature(log)
    return log


class DuplicateSignatureTests(unittest.TestCase):
    def test_unknown_deletions_ignore_delivery_ids(self):
        first = unknown_delete(1258696952929980447, "ca494fcc")
        second = unknown_delete(1256936866804924467, "fc969ec0")

        self.assertEqual(first.collapse_signature, second.collapse_signature)

    def test_meaningful_card_text_remains_distinct(self):
        first = unknown_delete(1258696952929980447, "ca494fcc")
        second = unknown_delete(1256936866804924467, "fc969ec0")
        second.embeds[0].description += "\nBy <@987654321098765432>"
        second.collapse_signature = Logger.collapse_signature(second)

        self.assertNotEqual(first.collapse_signature, second.collapse_signature)


class DuplicateDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_matching_delivery_edits_primary_and_mirror(self):
        logger = object.__new__(Logger)
        logger.last_log_deliveries = {}
        channel = SimpleNamespace(id=55)
        primary = SimpleNamespace(edit=AsyncMock())
        mirror = SimpleNamespace(edit=AsyncMock())
        first = unknown_delete(1258696952929980447, "ca494fcc")
        second = unknown_delete(1256936866804924467, "fc969ec0")
        logger.remember_log_delivery("1", channel, first, primary, [mirror])

        delivered = await logger.collapse_consecutive_log("1", channel, second)

        self.assertIs(delivered, primary)
        primary.edit.assert_awaited_once_with(content="2 Occurrences")
        mirror.edit.assert_awaited_once_with(content="2 Occurrences")
        self.assertEqual(logger.last_log_deliveries["1"][55]["count"], 2)

    async def test_nonmatching_delivery_is_not_edited(self):
        logger = object.__new__(Logger)
        logger.last_log_deliveries = {}
        channel = SimpleNamespace(id=55)
        primary = SimpleNamespace(edit=AsyncMock())
        first = unknown_delete(1258696952929980447, "ca494fcc")
        second = unknown_delete(1256936866804924467, "fc969ec0")
        second.embeds[0].description += "\nBy <@987654321098765432>"
        second.collapse_signature = Logger.collapse_signature(second)
        logger.remember_log_delivery("1", channel, first, primary, [])

        delivered = await logger.collapse_consecutive_log("1", channel, second)

        self.assertIsNone(delivered)
        primary.edit.assert_not_awaited()

    async def test_occurrence_three_and_later_are_collected_before_editing(self):
        logger = object.__new__(Logger)
        logger.last_log_deliveries = {}
        channel = SimpleNamespace(id=55)
        primary = SimpleNamespace(edit=AsyncMock())
        log = unknown_delete(1258696952929980447, "ca494fcc")
        logger.remember_log_delivery("1", channel, log, primary, [])

        await logger.collapse_consecutive_log("1", channel, log)
        with patch("cogs.moderation.logger.asyncio.sleep", new=AsyncMock()):
            await logger.collapse_consecutive_log("1", channel, log)
            await logger.collapse_consecutive_log("1", channel, log)
            pending = logger.last_log_deliveries["1"][55]["pending_update"]
            await pending

        self.assertEqual(primary.edit.await_count, 2)
        primary.edit.assert_awaited_with(content="4 Occurrences")

    async def test_occurrence_arriving_during_flush_gets_another_batch(self):
        logger = object.__new__(Logger)
        logger.last_log_deliveries = {}
        channel = SimpleNamespace(id=55)
        primary = SimpleNamespace(edit=AsyncMock())
        log = unknown_delete(1258696952929980447, "ca494fcc")
        logger.remember_log_delivery("1", channel, log, primary, [])
        delivery = logger.last_log_deliveries["1"][55]

        await logger.collapse_consecutive_log("1", channel, log)

        async def edit(content):
            if content == "3 Occurrences":
                delivery["count"] = 4

        primary.edit.side_effect = edit
        with patch("cogs.moderation.logger.asyncio.sleep", new=AsyncMock()):
            await logger.collapse_consecutive_log("1", channel, log)
            await delivery["pending_update"]

        self.assertEqual(primary.edit.await_count, 3)
        primary.edit.assert_awaited_with(content="4 Occurrences")


class IgnoredBotEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_ignore_command_persists_member_bot_ids(self):
        logger = object.__new__(Logger)
        logger.config = {
            "1": {
                "secure": False,
                "ignored_bots": [],
                "ignored_channels": [],
            }
        }
        logger.save_data = AsyncMock()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, owner=SimpleNamespace(id=2)),
            author=SimpleNamespace(id=3),
            send=AsyncMock(),
        )
        bot_member = MagicMock(spec=Member)
        bot_member.id = 987654321098765432
        bot_member.mention = "<@987654321098765432>"

        await Logger._ignore.callback(logger, ctx, target=bot_member)

        self.assertEqual(
            logger.config["1"]["ignored_bots"],
            [987654321098765432],
        )
        logger.save_data.assert_awaited_once()

    async def test_cached_bot_edit_is_filtered_before_any_snapshot_or_log(self):
        logger = object.__new__(Logger)
        logger.config = {"1": {"ignored_bots": ["987654321098765432"]}}
        logger.run_checks = AsyncMock(return_value="1")
        logger.capture_message_snapshot = AsyncMock()
        message = SimpleNamespace(
            author=SimpleNamespace(id=987654321098765432)
        )

        await logger.on_message_edit(message, message)

        logger.capture_message_snapshot.assert_not_awaited()

    def test_ignored_bot_ids_accept_json_strings(self):
        logger = object.__new__(Logger)
        logger.config = {"1": {"ignored_bots": ["987654321098765432"]}}

        self.assertTrue(logger.is_ignored_bot("1", 987654321098765432))


class PurgeLimitTests(unittest.TestCase):
    @staticmethod
    def payload(limit: int) -> dict:
        return {
            "general": {"purge_limit": limit},
            "prefix": {"value": "."},
            "ranking": {},
            "messages": {},
            "verification": {},
        }

    def test_overall_purge_limit_is_3000(self):
        self.assertEqual(MAX_PURGE_LIMIT, 3000)
        settings = validate_settings(self.payload(3000))
        self.assertEqual(settings["general"]["purge_limit"], 3000)

        with self.assertRaises(ValidationError):
            validate_settings(self.payload(3001))


if __name__ == "__main__":
    unittest.main()
