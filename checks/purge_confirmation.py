import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from apps.Dashboard.dashboard.settings_store import GENERAL_DEFAULTS
from apps.Dashboard.dashboard.validation import ValidationError, validate_settings
from cogs.core.settings import DEFAULT_SETTINGS
from cogs.moderation.mod import Moderation, PurgeConfirmation, purge_confirmation_enabled


def payload(confirmation=None):
    general = {"purge_limit": 1000}
    if confirmation is not None:
        general["purge_confirmation"] = confirmation
    return {
        "general": general,
        "prefix": {"value": "."},
        "ranking": {},
        "messages": {},
        "verification": {},
    }


class PurgeConfirmationTests(unittest.TestCase):
    def test_confirmation_is_enabled_by_default_everywhere(self):
        self.assertIs(DEFAULT_SETTINGS["purge_confirmation"], True)
        self.assertIs(GENERAL_DEFAULTS["purge_confirmation"], True)
        settings = validate_settings(payload())
        self.assertIs(settings["general"]["purge_confirmation"], True)

    def test_dashboard_accepts_boolean_opt_in(self):
        settings = validate_settings(payload(True))
        self.assertIs(settings["general"]["purge_confirmation"], True)
        settings = validate_settings(payload(False))
        self.assertIs(settings["general"]["purge_confirmation"], False)

        with self.assertRaises(ValidationError):
            validate_settings(payload("true"))

    def test_purge_prompts_by_default_and_honors_an_explicit_opt_out(self):
        self.assertTrue(purge_confirmation_enabled({}))
        self.assertFalse(purge_confirmation_enabled({"purge_confirmation": False}))
        self.assertTrue(purge_confirmation_enabled({"purge_confirmation": "true"}))
        self.assertTrue(purge_confirmation_enabled({"purge_confirmation": True}))

    def test_prompt_includes_dont_ask_again_button(self):
        button = PurgeConfirmation.__view_children_items__["confirm_and_disable"]
        self.assertEqual(
            button.__discord_ui_model_kwargs__["label"],
            "Confirm & don't ask again",
        )

    def test_dashboard_control_loads_and_saves_the_setting(self):
        html = Path("apps/Dashboard/static/dashboard.html").read_text(encoding="utf-8")
        javascript = Path("apps/Dashboard/static/dashboard.js").read_text(encoding="utf-8")
        self.assertIn('id="purge-confirmation" type="checkbox"', html)
        self.assertIn(
            "byId('purge-confirmation').checked = settings.general.purge_confirmation === true;",
            javascript,
        )
        self.assertIn(
            "state.settings.general.purge_confirmation = byId('purge-confirmation').checked;",
            javascript,
        )


class PurgeConfirmationButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_dont_ask_again_persists_opt_out_and_confirms(self):
        config = {"purge_confirmation": True}
        settings_cog = SimpleNamespace(
            get_config=MagicMock(return_value=config),
            save_config=AsyncMock(),
        )
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=42),
            guild=SimpleNamespace(id=123),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        view = PurgeConfirmation(ctx, "Confirm?", settings_cog)
        button = next(
            item for item in view.children
            if item.label == "Confirm & don't ask again"
        )

        await button.callback(interaction)

        self.assertIs(config["purge_confirmation"], False)
        settings_cog.save_config.assert_awaited_once_with(123, config)
        self.assertIs(view.value, True)
        interaction.response.send_message.assert_awaited_once_with(
            "Alright, I won't ask again. Run `.purge reset` to bring confirmation back",
            ephemeral=True,
        )

    async def test_purge_reset_reenables_confirmation(self):
        config = {"purge_confirmation": False}
        settings_cog = SimpleNamespace(
            get_config=MagicMock(return_value=config),
            save_config=AsyncMock(),
        )
        moderation = SimpleNamespace(
            bot=SimpleNamespace(get_cog=MagicMock(return_value=settings_cog))
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=123),
            defer=AsyncMock(),
            send=AsyncMock(),
        )

        await Moderation.purge.callback(moderation, ctx, args="reset")

        ctx.defer.assert_awaited_once_with()
        self.assertIs(config["purge_confirmation"], True)
        settings_cog.save_config.assert_awaited_once_with(123, config)
        ctx.send.assert_awaited_once_with("Purge confirmation is back on.")


if __name__ == "__main__":
    unittest.main()
