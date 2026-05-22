import time
import sys
import unittest
from unittest import mock
from types import SimpleNamespace

import XianyuAutoAsync
from XianyuAutoAsync import ConnectionState, XianyuLive


class _FakeTokenRefreshResponse:
    def __init__(self):
        self.status = 200
        self.headers = {}
        self.json_content_type = object()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        self.json_content_type = content_type
        return {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "accessToken": "oauth_access_token",
            },
        }


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.post_calls = []

    def post(self, *args, **kwargs):
        self.post_calls.append(
            {
                "args": args,
                "kwargs": kwargs,
            }
        )
        return self.response


class XianyuTokenRefreshRequestTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        original_xianyu_module = sys.modules.get("XianyuAutoAsync")
        sys.modules["XianyuAutoAsync"] = XianyuAutoAsync

        def _restore_module_binding():
            if original_xianyu_module is None:
                if sys.modules.get("XianyuAutoAsync") is XianyuAutoAsync:
                    sys.modules.pop("XianyuAutoAsync", None)
            else:
                sys.modules["XianyuAutoAsync"] = original_xianyu_module

        self.addCleanup(_restore_module_binding)

    async def test_preflight_token_after_password_login_reuses_existing_current_token(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "prewarmed_token_account"
        live.current_token = "oauth_browser_native_token"
        live.last_token_refresh_time = time.time()
        live.device_id = "browser-device-id"
        live.last_message_received_time = 0
        live.last_token_refresh_status = None
        live.last_token_refresh_error_message = None
        live._skip_db_cookie_reload_for_token_refresh = False
        live._canonical_account_id = lambda: "prewarmed_token_account"
        live.refresh_token = mock.AsyncMock(
            side_effect=AssertionError("should not refresh token when current_token is already available")
        )

        with mock.patch.object(XianyuLive, "cache_auth_prewarmed_token") as cache_token:
            token = await live.preflight_token_after_password_login()

        self.assertEqual("oauth_browser_native_token", token)
        live.refresh_token.assert_not_awaited()
        cache_token.assert_called_once()
        self.assertEqual("prewarmed_token_account", cache_token.call_args.args[0])
        self.assertEqual("oauth_browser_native_token", cache_token.call_args.args[1])
        self.assertEqual("password_login_refresh", cache_token.call_args.kwargs["source"])
        self.assertEqual("browser-device-id", cache_token.call_args.kwargs["device_id"])

    def test_live_browser_login_token_success_caches_auth_prewarmed_token(self):
        import utils.xianyu_slider_stealth as slider_stealth

        class _FakeResponse:
            url = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
            status = 200
            request = SimpleNamespace(
                post_data="data=%7B%22appKey%22%3A%22444e9908a51d1cb236a27862abc769c9%22%2C%22deviceId%22%3A%22browser-native-device-id%22%7D"
            )

            def text(self):
                return (
                    '{"api":"mtop.taobao.idlemessage.pc.login.token","data":{"accessToken":"oauth_browser_live_token"},'
                    '"ret":["SUCCESS::调用成功"],"v":"1.0"}'
                )

        slider_like = SimpleNamespace(
            pure_user_id="10",
            last_live_browser_business_probe_status={},
        )
        slider_like._extract_login_token_device_id_from_payload_text = (
            slider_stealth.XianyuSliderStealth._extract_login_token_device_id_from_payload_text.__get__(
                slider_like, SimpleNamespace
            )
        )

        with mock.patch.object(XianyuLive, "cache_auth_prewarmed_token") as cache_token:
            slider_stealth.XianyuSliderStealth._update_live_browser_business_probe_status_from_response(
                slider_like,
                _FakeResponse(),
            )

        self.assertEqual(
            {
                "login_token_native": True,
            },
            slider_like.last_live_browser_business_probe_status,
        )
        cache_token.assert_called_once()
        self.assertEqual("10", cache_token.call_args.args[0])
        self.assertEqual("oauth_browser_live_token", cache_token.call_args.args[1])
        self.assertEqual("browser_live_login_token", cache_token.call_args.kwargs["source"])
        self.assertEqual("browser-native-device-id", cache_token.call_args.kwargs["device_id"])

    def test_browser_cookie_warmup_login_token_success_caches_auth_prewarmed_token(self):
        import utils.xianyu_slider_stealth as slider_stealth

        slider_like = SimpleNamespace(
            pure_user_id="10",
        )
        slider_like._extract_login_token_device_id_from_payload_text = (
            slider_stealth.XianyuSliderStealth._extract_login_token_device_id_from_payload_text.__get__(
                slider_like, SimpleNamespace
            )
        )
        probe_result = {
            "ok": True,
            "text": (
                '{"api":"mtop.taobao.idlemessage.pc.login.token","data":{"accessToken":"oauth_browser_warmup_token"},'
                '"ret":["SUCCESS::调用成功"],"v":"1.0"}'
            ),
        }

        with mock.patch.object(XianyuLive, "cache_auth_prewarmed_token") as cache_token:
            slider_stealth.XianyuSliderStealth._cache_auth_prewarmed_token_from_probe_result(
                slider_like,
                "login_token_fetch",
                probe_result,
                request_body="data=%7B%22appKey%22%3A%22444e9908a51d1cb236a27862abc769c9%22%2C%22deviceId%22%3A%22browser-warmup-device-id%22%7D",
            )

        self.assertEqual("oauth_browser_warmup_token", slider_like.last_browser_warmup_auth_token)
        self.assertEqual("browser-warmup-device-id", slider_like.last_browser_warmup_auth_device_id)
        cache_token.assert_called_once()
        self.assertEqual("10", cache_token.call_args.args[0])
        self.assertEqual("oauth_browser_warmup_token", cache_token.call_args.args[1])
        self.assertEqual("browser_warmup_login_token", cache_token.call_args.kwargs["source"])
        self.assertEqual("browser-warmup-device-id", cache_token.call_args.kwargs["device_id"])

    def test_apply_auth_prewarmed_token_info_reuses_browser_device_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "10"
        live.current_token = None
        live.last_token_refresh_time = 0
        live.device_id = "python-generated-device-id"

        live._apply_auth_prewarmed_token_info(
            {
                "token": "oauth_browser_bundle_token",
                "timestamp": 123.0,
                "source": "browser_live_login_token",
                "device_id": "browser-bundle-device-id",
            },
            init_log_account_id="10",
        )

        self.assertEqual("oauth_browser_bundle_token", live.current_token)
        self.assertEqual(123.0, live.last_token_refresh_time)
        self.assertEqual("browser-bundle-device-id", live.device_id)

    async def test_init_disables_password_login_recovery_during_handoff_recovery(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "handoff_init_account"
        live.current_token = None
        live.last_token_refresh_time = 0
        live.token_refresh_interval = 60
        live.last_token_refresh_status = None
        live.last_init_failure_reason = None
        live.last_init_failure_type = None
        live.init_auth_failures = 1
        live.device_id = "device-id"
        live._canonical_account_id = lambda: "1"
        live.get_manual_refresh_state = lambda _account_id: {
            "phase": "handoff_recovery",
        }
        live.clear_init_auth_failure_state = lambda *_args, **_kwargs: None

        async def fake_refresh_token(*, allow_password_login_recovery=True, **_kwargs):
            live.current_token = "oauth_access_token"
            return "oauth_access_token"

        live.refresh_token = mock.AsyncMock(side_effect=fake_refresh_token)

        async def fail_send_notification(*_args, **_kwargs):
            raise AssertionError("init success path should not send token notification")

        live.send_token_refresh_notification = fail_send_notification
        fake_ws = mock.AsyncMock()

        with mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            await live.init(fake_ws)

        live.refresh_token.assert_awaited_once_with(allow_password_login_recovery=False)
        self.assertEqual(fake_ws.send.await_count, 2)
        self.assertEqual(live.current_token, "oauth_access_token")
        self.assertEqual(live.init_auth_failures, 0)

    async def test_init_keeps_password_login_recovery_enabled_outside_handoff(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "normal_init_account"
        live.current_token = None
        live.last_token_refresh_time = 0
        live.token_refresh_interval = 60
        live.last_token_refresh_status = None
        live.last_init_failure_reason = None
        live.last_init_failure_type = None
        live.init_auth_failures = 1
        live.device_id = "device-id"
        live._canonical_account_id = lambda: "1"
        live.get_manual_refresh_state = lambda _account_id: None
        live.clear_init_auth_failure_state = lambda *_args, **_kwargs: None

        async def fake_refresh_token(*, allow_password_login_recovery=True, **_kwargs):
            live.current_token = "oauth_access_token"
            return "oauth_access_token"

        live.refresh_token = mock.AsyncMock(side_effect=fake_refresh_token)

        async def fail_send_notification(*_args, **_kwargs):
            raise AssertionError("init success path should not send token notification")

        live.send_token_refresh_notification = fail_send_notification
        fake_ws = mock.AsyncMock()

        with mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            await live.init(fake_ws)

        live.refresh_token.assert_awaited_once_with(allow_password_login_recovery=True)
        self.assertEqual(fake_ws.send.await_count, 2)
        self.assertEqual(live.current_token, "oauth_access_token")
        self.assertEqual(live.init_auth_failures, 0)

    async def test_init_disables_password_login_recovery_during_qr_real_cookie_handoff(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "qr_handoff_init_account"
        live.current_token = None
        live.last_token_refresh_time = 0
        live.token_refresh_interval = 60
        live.last_token_refresh_status = None
        live.last_init_failure_reason = None
        live.last_init_failure_type = None
        live.init_auth_failures = 1
        live.device_id = "device-id"
        live._canonical_account_id = lambda: "1"
        live.get_manual_refresh_state = lambda _account_id: None
        live.get_qr_login_grace = lambda _account_id: {
            "stage": "real_cookie_ready",
        }
        live.clear_init_auth_failure_state = lambda *_args, **_kwargs: None

        async def fake_refresh_token(*, allow_password_login_recovery=True, **_kwargs):
            live.current_token = "oauth_access_token"
            return "oauth_access_token"

        live.refresh_token = mock.AsyncMock(side_effect=fake_refresh_token)

        async def fail_send_notification(*_args, **_kwargs):
            raise AssertionError("init success path should not send token notification")

        live.send_token_refresh_notification = fail_send_notification
        fake_ws = mock.AsyncMock()

        with mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            await live.init(fake_ws)

        live.refresh_token.assert_awaited_once_with(allow_password_login_recovery=False)
        self.assertEqual(fake_ws.send.await_count, 2)
        self.assertEqual(live.current_token, "oauth_access_token")
        self.assertEqual(live.init_auth_failures, 0)

    async def test_refresh_token_reuses_session_and_passes_proxy(self):
        fake_response = _FakeTokenRefreshResponse()
        fake_session = _FakeSession(fake_response)

        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "token_refresh_proxy_test"
        live.session = fake_session
        live._http_proxy_url = "http://127.0.0.1:8888"
        live.device_id = "device-id"
        live.cookies_str = "_m_h5_tk=test_token_12345; cookie2=dummy_cookie2"
        live.current_token = None
        live.last_token_refresh_time = 0
        live.last_message_received_time = 123
        live.message_cookie_refresh_cooldown = 0
        live.max_captcha_verification_count = 3
        live.last_token_refresh_status = None
        live.last_token_refresh_error_message = None
        live.restarted_in_browser_refresh = True
        live.init_auth_failures = 2
        live.last_init_failure_reason = "old_reason"
        live.last_init_failure_type = "old_type"
        live._skip_db_cookie_reload_for_token_refresh = True

        create_session_called = False

        async def fake_create_session():
            nonlocal create_session_called
            create_session_called = True

        live.create_session = fake_create_session
        live._reload_latest_cookies_from_db = lambda *_args, **_kwargs: None
        live._extract_set_cookie_updates = lambda headers: {}
        live._build_cookie_string_with_updates = lambda cookie_string, updates: cookie_string
        live._need_captcha_verification = lambda _payload: False
        live._consume_pending_slider_success_notice = lambda: False
        live.clear_qr_login_grace = lambda *_args, **_kwargs: None
        live.clear_init_auth_failure_state = lambda *_args, **_kwargs: None

        async def fail_send_notification(*_args, **_kwargs):
            raise AssertionError("success path should not send token refresh notification")

        live.send_token_refresh_notification = fail_send_notification

        token = await live._refresh_token_impl(allow_password_login_recovery=False)

        self.assertEqual(token, "oauth_access_token")
        self.assertFalse(create_session_called)
        self.assertEqual(live.current_token, "oauth_access_token")
        self.assertEqual(live.last_token_refresh_status, "success")
        self.assertIsNone(live.last_token_refresh_error_message)
        self.assertEqual(live.last_message_received_time, 0)
        self.assertEqual(len(fake_session.post_calls), 1)
        request = fake_session.post_calls[0]
        self.assertEqual(request["kwargs"]["proxy"], "http://127.0.0.1:8888")
        self.assertEqual(fake_response.json_content_type, None)

    async def test_handle_captcha_verification_marks_slider_scene_as_token_refresh(self):
        created_sliders = []

        class _FakeSlider:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.risk_trigger_scene = None
                created_sliders.append(self)

            async def async_run(self, verification_url):
                self.verification_url = verification_url
                return False, None

        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "token_refresh_captcha_scene_test"
        live.cookies_str = "_m_h5_tk=test_token_12345; cookie2=dummy_cookie2"
        live.proxy_config = {}
        live.connection_state = ConnectionState.DISCONNECTED
        live.ws = None
        live._safe_str = lambda exc: str(exc)

        async def fake_send_notification(*_args, **_kwargs):
            return None

        live.send_token_refresh_notification = fake_send_notification

        with mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={}), \
             mock.patch("XianyuAutoAsync.log_captcha_event"), \
             mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", _FakeSlider):
            result = await live._handle_captcha_verification(
                {"data": {"url": "https://example.com/punish?action=captcha"}}
            )

        self.assertIsNone(result)
        self.assertEqual(len(created_sliders), 1)
        self.assertEqual(created_sliders[0].risk_trigger_scene, "token_refresh")

    async def test_handle_captcha_verification_enables_account_persistent_profile_for_token_refresh(self):
        created_sliders = []

        class _FakeSlider:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.risk_trigger_scene = None
                created_sliders.append(self)

            async def async_run(self, verification_url):
                self.verification_url = verification_url
                return False, None

        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "token_refresh_persistent_profile_test"
        live.cookies_str = "_m_h5_tk=test_token_12345; cookie2=dummy_cookie2"
        live.proxy_config = {}
        live.connection_state = ConnectionState.DISCONNECTED
        live.ws = None
        live._safe_str = lambda exc: str(exc)

        async def fake_send_notification(*_args, **_kwargs):
            return None

        live.send_token_refresh_notification = fake_send_notification

        with mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={}), \
             mock.patch("XianyuAutoAsync.log_captcha_event"), \
             mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", _FakeSlider):
            result = await live._handle_captcha_verification(
                {"data": {"url": "https://example.com/punish?action=captcha"}}
            )

        self.assertIsNone(result)
        self.assertEqual(len(created_sliders), 1)
        self.assertTrue(created_sliders[0].kwargs.get("use_account_persistent_profile"))

    async def test_handle_captcha_verification_closes_slider_resources_when_runtime_attach_fails(self):
        created_sliders = []

        class _FakeSlider:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.risk_trigger_scene = None
                self._concurrency_slot_registered = True
                self.browser = None
                self.context = None
                self.page = None
                self.playwright = None
                self.close_browser = mock.Mock()
                created_sliders.append(self)

            async def _run_sync_method_on_fresh_thread(self, *_args, **_kwargs):
                raise RuntimeError("attach failed before run")

        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "token_refresh_cleanup_test"
        live.cookies_str = "_m_h5_tk=test_token_12345; cookie2=dummy_cookie2"
        live.proxy_config = {}
        live.connection_state = ConnectionState.DISCONNECTED
        live.ws = None
        live._safe_str = lambda exc: str(exc)
        live._canonical_account_id = lambda: "token_refresh_cleanup_test"
        live.is_manual_refresh_active = lambda *_args, **_kwargs: False

        async def fake_send_notification(*_args, **_kwargs):
            return None

        live.send_token_refresh_notification = fake_send_notification

        with mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={}), \
             mock.patch("XianyuAutoAsync.log_captcha_event"), \
             mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", _FakeSlider):
            result = await live._handle_captcha_verification(
                {"data": {"url": "https://example.com/punish?action=captcha"}}
            )

        self.assertIsNone(result)
        self.assertEqual(len(created_sliders), 1)
        created_sliders[0].close_browser.assert_called_once_with()

    async def test_handle_captcha_verification_runs_browser_stabilization_when_only_havana_missing(self):
        created_sliders = []

        slider_cookies = {
            "unb": "u1",
            "sgcookie": "sg1",
            "cookie2": "c2_new",
            "_m_h5_tk": "tk_new",
            "_m_h5_tk_enc": "enc_new",
            "t": "t_new",
            "cna": "cna_new",
            "_tb_token_": "tb_new",
            "x5sec": "x5_new",
            "x5secdata": "x5data_new",
        }

        class _FakeSlider:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.risk_trigger_scene = None
                self._concurrency_slot_registered = False
                self.browser = None
                self.context = None
                self.page = None
                self.playwright = None
                self.close_browser = mock.Mock()
                created_sliders.append(self)

            async def _run_sync_method_on_fresh_thread(self, *_args, **_kwargs):
                return True, dict(slider_cookies)

        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "token_refresh_havana_stabilize_test"
        live.cookies_str = "unb=u1; sgcookie=sg_old; cookie2=c2_old; _m_h5_tk=tk_old; _m_h5_tk_enc=enc_old; t=t_old; cna=cna_old; _tb_token_=tb_old"
        live.cookies = {
            "unb": "u1",
            "sgcookie": "sg_old",
            "cookie2": "c2_old",
            "_m_h5_tk": "tk_old",
            "_m_h5_tk_enc": "enc_old",
            "t": "t_old",
            "cna": "cna_old",
            "_tb_token_": "tb_old",
        }
        live.proxy_config = {}
        live.connection_state = ConnectionState.DISCONNECTED
        live.ws = None
        live._safe_str = lambda exc: str(exc)
        live._canonical_account_id = lambda: "token_refresh_havana_stabilize_test"
        live.is_manual_refresh_active = lambda *_args, **_kwargs: False
        live.get_qr_login_grace = lambda *_args, **_kwargs: None
        live._log_protected_merge_event = lambda *_args, **_kwargs: None
        live._log_cookie_merge_summary = lambda *_args, **_kwargs: None
        live._mark_slider_success_recovery = lambda *_args, **_kwargs: None
        live._mark_pending_slider_success_notice = lambda *_args, **_kwargs: None

        def _set_runtime_cookie_state(*, cookies_str, cookies_dict, source):
            live.cookies_str = cookies_str
            live.cookies = dict(cookies_dict)
            live.last_cookie_source = source

        live._set_runtime_cookie_state = _set_runtime_cookie_state
        live.update_config_cookies = mock.AsyncMock()

        merged_cookies = dict(slider_cookies)
        merge_result = {
            "merged_cookies_dict": merged_cookies,
            "updated_fields": list(merged_cookies.keys()),
            "changed_fields": ["cookie2", "_m_h5_tk", "_m_h5_tk_enc", "t", "cna", "_tb_token_"],
            "new_fields": ["x5sec", "x5secdata"],
            "removed_fields": [],
            "preserved_fields": [],
            "preserved_protected_fields": [],
            "would_remove_fields": [],
            "missing_protected_fields": ["havana_lgc2_77"],
            "missing_required_fields": [],
            "incoming_missing_protected_fields": ["havana_lgc2_77"],
            "account_switched": False,
        }
        live.protected_merge_cookie_dicts = mock.Mock(return_value=merge_result)

        async def fake_browser_stabilization(cookie_string, restart_on_success=True):
            self = live
            self.cookies["havana_lgc2_77"] = "hv1"
            self.cookies_str = cookie_string + "; havana_lgc2_77=hv1"
            return True

        live._refresh_cookies_via_browser_page = mock.AsyncMock(side_effect=fake_browser_stabilization)

        async def fake_send_notification(*_args, **_kwargs):
            return None

        live.send_token_refresh_notification = fake_send_notification

        with mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={}), \
             mock.patch("XianyuAutoAsync.log_captcha_event"), \
             mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", _FakeSlider), \
             mock.patch.object(XianyuLive, "clear_password_login_failure_backoff"), \
             mock.patch(
                 "XianyuAutoAsync.account_browser_runtime_manager.run_sync_task_on_account_thread_async",
                 new=mock.AsyncMock(return_value=True),
             ) as invalidate_runtime_mock:
            result = await live._handle_captcha_verification(
                {"data": {"url": "https://example.com/punish?action=captcha"}}
            )

        self.assertIsNotNone(result)
        invalidate_runtime_mock.assert_awaited_once()
        live._refresh_cookies_via_browser_page.assert_awaited_once()
        self.assertIn("havana_lgc2_77=hv1", live.cookies_str)
        self.assertEqual(len(created_sliders), 1)


if __name__ == "__main__":
    unittest.main()
