import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from apps.Dashboard.dashboard.module_store import BotModuleStore, module_defaults
from apps.Dashboard.server import DASHBOARD_PATH, Dashboard, register_routes


class DashboardModulePermissionTests(unittest.TestCase):
    @staticmethod
    def _actor(**permissions):
        return SimpleNamespace(
            id=42,
            guild_permissions=SimpleNamespace(administrator=False, **permissions),
        )

    def test_locked_module_configuration_is_redacted(self):
        modules = module_defaults()
        modules["chatfilter"].update(
            enabled=True,
            blacklist=["private phrase"],
            ignored_channels=["123"],
        )
        guild = SimpleNamespace(owner_id=1)

        BotModuleStore._apply_module_access(modules, guild, self._actor())

        self.assertTrue(modules["chatfilter"]["locked"])
        self.assertEqual(modules["chatfilter"]["required_permission"], "Manage Messages")
        self.assertTrue(modules["chatfilter"]["enabled"])
        self.assertEqual(modules["chatfilter"]["blacklist"], [])
        self.assertEqual(modules["chatfilter"]["ignored_channels"], [])

    def test_owner_can_read_module_configuration(self):
        modules = module_defaults()
        modules["logger"]["channel_id"] = "123"
        guild = SimpleNamespace(owner_id=42)

        BotModuleStore._apply_module_access(modules, guild, self._actor())

        self.assertFalse(modules["logger"]["locked"])
        self.assertEqual(modules["logger"]["channel_id"], "123")

    def test_giveaways_are_exposed_as_a_live_read_only_module(self):
        modules = module_defaults()
        self.assertIn("giveaways", modules)
        self.assertEqual(modules["giveaways"]["active_count"], 0)
        self.assertEqual(modules["giveaways"]["giveaways"], [])

        cog = SimpleNamespace(
            data={
                "7": {
                    "101": {
                        "id": "101",
                        "prize": "Nitro",
                        "status": "active",
                        "channel_id": 11,
                        "message_id": 101,
                        "winner_count": 2,
                        "entrants": [1, 1, 2],
                        "end_at": "2026-08-29T00:00:00+00:00",
                    },
                    "99": {
                        "id": "99",
                        "prize": "Role",
                        "status": "ended",
                        "channel_id": 11,
                        "message_id": 99,
                        "winner_count": 1,
                        "entrants": [3],
                        "end_at": "2026-08-27T00:00:00+00:00",
                    },
                }
            }
        )
        store = BotModuleStore(
            SimpleNamespace(get_cog=lambda name: cog if name == "Giveaways" else None)
        )

        store._read_giveaways(7, modules["giveaways"])

        giveaway = modules["giveaways"]
        self.assertTrue(giveaway["enabled"])
        self.assertEqual(giveaway["active_count"], 1)
        self.assertEqual(giveaway["recent_count"], 1)
        self.assertEqual(giveaway["total_entries"], 2)
        self.assertEqual(giveaway["giveaways"][0]["prize"], "Nitro")
        self.assertEqual(
            giveaway["giveaways"][0]["url"],
            "https://discord.com/channels/7/11/101",
        )

    def test_dashboard_catalog_has_a_read_only_giveaway_dialog(self):
        javascript = Path("apps/Dashboard/static/dashboard.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("'selfroles', 'giveaways', 'starboard'", javascript)
        self.assertIn("giveaways: { title: 'Giveaways'", javascript)
        self.assertIn("name === 'giveaways'", javascript)
        self.assertIn("byId('module-save').hidden = true", javascript)
        self.assertIn("Open in Discord", javascript)


class DashboardRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        environment = {
            "DASHBOARD_DEV_MODE": "1",
            "DASHBOARD_SESSION_SECRET": "dashboard-route-test-session-secret",
        }
        self.environment = patch.dict(os.environ, environment, clear=False)
        self.environment.start()
        self.dashboard = Dashboard()
        app = web.Application()
        register_routes(app, self.dashboard)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.environment.stop()

    async def test_dashboard_is_the_canonical_path(self):
        response = await self.client.get(DASHBOARD_PATH)

        self.assertEqual(DASHBOARD_PATH, "/dashboard")
        self.assertEqual(response.status, 200)
        self.assertIn("Mission Control", await response.text())
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_legacy_fate_redirects_to_dashboard(self):
        response = await self.client.get("/fate", allow_redirects=False)

        self.assertEqual(response.status, 308)
        self.assertEqual(response.headers["Location"], DASHBOARD_PATH)

    async def test_dev_login_returns_to_dashboard(self):
        response = await self.client.get("/auth/dev", allow_redirects=False)

        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], DASHBOARD_PATH)

    def test_completed_oauth_signin_records_only_the_aggregate_metric(self):
        recorded = []
        self.dashboard.telemetry = SimpleNamespace(
            increment=lambda metric: recorded.append(metric)
        )

        self.dashboard.record_dashboard_signin()

        self.assertEqual(recorded, ["dashboard_signins"])


if __name__ == "__main__":
    unittest.main()
