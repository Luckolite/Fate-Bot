"""Role permission logs retain changed names, enabled states, and audit context."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from botutils import emojis
from cogs.moderation.logger import Logger


class Role(SimpleNamespace):
    def __getattr__(self, name):
        return None


class RolePermissionChecks(unittest.IsolatedAsyncioTestCase):
    async def dispatch(self, before_permissions, after_permissions):
        guild = SimpleNamespace(id=1)
        common = dict(id=3, guild=guild, name="Role", mention="<@&3>", color=discord.Color.default())
        before = Role(**common, permissions=before_permissions)
        after = Role(**common, permissions=after_permissions)
        audit = SimpleNamespace(
            user=SimpleNamespace(id=2), user_mention="<@2>",
            user_avatar=None, user_display_avatar=None, reason="Updated permissions",
        )
        logger = Logger.__new__(Logger)
        logger.config = {"1": {}}
        logger.add_to_queue = Mock()
        with patch("cogs.moderation.logger.AuditLogSearch", AsyncMock(return_value=audit)):
            await logger.on_guild_role_update(before, after)
        return logger.add_to_queue, after, audit

    async def test_mixed_changes_preserve_card_order_and_archive_context(self):
        queue, after, audit = await self.dispatch(
            discord.Permissions(send_messages=True),
            discord.Permissions(administrator=True, manage_messages=True),
        )

        queue.assert_called_once()
        call = queue.call_args
        self.assertEqual(call.args, ("1", "role_permissions"))
        self.assertEqual(call.kwargs["details"], {
            "🔐 Changed Permissions": ["administrator", "send_messages", "manage_messages"],
        })
        description = call.kwargs["embed"].description
        self.assertIn(f"{emojis.on} administrator", description)
        self.assertIn(f"{emojis.off} send_messages", description)
        self.assertIn(f"{emojis.on} manage_messages", description)
        self.assertIs(call.kwargs["target"], after)
        self.assertIs(call.kwargs["actor"], audit.user)
        self.assertEqual(call.kwargs["reason"], audit.reason)

    async def test_unchanged_permissions_make_no_log(self):
        queue, _, _ = await self.dispatch(discord.Permissions.all(), discord.Permissions.all())

        queue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
