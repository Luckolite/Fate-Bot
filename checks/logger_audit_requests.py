"""Regressions for shared audit lookups and Discord's global request budget."""
import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from discord import AuditLogAction, Forbidden
from yarl import URL

from botutils.discord_rate_limits import DiscordRateLimitTracker
from cogs.moderation import logger


class AuditRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        logger.audit_rest_cache.clear()
        logger.audit_entry_cache.clear()

    async def asyncTearDown(self):
        for task in list(logger.audit_rest_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(logger.audit_rest_tasks.values()), return_exceptions=True)
        logger.audit_rest_cache.clear()
        logger.audit_entry_cache.clear()

    def guild(self, entries=(), error=None):
        calls = []
        async def audit_logs(**kwargs):
            calls.append(kwargs)
            await asyncio.sleep(0)
            if error:
                raise error
            for entry in entries:
                yield entry
        return SimpleNamespace(id=123, audit_logs=audit_logs, me=SimpleNamespace(
            guild_permissions=SimpleNamespace(view_audit_log=True))), calls

    async def test_hundred_concurrent_cache_misses_make_one_request(self):
        guild, calls = self.guild()
        results = await asyncio.gather(*(
            logger.AuditLogSearch(guild, AuditLogAction.member_update, target=i)._search()
            for i in range(100)
        ))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(result.entry_id is None for result in results))
        await logger.shared_audit_entries(guild, logger.Logger.past())
        self.assertEqual(len(calls), 1)

    async def test_result_is_filtered_separately_for_each_event(self):
        entry = SimpleNamespace(id=22, created_at=datetime.now(timezone.utc),
            action=AuditLogAction.member_update, target=SimpleNamespace(id=2),
            user=None, reason=None, extra=None, before=None, after=None)
        guild, calls = self.guild((entry,))
        first, second = await asyncio.gather(*(
            logger.AuditLogSearch(guild, AuditLogAction.member_update, target=i)._search()
            for i in (1, 2)
        ))
        self.assertIsNone(first.entry_id)
        self.assertEqual(second.entry_id, 22)
        self.assertEqual(len(calls), 1)

    async def test_miss_expires_and_new_entries_can_be_fetched(self):
        guild, calls = self.guild()
        with patch.object(logger, 'monotonic', return_value=10):
            await logger.shared_audit_entries(guild, logger.Logger.past())
        with patch.object(logger, 'monotonic', return_value=13):
            await logger.shared_audit_entries(guild, logger.Logger.past())
        self.assertEqual(len(calls), 2)

    async def test_forbidden_result_does_not_trigger_retry_storm(self):
        response = SimpleNamespace(status=403, reason='Forbidden')
        guild, calls = self.guild(error=Forbidden(response, 'Missing Permissions'))
        for _ in range(10):
            self.assertEqual(await logger.shared_audit_entries(guild, logger.Logger.past()), ())
        self.assertEqual(len(calls), 1)

    async def test_cancelled_waiter_does_not_cancel_shared_request(self):
        ready = asyncio.Event()
        release = asyncio.Event()
        async def audit_logs(**kwargs):
            ready.set()
            await release.wait()
            if False:
                yield None
        guild = SimpleNamespace(id=123, audit_logs=audit_logs)
        first = asyncio.create_task(logger.shared_audit_entries(guild, logger.Logger.past()))
        await ready.wait()
        second = asyncio.create_task(logger.shared_audit_entries(guild, logger.Logger.past()))
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        release.set()
        self.assertEqual(await second, ())

    async def test_gateway_entry_still_wins_over_cached_rest_miss(self):
        entry = SimpleNamespace(id=22, created_at=datetime.now(timezone.utc),
            action=AuditLogAction.member_update, target=SimpleNamespace(id=2),
            user=None, reason=None, extra=None, before=None, after=None)
        guild, calls = self.guild()
        await logger.shared_audit_entries(guild, logger.Logger.past())
        logger.audit_entry_cache[guild.id] = [entry]
        result = await logger.AuditLogSearch(guild, AuditLogAction.member_update, target=2)
        self.assertEqual(result.entry_id, 22)
        self.assertEqual(len(calls), 1)


class GlobalRequestTests(unittest.IsolatedAsyncioTestCase):
    def params(self, url='https://discord.com/api/v10/guilds/123/audit-logs', auth=True):
        return SimpleNamespace(url=URL(url), headers={'Authorization': 'Bot test'} if auth else {})

    async def test_concurrent_attempts_are_paced_without_a_burst(self):
        tracker = DiscordRateLimitTracker(None)
        now = [100.0]
        sends = []
        async def sleep(delay):
            now[0] += delay
        async def request():
            await tracker._before_request_headers(None, None, self.params())
            sends.append(now[0])
        with patch('botutils.discord_rate_limits.monotonic', side_effect=lambda: now[0]), patch(
            'botutils.discord_rate_limits.asyncio.sleep', side_effect=sleep
        ):
            await asyncio.gather(*(request() for _ in range(100)))
        self.assertEqual(len(sends), 100)
        self.assertTrue(all(b - a >= 0.0249 for a, b in zip(sends, sends[1:])))

    async def test_non_bot_requests_and_interactions_do_not_consume_budget(self):
        tracker = DiscordRateLimitTracker(None)
        for params in (self.params(auth=False), self.params('https://example.com/api/a'),
                       self.params('https://discord.com/api/v10/interactions/123/token/callback')):
            await tracker._before_request_headers(None, None, params)
        self.assertEqual(tracker._next_request_at, 0)

    async def test_global_retry_after_is_honored_even_if_metrics_fail(self):
        tracker = DiscordRateLimitTracker(None)
        now = [100.0]
        waits = []
        async def sleep(delay):
            waits.append(delay)
            now[0] += delay
        params = self.params()
        params.method = 'GET'
        params.response = SimpleNamespace(status=429, headers={
            'X-RateLimit-Global': 'true', 'Retry-After': '2', 'Via': 'discord'})
        with patch('botutils.discord_rate_limits.monotonic', side_effect=lambda: now[0]), patch(
            'botutils.discord_rate_limits.asyncio.sleep', side_effect=sleep
        ):
            await tracker._on_request_end(None, None, params)
            await tracker._before_request_headers(None, None, params)
        self.assertEqual(waits, [2.0])


if __name__ == '__main__':
    unittest.main()
