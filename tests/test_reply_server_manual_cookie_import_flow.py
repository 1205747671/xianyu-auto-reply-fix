import asyncio
import os
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

    def run(self, target_url, notification_callback=None, notification_scene=None, **_kwargs):
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


class ReplyServerManagedRuntimeHelperTest(unittest.TestCase):
    def test_acquire_slider_managed_runtime_sync_forwards_profile_dir_and_attaches_runtime(self):
        runtime = SimpleNamespace(browser=object(), playwright=object())
        lease = SimpleNamespace(
            account_id="same-account",
            purpose="manual_cookie_import",
            runtime=runtime,
        )
        page = object()
        context = object()
        runtime_request = {
            "account_id": "same-account",
            "purpose": "manual_cookie_import",
            "profile_dir": os.path.join(os.getcwd(), "browser_data", "user_same-account"),
            "profile_id": "profile-same-account",
            "browser_features": {"user_agent": "unit-test-agent"},
            "launch_options": {"headless": True},
            "context_options": {"viewport": {"width": 1600, "height": 900}},
            "persistent_context_options": {"accept_downloads": True},
            "initial_cookie_payload": [],
            "use_persistent_context": True,
        }
        slider = mock.Mock()
        slider.build_managed_runtime_request.return_value = dict(runtime_request)
        slider.attach_managed_runtime = mock.Mock()
        runtime_manager = SimpleNamespace(
            acquire_runtime_sync=mock.Mock(return_value=lease),
            get_fresh_page_sync=mock.Mock(return_value=(page, context)),
            release_runtime_sync=mock.Mock(),
        )

        with mock.patch.object(reply_server, "account_browser_runtime_manager", runtime_manager):
            result = reply_server._acquire_slider_managed_runtime_sync(
                "same-account",
                "manual_cookie_import",
                slider,
            )

        self.assertIs(result, lease)
        slider.build_managed_runtime_request.assert_called_once_with(
            account_id="same-account",
            purpose="manual_cookie_import",
        )
        runtime_manager.acquire_runtime_sync.assert_called_once_with(
            "same-account",
            "manual_cookie_import",
            exclusive=True,
            runtime_request=runtime_request,
        )
        runtime_manager.get_fresh_page_sync.assert_called_once_with(lease)
        slider.attach_managed_runtime.assert_called_once_with(
            lease=lease,
            runtime=runtime,
            browser=runtime.browser,
            context=context,
            page=page,
            playwright=runtime.playwright,
            browser_features={"user_agent": "unit-test-agent"},
            profile_id="profile-same-account",
        )
        runtime_manager.release_runtime_sync.assert_not_called()

    def test_slider_managed_runtime_request_keeps_same_account_profile_dir_across_purposes(self):
        from utils.xianyu_slider_stealth import XianyuSliderStealth

        browser_features = {
            "profile_id": "win_chrome_120_desktop",
        }

        def build_request(purpose):
            slider = XianyuSliderStealth.__new__(XianyuSliderStealth)
            slider.headless = True
            slider.initial_cookies = ""
            slider.browser_channel = None
            slider.executable_path = None
            slider.use_account_persistent_profile = True
            slider.account_persistent_profile_dir = None
            slider.pure_user_id = "same-account"
            slider.automation_backend = "cloakbrowser"
            slider.risk_trigger_scene = purpose
            slider.browser_features = {}
            slider.profile_id = "unassigned"
            slider._build_browser_features = mock.Mock(return_value=dict(browser_features))
            slider._build_browser_proxy_settings = mock.Mock(return_value=None)
            slider._build_browser_launch_args = mock.Mock(return_value=["--cloak-flag"])
            slider._sanitize_provider_launch_options = mock.Mock(side_effect=lambda options: dict(options))
            slider._apply_provider_launch_defaults = mock.Mock(side_effect=lambda options: dict(options))
            slider._build_browser_context_options = mock.Mock(
                return_value={"viewport": {"width": 1600, "height": 900}}
            )
            slider._build_persistent_context_options = mock.Mock(
                return_value={"accept_downloads": True, "ignore_https_errors": True}
            )
            slider._build_initial_cookie_payload = mock.Mock(return_value=[])
            return XianyuSliderStealth.build_managed_runtime_request(
                slider,
                account_id="same-account",
                purpose=purpose,
            )

        with mock.patch("utils.xianyu_slider_stealth.os.makedirs"):
            manual_request = build_request("manual_cookie_import")
            password_request = build_request("password_login")

        expected_profile_dir = os.path.join(os.getcwd(), "browser_data", "user_same-account")
        self.assertEqual(manual_request["profile_dir"], expected_profile_dir)
        self.assertEqual(password_request["profile_dir"], expected_profile_dir)
        self.assertEqual(manual_request["account_id"], "same-account")
        self.assertEqual(password_request["account_id"], "same-account")
        self.assertEqual(manual_request["purpose"], "manual_cookie_import")
        self.assertEqual(password_request["purpose"], "password_login")
        self.assertEqual(manual_request["profile_id"], "win_chrome_120_desktop")
        self.assertEqual(password_request["profile_id"], "win_chrome_120_desktop")
        self.assertTrue(manual_request["use_persistent_context"])
        self.assertTrue(password_request["use_persistent_context"])


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
             mock.patch.object(reply_server, "_acquire_slider_managed_runtime_sync") as acquire_runtime_mock, \
             mock.patch.object(reply_server.db_manager, "get_cookie_details", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={"bound_unb": "", "bind_status": "pending_bind"}), \
             mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=True), \
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
        acquire_runtime_mock.assert_not_called()
        save_cookie_mock.assert_called_once()
        update_cookie_mock.assert_not_called()
        saved_cookie_value = save_cookie_mock.call_args.args[1]
        self.assertIn("_m_h5_tk=refreshed_token_12345", saved_cookie_value)
        self.assertIn("cookie2=updated_cookie2", saved_cookie_value)
        fake_manager.add_cookie.assert_called_once()

    def test_execute_manual_cookie_import_acquires_managed_runtime_when_browser_repair_is_required(self):
        session_id = "manual_import_managed_runtime_session"
        account_id = "manual_import_managed_runtime_account"
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

        fake_slider = _FakeManagedManualCookieSlider()
        fake_manager = SimpleNamespace(
            cookies={},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
        )
        fake_lease = SimpleNamespace(account_id=account_id, purpose="manual_cookie_import")
        probe_result = {
            "status": "verification_required",
            "verification_url": "https://passport.goofish.com/iv/test",
            "payload": {"ret": ["FAIL_SYS_USER_VALIDATE"]},
        }

        def acquire_runtime(account_id_value, purpose, slider_instance):
            self.assertEqual(account_id_value, account_id)
            self.assertEqual(purpose, "manual_cookie_import")
            self.assertIs(slider_instance, fake_slider)
            slider_instance.browser = object()
            slider_instance.context = object()
            slider_instance.page = object()
            return fake_lease

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
             mock.patch.object(reply_server, "_acquire_slider_managed_runtime_sync", side_effect=acquire_runtime) as acquire_mock, \
             mock.patch.object(reply_server, "_release_slider_managed_runtime_sync") as release_mock, \
             mock.patch("XianyuAutoAsync.XianyuLive.protected_merge_cookie_dicts", return_value={
                 "incoming_missing_protected_fields": [],
                 "preserved_protected_fields": [],
                 "merged_cookies_dict": {
                     "unb": "test_user",
                     "_m_h5_tk": "browser_token_12345",
                     "cookie2": "browser_cookie2",
                 },
             }), \
             mock.patch.object(reply_server.db_manager, "get_cookie_details", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={"bound_unb": "", "bind_status": "pending_bind"}), \
             mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=True), \
             mock.patch.object(reply_server.db_manager, "save_cookie") as save_cookie_mock, \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={}), \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "log_with_user"):
            asyncio.run(invoke())

            deadline = time.time() + 2
            while (
                reply_server.manual_cookie_import_sessions[session_id]["status"] == "processing"
                and time.time() < deadline
            ):
                time.sleep(0.01)

        self.assertTrue(fake_slider.run_browser_ready)
        self.assertTrue(fake_slider.run_kwargs["require_managed_runtime"])
        acquire_mock.assert_called_once()
        release_mock.assert_called_once_with(fake_lease, reason="manual_cookie_import_completed")
        save_cookie_mock.assert_called_once()

    def test_execute_manual_cookie_import_does_not_overwrite_old_cookie_or_bound_unb_on_binding_conflict(self):
        session_id = "manual_import_bound_unb_conflict_session"
        account_id = "manual_import_bound_unb_conflict_account"
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

        fake_slider = _FakeManagedManualCookieSlider(
            cookies_result={
                "unb": "other_user",
                "_m_h5_tk": "browser_token_12345",
                "cookie2": "browser_cookie2",
            }
        )
        fake_manager = SimpleNamespace(
            cookies={account_id: "unb=old_user; cookie2=old_cookie2"},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
        )
        fake_lease = SimpleNamespace(account_id=account_id, purpose="manual_cookie_import")
        probe_result = {
            "status": "verification_required",
            "verification_url": "https://passport.goofish.com/iv/test",
            "payload": {"ret": ["FAIL_SYS_USER_VALIDATE"]},
        }

        def acquire_runtime(_account_id, _purpose, slider_instance):
            slider_instance.browser = object()
            slider_instance.context = object()
            slider_instance.page = object()
            return fake_lease

        async def invoke():
            await reply_server._execute_manual_cookie_import(
                session_id=session_id,
                account_id=account_id,
                cookie_value="unb=old_user; cookie2=old_cookie2",
                show_browser=False,
                user_id=1,
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", return_value=fake_slider), \
             mock.patch("utils.xianyu_slider_stealth.probe_cookie_verification_from_cookie", return_value=probe_result), \
             mock.patch("utils.xianyu_slider_stealth.concurrency_manager.unregister_instance"), \
             mock.patch.object(reply_server, "_acquire_slider_managed_runtime_sync", side_effect=acquire_runtime), \
             mock.patch.object(reply_server, "_release_slider_managed_runtime_sync"), \
             mock.patch("XianyuAutoAsync.XianyuLive.protected_merge_cookie_dicts", return_value={
                 "incoming_missing_protected_fields": [],
                 "preserved_protected_fields": [],
                 "merged_cookies_dict": {
                     "unb": "other_user",
                     "_m_h5_tk": "browser_token_12345",
                     "cookie2": "browser_cookie2",
                 },
             }), \
             mock.patch.object(reply_server.db_manager, "get_cookie_details", return_value={
                 "value": "unb=old_user; cookie2=old_cookie2",
                 "bound_unb": "old_user",
             }), \
             mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={"bound_unb": "old_user", "bind_status": "active"}), \
             mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=False) as bind_mock, \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={account_id: "unb=old_user; cookie2=old_cookie2"}), \
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
        self.assertEqual(session["status"], "failed")
        self.assertIn("bound_unb", session["error"])
        bind_mock.assert_called_once()
        save_cookie_mock.assert_not_called()
        update_cookie_mock.assert_not_called()
        fake_manager.add_cookie.assert_not_called()
        fake_manager.update_cookie.assert_not_called()

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
        fake_lease = SimpleNamespace(account_id=account_id, purpose="manual_cookie_import")
        probe_result = {
            "status": "verification_required",
            "verification_url": "https://passport.goofish.com/iv/test",
            "payload": {"ret": ["FAIL_SYS_USER_VALIDATE"]},
        }

        def acquire_runtime(_account_id, _purpose, slider_instance):
            slider_instance.browser = object()
            slider_instance.context = object()
            slider_instance.page = object()
            return fake_lease

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
             mock.patch.object(reply_server, "_acquire_slider_managed_runtime_sync", side_effect=acquire_runtime), \
             mock.patch.object(reply_server, "_release_slider_managed_runtime_sync"), \
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
                    "account_id": "qr_account",
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
            "qr_account",
            "unb=test_user; cookie2=test_cookie2",
            "test_user",
            {"user_id": 1, "username": "admin"},
            managed_runtime_lease=None,
            managed_runtime=managed_runtime,
            managed_context=managed_context,
            managed_page=managed_page,
        )


