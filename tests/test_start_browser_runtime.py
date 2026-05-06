from pathlib import Path
import unittest


START_PY = Path(__file__).resolve().parent.parent / "Start.py"
ITEM_SEARCH_PY = Path(__file__).resolve().parent.parent / "utils" / "item_search.py"
SLIDER_PY = Path(__file__).resolve().parent.parent / "utils" / "xianyu_slider_stealth.py"


class StartBrowserRuntimeTest(unittest.TestCase):
    def test_start_runtime_no_longer_prompts_playwright_install_chromium(self):
        source = START_PY.read_text(encoding="utf-8")

        self.assertNotIn("playwright install chromium", source)

    def test_start_runtime_self_check_uses_cloakbrowser_install_hint(self):
        source = START_PY.read_text(encoding="utf-8")

        self.assertIn("python -m cloakbrowser install", source)

    def test_start_runtime_check_is_named_for_cloakbrowser(self):
        source = START_PY.read_text(encoding="utf-8")

        self.assertIn("_check_cloakbrowser_runtime", source)
        self.assertNotIn("_check_and_install_playwright", source)

    def test_item_search_runtime_hint_uses_cloakbrowser_install(self):
        source = ITEM_SEARCH_PY.read_text(encoding="utf-8")

        self.assertIn("python -m cloakbrowser install", source)
        self.assertNotIn("playwright install chromium", source)

    def test_slider_runtime_bootstrap_uses_cloakbrowser_install(self):
        source = SLIDER_PY.read_text(encoding="utf-8")

        self.assertIn("python -m cloakbrowser install", source)
        self.assertNotIn("-m playwright install", source)


if __name__ == "__main__":
    unittest.main()
