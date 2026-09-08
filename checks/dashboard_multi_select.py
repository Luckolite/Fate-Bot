import unittest
from pathlib import Path


class DashboardMultiSelectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.javascript = Path("apps/Dashboard/static/dashboard.js").read_text(
            encoding="utf-8"
        )

    def test_shared_multi_select_options_enable_click_toggling(self):
        self.assertIn(
            "enableClickToggleMultiSelect(select);",
            self.javascript,
        )
        self.assertIn(
            "event.target.selected = !event.target.selected;",
            self.javascript,
        )

    def test_multi_value_module_fields_use_multi_selects(self):
        for control_id in (
            "mod-ignored",
            "asp-trusted-roles",
            "raid-trusted-roles",
        ):
            with self.subTest(control_id=control_id):
                marker = f"selectSetting('{control_id}'"
                matching_lines = [
                    line for line in self.javascript.splitlines() if marker in line
                ]
                self.assertTrue(matching_lines)
                self.assertTrue(any(", true)" in line for line in matching_lines))
        self.assertIn('id="mod-roles" multiple', self.javascript)


if __name__ == "__main__":
    unittest.main()
