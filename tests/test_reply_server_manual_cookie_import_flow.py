import asyncio
import time
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest import mock

import reply_server


class _FakeSlider:
    def __init__(self):
        self.run_called = False

    def run(self, *args, **kwargs):
        self.run_called = True
        return True, {"unexpected": "browser"}


class _FakeSessionClosedSlider:
    def __init__(self):
        self.run_called = False
        self.last_login_error = ""

    def _get_slider_failure_message(self, message):
        return message

    def run(self, target_url, notification_callback=None, notification_scene=None):
        self.run_called = True
        self.last_login_error = "浏览器会话已关闭或 CDP 已断开"
        if notification_callback:
            notification_callback(
                "Target page, context or browser has been closed",
                verification_url=target_url,
                verification_type="face_verify",
            )
        return False, None


class _FakeDeadRuntimeSlider:
    def __init__(self, error_message):
        self.context = object()
        self.page = object()
        self._error_message = error_message

    def _ensure_active_verification_session(self, _context, _page):
        return self._error_message


class ReplyServerManualCookieImportFlowTest(unittest.TestCase):
    def setUp(self):
        self._original_sessions = reply_server.manual_cookie_import_sessions
        reply_server.manual_cookie_import_sessions = {}

    def tearDown(self):
        reply_server.manual_cookie_import_sessions = self._original_sessions

    def test_execute_manual_cookie_import_short_circuits_when_cookie_precheck_is_already_valid(self):
        session_id = "manual_import_cookie_valid_session"
        account_id = "manual_import_cookie_valid_account"
        reply_server.manual_cookie_import_sessions[session_id] = {
            "account_id": account_id,
            "status": "processing",
            "verification_url": None,
            "screenshot_path": None,
            "verification_type": None,
            "slider_instance": None,
            "task": None,
            "timestamp": time.time(),
            "completed_at": None,
            "user_id": 1,
        }

        fake_slider = _FakeSlider()
        fake_manager = SimpleNamespace(
            cookies={},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
        )
        probe_result = {
            "status": "cookie_valid",
            "verification_url": None,
            "payload": {
                "ret": ["SUCCESS::调用成功"],
                "data": {
                    "accessToken": "oauth_access_token",
                    "refreshToken": "oauth_refresh_token",
                },
            },
            "session_cookies": {
                "unb": "test_user",
                "_m_h5_tk": "refreshed_token_12345",
                "cookie2": "updated_cookie2",
            },
        }
        merged_cookie_dict = dict(probe_result["session_cookies"])

        async def invoke():
            await reply_server._execute_manual_cookie_import(
                session_id=session_id,
                account_id=account_id,
                cookie_value="unb=test_user; _m_h5_tk=old_token_12345; cookie2=old_cookie2",
                show_browser=False,
                user_id=1,
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", return_value=fake_slider), \
             mock.patch("utils.xianyu_slider_stealth.probe_cookie_verification_from_cookie", return_value=probe_result), \
             mock.patch("utils.xianyu_slider_stealth.concurrency_manager.unregister_instance"), \
             mock.patch("XianyuAutoAsync.XianyuLive.protected_merge_cookie_dicts", return_value={
                 "incoming_missing_protected_fields": [],
                 "preserved_protected_fields": [],
                 "merged_cookies_dict": merged_cookie_dict,
             }), \
             mock.patch.object(reply_server.db_manager, "get_cookie_details", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={}), \
             mock.patch.object(reply_server.db_manager, "save_cookie") as save_cookie_mock, \
             mock.patch.object(reply_server.db_manager, "update_cookie_account_info") as update_cookie_mock, \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "log_with_user"):
            asyncio.run(invoke())

            deadline = time.time() + 2
            while (
                reply_server.manual_cookie_import_sessions[session_id]["status"] == "processing"
                and time.time() < deadline
            ):
                time.sleep(0.01)

        session = reply_server.manual_cookie_import_sessions[session_id]
        self.assertEqual(session["status"], "success")
        self.assertFalse(fake_slider.run_called)
        save_cookie_mock.assert_called_once()
        update_cookie_mock.assert_not_called()
        saved_cookie_value = save_cookie_mock.call_args.args[1]
        self.assertIn("_m_h5_tk=refreshed_token_12345", saved_cookie_value)
        self.assertIn("cookie2=updated_cookie2", saved_cookie_value)
        fake_manager.add_cookie.assert_called_once()

    def test_execute_manual_cookie_import_fast_fails_when_browser_session_is_closed(self):
        session_id = "manual_import_runtime_closed_session"
        account_id = "manual_import_runtime_closed_account"
        reply_server.manual_cookie_import_sessions[session_id] = {
            "account_id": account_id,
            "status": "processing",
            "verification_url": None,
            "screenshot_path": None,
            "verification_type": None,
            "slider_instance": None,
            "task": None,
            "timestamp": time.time(),
            "completed_at": None,
            "user_id": 1,
        }

        fake_slider = _FakeSessionClosedSlider()
        probe_result = {
            "status": "verification_required",
            "verification_url": "https://passport.goofish.com/iv/test",
            "payload": {"ret": ["FAIL_SYS_USER_VALIDATE"]},
        }

        async def invoke():
            await reply_server._execute_manual_cookie_import(
                session_id=session_id,
                account_id=account_id,
                cookie_value="unb=test_user; cookie2=old_cookie2",
                show_browser=False,
                user_id=1,
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", return_value=fake_slider), \
             mock.patch("utils.xianyu_slider_stealth.probe_cookie_verification_from_cookie", return_value=probe_result), \
             mock.patch("utils.xianyu_slider_stealth.concurrency_manager.unregister_instance"), \
             mock.patch.object(reply_server, "log_with_user"), \
             mock.patch.object(reply_server.db_manager, "get_cookie_details", return_value={}), \
             mock.patch.object(reply_server.db_manager, "save_cookie") as save_cookie_mock, \
             mock.patch.object(reply_server.db_manager, "update_cookie_account_info") as update_cookie_mock:
            asyncio.run(invoke())

            deadline = time.time() + 2
            while (
                reply_server.manual_cookie_import_sessions[session_id]["status"] == "processing"
                and time.time() < deadline
            ):
                time.sleep(0.01)

        session = reply_server.manual_cookie_import_sessions[session_id]
        self.assertEqual(session["status"], "failed")
        self.assertEqual(session["error"], "浏览器会话已关闭或 CDP 已断开")
        self.assertTrue(fake_slider.run_called)
        save_cookie_mock.assert_not_called()
        update_cookie_mock.assert_not_called()


class ReplyServerManualCookieImportStatusTest(unittest.TestCase):
    def setUp(self):
        self._original_sessions = reply_server.manual_cookie_import_sessions
        reply_server.manual_cookie_import_sessions = {}

    def tearDown(self):
        reply_server.manual_cookie_import_sessions = self._original_sessions

    def test_check_manual_cookie_import_status_fails_when_runtime_is_already_closed(self):
        session_id = "manual_import_dead_runtime_status_session"
        reply_server.manual_cookie_import_sessions[session_id] = {
            "account_id": "dead_runtime_account",
            "status": "verification_required",
            "verification_url": "https://passport.goofish.com/iv/test",
            "screenshot_path": None,
            "verification_type": "人脸验证",
            "slider_instance": _FakeDeadRuntimeSlider("浏览器会话已关闭或 CDP 已断开"),
            "task": None,
            "timestamp": time.time(),
            "completed_at": None,
            "user_id": 1,
        }

        result = asyncio.run(
            reply_server.check_manual_cookie_import_status(
                session_id,
                current_user={"user_id": 1, "username": "admin"},
            )
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "浏览器会话已关闭或 CDP 已断开")
        self.assertEqual(
            reply_server.manual_cookie_import_sessions[session_id]["status"],
            "failed",
        )


class ReplyServerQrCodeStatusTest(unittest.TestCase):
    def setUp(self):
        self._original_processed = reply_server.qr_check_processed
        self._original_locks = reply_server.qr_check_locks
        reply_server.qr_check_processed = {}
        reply_server.qr_check_locks = defaultdict(lambda: asyncio.Lock())

    def tearDown(self):
        reply_server.qr_check_processed = self._original_processed
        reply_server.qr_check_locks = self._original_locks

    def test_check_qr_code_status_passes_managed_runtime_context_and_page_to_background_processing(self):
        managed_runtime = object()
        managed_context = object()
        managed_page = object()
        process_mock = mock.AsyncMock(return_value={"account_id": "qr_account"})

        async def invoke():
            result = await reply_server.check_qr_code_status(
                "qr_session_with_managed_runtime",
                current_user={"user_id": 1, "username": "admin"},
            )
            await asyncio.sleep(0)
            return result

        with mock.patch.object(reply_server, "cleanup_qr_check_records"), \
             mock.patch.object(reply_server.qr_login_manager, "cleanup_expired_sessions"), \
             mock.patch.object(
                 reply_server.qr_login_manager,
                 "get_session_status",
                 return_value={"status": "success"},
             ), \
             mock.patch.object(
                 reply_server.qr_login_manager,
                 "get_session_cookies",
                 return_value={
                     "cookies": "unb=test_user; cookie2=test_cookie2",
                     "unb": "test_user",
                     "managed_runtime": managed_runtime,
                     "managed_context": managed_context,
                     "managed_page": managed_page,
                 },
             ), \
             mock.patch.object(reply_server, "process_qr_login_cookies", process_mock), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertEqual(result["status"], "confirmed")
        process_mock.assert_awaited_once_with(
            "unb=test_user; cookie2=test_cookie2",
            "test_user",
            {"user_id": 1, "username": "admin"},
            managed_runtime=managed_runtime,
            managed_context=managed_context,
            managed_page=managed_page,
        )


class _FakeQrRefreshSuccessLive:
    def __init__(self, cookies_str, cookie_id, user_id, register_instance=False, **kwargs):
        self.cookies_str = cookies_str
        self.cookie_id = cookie_id
        self.user_id = user_id
        self.register_instance = register_instance
        self.kwargs = kwargs

    async def refresh_cookies_from_qr_login(self, **kwargs):
        return True


class _FakeQrRefreshFailureLive(_FakeQrRefreshSuccessLive):
    async def refresh_cookies_from_qr_login(self, **kwargs):
        return False


class _FakeQrRefreshExceptionLive(_FakeQrRefreshSuccessLive):
    async def refresh_cookies_from_qr_login(self, **kwargs):
        raise RuntimeError("refresh exploded")


class _FakeQrRefreshHangOnTokenPrewarmLive(_FakeQrRefreshSuccessLive):
    @staticmethod
    def mark_qr_login_grace(*_args, **_kwargs):
        return None

    @staticmethod
    def cache_qr_prewarmed_token(*_args, **_kwargs):
        return None

    @staticmethod
    def clear_qr_login_grace(*_args, **_kwargs):
        return None

    @staticmethod
    def clear_qr_prewarmed_token(*_args, **_kwargs):
        return None

    async def refresh_cookies_from_qr_login(self, **kwargs):
        self.cookies_str = "unb=test_user; cookie2=real_cookie2"
        return True

    async def refresh_token(self, *args, **kwargs):
        await asyncio.Event().wait()


class _FakeQrRefreshPrewarmSuccessLive(_FakeQrRefreshSuccessLive):
    @staticmethod
    def mark_qr_login_grace(*_args, **_kwargs):
        return None

    @staticmethod
    def cache_qr_prewarmed_token(*_args, **_kwargs):
        return None

    @staticmethod
    def clear_qr_login_grace(*_args, **_kwargs):
        return None

    @staticmethod
    def clear_qr_prewarmed_token(*_args, **_kwargs):
        return None

    async def refresh_cookies_from_qr_login(self, **kwargs):
        self.cookies_str = "unb=test_user; cookie2=real_cookie2"
        return True

    async def refresh_token(self, *args, **kwargs):
        return "prewarmed_token"


class ReplyServerProcessQrLoginCookiesTest(unittest.TestCase):
    def test_process_qr_login_cookies_raises_and_never_saves_raw_cookie_when_real_cookie_flow_fails(self):
        current_user = {"user_id": 1, "username": "admin"}
        fake_manager = SimpleNamespace(
            cookies={},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
        )

        scenarios = (
            (
                "missing_real_cookie_in_db",
                _FakeQrRefreshSuccessLive,
                None,
                "扫码登录未完成：无法从数据库获取真实Cookie",
            ),
            (
                "refresh_failed",
                _FakeQrRefreshFailureLive,
                mock.DEFAULT,
                "扫码登录未完成：真实Cookie获取失败",
            ),
            (
                "refresh_exception",
                _FakeQrRefreshExceptionLive,
                mock.DEFAULT,
                "扫码登录未完成：获取真实Cookie异常: refresh exploded",
            ),
        )

        for scenario_name, live_cls, db_cookie_result, expected_error in scenarios:
            with self.subTest(scenario=scenario_name):
                get_cookie_by_id_mock = mock.Mock(return_value=db_cookie_result)
                save_cookie_mock = mock.Mock()
                update_cookie_mock = mock.Mock()

                with mock.patch("XianyuAutoAsync.XianyuLive", live_cls), \
                     mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={}), \
                     mock.patch.object(reply_server.db_manager, "get_cookie_by_id", get_cookie_by_id_mock), \
                     mock.patch.object(reply_server.db_manager, "add_risk_control_log", return_value=None), \
                     mock.patch.object(reply_server.db_manager, "update_risk_control_log"), \
                     mock.patch.object(reply_server.db_manager, "save_cookie", save_cookie_mock), \
                     mock.patch.object(reply_server.db_manager, "update_cookie_account_info", update_cookie_mock), \
                     mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
                     mock.patch.object(reply_server, "log_with_user"):
                    with self.assertRaisesRegex(RuntimeError, expected_error):
                        asyncio.run(
                            reply_server.process_qr_login_cookies(
                                "unb=test_user; cookie2=test_cookie2",
                                "test_user",
                                current_user,
                            )
                        )

                save_cookie_mock.assert_not_called()
                update_cookie_mock.assert_not_called()
                fake_manager.add_cookie.assert_not_called()
                fake_manager.update_cookie.assert_not_called()
                fake_manager.add_cookie.reset_mock()
                fake_manager.update_cookie.reset_mock()

    def test_process_qr_login_cookies_continues_when_token_prewarm_hangs_and_starts_account_task(self):
        current_user = {"user_id": 1, "username": "admin"}
        fake_loop = SimpleNamespace(
            is_closed=mock.Mock(return_value=False),
            is_running=mock.Mock(return_value=True),
        )
        fake_manager = SimpleNamespace(
            cookies={},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
            loop=fake_loop,
        )

        async def invoke():
            return await asyncio.wait_for(
                reply_server.process_qr_login_cookies(
                    "unb=test_user; cookie2=test_cookie2",
                    "test_user",
                    current_user,
                ),
                timeout=0.2,
            )

        with mock.patch("XianyuAutoAsync.XianyuLive", _FakeQrRefreshHangOnTokenPrewarmLive), \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={}), \
             mock.patch.object(
                 reply_server.db_manager,
                 "get_cookie_by_id",
                 return_value={"cookies_str": "unb=test_user; cookie2=real_cookie2"},
             ), \
             mock.patch.object(reply_server.db_manager, "add_risk_control_log", return_value=None), \
             mock.patch.object(reply_server.db_manager, "update_risk_control_log"), \
             mock.patch.object(reply_server.db_manager, "delete_cookie") as delete_cookie_mock, \
             mock.patch.object(reply_server.db_manager, "update_cookie_account_info") as update_cookie_mock, \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "QR_LOGIN_TOKEN_PREWARM_TIMEOUT_SECONDS", 0.01, create=True), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertEqual(result["account_id"], "test_user")
        self.assertTrue(result["task_restarted"])
        self.assertFalse(result["token_prewarmed"])
        self.assertIn("首次Token初始化超时", result["warning_message"])
        fake_manager.add_cookie.assert_called_once_with(
            "test_user",
            "unb=test_user; cookie2=real_cookie2",
            user_id=1,
        )
        fake_manager.update_cookie.assert_not_called()
        delete_cookie_mock.assert_not_called()
        update_cookie_mock.assert_not_called()

    def test_process_qr_login_cookies_rolls_back_when_manager_loop_is_not_running(self):
        current_user = {"user_id": 1, "username": "admin"}
        fake_loop = SimpleNamespace(
            is_closed=mock.Mock(return_value=False),
            is_running=mock.Mock(return_value=False),
        )
        fake_manager = SimpleNamespace(
            cookies={"test_user": "unb=test_user; cookie2=old_cookie2"},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
            loop=fake_loop,
        )

        async def invoke():
            return await reply_server.process_qr_login_cookies(
                "unb=test_user; cookie2=test_cookie2",
                "test_user",
                current_user,
            )

        with mock.patch("XianyuAutoAsync.XianyuLive", _FakeQrRefreshPrewarmSuccessLive), \
             mock.patch.object(
                 reply_server.db_manager,
                 "get_all_cookies",
                 return_value={"test_user": "unb=test_user; cookie2=old_cookie2"},
             ), \
             mock.patch.object(
                 reply_server.db_manager,
                 "get_cookie_by_id",
                 return_value={"cookies_str": "unb=test_user; cookie2=real_cookie2"},
             ), \
             mock.patch.object(reply_server.db_manager, "add_risk_control_log", return_value=None), \
             mock.patch.object(reply_server.db_manager, "update_risk_control_log"), \
             mock.patch.object(reply_server.db_manager, "delete_cookie") as delete_cookie_mock, \
             mock.patch.object(reply_server.db_manager, "update_cookie_account_info") as update_cookie_mock, \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertEqual(result["account_id"], "test_user")
        self.assertFalse(result["task_restarted"])
        self.assertTrue(result["token_prewarmed"])
        self.assertIn("账号事件循环未运行", result["warning_message"])
        fake_manager.add_cookie.assert_not_called()
        fake_manager.update_cookie.assert_not_called()
        delete_cookie_mock.assert_not_called()
        update_cookie_mock.assert_called_once_with(
            "test_user",
            cookie_value="unb=test_user; cookie2=old_cookie2",
        )


