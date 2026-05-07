import os
import sys
import types
import typing
import unittest
from unittest import mock

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

import utils.captcha_remote_control as captcha_remote_control
import utils.item_search as item_search
import utils.order_detail_fetcher as order_detail_fetcher
import utils.qr_login as qr_login


class BrowserSidecarsProviderMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_item_search_init_browser_uses_provider_persistent_context_launcher(self):
        fake_page = mock.Mock()
        fake_context = mock.Mock()
        fake_context.browser = object()
        fake_context.new_page = mock.AsyncMock(return_value=fake_page)

        searcher = item_search.XianyuSearcher()

        with mock.patch.object(item_search, "PLAYWRIGHT_AVAILABLE", True), \
             mock.patch.object(
                 item_search,
                 "launch_browser_persistent_context_async",
                 new=mock.AsyncMock(return_value=fake_context),
                 create=True,
             ) as launch_browser_persistent_context_async:
            await searcher.init_browser()

        self.assertIs(searcher.context, fake_context)
        self.assertIs(searcher.browser, fake_context.browser)
        self.assertIs(searcher.page, fake_page)
        launch_browser_persistent_context_async.assert_awaited_once()
        launch_kwargs = launch_browser_persistent_context_async.await_args.kwargs
        self.assertNotIn("user_agent", launch_kwargs)
        self.assertNotIn("viewport", launch_kwargs)
        self.assertNotIn("locale", launch_kwargs)
        self.assertNotIn("--lang=zh-CN", launch_kwargs["args"])
        self.assertFalse(any(arg.startswith("--accept-lang=") for arg in launch_kwargs["args"]))

    async def test_order_detail_fetcher_init_browser_uses_provider_launcher(self):
        fake_page = mock.Mock()
        fake_browser = mock.Mock()
        fake_browser.new_context = mock.AsyncMock()
        fake_context = mock.Mock()
        fake_context.add_cookies = mock.AsyncMock()
        fake_context.set_extra_http_headers = mock.AsyncMock()
        fake_context.new_page = mock.AsyncMock(return_value=fake_page)
        fake_browser.new_context.return_value = fake_context

        fetcher = order_detail_fetcher.OrderDetailFetcher(cookie_string="a=b", headless=True)

        with mock.patch.object(
            order_detail_fetcher,
            "launch_browser_async",
            new=mock.AsyncMock(return_value=fake_browser),
            create=True,
        ) as launch_browser_async:
            result = await fetcher.init_browser()

        self.assertTrue(result)
        self.assertIs(fetcher.browser, fake_browser)
        self.assertIs(fetcher.context, fake_context)
        self.assertIs(fetcher.page, fake_page)
        launch_browser_async.assert_awaited_once()
        fake_browser.new_context.assert_awaited_once_with()
        fake_context.set_extra_http_headers.assert_not_awaited()

    async def test_order_detail_fetcher_init_browser_docker_args_skip_enable_automation(self):
        fake_page = mock.Mock()
        fake_browser = mock.Mock()
        fake_browser.new_context = mock.AsyncMock()
        fake_context = mock.Mock()
        fake_context.add_cookies = mock.AsyncMock()
        fake_context.set_extra_http_headers = mock.AsyncMock()
        fake_context.new_page = mock.AsyncMock(return_value=fake_page)
        fake_browser.new_context.return_value = fake_context

        fetcher = order_detail_fetcher.OrderDetailFetcher(cookie_string="a=b", headless=True)

        with mock.patch.object(
            order_detail_fetcher,
            "launch_browser_async",
            new=mock.AsyncMock(return_value=fake_browser),
            create=True,
        ) as launch_browser_async, \
             mock.patch.object(
                 order_detail_fetcher.os,
                 "getenv",
                 side_effect=lambda key: "1" if key == "DOCKER_ENV" else None,
             ):
            result = await fetcher.init_browser()

        self.assertTrue(result)
        launch_args = launch_browser_async.await_args.kwargs["args"]
        self.assertNotIn("--enable-automation", launch_args)

    async def test_qr_login_verification_page_uses_provider_launcher(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-1")
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

        async def bind_session_handles(current_session, page, context, managed_runtime=None):
            current_session.status = "success"
            current_session.managed_runtime = managed_runtime
            current_session.managed_context = context
            current_session.managed_page = page
            return True

        with mock.patch.object(
            qr_login,
            "launch_browser_persistent_context_async",
            new=mock.AsyncMock(return_value=fake_context),
            create=True,
        ) as launch_browser_persistent_context_async, \
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

        launch_browser_persistent_context_async.assert_awaited_once()
        launch_kwargs = launch_browser_persistent_context_async.await_args.kwargs
        self.assertEqual(
            launch_kwargs["user_data_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_unb-value"),
        )
        self.assertTrue(launch_kwargs["headless"])
        self.assertEqual(
            launch_kwargs["proxy"],
            {"server": "http://127.0.0.1:1081"},
        )
        self.assertEqual(launch_kwargs["locale"], "zh-CN")
        self.assertEqual(launch_kwargs["timezone"], "Asia/Shanghai")
        self.assertEqual(launch_kwargs["color_scheme"], "light")
        self.assertTrue(launch_kwargs["accept_downloads"])
        self.assertTrue(launch_kwargs["ignore_https_errors"])
        self.assertEqual(launch_kwargs["viewport"], {"width": 1600, "height": 900})
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
        session = qr_login.QRLoginSession("session-1b")
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

        async def bind_existing_page(current_session, page, context, managed_runtime=None):
            current_session.status = "success"
            current_session.managed_runtime = managed_runtime
            current_session.managed_context = context
            current_session.managed_page = existing_page
            return True

        with mock.patch.object(
            qr_login,
            "launch_browser_persistent_context_async",
            new=mock.AsyncMock(return_value=fake_context),
            create=True,
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

    def test_qr_login_verification_profile_dir_prefers_existing_account_profile(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-existing")
        session.unb = "unb-value"
        session.proxy_account_id = "2095002164"

        profile_dir = manager._resolve_verification_profile_dir(session)

        self.assertEqual(
            profile_dir,
            os.path.join(os.getcwd(), "browser_data", "user_2095002164"),
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

    def test_qr_login_get_session_cookies_returns_managed_handles(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-3")
        session.status = "success"
        session.cookies = {"unb": "unb-value", "cookie2": "cookie2-value"}
        session.unb = "unb-value"
        session.managed_runtime = object()
        session.managed_context = object()
        session.managed_page = object()
        manager.sessions[session.session_id] = session

        cookies_info = manager.get_session_cookies(session.session_id)

        self.assertEqual(cookies_info["cookies"], "unb=unb-value; cookie2=cookie2-value")
        self.assertEqual(cookies_info["unb"], "unb-value")
        self.assertIs(cookies_info["managed_runtime"], session.managed_runtime)
        self.assertIs(cookies_info["managed_context"], session.managed_context)
        self.assertIs(cookies_info["managed_page"], session.managed_page)

    def test_qr_login_cleanup_session_assets_schedules_managed_handle_cleanup(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-4")
        session.managed_runtime = mock.Mock(close=mock.AsyncMock())
        session.managed_context = mock.Mock(close=mock.AsyncMock())
        session.managed_page = mock.Mock(close=mock.AsyncMock())

        fake_loop = mock.Mock()
        fake_loop.create_task.side_effect = lambda coro: coro.close()

        with mock.patch.object(qr_login.asyncio, "get_running_loop", return_value=fake_loop):
            manager._cleanup_session_assets(session)

        fake_loop.create_task.assert_called_once()
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
