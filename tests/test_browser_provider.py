import importlib
import importlib.util
import sys
import types
import unittest
from unittest import IsolatedAsyncioTestCase, mock


WRAPPER_EXPORT_NAMES = [
    "launch_browser",
    "launch_browser_async",
    "launch_browser_context",
    "launch_browser_context_async",
    "launch_browser_persistent_context",
    "launch_browser_persistent_context_async",
    "ManagedBrowserRuntime",
    "AsyncManagedBrowserRuntime",
    "launch_managed_browser_runtime",
    "launch_managed_browser_runtime_async",
    "close_managed_browser_runtime",
    "close_managed_browser_runtime_async",
]


class BrowserProviderTestMixin:
    def tearDown(self):
        sys.modules.pop("cloakbrowser", None)
        sys.modules.pop("utils.browser_provider", None)

    def _create_fake_cloakbrowser_module(self):
        fake_module = types.ModuleType("cloakbrowser")
        fake_module.launch = mock.Mock(return_value="browser")
        fake_module.launch_async = mock.AsyncMock(return_value="browser-async")
        fake_module.launch_context = mock.Mock(return_value="context")
        fake_module.launch_context_async = mock.AsyncMock(return_value="context-async")
        fake_module.launch_persistent_context = mock.Mock(return_value="persistent-context")
        fake_module.launch_persistent_context_async = mock.AsyncMock(
            return_value="persistent-context-async"
        )
        fake_module.ensure_binary = mock.Mock(return_value="C:/cloakbrowser/chrome.exe")
        fake_module.build_args = mock.Mock(
            side_effect=lambda stealth_args, extra_args, timezone=None, locale=None, headless=True: list(extra_args or [])
        )
        fake_module.maybe_resolve_geoip = mock.Mock(
            side_effect=lambda geoip, proxy, timezone, locale: (timezone, locale, None)
        )
        return fake_module

    def _load_provider_with_fake_cloakbrowser(self):
        fake_module = self._create_fake_cloakbrowser_module()
        sys.modules["cloakbrowser"] = fake_module

        provider = importlib.import_module("utils.browser_provider")
        provider = importlib.reload(provider)
        return provider, fake_module


class BrowserProviderImportTest(BrowserProviderTestMixin, unittest.TestCase):
    def test_import_requires_real_cloakbrowser_module(self):
        sys.modules["cloakbrowser"] = None

        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("utils.browser_provider")

    def test_import_uses_installed_cloakbrowser_when_available(self):
        if importlib.util.find_spec("cloakbrowser") is None:
            self.skipTest("cloakbrowser is not installed in current environment")

        provider = importlib.import_module("utils.browser_provider")

        for name in WRAPPER_EXPORT_NAMES:
            self.assertTrue(hasattr(provider, name), msg=name)

    def test_build_download_proxy_env_sets_http_and_https(self):
        provider, _ = self._load_provider_with_fake_cloakbrowser()

        env = provider.build_download_proxy_env("http://127.0.0.1:1081", {"PATH": "x"})

        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:1081")
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:1081")
        self.assertEqual(env["PATH"], "x")

    def test_launch_browser_delegates_to_cloakbrowser_launch(self):
        provider, fake_module = self._load_provider_with_fake_cloakbrowser()

        result = provider.launch_browser(headless=True, args=["--foo"])

        fake_module.launch.assert_called_once_with(headless=True, args=["--foo"])
        self.assertEqual(result, "browser")

    def test_launch_browser_context_delegates_to_cloakbrowser_launch_context(self):
        provider, fake_module = self._load_provider_with_fake_cloakbrowser()

        result = provider.launch_browser_context(locale="zh-CN")

        fake_module.launch_context.assert_called_once_with(locale="zh-CN")
        self.assertEqual(result, "context")

    def test_launch_browser_persistent_context_delegates(self):
        provider, fake_module = self._load_provider_with_fake_cloakbrowser()

        result = provider.launch_browser_persistent_context(
            user_data_dir="browser_data/user_1",
            headless=False,
        )

        fake_module.launch_persistent_context.assert_called_once_with(
            user_data_dir="browser_data/user_1",
            headless=False,
        )
        self.assertEqual(result, "persistent-context")


class BrowserProviderAsyncTest(BrowserProviderTestMixin, IsolatedAsyncioTestCase):
    async def test_launch_browser_async_delegates_to_cloakbrowser_launch_async(self):
        provider, fake_module = self._load_provider_with_fake_cloakbrowser()

        result = await provider.launch_browser_async(headless=True)

        fake_module.launch_async.assert_awaited_once_with(headless=True)
        self.assertEqual(result, "browser-async")

    async def test_launch_browser_context_async_delegates(self):
        provider, fake_module = self._load_provider_with_fake_cloakbrowser()

        result = await provider.launch_browser_context_async(locale="zh-CN")

        fake_module.launch_context_async.assert_awaited_once_with(locale="zh-CN")
        self.assertEqual(result, "context-async")

    async def test_launch_browser_persistent_context_async_delegates(self):
        provider, fake_module = self._load_provider_with_fake_cloakbrowser()

        result = await provider.launch_browser_persistent_context_async(
            user_data_dir="browser_data/user_1",
            headless=True,
            args=["--bar"],
        )

        fake_module.launch_persistent_context_async.assert_awaited_once_with(
            user_data_dir="browser_data/user_1",
            headless=True,
            args=["--bar"],
        )
        self.assertEqual(result, "persistent-context-async")
