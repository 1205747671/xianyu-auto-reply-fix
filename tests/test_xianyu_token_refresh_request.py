import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