class _FakePasswordLoginSlider:
    def __init__(self):
        self.last_login_error = ""

    def login_with_password_browser(self, **_kwargs):
        return {
            "unb": "test_user",
            "_m_h5_tk": "raw_token_12345",
            "cookie2": "raw_cookie2",
        }


class _FakePasswordLoginPreflightSuccessLive:
    preflight_calls = 0
    browser_refresh_calls = 0
    reset_calls = 0

    def __init__(self, cookies_str, cookie_id, user_id, register_instance=False, **kwargs):
        self.cookies_str = cookies_str
        self.cookie_id = cookie_id
        self.user_id = user_id
        self.register_instance = register_instance
        self.kwargs = kwargs

    @classmethod
    def reset_state(cls):
        cls.preflight_calls = 0
        cls.browser_refresh_calls = 0
        cls.reset_calls = 0

    @staticmethod
    def protected_merge_cookie_dicts(existing_cookie_dict, incoming_cookie_dict):
        merged = dict(existing_cookie_dict or {})
        merged.update(incoming_cookie_dict or {})
        return {
            "incoming_missing_protected_fields": [],
            "preserved_protected_fields": [],
            "merged_cookies_dict": merged,
            "missing_required_fields": [],
            "account_switched": False,
            "incoming_count": len(incoming_cookie_dict or {}),
            "existing_count": len(existing_cookie_dict or {}),
            "merged_count": len(merged),
            "would_remove_fields": [],
        }

    async def preflight_token_after_password_login(self):
        type(self).preflight_calls += 1
        self.cookies_str = "unb=test_user; _m_h5_tk=prewarmed_token_12345; cookie2=prewarmed_cookie2"
        return "prewarmed_token_12345"

    def reset_qr_cookie_refresh_flag(self):
        type(self).reset_calls += 1

    async def _refresh_cookies_via_browser(self, triggered_by_refresh_token=False):
        _ = triggered_by_refresh_token
        type(self).browser_refresh_calls += 1
        return True


