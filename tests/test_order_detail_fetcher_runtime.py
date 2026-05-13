import importlib
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock

_STUBBED_MODULE_NAMES = (
    "loguru",
    "utils.browser_provider",
)
_IMPORTED_MODULE_NAMES = (
    "utils.order_detail_fetcher",
)
_MODULE_SNAPSHOT = {
    name: sys.modules.get(name)
    for name in _STUBBED_MODULE_NAMES + _IMPORTED_MODULE_NAMES
}


def _restore_module_snapshot():
    for name in _IMPORTED_MODULE_NAMES + _STUBBED_MODULE_NAMES:
        original = _MODULE_SNAPSHOT.get(name)
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


if "loguru" not in sys.modules:
    loguru_module = ModuleType("loguru")
    loguru_module.logger = SimpleNamespace(
        add=lambda *args, **kwargs: 1,
        remove=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
    )
    sys.modules["loguru"] = loguru_module

if "utils.browser_provider" not in sys.modules:
    browser_provider_module = ModuleType("utils.browser_provider")
    browser_provider_module.BrowserContextLike = object
    browser_provider_module.BrowserLike = object
    browser_provider_module.PageLike = object

    async def _unexpected_launch_browser_async(**kwargs):
        raise AssertionError(f"unexpected launch_browser_async call: {kwargs}")

    async def _unexpected_launch_browser_persistent_context_async(**kwargs):
        raise AssertionError(
            f"unexpected launch_browser_persistent_context_async call: {kwargs}"
        )

    def _unexpected_launch_browser(**kwargs):
        raise AssertionError(f"unexpected launch_browser call: {kwargs}")

    def _unexpected_launch_browser_persistent_context(**kwargs):
        raise AssertionError(
            f"unexpected launch_browser_persistent_context call: {kwargs}"
        )

    browser_provider_module.build_download_proxy_env = (
        lambda proxy_url, base_env=None: dict(base_env or {})
    )
    browser_provider_module.launch_browser = _unexpected_launch_browser
    browser_provider_module.launch_browser_async = _unexpected_launch_browser_async
    browser_provider_module.launch_browser_persistent_context = (
        _unexpected_launch_browser_persistent_context
    )
    browser_provider_module.launch_browser_persistent_context_async = (
        _unexpected_launch_browser_persistent_context_async
    )
    sys.modules["utils.browser_provider"] = browser_provider_module

sys.modules.pop("utils.order_detail_fetcher", None)
order_detail_fetcher = importlib.import_module("utils.order_detail_fetcher")

# unittest discover 会先导入所有测试模块，立刻恢复 import-time stub，
# 避免后续模块看到半残的 loguru / browser_provider。
_restore_module_snapshot()


def tearDownModule():
    _restore_module_snapshot()


class _FakeResponse:
    status = 200


class _FakePage:
    def __init__(self):
        self.goto_calls = []
        self.wait_for_load_state_calls = []

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(
            {
                "url": url,
                "wait_until": wait_until,
                "timeout": timeout,
            }
        )
        return _FakeResponse()

    async def wait_for_load_state(self, state):
        self.wait_for_load_state_calls.append(state)

    async def title(self):
        return "order detail title"


def _build_db_manager_module(existing_order):
    db_module = ModuleType("db_manager")
    db_manager = mock.Mock()
    db_manager.get_order_by_id.return_value = existing_order
    db_manager.get_item_info.return_value = None
    db_module.db_manager = db_manager
    return db_module, db_manager


class OrderDetailFetcherRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_order_detail_simple_requires_account_id(self):
        db_module, _ = _build_db_manager_module(None)
        runtime_manager = mock.Mock()
        runtime_manager.acquire_runtime = mock.AsyncMock(
            side_effect=AssertionError("missing account_id should not acquire runtime")
        )
        runtime_manager.get_fresh_page = mock.AsyncMock()
        runtime_manager.release_runtime = mock.AsyncMock()

        with mock.patch.dict(sys.modules, {"db_manager": db_module}, clear=False), \
             mock.patch.object(order_detail_fetcher, "account_browser_runtime_manager", runtime_manager):
            result = await order_detail_fetcher.fetch_order_detail_simple(
                "order-missing-account",
                cookie_string="cookie2=current-token",
                force_refresh=True,
            )

        self.assertIsNone(result)
        runtime_manager.acquire_runtime.assert_not_awaited()

    async def test_fetch_order_detail_returns_cache_without_touching_runtime(self):
        fetcher = order_detail_fetcher.OrderDetailFetcher(
            cookie_string="cookie2=current-token",
            account_id="account_123",
        )
        db_module, db_manager = _build_db_manager_module(
            {
                "order_id": "order-1",
                "amount": "99.00",
                "order_status": "completed",
                "spec_name": "颜色",
                "spec_value": "黑色",
                "quantity": "1",
            }
        )
        runtime_manager = mock.Mock()
        runtime_manager.acquire_runtime = mock.AsyncMock(
            side_effect=AssertionError("cache hit should not acquire runtime")
        )
        runtime_manager.get_fresh_page = mock.AsyncMock(
            side_effect=AssertionError("cache hit should not request fresh page")
        )
        runtime_manager.release_runtime = mock.AsyncMock()

        with mock.patch.dict(sys.modules, {"db_manager": db_module}, clear=False), \
             mock.patch.object(order_detail_fetcher, "account_browser_runtime_manager", runtime_manager), \
             mock.patch.object(fetcher, "_ensure_browser_ready", mock.AsyncMock(side_effect=AssertionError("cache hit should not ensure browser"))):
            result = await fetcher.fetch_order_detail("order-1")

        self.assertIsNotNone(result)
        self.assertTrue(result["from_cache"])
        self.assertEqual(result["amount_source"], "cache")
        db_manager.get_order_by_id.assert_called_once_with("order-1", account_id="account_123")
        runtime_manager.acquire_runtime.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_not_awaited()

    async def test_fetch_order_detail_simple_scopes_cache_lookup_by_account_id(self):
        db_module, db_manager = _build_db_manager_module(
            {
                "order_id": "order-simple-cache-1",
                "amount": "88.00",
                "order_status": "completed",
                "spec_name": "颜色",
                "spec_value": "黑色",
                "quantity": "1",
                "account_id": "account_789",
            }
        )
        runtime_manager = mock.Mock()
        runtime_manager.acquire_runtime = mock.AsyncMock(
            side_effect=AssertionError("cache hit should not acquire runtime")
        )
        runtime_manager.get_fresh_page = mock.AsyncMock(
            side_effect=AssertionError("cache hit should not request fresh page")
        )
        runtime_manager.release_runtime = mock.AsyncMock()

        with mock.patch.dict(sys.modules, {"db_manager": db_module}, clear=False), \
             mock.patch.object(order_detail_fetcher, "account_browser_runtime_manager", runtime_manager):
            result = await order_detail_fetcher.fetch_order_detail_simple(
                "order-simple-cache-1",
                cookie_string="cookie2=current-token",
                account_id="account_789",
            )

        self.assertIsNotNone(result)
        self.assertTrue(result["from_cache"])
        db_manager.get_order_by_id.assert_called_once_with(
            "order-simple-cache-1",
            account_id="account_789",
        )
        runtime_manager.acquire_runtime.assert_not_awaited()

    async def test_fetch_order_detail_cache_miss_uses_runtime_manager_with_account_id(self):
        fetcher = order_detail_fetcher.OrderDetailFetcher(
            cookie_string="cookie2=current-token",
            account_id="account_123",
        )
        db_module, db_manager = _build_db_manager_module(None)
        runtime_lease = SimpleNamespace(account_id="account_123", released=False, pages=[])
        fake_page = _FakePage()
        fake_context = object()
        runtime_manager = mock.Mock()
        runtime_manager.acquire_runtime = mock.AsyncMock(return_value=runtime_lease)
        runtime_manager.get_fresh_page = mock.AsyncMock(return_value=(fake_page, fake_context))
        runtime_manager.release_runtime = mock.AsyncMock()

        with mock.patch.dict(sys.modules, {"db_manager": db_module}, clear=False), \
             mock.patch.object(order_detail_fetcher, "account_browser_runtime_manager", runtime_manager), \
             mock.patch.object(order_detail_fetcher.asyncio, "sleep", mock.AsyncMock(return_value=None)), \
             mock.patch.object(fetcher, "_register_response_capture_handler"), \
             mock.patch.object(fetcher, "_wait_for_response_capture_tasks", mock.AsyncMock(return_value=None)), \
             mock.patch.object(fetcher, "_clear_response_capture_handler"), \
             mock.patch.object(
                 fetcher,
                 "_get_sku_content",
                 mock.AsyncMock(
                     return_value={
                         "spec_name": "颜色",
                         "spec_value": "黑色",
                         "quantity": "1",
                         "amount": "99.00",
                         "amount_source": "page",
                     }
                 ),
             ), \
             mock.patch.object(fetcher, "_get_order_status", mock.AsyncMock(return_value="completed")), \
             mock.patch.object(
                 fetcher,
                 "_get_order_time_fields",
                 mock.AsyncMock(
                     return_value={
                         "platform_created_at": "2026-05-09 10:00:00",
                         "platform_paid_at": "2026-05-09 10:01:00",
                         "platform_completed_at": "2026-05-09 10:02:00",
                     }
                 ),
             ), \
             mock.patch.object(fetcher, "_is_order_detail_parse_success", return_value=True):
            result = await fetcher.fetch_order_detail("order-2")

        self.assertIsNotNone(result)
        self.assertFalse(result["from_cache"])
        self.assertEqual(fetcher.page, fake_page)
        self.assertIs(fetcher.context, fake_context)
        runtime_manager.acquire_runtime.assert_awaited_once()
        acquire_args, acquire_kwargs = runtime_manager.acquire_runtime.await_args
        self.assertEqual(acquire_args[0], "account_123")
        self.assertEqual(acquire_args[1], "order_detail_fetch")
        self.assertFalse(acquire_kwargs["exclusive"])
        db_manager.get_order_by_id.assert_called_once_with("order-2", account_id="account_123")
        runtime_manager.get_fresh_page.assert_awaited_once_with(runtime_lease)
        self.assertEqual(fake_page.goto_calls[0]["url"], "https://www.goofish.com/order-detail?orderId=order-2&role=seller")


if __name__ == "__main__":
    unittest.main()
