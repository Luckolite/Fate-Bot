"""Regressions for logger saturation and members removed during AntiSpam."""

import asyncio
import unittest
from collections import defaultdict, deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cogs.moderation.anti_spam import AntiSpam
from cogs.moderation.logger import Logger


class ModerationAlertTests(unittest.IsolatedAsyncioTestCase):
    def test_overflow_severity_and_grouping(self):
        for running in (True, False):
            with self.subTest(worker_running=running):
                cog = Logger.__new__(Logger)
                cog.queue = {"1": asyncio.Queue(maxsize=1)}
                cog.queue["1"].put_nowait(object())
                cog.health = {}
                cog.prepare_log_embed = Mock()
                cog.collapse_signature = Mock(return_value=())
                cog.stage_local_log = Mock()
                cog.bot = SimpleNamespace(log=Mock())
                cog.get_worker_snapshot = Mock(return_value={
                    "blocked_permissions": [], "worker_running": running,
                    "queue_size": 1, "queue_capacity": 1,
                })
                for _ in range(2):
                    cog.add_to_queue("1", "member_ban", embed=discord.Embed())
                expected = cog.bot.log.warning if running else cog.bot.log.critical
                other = cog.bot.log.critical if running else cog.bot.log.warning
                expected.assert_called_once()
                other.assert_not_called()
                self.assertIn("dropped 1 event", expected.call_args.args[0])
                self.assertEqual(cog.health["1"]["dropped"], 2)
                self.assertEqual(cog.stage_local_log.call_count, 2)

    async def test_member_removal_race_and_other_errors(self):
        for action in ("adaptive", "timeout:300", "kick", "ban"):
            for status, code in ((404, 10007), (404, 10003), (403, 50013), (500, 0)):
                with self.subTest(action=action, status=status, code=code):
                    response = SimpleNamespace(status=status, reason="test", headers={})
                    error_type = {404: discord.NotFound, 403: discord.Forbidden}.get(
                        status, discord.HTTPException
                    )
                    error = error_type(response, {"code": code, "message": "test"})
                    member = SimpleNamespace(
                        id=7, top_role=1,
                        guild_permissions=SimpleNamespace(administrator=False),
                        kick=AsyncMock(side_effect=error),
                        ban=AsyncMock(side_effect=error),
                        timeout=AsyncMock(side_effect=error), send=AsyncMock(),
                    )
                    permissions = SimpleNamespace(
                        kick_members=True, ban_members=True, moderate_members=True
                    )
                    message = SimpleNamespace(
                        author=member,
                        guild=SimpleNamespace(id=11, me=SimpleNamespace(
                            top_role=2, guild_permissions=permissions
                        )),
                        channel=SimpleNamespace(id=12, permissions_for=lambda _: permissions),
                    )
                    cog = AntiSpam.__new__(AntiSpam)
                    cog.stats = {}
                    cog.incident_cooldowns = {}
                    cog.incident_history = defaultdict(deque)
                    cog.recent_incidents = defaultdict(deque)
                    cog.get_config = Mock(return_value={
                        "enabled": True, "punishments": {"default": action}
                    })
                    cog.bot = SimpleNamespace(telemetry=Mock(), log=Mock())
                    cog._delete_evidence = AsyncMock(return_value=1)
                    cog._announce_action = AsyncMock()
                    signal = SimpleNamespace(module="rate_limit", reason="spam", records=[
                        SimpleNamespace(author_id=7)
                    ])
                    await cog._handle_incident(message, signal, [signal], 10)
                    stats = cog._stats(11)
                    self.assertEqual(stats["deleted"], 1)
                    self.assertEqual(stats["timeouts"] + stats["kicks"] + stats["bans"], 0)
                    if code == 10007:
                        self.assertEqual(stats["failures"], 0)
                        cog.bot.log.warning.assert_not_called()
                        cog._announce_action.assert_not_awaited()
                    else:
                        self.assertEqual(stats["failures"], 1)
                        cog.bot.log.warning.assert_called_once()
                        cog._announce_action.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
