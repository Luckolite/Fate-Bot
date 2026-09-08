import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.utility.giveaways import GiveawayEntryView, Giveaways, parse_datetime, utcnow


class GiveawayLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.cog = object.__new__(Giveaways)

    def test_legacy_reaction_record_is_migrated(self):
        payload = {
            "1": {
                "2": {
                    "end_time": "2030-06-28 09:26:12.403309",
                    "user": 10,
                    "giveaway": "Nitro",
                    "winners": 2,
                    "channel": 3,
                    "message": 4,
                }
            }
        }

        migrated, changed = Giveaways.normalize_data(payload)
        data = migrated["1"]["2"]

        self.assertTrue(changed)
        self.assertEqual(data["prize"], "Nitro")
        self.assertEqual(data["winner_count"], 2)
        self.assertEqual(data["entry_mode"], "reaction")
        self.assertEqual(parse_datetime(data["end_at"]).year, 2030)

    def test_empty_legacy_guilds_are_pruned(self):
        migrated, changed = Giveaways.normalize_data({"1": {}, "2": {}})

        self.assertTrue(changed)
        self.assertEqual(migrated, {})

    def test_required_and_blocked_roles_are_enforced(self):
        now = utcnow()
        member = SimpleNamespace(
            bot=False,
            roles=[SimpleNamespace(id=7)],
            created_at=now - timedelta(days=100),
            joined_at=now - timedelta(days=50),
        )
        data = {
            "required_role_id": 8,
            "blocked_role_id": None,
            "minimum_account_age_hours": 0,
            "minimum_server_age_hours": 0,
        }
        self.assertIn("<@&8>", Giveaways.eligibility_error(self.cog, member, data))

        data["required_role_id"] = 7
        data["blocked_role_id"] = 7
        self.assertIn("excluded", Giveaways.eligibility_error(self.cog, member, data))

    def test_account_and_server_age_are_enforced(self):
        now = utcnow()
        member = SimpleNamespace(
            bot=False,
            roles=[],
            created_at=now - timedelta(hours=2),
            joined_at=now - timedelta(hours=1),
        )
        data = {
            "required_role_id": None,
            "blocked_role_id": None,
            "minimum_account_age_hours": 24,
            "minimum_server_age_hours": 0,
        }
        self.assertIn("account", Giveaways.eligibility_error(self.cog, member, data))

        data["minimum_account_age_hours"] = 0
        data["minimum_server_age_hours"] = 24
        self.assertIn("server", Giveaways.eligibility_error(self.cog, member, data))

    def test_winner_draw_is_unique_and_honors_count(self):
        now = utcnow()
        members = [
            SimpleNamespace(
                id=user_id,
                bot=False,
                roles=[],
                created_at=now - timedelta(days=100),
                joined_at=now - timedelta(days=50),
            )
            for user_id in range(1, 6)
        ]
        data = {
            "required_role_id": None,
            "blocked_role_id": None,
            "bonus_role_id": None,
            "bonus_entries": 0,
            "minimum_account_age_hours": 0,
            "minimum_server_age_hours": 0,
        }

        winners = Giveaways.draw_winners(self.cog, members, data, 3)

        self.assertEqual(len(winners), 3)
        self.assertEqual(len({winner.id for winner in winners}), 3)

    def test_persistent_button_uses_stable_custom_id(self):
        cog = SimpleNamespace(
            get_giveaway=MagicMock(return_value={"entrants": [1, 2, 2]})
        )

        view = GiveawayEntryView(cog, "10", "abc123")

        self.assertIsNone(view.timeout)
        self.assertEqual(
            view.entry_button.custom_id,
            "fate:giveaway:entry:10:abc123",
        )
        self.assertEqual(view.entry_button.label, "Enter • 2")

    def test_competitor_baseline_lifecycle_commands_are_registered(self):
        commands = {command.name for command in Giveaways.giveaway.commands}

        self.assertEqual(
            commands,
            {"create", "advanced", "list", "info", "end", "reroll", "delete"},
        )
        create = next(
            command for command in Giveaways.giveaway.commands
            if command.name == "create"
        )
        self.assertEqual(create.signature, "<channel> <winners> <duration> <prize>")


class GiveawayAsyncLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_inaccessible_persisted_giveaway_is_removed(self):
        response = SimpleNamespace(status=404, reason="Not Found")
        missing = discord.NotFound(
            response,
            {"code": 10003, "message": "Unknown Channel"},
        )
        data = {
            "id": "abc123",
            "status": "active",
            "channel_id": 3,
            "message_id": 4,
        }
        cog = object.__new__(Giveaways)
        cog.data = {"1": {"abc123": data}}
        cog.finish_locks = {}
        cog.resume_fetch_lock = __import__("asyncio").Lock()
        cog.fetch_message = AsyncMock(side_effect=missing)
        cog.save_data = AsyncMock()
        cog.bot = SimpleNamespace()

        winners = await cog.finish_giveaway("1", "abc123")

        self.assertEqual(winners, [])
        self.assertEqual(cog.data, {})
        cog.save_data.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
