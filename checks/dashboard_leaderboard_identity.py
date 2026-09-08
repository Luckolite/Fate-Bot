import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from apps.Dashboard.server import Dashboard


def discord_identity(user_id: int, name: str):
    return SimpleNamespace(
        id=user_id,
        display_name=name,
        display_avatar=SimpleNamespace(
            url=f"https://cdn.discordapp.com/avatars/{user_id}/avatar.png"
        ),
    )


def dashboard_with_bot(bot):
    dashboard = object.__new__(Dashboard)
    dashboard.bot = bot
    dashboard.bot_token = ""
    dashboard.http = None
    dashboard.user_cache = {}
    return dashboard


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self.payload


class FakeHTTP:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        return next(self.responses)


class DashboardLeaderboardIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_board_prefers_cached_member_name_and_avatar(self):
        member = discord_identity(42, "Visible Nickname")
        guild = SimpleNamespace(
            get_member=MagicMock(return_value=member),
            fetch_member=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_guild=MagicMock(return_value=guild),
            get_user=MagicMock(),
            fetch_user=AsyncMock(),
        )
        dashboard = dashboard_with_bot(bot)

        resolved = await dashboard.resolve_user("42", "100", True)

        self.assertEqual(resolved["name"], "Visible Nickname")
        self.assertIn("/42/avatar.png", resolved["avatar"])
        bot.get_user.assert_not_called()
        guild.fetch_member.assert_not_awaited()

    async def test_departed_server_member_uses_global_user_cache(self):
        user = discord_identity(42, "Visible Username")
        guild = SimpleNamespace(
            get_member=MagicMock(return_value=None),
            fetch_member=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_guild=MagicMock(return_value=guild),
            get_user=MagicMock(return_value=user),
            fetch_user=AsyncMock(),
        )
        dashboard = dashboard_with_bot(bot)

        resolved = await dashboard.resolve_user("42", "100", True)

        self.assertEqual(resolved["name"], "Visible Username")
        self.assertIsNotNone(resolved["avatar"])
        guild.fetch_member.assert_not_awaited()

    async def test_uncached_global_user_is_fetched_through_fate(self):
        fetched = discord_identity(42, "Fetched Username")
        bot = SimpleNamespace(
            get_guild=MagicMock(return_value=None),
            get_user=MagicMock(return_value=None),
            fetch_user=AsyncMock(return_value=fetched),
        )
        dashboard = dashboard_with_bot(bot)

        resolved = await dashboard.resolve_user("42", "100", False)

        self.assertEqual(resolved["name"], "Fetched Username")
        self.assertIsNotNone(resolved["avatar"])
        bot.fetch_user.assert_awaited_once_with(42)

    async def test_standalone_server_board_falls_back_to_global_user(self):
        dashboard = object.__new__(Dashboard)
        dashboard.bot = None
        dashboard.bot_token = "token"
        dashboard.http = FakeHTTP(
            [
                FakeResponse(404),
                FakeResponse(
                    200,
                    {
                        "id": "42",
                        "username": "Historical User",
                        "global_name": "Historical Name",
                        "avatar": "avatar-hash",
                        "discriminator": "0",
                    },
                ),
            ]
        )
        dashboard.user_cache = {}

        resolved = await dashboard.resolve_user("42", "100", True)

        self.assertEqual(resolved["name"], "Historical Name")
        self.assertIn("/42/avatar-hash.png", resolved["avatar"])
        self.assertIn("/guilds/100/members/42", dashboard.http.urls[0])
        self.assertIn("/users/42", dashboard.http.urls[1])


if __name__ == "__main__":
    unittest.main()
