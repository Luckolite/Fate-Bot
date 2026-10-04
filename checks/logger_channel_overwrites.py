"""Channel overwrite cards retain permission states, field order, and audit data."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from botutils import emojis
from cogs.moderation.logger import Logger


class Channel(SimpleNamespace):
    def __getattr__(self, name):
        return None

    @property
    def overwrites(self):
        self.overwrite_reads += 1
        return self._overwrites.copy()


def channels(before_overwrites, after_overwrites):
    names = {1: "Removed", 2: "Changed", 3: "Added", 4: "Unchanged"}
    guild = SimpleNamespace(
        id=1,
        get_role=lambda identifier: SimpleNamespace(name=names[identifier]) if identifier in names else None,
        get_member=lambda identifier: None,
    )
    common = dict(id=5, guild=guild, name="Channel", mention="<#5>", jump_url="https://example.test/channel", overwrite_reads=0)
    return (
        Channel(**common, _overwrites=before_overwrites),
        Channel(**common, _overwrites=after_overwrites),
    )


class ChannelOverwriteChecks(unittest.IsolatedAsyncioTestCase):
    async def dispatch(self, before, after):
        logger = Logger.__new__(Logger)
        logger.config = {"1": {"ignored_bots": [], "ignored_channels": []}}
        logger.add_to_queue = Mock()
        audit = SimpleNamespace(
            user=SimpleNamespace(id=2, bot=False), user_mention="<@2>",
            user_avatar=None, user_display_avatar=None, reason="Channel maintenance", target=after,
        )
        with patch("cogs.moderation.logger.AuditLogSearch", AsyncMock(return_value=audit)), patch("cogs.moderation.logger.asyncio.sleep", AsyncMock()) as pause:
            await logger.on_guild_channel_update(before, after)
        return logger.add_to_queue, audit, pause

    async def test_removed_changed_added_fields_keep_order_and_audit_context(self):
        removed, changed, added, unchanged = [discord.Object(id=identifier) for identifier in range(1, 5)]
        before, after = channels(
            {removed: discord.PermissionOverwrite(send_messages=True), changed: discord.PermissionOverwrite(send_messages=True), unchanged: discord.PermissionOverwrite()},
            {changed: discord.PermissionOverwrite(send_messages=None), unchanged: discord.PermissionOverwrite(), added: discord.PermissionOverwrite(send_messages=False)},
        )

        queue, audit, pause = await self.dispatch(before, after)

        queue.assert_called_once()
        call = queue.call_args
        self.assertEqual(call.args, ("1", "channel_overwrites"))
        fields = call.kwargs["embed"].fields
        self.assertEqual([field.name for field in fields], [
            "❌ Removed removed", "<:edited:550291696861315093> Changed", "<:plus:548465119462424595> Added",
        ])
        self.assertEqual([field.value for field in fields], [
            f"{emojis.on} send_messages", f"{emojis.off} send_messages", f"{emojis.off} send_messages",
        ])
        self.assertEqual(call.kwargs["details"], {"🔐 Before": 3, "🔐 After": 3, "🔗 Synced": None})
        self.assertEqual(call.kwargs["links"], [("Open channel", after.jump_url, "↗️")])
        self.assertIs(call.kwargs["actor"], audit.user)
        self.assertIs(call.kwargs["target"], after)
        self.assertEqual(call.kwargs["reason"], audit.reason)
        self.assertEqual((before.overwrite_reads, after.overwrite_reads), (1, 1))
        self.assertEqual(pause.await_count, 6)

    async def test_default_to_explicit_deny_is_still_a_change(self):
        target = discord.Object(id=2)
        before, after = channels({target: discord.PermissionOverwrite()}, {target: discord.PermissionOverwrite(send_messages=False)})

        queue, _, _ = await self.dispatch(before, after)

        self.assertEqual(queue.call_args.kwargs["embed"].fields[0].value, f"{emojis.off} send_messages")

    async def test_empty_overwrites_keep_added_and_removed_fallback_text(self):
        before, after = channels({discord.Object(id=1): discord.PermissionOverwrite()}, {discord.Object(id=3): discord.PermissionOverwrite()})

        queue, _, _ = await self.dispatch(before, after)

        self.assertEqual([field.value for field in queue.call_args.kwargs["embed"].fields], ["`had no permissions`", "`has no permissions`"])

    async def test_uncached_target_uses_identifier_fallback(self):
        before, after = channels({}, {discord.Object(id=99): discord.PermissionOverwrite(send_messages=True)})

        queue, _, _ = await self.dispatch(before, after)

        self.assertIn("Unknown target (99)", queue.call_args.kwargs["embed"].fields[0].name)

    async def test_equal_overwrites_make_no_log_or_yield(self):
        target = discord.Object(id=2)
        before, after = channels({target: discord.PermissionOverwrite(send_messages=True)}, {target: discord.PermissionOverwrite(send_messages=True)})

        queue, _, pause = await self.dispatch(before, after)

        queue.assert_not_called()
        pause.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
