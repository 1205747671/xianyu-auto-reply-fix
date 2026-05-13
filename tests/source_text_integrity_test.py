from pathlib import Path
import subprocess
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
ORDER_STATUS_HANDLER_TEST_SOURCE = "tests/test_order_status_handler_account_id.py"
EXPECTED_STATS_ERROR = "console.error('加载系统统计失败:', error);"
BROKEN_STATS_ERROR = "console.error('" + ("?" * 8) + ":', error);"


def _gbk_mojibake(text: str, replace_invalid: bool = False) -> str:
    errors = "replace" if replace_invalid else "strict"
    return text.encode("utf-8").decode("gbk", errors=errors).replace("\ufffd", "?")


BROKEN_STATS_ERROR_MOJIBAKE = "console.error('鍔犺浇绯荤粺缁熻澶辫触:', error);"
ORDER_STATUS_HANDLER_SUCCESS_MOJIBAKE = _gbk_mojibake("交易成功")
SOURCE_PATTERNS = [
    "static/index.html",
    "static/login.html",
    "static/register.html",
    "static/js/app.js",
    "static/userscripts/goofish-dark-mode.user.js",
    "reply_server.py",
    "README.md",
    ORDER_STATUS_HANDLER_TEST_SOURCE,
]


class SourceTextIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        tracked = subprocess.check_output(
            ["git", "ls-files", "--", *SOURCE_PATTERNS],
            cwd=REPO_ROOT,
            text=True,
            encoding="utf-8",
        ).splitlines()
        cls.tracked_sources = {
            relative_path: (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            for relative_path in tracked
        }

    def test_system_stats_error_message_is_restored(self):
        self.assertIn(EXPECTED_STATS_ERROR, self.app_js)
        self.assertIn("加载系统统计失败", self.app_js)
        self.assertNotIn(BROKEN_STATS_ERROR, self.app_js)
        self.assertNotIn(BROKEN_STATS_ERROR_MOJIBAKE, self.app_js)

    def test_targeted_sources_do_not_contain_known_broken_stats_error(self):
        for relative_path, source in self.tracked_sources.items():
            with self.subTest(path=relative_path):
                self.assertNotIn(BROKEN_STATS_ERROR, source)
                self.assertNotIn(BROKEN_STATS_ERROR_MOJIBAKE, source)

    def test_order_status_handler_account_scope_test_uses_clean_success_message(self):
        source = self.tracked_sources[ORDER_STATUS_HANDLER_TEST_SOURCE]

        self.assertIn("交易成功", source)
        self.assertNotIn(ORDER_STATUS_HANDLER_SUCCESS_MOJIBAKE, source)


if __name__ == "__main__":
    unittest.main()
