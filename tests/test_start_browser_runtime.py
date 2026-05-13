from pathlib import Path
import unittest


START_PY = Path(__file__).resolve().parent.parent / "Start.py"
CONFIG_PY = Path(__file__).resolve().parent.parent / "config.py"
GLOBAL_CONFIG_YML = Path(__file__).resolve().parent.parent / "global_config.yml"
XIANYU_AUTO_ASYNC_PY = Path(__file__).resolve().parent.parent / "XianyuAutoAsync.py"
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

    def test_start_runtime_env_cookie_requires_explicit_account_id(self):
        source = START_PY.read_text(encoding="utf-8")

        self.assertIn("env_account_id = str(os.getenv('ACCOUNT_ID') or '').strip()", source)
        self.assertIn("manager.add_cookie(env_account_id, env_cookie)", source)
        self.assertNotIn("manager.add_cookie('default', env_cookie)", source)

    def test_config_runtime_no_longer_backfills_default_account_from_legacy_cookie_value(self):
        source = CONFIG_PY.read_text(encoding="utf-8")

        self.assertNotIn("COOKIES_STR = config.get('COOKIES.value', '')", source)
        self.assertNotIn("COOKIES_LIST = [{'id': 'default', 'value': val}] if val else []", source)

    def test_start_runtime_config_cookie_entries_use_account_id_field(self):
        source = START_PY.read_text(encoding="utf-8")

        self.assertIn("account_id = str(entry.get('account_id') or '').strip()", source)
        self.assertNotIn("account_id = entry.get('id')", source)

    def test_global_config_cookie_template_is_new_account_list_schema(self):
        source = GLOBAL_CONFIG_YML.read_text(encoding="utf-8")

        self.assertIn("COOKIES: []", source)
        self.assertNotIn("COOKIES:\n  last_update_time: ''", source)

    def test_xianyu_live_init_requires_explicit_cookie_input(self):
        source = XIANYU_AUTO_ASYNC_PY.read_text(encoding="utf-8")

        self.assertNotIn("COOKIES_STR,", source)
        self.assertNotIn("cookies_str = COOKIES_STR", source)
        self.assertNotIn("请在global_config.yml中配置COOKIES_STR或过参数传入", source)


if __name__ == "__main__":
    unittest.main()