class _FakeQrRefreshSuccessLive:
    def __init__(
        self,
        cookies_str,
        cookie_id=None,
        user_id=None,
        register_instance=False,
        account_id=None,
        **kwargs,
    ):
        resolved_account_id = account_id or cookie_id
        self.cookies_str = cookies_str
        self.cookie_id = resolved_account_id
        self.account_id = resolved_account_id
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
                     mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={"bound_unb": "", "bind_status": "pending_bind"}), \
                     mock.patch.object(reply_server.db_manager, "assert_cookie_belongs_to_user", return_value=True), \
                     mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=True), \
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
                                "test_user",
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
        add_cookie_async = mock.AsyncMock(return_value=None)
        fake_manager = SimpleNamespace(
            cookies={},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
            _add_cookie_async=add_cookie_async,
            loop=fake_loop,
        )

        async def run_on_manager_loop(account_id, coroutine_factory, timeout=None, cancel_on_timeout=True):
            _ = account_id, timeout, cancel_on_timeout
            return await coroutine_factory()

        async def invoke():
            return await asyncio.wait_for(
                reply_server.process_qr_login_cookies(
                    "test_user",
                    "unb=test_user; cookie2=test_cookie2",
                    "test_user",
                    current_user,
                ),
                timeout=0.2,
            )

        with mock.patch("XianyuAutoAsync.XianyuLive", _FakeQrRefreshHangOnTokenPrewarmLive), \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={"bound_unb": "", "bind_status": "pending_bind"}), \
             mock.patch.object(reply_server.db_manager, "assert_cookie_belongs_to_user", return_value=True), \
             mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=True), \
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
             mock.patch.object(reply_server, "_run_live_instance_on_manager_loop", side_effect=run_on_manager_loop), \
             mock.patch.object(reply_server, "QR_LOGIN_TOKEN_PREWARM_TIMEOUT_SECONDS", 0.01, create=True), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertEqual(result["account_id"], "test_user")
        self.assertTrue(result["task_restarted"])
        self.assertFalse(result["token_prewarmed"])
        self.assertIn("首次Token将在后台继续初始化", result["warning_message"])
        add_cookie_async.assert_awaited_once_with(
            "test_user",
            "unb=test_user; cookie2=real_cookie2",
            user_id=1,
        )
        fake_manager.add_cookie.assert_not_called()
        fake_manager.update_cookie.assert_not_called()
        delete_cookie_mock.assert_not_called()
        update_cookie_mock.assert_not_called()

    def test_process_qr_login_cookies_reports_runtime_issue_when_manager_loop_is_not_running(self):
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
                "test_user",
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
             mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={"bound_unb": "test_user", "bind_status": "active"}), \
             mock.patch.object(reply_server.db_manager, "assert_cookie_belongs_to_user", return_value=True), \
             mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=True), \
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
        self.assertFalse(result["token_prewarmed"])
        self.assertIn("账号事件循环未运行", result["warning_message"])
        fake_manager.add_cookie.assert_not_called()
        fake_manager.update_cookie.assert_not_called()
        delete_cookie_mock.assert_not_called()
        update_cookie_mock.assert_not_called()


