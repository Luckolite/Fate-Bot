"""Mute-role setup keeps request order, permission checks, and error boundaries."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.moderation.mod import _set_mute_role_overwrites


class Channel:
    def __init__(self, name, events, role, *, existing=False, manageable=True, error=None):
        self.name = name
        self.events = events
        self.role = role
        self.overwrites = {role: object()} if existing else {}
        self.manageable = manageable
        self.error = error

    def permissions_for(self, member):
        self.events.append(("check", self.name))
        return SimpleNamespace(manage_channels=self.manageable)

    async def set_permissions(self, role, **permissions):
        if role is not self.role:
            raise AssertionError("The requested mute role changed")
        self.events.append(("set", self.name, permissions))
        if self.error:
            raise self.error


class MuteRolePermissionChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.role = object()
        self.guild = SimpleNamespace(me=object(), text_channels=[], voice_channels=[])

    def channel(self, name, **options):
        return Channel(name, self.events, self.role, **options)

    async def dispatch(self, *, only_missing):
        async def record_pause(seconds):
            self.events.append(("sleep", seconds))

        with patch("cogs.moderation.mod.asyncio.sleep", AsyncMock(side_effect=record_pause)):
            await _set_mute_role_overwrites(self.guild, self.role, only_missing=only_missing)

    async def test_new_role_sets_all_channels_in_text_then_voice_order(self):
        self.guild.text_channels = [self.channel("text1", existing=True), self.channel("text2", manageable=False)]
        self.guild.voice_channels = [self.channel("voice")]

        await self.dispatch(only_missing=False)

        self.assertEqual(self.events, [
            ("set", "text1", {"send_messages": False}),
            ("set", "text2", {"send_messages": False}),
            ("sleep", 0.5),
            ("set", "voice", {"speak": False}),
            ("sleep", 0.5),
        ])

    async def test_existing_role_skips_unmanageable_and_configured_channels(self):
        self.guild.text_channels = [self.channel("missing"), self.channel("blocked", manageable=False)]
        self.guild.voice_channels = [self.channel("voice"), self.channel("configured", existing=True)]

        await self.dispatch(only_missing=True)

        self.assertEqual(self.events, [
            ("check", "missing"), ("set", "missing", {"send_messages": False}),
            ("check", "blocked"),
            ("check", "voice"), ("set", "voice", {"speak": False}),
            ("check", "configured"),
        ])

    async def test_forbidden_channel_does_not_stop_remaining_setup(self):
        forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Denied")
        self.guild.text_channels = [self.channel("blocked", error=forbidden)]
        self.guild.voice_channels = [self.channel("voice")]

        await self.dispatch(only_missing=False)

        self.assertEqual(self.events[-2:], [("set", "voice", {"speak": False}), ("sleep", 0.5)])
        self.assertEqual([event for event in self.events if event[0] == "sleep"], [("sleep", 0.5)] * 2)

    async def test_http_failure_propagates_before_later_channels(self):
        error = discord.HTTPException(SimpleNamespace(status=500, reason="Failure"), "Failed")
        self.guild.text_channels = [self.channel("failed", error=error), self.channel("later")]

        with self.assertRaises(discord.HTTPException):
            await self.dispatch(only_missing=False)

        self.assertEqual(self.events, [("set", "failed", {"send_messages": False})])

    async def test_cancellation_stops_setup(self):
        self.guild.text_channels = [self.channel("cancelled", error=asyncio.CancelledError())]
        self.guild.voice_channels = [self.channel("later")]

        with self.assertRaises(asyncio.CancelledError):
            await self.dispatch(only_missing=False)

        self.assertEqual(self.events, [("set", "cancelled", {"send_messages": False})])

    async def test_empty_channel_lists_make_no_requests_or_pauses(self):
        await self.dispatch(only_missing=False)
        await self.dispatch(only_missing=True)

        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
