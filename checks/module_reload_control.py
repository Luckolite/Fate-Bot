"""Authenticated reload jobs keep the process alive and report real outcomes."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import web
import discord

from apps.FateControl.server import FateController
from botutils.module_reload import ModuleReloadControl
from cogs.dev.reload import ExtensionResult, Reload
from cogs.core.error_handler import ErrorHandler
from checks.exceptions import IgnoredExit
from botutils.attributes import Attributes


class ModerationReloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {"1": {"usermod": [2], "rolemod": [3], "mute_role": 4}}
        self.bot = SimpleNamespace(cogs={"Moderation": SimpleNamespace(config=self.config)})
        self.attrs = Attributes(self.bot)
        self.member = Mock(spec=discord.Member)
        self.member.guild.id = 1
        self.member.id = 2
        self.member.roles = []
        self.member.guild_permissions.administrator = False

    async def test_user_and_role_moderators_remain_exempt_during_reload(self):
        self.bot.cogs.clear()
        self.assertTrue(self.attrs.is_moderator(self.member))
        self.member.id = 99
        self.member.roles = [SimpleNamespace(id=3)]
        self.assertTrue(self.attrs.is_moderator(self.member))
        self.member.roles = []
        self.assertFalse(self.attrs.is_moderator(self.member))

    async def test_replacement_settings_immediately_replace_saved_permissions(self):
        self.bot.cogs["Moderation"] = SimpleNamespace(config={})
        self.assertFalse(self.attrs.is_moderator(self.member))
        self.bot.cogs.clear()
        self.assertFalse(self.attrs.is_moderator(self.member))

    async def test_startup_without_moderation_is_safe_and_preserves_admins(self):
        self.bot.cogs.clear()
        attrs = Attributes(self.bot)
        self.assertFalse(attrs.is_moderator(self.member))
        self.member.guild_permissions.administrator = True
        self.assertTrue(attrs.is_moderator(self.member))
        self.assertFalse(attrs.is_moderator(SimpleNamespace()))

    async def test_missing_cog_allows_existing_mute_role_but_no_writes(self):
        self.bot.cogs.clear()
        guild = Mock(spec=discord.Guild)
        guild.id = 1
        role = object()
        guild.get_role.return_value = role
        self.assertIs(await self.attrs.get_mute_role(guild, upsert=True), role)
        guild.get_role.return_value = None
        self.assertIsNone(await self.attrs.get_mute_role(guild, upsert=True))
        self.assertEqual(self.config["1"]["mute_role"], 4)
        guild.create_role.assert_not_called()

    async def test_live_reload_replaces_both_helper_references(self):
        self.bot.attrs = self.attrs
        self.bot.utils = SimpleNamespace(attrs=self.attrs)
        Reload(self.bot)
        self.assertIsNot(self.bot.attrs, self.attrs)
        self.assertIs(self.bot.attrs, self.bot.utils.attrs)
        self.bot.cogs.clear()
        self.assertTrue(self.bot.attrs.is_moderator(self.member))

    async def test_error_cleanup_uses_command_owner_while_cog_is_absent(self):
        loop = asyncio.get_running_loop()
        original = loop.get_exception_handler()
        self.addCleanup(loop.set_exception_handler, original)
        bot = SimpleNamespace(loop=loop, cogs={})
        handler = ErrorHandler(bot)
        owner = type("Moderation", (), {"cog_after_invoke": AsyncMock()})()
        ctx = SimpleNamespace(command=SimpleNamespace(cog=owner), cog=None)
        await handler.on_command_error(ctx, IgnoredExit())
        owner.cog_after_invoke.assert_awaited_once_with(ctx)


class ModuleReloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.control = ModuleReloadControl(self.root / "reload.json")
        self.loader = SimpleNamespace(reload_from_control=AsyncMock(return_value={
            "reloaded": 49, "failed": 0, "message": "Reloaded 49 of 49 modules.",
        }))
        self.bot = SimpleNamespace(get_cog=lambda _: self.loader)

    async def test_duplicate_requests_share_one_job_and_one_execution(self):
        first = self.control.queue(self.control.runtime_id)
        second = self.control.queue(self.control.runtime_id)
        self.assertEqual(first["id"], second["id"])
        await self.control.execute_pending(self.bot)
        await self.control.execute_pending(self.bot)
        self.loader.reload_from_control.assert_awaited_once()
        self.assertEqual(self.control.read()["state"], "completed")

    async def test_job_survives_controller_recreation_without_replaying(self):
        job = self.control.queue(self.control.runtime_id)
        controller_side = ModuleReloadControl(self.control.path)
        self.assertEqual(controller_side.queue(self.control.runtime_id)["id"], job["id"])
        await self.control.execute_pending(self.bot)
        self.assertEqual(controller_side.read()["reloaded"], 49)

    async def test_old_runtime_and_expired_requests_are_not_executed(self):
        self.control.queue("old-runtime")
        await self.control.execute_pending(self.bot)
        self.loader.reload_from_control.assert_not_awaited()
        job = self.control.queue(self.control.runtime_id)
        job["requested_at"] = 1
        self.control.write(job)
        await self.control.execute_pending(self.bot)
        self.loader.reload_from_control.assert_not_awaited()
        self.assertEqual(self.control.read()["state"], "failed")

    async def test_failures_are_reported_and_a_later_request_can_run(self):
        self.control.queue(self.control.runtime_id)
        self.loader.reload_from_control.side_effect = RuntimeError("module failed")
        await self.control.execute_pending(self.bot)
        self.assertEqual(self.control.read()["state"], "failed")
        self.assertIn("module failed", self.control.read()["message"])
        self.control.queue(self.control.runtime_id)
        self.loader.reload_from_control.side_effect = None
        await self.control.execute_pending(self.bot)
        self.assertEqual(self.control.read()["state"], "completed")

    async def test_partial_failure_keeps_individual_module_details(self):
        self.control.queue(self.control.runtime_id)
        self.loader.reload_from_control.return_value = {
            "reloaded": 48, "failed": 1, "message": "One failed",
            "errors": [{"module": "cogs.example", "error": "Bad import"}],
        }
        await self.control.execute_pending(self.bot)
        result = self.control.read()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["errors"][0]["module"], "cogs.example")

    async def test_cancellation_records_interruption_and_does_not_replay(self):
        self.control.queue(self.control.runtime_id)
        entered = asyncio.Event()

        async def block():
            entered.set()
            await asyncio.Event().wait()

        self.loader.reload_from_control.side_effect = block
        task = asyncio.create_task(self.control.execute_pending(self.bot))
        await entered.wait()
        self.assertEqual(self.control.read()["state"], "running")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.control.read()["state"], "failed")

    async def test_oversized_and_invalid_state_are_rejected(self):
        for content in ("x" * 65_537, '{"state":"running","requested_at":"bad"}'):
            self.control.path.write_text(content)
            with self.assertRaises(ValueError):
                self.control.read()


class ModuleReloadApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.controller = FateController(config_path=root / "config.json", token_path=root / "control.token", autostart=False)
        self.control = ModuleReloadControl(root / "reload.json")
        self.controller.module_reload_control = lambda: self.control
        self.controller.bot_status = AsyncMock(return_value={
            "online": True, "runtime_id": self.control.runtime_id,
            "capabilities": {"module_reload": True},
        })
        self.controller.restart_bot = AsyncMock()
        self.controller.stop_bot = AsyncMock()

    def request(self, token):
        return SimpleNamespace(
            headers={"Authorization": f"Bearer {token}"}, cookies={}, method="POST",
            json=AsyncMock(return_value={"action": "reload_modules"}),
        )

    async def test_reload_requires_authentication_before_queueing(self):
        with self.assertRaises(web.HTTPUnauthorized):
            await self.controller.bot_action(self.request("incorrect"))
        self.controller.bot_status.assert_not_awaited()
        self.assertFalse(self.control.path.exists())

    async def test_reload_returns_job_without_stopping_or_restarting_bot(self):
        responses = await asyncio.gather(*(
            self.controller.bot_action(self.request(self.controller.token)) for _ in range(2)
        ))
        self.assertTrue(all(response.status == 202 for response in responses))
        ids = [json.loads(response.text)["module_reload"]["id"] for response in responses]
        self.assertEqual(ids[0], ids[1])
        self.controller.restart_bot.assert_not_awaited()
        self.controller.stop_bot.assert_not_awaited()

    async def test_offline_or_old_runtime_is_rejected_without_writing(self):
        for status in (None, {"online": False}, {"online": True}):
            self.controller.bot_status.return_value = status
            with self.assertRaises(web.HTTPConflict):
                await self.controller.reload_modules()
            self.assertFalse(self.control.path.exists())


class ReloadEntryPointTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_handler_has_no_gap_and_restores_original_on_unload(self):
        loop = asyncio.get_running_loop()
        original = loop.get_exception_handler()
        self.addCleanup(loop.set_exception_handler, original)
        bot = SimpleNamespace(loop=loop, _preserve_error_handler_on_reload=True)
        old = ErrorHandler(bot)
        old.cog_unload()
        self.assertIs(loop.get_exception_handler().__self__, old)
        # The still-installed handler safely ignores normal control-flow exits.
        loop.get_exception_handler()(loop, {"exception": IgnoredExit()})
        new = ErrorHandler(bot)
        self.assertIs(new._previous_exception_handler, original)
        self.assertIs(loop.get_exception_handler().__self__, new)
        bot._preserve_error_handler_on_reload = False
        new.cog_unload()
        self.assertIs(loop.get_exception_handler(), original)

    async def test_error_handler_preservation_flag_is_cleared_on_reload_failure(self):
        import discord
        bot = SimpleNamespace(config={"extensions": {}}, reload_extension=AsyncMock(
            side_effect=discord.ext.commands.ExtensionFailed("cogs.core.error_handler", RuntimeError("broken"))
        ))
        cog = Reload(bot)
        observed = []

        async def failed(_extension):
            observed.append(bot._preserve_error_handler_on_reload)
            raise discord.ext.commands.ExtensionFailed("cogs.core.error_handler", RuntimeError("broken"))

        bot.reload_extension.side_effect = failed
        result = await cog._reload_extensions(["core.error_handler"])
        self.assertEqual(observed, [True])
        self.assertFalse(bot._preserve_error_handler_on_reload)
        self.assertFalse(result[0].succeeded)

    async def test_reloading_reload_cog_keeps_shared_lock_and_sync_cooldown(self):
        bot = SimpleNamespace(config={"extensions": {}}, _application_command_sync_at=monotonic(),
                              sync_application_commands=AsyncMock())
        first, replacement = Reload(bot), Reload(bot)
        self.assertIs(first._operation_lock, replacement._operation_lock)
        first._reload_extensions = AsyncMock(return_value=[
            ExtensionResult("mod", "cogs.moderation.mod", "reloaded"),
            ExtensionResult("bad", "cogs.example.bad", error="Bad import"),
        ])
        first._refresh_website = Mock(return_value="refreshed")
        result = await first.reload_from_control()
        self.assertEqual((result["reloaded"], result["failed"]), (1, 1))
        self.assertEqual(result["errors"][0]["error"], "Bad import")
        bot.sync_application_commands.assert_not_awaited()

    async def test_no_overlapping_operations_after_reload_cog_replacement(self):
        bot = SimpleNamespace(config={"extensions": {}}, sync_application_commands=AsyncMock())
        first, replacement = Reload(bot), Reload(bot)
        replacement._reload_extensions = AsyncMock(return_value=[])
        replacement._refresh_website = Mock(return_value="refreshed")
        async with first._operation_lock:
            task = asyncio.create_task(replacement.reload_from_control())
            await asyncio.sleep(0)
            replacement._reload_extensions.assert_not_awaited()
        await task
        replacement._reload_extensions.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