class _FakePasswordLoginSlider:
    def __init__(self):
        self.last_login_error = ""

    def build_managed_runtime_request(self, **kwargs):
        request = {
            "browser_features": {"user_agent": "unit-test-agent"},
            "launch_options": {},
            "context_options": {},
            "persistent_context_options": {},
            "use_persistent_context": False,
            "initial_cookie_payload": [],
        }
        request.update(kwargs)
        return request

    def login_with_password_browser(self, **_kwargs):
        return {
            "unb": "test_user",
            "_m_h5_tk": "raw_token_12345",
            "cookie2": "raw_cookie2",
        }


class _FakeManagedPasswordLoginSlider(_FakePasswordLoginSlider):
    def __init__(self):
        super().__init__()
        self.login_browser_ready = False
        self.login_kwargs = None

    def login_with_password_browser(self, **_kwargs):
        self.login_kwargs = dict(_kwargs)
        self.login_browser_ready = bool(getattr(self, "context", None) and getattr(self, "page", None))
        if not self.login_browser_ready:
            raise AssertionError("managed runtime was not attached before password login")
        return super().login_with_password_browser(**_kwargs)


class _FakeManagedManualCookieSlider:
    def __init__(self, *, cookies_result=None):
        self.run_called = False
        self.run_browser_ready = False
        self.run_kwargs = None
        self.last_login_error = ""
        self._cookies_result = cookies_result or {
            "unb": "test_user",
            "_m_h5_tk": "browser_token_12345",
            "cookie2": "browser_cookie2",
        }

    def build_managed_runtime_request(self, **kwargs):
        request = {
            "browser_features": {"user_agent": "unit-test-agent"},
            "launch_options": {},
            "context_options": {},
            "persistent_context_options": {},
            "use_persistent_context": False,
            "initial_cookie_payload": [],
        }
        request.update(kwargs)
        return request

    def _get_slider_failure_message(self, message):
        return message

    def run(self, *args, **kwargs):
        _ = args, kwargs
        self.run_called = True
        self.run_kwargs = dict(kwargs)
        self.run_browser_ready = bool(getattr(self, "context", None) and getattr(self, "page", None))
        if not self.run_browser_ready:
            raise AssertionError("managed runtime was not attached before manual cookie browser flow")
        return True, dict(self._cookies_result)


