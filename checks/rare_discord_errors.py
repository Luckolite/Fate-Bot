"""Regression checks for rare transient Discord response and access failures."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from aiohttp import ClientPayloadError

from botutils.views import CancelButton
from cogs.moderation.logger import fetch_guild_invites


class InviteFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_response_is_retried_once(self):
        expected = [SimpleNamespace(code="working")]
        guild = SimpleNamespace(
            invites=AsyncMock(
                side_effect=[ClientPayloadError("truncated response"), expected]
            )
        )

        with patch("cogs.moderation.logger.asyncio.sleep", new=AsyncMock()) as sleep:
            invites = await fetch_guild_invites(guild)

        self.assertEqual(invites, expected)
        self.assertEqual(guild.invites.await_count, 2)
        sleep.assert_awaited_once_with(0)

    async def test_second_truncated_response_degrades_to_unknown_invite(self):
        guild = SimpleNamespace(
            invites=AsyncMock(side_effect=ClientPayloadError("truncated response"))
        )

        with patch("cogs.moderation.logger.asyncio.sleep", new=AsyncMock()):
            invites = await fetch_guild_invites(guild)

        self.assertEqual(invites, [])
        self.assertEqual(guild.invites.await_count, 2)


class CancelButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_message_access_does_not_undo_cancellation(self):
        response = SimpleNamespace(status=403, reason="Forbidden")
        missing_access = discord.Forbidden(
            response,
            {"code": 50001, "message": "Missing Access"},
        )
        view = CancelButton("manage_roles")
        member = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=True),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(get_member=lambda _user_id: member),
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(send_message=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock(side_effect=missing_access)),
        )

        await view.children[0].callback(interaction)

        self.assertTrue(view.is_cancelled)
        self.assertTrue(view.is_finished())
        interaction.response.send_message.assert_awaited_once_with(
            "Cancelled the operation"
        )
        interaction.message.edit.assert_awaited_once_with(view=None)

    async def test_view_error_handler_ignores_missing_access(self):
        response = SimpleNamespace(status=403, reason="Forbidden")
        missing_access = discord.Forbidden(
            response,
            {"code": 50001, "message": "Missing Access"},
        )
        view = CancelButton("manage_roles")

        await view.on_error(SimpleNamespace(), missing_access, view.children[0])


if __name__ == "__main__":
    unittest.main()