class _FakePasswordLoginFallbackLive(_FakePasswordLoginPreflightSuccessLive):
    async def preflight_token_after_password_login(self):
        type(self).preflight_calls += 1
        raise RuntimeError("preflight failed")

    async def _refresh_cookies_via_browser(self, triggered_by_refresh_token=False):
        _ = triggered_by_refresh_token
        type(self).browser_refresh_calls += 1
        self.cookies_str = (
            "unb=test_user; _m_h5_tk=browser_refreshed_token_12345; "
            "cookie2=browser_refreshed_cookie2"
        )
        return True


class ReplyServerPasswordLoginStatusTest(unittest.TestCase):
    def setUp(self):
        self._original_sessions = reply_server.password_login_sessions
        reply_server.password_login_sessions = {}

    def tearDown(self):
        reply_server.password_login_sessions = self._original_sessions

    def test_check_password_login_status_clears_stale_screenshot_after_verification_cleanup(self):
        session_id = "password_login_stale_screenshot_session"
        reply_server.password_login_sessions[session_id] = {
            "account_id": "stale_screenshot_account",
            "account": "test_user",
            "show_browser": False,
            "refresh_mode": False,
            "risk_control_log_id": None,
            "risk_session_id": session_id,
            "status": "verification_required",
            "verification_url": None,
            "screenshot_path": "C:\\does-not-exist\\face_verify_latest.jpg",
            "qr_code_url": None,
            "verification_type": "face_verify",
            "slider_instance": None,
            "task": None,
            "timestamp": time.time(),
            "completed_at": None,
            "user_id": 1,
        }

        result = asyncio.run(
            reply_server.check_password_login_status(
                session_id,
                current_user={"user_id": 1, "username": "admin"},
            )
        )

        self.assertEqual(result["status"], "verification_required")
        self.assertIsNone(result["screenshot_path"])
        self.assertFalse(result["verification_material_ready"])
        self.assertIn("等待登录完成", result["message"])
        self.assertIsNone(
            reply_server.password_login_sessions[session_id]["screenshot_path"]
        )


class ReplyServerPasswordLoginExecutionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._original_sessions = reply_server.password_login_sessions
        reply_server.password_login_sessions = {}

    def tearDown(self):
        reply_server.password_login_sessions = self._original_sessions

    async def _wait_for_password_login_status(self, session_id, expected_status, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            current_status = reply_server.password_login_sessions[session_id]["status"]
            if current_status == expected_status:
                return
            await asyncio.sleep(0.01)
        self.fail(
            f"password login session {session_id} did not reach {expected_status}, "
            f"last status: {reply_server.password_login_sessions[session_id]['status']}"
        )

    def _build_password_login_session(self, session_id):
        reply_server.password_login_sessions[session_id] = {
            "account_id": "test_user",
            "account": "test_account",
            "show_browser": False,
            "refresh_mode": False,
            "risk_control_log_id": None,
            "risk_session_id": session_id,
            "status": "processing",
            "verification_url": None,
            "screenshot_path": None,
            "qr_code_url": None,
            "verification_type": None,
            "slider_instance": None,
            "task": None,
            "timestamp": time.time(),
            "completed_at": None,
            "user_id": 1,
        }

    async def test_execute_password_login_prefers_token_preflight_before_browser_refresh(self):
        session_id = "password_login_preflight_first_session"
        self._build_password_login_session(session_id)
        _FakePasswordLoginPreflightSuccessLive.reset_state()

        fake_manager = SimpleNamespace(
            cookies={},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
        )
        fake_slider = _FakePasswordLoginSlider()

        with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", return_value=fake_slider), \
             mock.patch("utils.xianyu_slider_stealth.concurrency_manager.unregister_instance", return_value=True), \
             mock.patch("XianyuAutoAsync.XianyuLive", _FakePasswordLoginPreflightSuccessLive), \
             mock.patch.object(reply_server.db_manager, "get_cookie_details", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_proxy_config", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={}), \
             mock.patch.object(reply_server.db_manager, "update_cookie_account_info", return_value=True) as update_cookie_mock, \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "dispatch_account_notifications_sync", return_value=False), \
             mock.patch.object(reply_server, "render_notification_template", return_value="login ok"), \
             mock.patch.object(reply_server, "_close_password_login_pending_verification_risk_logs"), \
             mock.patch.object(reply_server, "_update_session_risk_log"), \
             mock.patch.object(reply_server, "log_with_user"):
            await reply_server._execute_password_login(
                session_id=session_id,
                account_id="test_user",
                account="test_account",
                password="test_password",
                show_browser=False,
                user_id=1,
                current_user={"user_id": 1, "username": "admin"},
            )
            await self._wait_for_password_login_status(session_id, "success")

        session = reply_server.password_login_sessions[session_id]
        self.assertEqual(session["status"], "success")
        self.assertEqual(_FakePasswordLoginPreflightSuccessLive.preflight_calls, 1)
        self.assertEqual(_FakePasswordLoginPreflightSuccessLive.browser_refresh_calls, 0)
        self.assertEqual(_FakePasswordLoginPreflightSuccessLive.reset_calls, 0)
        fake_manager.add_cookie.assert_called_once()
        saved_cookie_value = update_cookie_mock.call_args.kwargs["cookie_value"]
        self.assertIn("_m_h5_tk=prewarmed_token_12345", saved_cookie_value)
        self.assertIn("cookie2=prewarmed_cookie2", saved_cookie_value)

    async def test_execute_password_login_falls_back_to_browser_refresh_when_token_preflight_fails(self):
        session_id = "password_login_browser_refresh_fallback_session"
        self._build_password_login_session(session_id)
        _FakePasswordLoginFallbackLive.reset_state()

        fake_manager = SimpleNamespace(
            cookies={},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
        )
        fake_slider = _FakePasswordLoginSlider()

        with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", return_value=fake_slider), \
             mock.patch("utils.xianyu_slider_stealth.concurrency_manager.unregister_instance", return_value=True), \
             mock.patch("XianyuAutoAsync.XianyuLive", _FakePasswordLoginFallbackLive), \
             mock.patch.object(reply_server.db_manager, "get_cookie_details", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_proxy_config", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={}), \
             mock.patch.object(reply_server.db_manager, "update_cookie_account_info", return_value=True) as update_cookie_mock, \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "dispatch_account_notifications_sync", return_value=False), \
             mock.patch.object(reply_server, "render_notification_template", return_value="login ok"), \
             mock.patch.object(reply_server, "_close_password_login_pending_verification_risk_logs"), \
             mock.patch.object(reply_server, "_update_session_risk_log"), \
             mock.patch.object(reply_server, "log_with_user"):
            await reply_server._execute_password_login(
                session_id=session_id,
                account_id="test_user",
                account="test_account",
                password="test_password",
                show_browser=False,
                user_id=1,
                current_user={"user_id": 1, "username": "admin"},
            )
            await self._wait_for_password_login_status(session_id, "success")

        session = reply_server.password_login_sessions[session_id]
        self.assertEqual(session["status"], "success")
        self.assertEqual(_FakePasswordLoginFallbackLive.preflight_calls, 1)
        self.assertEqual(_FakePasswordLoginFallbackLive.browser_refresh_calls, 1)
        self.assertEqual(_FakePasswordLoginFallbackLive.reset_calls, 1)
        fake_manager.add_cookie.assert_called_once()
        saved_cookie_value = update_cookie_mock.call_args.kwargs["cookie_value"]
        self.assertIn("_m_h5_tk=browser_refreshed_token_12345", saved_cookie_value)
        self.assertIn("cookie2=browser_refreshed_cookie2", saved_cookie_value)


if __name__ == "__main__":
    unittest.main()
