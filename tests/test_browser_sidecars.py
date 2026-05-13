import os
import sys
import types
import typing
import unittest
from unittest import mock

_SNAPSHOT_MODULE_NAMES = (
    "loguru",
    "cloakbrowser",
    "qrcode",
    "qrcode.constants",
    "utils.image_utils",
)
_IMPORTED_MODULE_NAMES = (
    "utils.captcha_remote_control",
    "utils.item_search",
    "utils.order_detail_fetcher",
    "utils.qr_login",
)
_MODULE_SNAPSHOT = {
    name: sys.modules.get(name)
    for name in _SNAPSHOT_MODULE_NAMES + _IMPORTED_MODULE_NAMES
}


def _purge_stubbed_module(module_name):
    module = sys.modules.get(module_name)
    if module is None or getattr(module, "__file__", None) is not None:
        return

    sys.modules.pop(module_name, None)

    if "." not in module_name:
        return

    package_name, attr_name = module_name.rsplit(".", 1)
    package = sys.modules.get(package_name)
    if package is not None and hasattr(package, attr_name):
        delattr(package, attr_name)


def _restore_module_snapshot():
    for name in _IMPORTED_MODULE_NAMES + _SNAPSHOT_MODULE_NAMES:
        original = _MODULE_SNAPSHOT.get(name)
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original

if "loguru" not in sys.modules:
    loguru_stub = types.ModuleType("loguru")
    loguru_stub.logger = mock.Mock()
    sys.modules["loguru"] = loguru_stub

if "cloakbrowser" not in sys.modules:
    cloakbrowser_stub = types.ModuleType("cloakbrowser")

    def _not_used(*args, **kwargs):
        raise AssertionError("cloakbrowser stub should be patched in tests")

    cloakbrowser_stub.launch = _not_used
    cloakbrowser_stub.launch_async = _not_used
    cloakbrowser_stub.launch_context = _not_used
    cloakbrowser_stub.launch_context_async = _not_used
    cloakbrowser_stub.launch_persistent_context = _not_used
    cloakbrowser_stub.launch_persistent_context_async = _not_used
    cloakbrowser_stub.ensure_binary = lambda: "C:/cloakbrowser/chrome.exe"
    cloakbrowser_stub.build_args = (
        lambda stealth_args, extra_args, timezone=None, locale=None, headless=True: list(extra_args or [])
    )
    cloakbrowser_stub.maybe_resolve_geoip = (
        lambda geoip, proxy, timezone, locale: (timezone, locale, None)
    )
    sys.modules["cloakbrowser"] = cloakbrowser_stub

if "qrcode" not in sys.modules:
    qrcode_stub = types.ModuleType("qrcode")
    qrcode_stub.QRCode = object
    qrcode_constants_stub = types.ModuleType("qrcode.constants")
    qrcode_constants_stub.ERROR_CORRECT_L = 1
    qrcode_stub.constants = qrcode_constants_stub
    sys.modules["qrcode"] = qrcode_stub
    sys.modules["qrcode.constants"] = qrcode_constants_stub

if "utils.image_utils" not in sys.modules:
    image_utils_stub = types.ModuleType("utils.image_utils")
    image_utils_stub.image_manager = mock.Mock()
    sys.modules["utils.image_utils"] = image_utils_stub

for _module_name in _IMPORTED_MODULE_NAMES:
    _purge_stubbed_module(_module_name)

import utils.captcha_remote_control as captcha_remote_control
import utils.item_search as item_search
import utils.order_detail_fetcher as order_detail_fetcher
import utils.qr_login as qr_login

# Avoid leaking modules imported under test-only stubs into later discovery
# imports. Local references above stay bound for this module's tests.
_restore_module_snapshot()


def tearDownModule():
    _restore_module_snapshot()


class BrowserSidecarsProviderMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_item_search_init_browser_uses_provider_persistent_context_launcher(self):
        fake_page = mock.Mock()
        fake_browser = mock.Mock()
        fake_context = mock.Mock()
        fake_runtime = types.SimpleNamespace(browser=fake_browser)
        fake_lease = types.SimpleNamespace(runtime=fake_runtime)
        profile_dir = os.path.join(os.getcwd(), "browser_data", "user_search_account")

        searcher = item_search.XianyuSearcher(
            account_id="search_account",
            cookie_value="cookie2=unit",
        )

        with mock.patch.object(item_search, "PLAYWRIGHT_AVAILABLE", True), \
             mock.patch.object(
                 item_search.account_browser_runtime_manager,
                 "resolve_profile_dir",
                 return_value=profile_dir,
             ) as resolve_profile_dir_mock, \
             mock.patch.object(
                 item_search.account_browser_runtime_manager,
                 "acquire_runtime",
                 new=mock.AsyncMock(return_value=fake_lease),
             ) as acquire_runtime_mock, \
             mock.patch.object(
                 item_search.account_browser_runtime_manager,
                 "get_fresh_page",
                 new=mock.AsyncMock(return_value=(fake_page, fake_context)),
             ) as get_fresh_page_mock:
            await searcher.init_browser()

        self.assertIs(searcher.context, fake_context)
        self.assertIs(searcher.browser, fake_browser)
        self.assertIs(searcher.page, fake_page)
        resolve_profile_dir_mock.assert_called_once_with("search_account")
        acquire_runtime_mock.assert_awaited_once()
        get_fresh_page_mock.assert_awaited_once_with(fake_lease)
        acquire_args = acquire_runtime_mock.await_args
        self.assertEqual(acquire_args.args[0], "search_account")
        self.assertEqual(acquire_args.args[1], "item_search")
        self.assertFalse(acquire_args.kwargs["exclusive"])
        launch_kwargs = acquire_args.kwargs["runtime_request"]
        self.assertEqual(
            launch_kwargs["profile_dir"],
            profile_dir,
        )
        self.assertTrue(launch_kwargs["use_persistent_context"])
        self.assertTrue(launch_kwargs["headless"])
        launch_options = launch_kwargs["launch_options"]
        self.assertTrue(launch_options["headless"])
        self.assertNotIn("--lang=zh-CN", launch_options["args"])
        self.assertFalse(any(arg.startswith("--accept-lang=") for arg in launch_options["args"]))

    async def test_order_detail_fetcher_init_browser_uses_provider_launcher(self):
        fake_page = mock.Mock()
        fake_browser = mock.Mock()
        fake_context = mock.Mock()
        fake_runtime = types.SimpleNamespace(browser=fake_browser)
        fake_lease = types.SimpleNamespace(runtime=fake_runtime)

        fetcher = order_detail_fetcher.OrderDetailFetcher(
            cookie_string="a=b",
            account_id="order-account-1",
            headless=True,
        )

        with mock.patch.object(
            order_detail_fetcher.account_browser_runtime_manager,
            "acquire_runtime",
            new=mock.AsyncMock(return_value=fake_lease),
        ) as acquire_runtime_mock, \
            mock.patch.object(
                order_detail_fetcher.account_browser_runtime_manager,
                "get_fresh_page",
                new=mock.AsyncMock(return_value=(fake_page, fake_context)),
            ) as get_fresh_page_mock, \
            mock.patch.object(fetcher, "_set_cookies", new=mock.AsyncMock()) as set_cookies_mock:
            result = await fetcher.init_browser()

        self.assertTrue(result)
        self.assertIs(fetcher.browser, fake_browser)
        self.assertIs(fetcher.context, fake_context)
        self.assertIs(fetcher.page, fake_page)
        acquire_runtime_mock.assert_awaited_once()
        acquire_args = acquire_runtime_mock.await_args
        self.assertEqual(acquire_args.args[0], "order-account-1")
        self.assertEqual(acquire_args.args[1], "order_detail_fetch")
        self.assertFalse(acquire_args.kwargs["exclusive"])
        get_fresh_page_mock.assert_awaited_once_with(fake_lease)
        set_cookies_mock.assert_awaited_once()

    async def test_order_detail_fetcher_init_browser_docker_args_skip_enable_automation(self):
        fake_page = mock.Mock()
        fake_browser = mock.Mock()
        fake_context = mock.Mock()
        fake_runtime = types.SimpleNamespace(browser=fake_browser)
        fake_lease = types.SimpleNamespace(runtime=fake_runtime)

        fetcher = order_detail_fetcher.OrderDetailFetcher(
            cookie_string="a=b",
            account_id="order-account-2",
            headless=True,
        )

        with mock.patch.object(
            order_detail_fetcher.account_browser_runtime_manager,
            "acquire_runtime",
            new=mock.AsyncMock(return_value=fake_lease),
        ) as acquire_runtime_mock, \
             mock.patch.object(
                 order_detail_fetcher.account_browser_runtime_manager,
                 "get_fresh_page",
                 new=mock.AsyncMock(return_value=(fake_page, fake_context)),
             ), \
             mock.patch.object(fetcher, "_set_cookies", new=mock.AsyncMock()), \
             mock.patch.object(
                 order_detail_fetcher.os,
                 "getenv",
                 side_effect=lambda key: "1" if key == "DOCKER_ENV" else None,
             ):
            result = await fetcher.init_browser()

        self.assertTrue(result)
        runtime_request = acquire_runtime_mock.await_args.kwargs["runtime_request"]
        self.assertNotIn("args", runtime_request)
        self.assertEqual(runtime_request["account_id"], "order-account-2")
        self.assertTrue(runtime_request["headless"])

    async def test_qr_login_verification_page_uses_account_runtime_manager(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-1", account_id="qr-account-1")
        session.verification_url = "https://passport.goofish.com/iv/test"
        session.status = "verification_required"
        session.cookies = {
            "unb": "unb-value",
            "cookie2": "cookie2-value",
            "foo": "bar",
        }
        session.unb = "unb-value"
        session.proxy_config = {
            "proxy_type": "http",
            "proxy_host": "127.0.0.1",
            "proxy_port": 1081,
            "proxy_user": "",
            "proxy_pass": "",
        }
        manager.sessions[session.session_id] = session

        fake_page = mock.Mock()
        fake_page.goto = mock.AsyncMock()
        fake_page.wait_for_timeout = mock.AsyncMock()
        fake_page.screenshot = mock.AsyncMock(return_value=b"image-bytes")
        fake_page.close = mock.AsyncMock()
        fake_page.url = "https://www.goofish.com/im"

        fake_context = mock.Mock()
        fake_context.add_cookies = mock.AsyncMock()
        fake_context.new_page = mock.AsyncMock(return_value=fake_page)
        fake_context.close = mock.AsyncMock()
        fake_context.browser = object()
        lease = types.SimpleNamespace(runtime=types.SimpleNamespace(context=fake_context, browser=fake_context.browser))
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(os.getcwd(), "browser_data", "user_qr-account-1")
            ),
        )

        async def bind_session_handles(
            current_session,
            page,
            context,
            managed_runtime=None,
            managed_runtime_lease=None,
        ):
            current_session.status = "success"
            current_session.managed_runtime = managed_runtime
            current_session.managed_runtime_lease = managed_runtime_lease
            current_session.managed_context = context
            current_session.managed_page = page
            return True

        with mock.patch.object(
            qr_login,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 manager,
                 "_should_show_verification_browser",
                 return_value=False,
             ), \
             mock.patch.object(
                 manager,
                 "_probe_browser_login_success",
                 new=mock.AsyncMock(side_effect=bind_session_handles),
             ), \
             mock.patch("utils.qr_login.image_manager.save_image", return_value="saved.png"):
            await manager._launch_verification_page(session.session_id)

        runtime_manager.acquire_runtime.assert_awaited_once_with(
            "qr-account-1",
            "qr_login_verification",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_request = runtime_manager.acquire_runtime.await_args.kwargs["runtime_request"]
        self.assertEqual(
            runtime_request["profile_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_qr-account-1"),
        )
        self.assertTrue(runtime_request["use_persistent_context"])
        self.assertTrue(runtime_request["launch_options"]["headless"])
        self.assertEqual(
            runtime_request["launch_options"]["proxy"],
            {"server": "http://127.0.0.1:1081"},
        )
        self.assertEqual(runtime_request["persistent_context_options"]["locale"], "zh-CN")
        self.assertEqual(runtime_request["persistent_context_options"]["timezone"], "Asia/Shanghai")
        self.assertEqual(runtime_request["persistent_context_options"]["color_scheme"], "light")
        self.assertTrue(runtime_request["persistent_context_options"]["accept_downloads"])
        self.assertTrue(runtime_request["persistent_context_options"]["ignore_https_errors"])
        self.assertEqual(
            runtime_request["persistent_context_options"]["viewport"],
            {"width": 1600, "height": 900},
        )
        self.assertIs(session.managed_runtime_lease, lease)
        injected_cookie_keys = {
            (cookie["name"], cookie["domain"]): cookie["value"]
            for cookie in fake_context.add_cookies.await_args.args[0]
        }
        self.assertEqual(injected_cookie_keys[("unb", ".goofish.com")], "unb-value")
        self.assertEqual(injected_cookie_keys[("unb", ".taobao.com")], "unb-value")
        self.assertEqual(injected_cookie_keys[("cookie2", ".goofish.com")], "cookie2-value")
        self.assertEqual(injected_cookie_keys[("cookie2", ".taobao.com")], "cookie2-value")
        self.assertEqual(injected_cookie_keys[("foo", ".goofish.com")], "bar")
        self.assertNotIn(("foo", ".taobao.com"), injected_cookie_keys)
        fake_context.new_page.assert_awaited_once_with()
        fake_page.close.assert_not_awaited()
        fake_context.close.assert_not_awaited()

    async def test_qr_login_verification_page_closes_verification_tab_but_keeps_reused_session_handles(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-1b", account_id="qr-account-1b")
        session.verification_url = "https://passport.goofish.com/iv/test"
        session.status = "verification_required"
        session.cookies = {
            "unb": "unb-value",
            "cookie2": "cookie2-value",
        }
        session.unb = "unb-value"
        manager.sessions[session.session_id] = session

        verification_page = mock.Mock()
        verification_page.goto = mock.AsyncMock()
        verification_page.wait_for_timeout = mock.AsyncMock()
        verification_page.screenshot = mock.AsyncMock(return_value=b"image-bytes")
        verification_page.close = mock.AsyncMock()
        verification_page.url = "https://passport.goofish.com/iv/test"

        existing_page = mock.Mock()
        existing_page.url = "https://www.goofish.com/im"
        existing_page.close = mock.AsyncMock()

        fake_context = mock.Mock()
        fake_context.add_cookies = mock.AsyncMock()
        fake_context.new_page = mock.AsyncMock(return_value=verification_page)
        fake_context.close = mock.AsyncMock()
        fake_context.pages = [verification_page, existing_page]
        fake_context.browser = object()

        lease = types.SimpleNamespace(runtime=types.SimpleNamespace(context=fake_context, browser=fake_context.browser))
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(os.getcwd(), "browser_data", "user_qr-account-1b")
            ),
        )

        async def bind_existing_page(
            current_session,
            page,
            context,
            managed_runtime=None,
            managed_runtime_lease=None,
        ):
            current_session.status = "success"
            current_session.managed_runtime = managed_runtime
            current_session.managed_runtime_lease = managed_runtime_lease
            current_session.managed_context = context
            current_session.managed_page = existing_page
            return True

        with mock.patch.object(
            qr_login,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 manager,
                 "_should_show_verification_browser",
                 return_value=False,
             ), \
             mock.patch.object(
                 manager,
                 "_probe_browser_login_success",
                 new=mock.AsyncMock(side_effect=bind_existing_page),
             ), \
             mock.patch("utils.qr_login.image_manager.save_image", return_value="saved.png"):
            await manager._launch_verification_page(session.session_id)

        self.assertIs(session.managed_runtime_lease, lease)
        verification_page.close.assert_awaited_once_with()
        fake_context.close.assert_not_awaited()
        existing_page.close.assert_not_awaited()

    def test_qr_login_build_cross_domain_browser_cookies_includes_taobao_for_key_tickets(self):
        manager = qr_login.QRLoginManager()

        cookies = manager._build_cross_domain_browser_cookies(
            "https://passport.goofish.com/iv/test",
            {
                "unb": "unb-value",
                "cookie2": "cookie2-value",
                "foo": "bar",
            },
        )

        cookie_keys = {
            (cookie["name"], cookie["domain"]): cookie["value"]
            for cookie in cookies
        }

        self.assertEqual(cookie_keys[("unb", ".goofish.com")], "unb-value")
        self.assertEqual(cookie_keys[("unb", ".taobao.com")], "unb-value")
        self.assertEqual(cookie_keys[("cookie2", ".goofish.com")], "cookie2-value")
        self.assertEqual(cookie_keys[("cookie2", ".taobao.com")], "cookie2-value")
        self.assertEqual(cookie_keys[("foo", ".goofish.com")], "bar")
        self.assertNotIn(("foo", ".taobao.com"), cookie_keys)

    def test_qr_login_verification_profile_dir_uses_account_id_only(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-existing", account_id="qr-account-existing")
        session.unb = "unb-value"
        session.proxy_account_id = "2095002164"

        profile_dir = manager._resolve_verification_profile_dir(session)

        self.assertEqual(
            profile_dir,
            os.path.join(os.getcwd(), "browser_data", "user_qr-account-existing"),
        )

    def test_qr_login_build_browser_cookies_delegates_to_cross_domain_payload(self):
        manager = qr_login.QRLoginManager()

        cookies = manager._build_browser_cookies(
            "https://passport.goofish.com/iv/test",
            {
                "unb": "unb-value",
                "foo": "bar",
            },
        )

        cookie_keys = {
            (cookie["name"], cookie["domain"]): cookie["value"]
            for cookie in cookies
        }

        self.assertEqual(cookie_keys[("unb", ".goofish.com")], "unb-value")
        self.assertEqual(cookie_keys[("unb", ".taobao.com")], "unb-value")
        self.assertEqual(cookie_keys[("foo", ".goofish.com")], "bar")
        self.assertNotIn(("foo", ".taobao.com"), cookie_keys)

    async def test_qr_login_probe_browser_login_success_binds_managed_handles(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-2")

        fake_page = mock.Mock()
        fake_page.url = "https://www.goofish.com/im"

        fake_context = mock.Mock()
        fake_context.cookies = mock.AsyncMock(return_value=[
            {"name": "unb", "value": "unb-value"},
            {"name": "cookie2", "value": "cookie2-value"},
        ])

        success = await manager._probe_browser_login_success(session, fake_page, fake_context)

        self.assertTrue(success)
        self.assertEqual(session.status, "success")
        self.assertEqual(session.success_source, "browser")
        self.assertIs(session.managed_context, fake_context)
        self.assertIs(session.managed_page, fake_page)

    async def test_qr_login_probe_browser_login_success_reuses_existing_logged_in_page(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-2b")

        fake_page = mock.Mock()
        fake_page.url = "https://passport.goofish.com/iv/test"

        existing_page = mock.Mock()
        existing_page.url = "https://www.goofish.com/im"

        fake_context = mock.Mock()
        fake_context.pages = [fake_page, existing_page]
        fake_context.cookies = mock.AsyncMock(return_value=[
            {"name": "unb", "value": "unb-value"},
            {"name": "cookie2", "value": "cookie2-value"},
        ])
        fake_context.new_page = mock.AsyncMock()

        success = await manager._probe_browser_login_success(session, fake_page, fake_context)

        self.assertTrue(success)
        self.assertEqual(session.status, "success")
        self.assertIs(session.managed_context, fake_context)
        self.assertIs(session.managed_page, existing_page)
        fake_context.new_page.assert_not_awaited()

    async def test_qr_login_probe_browser_login_success_throttles_active_probe_tabs(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-2c")
        session.last_active_probe_time = 100.0

        fake_page = mock.Mock()
        fake_page.url = "https://passport.goofish.com/iv/test"

        fake_context = mock.Mock()
        fake_context.pages = [fake_page]
        fake_context.cookies = mock.AsyncMock(return_value=[
            {"name": "unb", "value": "unb-value"},
            {"name": "cookie2", "value": "cookie2-value"},
        ])
        fake_context.new_page = mock.AsyncMock()

        with mock.patch.object(qr_login.time, "time", return_value=105.0):
            success = await manager._probe_browser_login_success(session, fake_page, fake_context)

        self.assertFalse(success)
        self.assertEqual(session.status, "waiting")
        self.assertEqual(session.last_active_probe_time, 100.0)
        fake_context.new_page.assert_not_awaited()

    async def test_qr_login_probe_browser_login_success_uses_runtime_lease_fresh_page_for_active_probe(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-2d")
        lease = object()
        managed_runtime = object()

        fake_page = mock.Mock()
        fake_page.url = "https://passport.goofish.com/iv/test"

        probe_page = mock.Mock()
        probe_page.url = "https://www.goofish.com/im"
        probe_page.goto = mock.AsyncMock()
        probe_page.wait_for_timeout = mock.AsyncMock()
        probe_page.query_selector = mock.AsyncMock(return_value=object())
        probe_page.close = mock.AsyncMock()

        fake_context = mock.Mock()
        fake_context.pages = [fake_page]
        fake_context.cookies = mock.AsyncMock(return_value=[
            {"name": "unb", "value": "unb-value"},
            {"name": "cookie2", "value": "cookie2-value"},
        ])
        fake_context.new_page = mock.AsyncMock(side_effect=AssertionError("lease path must use runtime manager fresh page"))

        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(return_value=(probe_page, fake_context)),
        )

        with mock.patch.object(qr_login, "account_browser_runtime_manager", new=runtime_manager):
            success = await manager._probe_browser_login_success(
                session,
                fake_page,
                fake_context,
                managed_runtime=managed_runtime,
                managed_runtime_lease=lease,
            )

        self.assertTrue(success)
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        fake_context.new_page.assert_not_awaited()
        self.assertIs(session.managed_context, fake_context)
        self.assertIs(session.managed_page, probe_page)
        probe_page.close.assert_not_awaited()

    async def test_qr_login_probe_browser_login_success_removes_failed_lease_probe_page_from_tracking(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-2e")
        lease = types.SimpleNamespace(pages=[])
        managed_runtime = object()

        fake_page = mock.Mock()
        fake_page.url = "https://passport.goofish.com/iv/test"

        probe_page = mock.Mock()
        probe_page.url = "https://passport.goofish.com/mini_login"
        probe_page.goto = mock.AsyncMock()
        probe_page.wait_for_timeout = mock.AsyncMock()
        probe_page.query_selector = mock.AsyncMock(return_value=None)
        probe_page.close = mock.AsyncMock()

        fake_context = mock.Mock()
        fake_context.pages = [fake_page]
        fake_context.cookies = mock.AsyncMock(return_value=[
            {"name": "unb", "value": "unb-value"},
            {"name": "cookie2", "value": "cookie2-value"},
        ])
        fake_context.new_page = mock.AsyncMock(side_effect=AssertionError("lease path must use runtime manager fresh page"))

        async def _get_fresh_page(current_lease):
            current_lease.pages.append(probe_page)
            return probe_page, fake_context

        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(side_effect=_get_fresh_page),
        )

        with mock.patch.object(qr_login, "account_browser_runtime_manager", new=runtime_manager):
            success = await manager._probe_browser_login_success(
                session,
                fake_page,
                fake_context,
                managed_runtime=managed_runtime,
                managed_runtime_lease=lease,
            )

        self.assertFalse(success)
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        self.assertNotIn(probe_page, lease.pages)
        probe_page.close.assert_awaited_once_with()

    def test_qr_login_get_session_cookies_returns_managed_handles_and_runtime_lease(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-3")
        session.status = "success"
        session.cookies = {"unb": "unb-value", "cookie2": "cookie2-value"}
        session.unb = "unb-value"
        session.managed_runtime_lease = object()
        session.managed_runtime = object()
        session.managed_context = object()
        session.managed_page = object()
        manager.sessions[session.session_id] = session

        cookies_info = manager.get_session_cookies(session.session_id)

        self.assertEqual(cookies_info["cookies"], "unb=unb-value; cookie2=cookie2-value")
        self.assertEqual(cookies_info["unb"], "unb-value")
        self.assertIs(cookies_info["managed_runtime_lease"], session.managed_runtime_lease)
        self.assertIs(cookies_info["managed_runtime"], session.managed_runtime)
        self.assertIs(cookies_info["managed_context"], session.managed_context)
        self.assertIs(cookies_info["managed_page"], session.managed_page)

    def test_qr_login_cleanup_session_assets_releases_runtime_lease(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-4")
        lease = object()
        runtime_manager = types.SimpleNamespace(
            release_runtime=mock.AsyncMock(return_value=None),
        )
        session.managed_runtime_lease = lease
        session.managed_runtime = mock.Mock(close=mock.AsyncMock())
        session.managed_context = mock.Mock(close=mock.AsyncMock())
        session.managed_page = mock.Mock(close=mock.AsyncMock())
        managed_runtime = session.managed_runtime
        managed_context = session.managed_context
        managed_page = session.managed_page

        with mock.patch.object(qr_login, "account_browser_runtime_manager", new=runtime_manager), \
             mock.patch.object(qr_login.asyncio, "get_running_loop", side_effect=RuntimeError):
            manager._cleanup_session_assets(session)

        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="qr_login_session_cleanup",
        )
        managed_page.close.assert_not_awaited()
        managed_context.close.assert_not_awaited()
        managed_runtime.close.assert_not_awaited()
        self.assertIsNone(session.managed_runtime_lease)
        self.assertIsNone(session.managed_runtime)
        self.assertIsNone(session.managed_context)
        self.assertIsNone(session.managed_page)


class CaptchaRemoteControlTypingGuardTest(unittest.TestCase):
    def test_captcha_remote_control_uses_provider_neutral_page_annotations(self):
        controller_type_hints = typing.get_type_hints(
            captcha_remote_control.CaptchaRemoteController.create_session
        )
        self.assertIs(controller_type_hints["page"], typing.Any)


if __name__ == "__main__":
    unittest.main()
