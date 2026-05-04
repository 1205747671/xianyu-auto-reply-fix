import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import reply_server


class _FakeSlider:
    def __init__(self):
        self.run_called = False

    def run(self, *args, **kwargs):
        self.run_called = True
        return True, {"unexpected": "browser"}


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


if __name__ == "__main__":
    unittest.main()