class _FakePasswordLoginPreflightSuccessLive:
    preflight_calls = 0
    browser_refresh_calls = 0
    reset_calls = 0

    def __init__(
        self,
        cookies_str,
        cookie_id=None,
        user_id=None,
        register_instance=False,
        account_id=None,
        **kwargs,
    ):
        resolved_account_id = account_id or cookie_id
        self.cookies_str = cookies_str
        self.cookie_id = resolved_account_id
        self.account_id = resolved_account_id
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

    @staticmethod
    def mark_manual_refresh_handoff(account_id=None, source='manual_refresh_handoff', ttl=None):
        _ = account_id, source, ttl
        return {"updated": True, "phase": "handoff_recovery"}

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

        self.assertEqual(result["status"], "verification_processing")
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
        fake_lease = SimpleNamespace(account_id="test_user", purpose="password_login")

        def acquire_runtime(_account_id, _purpose, slider_instance):
            slider_instance.browser = object()
            slider_instance.context = object()
            slider_instance.page = object()
            return fake_lease

        with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", return_value=fake_slider), \
             mock.patch("utils.xianyu_slider_stealth.concurrency_manager.unregister_instance", return_value=True), \
             mock.patch.object(reply_server, "_acquire_slider_managed_runtime_sync", side_effect=acquire_runtime), \
             mock.patch.object(reply_server, "_release_slider_managed_runtime_sync"), \
             mock.patch("XianyuAutoAsync.XianyuLive", _FakePasswordLoginPreflightSuccessLive), \
             mock.patch.object(reply_server.db_manager, "get_cookie_details", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_proxy_config", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={"bound_unb": "", "bind_status": "pending_bind"}), \
             mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=True), \
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

    async def test_execute_password_login_acquires_managed_runtime_before_browser_login(self):
        session_id = "password_login_managed_runtime_session"
        self._build_password_login_session(session_id)

        fake_manager = SimpleNamespace(
            cookies={},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
        )
        fake_slider = _FakeManagedPasswordLoginSlider()
        fake_lease = SimpleNamespace(account_id="test_user", purpose="password_login")

        def acquire_runtime(account_id, purpose, slider_instance):
            self.assertEqual(account_id, "test_user")
            self.assertEqual(purpose, "password_login")
            self.assertIs(slider_instance, fake_slider)
            slider_instance.browser = object()
            slider_instance.context = object()
            slider_instance.page = object()
            return fake_lease

        with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", return_value=fake_slider), \
             mock.patch("utils.xianyu_slider_stealth.concurrency_manager.unregister_instance", return_value=True), \
             mock.patch("XianyuAutoAsync.XianyuLive", _FakePasswordLoginPreflightSuccessLive), \
             mock.patch.object(reply_server, "_acquire_slider_managed_runtime_sync", side_effect=acquire_runtime) as acquire_mock, \
             mock.patch.object(reply_server, "_release_slider_managed_runtime_sync") as release_mock, \
             mock.patch.object(reply_server.db_manager, "get_cookie_details", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_proxy_config", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={"bound_unb": "", "bind_status": "pending_bind"}), \
             mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=True), \
             mock.patch.object(reply_server.db_manager, "update_cookie_account_info", return_value=True), \
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

        self.assertTrue(fake_slider.login_browser_ready)
        self.assertTrue(fake_slider.login_kwargs["require_managed_runtime"])
        acquire_mock.assert_called_once()
        release_mock.assert_called_once_with(fake_lease, reason="password_login_handoff_release")

    async def test_execute_password_login_defers_handoff_without_second_browser_refresh_when_token_preflight_fails(self):
        session_id = "password_login_browser_refresh_fallback_session"
        self._build_password_login_session(session_id)
        _FakePasswordLoginFallbackLive.reset_state()

        fake_manager = SimpleNamespace(
            cookies={},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
        )
        fake_slider = _FakePasswordLoginSlider()
        fake_lease = SimpleNamespace(account_id="test_user", purpose="password_login")

        def acquire_runtime(_account_id, _purpose, slider_instance):
            slider_instance.browser = object()
            slider_instance.context = object()
            slider_instance.page = object()
            return fake_lease

        with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", return_value=fake_slider), \
             mock.patch("utils.xianyu_slider_stealth.concurrency_manager.unregister_instance", return_value=True), \
             mock.patch.object(reply_server, "_acquire_slider_managed_runtime_sync", side_effect=acquire_runtime), \
             mock.patch.object(reply_server, "_release_slider_managed_runtime_sync"), \
             mock.patch("XianyuAutoAsync.XianyuLive", _FakePasswordLoginFallbackLive), \
             mock.patch.object(reply_server.db_manager, "get_cookie_details", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_proxy_config", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={"bound_unb": "", "bind_status": "pending_bind"}), \
             mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=True), \
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
        self.assertGreaterEqual(_FakePasswordLoginFallbackLive.preflight_calls, 1)
        self.assertEqual(_FakePasswordLoginFallbackLive.browser_refresh_calls, 0)
        self.assertEqual(_FakePasswordLoginFallbackLive.reset_calls, 0)
        self.assertFalse(session["token_prewarmed"])
        self.assertFalse(session["real_cookie_refreshed"])
        fake_manager.add_cookie.assert_called_once()
        saved_cookie_value = update_cookie_mock.call_args.kwargs["cookie_value"]
        self.assertIn("_m_h5_tk=raw_token_12345", saved_cookie_value)
        self.assertIn("cookie2=raw_cookie2", saved_cookie_value)

    async def test_execute_password_login_does_not_overwrite_old_cookie_when_bound_unb_conflicts(self):
        session_id = "password_login_bound_unb_conflict_session"
        self._build_password_login_session(session_id)

        fake_manager = SimpleNamespace(
            cookies={"test_user": "unb=old_user; cookie2=old_cookie2"},
            add_cookie=mock.Mock(),
            update_cookie=mock.Mock(),
        )
        fake_slider = _FakeManagedPasswordLoginSlider()
        fake_lease = SimpleNamespace(account_id="test_user", purpose="password_login")

        def acquire_runtime(_account_id, _purpose, slider_instance):
            slider_instance.browser = object()
            slider_instance.context = object()
            slider_instance.page = object()
            return fake_lease

        with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", return_value=fake_slider), \
             mock.patch("utils.xianyu_slider_stealth.concurrency_manager.unregister_instance", return_value=True), \
             mock.patch.object(reply_server, "_acquire_slider_managed_runtime_sync", side_effect=acquire_runtime), \
             mock.patch.object(reply_server, "_release_slider_managed_runtime_sync"), \
             mock.patch("XianyuAutoAsync.XianyuLive", _FakePasswordLoginPreflightSuccessLive), \
             mock.patch.object(reply_server.db_manager, "get_cookie_details", return_value={
                 "value": "unb=old_user; cookie2=old_cookie2",
                 "bound_unb": "old_user",
             }), \
             mock.patch.object(reply_server.db_manager, "get_cookie_proxy_config", return_value={}), \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"test_user": "unb=old_user; cookie2=old_cookie2"}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={"bound_unb": "old_user", "bind_status": "active"}), \
             mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=False) as bind_mock, \
             mock.patch.object(reply_server.db_manager, "update_cookie_account_info") as update_cookie_mock, \
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
            await self._wait_for_password_login_status(session_id, "failed")

        self.assertIn("bound_unb", reply_server.password_login_sessions[session_id]["error"])
        bind_mock.assert_called_once()
        update_cookie_mock.assert_not_called()
        fake_manager.add_cookie.assert_not_called()
        fake_manager.update_cookie.assert_not_called()


