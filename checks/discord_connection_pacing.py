"""Exercise pacing through aiohttp, including the connection acquisition boundary."""

import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import aiohttp
import discord

from botutils.discord_rate_limits import DiscordRateLimitTracker


class ConnectionPacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_purge_waits_before_acquiring_transport_on_every_attempt(self):
        tracker = DiscordRateLimitTracker(None)
        current = 100.0
        acquired = []
        waits = []

        class ConnectionBoundary(Exception):
            pass

        async def connect(*args, **kwargs):
            acquired.append(current)
            raise ConnectionBoundary

        async def sleep(delay):
            nonlocal current
            # No connection may be acquired while this attempt is pacing.
            self.assertEqual(len(acquired), len(waits))
            waits.append(delay)
            current += delay

        message_id = discord.utils.time_snowflake(
            discord.utils.utcnow() - timedelta(days=30)
        )
        url = f'https://discord.com/api/v10/channels/12345/messages/{message_id}'
        tracker._old_delete_deadlines['12345'] = current + 30
        connector = aiohttp.TCPConnector()
        async with aiohttp.ClientSession(
            connector=connector, trace_configs=[tracker.trace]
        ) as session:
            with patch.object(connector, 'connect', AsyncMock(side_effect=connect)), patch(
                'botutils.discord_rate_limits.monotonic', side_effect=lambda: current
            ), patch('botutils.discord_rate_limits.asyncio.sleep', side_effect=sleep):
                for _ in range(2):
                    with self.assertRaises(ConnectionBoundary):
                        await session.delete(url, headers={'Authorization': 'Bot test'})

        self.assertEqual(len(acquired), 2)
        self.assertEqual(waits[0], 30)
        self.assertAlmostEqual(waits[1], 1.1)
        self.assertEqual(acquired[0], 130)
        self.assertAlmostEqual(acquired[1], 131.1)
