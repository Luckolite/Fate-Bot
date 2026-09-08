import unittest
from unittest.mock import AsyncMock, patch

from cogs.core.menus import Menus


class HelpMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_restructure_omits_empty_categories_and_subcategories(self):
        menu_cog = Menus.__new__(Menus)
        command = object()
        menu_cog.construct = AsyncMock(
            side_effect=lambda cogs: [command] if cogs == "LoadedCog" else []
        )

        help_structure = {
            "Core": "LoadedCog",
            "Utility": {
                "Loaded": "LoadedCog",
                "Empty": "MissingCog",
            },
            "Misc": ["GlobalChat", "NSFW", "Reddit"],
        }
        with patch("cogs.core.menus.structure", help_structure):
            await menu_cog.restructure()

        self.assertEqual(menu_cog.structure["Core"], [command])
        self.assertEqual(menu_cog.structure["Utility"], {"Loaded": [command]})
        self.assertNotIn("Misc", menu_cog.structure)


if __name__ == "__main__":
    unittest.main()
