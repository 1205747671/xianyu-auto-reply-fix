import unittest
from unittest import IsolatedAsyncioTestCase, mock


class BrowserProviderTest(unittest.TestCase):
    def test_build_download_proxy_env_sets_http_and_https(self):
        from utils.browser_provider import build_download_proxy_env

        env = build_download_proxy_env("http://127.0.0.1:1081", {"PATH": "x"})

        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:1081")
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:1081")
        self.assertEqual(env["PATH"], "x")

    @mock.patch("utils.browser_provider.cloak_launch")
    def test_launch_browser_delegates_to_cloakbrowser_launch(self, mock_launch):
        from utils.browser_provider import launch_browser

        launch_browser(headless=True, args=["--foo"])

        mock_launch.assert_called_once_with(headless=True, args=["--foo"])


class BrowserProviderAsyncTest(IsolatedAsyncioTestCase):
    @mock.patch("utils.browser_provider.cloak_launch_persistent_context_async")
    async def test_launch_browser_persistent_context_async_delegates(self, mock_launch):
        from utils.browser_provider import launch_browser_persistent_context_async

        await launch_browser_persistent_context_async(
            user_data_dir="browser_data/user_1",
            headless=True,
            args=["--bar"],
        )

        mock_launch.assert_awaited_once_with(
            user_data_dir="browser_data/user_1",
            headless=True,
            args=["--bar"],
        )
