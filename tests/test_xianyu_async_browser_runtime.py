import asyncio
import os
import sys
import types
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if "loguru" not in sys.modules:
    loguru_stub = types.ModuleType("loguru")
    loguru_stub.logger = mock.Mock()
    sys.modules["loguru"] = loguru_stub

if "websockets" not in sys.modules:
    sys.modules["websockets"] = types.ModuleType("websockets")

if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")

    class _ClientTimeout:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _ClientSession:
        pass

    class _TCPConnector:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _ClientError(Exception):
        pass

    aiohttp_stub.ClientTimeout = _ClientTimeout
    aiohttp_stub.ClientSession = _ClientSession
    aiohttp_stub.TCPConnector = _TCPConnector
    aiohttp_stub.ClientError = _ClientError
    sys.modules["aiohttp"] = aiohttp_stub

if "blackboxprotobuf" not in sys.modules:
    sys.modules["blackboxprotobuf"] = types.ModuleType("blackboxprotobuf")

if "execjs" not in sys.modules:
    execjs_stub = types.ModuleType("execjs")
    execjs_stub.runtime_names = ["stub-runtime"]
    execjs_stub.get = lambda: types.SimpleNamespace(name="stub-runtime")
    execjs_stub.compile = lambda _source: types.SimpleNamespace(call=lambda *args, **kwargs: None)
    sys.modules["execjs"] = execjs_stub

if "db_manager" not in sys.modules:
    db_manager_stub = types.ModuleType("db_manager")
    db_manager_stub.db_manager = mock.Mock()
    sys.modules["db_manager"] = db_manager_stub

if "utils.notification_dispatcher" not in sys.modules:
    notification_stub = types.ModuleType("utils.notification_dispatcher")
    notification_stub.SUPPORTED_NOTIFICATION_TEMPLATE_TYPES = ()
    notification_stub.build_face_verify_notification = lambda *args, **kwargs: ""
    notification_stub.dispatch_account_notifications = lambda *args, **kwargs: None
    notification_stub.dispatch_account_notifications_sync = lambda *args, **kwargs: None
    notification_stub.format_notification_template = lambda *args, **kwargs: ""
    notification_stub.get_notification_template_text = lambda *args, **kwargs: ""
    notification_stub.guess_verification_type = lambda *args, **kwargs: None
    notification_stub.render_notification_template = lambda *args, **kwargs: ""
    notification_stub.resolve_verification_type_label = lambda *args, **kwargs: ""
    sys.modules["utils.notification_dispatcher"] = notification_stub

if "utils.browser_provider" not in sys.modules:
    browser_provider_stub = types.ModuleType("utils.browser_provider")

    async def _launch_browser_async(*args, **kwargs):
        raise AssertionError("launch_browser_async should be patched in tests")

    async def _launch_browser_persistent_context_async(*args, **kwargs):
        raise AssertionError("launch_browser_persistent_context_async should be patched in tests")

    def _launch_browser(*args, **kwargs):
        raise AssertionError("launch_browser should be patched in tests")

    def _launch_browser_persistent_context(*args, **kwargs):
        raise AssertionError("launch_browser_persistent_context should be patched in tests")

    browser_provider_stub.BrowserLike = object
    browser_provider_stub.BrowserContextLike = object
    browser_provider_stub.PageLike = object
    browser_provider_stub.build_download_proxy_env = lambda proxy_url, base_env=None: dict(base_env or {})
    browser_provider_stub.launch_browser = _launch_browser
    browser_provider_stub.launch_browser_async = _launch_browser_async
    browser_provider_stub.launch_browser_persistent_context = _launch_browser_persistent_context
    browser_provider_stub.launch_browser_persistent_context_async = _launch_browser_persistent_context_async
    sys.modules["utils.browser_provider"] = browser_provider_stub

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

import XianyuAutoAsync
from XianyuAutoAsync import XianyuLive


class XianyuAsyncBrowserRuntimeTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _build_merge_result(merged_cookies_dict):
        return {
            "merged_cookies_dict": merged_cookies_dict,
            "updated_fields": sorted(merged_cookies_dict.keys()),
            "changed_fields": sorted(merged_cookies_dict.keys()),
            "new_fields": [],
            "preserved_fields": [],
            "preserved_protected_fields": [],
            "would_remove_fields": [],
            "removed_fields": [],
            "missing_protected_fields": [],
            "missing_required_fields": [],
            "incoming_missing_protected_fields": [],
            "account_switched": False,
            "incoming_count": len(merged_cookies_dict),
            "existing_count": 0,
            "merged_count": len(merged_cookies_dict),
        }

    async def test_launch_browser_safe_delegates_to_provider_helper(self):
        sentinel_browser = object()

        with mock.patch.object(XianyuAutoAsync, "_is_docker_env", return_value=False), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "launch_browser_async",
                 new=mock.AsyncMock(return_value=sentinel_browser),
             ) as launch_browser_async:
            browser = await XianyuAutoAsync._launch_browser_safe(
                "runtime-test",
                headless=True,
                args=["--flag"],
            )

        self.assertIs(browser, sentinel_browser)
        launch_browser_async.assert_awaited_once_with(
            headless=True,
            args=["--flag"],
        )

    async def test_launch_browser_safe_keeps_docker_event_loop_policy_compatibility(self):
        sentinel_browser = object()
        original_policy = asyncio.get_event_loop_policy()
        observed_policy_names = []

        async def fake_launch_browser_async(**_kwargs):
            observed_policy_names.append(type(asyncio.get_event_loop_policy()).__name__)
            return sentinel_browser

        with mock.patch.object(XianyuAutoAsync, "_is_docker_env", return_value=True), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "launch_browser_async",
                 side_effect=fake_launch_browser_async,
             ) as launch_browser_async:
            browser = await XianyuAutoAsync._launch_browser_safe("docker-runtime-test")

        self.assertIs(browser, sentinel_browser)
        self.assertEqual(observed_policy_names, ["_DockerEventLoopPolicy"])
        self.assertIs(asyncio.get_event_loop_policy(), original_policy)
        launch_browser_async.assert_awaited_once_with()

    def test_build_browser_refresh_context_options_defer_identity_to_provider(self):
        live = XianyuLive.__new__(XianyuLive)

        context_options = live._build_browser_refresh_context_options()

        self.assertEqual(context_options, {})

    def test_build_browser_cookie_payload_expands_cross_domain_session_cookies(self):
        live = XianyuLive.__new__(XianyuLive)

        cookies = live._build_browser_cookie_payload(
            "unb=user1; cookie2=cookie2v; _m_h5_tk=tkv; cna=cnav; foo=bar"
        )

        cookie_keys = {
            (cookie["name"], cookie["domain"]): cookie["value"]
            for cookie in cookies
        }

        self.assertEqual(cookie_keys[("unb", ".goofish.com")], "user1")
        self.assertEqual(cookie_keys[("unb", ".taobao.com")], "user1")
        self.assertEqual(cookie_keys[("cookie2", ".goofish.com")], "cookie2v")
        self.assertEqual(cookie_keys[("cookie2", ".taobao.com")], "cookie2v")
        self.assertEqual(cookie_keys[("_m_h5_tk", ".goofish.com")], "tkv")
        self.assertEqual(cookie_keys[("_m_h5_tk", ".taobao.com")], "tkv")
        self.assertEqual(cookie_keys[("cna", ".goofish.com")], "cnav")
        self.assertEqual(cookie_keys[("cna", ".taobao.com")], "cnav")
        self.assertEqual(cookie_keys[("foo", ".goofish.com")], "bar")
        self.assertNotIn(("foo", ".taobao.com"), cookie_keys)

    def test_resolve_account_browser_profile_dir_prefers_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "2095002164"
        live.cookies_str = "unb=qr-unb"

        profile_dir = live._resolve_account_browser_profile_dir()

        self.assertEqual(
            profile_dir,
            os.path.join(os.getcwd(), "browser_data", "user_2095002164"),
        )

    def test_build_browser_refresh_launch_args_skip_enable_automation_in_docker(self):
        live = XianyuLive.__new__(XianyuLive)

        with mock.patch.object(
            XianyuAutoAsync.os,
            "getenv",
            side_effect=lambda key: "1" if key == "DOCKER_ENV" else None,
        ):
            browser_args = live._build_browser_refresh_launch_args()

        self.assertNotIn("--enable-automation", browser_args)

    def test_calculate_retry_delay_uses_init_auth_failures_when_connection_failures_reset(self):
        live = XianyuLive.__new__(XianyuLive)
        live.connection_failures = 0
        live.init_auth_failures = 2

        retry_delay = live._calculate_retry_delay("Token获取失败(status=captcha_verification_failed)")

        self.assertEqual(retry_delay, 10)

    async def test_async_close_browser_closes_context_before_browser(self):
        close_order = []

        async def close_context():
            close_order.append("context.close")

        async def close_browser():
            close_order.append("browser.close")

        browser = mock.Mock()
        browser.close = mock.AsyncMock(side_effect=close_browser)

        context = mock.Mock()
        context.close = mock.AsyncMock(side_effect=close_context)

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "runtime-close-test"

        await live._async_close_browser(
            browser=browser,
            context=context,
        )

        self.assertEqual(close_order, ["context.close", "browser.close"])
        context.close.assert_awaited_once_with()
        browser.close.assert_awaited_once_with()

    async def test_async_close_browser_can_skip_managed_runtime_resources(self):
        close_order = []

        async def close_page():
            close_order.append("page.close")

        page = mock.Mock()
        page.close = mock.AsyncMock(side_effect=close_page)

        browser = mock.Mock()
        browser.close = mock.AsyncMock(side_effect=AssertionError("browser should stay open"))

        context = mock.Mock()
        context.close = mock.AsyncMock(side_effect=AssertionError("context should stay open"))

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "managed-runtime-close-test"
        live._safe_str = str

        await live._async_close_browser(
            browser=browser,
            context=context,
            page=page,
            close_browser=False,
            close_context=False,
        )

        self.assertEqual(close_order, ["page.close"])
        page.close.assert_awaited_once_with()
        context.close.assert_not_awaited()
        browser.close.assert_not_awaited()

    async def test_async_close_browser_closes_external_browser_only_once_when_context_is_not_owned(self):
        close_order = []

        async def close_browser():
            close_order.append("browser.close")

        browser = mock.Mock()
        browser.close = mock.AsyncMock(side_effect=close_browser)

        context = mock.Mock()
        context.close = mock.AsyncMock(side_effect=AssertionError("context should stay open"))

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "managed-runtime-browser-close-once-test"
        live._safe_str = str

        await live._async_close_browser(
            browser=browser,
            context=context,
            close_browser=True,
            close_context=False,
            close_page=False,
        )

        self.assertEqual(close_order, ["browser.close"])
        context.close.assert_not_awaited()
        browser.close.assert_awaited_once_with()

    async def test_force_close_resources_closes_page_context_browser_sequentially(self):
        close_order = []

        async def close_page():
            close_order.append("page.close")

        async def close_context():
            close_order.append("context.close")

        async def close_browser():
            close_order.append("browser.close")

        page = mock.Mock()
        page.close = mock.AsyncMock(side_effect=close_page)

        context = mock.Mock()
        context.close = mock.AsyncMock(side_effect=close_context)

        browser = mock.Mock()
        browser.close = mock.AsyncMock(side_effect=close_browser)

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "force-close-order-test"
        live._safe_str = str

        await live._force_close_resources(
            browser=browser,
            context=context,
            page=page,
        )

        self.assertEqual(
            close_order,
            ["page.close", "context.close", "browser.close"],
        )
        page.close.assert_awaited_once_with()
        context.close.assert_awaited_once_with()
        browser.close.assert_awaited_once_with()

    async def test_refresh_cookies_from_qr_login_launches_minimal_context_without_identity_overrides(self):
        browser = mock.Mock()
        browser.close = mock.AsyncMock()

        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()
        page.close = mock.AsyncMock()

        context = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=page)
        context.cookies = mock.AsyncMock(
            return_value=[
                {"name": "unb", "value": "new-unb"},
                {"name": "sgcookie", "value": "new-sg"},
                {"name": "cookie2", "value": "new-cookie2"},
                {"name": "_m_h5_tk", "value": "new-token_123"},
                {"name": "_m_h5_tk_enc", "value": "new-enc"},
                {"name": "t", "value": "new-t"},
                {"name": "cna", "value": "new-cna"},
            ]
        )
        context.close = mock.AsyncMock()
        browser.new_context = mock.AsyncMock(return_value=context)

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "fresh-context-test"
        live.user_id = 7
        live.qr_cookie_refresh_cooldown = 180
        live.last_qr_cookie_refresh_time = 0
        live._safe_str = str
        live._extract_cookie_value = lambda cookie_record: (cookie_record or {}).get("cookie")
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._set_runtime_cookie_state = mock.Mock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)

        qr_cookies_str = (
            "unb=qr-unb; sgcookie=qr-sg; cookie2=qr-cookie2; "
            "_m_h5_tk=qr-token_123; _m_h5_tk_enc=qr-enc; t=qr-t"
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "_launch_browser_safe",
            new=mock.AsyncMock(return_value=browser),
        ) as launch_browser_safe, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()), \
             mock.patch.object(
                 XianyuAutoAsync.os,
                 "getenv",
                 side_effect=lambda key: "1" if key == "DOCKER_ENV" else None,
             ), \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}), \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True):
            result = await live.refresh_cookies_from_qr_login(qr_cookies_str)

        self.assertTrue(result)
        launch_args = launch_browser_safe.await_args.kwargs["args"]
        self.assertNotIn("--enable-automation", launch_args)
        browser.new_context.assert_awaited_once_with()
        page.close.assert_awaited_once_with()
        context.close.assert_awaited_once_with()
        browser.close.assert_awaited_once_with()

    async def test_refresh_cookies_via_browser_page_reuses_persistent_profile_during_qr_grace(self):
        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()

        context = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=page)
        context.cookies = mock.AsyncMock(
            return_value=[
                {"name": "unb", "value": "user1"},
                {"name": "cookie2", "value": "cookie2v"},
                {"name": "_m_h5_tk", "value": "tokenv_123"},
                {"name": "_m_h5_tk_enc", "value": "encv"},
                {"name": "sgcookie", "value": "sgv"},
                {"name": "t", "value": "tv"},
                {"name": "cna", "value": "cnav"},
            ]
        )
        context.browser = mock.Mock()

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "2095002164"
        live.cookies_str = "unb=user1; cookie2=old; _m_h5_tk=old_123; _m_h5_tk_enc=oldenc; sgcookie=oldsg; t=oldt"
        live.cookies = {
            "unb": "user1",
            "cookie2": "old",
            "_m_h5_tk": "old_123",
            "_m_h5_tk_enc": "oldenc",
            "sgcookie": "oldsg",
            "t": "oldt",
        }
        live._safe_str = str
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._log_protected_merge_event = mock.Mock()
        live._log_cookie_merge_summary = mock.Mock()
        live._set_runtime_cookie_state = mock.Mock()
        live.update_config_cookies = mock.AsyncMock()
        live._async_close_browser = mock.AsyncMock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)
        live.get_qr_login_grace = mock.Mock(return_value={"stage": "real_cookie_ready"})

        with mock.patch.object(
            XianyuAutoAsync,
            "launch_browser_persistent_context_async",
            new=mock.AsyncMock(return_value=context),
        ) as launch_browser_persistent_context_async, \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("should not launch clean browser")),
             ):
            success = await live._refresh_cookies_via_browser_page(
                live.cookies_str,
                restart_on_success=False,
            )

        self.assertTrue(success)
        launch_kwargs = launch_browser_persistent_context_async.await_args.kwargs
        self.assertEqual(
            launch_kwargs["user_data_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_2095002164"),
        )
        self.assertTrue(launch_kwargs["headless"])
        context.add_cookies.assert_awaited_once()
        live.update_config_cookies.assert_awaited_once_with()

    async def test_refresh_cookies_via_browser_page_reuses_persistent_profile_after_recent_slider_success(self):
        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()

        context = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=page)
        context.cookies = mock.AsyncMock(
            return_value=[
                {"name": "unb", "value": "user1"},
                {"name": "cookie2", "value": "cookie2v"},
                {"name": "_m_h5_tk", "value": "tokenv_123"},
                {"name": "_m_h5_tk_enc", "value": "encv"},
                {"name": "sgcookie", "value": "sgv"},
                {"name": "t", "value": "tv"},
                {"name": "cna", "value": "cnav"},
            ]
        )
        context.browser = mock.Mock()

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "recent-slider-user"
        live.cookies_str = "unb=user1; cookie2=old; _m_h5_tk=old_123; _m_h5_tk_enc=oldenc; sgcookie=oldsg; t=oldt"
        live.cookies = {
            "unb": "user1",
            "cookie2": "old",
            "_m_h5_tk": "old_123",
            "_m_h5_tk_enc": "oldenc",
            "sgcookie": "oldsg",
            "t": "oldt",
        }
        live._safe_str = str
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._log_protected_merge_event = mock.Mock()
        live._log_cookie_merge_summary = mock.Mock()
        live._set_runtime_cookie_state = mock.Mock()
        live.update_config_cookies = mock.AsyncMock()
        live._async_close_browser = mock.AsyncMock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=True)

        with mock.patch.object(
            XianyuAutoAsync,
            "launch_browser_persistent_context_async",
            new=mock.AsyncMock(return_value=context),
        ) as launch_browser_persistent_context_async, \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("should not launch clean browser")),
             ):
            success = await live._refresh_cookies_via_browser_page(
                live.cookies_str,
                restart_on_success=False,
            )

        self.assertTrue(success)
        launch_kwargs = launch_browser_persistent_context_async.await_args.kwargs
        self.assertEqual(
            launch_kwargs["user_data_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_recent-slider-user"),
        )
        self.assertTrue(launch_kwargs["headless"])
        live.update_config_cookies.assert_awaited_once_with()

    async def test_try_password_login_refresh_disables_clean_context_during_qr_grace(self):
        captured = {}

        class _FakeSlider:
            def __init__(self, *args, **kwargs):
                captured["init_kwargs"] = kwargs

            def login_with_password_browser(self, *args, **kwargs):
                raise AssertionError("should be invoked via _run_sync_method_on_fresh_thread")

            async def _run_sync_method_on_fresh_thread(self, _func, **kwargs):
                captured["run_kwargs"] = kwargs
                raise RuntimeError("sentinel-slider-stop")

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "2095002164"
        live.cookies_str = "unb=user1; cookie2=old; _m_h5_tk=old_123; _m_h5_tk_enc=oldenc; sgcookie=oldsg; t=oldt"
        live.proxy_config = {}
        live.last_token_refresh_error_message = None
        live._safe_str = str
        live._normalize_risk_trigger_scene = XianyuLive._normalize_risk_trigger_scene.__get__(live, XianyuLive)
        live._new_risk_session_id = mock.Mock(return_value="risk-session")
        live._build_risk_event_meta = mock.Mock(return_value={})
        live._create_risk_log = mock.Mock(return_value=None)
        live._update_risk_log = mock.Mock()
        live.send_token_refresh_notification = mock.AsyncMock()
        live.get_qr_login_grace = mock.Mock(return_value={"stage": "real_cookie_ready"})

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()), \
             mock.patch.object(XianyuAutoAsync.db_manager, "mark_stale_risk_control_logs_failed", return_value=0), \
             mock.patch.object(XianyuAutoAsync.db_manager, "get_cookie_details", return_value={
                 "cookie_value": live.cookies_str,
                 "username": "user@example.com",
                 "password": "secret",
                 "show_browser": False,
             }), \
             mock.patch.object(XianyuLive, "acquire_auth_recovery_lock", return_value=(True, None)), \
             mock.patch.object(XianyuLive, "release_auth_recovery_lock"), \
             mock.patch.object(XianyuAutoAsync.os, "getenv", return_value=""), \
             mock.patch.dict(sys.modules, {
                 "utils.xianyu_slider_stealth": types.SimpleNamespace(XianyuSliderStealth=_FakeSlider),
             }):
            success = await live._try_password_login_refresh("滑块验证失败", trigger_scene="token_refresh")

        self.assertFalse(success)
        self.assertTrue(captured["init_kwargs"]["use_account_persistent_profile"])
        self.assertEqual(
            captured["init_kwargs"]["account_persistent_profile_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_2095002164"),
        )
        self.assertFalse(captured["run_kwargs"]["force_clean_context"])

    async def test_refresh_cookies_from_qr_login_reuses_managed_context_without_launching_browser(self):
        managed_runtime = mock.Mock()
        managed_runtime.close = mock.AsyncMock()

        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()
        page.close = mock.AsyncMock()

        context = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=page)
        context.cookies = mock.AsyncMock(
            return_value=[
                {"name": "unb", "value": "new-unb"},
                {"name": "sgcookie", "value": "new-sg"},
                {"name": "cookie2", "value": "new-cookie2"},
                {"name": "_m_h5_tk", "value": "new-token_123"},
                {"name": "_m_h5_tk_enc", "value": "new-enc"},
                {"name": "t", "value": "new-t"},
                {"name": "cna", "value": "new-cna"},
            ]
        )
        context.close = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "managed-context-test"
        live.user_id = 7
        live.qr_cookie_refresh_cooldown = 180
        live.last_qr_cookie_refresh_time = 0
        live._safe_str = str
        live._extract_cookie_value = lambda cookie_record: (cookie_record or {}).get("cookie")
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._set_runtime_cookie_state = mock.Mock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)

        qr_cookies_str = (
            "unb=qr-unb; sgcookie=qr-sg; cookie2=qr-cookie2; "
            "_m_h5_tk=qr-token_123; _m_h5_tk_enc=qr-enc; t=qr-t"
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "_launch_browser_safe",
            new=mock.AsyncMock(side_effect=AssertionError("managed context path should not launch browser")),
        ) as launch_browser_safe, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime=managed_runtime,
                managed_context=context,
            )

        self.assertTrue(result)
        launch_browser_safe.assert_not_awaited()
        context.add_cookies.assert_awaited_once()
        context.new_page.assert_awaited_once_with()
        page.goto.assert_awaited_once_with(
            "https://www.goofish.com/im",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        page.reload.assert_awaited_once_with(
            wait_until="domcontentloaded",
            timeout=12000,
        )
        page.close.assert_awaited_once_with()
        context.close.assert_not_awaited()
        managed_runtime.close.assert_not_awaited()
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_ignores_managed_page_without_context(self):
        managed_runtime = mock.Mock()
        managed_runtime.close = mock.AsyncMock()

        orphan_page = mock.Mock()
        orphan_page.goto = mock.AsyncMock()
        orphan_page.reload = mock.AsyncMock()
        orphan_page.close = mock.AsyncMock()

        created_page = mock.Mock()
        created_page.goto = mock.AsyncMock()
        created_page.reload = mock.AsyncMock()
        created_page.close = mock.AsyncMock()

        created_context = mock.Mock()
        created_context.add_cookies = mock.AsyncMock()
        created_context.new_page = mock.AsyncMock(return_value=created_page)
        created_context.cookies = mock.AsyncMock(
            return_value=[
                {"name": "unb", "value": "new-unb"},
                {"name": "sgcookie", "value": "new-sg"},
                {"name": "cookie2", "value": "new-cookie2"},
                {"name": "_m_h5_tk", "value": "new-token_123"},
                {"name": "_m_h5_tk_enc", "value": "new-enc"},
                {"name": "t", "value": "new-t"},
                {"name": "cna", "value": "new-cna"},
            ]
        )
        created_context.close = mock.AsyncMock()
        managed_runtime.new_context = mock.AsyncMock(return_value=created_context)

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "managed-page-without-context-test"
        live.user_id = 7
        live.qr_cookie_refresh_cooldown = 180
        live.last_qr_cookie_refresh_time = 0
        live._safe_str = str
        live._extract_cookie_value = lambda cookie_record: (cookie_record or {}).get("cookie")
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._set_runtime_cookie_state = mock.Mock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)

        qr_cookies_str = (
            "unb=qr-unb; sgcookie=qr-sg; cookie2=qr-cookie2; "
            "_m_h5_tk=qr-token_123; _m_h5_tk_enc=qr-enc; t=qr-t"
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "_launch_browser_safe",
            new=mock.AsyncMock(side_effect=AssertionError("managed runtime path should not launch browser")),
        ) as launch_browser_safe, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime=managed_runtime,
                managed_page=orphan_page,
            )

        self.assertTrue(result)
        launch_browser_safe.assert_not_awaited()
        managed_runtime.new_context.assert_awaited_once_with()
        created_context.new_page.assert_awaited_once_with()
        orphan_page.goto.assert_not_awaited()
        orphan_page.reload.assert_not_awaited()
        orphan_page.close.assert_not_awaited()
        created_page.close.assert_awaited_once_with()
        created_context.close.assert_awaited_once_with()
        managed_runtime.close.assert_not_awaited()
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_reuses_managed_page_without_creating_or_closing_extra_tabs(self):
        managed_runtime = mock.Mock()
        managed_runtime.close = mock.AsyncMock()

        managed_page = mock.Mock()
        managed_page.goto = mock.AsyncMock()
        managed_page.reload = mock.AsyncMock()
        managed_page.close = mock.AsyncMock()

        context = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(side_effect=AssertionError("managed page path should not create extra tab"))
        context.cookies = mock.AsyncMock(
            return_value=[
                {"name": "unb", "value": "new-unb"},
                {"name": "sgcookie", "value": "new-sg"},
                {"name": "cookie2", "value": "new-cookie2"},
                {"name": "_m_h5_tk", "value": "new-token_123"},
                {"name": "_m_h5_tk_enc", "value": "new-enc"},
                {"name": "t", "value": "new-t"},
                {"name": "cna", "value": "new-cna"},
            ]
        )
        context.close = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "managed-page-test"
        live.user_id = 7
        live.qr_cookie_refresh_cooldown = 180
        live.last_qr_cookie_refresh_time = 0
        live._safe_str = str
        live._extract_cookie_value = lambda cookie_record: (cookie_record or {}).get("cookie")
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._set_runtime_cookie_state = mock.Mock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)

        qr_cookies_str = (
            "unb=qr-unb; sgcookie=qr-sg; cookie2=qr-cookie2; "
            "_m_h5_tk=qr-token_123; _m_h5_tk_enc=qr-enc; t=qr-t"
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "_launch_browser_safe",
            new=mock.AsyncMock(side_effect=AssertionError("managed page path should not launch browser")),
        ) as launch_browser_safe, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime=managed_runtime,
                managed_context=context,
                managed_page=managed_page,
            )

        self.assertTrue(result)
        launch_browser_safe.assert_not_awaited()
        context.add_cookies.assert_awaited_once()
        context.new_page.assert_not_awaited()
        managed_page.goto.assert_awaited_once_with(
            "https://www.goofish.com/im",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        managed_page.reload.assert_awaited_once_with(
            wait_until="domcontentloaded",
            timeout=12000,
        )
        managed_page.close.assert_not_awaited()
        context.close.assert_not_awaited()
        managed_runtime.close.assert_not_awaited()
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_fetch_item_detail_from_browser_defers_identity_to_provider(self):
        browser = mock.Mock()
        context = mock.Mock()
        page = mock.Mock()
        detail_element = mock.Mock()

        browser.new_context = mock.AsyncMock(return_value=context)
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=page)
        detail_element.inner_text = mock.AsyncMock(return_value="detail text")
        page.goto = mock.AsyncMock()
        page.wait_for_selector = mock.AsyncMock(return_value=True)
        page.query_selector = mock.AsyncMock(return_value=detail_element)

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "item-detail-browser-test"
        live.cookies_str = "unb=1; cookie2=2"
        live._safe_str = str
        live._build_browser_refresh_launch_args = mock.Mock(return_value=["--cloak-flag"])
        live._build_browser_refresh_context_options = mock.Mock(return_value={})
        live._async_close_browser = mock.AsyncMock()

        with mock.patch.object(
            XianyuAutoAsync,
            "_launch_browser_safe",
            new=mock.AsyncMock(return_value=browser),
        ) as launch_browser_safe, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            result = await live._fetch_item_detail_from_browser("123456")

        self.assertEqual(result, "detail text")
        launch_browser_safe.assert_awaited_once_with(
            "item-detail-browser-test",
            headless=True,
            args=["--cloak-flag"],
        )
        browser.new_context.assert_awaited_once_with()
        page.goto.assert_awaited_once_with(
            "https://www.goofish.com/item?id=123456",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        live._build_browser_refresh_context_options.assert_called_once_with()
        live._async_close_browser.assert_awaited_once_with(browser=browser, context=context)

    async def test_refresh_cookies_via_browser_reuses_persistent_profile_after_recent_slider_success(self):
        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()
        page.title = mock.AsyncMock(return_value="聊天_闲鱼")

        context = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=page)
        context.cookies = mock.AsyncMock(
            return_value=[
                {"name": "unb", "value": "user1"},
                {"name": "cookie2", "value": "cookie2v"},
                {"name": "_m_h5_tk", "value": "tokenv_123"},
                {"name": "_m_h5_tk_enc", "value": "encv"},
                {"name": "sgcookie", "value": "sgv"},
                {"name": "t", "value": "tv"},
                {"name": "cna", "value": "cnav"},
            ]
        )
        context.browser = mock.Mock()

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "recent-slider-user"
        live.cookies_str = "unb=user1; cookie2=old; _m_h5_tk=old_123; _m_h5_tk_enc=oldenc; sgcookie=oldsg; t=oldt"
        live.cookies = {
            "unb": "user1",
            "cookie2": "old",
            "_m_h5_tk": "old_123",
            "_m_h5_tk_enc": "oldenc",
            "sgcookie": "oldsg",
            "t": "oldt",
        }
        live.last_qr_cookie_refresh_time = 0
        live.qr_cookie_refresh_cooldown = 0
        live._safe_str = str
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._log_protected_merge_event = mock.Mock()
        live._log_cookie_merge_summary = mock.Mock()
        live._set_runtime_cookie_state = mock.Mock()
        live.update_config_cookies = mock.AsyncMock()
        live._async_close_browser = mock.AsyncMock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=True)
        live.browser_cookie_refreshed = False

        with mock.patch.object(
            XianyuAutoAsync,
            "launch_browser_persistent_context_async",
            new=mock.AsyncMock(return_value=context),
        ) as launch_browser_persistent_context_async, \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("should not launch clean browser")),
             ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            success = await live._refresh_cookies_via_browser(triggered_by_refresh_token=False)

        self.assertTrue(success)
        launch_kwargs = launch_browser_persistent_context_async.await_args.kwargs
        self.assertEqual(
            launch_kwargs["user_data_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_recent-slider-user"),
        )
        self.assertTrue(launch_kwargs["headless"])
        live.update_config_cookies.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
