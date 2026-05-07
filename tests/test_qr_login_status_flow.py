import asyncio
import sys
import types
import unittest
from collections import defaultdict
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

import reply_server
import utils.qr_login as qr_login


class QRLoginStatusFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_qr_status_requires_complete_cookies_before_api_success(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("qr-incomplete-cookie-session")
        session.params = {"t": "1"}
        manager.sessions[session.session_id] = session

        response = mock.Mock()
        response.json.return_value = {
            "content": {
                "data": {
                    "qrCodeStatus": "CONFIRMED",
                    "iframeRedirect": False,
                }
            }
        }
        response.cookies = {"unb": "only-unb"}

        async def poll_and_stop(_session):
            manager.sessions.pop(session.session_id, None)
            return response

        mark_calls = []
        original_mark = manager._mark_session_success

        def mark_session_success(*args, **kwargs):
            mark_calls.append(kwargs.get("require_complete_cookies", False))
            return original_mark(*args, **kwargs)

        with mock.patch.object(manager, "_poll_qrcode_status", new=mock.AsyncMock(side_effect=poll_and_stop)), \
             mock.patch.object(manager, "_mark_session_success", side_effect=mark_session_success), \
             mock.patch.object(qr_login.asyncio, "sleep", new=mock.AsyncMock()):
            await manager._monitor_qr_status(session.session_id)

        self.assertEqual(mark_calls, [True])
        self.assertNotEqual(session.status, "success")

    async def test_launch_verification_page_marks_session_failed_when_browser_task_crashes(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("qr-verification-crash-session")
        session.status = "verification_required"
        session.verification_url = "https://passport.goofish.com/iv/test"
        manager.sessions[session.session_id] = session

        with mock.patch.object(
            manager,
            "_launch_verification_browser_context",
            new=mock.AsyncMock(side_effect=RuntimeError("verification browser exploded")),
        ):
            await manager._launch_verification_page(session.session_id)

        self.assertEqual(session.status, "failed")
        self.assertIn("verification browser exploded", session.error_message)

    def test_get_session_status_exposes_phase_verification_type_and_handoff_state(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("qr-session-status-fields")
        session.status = "verification_required"
        session.phase = "verification_pending"
        session.verification_type = "face_verify"
        session.verification_url = "https://passport.goofish.com/iv/test"
        session.screenshot_path = "face_verify.png"
        session.handoff_status = "pending"
        session.error_message = ""
        manager.sessions[session.session_id] = session

        result = manager.get_session_status(session.session_id)

        self.assertEqual(result["status"], "verification_required")
        self.assertEqual(result["phase"], "verification_pending")
        self.assertIn(result["verification_type"], {"face_verify", "人脸验证"})
        self.assertEqual(result["handoff_status"], "pending")
        self.assertEqual(result["screenshot_path"], "face_verify.png")


class ReplyServerQrLoginStatusFlowTest(unittest.TestCase):
    def setUp(self):
        self._original_processed = reply_server.qr_check_processed
        self._original_locks = reply_server.qr_check_locks
        self._original_manager = reply_server.qr_login_manager
        reply_server.qr_check_processed = {}
        reply_server.qr_check_locks = defaultdict(lambda: asyncio.Lock())

    def tearDown(self):
        reply_server.qr_check_processed = self._original_processed
        reply_server.qr_check_locks = self._original_locks
        reply_server.qr_login_manager = self._original_manager

    def test_check_qr_code_status_syncs_handoff_processing_and_success_back_to_session_manager(self):
        managed_runtime = object()
        managed_context = object()
        managed_page = object()
        process_mock = mock.AsyncMock(return_value={"account_id": "qr_account"})

        class _FakeQRManager:
            def __init__(self):
                self.handoff_updates = []

            def cleanup_expired_sessions(self):
                return None

            def get_session_status(self, session_id):
                return {
                    "status": "success",
                    "session_id": session_id,
                    "phase": "browser_cookie_ready",
                    "handoff_status": "pending",
                }

            def get_session_cookies(self, _session_id):
                return {
                    "cookies": "unb=test_user; cookie2=test_cookie2",
                    "unb": "test_user",
                    "managed_runtime": managed_runtime,
                    "managed_context": managed_context,
                    "managed_page": managed_page,
                }

            def update_session_handoff_status(self, session_id, status, **kwargs):
                self.handoff_updates.append((session_id, status, kwargs))

        fake_manager = _FakeQRManager()
        reply_server.qr_login_manager = fake_manager

        async def invoke():
            result = await reply_server.check_qr_code_status(
                "qr-session-handoff-sync",
                current_user={"user_id": 1, "username": "admin"},
            )
            await asyncio.sleep(0)
            return result

        with mock.patch.object(reply_server, "cleanup_qr_check_records"), \
             mock.patch.object(reply_server, "process_qr_login_cookies", process_mock), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(fake_manager.handoff_updates[0][1], "processing")
        self.assertEqual(fake_manager.handoff_updates[-1][1], "success")
        process_mock.assert_awaited_once_with(
            "unb=test_user; cookie2=test_cookie2",
            "test_user",
            {"user_id": 1, "username": "admin"},
            managed_runtime=managed_runtime,
            managed_context=managed_context,
            managed_page=managed_page,
        )

    def test_check_qr_code_status_locked_branch_includes_session_state_fields(self):
        session_id = "qr-session-locked"

        class _FakeQRManager:
            def cleanup_expired_sessions(self):
                return None

            def get_session_status(self, sid):
                return {
                    "status": "confirmed",
                    "session_id": sid,
                    "phase": "handoff_processing",
                    "handoff_status": "processing",
                    "handoff_error": None,
                    "verification_type": "face_verify",
                    "verification_type_label": "人脸验证",
                    "success_stage": "confirmed_pending_cookies",
                    "browser_alive": True,
                }

        class _AlwaysLocked:
            def locked(self):
                return True

        reply_server.qr_login_manager = _FakeQRManager()
        reply_server.qr_check_locks = defaultdict(_AlwaysLocked)

        async def invoke():
            return await reply_server.check_qr_code_status(
                session_id,
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "cleanup_qr_check_records"), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertEqual(result["status"], "processing")
        self.assertEqual(result["phase"], "handoff_processing")
        self.assertEqual(result["handoff_status"], "processing")
        self.assertEqual(result["verification_type"], "face_verify")
        self.assertEqual(result["verification_type_label"], "人脸验证")
        self.assertEqual(result["success_stage"], "confirmed_pending_cookies")
        self.assertTrue(result["browser_alive"])

    def test_check_qr_code_status_after_lock_processed_branch_keeps_state_fields(self):
        session_id = "qr-session-processed-after-lock"
        reply_server.qr_check_processed[session_id] = {
            "processed": False,
            "processing": True,
            "timestamp": 0,
        }

        class _FakeQRManager:
            def cleanup_expired_sessions(self):
                return None

            def get_session_status(self, sid):
                return {
                    "status": "success",
                    "session_id": sid,
                    "phase": "handoff_completed",
                    "handoff_status": "success",
                    "handoff_error": None,
                    "verification_type": "face_verify",
                    "verification_type_label": "人脸验证",
                    "success_stage": "browser_complete",
                    "browser_alive": False,
                }

        class _FlipProcessedLock:
            def __init__(self, sid):
                self._session_id = sid

            def locked(self):
                return False

            async def __aenter__(self):
                reply_server.qr_check_processed[self._session_id].update({
                    "processed": True,
                    "processing": False,
                    "account_info": {"account_id": "qr_account_after_lock"},
                })
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        reply_server.qr_login_manager = _FakeQRManager()
        reply_server.qr_check_locks = defaultdict(lambda: _FlipProcessedLock(session_id))

        async def invoke():
            return await reply_server.check_qr_code_status(
                session_id,
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "cleanup_qr_check_records"), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["account_info"]["account_id"], "qr_account_after_lock")
        self.assertEqual(result["phase"], "handoff_completed")
        self.assertEqual(result["handoff_status"], "success")
        self.assertEqual(result["verification_type"], "face_verify")
        self.assertEqual(result["verification_type_label"], "人脸验证")
        self.assertEqual(result["success_stage"], "browser_complete")
        self.assertFalse(result["browser_alive"])
        self.assertTrue(result["already_processed"])


if __name__ == "__main__":
    unittest.main()
