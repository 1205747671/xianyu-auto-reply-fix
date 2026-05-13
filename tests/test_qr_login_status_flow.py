import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest
from collections import defaultdict
from unittest import mock

_SNAPSHOT_MODULE_NAMES = (
    "utils.image_utils",
    "loguru",
    "cloakbrowser",
    "qrcode",
    "qrcode.constants",
)
_IMPORTED_MODULE_NAMES = (
    "reply_server",
    "utils.qr_login",
)
_MODULE_SNAPSHOT = {
    name: sys.modules.get(name)
    for name in _SNAPSHOT_MODULE_NAMES + _IMPORTED_MODULE_NAMES
}


def _restore_module_snapshot():
    for name in _IMPORTED_MODULE_NAMES + _SNAPSHOT_MODULE_NAMES:
        original = _MODULE_SNAPSHOT.get(name)
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


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

browser_provider_stub = sys.modules.get("utils.browser_provider")
if browser_provider_stub is None or getattr(browser_provider_stub, "__file__", None) is None:
    sys.modules.pop("utils.browser_provider", None)
    utils_package = sys.modules.get("utils")
    if utils_package is not None and hasattr(utils_package, "browser_provider"):
        delattr(utils_package, "browser_provider")
    browser_provider_stub = importlib.import_module("utils.browser_provider")

browser_provider_stub.BrowserContextLike = getattr(browser_provider_stub, "BrowserContextLike", object)
browser_provider_stub.BrowserLike = getattr(browser_provider_stub, "BrowserLike", object)
browser_provider_stub.PageLike = getattr(browser_provider_stub, "PageLike", object)

def _sync_browser_provider_noop(*_args, **_kwargs):
    return None

async def _async_browser_provider_noop(*_args, **_kwargs):
    return None

for attr_name in (
    "launch_browser",
    "launch_browser_context",
    "launch_browser_persistent_context",
):
    if not hasattr(browser_provider_stub, attr_name):
        setattr(browser_provider_stub, attr_name, _sync_browser_provider_noop)

for attr_name in (
    "launch_browser_async",
    "launch_browser_context_async",
    "launch_browser_persistent_context_async",
):
    if not hasattr(browser_provider_stub, attr_name):
        setattr(browser_provider_stub, attr_name, _async_browser_provider_noop)

_purge_stubbed_module("XianyuAutoAsync")
_purge_stubbed_module("reply_server")
_purge_stubbed_module("db_manager")
sys.modules.pop("utils.qr_login", None)
utils_package = sys.modules.get("utils")
if utils_package is not None and hasattr(utils_package, "qr_login"):
    delattr(utils_package, "qr_login")
import reply_server
import utils.qr_login as qr_login

# Restore import-time stubs immediately so unittest discovery does not leak
# partially stubbed project modules into later test imports.
_restore_module_snapshot()


def tearDownModule():
    _restore_module_snapshot()


