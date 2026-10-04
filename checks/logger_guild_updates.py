"""Guild update logs preserve audit attribution and the intended targets."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.moderation.logger import Logger


class Guild(SimpleNamespace):
    def __getattr__(self, name):
        return None


def guild(**settings):
    defaults = {
        "id": 1,
        "name": "Server",
        "features": [],
        "premium_tier": 0,
        "premium_subscription_count": 0,
        "premium_subscribers": [],
    }
    return Guild(**(defaults | settings))


class GuildUpdateChecks(unittest.IsolatedAsyncioTestCase):
    async def dispatch(self, before, after):
        logger = Logger.__new__(Logger)
        logger.config = {"1": {}}
        logger.add_to_queue = Mock()
        audit = SimpleNamespace(
            user=SimpleNamespace(id=2),
            user_mention="<@2>",
            user_avatar=None,
            user_display_avatar=None,
            reason="Server maintenance",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        with patch("cogs.moderation.logger.AuditLogSearch", AsyncMock(return_value=audit)):
            await logger.on_guild_update(before, after)
        return logger.add_to_queue.call_args_list, audit

    async def test_owner_transfer_targets_new_owner_with_audit_context(self):
        previous = SimpleNamespace(id=3, mention="<@3>")
        new = SimpleNamespace(id=4, mention="<@4>")

        calls, audit = await self.dispatch(guild(owner=previous), guild(owner=new))

        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.args, ("1", "owner_change"))
        self.assertIs(call.kwargs["target"], new)
        self.assertEqual(call.kwargs["target_label"], "New Owner")
        self.assertIs(call.kwargs["actor"], audit.user)
        self.assertEqual(call.kwargs["reason"], audit.reason)
        self.assertIs(call.kwargs["details"]["⬅️ Previous Owner"], previous)

    async def test_system_flags_target_channel_or_server(self):
        channel = SimpleNamespace(id=5, mention="<#5>")
        for system_channel in (channel, None):
            with self.subTest(channel=system_channel):
                before = guild(system_channel=system_channel, system_channel_flags=discord.SystemChannelFlags(join_notifications=True))
                after = guild(system_channel=system_channel, system_channel_flags=discord.SystemChannelFlags(join_notifications=False))

                calls, audit = await self.dispatch(before, after)

                self.assertEqual(len(calls), 1)
                call = calls[0]
                self.assertEqual(call.args, ("1", "system_channel_flags"))
                self.assertIs(call.kwargs["target"], system_channel or after)
                self.assertEqual(call.kwargs["target_label"], "System Channel" if system_channel else "Server")
                self.assertIs(call.kwargs["actor"], audit.user)
                self.assertIn("Join Notifications", call.kwargs["details"]["⚙️ Changed Flags"])

    async def test_boost_log_does_not_attribute_unrelated_guild_audit(self):
        booster = SimpleNamespace(id=6, mention="<@6>")
        calls, _ = await self.dispatch(
            guild(), guild(premium_subscription_count=1, premium_subscribers=[booster]),
        )

        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.args, ("1", "boost"))
        self.assertIs(call.kwargs["target"], booster)
        self.assertNotIn("actor", call.kwargs)
        self.assertNotIn("reason", call.kwargs)

    async def test_settings_change_keeps_guild_target_and_event_timestamp(self):
        after = guild(description="New description")
        calls, audit = await self.dispatch(guild(), after)

        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.args, ("1", "server_settings"))
        self.assertIs(call.kwargs["target"], after)
        self.assertEqual(call.kwargs["target_label"], "Server")
        self.assertEqual(call.kwargs["created_at"], audit.created_at.timestamp())
        self.assertEqual(call.kwargs["details"], {"📝 Description": "Not set → New description"})


if __name__ == "__main__":
    unittest.main()
