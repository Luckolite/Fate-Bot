import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.core.core import Toggles


class PrivacyMenuTests(unittest.TestCase):
    def test_dropdown_constructs_and_toggles_with_current_discord_ui(self):
        async def exercise():
            user = SimpleNamespace(id=1)
            view = Toggles({"xp": False}, user)
            interaction = SimpleNamespace(
                user=user,
                data={"values": ["xp"]},
                response=SimpleNamespace(edit_message=AsyncMock()),
            )

            self.assertIs(view.dropdown.view, view)
            await view.dropdown.callback(interaction)

            self.assertTrue(view.result["xp"])
            interaction.response.edit_message.assert_awaited_once_with(view=view)
            view.stop()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