class QRLoginStatusFlowTest(unittest.IsolatedAsyncioTestCase):
    def test_qr_login_session_profile_is_account_id_scoped(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession(
            "qr-account-profile-session",
            user_id=1,
            account_id="qr_account_42",
        )
        session.unb = "unb_value_should_not_drive_profile"
        session.proxy_account_id = "legacy_proxy_match_should_not_drive_profile"

        profile_dir = manager._resolve_verification_profile_dir(session)

        self.assertTrue(profile_dir.endswith(r"browser_data\user_qr_account_42") or profile_dir.endswith("browser_data/user_qr_account_42"))

    def test_qr_login_session_profile_rejects_missing_account_id(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("qr-legacy-profile-session")
        session.unb = "unb_must_not_drive_profile"
        session.proxy_account_id = "legacy_proxy_must_not_drive_profile"

        with self.assertRaisesRegex(ValueError, "account_id"):
            manager._resolve_verification_profile_dir(session)

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

    async def test_launch_verification_page_releases_runtime_lease_when_navigation_fails(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("qr-verification-release-on-failure")
        session.status = "verification_required"
        session.verification_url = "https://passport.goofish.com/iv/test"
        manager.sessions[session.session_id] = session

        runtime_lease = types.SimpleNamespace(pages=[], released=False)
        browser = mock.Mock(close=mock.AsyncMock())
        context = mock.Mock(add_cookies=mock.AsyncMock(), close=mock.AsyncMock())
        page = mock.Mock(
            goto=mock.AsyncMock(side_effect=[None, RuntimeError("verification navigation exploded")]),
            wait_for_timeout=mock.AsyncMock(),
            screenshot=mock.AsyncMock(return_value=b""),
            close=mock.AsyncMock(),
            url="https://passport.goofish.com/iv/test",
        )
        release_runtime = mock.AsyncMock()

        with mock.patch.object(
            manager,
            "_launch_verification_browser_context",
            new=mock.AsyncMock(return_value=(runtime_lease, browser, context, False)),
        ), mock.patch.object(
            manager,
            "_get_or_create_context_page",
            new=mock.AsyncMock(return_value=page),
        ), mock.patch.object(
            qr_login.account_browser_runtime_manager,
            "release_runtime",
            release_runtime,
        ):
            await manager._launch_verification_page(session.session_id)

        self.assertEqual(session.status, "failed")
        self.assertIn("verification navigation exploded", session.error_message)
        release_runtime.assert_awaited_once_with(
            runtime_lease,
            reason="qr_login_verification_page_closed",
        )
        page.close.assert_not_awaited()
        context.close.assert_not_awaited()
        browser.close.assert_not_awaited()

    async def test_launch_verification_page_releases_runtime_lease_when_cancelled(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("qr-verification-release-on-cancel")
        session.status = "verification_required"
        session.verification_url = "https://passport.goofish.com/iv/test"
        manager.sessions[session.session_id] = session

        runtime_lease = types.SimpleNamespace(pages=[], released=False)
        browser = mock.Mock(close=mock.AsyncMock())
        context = mock.Mock(add_cookies=mock.AsyncMock(), close=mock.AsyncMock())
        page = mock.Mock(
            goto=mock.AsyncMock(side_effect=[None, None]),
            wait_for_timeout=mock.AsyncMock(),
            screenshot=mock.AsyncMock(return_value=b""),
            close=mock.AsyncMock(),
            url="https://passport.goofish.com/iv/test",
        )
        release_runtime = mock.AsyncMock()

        with mock.patch.object(
            manager,
            "_launch_verification_browser_context",
            new=mock.AsyncMock(return_value=(runtime_lease, browser, context, False)),
        ), mock.patch.object(
            manager,
            "_get_or_create_context_page",
            new=mock.AsyncMock(return_value=page),
        ), mock.patch.object(
            manager,
            "_probe_browser_login_success",
            new=mock.AsyncMock(side_effect=asyncio.CancelledError()),
        ), mock.patch.object(
            qr_login.account_browser_runtime_manager,
            "release_runtime",
            release_runtime,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await manager._launch_verification_page(session.session_id)

        release_runtime.assert_awaited_once_with(
            runtime_lease,
            reason="qr_login_verification_page_closed",
        )
        page.close.assert_not_awaited()
        context.close.assert_not_awaited()
        browser.close.assert_not_awaited()

    async def test_launch_verification_page_untracks_closed_page_when_success_handoff_keeps_other_page(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("qr-verification-success-handoff-page-switch")
        session.status = "verification_required"
        session.verification_url = "https://passport.goofish.com/iv/test"
        manager.sessions[session.session_id] = session

        runtime_lease = types.SimpleNamespace(pages=[], released=False)
        browser = mock.Mock(close=mock.AsyncMock())
        context = mock.Mock(add_cookies=mock.AsyncMock(), close=mock.AsyncMock())
        page = mock.Mock(
            goto=mock.AsyncMock(side_effect=[None, None]),
            wait_for_timeout=mock.AsyncMock(),
            screenshot=mock.AsyncMock(return_value=b""),
            close=mock.AsyncMock(),
            url="https://passport.goofish.com/iv/test",
        )
        managed_page = mock.Mock()
        release_runtime = mock.AsyncMock()

        async def mark_success(_session, *_args, **_kwargs):
            _session.status = "success"
            _session.managed_runtime_lease = runtime_lease
            _session.managed_runtime = browser
            _session.managed_context = context
            _session.managed_page = managed_page
            manager._track_runtime_lease_page(runtime_lease, managed_page)
            return True

        with mock.patch.object(
            manager,
            "_launch_verification_browser_context",
            new=mock.AsyncMock(return_value=(runtime_lease, browser, context, False)),
        ), mock.patch.object(
            manager,
            "_get_or_create_context_page",
            new=mock.AsyncMock(return_value=page),
        ), mock.patch.object(
            manager,
            "_probe_browser_login_success",
            new=mock.AsyncMock(side_effect=mark_success),
        ), mock.patch.object(
            qr_login.account_browser_runtime_manager,
            "release_runtime",
            release_runtime,
        ):
            await manager._launch_verification_page(session.session_id)

        self.assertNotIn(page, runtime_lease.pages)
        self.assertIn(managed_page, runtime_lease.pages)
        release_runtime.assert_not_awaited()
        page.close.assert_awaited_once_with()

    def test_get_session_status_exposes_phase_verification_type_and_handoff_state(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("qr-session-status-fields")
        session.user_id = 7
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
        self.assertEqual(result["user_id"], 7)
        self.assertEqual(result["phase"], "verification_pending")
        self.assertIn(result["verification_type"], {"face_verify", "人脸验证"})
        self.assertEqual(result["handoff_status"], "pending")
        self.assertEqual(result["screenshot_path"], "face_verify.png")

    def test_qr_login_session_to_dict_includes_user_and_account_identity(self):
        session = qr_login.QRLoginSession(
            "qr-session-dict-fields",
            user_id=9,
            account_id="qr_account_9",
        )

        result = session.to_dict()

        self.assertEqual(result["session_id"], "qr-session-dict-fields")
        self.assertEqual(result["user_id"], 9)
        self.assertEqual(result["account_id"], "qr_account_9")

    def test_update_session_handoff_status_allows_retryable_failure_without_terminal_session_status(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession(
            "qr-session-retryable-handoff",
            user_id=3,
            account_id="qr_account_retryable",
        )
        session.status = "success"
        session.phase = "browser_cookie_ready"
        manager.sessions[session.session_id] = session

        manager.update_session_handoff_status(
            session.session_id,
            "failed",
            error="cookie not ready yet",
            terminal=False,
        )

        self.assertEqual(session.status, "success")
        self.assertEqual(session.phase, "handoff_failed")
        self.assertEqual(session.handoff_status, "failed")
        self.assertEqual(session.handoff_error, "cookie not ready yet")
        self.assertIsNone(session.error_message)


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
        managed_runtime_lease = object()
        managed_runtime = object()
        managed_context = object()
        managed_page = object()
        process_mock = mock.AsyncMock(return_value={"account_id": "qr_account"})

        class _FakeQRManager:
            def __init__(self):
                self.handoff_updates = []
                self.release_calls = []

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
                    "account_id": "qr_account",
                    "cookies": "unb=test_user; cookie2=test_cookie2",
                    "unb": "test_user",
                    "managed_runtime_lease": managed_runtime_lease,
                    "managed_runtime": managed_runtime,
                    "managed_context": managed_context,
                    "managed_page": managed_page,
                }

            def update_session_handoff_status(self, session_id, status, **kwargs):
                self.handoff_updates.append((session_id, status, kwargs))

            def release_session_assets(self, session_id, *, reason):
                self.release_calls.append((session_id, reason))

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
            "qr_account",
            "unb=test_user; cookie2=test_cookie2",
            "test_user",
            {"user_id": 1, "username": "admin"},
            managed_runtime_lease=managed_runtime_lease,
            managed_runtime=managed_runtime,
            managed_context=managed_context,
            managed_page=managed_page,
        )
        self.assertEqual(
            fake_manager.release_calls,
            [("qr-session-handoff-sync", "qr_login_handoff_completed")],
        )

    def test_check_qr_code_status_marks_handoff_failed_when_cookie_processing_does_not_restart_task(self):
        session_id = "qr-session-handoff-task-not-restarted"
        warning_message = "真实Cookie已获取，但账号任务未启动"
        process_mock = mock.AsyncMock(return_value={
            "account_id": "qr_account",
            "task_restarted": False,
            "warning_message": warning_message,
        })

        class _FakeQRManager:
            def __init__(self):
                self.handoff_updates = []
                self.release_calls = []

            def cleanup_expired_sessions(self):
                return None

            def get_session_status(self, current_session_id):
                return {
                    "status": "success",
                    "session_id": current_session_id,
                    "user_id": 1,
                    "account_id": "qr_account",
                    "phase": "browser_cookie_ready",
                    "handoff_status": "pending",
                }

            def get_session_cookies(self, _session_id):
                return {
                    "account_id": "qr_account",
                    "cookies": "unb=test_user; cookie2=test_cookie2",
                    "unb": "test_user",
                }

            def update_session_handoff_status(self, current_session_id, status, **kwargs):
                self.handoff_updates.append((current_session_id, status, kwargs))

            def release_session_assets(self, current_session_id, *, reason):
                self.release_calls.append((current_session_id, reason))

        fake_manager = _FakeQRManager()
        reply_server.qr_login_manager = fake_manager

        async def invoke():
            result = await reply_server.check_qr_code_status(
                session_id,
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
        self.assertEqual(
            fake_manager.handoff_updates[-1],
            (session_id, "failed", {"error": warning_message}),
        )
        self.assertEqual(
            reply_server.qr_check_processed[session_id],
            {
                "processed": True,
                "processing": False,
                "timestamp": reply_server.qr_check_processed[session_id]["timestamp"],
                "error": warning_message,
            },
        )
        self.assertEqual(
            fake_manager.release_calls,
            [(session_id, "qr_login_handoff_completed")],
        )

    def test_check_qr_code_status_keeps_handoff_success_when_cookie_processing_returns_nonfatal_warning(self):
        session_id = "qr-session-handoff-nonfatal-warning"
        warning_message = "真实Cookie已获取，账号任务已切换；首次Token将在后台继续初始化"
        process_mock = mock.AsyncMock(return_value={
            "account_id": "qr_account",
            "task_restarted": True,
            "real_cookie_refreshed": True,
            "warning_message": warning_message,
        })

        class _FakeQRManager:
            def __init__(self):
                self.handoff_updates = []
                self.release_calls = []

            def cleanup_expired_sessions(self):
                return None

            def get_session_status(self, current_session_id):
                return {
                    "status": "success",
                    "session_id": current_session_id,
                    "user_id": 1,
                    "account_id": "qr_account",
                    "phase": "browser_cookie_ready",
                    "handoff_status": "pending",
                }

            def get_session_cookies(self, _session_id):
                return {
                    "account_id": "qr_account",
                    "cookies": "unb=test_user; cookie2=test_cookie2",
                    "unb": "test_user",
                }

            def update_session_handoff_status(self, current_session_id, status, **kwargs):
                self.handoff_updates.append((current_session_id, status, kwargs))

            def release_session_assets(self, current_session_id, *, reason):
                self.release_calls.append((current_session_id, reason))

        fake_manager = _FakeQRManager()
        reply_server.qr_login_manager = fake_manager

        async def invoke():
            result = await reply_server.check_qr_code_status(
                session_id,
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
        self.assertEqual(
            fake_manager.handoff_updates[-1][2]["account_info"]["warning_message"],
            warning_message,
        )
        self.assertEqual(
            reply_server.qr_check_processed[session_id]["account_info"]["warning_message"],
            warning_message,
        )
        self.assertEqual(
            fake_manager.release_calls,
            [(session_id, "qr_login_handoff_completed")],
        )

    def test_check_qr_code_status_processing_response_clears_stale_handoff_error(self):
        session_id = "qr-session-processing-clears-stale-error"
        process_mock = mock.AsyncMock(return_value={"account_id": "qr_account"})

        class _FakeQRManager:
            def __init__(self):
                self.handoff_updates = []
                self.status_info = {
                    "status": "success",
                    "session_id": session_id,
                    "user_id": 1,
                    "account_id": "qr_account",
                    "phase": "browser_cookie_ready",
                    "handoff_status": "failed",
                    "handoff_error": "old handoff error",
                }

            def cleanup_expired_sessions(self):
                return None

            def get_session_status(self, current_session_id):
                current = dict(self.status_info)
                current["session_id"] = current_session_id
                return current

            def get_session_cookies(self, _session_id):
                return {
                    "account_id": "qr_account",
                    "cookies": "unb=test_user; cookie2=test_cookie2",
                    "unb": "test_user",
                }

            def update_session_handoff_status(self, current_session_id, status, **kwargs):
                self.handoff_updates.append((current_session_id, status, kwargs))
                if status == "processing":
                    self.status_info["handoff_status"] = "processing"
                    self.status_info["handoff_error"] = None
                    self.status_info["phase"] = "handoff_processing"

        fake_manager = _FakeQRManager()
        reply_server.qr_login_manager = fake_manager

        async def invoke():
            return await reply_server.check_qr_code_status(
                session_id,
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "cleanup_qr_check_records"), \
             mock.patch.object(reply_server, "process_qr_login_cookies", process_mock), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["phase"], "handoff_processing")
        self.assertEqual(result["handoff_status"], "processing")
        self.assertIsNone(result["handoff_error"])

    def test_check_qr_code_status_processed_success_overrides_stale_handoff_state(self):
        session_id = "qr-session-processed-success-stale-handoff"
        reply_server.qr_check_processed[session_id] = {
            "processed": True,
            "processing": False,
            "timestamp": 0,
            "account_info": {"account_id": "qr_account"},
        }

        class _FakeQRManager:
            def cleanup_expired_sessions(self):
                return None

            def get_session_status(self, current_session_id):
                return {
                    "status": "success",
                    "session_id": current_session_id,
                    "user_id": 1,
                    "account_id": "qr_account",
                    "phase": "handoff_processing",
                    "handoff_status": "processing",
                    "handoff_error": "stale handoff error",
                }

        reply_server.qr_login_manager = _FakeQRManager()

        async def invoke():
            return await reply_server.check_qr_code_status(
                session_id,
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "cleanup_qr_check_records"), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["phase"], "handoff_completed")
        self.assertEqual(result["handoff_status"], "success")
        self.assertIsNone(result["handoff_error"])
        self.assertEqual(result["account_info"]["account_id"], "qr_account")
        self.assertTrue(result["already_processed"])

    def test_check_qr_code_status_rejects_cross_user_session_with_403(self):
        session_id = "qr-session-owned-by-other-user"

        class _FakeQRManager:
            def cleanup_expired_sessions(self):
                return None

            def get_session_status(self, sid):
                return {
                    "status": "waiting",
                    "session_id": sid,
                    "user_id": 2,
                    "account_id": "qr_account_other",
                }

        reply_server.qr_login_manager = _FakeQRManager()

        async def invoke():
            return await reply_server.check_qr_code_status(
                session_id,
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "cleanup_qr_check_records"), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 403)

    def test_check_qr_code_status_uses_status_account_id_when_cookie_payload_omits_it(self):
        process_mock = mock.AsyncMock(return_value={"account_id": "qr_account_from_status"})

        class _FakeQRManager:
            def __init__(self):
                self.handoff_updates = []

            def cleanup_expired_sessions(self):
                return None

            def get_session_status(self, session_id):
                return {
                    "status": "success",
                    "session_id": session_id,
                    "user_id": 1,
                    "account_id": "qr_account_from_status",
                    "phase": "browser_cookie_ready",
                    "handoff_status": "pending",
                }

            def get_session_cookies(self, _session_id):
                return {
                    "cookies": "unb=test_user; cookie2=test_cookie2",
                    "unb": "test_user",
                }

            def update_session_handoff_status(self, session_id, status, **kwargs):
                self.handoff_updates.append((session_id, status, kwargs))

        fake_manager = _FakeQRManager()
        reply_server.qr_login_manager = fake_manager

        async def invoke():
            result = await reply_server.check_qr_code_status(
                "qr-session-account-id-from-status",
                current_user={"user_id": 1, "username": "admin"},
            )
            await asyncio.sleep(0)
            return result

        with mock.patch.object(reply_server, "cleanup_qr_check_records"), \
             mock.patch.object(reply_server, "process_qr_login_cookies", process_mock), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertEqual(result["status"], "confirmed")
        process_mock.assert_awaited_once_with(
            "qr_account_from_status",
            "unb=test_user; cookie2=test_cookie2",
            "test_user",
            {"user_id": 1, "username": "admin"},
            managed_runtime_lease=None,
            managed_runtime=None,
            managed_context=None,
            managed_page=None,
        )

    def test_check_qr_code_status_marks_retryable_failure_when_success_session_has_no_cookies(self):
        session_id = "qr-session-success-without-cookies"

        class _FakeQRManager:
            def __init__(self):
                self.handoff_updates = []

            def cleanup_expired_sessions(self):
                return None

            def get_session_status(self, sid):
                return {
                    "status": "success",
                    "session_id": sid,
                    "user_id": 1,
                    "account_id": "qr_account",
                    "phase": "browser_cookie_ready",
                    "handoff_status": "pending",
                    "verification_type": "face_verify",
                    "verification_type_label": "浜鸿劯楠岃瘉",
                    "success_stage": "browser_complete",
                    "browser_alive": False,
                }

            def get_session_cookies(self, _session_id):
                return None

            def update_session_handoff_status(self, session_id, status, **kwargs):
                self.handoff_updates.append((session_id, status, kwargs))

        fake_manager = _FakeQRManager()
        reply_server.qr_login_manager = fake_manager

        async def invoke():
            return await reply_server.check_qr_code_status(
                session_id,
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "cleanup_qr_check_records"), \
             mock.patch.object(reply_server, "log_with_user"), \
             mock.patch.object(reply_server, "process_qr_login_cookies", new=mock.AsyncMock()) as process_mock:
            result = asyncio.run(invoke())

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["handoff_status"], "failed")
        self.assertEqual(result["phase"], "browser_cookie_ready")
        self.assertEqual(result["verification_type"], "face_verify")
        self.assertEqual(result["verification_type_label"], "浜鸿劯楠岃瘉")
        self.assertEqual(result["success_stage"], "browser_complete")
        self.assertFalse(result["browser_alive"])
        self.assertIn("Cookie", result["message"])
        self.assertIn("重试", result["message"])
        self.assertEqual(fake_manager.handoff_updates, [
            (session_id, "processing", {}),
            (session_id, "failed", {"error": result["message"], "terminal": False}),
        ])
        self.assertEqual(reply_server.qr_check_processed[session_id]["processed"], False)
        self.assertEqual(reply_server.qr_check_processed[session_id]["processing"], False)
        self.assertNotIn("account_info", reply_server.qr_check_processed[session_id])
        process_mock.assert_not_awaited()

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

        self.assertEqual(result["status"], "confirmed")
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

    def test_generate_qr_code_requires_explicit_account_id(self):
        async def invoke():
            return await reply_server.generate_qr_code(
                {},
                current_user={"user_id": 1, "username": "admin"},
            )

        with self.assertRaises(reply_server.HTTPException) as raised:
            asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 400)

    def test_generate_qr_code_rejects_default_account_id_before_placeholder_creation(self):
        fake_db = mock.Mock()
        fake_manager = mock.Mock()

        async def invoke():
            return await reply_server.generate_qr_code(
                {"account_id": "default"},
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "qr_login_manager", fake_manager), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("non-empty, non-default account_id", raised.exception.detail)
        fake_db.get_cookie_binding_info.assert_not_called()
        fake_db.create_cookie_account_placeholder.assert_not_called()
        fake_manager.generate_qr_code.assert_not_called()

    def test_generate_qr_code_creates_pending_placeholder_for_new_account(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_binding_info.return_value = None
        fake_db.assert_cookie_belongs_to_user = mock.Mock()
        fake_db.create_cookie_account_placeholder.return_value = True
        fake_manager = mock.Mock()
        fake_manager.generate_qr_code = mock.AsyncMock(return_value={
            "success": True,
            "session_id": "qr-session",
            "qr_code_url": "data:image/png;base64,unit",
        })

        async def invoke():
            return await reply_server.generate_qr_code(
                {"account_id": "qr_account"},
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "qr_login_manager", fake_manager), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertTrue(result["success"])
        fake_db.create_cookie_account_placeholder.assert_called_once_with(
            "qr_account",
            1,
            bind_status="pending_bind",
        )
        fake_db.assert_cookie_belongs_to_user.assert_not_called()
        fake_manager.generate_qr_code.assert_awaited_once_with(
            account_id="qr_account",
            user_id=1,
        )


    def test_generate_qr_code_validates_existing_account_owner(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_binding_info.return_value = {
            "account_id": "qr_account",
            "user_id": 1,
            "bound_unb": "existing_unb",
            "bind_status": "active",
        }
        fake_db.assert_cookie_belongs_to_user = mock.Mock(return_value=True)
        fake_manager = mock.Mock()
        fake_manager.generate_qr_code = mock.AsyncMock(return_value={
            "success": True,
            "session_id": "qr-session-existing",
            "qr_code_url": "data:image/png;base64,unit",
        })

        async def invoke():
            return await reply_server.generate_qr_code(
                {"account_id": "qr_account"},
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "qr_login_manager", fake_manager), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertTrue(result["success"])
        fake_db.assert_cookie_belongs_to_user.assert_called_once_with("qr_account", 1)
        fake_db.create_cookie_account_placeholder.assert_not_called()
        fake_manager.generate_qr_code.assert_awaited_once_with(
            account_id="qr_account",
            user_id=1,
        )

    def test_refresh_cookies_from_qr_login_rejects_missing_request_fields_with_400(self):
        async def invoke_missing_qr_cookies():
            return await reply_server.refresh_cookies_from_qr_login(
                {"account_id": "qr_account"},
                current_user={"user_id": 1, "username": "admin"},
            )

        with self.assertRaises(reply_server.HTTPException) as raised:
            asyncio.run(invoke_missing_qr_cookies())

        self.assertEqual(raised.exception.status_code, 400)

        async def invoke_missing_account_id():
            return await reply_server.refresh_cookies_from_qr_login(
                {"qr_cookies": "unb=test_user; cookie2=qr_cookie"},
                current_user={"user_id": 1, "username": "admin"},
            )

        with self.assertRaises(reply_server.HTTPException) as raised:
            asyncio.run(invoke_missing_account_id())

        self.assertEqual(raised.exception.status_code, 400)

    def test_refresh_cookies_from_qr_login_rejects_default_account_id_before_account_lookup(self):
        class _FailIfConstructed:
            def __init__(self, *args, **kwargs):
                raise AssertionError("default account_id must be rejected before XianyuLive")

        async def invoke():
            return await reply_server.refresh_cookies_from_qr_login(
                {
                    "qr_cookies": "unb=test_user; cookie2=qr_cookie",
                    "account_id": "default",
                },
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(
            reply_server.db_manager,
            "get_all_cookies",
            side_effect=AssertionError("default account_id should be rejected before account access lookup"),
        ), mock.patch(
            "XianyuAutoAsync.XianyuLive",
            _FailIfConstructed,
        ), mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("non-empty, non-default account_id", raised.exception.detail)

    def test_refresh_cookies_from_qr_login_rejects_cross_user_cookie_with_403(self):
        class _FailIfConstructed:
            def __init__(self, *args, **kwargs):
                raise AssertionError("cross-user refresh must stop before XianyuLive")

        async def invoke():
            return await reply_server.refresh_cookies_from_qr_login(
                {
                    "qr_cookies": "unb=test_user; cookie2=qr_cookie",
                    "account_id": "other_user_account",
                },
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"owned_account": "old"}), \
             mock.patch("XianyuAutoAsync.XianyuLive", _FailIfConstructed), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 403)

    def test_refresh_cookies_from_qr_login_rejects_bound_unb_conflict_without_manager_update(self):
        fake_instance = mock.Mock()
        fake_instance.refresh_cookies_from_qr_login = mock.AsyncMock(return_value=True)
        fake_manager = mock.Mock()

        async def invoke():
            return await reply_server.refresh_cookies_from_qr_login(
                {
                    "qr_cookies": "unb=old_user; cookie2=qr_cookie",
                    "account_id": "qr_account",
                },
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"qr_account": "unb=old_user; cookie2=old"}), \
             mock.patch.object(reply_server.db_manager, "add_risk_control_log", return_value=123), \
             mock.patch.object(reply_server.db_manager, "update_risk_control_log"), \
             mock.patch.object(reply_server.db_manager, "get_cookie_by_id", return_value={"cookies_str": "unb=other_user; cookie2=real"}), \
             mock.patch.object(reply_server.db_manager, "update_cookie_account_info") as rollback_mock, \
             mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={
                 "account_id": "qr_account",
                 "user_id": 1,
                 "bound_unb": "old_user",
                 "bind_status": "active",
             }), \
             mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=False) as bind_mock, \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch("XianyuAutoAsync.XianyuLive", return_value=fake_instance), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 409)
        bind_mock.assert_called_once_with("qr_account", "other_user", user_id=1)
        rollback_mock.assert_called_once_with("qr_account", cookie_value="unb=old_user; cookie2=old")
        fake_manager.update_cookie.assert_not_called()

    def test_refresh_cookies_from_qr_login_passes_account_id_to_live_instance(self):
        fake_instance = mock.Mock()
        fake_instance.refresh_cookies_from_qr_login = mock.AsyncMock(return_value=False)

        async def invoke():
            return await reply_server.refresh_cookies_from_qr_login(
                {
                    "qr_cookies": "unb=test_user; cookie2=qr_cookie",
                    "account_id": "qr_account",
                },
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"qr_account": "old"}), \
             mock.patch.object(reply_server.db_manager, "add_risk_control_log", return_value=123), \
             mock.patch.object(reply_server.db_manager, "update_risk_control_log"), \
             mock.patch("XianyuAutoAsync.XianyuLive", return_value=fake_instance), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertFalse(result["success"])
        fake_instance.refresh_cookies_from_qr_login.assert_awaited_once()
        refresh_kwargs = fake_instance.refresh_cookies_from_qr_login.await_args.kwargs
        self.assertEqual(refresh_kwargs["qr_cookies_str"], "unb=test_user; cookie2=qr_cookie")
        self.assertEqual(refresh_kwargs["account_id"], "qr_account")
        self.assertEqual(refresh_kwargs["user_id"], 1)
        self.assertNotIn("cookie_id", refresh_kwargs)

    def test_process_qr_login_cookies_rolls_back_real_cookie_on_real_unb_conflict(self):
        class _FakeLive:
            def __init__(self, *args, **kwargs):
                pass

            async def refresh_cookies_from_qr_login(self, *args, **kwargs):
                return True

        async def invoke():
            return await reply_server.process_qr_login_cookies(
                "qr_account",
                "unb=old_user; cookie2=qr_cookie",
                "old_user",
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={
                "account_id": "qr_account",
                "user_id": 1,
                "bound_unb": "old_user",
                "bind_status": "active",
             }), \
             mock.patch.object(reply_server.db_manager, "assert_cookie_belongs_to_user", return_value=True), \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"qr_account": "unb=old_user; cookie2=old"}), \
             mock.patch.object(reply_server.db_manager, "add_risk_control_log", return_value=123), \
             mock.patch.object(reply_server.db_manager, "update_risk_control_log"), \
             mock.patch.object(reply_server.db_manager, "get_cookie_by_id", return_value={"cookies_str": "unb=other_user; cookie2=real"}), \
             mock.patch.object(reply_server.db_manager, "bind_cookie_account_unb", return_value=False), \
             mock.patch.object(reply_server.db_manager, "update_cookie_account_info") as rollback_mock, \
             mock.patch.object(reply_server.cookie_manager, "manager", mock.Mock()), \
             mock.patch("XianyuAutoAsync.XianyuLive", _FakeLive), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaisesRegex(RuntimeError, "bound_unb"):
                asyncio.run(invoke())

        rollback_mock.assert_called_once_with("qr_account", cookie_value="unb=old_user; cookie2=old")

    def test_process_qr_login_cookies_passes_account_id_to_live_instance(self):
        managed_runtime_lease = object()
        fake_instance = mock.Mock()
        fake_instance.refresh_cookies_from_qr_login = mock.AsyncMock(return_value=False)

        async def invoke():
            return await reply_server.process_qr_login_cookies(
                "qr_account",
                "unb=test_user; cookie2=qr_cookie",
                "test_user",
                current_user={"user_id": 1, "username": "admin"},
                managed_runtime_lease=managed_runtime_lease,
            )

        with mock.patch.object(reply_server.db_manager, "get_cookie_binding_info", return_value={
                "account_id": "qr_account",
                "user_id": 1,
                "bound_unb": "test_user",
                "bind_status": "active",
             }), \
             mock.patch.object(reply_server.db_manager, "assert_cookie_belongs_to_user", return_value=True), \
             mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"qr_account": "old"}), \
             mock.patch.object(reply_server.db_manager, "add_risk_control_log", return_value=123), \
             mock.patch.object(reply_server.db_manager, "update_risk_control_log"), \
             mock.patch("XianyuAutoAsync.XianyuLive", return_value=fake_instance), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaisesRegex(RuntimeError, "真实Cookie获取失败"):
                asyncio.run(invoke())

        fake_instance.refresh_cookies_from_qr_login.assert_awaited_once()
        refresh_kwargs = fake_instance.refresh_cookies_from_qr_login.await_args.kwargs
        self.assertEqual(refresh_kwargs["qr_cookies_str"], "unb=test_user; cookie2=qr_cookie")
        self.assertEqual(refresh_kwargs["account_id"], "qr_account")
        self.assertEqual(refresh_kwargs["user_id"], 1)
        self.assertIs(refresh_kwargs["managed_runtime_lease"], managed_runtime_lease)
        self.assertNotIn("cookie_id", refresh_kwargs)

    def test_process_qr_login_cookies_rejects_default_account_id_before_placeholder_or_binding(self):
        class _FailIfConstructed:
            def __init__(self, *args, **kwargs):
                raise AssertionError("default account_id must be rejected before XianyuLive")

        async def invoke():
            return await reply_server.process_qr_login_cookies(
                "default",
                "unb=test_user; cookie2=qr_cookie",
                "test_user",
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(
            reply_server.db_manager,
            "get_cookie_binding_info",
            side_effect=AssertionError("default account_id should be rejected before binding lookup"),
        ), mock.patch.object(
            reply_server.db_manager,
            "create_cookie_account_placeholder",
            side_effect=AssertionError("default account_id should be rejected before placeholder creation"),
        ), mock.patch(
            "XianyuAutoAsync.XianyuLive",
            _FailIfConstructed,
        ), mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaisesRegex(RuntimeError, "non-empty, non-default account_id"):
                asyncio.run(invoke())


class ReplyServerItemAccountApiOwnershipTest(unittest.TestCase):
    def test_get_all_items_from_account_rejects_cross_user_cookie_with_403(self):
        async def invoke():
            return await reply_server.get_all_items_from_account(
                {"account_id": "other_account"},
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"owned_account": "cookie"}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_by_id", side_effect=AssertionError("must not load other user's cookie")):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 403)

    def test_get_items_by_page_rejects_cross_user_cookie_with_403(self):
        async def invoke():
            return await reply_server.get_items_by_page(
                {"account_id": "other_account", "page_number": 1, "page_size": 20},
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"owned_account": "cookie"}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_by_id", side_effect=AssertionError("must not load other user's cookie")):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 403)

    def test_polish_account_items_rejects_cross_user_cookie_with_403(self):
        async def invoke():
            return await reply_server.polish_account_items(
                "other_account",
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"owned_account": "cookie"}), \
             mock.patch.object(reply_server.db_manager, "get_cookie_by_id", side_effect=AssertionError("must not load other user's cookie")):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 403)


class QRLoginFrontendContractTest(unittest.TestCase):
    def test_frontend_posts_explicit_account_id_when_generating_qr_code(self):
        app_js = Path("static/js/app.js").read_text(encoding="utf-8")

        self.assertIn("function getQRCodeLoginAccountId()", app_js)
        self.assertIn("const accountId = getQRCodeLoginAccountId();", app_js)
        self.assertIn("JSON.stringify({ account_id: accountId })", app_js)
        self.assertIn("body:", app_js)

    def test_qr_login_modal_has_account_id_input(self):
        index_html = Path("static/index.html").read_text(encoding="utf-8")

        self.assertIn('id="qrLoginAccountId"', index_html)
        self.assertIn('onclick="refreshQRCode()"', index_html)


if __name__ == "__main__":
    unittest.main()