class XianyuLiveBusinessReadyCookieHandoffTest(unittest.TestCase):
    def test_accepts_qr_cookie_when_only_cna_missing_but_business_fields_ready(self):
        import XianyuAutoAsync

        live = XianyuAutoAsync.XianyuLive.__new__(XianyuAutoAsync.XianyuLive)
        cookies = {
            "unb": "2095002164",
            "sgcookie": "sg",
            "cookie2": "cookie2v",
            "_m_h5_tk": "token_123",
            "_m_h5_tk_enc": "enc_123",
            "t": "t_123",
            "_tb_token_": "tb_123",
        }

        accepted = live._should_accept_business_ready_cookie_handoff(
            cookies,
            missing_required_fields=["cna"],
        )

        self.assertTrue(accepted)

    def test_rejects_qr_cookie_when_missing_more_than_cna(self):
        import XianyuAutoAsync

        live = XianyuAutoAsync.XianyuLive.__new__(XianyuAutoAsync.XianyuLive)
        cookies = {
            "unb": "2095002164",
            "sgcookie": "sg",
            "cookie2": "cookie2v",
            "_m_h5_tk": "token_123",
            "t": "t_123",
            "_tb_token_": "tb_123",
        }

        accepted = live._should_accept_business_ready_cookie_handoff(
            cookies,
            missing_required_fields=["cna", "_m_h5_tk_enc"],
        )

        self.assertFalse(accepted)


if __name__ == "__main__":
    unittest.main()
