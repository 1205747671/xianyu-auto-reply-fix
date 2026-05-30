import asyncio
import base64
import concurrent.futures
from datetime import datetime
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
import types
import unittest
from unittest import mock
from fastapi.testclient import TestClient
from pydantic import ValidationError

if "loguru" not in sys.modules:
    loguru_stub = types.ModuleType("loguru")
    loguru_stub.logger = mock.Mock()
    sys.modules["loguru"] = loguru_stub

import reply_server


REPO_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_MODULE_REFERENCES = {
    "reply_server": reply_server,
    "db_manager": sys.modules.get("db_manager"),
    "XianyuAutoAsync": sys.modules.get("XianyuAutoAsync"),
}
_PROJECT_REPLY_SERVER_DB_MANAGER = getattr(reply_server, "db_manager", None)


class _ReplyServerModuleBindingMixin:
    def setUp(self):
        super().setUp()
        original_modules = {
            name: sys.modules.get(name)
            for name in _PROJECT_MODULE_REFERENCES
        }
        for name, module in _PROJECT_MODULE_REFERENCES.items():
            if module is not None:
                sys.modules[name] = module

        original_reply_server_db_manager = getattr(reply_server, "db_manager", None)
        if _PROJECT_REPLY_SERVER_DB_MANAGER is not None:
            reply_server.db_manager = _PROJECT_REPLY_SERVER_DB_MANAGER

        def _restore_module_binding():
            for name, original_module in original_modules.items():
                if original_module is None:
                    expected_module = _PROJECT_MODULE_REFERENCES.get(name)
                    if expected_module is not None and sys.modules.get(name) is expected_module:
                        sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original_module

            reply_server.db_manager = original_reply_server_db_manager

        self.addCleanup(_restore_module_binding)


class _FakeAiohttpResponse:
    def __init__(self, *, status=200, text='{"code": 0}'):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return self._text


class _FakeAiohttpClientSession:
    def __init__(self, payload_sink, *, status=200, text='{"code": 0}', client_session_kwargs=None):
        self.payload_sink = payload_sink
        self.status = status
        self.text_payload = text
        self.payload_sink["client_session_kwargs"] = client_session_kwargs or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json=None, timeout=None, headers=None):
        self.payload_sink["post_url"] = url
        self.payload_sink["post_json"] = json
        self.payload_sink["post_timeout"] = timeout
        self.payload_sink["post_headers"] = headers
        return _FakeAiohttpResponse(status=self.status, text=self.text_payload)

    def get(self, url, params=None, timeout=None):
        self.payload_sink["get_url"] = url
        self.payload_sink["get_params"] = params
        self.payload_sink["get_timeout"] = timeout
        return _FakeAiohttpResponse(status=self.status, text=self.text_payload)

    def put(self, url, json=None, timeout=None, headers=None):
        self.payload_sink["put_url"] = url
        self.payload_sink["put_json"] = json
        self.payload_sink["put_timeout"] = timeout
        self.payload_sink["put_headers"] = headers
        return _FakeAiohttpResponse(status=self.status, text=self.text_payload)


class _FakeAsyncWebsocketConnection:
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent_messages = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send(self, message):
        self.sent_messages.append(message)


def _expected_feishu_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    secret_bytes = secret.encode("utf-8")
    digest = hmac.new(secret_bytes, string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _expected_dingtalk_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    secret_bytes = secret.encode("utf-8")
    digest = hmac.new(secret_bytes, string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class ReplyServerAccountScopeContractTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_admin_permissions_do_not_fallback_to_username_after_session_reconcile(self):
        user_info = {"user_id": 1, "username": "admin", "is_admin": False}

        self.assertFalse(reply_server._is_admin_session(user_info))
        self.assertEqual(
            {
                "authenticated": True,
                "user_id": 1,
                "username": "admin",
                "is_admin": False,
            },
            asyncio.run(reply_server.verify(user_info=user_info)),
        )
        with mock.patch.object(reply_server, "verify_token", return_value=user_info):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.verify_admin_token(
                    credentials=SimpleNamespace(credentials="demo-token")
                )
        self.assertEqual(403, raised.exception.status_code)

    def test_item_routes_use_account_id_contract(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn('@app.get("/items/account/{account_id}")', source)
        self.assertNotIn('@app.get("/items/{account_id}")', source)
        self.assertNotIn('@app.get("/items/cookie/{account_id}")', source)
        self.assertIn('@app.get("/items/{account_id}/{item_id}")', source)
        self.assertIn('@app.put("/items/{account_id}/{item_id}")', source)
        self.assertIn('@app.delete("/items/{account_id}/{item_id}")', source)
        self.assertIn('@app.put("/items/{account_id}/{item_id}/multi-spec")', source)
        self.assertIn('@app.put("/items/{account_id}/{item_id}/multi-quantity-delivery")', source)

    def test_ai_reply_routes_use_account_id_contract(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn('@app.get("/ai-reply-settings/{account_id}")', source)
        self.assertIn('@app.put("/ai-reply-settings/{account_id}")', source)
        self.assertIn('@app.post("/ai-reply-test/{account_id}")', source)
        self.assertIn('detail="无权限访问该账号"', source)
        self.assertIn('detail="无权限操作该账号"', source)
        self.assertIn("account_id=account_id", source)
        self.assertNotIn("or user_info['username'] == ADMIN_USERNAME", source)
        self.assertNotIn("or username == ADMIN_USERNAME", source)
        self.assertNotIn("or current_user.get('username') == ADMIN_USERNAME", source)

    def test_item_reply_routes_and_batch_body_use_account_id_contract(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn('@app.get("/itemReplays/account/{account_id}")', source)
        self.assertNotIn('@app.get("/itemReplays/cookie/{account_id}")', source)
        self.assertIn('@app.put("/item-reply/{account_id}/{item_id}")', source)
        self.assertIn('@app.delete("/item-reply/{account_id}/{item_id}")', source)
        self.assertIn('@app.get("/item-reply/{account_id}/{item_id}")', source)
        self.assertIn("account_id: str", source)
        self.assertNotIn('"cookie_id": account_id', source)
        self.assertNotIn("item_reply.get('account_id') or item_reply.get('cookie_id') or account_id", source)

    def test_qr_cooldown_routes_use_account_id_contract(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn('@app.post("/qr-login/reset-cooldown/{account_id}")', source)
        self.assertIn('@app.get("/qr-login/cooldown-status/{account_id}")', source)
        self.assertIn("'account_id': account_id", source)
        self.assertNotIn(
            "instance = cookie_manager.manager.get_xianyu_instance(account_id) if cookie_manager.manager else None",
            source,
        )

    def test_order_history_sync_request_prefers_account_id(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")
        history_sync = (REPO_ROOT / "utils" / "order_history_sync.py").read_text(encoding="utf-8")
        order_detail_fetcher = (REPO_ROOT / "utils" / "order_detail_fetcher.py").read_text(encoding="utf-8")
        xianyu_async = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")

        self.assertIn("class OrderHistorySyncRequest(BaseModel):", source)
        self.assertIn("account_id: Optional[str] = None", source)
        self.assertNotIn("cookie_id: Optional[str] = None", source)
        self.assertIn("'account_id': account_id", source)
        self.assertIn("request_data.get('account_id')", source)
        self.assertNotIn("request_data.get('cookie_id')", source)
        self.assertIn('OrderHistoryPageFetcher(cookie_string, account_id=account_id, headless=True)', source)
        self.assertIn("detail_result = await _run_managed_live_instance_call(", source)
        self.assertNotIn("live_instance = cookie_manager.manager.get_xianyu_instance(account_id)", source)
        self.assertIn("def __init__(self, cookie_string: str, account_id: str, headless: bool = True):", history_sync)
        self.assertNotIn("account_id_for_log", history_sync)
        self.assertIn("def __init__(self, cookie_string: str = None, headless: bool = True, account_id: str = None):", order_detail_fetcher)
        self.assertNotIn("account_id_for_log", order_detail_fetcher)
        self.assertNotIn("account_id_for_log=self.cookie_id", xianyu_async)


class ReplyServerAccountWorkerFutureWaitTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_wait_threadsafe_future_result_services_same_account_worker_queue(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        manager = AccountBrowserRuntimeManager(base_dir=temp_dir)
        original_manager = reply_server.account_browser_runtime_manager
        reply_server.account_browser_runtime_manager = manager
        self.addCleanup(lambda: setattr(reply_server, "account_browser_runtime_manager", original_manager))

        thread_future = concurrent.futures.Future()

        def nested_worker_task():
            thread_future.set_result("nested-ok")
            return "nested-ok"

        def wait_inside_account_worker():
            def enqueue_nested_task():
                manager.run_sync_task_on_account_thread(
                    "account-42",
                    nested_worker_task,
                    timeout=0.5,
                )

            enqueue_thread = threading.Thread(target=enqueue_nested_task, daemon=True)
            enqueue_thread.start()
            try:
                return reply_server._wait_threadsafe_future_result(
                    thread_future,
                    0.5,
                    "future wait timed out",
                    account_id="account-42",
                )
            finally:
                enqueue_thread.join(timeout=0.5)

        result = manager.run_sync_task_on_account_thread(
            "account-42",
            wait_inside_account_worker,
            timeout=1.0,
        )

        self.assertEqual("nested-ok", result)

    def test_item_sync_endpoints_accept_account_id_request_field(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("request.get('account_id')", source)
        self.assertNotIn("request.get('account_id') or request.get('cookie_id')", source)
        self.assertIn('缺少account_id参数', source)

    def test_risk_log_and_slider_filters_use_account_id_contract(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("account_id: str = None,", source)
        self.assertIn("target_account_id = str(account_id or '').strip() or None", source)
        self.assertIn("target_account_id = str(account_id or '').strip()", source)
        self.assertIn("target_account_ids = [target_account_id]", source)
        self.assertIn("stats = db_manager.get_slider_verification_session_stats(account_ids=target_account_ids, range_key=normalized_range)", source)
        self.assertIn("'selected_account_id': target_account_id", source)
        self.assertNotIn("event_meta=_build_risk_event_meta({'account_id': cookie_id})", source)
        self.assertNotIn("'selected_cookie_id': target_account_id", source)
        self.assertNotIn("target_cookie_ids =", source)
        self.assertNotIn("log.get('account_id') or log.get('cookie_id')", source)

    def test_send_message_and_auto_reply_endpoints_use_account_id_contract(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")
        xianyu_async = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")

        self.assertIn("class SendMessageRequest(BaseModel):\n    api_key: str\n    account_id: str", source)
        self.assertIn("class RequestModel(BaseModel):\n    account_id: str", source)
        self.assertIn("cleaned_account_id = clean_param(request.account_id)", source)
        self.assertNotIn("cleaned_cookie_id = clean_param(request.cookie_id)", source)
        self.assertNotIn("live_instance = _get_managed_live_instance(cleaned_account_id)", source)
        self.assertIn("await _run_managed_live_instance_call(", source)
        self.assertNotIn("live_instance = XianyuLive.get_instance(cleaned_account_id)", source)
        self.assertIn("msg_template = match_reply(req.account_id, req.send_message)", source)
        self.assertIn("default_reply_settings = db_manager.get_default_reply(req.account_id)", source)
        self.assertIn("db_manager.has_default_reply_record(req.account_id, req.chat_id)", source)
        self.assertIn('"account_id": current_account_id', xianyu_async)
        self.assertNotIn('"cookie_id": self.cookie_id', xianyu_async)

    def test_item_sync_batch_payloads_use_account_id_contract(self):
        xianyu_async = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")

        self.assertIn("batch_update_data.append({\n                        'account_id': current_account_id,", xianyu_async)
        self.assertIn("batch_new_data.append({\n                        'account_id': current_account_id,", xianyu_async)
        self.assertNotIn("batch_update_data.append({\n                        'cookie_id': self.cookie_id,", xianyu_async)
        self.assertNotIn("batch_new_data.append({\n                        'cookie_id': self.cookie_id,", xianyu_async)

    def test_xianyu_async_runtime_messages_drop_cookie_id_fallback_wording(self):
        xianyu_async = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")

        self.assertNotIn("拒绝按旧 cookie_id", xianyu_async)
        self.assertNotIn("跳过按旧 cookie_id", xianyu_async)
        self.assertNotIn("回退旧 cookie_id", xianyu_async)
        self.assertNotIn("cookie_id_missing", xianyu_async)
        self.assertNotIn("检查cookie_id是否在cookies表中存在", xianyu_async)
        self.assertNotIn("如果当前实例的cookie_id匹配", xianyu_async)

    def test_qr_cookie_refresh_source_keeps_human_readable_cookie_diff_logs(self):
        xianyu_async = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")

        self.assertIn('logger.info(f"【{target_account_id}】  {i:2d}. {key}: {value}")', xianyu_async)
        self.assertIn(
            'logger.info(f"【{target_account_id}】  值: {self._mask_secret_value(cookie_value, head=8, tail=6)}")',
            xianyu_async,
        )
        self.assertIn('logger.info(f"【{target_account_id}】  ---")', xianyu_async)
        self.assertNotIn('logger.info(f"【{target_account_id}? {i:2d}. {key}: {value}")', xianyu_async)
        self.assertNotIn(
            'logger.info(f"【{target_account_id}? ? {self._mask_secret_value(cookie_value, head=8, tail=6)}")',
            xianyu_async,
        )
        self.assertNotIn('logger.info(f"【{target_account_id}? ---")', xianyu_async)

    def test_save_items_list_to_db_failure_stays_local_and_does_not_escalate_fetch_retry(self):
        xianyu_async = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")

        self.assertIn('logger.error(f"批量保存商品信息异常: {self._safe_str(e)}")', xianyu_async)
        self.assertIn("return 0", xianyu_async)
        self.assertNotIn('logger.error(f"批量保存商品信息异常: {self._safe_str(e)}")\n            raise', xianyu_async)

    def test_xianyu_async_source_has_no_cookie_id_residue(self):
        xianyu_async = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")
        self.assertNotIn("cookie_id", xianyu_async)

    def test_browser_modules_do_not_keep_direct_browser_launch_bypasses(self):
        xianyu_async = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")
        order_detail_fetcher = (REPO_ROOT / "utils" / "order_detail_fetcher.py").read_text(encoding="utf-8")
        qr_login = (REPO_ROOT / "utils" / "qr_login.py").read_text(encoding="utf-8")

        self.assertNotIn("def _launch_browser_safe(", xianyu_async)
        self.assertNotIn("launch_browser_async,", xianyu_async)
        self.assertNotIn("launch_browser_persistent_context_async,", xianyu_async)
        self.assertNotIn("async def launch_browser_persistent_context_async", (REPO_ROOT / "utils" / "item_search.py").read_text(encoding="utf-8"))
        self.assertNotIn("launch_browser_async", order_detail_fetcher)
        self.assertNotIn("launch_browser_persistent_context_async", qr_login)

    def test_slider_business_entrypoints_require_managed_runtime(self):
        reply_server_source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")
        xianyu_async = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")

        self.assertIn(
            "runtime_lease = _acquire_slider_managed_runtime_sync(\n"
            "                    account_id,\n"
            "                    'password_login',\n"
            "                    slider_instance,\n"
            "                )\n"
            "                cookies_dict = slider_instance.login_with_password_browser(\n"
            "                    account=account,\n"
            "                    password=password,\n"
            "                    notification_callback=notification_callback,\n"
            "                    force_clean_context=is_refresh_mode,\n"
            "                    require_managed_runtime=True,\n"
            "                )",
            reply_server_source,
        )
        self.assertIn(
            "runtime_lease = _acquire_slider_managed_runtime_sync(\n"
            "                    account_id,\n"
            "                    'manual_cookie_import',\n"
            "                    slider_instance,\n"
            "                )\n"
            "                success, cookies_dict = slider_instance.run(\n"
            "                    target_url,\n"
            "                    notification_callback=notification_callback,\n"
            "                    notification_scene='手动导入 Cookie',\n"
            "                    require_managed_runtime=True,\n"
            "                )",
            reply_server_source,
        )
        self.assertIn(
            "return slider.run(\n"
            "                verification_url,\n"
            "                require_managed_runtime=True,\n"
            "            )",
            xianyu_async,
        )
        self.assertIn(
            "return slider.login_with_password_browser(\n"
            "                account=account,\n"
            "                password=password,\n"
            "                show_browser=show_browser,\n"
            "                notification_callback=notification_callback,\n"
            "                force_clean_context=force_clean_context,\n"
            "                require_managed_runtime=True,\n"
            "            )",
            xianyu_async,
        )

    def test_password_login_handoff_releases_sync_runtime_before_persisting_account_task(self):
        reply_server_source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn(
            "if not is_refresh_mode and runtime_lease is not None:\n"
            "                    try:\n"
            "                        _release_slider_managed_runtime_sync(\n"
            "                            runtime_lease,\n"
            "                            reason='password_login_handoff_release',\n"
            "                        )\n"
            "                    except Exception as runtime_release_err:\n"
            "                        log_with_user(\n"
            "                            'warning',\n"
            "                            f\"密码登录交接前释放账号runtime失败（后续正式实例仍会继续接管）: {account_id}, 错误: {str(runtime_release_err)}\",\n"
            "                            current_user,\n"
            "                        )\n"
            "                    runtime_lease = None\n"
            "                    try:\n"
            "                        _invalidate_slider_managed_runtime_sync(\n"
            "                            account_id,\n"
            "                            reason='password_login_handoff_invalidate',\n"
            "                        )\n"
            "                        log_with_user(\n"
            "                            'info',\n"
            "                            f\"密码登录交接前已主动失效旧的账号级浏览器runtime，避免正式实例首轮恢复继续抢同一profile: {account_id}\",\n"
            "                            current_user,\n"
            "                        )\n"
            "                    except Exception as runtime_invalidate_err:\n"
            "                        log_with_user(\n"
            "                            'warning',\n"
            "                            f\"密码登录交接前失效旧账号runtime失败（后续正式实例仍会继续接管）: {account_id}, 错误: {str(runtime_invalidate_err)}\",\n"
            "                            current_user,\n"
            "                        )\n"
            "\n"
            "                if not is_refresh_mode:\n"
            "                    from XianyuAutoAsync import XianyuLive\n"
"\n"
            "                    XianyuLive.mark_manual_refresh_handoff(",
            reply_server_source,
        )


class ReplyServerProxyUpdateBehaviorTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_update_cookie_proxy_config_skips_restart_when_effective_config_unchanged(self):
        fake_manager = mock.Mock()
        fake_db = mock.Mock()
        fake_db.get_cookie_proxy_config.return_value = {
            "proxy_type": "none",
            "proxy_host": "",
            "proxy_port": 0,
            "proxy_user": "",
            "proxy_pass": "",
        }

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "cookie_manager", SimpleNamespace(manager=fake_manager)), \
             mock.patch.object(reply_server, "_ensure_account_access", return_value="1"):
            result = reply_server.update_cookie_proxy_config(
                "1",
                reply_server.ProxyConfig(
                    proxy_type="none",
                    proxy_host="",
                    proxy_port=0,
                    proxy_user="",
                    proxy_pass="",
                ),
                current_user={"user_id": 1, "username": "tester"},
            )

        self.assertTrue(result["success"])
        self.assertFalse(result["task_restarted"])
        fake_db.update_cookie_proxy_config.assert_not_called()
        fake_manager.update_cookie.assert_not_called()

    def test_update_cookie_proxy_config_restarts_when_effective_config_changes(self):
        fake_manager = mock.Mock()
        fake_db = mock.Mock()
        fake_db.get_cookie_proxy_config.return_value = {
            "proxy_type": "none",
            "proxy_host": "",
            "proxy_port": 0,
            "proxy_user": "",
            "proxy_pass": "",
        }
        fake_db.update_cookie_proxy_config.return_value = True

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "cookie_manager", SimpleNamespace(manager=fake_manager)), \
             mock.patch.object(reply_server, "_ensure_account_access", return_value="1"), \
             mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"1": "cookie2=v1"}):
            result = reply_server.update_cookie_proxy_config(
                "1",
                reply_server.ProxyConfig(
                    proxy_type="http",
                    proxy_host="127.0.0.1",
                    proxy_port=1081,
                    proxy_user="",
                    proxy_pass="",
                ),
                current_user={"user_id": 1, "username": "tester"},
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["task_restarted"])
        fake_db.update_cookie_proxy_config.assert_called_once_with(
            "1",
            proxy_type="http",
            proxy_host="127.0.0.1",
            proxy_port=1081,
            proxy_user="",
            proxy_pass="",
        )
        fake_manager.update_cookie.assert_called_once_with("1", "cookie2=v1", save_to_db=False)

    def test_update_cookie_proxy_config_keeps_database_change_when_cookie_manager_unready(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_proxy_config.return_value = {
            "proxy_type": "none",
            "proxy_host": "",
            "proxy_port": 0,
            "proxy_user": "",
            "proxy_pass": "",
        }
        fake_db.update_cookie_proxy_config.return_value = True

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "cookie_manager", SimpleNamespace(manager=None)), \
             mock.patch.object(reply_server, "_ensure_account_access", return_value="1"), \
             mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"1": "cookie2=v1"}):
            result = reply_server.update_cookie_proxy_config(
                "1",
                reply_server.ProxyConfig(
                    proxy_type="http",
                    proxy_host="127.0.0.1",
                    proxy_port=1081,
                    proxy_user="",
                    proxy_pass="",
                ),
                current_user={"user_id": 1, "username": "tester"},
            )

        self.assertTrue(result["success"])
        self.assertFalse(result["task_restarted"])
        self.assertIn("下次启动", result["msg"])
        fake_db.update_cookie_proxy_config.assert_called_once_with(
            "1",
            proxy_type="http",
            proxy_host="127.0.0.1",
            proxy_port=1081,
            proxy_user="",
            proxy_pass="",
        )

    def test_password_login_refresh_mode_invalidates_sync_runtime_after_capturing_cookies(self):
        reply_server_source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn(
            "                    runtime_lease = None\n"
            "                    try:\n"
            "                        _invalidate_slider_managed_runtime_sync(\n"
            "                            account_id,\n"
            "                            reason='password_login_captured_cookies_handoff_invalidate',\n"
            "                        )\n"
            "                        log_with_user(\n"
            "                            'info',\n"
            "                            f\"刷新模式已主动失效旧的账号级浏览器runtime，避免首轮接管继续占用同一profile: {account_id}\",\n"
            "                            current_user,\n"
            "                        )\n",
            reply_server_source,
        )

    def test_qr_login_handoff_uses_relaxed_soft_timeout_without_cancelling_manager_switch(self):
        reply_server_source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn(
            "                            await _run_live_instance_on_manager_loop(\n"
            "                                account_id,\n"
            "                                _switch_cookie_manager_runtime,\n"
            "                                # 这里不把“任务切换”作为扫码登录成功的硬前置条件：\n"
            "                                # - 等太久用户会以为“扫了没反应”\n"
            "                                # - 但我们又希望任务切换能继续在后台完成\n"
            "                                #\n"
            "                                # 所以：设置一个软超时，并且不在超时时取消 manager.loop 内部的切换协程。\n"
            "                                timeout=25.0,\n"
            "                                cancel_on_timeout=False,\n"
            "                            )",
            reply_server_source,
        )

    def test_db_manager_and_startup_drop_cookie_id_identity_wording(self):
        db_manager_source = (REPO_ROOT / "db_manager.py").read_text(encoding="utf-8")
        start_source = (REPO_ROOT / "Start.py").read_text(encoding="utf-8")

        self.assertNotIn("account_id: Cookie ID", db_manager_source)
        self.assertIn("account_id = str(entry.get('account_id') or '').strip()", start_source)
        self.assertNotIn("account_id = entry.get('id')", start_source)
        self.assertNotIn("cid = entry.get('account_id')", start_source)
        self.assertNotIn("manager.add_cookie(cid, val, kw_list)", start_source)

    def test_runtime_and_cookie_responses_use_account_id_fields(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("def _get_managed_live_instance(account_id: str):", source)
        self.assertIn("async def _run_managed_live_instance_call(", source)
        self.assertIn("def _build_live_runtime_status(account_id: str)", source)
        self.assertNotIn("def _build_live_runtime_status(cookie_id: str)", source)
        self.assertIn("live_instance = _get_managed_live_instance(normalized_account_id)", source)
        self.assertNotIn("getattr(cookie_manager.manager, 'live_instances', {})", source)
        self.assertIn('@app.get("/accounts/{account_id}/details")', source)
        self.assertIn('@app.get("/accounts/{account_id}/runtime-status")', source)
        self.assertIn('@app.get("/accounts/{account_id}/conversations/{conversation_id}/history")', source)
        self.assertIn('@app.post("/accounts/{account_id}/session-keepalive")', source)
        self.assertIn('@app.post("/accounts/{account_id}/token-refresh")', source)
        self.assertNotIn('@app.get("/cookie/{account_id}/details")', source)
        self.assertNotIn('@app.get("/cookies/{account_id}/runtime-status")', source)
        self.assertNotIn('@app.get("/cookies/{account_id}/conversations/{conversation_id}/history")', source)
        self.assertNotIn('@app.post("/cookies/{account_id}/session-keepalive")', source)
        self.assertNotIn('@app.post("/cookies/{account_id}/token-refresh")', source)
        self.assertNotIn("def _ensure_cookie_access(", source)
        self.assertIn('_ensure_account_access(account_id, current_user, "访问")', source)
        self.assertNotIn("live_instance = XianyuLive.get_instance(account_id)", source)
        self.assertIn("'account_id': account_id,\n            'runtime_status': await _build_live_runtime_status(account_id),", source)
        self.assertNotIn("'cookie_id': account_id,\n            'runtime_status': await _build_live_runtime_status(account_id),", source)
        self.assertIn("'account_id': account_id,\n            'conversation_id': normalized_conversation_id,", source)
        self.assertIn("'account_id': account_id,\n            'message': '轻量会话保活成功' if keepalive_ok else '轻量会话保活失败',", source)
        self.assertIn("'account_id': account_id,", source)
        self.assertIn("'user_id': user_id,", source)
        self.assertIn("'value': mask_cookie_value(cookie_value),", source)
        self.assertNotIn("'id': cookie_id,\n            'value': mask_cookie_value(cookie_value),", source)

    def test_xianyu_live_callers_use_account_id_keyword_contract(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("account_id=runtime_account_id", source)
        self.assertNotIn("cookie_id=runtime_account_id", source)
        self.assertIn("XianyuLive(cookies_str, account_id=account_id, register_instance=False)", source)
        self.assertNotIn("XianyuLive(cookies_str, account_id, register_instance=False)", source)

    def test_require_runtime_account_id_rejects_blank_or_default_scope(self):
        self.assertEqual(
            reply_server._require_runtime_account_id("  acc-runtime-1  ", action_text="unit test"),
            "acc-runtime-1",
        )
        for invalid_account_id in ("   ", "default"):
            with self.subTest(account_id=invalid_account_id):
                with self.assertRaisesRegex(ValueError, "non-empty, non-default account_id"):
                    reply_server._require_runtime_account_id(
                        invalid_account_id,
                        action_text="unit test",
                    )

    def test_require_runtime_account_id_rejects_invalid_format_scope(self):
        for invalid_account_id in ("bad scope!", "scope/1", "中文账号"):
            with self.subTest(account_id=invalid_account_id):
                with self.assertRaisesRegex(ValueError, "account_id"):
                    reply_server._require_runtime_account_id(
                        invalid_account_id,
                        action_text="unit test",
                    )

    def test_ensure_account_access_rejects_invalid_format_before_cookie_lookup(self):
        with mock.patch.object(
            reply_server,
            "_get_user_cookies_map",
            side_effect=AssertionError("invalid account_id should be rejected before account lookup"),
        ) as get_user_cookies_map:
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server._ensure_account_access(
                    "bad scope!",
                    {"user_id": 1, "username": "admin"},
                    "访问",
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("account_id", raised.exception.detail)
        get_user_cookies_map.assert_not_called()

    def test_add_cookie_rejects_invalid_account_id_before_db_access(self):
        current_user = {"user_id": 1, "username": "admin"}

        with mock.patch.object(reply_server.cookie_manager, "manager", mock.Mock()), mock.patch.object(
            reply_server.db_manager,
            "get_cookie_binding_info",
            side_effect=AssertionError("invalid account_id should be rejected before binding lookup"),
        ) as get_cookie_binding_info, mock.patch.object(reply_server, "log_with_user"):
            for invalid_account_id in ("default", "bad scope!"):
                with self.subTest(account_id=invalid_account_id):
                    with self.assertRaises(reply_server.HTTPException) as raised:
                        reply_server.add_cookie(
                            reply_server.AccountCookieUpsertIn(
                                account_id=invalid_account_id,
                                value="unb=test_user; cookie2=test_cookie2",
                            ),
                            current_user=current_user,
                        )
                    self.assertEqual(raised.exception.status_code, 400)
                    self.assertIn("account_id", raised.exception.detail)

        get_cookie_binding_info.assert_not_called()

    def test_prepare_manual_refresh_runtime_account_id_marks_handoff_with_normalized_scope(self):
        with mock.patch("XianyuAutoAsync.XianyuLive.mark_manual_refresh_handoff") as handoff:
            runtime_account_id = reply_server._prepare_manual_refresh_runtime_account_id(
                "  acc-runtime-2  ",
                manual_refresh_owner="unit-test-owner",
            )

        self.assertEqual(runtime_account_id, "acc-runtime-2")
        handoff.assert_called_once_with("acc-runtime-2", source="unit-test-owner")

    def test_prepare_manual_refresh_runtime_account_id_rejects_invalid_scope_before_handoff(self):
        with mock.patch("XianyuAutoAsync.XianyuLive.mark_manual_refresh_handoff") as handoff:
            for invalid_account_id in ("   ", "default"):
                with self.subTest(account_id=invalid_account_id):
                    with self.assertRaisesRegex(ValueError, "non-empty, non-default account_id"):
                        reply_server._prepare_manual_refresh_runtime_account_id(
                            invalid_account_id,
                            manual_refresh_owner="unit-test-owner",
                        )

        handoff.assert_not_called()

    def test_password_login_rejects_default_account_id_before_refresh_state(self):
        async def invoke():
            return await reply_server.password_login(
                {
                    "account_id": "default",
                    "account": "unit-user",
                    "password": "unit-password",
                    "refresh_mode": True,
                },
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(
            reply_server.db_manager,
            "get_cookie_details",
            side_effect=AssertionError("default account_id should be rejected before cookie lookup"),
        ), mock.patch(
            "XianyuAutoAsync.XianyuLive.is_manual_refresh_active",
            side_effect=AssertionError("default account_id should be rejected before refresh state check"),
        ), mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertFalse(result["success"])
        self.assertIn("non-empty, non-default account_id", result["message"])

    def test_password_login_rejects_foreign_account_id_before_creating_session(self):
        current_user = {"user_id": 1, "username": "admin"}
        original_sessions = dict(reply_server.password_login_sessions)
        reply_server.password_login_sessions.clear()

        def restore_sessions():
            reply_server.password_login_sessions.clear()
            reply_server.password_login_sessions.update(original_sessions)

        self.addCleanup(restore_sessions)

        async def invoke():
            return await reply_server.password_login(
                {
                    "account_id": "acc-foreign-1",
                    "account": "unit-user",
                    "password": "unit-password",
                    "refresh_mode": False,
                },
                current_user=current_user,
            )

        with mock.patch.object(
            reply_server.db_manager,
            "get_cookie_binding_info",
            return_value={"account_id": "acc-foreign-1", "user_id": 2, "bind_status": "active"},
        ), mock.patch.object(
            reply_server.db_manager,
            "assert_cookie_belongs_to_user",
            side_effect=PermissionError("无权操作此账号"),
        ), mock.patch(
            "asyncio.create_task",
            return_value=mock.Mock(),
        ) as create_task, mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertFalse(result["success"])
        self.assertEqual("无权操作此账号", result["message"])
        self.assertEqual({}, reply_server.password_login_sessions)
        create_task.assert_not_called()

    def test_password_login_refresh_mode_accepts_string_user_id_snapshot(self):
        current_user = {"user_id": "7", "username": "demo-user"}
        original_sessions = dict(reply_server.password_login_sessions)
        reply_server.password_login_sessions.clear()

        def restore_sessions():
            reply_server.password_login_sessions.clear()
            reply_server.password_login_sessions.update(original_sessions)

        self.addCleanup(restore_sessions)

        def fake_create_task(coro):
            coro.close()
            return mock.Mock()

        async def invoke():
            return await reply_server.password_login(
                {
                    "account_id": "acc-refresh-owner-1",
                    "refresh_mode": True,
                },
                current_user=current_user,
            )

        with mock.patch.object(
            reply_server.db_manager,
            "get_cookie_details",
            return_value={
                "user_id": 7,
                "username": "refresh-user",
                "password": "refresh-pass",
                "show_browser": False,
            },
        ), mock.patch(
            "XianyuAutoAsync.XianyuLive.is_manual_refresh_active",
            return_value=False,
        ), mock.patch(
            "asyncio.create_task",
            side_effect=fake_create_task,
        ) as create_task, mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertTrue(result["success"])
        self.assertEqual("processing", result["status"])
        self.assertIn("session_id", result)
        create_task.assert_called_once()

    def test_manual_cookie_import_rejects_default_account_id_before_db_access(self):
        async def invoke():
            return await reply_server.manual_cookie_import(
                reply_server.ManualCookieImportRequest(
                    account_id="default",
                    cookie="unb=test_user; cookie2=test_cookie2",
                ),
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(
            reply_server.db_manager,
            "get_all_cookies",
            side_effect=AssertionError("default account_id should be rejected before cookie lookup"),
        ), mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertFalse(result["success"])
        self.assertIn("non-empty, non-default account_id", result["message"])

    def test_password_login_cleans_stale_foreign_pending_placeholder_before_creating_session(self):
        current_user = {"user_id": 1, "username": "admin"}
        original_sessions = dict(reply_server.password_login_sessions)
        original_qr_sessions = dict(reply_server.qr_login_manager.sessions)
        reply_server.password_login_sessions.clear()
        reply_server.qr_login_manager.sessions.clear()
        stale_placeholder = {"present": True}

        def restore_state():
            reply_server.password_login_sessions.clear()
            reply_server.password_login_sessions.update(original_sessions)
            reply_server.qr_login_manager.sessions.clear()
            reply_server.qr_login_manager.sessions.update(original_qr_sessions)

        self.addCleanup(restore_state)

        def fake_get_cookie_binding_info(_account_id):
            if stale_placeholder["present"]:
                return {"account_id": "acc-stale-login-1", "user_id": 2, "bind_status": "pending_bind"}
            return None

        def fake_delete_placeholder(_account_id, user_id=None):
            self.assertEqual(user_id, 2)
            stale_placeholder["present"] = False
            return True

        def fake_create_task(coro):
            coro.close()
            return mock.Mock()

        async def invoke():
            return await reply_server.password_login(
                {
                    "account_id": "acc-stale-login-1",
                    "account": "unit-user",
                    "password": "unit-password",
                    "refresh_mode": False,
                },
                current_user=current_user,
            )

        with mock.patch.object(
            reply_server.db_manager,
            "get_cookie_binding_info",
            side_effect=fake_get_cookie_binding_info,
        ), mock.patch.object(
            reply_server.db_manager,
            "delete_pending_cookie_placeholder",
            side_effect=fake_delete_placeholder,
        ) as delete_placeholder, mock.patch.object(
            reply_server.db_manager,
            "assert_cookie_belongs_to_user",
            side_effect=PermissionError("stale placeholder should have been cleaned first"),
        ) as assert_ownership, mock.patch.object(
            reply_server.qr_login_manager,
            "cleanup_expired_sessions",
        ) as cleanup_expired_sessions, mock.patch(
            "asyncio.create_task",
            side_effect=fake_create_task,
        ) as create_task, mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertTrue(result["success"])
        delete_placeholder.assert_called_once_with("acc-stale-login-1", user_id=2)
        cleanup_expired_sessions.assert_called_once()
        assert_ownership.assert_not_called()
        create_task.assert_called_once()

    def test_manual_cookie_import_cleans_stale_foreign_pending_placeholder_before_start(self):
        current_user = {"user_id": 1, "username": "admin"}
        original_sessions = dict(reply_server.manual_cookie_import_sessions)
        original_qr_sessions = dict(reply_server.qr_login_manager.sessions)
        reply_server.manual_cookie_import_sessions.clear()
        reply_server.qr_login_manager.sessions.clear()
        stale_placeholder = {"present": True}

        def restore_state():
            reply_server.manual_cookie_import_sessions.clear()
            reply_server.manual_cookie_import_sessions.update(original_sessions)
            reply_server.qr_login_manager.sessions.clear()
            reply_server.qr_login_manager.sessions.update(original_qr_sessions)

        self.addCleanup(restore_state)

        def fake_get_cookie_binding_info(_account_id):
            if stale_placeholder["present"]:
                return {"account_id": "acc-stale-import-1", "user_id": 2, "bind_status": "pending_bind"}
            return None

        def fake_delete_placeholder(_account_id, user_id=None):
            self.assertEqual(user_id, 2)
            stale_placeholder["present"] = False
            return True

        def fake_create_task(coro):
            coro.close()
            return mock.Mock()

        async def invoke():
            return await reply_server.manual_cookie_import(
                reply_server.ManualCookieImportRequest(
                    account_id="acc-stale-import-1",
                    cookie="unb=test_user; cookie2=test_cookie2",
                ),
                current_user=current_user,
            )

        with mock.patch.object(
            reply_server.db_manager,
            "get_cookie_binding_info",
            side_effect=fake_get_cookie_binding_info,
        ), mock.patch.object(
            reply_server.db_manager,
            "delete_pending_cookie_placeholder",
            side_effect=fake_delete_placeholder,
        ) as delete_placeholder, mock.patch.object(
            reply_server.qr_login_manager,
            "cleanup_expired_sessions",
        ) as cleanup_expired_sessions, mock.patch.object(
            reply_server.db_manager,
            "get_all_cookies",
            return_value={"acc-stale-import-1": ""},
        ), mock.patch(
            "asyncio.create_task",
            side_effect=fake_create_task,
        ) as create_task, mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertTrue(result["success"])
        delete_placeholder.assert_called_once_with("acc-stale-import-1", user_id=2)
        cleanup_expired_sessions.assert_called_once()
        create_task.assert_called_once()

    def test_password_login_session_status_and_cancel_accept_string_user_id_snapshot(self):
        original_sessions = dict(reply_server.password_login_sessions)
        reply_server.password_login_sessions.clear()

        def restore_sessions():
            reply_server.password_login_sessions.clear()
            reply_server.password_login_sessions.update(original_sessions)

        self.addCleanup(restore_sessions)

        reply_server.password_login_sessions["pwd-status-string"] = {
            "account_id": "acc-pwd-status-1",
            "status": "success",
            "timestamp": reply_server.time.time(),
            "completed_at": None,
            "user_id": 7,
            "is_new_account": False,
            "cookie_count": 1,
            "token_prewarmed": False,
            "real_cookie_refreshed": False,
            "fallback_reason": None,
        }
        reply_server.password_login_sessions["pwd-cancel-string"] = {
            "account_id": "acc-pwd-cancel-1",
            "status": "processing",
            "timestamp": reply_server.time.time(),
            "completed_at": None,
            "user_id": 7,
            "task": None,
            "slider_instance": None,
        }

        with mock.patch.object(reply_server, "log_with_user"), mock.patch.object(
            reply_server,
            "_update_session_risk_log",
        ), mock.patch.object(
            reply_server,
            "_close_password_login_pending_verification_risk_logs",
        ):
            status_result = asyncio.run(
                reply_server.check_password_login_status(
                    "pwd-status-string",
                    current_user={"user_id": "7", "username": "tester"},
                )
            )
            cancel_result = asyncio.run(
                reply_server.cancel_password_login(
                    "pwd-cancel-string",
                    current_user={"user_id": "7", "username": "tester"},
                )
            )

        self.assertEqual("success", status_result["status"])
        self.assertEqual("acc-pwd-status-1", status_result["account_id"])
        self.assertTrue(cancel_result["success"])
        self.assertEqual("cancelled", cancel_result["status"])

    def test_manual_cookie_import_session_status_and_cancel_accept_string_user_id_snapshot(self):
        original_sessions = dict(reply_server.manual_cookie_import_sessions)
        reply_server.manual_cookie_import_sessions.clear()

        def restore_sessions():
            reply_server.manual_cookie_import_sessions.clear()
            reply_server.manual_cookie_import_sessions.update(original_sessions)

        self.addCleanup(restore_sessions)

        reply_server.manual_cookie_import_sessions["manual-status-string"] = {
            "account_id": "acc-manual-status-1",
            "status": "success",
            "timestamp": reply_server.time.time(),
            "completed_at": None,
            "user_id": 7,
            "is_new_account": False,
            "cookie_count": 2,
        }
        reply_server.manual_cookie_import_sessions["manual-cancel-string"] = {
            "account_id": "acc-manual-cancel-1",
            "status": "processing",
            "timestamp": reply_server.time.time(),
            "completed_at": None,
            "user_id": 7,
            "task": None,
            "slider_instance": None,
        }

        with mock.patch.object(reply_server, "log_with_user"):
            status_result = asyncio.run(
                reply_server.check_manual_cookie_import_status(
                    "manual-status-string",
                    current_user={"user_id": "7", "username": "tester"},
                )
            )
            cancel_result = asyncio.run(
                reply_server.cancel_manual_cookie_import(
                    "manual-cancel-string",
                    current_user={"user_id": "7", "username": "tester"},
                )
            )

        self.assertEqual("success", status_result["status"])
        self.assertEqual("acc-manual-status-1", status_result["account_id"])
        self.assertTrue(cancel_result["success"])
        self.assertEqual("cancelled", cancel_result["status"])

    def test_account_management_routes_use_account_id_placeholders(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn('@app.get("/accounts")', source)
        self.assertIn('@app.post("/accounts")', source)
        self.assertIn('@app.get("/accounts/details")', source)
        self.assertIn('@app.post("/accounts/{account_id}/account-info")', source)
        self.assertIn('@app.get("/accounts/{account_id}/proxy")', source)
        self.assertIn('@app.post("/accounts/{account_id}/proxy")', source)
        self.assertIn("@app.put('/accounts/{account_id}')", source)
        self.assertIn("@app.put('/accounts/{account_id}/status')", source)
        self.assertIn('@app.delete("/accounts/{account_id}")', source)
        self.assertIn('@app.put("/accounts/{account_id}/auto-confirm")', source)
        self.assertIn('@app.get("/accounts/{account_id}/auto-confirm")', source)
        self.assertIn('@app.put("/accounts/{account_id}/auto-comment")', source)
        self.assertIn('@app.get("/accounts/{account_id}/auto-comment")', source)
        self.assertIn('@app.get("/accounts/{account_id}/comment-templates")', source)
        self.assertIn('@app.post("/accounts/{account_id}/comment-templates")', source)
        self.assertIn('@app.put("/accounts/{account_id}/comment-templates/{template_id}")', source)
        self.assertIn('@app.delete("/accounts/{account_id}/comment-templates/{template_id}")', source)
        self.assertIn('@app.put("/accounts/{account_id}/comment-templates/{template_id}/activate")', source)
        self.assertIn('@app.put("/accounts/{account_id}/remark")', source)
        self.assertIn('@app.get("/accounts/{account_id}/remark")', source)
        self.assertIn('@app.put("/accounts/{account_id}/pause-duration")', source)
        self.assertIn('@app.get("/accounts/{account_id}/pause-duration")', source)
        self.assertIn('@app.get("/accounts/check")', source)
        self.assertIn("@app.get('/admin/accounts')", source)
        self.assertNotIn('@app.post("/cookie/{account_id}/account-info")', source)
        self.assertNotIn('@app.get("/cookie/{account_id}/proxy")', source)
        self.assertNotIn('@app.post("/cookie/{account_id}/proxy")', source)
        self.assertNotIn('@app.get("/cookies")', source)
        self.assertNotIn('@app.post("/cookies")', source)
        self.assertNotIn("@app.put('/cookies/{account_id}')", source)
        self.assertNotIn("@app.put('/cookies/{account_id}/status')", source)
        self.assertNotIn('@app.delete("/cookies/{account_id}")', source)
        self.assertNotIn('@app.put("/cookies/{account_id}/auto-confirm")', source)
        self.assertNotIn('@app.get("/cookies/{account_id}/auto-confirm")', source)
        self.assertNotIn('@app.put("/cookies/{account_id}/auto-comment")', source)
        self.assertNotIn('@app.get("/cookies/{account_id}/auto-comment")', source)
        self.assertNotIn('@app.get("/cookies/{account_id}/comment-templates")', source)
        self.assertNotIn('@app.post("/cookies/{account_id}/comment-templates")', source)
        self.assertNotIn('@app.put("/cookies/{account_id}/comment-templates/{template_id}")', source)
        self.assertNotIn('@app.delete("/cookies/{account_id}/comment-templates/{template_id}")', source)
        self.assertNotIn('@app.put("/cookies/{account_id}/comment-templates/{template_id}/activate")', source)
        self.assertNotIn('@app.put("/cookies/{account_id}/remark")', source)
        self.assertNotIn('@app.get("/cookies/{account_id}/remark")', source)
        self.assertNotIn('@app.put("/cookies/{account_id}/pause-duration")', source)
        self.assertNotIn('@app.get("/cookies/{account_id}/pause-duration")', source)
        self.assertNotIn('@app.get("/cookies/check")', source)
        self.assertNotIn("@app.get('/admin/cookies')", source)
        self.assertIn('@app.get(\'/default-replies/{account_id}\')', source)

    def test_account_upsert_request_model_uses_account_id_field(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("class AccountCookieUpsertIn(BaseModel):", source)
        self.assertIn("account_id: str", source)
        self.assertIn("value: str", source)
        self.assertNotIn("class CookieIn(BaseModel):", source)
        self.assertNotIn("\n    id: str\n    value: str", source)

    def test_account_details_list_degrades_single_account_failures_instead_of_500(self):
        fake_runtime_manager = mock.Mock()
        fake_runtime_manager.get_cookie_status.side_effect = [True, RuntimeError("status exploded")]
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-good", "acc-bad"]
        fake_db.get_cookie_list_metadata.side_effect = [
            {"remark": "good", "username": "u1", "has_password": True, "pause_duration": 12},
            RuntimeError("metadata exploded"),
        ]
        fake_db.get_auto_confirm.return_value = True
        fake_db.get_auto_comment.return_value = False

        async def invoke():
            return await reply_server.get_cookies_details(
                include_runtime_status=False,
                summary_only=False,
                current_user={"user_id": 7, "username": "tester"},
            )

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "cookie_manager", SimpleNamespace(manager=fake_runtime_manager)), \
             mock.patch.object(reply_server, "_get_user_cookies_map", return_value={}), \
             mock.patch.object(reply_server, "logger"):
            result = asyncio.run(invoke())

        self.assertEqual(2, len(result))
        self.assertEqual("acc-good", result[0]["account_id"])
        self.assertTrue(result[0]["enabled"])
        self.assertEqual("acc-bad", result[1]["account_id"])
        self.assertTrue(result[1]["load_error"])
        self.assertFalse(result[1]["enabled"])
        self.assertEqual("", result[1]["value"])

    def test_system_settings_routes_require_admin_dependency(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("@app.get('/system-settings')", source)
        self.assertIn(
            "def get_system_settings(admin_user: Dict[str, Any] = Depends(require_admin)):",
            source,
        )
        self.assertIn("@app.put('/system-settings/{key}')", source)
        self.assertIn(
            "def update_system_setting(key: str, setting_data: SystemSettingIn, admin_user: Dict[str, Any] = Depends(require_admin)):",
            source,
        )
        self.assertNotIn(
            "def get_system_settings(current_user: Dict[str, Any] = Depends(get_current_user)):",
            source,
        )
        self.assertNotIn(
            "def update_system_setting(key: str, setting_data: SystemSettingIn, current_user: Dict[str, Any] = Depends(get_current_user)):",
            source,
        )

    def test_message_notifications_fallback_stays_account_scoped_without_mock_specific_reset(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("fallback_account_ids = _normalize_account_id_list(db_manager.get_account_ids(user_id))", source)
        self.assertIn("fallback_account_ids = _normalize_account_id_list(db_manager.get_all_cookies(user_id))", source)
        self.assertNotIn("reset_mock()", source)

    def test_notification_template_runtime_uses_user_scope_contract(self):
        reply_server_source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")
        dispatcher_source = (REPO_ROOT / "utils" / "notification_dispatcher.py").read_text(encoding="utf-8")
        xianyu_async_source = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")

        self.assertIn("db_manager.get_all_notification_templates(current_user['user_id'])", reply_server_source)
        self.assertIn(
            "db_manager.get_notification_template(template_type, user_id=current_user['user_id'])",
            reply_server_source,
        )
        self.assertNotIn("INSERT INTO notification_templates (type, template)", reply_server_source)
        self.assertIn("resolved_owner_user_id", dispatcher_source)
        self.assertIn("user_id=resolved_owner_user_id", dispatcher_source)
        self.assertIn("owner_account_id=current_account_id", xianyu_async_source)

    def test_notification_template_test_route_reuses_shared_channel_sender(self):
        reply_server_source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("channel_sent = await send_channel_notification(", reply_server_source)
        self.assertNotIn("async with session.post(webhook_url, json=payload)", reply_server_source)
        self.assertNotIn("async with session.get(api_url, params=params)", reply_server_source)
        self.assertNotIn("if normalized_channel_type == 'feishu':", reply_server_source)
        self.assertNotIn("elif normalized_channel_type == 'dingtalk':", reply_server_source)

    def test_list_scheduled_tasks_surfaces_database_failures_instead_of_empty_success(self):
        async def invoke():
            return await reply_server.list_scheduled_tasks(
                current_user={"user_id": 7, "username": "tester"},
            )

        with mock.patch.object(
            reply_server.db_manager,
            "get_scheduled_tasks",
            side_effect=RuntimeError("scheduled task exploded"),
        ):
            result = asyncio.run(invoke())

        self.assertFalse(result["success"])
        self.assertIn("scheduled task exploded", result["message"])

    def test_get_all_message_notifications_fallback_filters_to_owned_accounts(self):
        fake_db = mock.Mock()
        fake_db.get_all_message_notifications.side_effect = [
            [],
            {
                "acc-owned": [{"channel_id": 1, "channel_config": "{\"token\":\"secret\"}"}],
                "acc-other": [{"channel_id": 2, "channel_config": "{\"token\":\"other\"}"}],
            },
        ]
        fake_db.get_account_ids.return_value = ["acc-owned"]

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "_is_real_db_manager_instance", return_value=False):
            result = reply_server.get_all_message_notifications(
                current_user={"user_id": 7, "username": "tester"},
            )

        self.assertEqual(["acc-owned"], list(result.keys()))
        self.assertEqual("****", json.loads(result["acc-owned"][0]["channel_config"])["token"])
        fake_db.get_account_ids.assert_called_once_with(7)
        self.assertEqual(2, fake_db.get_all_message_notifications.call_count)

    def test_reload_cache_treats_reload_without_exception_as_success(self):
        fake_cookie_manager = mock.Mock()
        fake_cookie_manager.reload_from_db.return_value = None

        with mock.patch.object(reply_server.cookie_manager, "manager", fake_cookie_manager):
            result = reply_server.reload_cache(
                admin_user={"user_id": 1, "username": "admin"},
            )

        self.assertEqual({"message": "系统缓存已刷新", "success": True}, result)
        fake_cookie_manager.reload_from_db.assert_called_once_with()

    def test_reply_server_lifecycle_hooks_account_browser_runtime_janitor(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn('async def account_browser_runtime_janitor(', source)
        self.assertIn('async def start_account_browser_runtime_janitor()', source)
        self.assertIn('app.state.account_browser_runtime_janitor_task', source)
        self.assertIn('async def stop_account_browser_runtime_janitor()', source)
        self.assertIn('@app.put(\'/default-replies/{account_id}\')', source)
        self.assertIn('@app.delete(\'/default-replies/{account_id}\')', source)
        self.assertIn('@app.post(\'/default-replies/{account_id}/clear-records\')', source)
        self.assertIn('@app.get(\'/message-notifications/{account_id}\')', source)
        self.assertIn('@app.post(\'/message-notifications/{account_id}\')', source)
        self.assertIn('@app.put(\'/message-notifications/{account_id}/replace\')', source)
        self.assertIn('@app.delete(\'/message-notifications/account/{account_id}\')', source)
        self.assertIn('@app.put(\'/user-settings/menu-settings/replace\')', source)
        self.assertIn('@app.get("/keywords/{account_id}")', source)
        self.assertIn('@app.get("/keywords/counts")', source)
        self.assertIn('@app.get("/keywords-with-item-id/{account_id}")', source)
        self.assertIn('@app.post("/keywords/{account_id}")', source)
        self.assertIn('@app.post("/keywords-with-item-id/{account_id}")', source)
        self.assertIn('@app.get("/items/account/{account_id}")', source)
        self.assertIn('@app.get("/keywords-export/{account_id}")', source)
        self.assertIn('@app.post("/keywords-import/{account_id}")', source)
        self.assertIn('@app.post("/keywords/{account_id}/image")', source)
        self.assertIn('@app.post("/keywords/{account_id}/image-batch")', source)
        self.assertIn('@app.get("/keywords-with-type/{account_id}")', source)
        self.assertIn('@app.delete("/keywords/{account_id}/{index}")', source)
        self.assertNotIn('{cid}', source)

    def test_frontend_items_requests_use_single_account_scoped_contract(self):
        source = (REPO_ROOT / "static/js/app.js").read_text(encoding="utf-8")

        self.assertNotIn('`${apiBase}/items/${accountId}`', source)
        self.assertIn('`${apiBase}/items/account/${encodeURIComponent(accountId)}`', source)

    def test_order_routes_use_account_id_db_boundary(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("db_manager.get_orders_by_account(account_id, limit=None)", source)
        self.assertNotIn("db_manager.get_orders_by_account(account_id, limit=1000)", source)
        self.assertNotIn("db_manager.get_orders_by_cookie(account_id, limit=1000)", source)
        self.assertNotIn("or normalized_order.get('cookie_id')", source)
        self.assertNotIn("cookie_id=account_id", source)
        self.assertNotIn("cookie_id = order.get('cookie_id')", source)

    def test_order_action_routes_require_account_id_scoped_lookup(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("def _get_scoped_order_for_current_user(", source)
        self.assertIn("db_manager.get_order_by_id(order_id, account_id=normalized_account_id, user_id=user_id)", source)
        self.assertIn("def delete_user_order(", source)
        self.assertIn("async def manual_deliver_order(", source)
        self.assertIn("async def refresh_order_status(", source)
        self.assertIn("account_id: str = None,", source)
        self.assertNotIn("order = db_manager.get_order_by_id(order_id, user_id=user_id)", source)
        self.assertIn("_get_scoped_order_for_current_user(order_id, account_id, current_user", source)

    def test_sales_queries_use_orders_account_id_column(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("FROM orders WHERE account_id IN", source)
        self.assertIn("AND account_id IN", source)
        self.assertNotIn("FROM orders WHERE cookie_id IN", source)
        self.assertNotIn("AND cookie_id IN", source)

    def test_delivery_chain_uses_account_id_contract(self):
        reply_server = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")
        xianyu_async = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")
        order_event_hub = (REPO_ROOT / "order_event_hub.py").read_text(encoding="utf-8")

        self.assertNotIn("cookie_id=order_account_id", reply_server)
        self.assertNotIn("cookie_id=cookie_id", reply_server)
        self.assertIn("account_id=order_account_id", reply_server)
        self.assertIn("expected_quantity=expected_quantity,", reply_server)
        self.assertIn("log['account_id'] = str(log.get('account_id') or '').strip()", reply_server)
        self.assertNotIn("log.pop('cookie_id', None)", reply_server)
        self.assertNotIn("current_account_id = self._current_account_id()", xianyu_async)
        self.assertIn("canonical_account_id = self._canonical_account_id()", xianyu_async)
        self.assertIn("db_manager.create_delivery_log(\n                user_id=self.user_id,\n                account_id=current_account_id,", xianyu_async)
        self.assertNotIn("db_manager.create_delivery_log(\n                user_id=self.user_id,\n                account_id=self.cookie_id,", xianyu_async)
        self.assertIn("return db_manager.upsert_delivery_finalization_state(\n            order_id=normalized_order_id,\n            unit_index=unit_index,\n            account_id=current_account_id,", xianyu_async)
        self.assertNotIn("return db_manager.upsert_delivery_finalization_state(\n            order_id=normalized_order_id,\n            unit_index=unit_index,\n            account_id=self.cookie_id,", xianyu_async)
        self.assertIn("summary = self._summarize_delivery_progress(normalized_order_id, expected_quantity=expected_quantity)", xianyu_async)
        self.assertIn("account_id=canonical_account_id,", xianyu_async)
        self.assertIn("publish_order_update_event(order_id, account_id=order_account_id, source='manual_delivery_finalize')", reply_server)
        self.assertIn("publish_order_update_event(order_id, account_id=order_account_id, source='manual_delivery')", reply_server)
        self.assertIn("publish_order_update_event(\n                        normalized_order_id,\n                        account_id=canonical_account_id,\n                        source='delivery_progress_sync',", xianyu_async)
        self.assertIn("def publish_order_update_event(", order_event_hub)
        self.assertIn("account_id: str = None,", order_event_hub)
        self.assertNotIn("db_manager.get_order_by_id(order_id)", order_event_hub)
        self.assertIn("data_reservation = db_manager.reserve_batch_data(\n                        card_id=rule['card_id'],\n                        order_id=order_id,\n                        unit_index=delivery_unit_index,\n                        account_id=current_account_id,", xianyu_async)
        self.assertNotIn("data_reservation = db_manager.reserve_batch_data(\n                        card_id=rule['card_id'],\n                        order_id=order_id,\n                        unit_index=delivery_unit_index,\n                        cookie_id=self.cookie_id,", xianyu_async)


class FrontendAccountScopeContractTest(unittest.TestCase):
    def test_frontend_global_state_uses_account_id_names(self):
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn("let currentAccountId = '';", app_js)
        self.assertIn("let editAccountId = '';", app_js)
        self.assertNotIn("let currentCookieId = '';", app_js)
        self.assertNotIn("let editCookieId = '';", app_js)

    def test_item_and_item_reply_dom_drop_cookie_id_aliases(self):
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn("item.account_id", app_js)
        self.assertIn("account_id: selectedAccountId", app_js)
        self.assertIn("account_id: checkbox.dataset.accountId", app_js)
        self.assertIn("document.getElementById('editItemAccountId').value = item.account_id;", app_js)
        self.assertIn("document.getElementById('editItemAccountIdDisplay').value = item.account_id;", app_js)
        self.assertIn("accountSelect.value = data.account_id || accountId;", app_js)
        self.assertIn("async function onAccountChangeForReply()", app_js)
        self.assertNotIn("item.cookie_id", app_js)
        self.assertNotIn("selectedCookieId", app_js)
        self.assertNotIn("editItemCookieId", app_js)
        self.assertNotIn("data.cookie_id", app_js)
        self.assertNotIn("async function onCookieChangeForReply()", app_js)


class ReplyServerAccountRuntimeIsolationTest(_ReplyServerModuleBindingMixin, unittest.IsolatedAsyncioTestCase):
    async def test_get_managed_live_instance_rejects_calls_outside_manager_loop(self):
        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(return_value=object()),
            loop=SimpleNamespace(
                is_closed=mock.Mock(return_value=False),
                is_running=mock.Mock(return_value=True),
            ),
        )

        with mock.patch.object(reply_server.cookie_manager, "manager", fake_manager):
            with self.assertRaisesRegex(RuntimeError, "manager loop"):
                reply_server._get_managed_live_instance("acc-runtime-guard-1")

        fake_manager.get_xianyu_instance.assert_not_called()

    async def test_build_live_runtime_status_does_not_fallback_to_xianyulive_global_registry(self):
        manager_loop_state = {"active": False}

        def get_instance_only_on_manager_loop(_account_id):
            if not manager_loop_state["active"]:
                raise AssertionError("runtime lookup should stay inside manager loop")
            return None

        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(side_effect=get_instance_only_on_manager_loop),
            loop=SimpleNamespace(
                is_closed=mock.Mock(return_value=False),
                is_running=mock.Mock(return_value=True),
            ),
        )

        async def execute_on_manager_loop(_account_id, coroutine_factory, timeout=None):
            manager_loop_state["active"] = True
            access_token = reply_server._MANAGED_LIVE_INSTANCE_ACCESS.set(True)
            try:
                return await coroutine_factory()
            finally:
                reply_server._MANAGED_LIVE_INSTANCE_ACCESS.reset(access_token)
                manager_loop_state["active"] = False

        with mock.patch.object(reply_server, "_run_live_instance_on_manager_loop", side_effect=execute_on_manager_loop), \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch("XianyuAutoAsync.XianyuLive.get_instance", side_effect=AssertionError("global fallback should stay unused")), \
             mock.patch("XianyuAutoAsync.XianyuLive.is_manual_refresh_active", return_value=False):
            runtime_status = await reply_server._build_live_runtime_status("acc-runtime-1")

        self.assertFalse(runtime_status["instance_exists"])
        self.assertFalse(runtime_status["running"])
        fake_manager.get_xianyu_instance.assert_called_once_with("acc-runtime-1")

    async def test_send_message_api_uses_managed_account_runtime_without_global_fallback(self):
        from XianyuAutoAsync import ConnectionState

        current_live = SimpleNamespace(
            connection_state=ConnectionState.CONNECTED,
            ws=object(),
            send_msg=mock.AsyncMock(),
        )
        manager_loop_state = {"active": False}

        def get_instance_only_on_manager_loop(_account_id):
            if not manager_loop_state["active"]:
                raise AssertionError("runtime lookup should stay inside manager loop")
            return current_live

        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(side_effect=get_instance_only_on_manager_loop),
            loop=SimpleNamespace(
                is_closed=mock.Mock(return_value=False),
                is_running=mock.Mock(return_value=True),
            ),
        )
        request = reply_server.SendMessageRequest(
            api_key="real-key",
            account_id="acc-send-1",
            chat_id="chat-send-1",
            to_user_id="buyer-send-1",
            message="hello",
        )

        async def execute_on_manager_loop(_account_id, coroutine_factory, timeout=None):
            manager_loop_state["active"] = True
            access_token = reply_server._MANAGED_LIVE_INSTANCE_ACCESS.set(True)
            try:
                return await coroutine_factory()
            finally:
                reply_server._MANAGED_LIVE_INSTANCE_ACCESS.reset(access_token)
                manager_loop_state["active"] = False

        with mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "verify_api_key", return_value=True), \
             mock.patch.object(reply_server, "_run_live_instance_on_manager_loop", side_effect=execute_on_manager_loop) as run_mock, \
             mock.patch("XianyuAutoAsync.XianyuLive.get_instance", side_effect=AssertionError("global fallback should stay unused")):
            response = await reply_server.send_message_api(request)

        self.assertTrue(response.success)
        self.assertEqual(fake_manager.get_xianyu_instance.call_count, 1)
        current_live.send_msg.assert_awaited_once()
        run_mock.assert_awaited_once()

    def test_verify_api_key_uses_module_bound_database_setting(self):
        fake_db = mock.Mock()
        fake_db.get_system_setting.return_value = "scoped-secret-key"
        shadow_db = mock.Mock()
        shadow_db.get_system_setting.side_effect = AssertionError("秘钥校验不该绕开 reply_server.db_manager")

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ):
            self.assertTrue(reply_server.verify_api_key("scoped-secret-key"))
            self.assertFalse(reply_server.verify_api_key("wrong-secret-key"))

        self.assertEqual(fake_db.get_system_setting.call_count, 2)

    async def test_send_message_api_preserves_multiline_message_content(self):
        from XianyuAutoAsync import ConnectionState

        current_live = SimpleNamespace(
            connection_state=ConnectionState.CONNECTED,
            ws=object(),
            send_msg=mock.AsyncMock(),
        )
        manager_loop_state = {"active": False}

        def get_instance_only_on_manager_loop(_account_id):
            if not manager_loop_state["active"]:
                raise AssertionError("runtime lookup should stay inside manager loop")
            return current_live

        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(side_effect=get_instance_only_on_manager_loop),
            loop=SimpleNamespace(
                is_closed=mock.Mock(return_value=False),
                is_running=mock.Mock(return_value=True),
            ),
        )
        request = reply_server.SendMessageRequest(
            api_key="real-key",
            account_id="acc-send-1",
            chat_id="chat-send-1",
            to_user_id="buyer-send-1",
            message="第一行\n第二行",
        )

        async def execute_on_manager_loop(_account_id, coroutine_factory, timeout=None):
            manager_loop_state["active"] = True
            access_token = reply_server._MANAGED_LIVE_INSTANCE_ACCESS.set(True)
            try:
                return await coroutine_factory()
            finally:
                reply_server._MANAGED_LIVE_INSTANCE_ACCESS.reset(access_token)
                manager_loop_state["active"] = False

        with mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "verify_api_key", return_value=True), \
             mock.patch.object(reply_server, "_run_live_instance_on_manager_loop", side_effect=execute_on_manager_loop), \
             mock.patch("XianyuAutoAsync.XianyuLive.get_instance", side_effect=AssertionError("global fallback should stay unused")):
            response = await reply_server.send_message_api(request)

        self.assertTrue(response.success)
        current_live.send_msg.assert_awaited_once()
        self.assertEqual(
            current_live.send_msg.await_args.args,
            (current_live.ws, "chat-send-1", "buyer-send-1", "第一行\n第二行"),
        )

    async def test_get_conversation_history_uses_managed_account_runtime_without_global_fallback(self):
        current_live = SimpleNamespace(
            list_all_conversations=mock.AsyncMock(return_value=[{"id": "m-1"}]),
        )
        manager_loop_state = {"active": False}

        def get_instance_only_on_manager_loop(_account_id):
            if not manager_loop_state["active"]:
                raise AssertionError("runtime lookup should stay inside manager loop")
            return current_live

        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(side_effect=get_instance_only_on_manager_loop),
            loop=SimpleNamespace(
                is_closed=mock.Mock(return_value=False),
                is_running=mock.Mock(return_value=True),
            ),
        )

        async def execute_on_manager_loop(_account_id, coroutine_factory, timeout=None):
            manager_loop_state["active"] = True
            access_token = reply_server._MANAGED_LIVE_INSTANCE_ACCESS.set(True)
            try:
                return await coroutine_factory()
            finally:
                reply_server._MANAGED_LIVE_INSTANCE_ACCESS.reset(access_token)
                manager_loop_state["active"] = False

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-history-1"), \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "log_with_user"), \
             mock.patch.object(reply_server, "_run_live_instance_on_manager_loop", side_effect=execute_on_manager_loop) as run_mock, \
             mock.patch("XianyuAutoAsync.XianyuLive.get_instance", side_effect=AssertionError("global fallback should stay unused")):
            response = await reply_server.get_conversation_history(
                "acc-history-1",
                "conv-history-1@ali",
                page_size=20,
                current_user={"user_id": 1},
            )

        self.assertTrue(response["success"])
        self.assertEqual(response["account_id"], "acc-history-1")
        self.assertEqual(response["conversation_id"], "conv-history-1")
        self.assertEqual(response["count"], 1)
        self.assertEqual(fake_manager.get_xianyu_instance.call_count, 2)
        current_live.list_all_conversations.assert_awaited_once_with(
            "conv-history-1",
            page_size=20,
        )
        self.assertEqual(run_mock.await_count, 2)

    async def test_trigger_session_keepalive_uses_managed_account_runtime_without_global_fallback(self):
        current_live = SimpleNamespace(
            keep_session_alive=mock.AsyncMock(return_value=True),
        )
        manager_loop_state = {"active": False}

        def get_instance_only_on_manager_loop(_account_id):
            if not manager_loop_state["active"]:
                raise AssertionError("runtime lookup should stay inside manager loop")
            return current_live

        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(side_effect=get_instance_only_on_manager_loop),
        )

        async def execute_on_manager_loop(_account_id, coroutine_factory, timeout=None):
            manager_loop_state["active"] = True
            access_token = reply_server._MANAGED_LIVE_INSTANCE_ACCESS.set(True)
            try:
                return await coroutine_factory()
            finally:
                reply_server._MANAGED_LIVE_INSTANCE_ACCESS.reset(access_token)
                manager_loop_state["active"] = False

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-keepalive-1"), \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "log_with_user"), \
             mock.patch.object(reply_server, "_run_live_instance_on_manager_loop", side_effect=execute_on_manager_loop) as run_mock, \
             mock.patch("XianyuAutoAsync.XianyuLive.get_instance", side_effect=AssertionError("global fallback should stay unused")):
            response = await reply_server.trigger_session_keepalive(
                "acc-keepalive-1",
                current_user={"user_id": 1},
            )

        self.assertTrue(response["success"])
        self.assertEqual(response["account_id"], "acc-keepalive-1")
        self.assertEqual(fake_manager.get_xianyu_instance.call_count, 2)
        current_live.keep_session_alive.assert_awaited_once_with()
        self.assertEqual(run_mock.await_count, 2)

    async def test_trigger_runtime_token_refresh_routes_via_managed_account_runtime(self):
        current_live = SimpleNamespace(
            refresh_token=mock.AsyncMock(return_value="token-debug-1"),
            last_token_refresh_status="success",
            last_token_refresh_error_message=None,
        )
        manager_loop_state = {"active": False}

        def get_instance_only_on_manager_loop(_account_id):
            if not manager_loop_state["active"]:
                raise AssertionError("runtime lookup should stay inside manager loop")
            return current_live

        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(side_effect=get_instance_only_on_manager_loop),
        )

        async def execute_on_manager_loop(_account_id, coroutine_factory, timeout=None):
            manager_loop_state["active"] = True
            access_token = reply_server._MANAGED_LIVE_INSTANCE_ACCESS.set(True)
            try:
                return await coroutine_factory()
            finally:
                reply_server._MANAGED_LIVE_INSTANCE_ACCESS.reset(access_token)
                manager_loop_state["active"] = False

        with mock.patch.object(reply_server, "cookie_manager", SimpleNamespace(manager=fake_manager)), \
             mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-refresh-1"), \
             mock.patch.object(reply_server, "_run_live_instance_on_manager_loop", side_effect=execute_on_manager_loop) as run_mock, \
             mock.patch.object(reply_server, "_build_live_runtime_status", mock.AsyncMock(return_value={"running": True})), \
             mock.patch.object(reply_server, "log_with_user"):
            response = await reply_server.trigger_runtime_token_refresh(
                "acc-refresh-1",
                request=reply_server.RuntimeTokenRefreshRequest(),
                current_user={"user_id": 1},
            )

        self.assertTrue(response["success"])
        self.assertEqual(response["account_id"], "acc-refresh-1")
        self.assertFalse(response["simulate_captcha"])
        self.assertEqual(response["result"]["path"], "refresh_token")
        self.assertEqual(fake_manager.get_xianyu_instance.call_count, 1)
        current_live.refresh_token.assert_awaited_once_with(allow_password_login_recovery=True)
        self.assertEqual(run_mock.await_count, 1)

    async def test_account_runtime_routes_stop_masking_runtime_or_status_failures_as_server_errors(self):
        current_user = {"user_id": 1}

        async def assert_http_exception(awaitable_factory, expected_status, expected_detail):
            with self.assertRaises(reply_server.HTTPException) as raised:
                await awaitable_factory()
            self.assertEqual(expected_status, raised.exception.status_code)
            self.assertEqual(expected_detail, raised.exception.detail)

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-runtime-error"):
            with mock.patch.object(
                reply_server,
                "_build_live_runtime_status",
                mock.AsyncMock(side_effect=RuntimeError("runtime status exploded")),
            ):
                await assert_http_exception(
                    lambda: reply_server.get_cookie_runtime_status(
                        "acc-runtime-error",
                        current_user=current_user,
                    ),
                    500,
                    "获取账号运行态失败，请稍后重试",
                )

            with mock.patch.object(
                reply_server,
                "_run_managed_live_instance_call",
                mock.AsyncMock(side_effect=RuntimeError("managed runtime exploded")),
            ), mock.patch.object(reply_server, "log_with_user"):
                await assert_http_exception(
                    lambda: reply_server.get_conversation_history(
                        "acc-runtime-error",
                        "conv-runtime-error@ali",
                        page_size=20,
                        current_user=current_user,
                    ),
                    500,
                    "获取历史消息失败，请稍后重试",
                )
                await assert_http_exception(
                    lambda: reply_server.trigger_session_keepalive(
                        "acc-runtime-error",
                        current_user=current_user,
                    ),
                    500,
                    "手动轻量保活失败，请稍后重试",
                )
                await assert_http_exception(
                    lambda: reply_server.trigger_runtime_token_refresh(
                        "acc-runtime-error",
                        request=reply_server.RuntimeTokenRefreshRequest(),
                        current_user=current_user,
                    ),
                    500,
                    "手动触发 Token 刷新失败，请稍后重试",
                )

    async def test_trigger_runtime_token_refresh_simulated_captcha_uses_debug_recovery(self):
        current_live = SimpleNamespace(
            debug_force_captcha_recovery=mock.AsyncMock(
                return_value={
                    "success": True,
                    "path": "simulated_captcha_password_login_recovery",
                    "token_received": True,
                }
            ),
        )
        manager_loop_state = {"active": False}

        def get_instance_only_on_manager_loop(_account_id):
            if not manager_loop_state["active"]:
                raise AssertionError("runtime lookup should stay inside manager loop")
            return current_live

        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(side_effect=get_instance_only_on_manager_loop),
        )

        async def execute_on_manager_loop(_account_id, coroutine_factory, timeout=None):
            manager_loop_state["active"] = True
            access_token = reply_server._MANAGED_LIVE_INSTANCE_ACCESS.set(True)
            try:
                return await coroutine_factory()
            finally:
                reply_server._MANAGED_LIVE_INSTANCE_ACCESS.reset(access_token)
                manager_loop_state["active"] = False

        with mock.patch.object(reply_server, "cookie_manager", SimpleNamespace(manager=fake_manager)), \
             mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-refresh-2"), \
             mock.patch.object(reply_server, "_run_live_instance_on_manager_loop", side_effect=execute_on_manager_loop), \
             mock.patch.object(reply_server, "_build_live_runtime_status", mock.AsyncMock(return_value={"running": True})), \
             mock.patch.object(reply_server, "log_with_user"):
            response = await reply_server.trigger_runtime_token_refresh(
                "acc-refresh-2",
                request=reply_server.RuntimeTokenRefreshRequest(simulate_captcha=True),
                current_user={"user_id": 1},
            )

        self.assertTrue(response["success"])
        self.assertTrue(response["simulate_captcha"])
        self.assertIn("debug_acc-refresh-2", response["verification_url"])
        current_live.debug_force_captcha_recovery.assert_awaited_once()

    async def test_reset_qr_cookie_refresh_cooldown_routes_runtime_call_via_manager_loop(self):
        live_instance = SimpleNamespace(
            get_qr_cookie_refresh_remaining_time=mock.Mock(return_value=123),
            reset_qr_cookie_refresh_flag=mock.Mock(),
        )
        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(return_value=live_instance),
            loop=SimpleNamespace(
                is_closed=mock.Mock(return_value=False),
                is_running=mock.Mock(return_value=True),
            ),
        )

        async def execute_on_manager_loop(_account_id, coroutine_factory, timeout=None):
            access_token = reply_server._MANAGED_LIVE_INSTANCE_ACCESS.set(True)
            try:
                return await coroutine_factory()
            finally:
                reply_server._MANAGED_LIVE_INSTANCE_ACCESS.reset(access_token)

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-qr-reset-1"), \
             mock.patch.object(reply_server.db_manager, "get_cookie_by_id", return_value={"id": "acc-qr-reset-1"}), \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "log_with_user"), \
             mock.patch.object(reply_server, "_run_live_instance_on_manager_loop", side_effect=execute_on_manager_loop) as run_mock:
            response = await reply_server.reset_qr_cookie_refresh_cooldown(
                "acc-qr-reset-1",
                current_user={"user_id": 1},
            )

        self.assertTrue(response["success"])
        self.assertEqual(response["account_id"], "acc-qr-reset-1")
        self.assertEqual(response["previous_remaining_time"], 123)
        live_instance.get_qr_cookie_refresh_remaining_time.assert_called_once_with()
        live_instance.reset_qr_cookie_refresh_flag.assert_called_once_with()
        run_mock.assert_awaited_once()

    async def test_get_qr_cookie_refresh_cooldown_status_routes_runtime_call_via_manager_loop(self):
        live_instance = SimpleNamespace(
            get_qr_cookie_refresh_remaining_time=mock.Mock(return_value=95),
            qr_cookie_refresh_cooldown=600,
            last_qr_cookie_refresh_time=1234567890,
        )
        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(return_value=live_instance),
            loop=SimpleNamespace(
                is_closed=mock.Mock(return_value=False),
                is_running=mock.Mock(return_value=True),
            ),
        )

        async def execute_on_manager_loop(_account_id, coroutine_factory, timeout=None):
            access_token = reply_server._MANAGED_LIVE_INSTANCE_ACCESS.set(True)
            try:
                return await coroutine_factory()
            finally:
                reply_server._MANAGED_LIVE_INSTANCE_ACCESS.reset(access_token)

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-qr-status-1"), \
             mock.patch.object(reply_server.db_manager, "get_cookie_by_id", return_value={"id": "acc-qr-status-1"}), \
             mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "_run_live_instance_on_manager_loop", side_effect=execute_on_manager_loop) as run_mock:
            response = await reply_server.get_qr_cookie_refresh_cooldown_status(
                "acc-qr-status-1",
                current_user={"user_id": 1},
            )

        self.assertTrue(response["success"])
        self.assertEqual(response["account_id"], "acc-qr-status-1")
        self.assertEqual(response["remaining_time"], 95)
        self.assertEqual(response["cooldown_duration"], 600)
        self.assertEqual(response["last_refresh_time"], 1234567890)
        self.assertTrue(response["is_in_cooldown"])
        self.assertEqual(response["remaining_minutes"], 1)
        self.assertEqual(response["remaining_seconds"], 35)
        live_instance.get_qr_cookie_refresh_remaining_time.assert_called_once_with()
        run_mock.assert_awaited_once()

    async def test_process_qr_login_cookies_uses_target_cookie_snapshot_instead_of_full_cookie_scan(self):
        class FakeXianyuLive:
            def __init__(self, *args, **kwargs):
                self.refresh_cookies_from_qr_login = mock.AsyncMock(return_value=False)

        fake_db = mock.Mock()
        fake_db.get_cookie_binding_info.return_value = {
            "account_id": "acc-qr-chain-1",
            "user_id": 7,
            "bound_unb": "",
            "bind_status": "active",
        }
        fake_db.assert_cookie_belongs_to_user = mock.Mock(return_value=True)
        fake_db.get_all_cookies.side_effect = AssertionError("扫码登录链路不该全量解密当前用户 Cookie")
        fake_db.get_cookie.return_value = ""
        fake_db.add_risk_control_log.return_value = None

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch("XianyuAutoAsync.XianyuLive", FakeXianyuLive), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(RuntimeError) as raised:
                await reply_server.process_qr_login_cookies(
                    "acc-qr-chain-1",
                    "qr-cookie-value",
                    "unb-demo",
                    current_user={"user_id": 7, "username": "demo-user"},
                )

        self.assertEqual("扫码登录未完成：获取真实Cookie异常: 扫码登录未完成：真实Cookie获取失败", str(raised.exception))
        fake_db.get_cookie_binding_info.assert_called_once_with("acc-qr-chain-1")
        fake_db.assert_cookie_belongs_to_user.assert_called_once_with("acc-qr-chain-1", 7)
        fake_db.get_cookie.assert_called_once_with("acc-qr-chain-1")

    async def test_check_valid_accounts_scopes_counts_to_current_user(self):
        fake_manager = SimpleNamespace(
            get_cookie_status=mock.Mock(side_effect=lambda account_id: account_id == "acc-owned-valid"),
        )

        with mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(
                 reply_server.db_manager,
                 "get_account_ids",
                 return_value=["acc-owned-valid", "acc-owned-disabled"],
             ) as get_account_ids, \
             mock.patch.object(
                 reply_server.db_manager,
                 "get_cookie",
                 side_effect=lambda account_id: "x" * 51 if account_id == "acc-owned-valid" else "y" * 51,
             ) as get_cookie:
            response = await reply_server.check_valid_accounts(
                current_user={"user_id": 321, "username": "owner"},
            )

        self.assertTrue(response["success"])
        self.assertTrue(response["hasValidAccounts"])
        self.assertEqual(response["validAccountCount"], 1)
        self.assertEqual(response["enabledAccountCount"], 1)
        self.assertEqual(response["totalAccountCount"], 2)
        get_account_ids.assert_called_once_with(321)
        get_cookie.assert_called_once_with("acc-owned-valid")
        fake_manager.get_cookie_status.assert_has_calls(
            [mock.call("acc-owned-valid"), mock.call("acc-owned-disabled")]
        )

    async def test_check_valid_accounts_fallbacks_to_database_status_when_cookie_manager_unready(self):
        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server.db_manager,
            "get_account_ids",
            return_value=["acc-owned-valid", "acc-owned-disabled", "acc-owned-short"],
        ) as get_account_ids, mock.patch.object(
            reply_server.db_manager,
            "get_cookie",
            side_effect=lambda account_id: {
                "acc-owned-valid": "x" * 51,
                "acc-owned-disabled": "y" * 51,
                "acc-owned-short": "z" * 20,
            }[account_id],
        ) as get_cookie, mock.patch.object(
            reply_server.db_manager,
            "get_cookie_status",
            side_effect=lambda account_id: account_id != "acc-owned-disabled",
        ) as get_cookie_status:
            response = await reply_server.check_valid_accounts(
                current_user={"user_id": 321, "username": "owner"},
            )

        self.assertTrue(response["success"])
        self.assertTrue(response["hasValidAccounts"])
        self.assertEqual(response["validAccountCount"], 1)
        self.assertEqual(response["enabledAccountCount"], 2)
        self.assertEqual(response["totalAccountCount"], 3)
        get_account_ids.assert_called_once_with(321)
        get_cookie.assert_has_calls(
            [mock.call("acc-owned-valid"), mock.call("acc-owned-short")]
        )
        get_cookie_status.assert_has_calls(
            [
                mock.call("acc-owned-valid"),
                mock.call("acc-owned-disabled"),
                mock.call("acc-owned-short"),
            ]
        )

    async def test_check_valid_accounts_rejects_disabled_selected_account_for_item_search_preflight(self):
        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server.db_manager,
            "get_account_ids",
            return_value=["acc-owned-disabled", "acc-owned-short"],
        ) as get_account_ids, mock.patch.object(
            reply_server.db_manager,
            "get_cookie",
            return_value="y" * 51,
        ) as get_cookie, mock.patch.object(
            reply_server.db_manager,
            "get_cookie_status",
            return_value=False,
        ) as get_cookie_status:
            response = await reply_server.check_valid_accounts(
                account_id="acc-owned-disabled",
                current_user={"user_id": 321, "username": "owner"},
            )

        self.assertTrue(response["success"])
        self.assertFalse(response["hasValidAccounts"])
        self.assertEqual(response["validAccountCount"], 0)
        self.assertEqual(response["enabledAccountCount"], 0)
        self.assertEqual(response["totalAccountCount"], 1)
        self.assertEqual(response["checkedAccountId"], "acc-owned-disabled")
        self.assertFalse(response["checkedAccountEnabled"])
        get_account_ids.assert_called_once_with(321)
        get_cookie.assert_called_once_with("acc-owned-disabled")
        get_cookie_status.assert_called_once_with("acc-owned-disabled")

    async def test_check_valid_accounts_without_current_user_returns_empty_counts(self):
        fake_manager = SimpleNamespace(get_cookie_status=mock.Mock())

        with mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(
                 reply_server.db_manager,
                 "get_account_ids",
                 side_effect=AssertionError("anonymous accounts check must not read account ids"),
             ), mock.patch.object(
                 reply_server.db_manager,
                 "get_cookie",
                 side_effect=AssertionError("anonymous accounts check must not read cookies"),
             ):
            response = await reply_server.check_valid_accounts(current_user=None)

        self.assertTrue(response["success"])
        self.assertFalse(response["hasValidAccounts"])
        self.assertEqual(response["validAccountCount"], 0)
        self.assertEqual(response["enabledAccountCount"], 0)
        self.assertEqual(response["totalAccountCount"], 0)
        fake_manager.get_cookie_status.assert_not_called()

    async def test_check_valid_accounts_uses_module_bound_database(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-owned-disabled"]
        fake_db.get_cookie.return_value = "y" * 51
        fake_db.get_cookie_status.return_value = False

        shadow_db = mock.Mock()
        shadow_db.get_account_ids.side_effect = AssertionError("商品搜索账号预检不该绕开 reply_server.db_manager")
        shadow_db.get_cookie.side_effect = AssertionError("商品搜索账号 Cookie 读取不该绕开 reply_server.db_manager")
        shadow_db.get_cookie_status.side_effect = AssertionError("商品搜索账号状态读取不该绕开 reply_server.db_manager")

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ), mock.patch.object(reply_server.cookie_manager, "manager", None):
            response = await reply_server.check_valid_accounts(
                account_id="acc-owned-disabled",
                current_user={"user_id": 321, "username": "owner"},
            )

        self.assertTrue(response["success"])
        self.assertFalse(response["hasValidAccounts"])
        self.assertEqual(response["validAccountCount"], 0)
        self.assertEqual(response["enabledAccountCount"], 0)
        self.assertEqual(response["totalAccountCount"], 1)
        self.assertEqual(response["checkedAccountId"], "acc-owned-disabled")
        self.assertFalse(response["checkedAccountEnabled"])
        fake_db.get_account_ids.assert_called_once_with(321)
        fake_db.get_cookie.assert_called_once_with("acc-owned-disabled")
        fake_db.get_cookie_status.assert_called_once_with("acc-owned-disabled")

    async def test_check_valid_accounts_stops_masking_database_failures_as_empty_preflight(self):
        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server.db_manager,
            "get_account_ids",
            return_value=["acc-owned-disabled"],
        ) as get_account_ids, mock.patch.object(
            reply_server.db_manager,
            "get_cookie_status",
            return_value=False,
        ) as get_cookie_status, mock.patch.object(
            reply_server.db_manager,
            "get_cookie",
            side_effect=RuntimeError("cookie preflight exploded"),
        ) as get_cookie:
            with self.assertRaises(reply_server.HTTPException) as raised:
                await reply_server.check_valid_accounts(
                    account_id="acc-owned-disabled",
                    current_user={"user_id": 321, "username": "owner"},
                )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "检查账号状态失败: cookie preflight exploded")
        get_account_ids.assert_called_once_with(321)
        get_cookie_status.assert_called_once_with("acc-owned-disabled")
        get_cookie.assert_called_once_with("acc-owned-disabled")

    async def test_health_check_surfaces_internal_probe_failures_as_503(self):
        with mock.patch.object(reply_server.cookie_manager, "manager", object()), \
             mock.patch("db_manager.db_manager.get_all_cookies", return_value={}), \
             mock.patch("psutil.cpu_percent", side_effect=RuntimeError("psutil probe exploded")):
            with self.assertRaises(reply_server.HTTPException) as raised:
                await reply_server.health_check()

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["status"], "unhealthy")
        self.assertEqual(raised.exception.detail["error"], "psutil probe exploded")

    def test_item_search_multiple_request_rejects_total_pages_outside_supported_range(self):
        with self.assertRaises(ValidationError):
            reply_server.ItemSearchMultipleRequest(
                account_id="acc-item-search-1",
                keyword="switch",
                total_pages=0,
            )

        with self.assertRaises(ValidationError):
            reply_server.ItemSearchMultipleRequest(
                account_id="acc-item-search-1",
                keyword="switch",
                total_pages=21,
            )

        request = reply_server.ItemSearchMultipleRequest(
            account_id="acc-item-search-1",
            keyword="switch",
            total_pages=20,
        )
        self.assertEqual(request.total_pages, 20)

    def test_item_search_request_rejects_invalid_page_and_page_size_ranges(self):
        with self.assertRaises(ValidationError):
            reply_server.ItemSearchRequest(
                account_id="acc-item-search-1",
                keyword="switch",
                page=0,
                page_size=20,
            )

        with self.assertRaises(ValidationError):
            reply_server.ItemSearchRequest(
                account_id="acc-item-search-1",
                keyword="switch",
                page=1,
                page_size=0,
            )

        with self.assertRaises(ValidationError):
            reply_server.ItemSearchRequest(
                account_id="acc-item-search-1",
                keyword="switch",
                page=1,
                page_size=101,
            )

        request = reply_server.ItemSearchRequest(
            account_id="acc-item-search-1",
            keyword="switch",
            page=2,
            page_size=100,
        )
        self.assertEqual(request.page, 2)
        self.assertEqual(request.page_size, 100)

    async def test_optional_managed_live_instance_call_raises_when_manager_runtime_unavailable(self):
        with mock.patch.object(reply_server, "_get_cookie_manager_runtime_issue", return_value="task_manager_loop_closed"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                await reply_server._run_managed_live_instance_optional_call(
                    "acc-runtime-issue-1",
                    lambda _live_instance: True,
                )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "账号事件循环已关闭")

    async def test_reset_qr_cookie_refresh_cooldown_re_raises_http_exception(self):
        with mock.patch.object(
            reply_server,
            "_ensure_account_access",
            side_effect=reply_server.HTTPException(status_code=403, detail="无权限访问该账号"),
        ):
            with self.assertRaises(reply_server.HTTPException) as raised:
                await reply_server.reset_qr_cookie_refresh_cooldown(
                    "acc-qr-reset-foreign",
                    current_user={"user_id": 1},
                )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "无权限访问该账号")

    async def test_qr_cookie_refresh_cooldown_routes_stop_masking_database_failures_as_account_missing(self):
        current_user = {"user_id": 1}

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-qr-db-error"), \
             mock.patch.object(
                 reply_server.db_manager,
                 "get_cookie_by_id",
                 side_effect=RuntimeError("qr account lookup exploded"),
             ), \
             mock.patch.object(reply_server, "log_with_user"):
            reset_response = await reply_server.reset_qr_cookie_refresh_cooldown(
                "acc-qr-db-error",
                current_user=current_user,
            )
            status_response = await reply_server.get_qr_cookie_refresh_cooldown_status(
                "acc-qr-db-error",
                current_user=current_user,
            )

        self.assertEqual(
            {"success": False, "message": "重置冷却时间失败: qr account lookup exploded"},
            reset_response,
        )
        self.assertEqual(
            {"success": False, "message": "获取冷却状态失败: qr account lookup exploded"},
            status_response,
        )

    async def test_get_qr_cookie_refresh_cooldown_status_re_raises_http_exception(self):
        with mock.patch.object(
            reply_server,
            "_ensure_account_access",
            side_effect=reply_server.HTTPException(status_code=403, detail="无权限访问该账号"),
        ):
            with self.assertRaises(reply_server.HTTPException) as raised:
                await reply_server.get_qr_cookie_refresh_cooldown_status(
                    "acc-qr-status-foreign",
                    current_user={"user_id": 1},
                )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "无权限访问该账号")

    async def test_manual_deliver_order_routes_live_calls_via_managed_helper(self):
        fake_db = mock.Mock()
        fake_db.get_item_info.return_value = {"item_title": "测试商品"}
        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(side_effect=AssertionError("direct live instance lookup should stay unused")),
        )
        managed_call = mock.AsyncMock(return_value={"success": True, "delivered": True, "message": "ok"})
        order = {
            "order_id": "order-deliver-2",
            "account_id": "acc-deliver-2",
            "item_id": "item-deliver-2",
            "buyer_id": "buyer-deliver-2",
            "quantity": 1,
        }

        with mock.patch.object(reply_server, "_get_scoped_order_for_current_user", return_value=("acc-deliver-2", order)), \
             mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch("cookie_manager.manager", fake_manager), \
             mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
             mock.patch.object(reply_server, "log_with_user"):
            result = await reply_server.manual_deliver_order(
                "order-deliver-2",
                account_id="acc-deliver-2",
                current_user={"user_id": 1, "username": "admin"},
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["delivered"])
        fake_db.get_item_info.assert_called_once_with("acc-deliver-2", "item-deliver-2")
        managed_call.assert_awaited_once()
        self.assertEqual(managed_call.await_args.args[0], "acc-deliver-2")
        self.assertTrue(callable(managed_call.await_args.args[1]))
        self.assertEqual(
            managed_call.await_args.kwargs["missing_detail"],
            "账号 acc-deliver-2 未运行，请先启动账号",
        )

    async def test_refresh_order_status_routes_runtime_call_via_managed_helper(self):
        fake_db = mock.Mock()
        fake_db.get_order_by_id.side_effect = [
            {
                "order_id": "order-refresh-2",
                "account_id": "acc-refresh-2",
                "order_status": "pending_ship",
                "item_id": "item-refresh-2",
                "buyer_id": "buyer-refresh-2",
                "sid": "sid-refresh-2",
            },
            {
                "order_id": "order-refresh-2",
                "account_id": "acc-refresh-2",
                "order_status": "shipped",
            },
        ]
        fake_manager = SimpleNamespace(
            get_xianyu_instance=mock.Mock(side_effect=AssertionError("direct live instance lookup should stay unused")),
        )
        managed_call = mock.AsyncMock(return_value=True)

        with mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"acc-refresh-2": "cookie"}), \
             mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch("cookie_manager.manager", fake_manager), \
             mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
             mock.patch.object(reply_server, "log_with_user"):
            result = await reply_server.refresh_order_status(
                "order-refresh-2",
                account_id="acc-refresh-2",
                current_user={"user_id": 1, "username": "admin"},
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["updated"])
        self.assertEqual(result["new_status"], "shipped")
        managed_call.assert_awaited_once()
        self.assertEqual(managed_call.await_args.args[0], "acc-refresh-2")
        self.assertTrue(callable(managed_call.await_args.args[1]))
        self.assertEqual(
            managed_call.await_args.kwargs["missing_detail"],
            "账号 acc-refresh-2 未运行，请先启动账号",
        )

    async def test_start_order_history_sync_rejects_invalid_request_before_creating_background_job(self):
        invalid_cases = [
            (
                {
                    "account_id": "acc-history-start-1",
                    "start_date": "2026/05/01",
                    "end_date": "2026-05-02",
                    "max_orders": 120,
                    "fetch_details": True,
                },
                "日期格式错误，应为 YYYY-MM-DD",
            ),
            (
                {
                    "account_id": "acc-history-start-2",
                    "start_date": "2026-05-03",
                    "end_date": "2026-05-02",
                    "max_orders": 120,
                    "fetch_details": True,
                },
                "开始日期必须早于结束日期",
            ),
            (
                {
                    "account_id": "acc-history-start-3",
                    "start_date": "2026-05-01",
                    "end_date": "2026-05-02",
                    "max_orders": 501,
                    "fetch_details": True,
                },
                "最多同步单数需在 1 到 500 之间",
            ),
        ]

        for request_kwargs, expected_detail in invalid_cases:
            with self.subTest(request_kwargs=request_kwargs):
                before_job_ids = set(reply_server.order_history_sync_jobs.keys())
                request = reply_server.OrderHistorySyncRequest(**request_kwargs)

                with mock.patch.object(reply_server, "_cleanup_order_history_sync_jobs") as cleanup_mock, \
                     mock.patch.object(reply_server.asyncio, "create_task") as create_task_mock, \
                     mock.patch.object(reply_server, "log_with_user"):
                    with self.assertRaises(reply_server.HTTPException) as ctx:
                        await reply_server.start_order_history_sync(
                            request,
                            current_user={"user_id": 1, "username": "tester"},
                        )

                self.assertEqual(ctx.exception.status_code, 400)
                self.assertEqual(str(ctx.exception.detail), expected_detail)
                cleanup_mock.assert_not_called()
                create_task_mock.assert_not_called()
                self.assertEqual(set(reply_server.order_history_sync_jobs.keys()), before_job_ids)

    async def test_start_order_history_sync_rejects_unscoped_or_missing_accounts_before_creating_background_job(self):
        base_request = {
            "start_date": "2026-05-01",
            "end_date": "2026-05-02",
            "max_orders": 120,
            "fetch_details": True,
        }
        invalid_cases = [
            (
                {**base_request, "account_id": "acc-history-foreign"},
                ["acc-history-owned"],
                403,
                "指定账号不存在或无权限访问",
            ),
            (
                {**base_request, "account_id": None},
                {},
                400,
                "当前没有可同步的账号",
            ),
        ]

        for request_kwargs, user_account_ids, expected_status, expected_detail in invalid_cases:
            with self.subTest(request_kwargs=request_kwargs, user_account_ids=user_account_ids):
                before_job_ids = set(reply_server.order_history_sync_jobs.keys())
                request = reply_server.OrderHistorySyncRequest(**request_kwargs)

                with mock.patch.object(reply_server.db_manager, "get_account_ids", return_value=user_account_ids), \
                     mock.patch.object(reply_server.db_manager, "get_all_cookies", side_effect=AssertionError("创建历史订单同步任务前不该解密整包 Cookie")), \
                     mock.patch.object(reply_server, "_cleanup_order_history_sync_jobs") as cleanup_mock, \
                     mock.patch.object(reply_server.asyncio, "create_task") as create_task_mock, \
                     mock.patch.object(reply_server, "log_with_user"):
                    with self.assertRaises(reply_server.HTTPException) as ctx:
                        await reply_server.start_order_history_sync(
                            request,
                            current_user={"user_id": 1, "username": "tester"},
                        )

                self.assertEqual(ctx.exception.status_code, expected_status)
                self.assertEqual(str(ctx.exception.detail), expected_detail)
                cleanup_mock.assert_not_called()
                create_task_mock.assert_not_called()
                self.assertEqual(set(reply_server.order_history_sync_jobs.keys()), before_job_ids)

    async def test_start_order_history_sync_reuses_existing_active_job_for_same_user(self):
        existing_job = {
            "job_id": "history_sync_existing_active",
            "status": "running",
            "message": "正在同步历史订单",
            "user_id": 1,
            "created_at": "2026-05-27 12:00:00",
            "request": {
                "account_id": "acc-history-owned",
                "start_date": "2026-05-01",
                "end_date": "2026-05-02",
                "max_orders": 120,
                "fetch_details": True,
            },
            "orders_discovered": 3,
            "orders_processed": 1,
            "orders_saved": 1,
        }
        request = reply_server.OrderHistorySyncRequest(
            account_id="acc-history-owned",
            start_date="2026-05-01",
            end_date="2026-05-02",
            max_orders=120,
            fetch_details=True,
        )
        reply_server.order_history_sync_jobs[existing_job["job_id"]] = dict(existing_job)
        try:
            with mock.patch.object(reply_server.db_manager, "get_account_ids", return_value=["acc-history-owned"]), \
                 mock.patch.object(reply_server.db_manager, "get_all_cookies", side_effect=AssertionError("复用历史订单同步任务前不该解密整包 Cookie")), \
                 mock.patch.object(reply_server, "_cleanup_order_history_sync_jobs") as cleanup_mock, \
                 mock.patch.object(reply_server.asyncio, "create_task") as create_task_mock, \
                 mock.patch.object(reply_server, "log_with_user"):
                result = await reply_server.start_order_history_sync(
                    request,
                    current_user={"user_id": 1, "username": "tester"},
                )
        finally:
            reply_server.order_history_sync_jobs.pop(existing_job["job_id"], None)
            reply_server.order_history_sync_tasks.pop(existing_job["job_id"], None)

        self.assertTrue(result["success"])
        self.assertEqual("已有历史订单同步任务正在执行，已返回当前任务状态", result["message"])
        self.assertEqual(existing_job["job_id"], result["data"]["job_id"])
        self.assertEqual("running", result["data"]["status"])
        self.assertEqual(existing_job["request"], result["data"]["request"])
        cleanup_mock.assert_called_once_with()
        create_task_mock.assert_not_called()

    async def test_order_history_sync_status_and_cancel_accept_string_user_id_snapshot(self):
        job_id = "history-sync-user-id-string"
        job = {
            "job_id": job_id,
            "status": "running",
            "message": "正在同步历史订单",
            "user_id": 7,
            "created_at": "2026-05-27 12:00:00",
            "request": {
                "account_id": "acc-history-owned",
                "start_date": "2026-05-01",
                "end_date": "2026-05-02",
                "max_orders": 120,
                "fetch_details": True,
            },
            "warnings": [],
        }
        reply_server.order_history_sync_jobs[job_id] = job
        try:
            status_result = reply_server.get_order_history_sync_status(
                job_id,
                current_user={"user_id": "7", "username": "tester"},
            )
            cancel_result = reply_server.cancel_order_history_sync(
                job_id,
                current_user={"user_id": "7", "username": "tester"},
            )
        finally:
            reply_server.order_history_sync_jobs.pop(job_id, None)
            reply_server.order_history_sync_tasks.pop(job_id, None)

        self.assertTrue(status_result["success"])
        self.assertEqual(job_id, status_result["data"]["job_id"])
        self.assertTrue(cancel_result["success"])
        self.assertEqual("cancelled", cancel_result["data"]["status"])
        self.assertEqual("历史订单同步已取消", cancel_result["data"]["message"])

    async def test_order_history_sync_prefers_managed_runtime_helper_for_detail_refresh(self):
        job_id = "history-sync-job-managed"
        managed_list_result = {
            "orders": [{
                "order_id": "order-history-1",
                "item_id": "item-history-1",
                "buyer_id": "buyer-history-1",
                "buyer_nick": "buyer-history-nick-1",
                "sid": "sid-history-1",
            }],
            "scanned_count": 1,
            "matched_count": 1,
            "out_of_range_count": 0,
        }
        fake_fetcher = SimpleNamespace(
            fetch_recent_orders=mock.AsyncMock(return_value={
                "orders": [{
                    "order_id": "order-history-1",
                    "item_id": "item-history-1",
                    "buyer_id": "buyer-history-1",
                    "buyer_nick": "buyer-history-nick-1",
                    "sid": "sid-history-1",
                }],
                "scanned_count": 1,
                "matched_count": 1,
                "out_of_range_count": 0,
            }),
            fetch_order_detail=mock.AsyncMock(return_value=None),
            close=mock.AsyncMock(),
        )
        managed_call = mock.AsyncMock(side_effect=[
            managed_list_result,
            {"order_id": "order-history-1", "order_status": "shipped"},
        ])
        job = {
            "request": {
                "start_date": "2026-05-01",
                "end_date": "2026-05-02",
                "account_id": "acc-history-1",
                "max_orders": 1,
                "fetch_details": True,
            },
            "user_info": {"user_id": 1},
            "status": "queued",
        }
        reply_server.order_history_sync_jobs[job_id] = job
        try:
            with mock.patch.object(reply_server.db_manager, "get_account_ids", return_value=["acc-history-1"]), \
                 mock.patch.object(reply_server.db_manager, "get_cookie", return_value="cookie-value"), \
                 mock.patch("utils.order_history_sync.OrderHistoryPageFetcher", return_value=fake_fetcher) as fetcher_cls, \
                 mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
                 mock.patch.object(reply_server, "_save_history_order_detail_result", return_value=True) as save_detail_mock, \
                 mock.patch.object(reply_server, "_save_history_order_candidate", return_value=True) as save_candidate_mock, \
                 mock.patch.object(reply_server, "_cleanup_order_history_sync_jobs"):
                await reply_server._run_order_history_sync_job(job_id)
        finally:
            reply_server.order_history_sync_jobs.pop(job_id, None)
            reply_server.order_history_sync_tasks.pop(job_id, None)

        self.assertEqual(job["status"], "completed")
        fetcher_cls.assert_not_called()
        self.assertEqual(managed_call.await_count, 2)
        self.assertEqual(managed_call.await_args_list[0].args[0], "acc-history-1")
        self.assertTrue(callable(managed_call.await_args_list[0].args[1]))
        self.assertEqual(managed_call.await_args_list[1].args[0], "acc-history-1")
        self.assertTrue(callable(managed_call.await_args_list[1].args[1]))
        fake_fetcher.fetch_order_detail.assert_not_awaited()
        save_detail_mock.assert_called_once_with(
            "acc-history-1",
            {
                "order_id": "order-history-1",
                "item_id": "item-history-1",
                "buyer_id": "buyer-history-1",
                "buyer_nick": "buyer-history-nick-1",
                "sid": "sid-history-1",
            },
            {"order_id": "order-history-1", "order_status": "shipped"},
        )
        save_candidate_mock.assert_not_called()

    async def test_order_history_sync_prefers_managed_runtime_helper_for_order_list(self):
        job_id = "history-sync-job-managed-list"
        managed_list_result = {
            "orders": [{
                "order_id": "order-history-list-1",
                "item_id": "item-history-list-1",
                "buyer_id": "buyer-history-list-1",
                "buyer_nick": "buyer-history-list-nick-1",
                "sid": "sid-history-list-1",
            }],
            "scanned_count": 1,
            "matched_count": 1,
            "out_of_range_count": 0,
        }
        fake_fetcher = SimpleNamespace(
            fetch_recent_orders=mock.AsyncMock(return_value={
                "orders": [],
                "scanned_count": 0,
                "matched_count": 0,
                "out_of_range_count": 0,
            }),
            fetch_order_detail=mock.AsyncMock(return_value=None),
            close=mock.AsyncMock(),
        )
        managed_call = mock.AsyncMock(side_effect=[
            managed_list_result,
            {"order_id": "order-history-list-1", "order_status": "shipped"},
        ])
        job = {
            "request": {
                "start_date": "2026-05-01",
                "end_date": "2026-05-02",
                "account_id": "acc-history-list-1",
                "max_orders": 1,
                "fetch_details": True,
            },
            "user_info": {"user_id": 1},
            "status": "queued",
        }
        reply_server.order_history_sync_jobs[job_id] = job
        try:
            with mock.patch.object(reply_server.db_manager, "get_account_ids", return_value=["acc-history-list-1"]), \
                 mock.patch.object(reply_server.db_manager, "get_cookie", return_value="cookie-value"), \
                 mock.patch("utils.order_history_sync.OrderHistoryPageFetcher", return_value=fake_fetcher) as fetcher_cls, \
                 mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
                 mock.patch.object(reply_server, "_save_history_order_detail_result", return_value=True) as save_detail_mock, \
                 mock.patch.object(reply_server, "_save_history_order_candidate", return_value=True) as save_candidate_mock, \
                 mock.patch.object(reply_server, "_cleanup_order_history_sync_jobs"):
                await reply_server._run_order_history_sync_job(job_id)
        finally:
            reply_server.order_history_sync_jobs.pop(job_id, None)
            reply_server.order_history_sync_tasks.pop(job_id, None)

        self.assertEqual(job["status"], "completed")
        fetcher_cls.assert_not_called()
        self.assertEqual(managed_call.await_count, 2)
        self.assertEqual(managed_call.await_args_list[0].args[0], "acc-history-list-1")
        self.assertTrue(callable(managed_call.await_args_list[0].args[1]))
        fake_fetcher.fetch_recent_orders.assert_not_awaited()
        fake_fetcher.fetch_order_detail.assert_not_awaited()
        save_detail_mock.assert_called_once_with(
            "acc-history-list-1",
            {
                "order_id": "order-history-list-1",
                "item_id": "item-history-list-1",
                "buyer_id": "buyer-history-list-1",
                "buyer_nick": "buyer-history-list-nick-1",
                "sid": "sid-history-list-1",
            },
            {"order_id": "order-history-list-1", "order_status": "shipped"},
        )
        save_candidate_mock.assert_not_called()

    async def test_order_history_sync_falls_back_to_fetcher_when_managed_order_list_missing(self):
        job_id = "history-sync-job-list-fallback"
        fake_fetcher = SimpleNamespace(
            fetch_recent_orders=mock.AsyncMock(return_value={
                "orders": [{
                    "order_id": "order-history-list-2",
                    "item_id": "item-history-list-2",
                    "buyer_id": "buyer-history-list-2",
                    "buyer_nick": "buyer-history-list-nick-2",
                    "sid": "sid-history-list-2",
                }],
                "scanned_count": 1,
                "matched_count": 1,
                "out_of_range_count": 0,
            }),
            fetch_order_detail=mock.AsyncMock(return_value=None),
            close=mock.AsyncMock(),
        )
        managed_call = mock.AsyncMock(side_effect=[
            reply_server.HTTPException(status_code=400, detail="账号未启动，暂无法执行当前操作"),
        ])
        job = {
            "request": {
                "start_date": "2026-05-01",
                "end_date": "2026-05-02",
                "account_id": "acc-history-list-2",
                "max_orders": 1,
                "fetch_details": False,
            },
            "user_info": {"user_id": 1},
            "status": "queued",
        }
        reply_server.order_history_sync_jobs[job_id] = job
        try:
            with mock.patch.object(reply_server.db_manager, "get_account_ids", return_value=["acc-history-list-2"]), \
                 mock.patch.object(reply_server.db_manager, "get_cookie", return_value="cookie-value"), \
                 mock.patch("utils.order_history_sync.OrderHistoryPageFetcher", return_value=fake_fetcher) as fetcher_cls, \
                 mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
                 mock.patch.object(reply_server, "_save_history_order_candidate", return_value=True) as save_candidate_mock, \
                 mock.patch.object(reply_server, "_cleanup_order_history_sync_jobs"):
                await reply_server._run_order_history_sync_job(job_id)
        finally:
            reply_server.order_history_sync_jobs.pop(job_id, None)
            reply_server.order_history_sync_tasks.pop(job_id, None)

        self.assertEqual(job["status"], "completed")
        managed_call.assert_awaited_once()
        fetcher_cls.assert_called_once_with("cookie-value", account_id="acc-history-list-2", headless=True)
        fake_fetcher.fetch_recent_orders.assert_awaited_once()
        save_candidate_mock.assert_called_once_with(
            "acc-history-list-2",
            {
                "order_id": "order-history-list-2",
                "item_id": "item-history-list-2",
                "buyer_id": "buyer-history-list-2",
                "buyer_nick": "buyer-history-list-nick-2",
                "sid": "sid-history-list-2",
            },
        )

    async def test_order_history_sync_falls_back_to_fetcher_when_managed_runtime_missing(self):
        job_id = "history-sync-job-fallback"
        fake_fetcher = SimpleNamespace(
            fetch_recent_orders=mock.AsyncMock(return_value={
                "orders": [{
                    "order_id": "order-history-2",
                    "item_id": "item-history-2",
                    "buyer_id": "buyer-history-2",
                    "buyer_nick": "buyer-history-nick-2",
                    "sid": "sid-history-2",
                }],
                "scanned_count": 1,
                "matched_count": 1,
                "out_of_range_count": 0,
            }),
            fetch_order_detail=mock.AsyncMock(return_value={"order_id": "order-history-2", "order_status": "shipped"}),
            close=mock.AsyncMock(),
        )
        managed_call = mock.AsyncMock(
            side_effect=reply_server.HTTPException(status_code=400, detail="账号未启动，暂无法执行当前操作")
        )
        job = {
            "request": {
                "start_date": "2026-05-01",
                "end_date": "2026-05-02",
                "account_id": "acc-history-2",
                "max_orders": 1,
                "fetch_details": True,
            },
            "user_info": {"user_id": 1},
            "status": "queued",
        }
        reply_server.order_history_sync_jobs[job_id] = job
        try:
            with mock.patch.object(reply_server.db_manager, "get_account_ids", return_value=["acc-history-2"]), \
                 mock.patch.object(reply_server.db_manager, "get_cookie", return_value="cookie-value"), \
                 mock.patch("utils.order_history_sync.OrderHistoryPageFetcher", return_value=fake_fetcher) as fetcher_cls, \
                 mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
                 mock.patch.object(reply_server, "_save_history_order_detail_result", return_value=True) as save_detail_mock, \
                 mock.patch.object(reply_server, "_save_history_order_candidate", return_value=True) as save_candidate_mock, \
                 mock.patch.object(reply_server, "_cleanup_order_history_sync_jobs"):
                await reply_server._run_order_history_sync_job(job_id)
        finally:
            reply_server.order_history_sync_jobs.pop(job_id, None)
            reply_server.order_history_sync_tasks.pop(job_id, None)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(managed_call.await_count, 2)
        fetcher_cls.assert_called_once_with("cookie-value", account_id="acc-history-2", headless=True)
        fake_fetcher.fetch_order_detail.assert_awaited_once_with("order-history-2", force_refresh=True)
        save_detail_mock.assert_called_once_with(
            "acc-history-2",
            {
                "order_id": "order-history-2",
                "item_id": "item-history-2",
                "buyer_id": "buyer-history-2",
                "buyer_nick": "buyer-history-nick-2",
                "sid": "sid-history-2",
            },
            {"order_id": "order-history-2", "order_status": "shipped"},
        )
        save_candidate_mock.assert_not_called()

    async def test_order_history_sync_rejects_selected_account_outside_current_user_scope(self):
        job_id = "history-sync-job-forbidden"
        managed_call = mock.AsyncMock()
        job = {
            "request": {
                "start_date": "2026-05-01",
                "end_date": "2026-05-02",
                "account_id": "acc-history-foreign",
                "max_orders": 1,
                "fetch_details": True,
            },
            "user_info": {"user_id": 1},
            "status": "queued",
        }
        reply_server.order_history_sync_jobs[job_id] = job
        try:
            with mock.patch.object(reply_server.db_manager, "get_account_ids", return_value=["acc-history-owned"]), \
                 mock.patch.object(reply_server.db_manager, "get_cookie", side_effect=AssertionError("越权历史订单同步不该读取 Cookie")), \
                 mock.patch("utils.order_history_sync.OrderHistoryPageFetcher") as fetcher_cls, \
                 mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
                 mock.patch.object(reply_server, "_save_history_order_detail_result", return_value=True) as save_detail_mock, \
                 mock.patch.object(reply_server, "_save_history_order_candidate", return_value=True) as save_candidate_mock, \
                 mock.patch.object(reply_server, "_cleanup_order_history_sync_jobs"):
                await reply_server._run_order_history_sync_job(job_id)
        finally:
            reply_server.order_history_sync_jobs.pop(job_id, None)
            reply_server.order_history_sync_tasks.pop(job_id, None)

        self.assertEqual(job["status"], "failed")
        self.assertIn("指定账号不存在或无权限访问", job["error"])
        fetcher_cls.assert_not_called()
        managed_call.assert_not_awaited()
        save_detail_mock.assert_not_called()
        save_candidate_mock.assert_not_called()

    async def test_order_history_sync_warns_and_falls_back_to_candidate_when_managed_detail_refresh_errors(self):
        job_id = "history-sync-job-detail-warning"
        candidate = {
            "order_id": "order-history-3",
            "item_id": "item-history-3",
            "buyer_id": "buyer-history-3",
            "buyer_nick": "buyer-history-nick-3",
            "sid": "sid-history-3",
        }
        fake_fetcher = SimpleNamespace(
            fetch_recent_orders=mock.AsyncMock(return_value={
                "orders": [candidate],
                "scanned_count": 1,
                "matched_count": 1,
                "out_of_range_count": 0,
            }),
            fetch_order_detail=mock.AsyncMock(return_value=None),
            close=mock.AsyncMock(),
        )
        managed_call = mock.AsyncMock(side_effect=[
            {
                "orders": [candidate],
                "scanned_count": 1,
                "matched_count": 1,
                "out_of_range_count": 0,
            },
            RuntimeError("detail boom"),
        ])
        job = {
            "request": {
                "start_date": "2026-05-01",
                "end_date": "2026-05-02",
                "account_id": "acc-history-3",
                "max_orders": 1,
                "fetch_details": True,
            },
            "user_info": {"user_id": 1},
            "status": "queued",
        }
        reply_server.order_history_sync_jobs[job_id] = job
        try:
            with mock.patch.object(reply_server.db_manager, "get_account_ids", return_value=["acc-history-3"]), \
                 mock.patch.object(reply_server.db_manager, "get_cookie", return_value="cookie-value"), \
                 mock.patch("utils.order_history_sync.OrderHistoryPageFetcher", return_value=fake_fetcher) as fetcher_cls, \
                 mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
                 mock.patch.object(reply_server, "_save_history_order_detail_result", return_value=True) as save_detail_mock, \
                 mock.patch.object(reply_server, "_save_history_order_candidate", return_value=True) as save_candidate_mock, \
                 mock.patch.object(reply_server, "_cleanup_order_history_sync_jobs"):
                await reply_server._run_order_history_sync_job(job_id)
        finally:
            reply_server.order_history_sync_jobs.pop(job_id, None)
            reply_server.order_history_sync_tasks.pop(job_id, None)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(managed_call.await_count, 2)
        fetcher_cls.assert_not_called()
        fake_fetcher.fetch_order_detail.assert_not_awaited()
        save_detail_mock.assert_not_called()
        save_candidate_mock.assert_called_once_with("acc-history-3", candidate)
        self.assertEqual(job["orders_saved"], 1)
        self.assertEqual(job["orders_failed"], 0)
        self.assertTrue(
            any("订单 order-history-3 详情刷新失败: detail boom" in warning for warning in job["warnings"])
        )

    def test_item_and_item_reply_dom_use_account_id_ids_and_datasets(self):
        index_html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="itemAccountFilter"', index_html)
        self.assertIn('id="itemReplayAccountFilter"', index_html)
        self.assertIn('id="editReplyAccountIdSelect"', index_html)
        self.assertIn('id="editItemAccountId"', index_html)
        self.assertIn('id="editItemAccountIdDisplay"', index_html)
        self.assertIn("checkbox.dataset.accountId", app_js)
        self.assertIn("data-account-id", app_js)
        self.assertIn("document.getElementById('itemAccountFilter')", app_js)
        self.assertIn("document.getElementById('itemReplayAccountFilter')", app_js)
        self.assertIn("document.getElementById('editReplyAccountIdSelect')", app_js)

    def test_order_history_sync_dom_uses_account_id_id(self):
        index_html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="orderHistorySyncAccountId"', index_html)
        self.assertNotIn('id="orderHistorySyncCookieId"', index_html)
        self.assertIn("orderHistorySyncAccountId", app_js)
        self.assertIn("account_id: accountId || null", app_js)
        self.assertIn("const startToastMessage = result.message || result.data?.message || '历史订单同步已开始';", app_js)
        self.assertIn("String(startToastMessage).includes('已有历史订单同步任务正在执行')", app_js)
        self.assertIn("if (activeOrderHistorySyncJobId && activeOrderHistorySyncJobId === jobId && isOrdersSectionActive()) {", app_js)
        self.assertIn("scheduleOrderHistorySyncPolling(jobId);", app_js)

    def test_order_list_and_detail_dom_use_account_id(self):
        index_html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="orderAccountFilter"', index_html)
        self.assertNotIn('id="orderCookieFilter"', index_html)
        self.assertIn("loadOrdersByAccount()", app_js)
        self.assertIn("document.getElementById('orderAccountFilter')", app_js)
        self.assertNotIn("order.account_id || order.cookie_id", app_js)
        self.assertIn("order.account_id", app_js)
        self.assertIn("账号ID", app_js)
        self.assertIn('data-account-id="${safeAccountIdAttr}"', app_js)
        self.assertIn("actionButton.dataset.accountId", app_js)
        self.assertIn("showOrderDetail(orderId, accountId)", app_js)
        self.assertIn("allOrdersData.find(o => o.order_id === orderId && String(o.account_id || '').trim() === normalizedAccountId)", app_js)
        self.assertIn("params.set('account_id', normalizedAccountId)", app_js)
        self.assertIn("accountId: cb.dataset.accountId", app_js)

    def test_auto_delivery_forms_validate_positive_delivery_count_on_frontend(self):
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const keyword = document.getElementById('productKeyword').value.trim();", app_js)
        self.assertIn("const keyword = document.getElementById('editProductKeyword').value.trim();", app_js)
        self.assertIn("const normalizedDeliveryCount = Number.parseInt(deliveryCount, 10);", app_js)
        self.assertIn("if (!Number.isInteger(normalizedDeliveryCount) || normalizedDeliveryCount < 1) {", app_js)
        self.assertIn("showToast('发货数量必须为大于等于 1 的整数', 'warning');", app_js)
        self.assertIn("delivery_count: normalizedDeliveryCount,", app_js)

    def test_card_forms_trim_card_name_on_frontend(self):
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const cardName = document.getElementById('cardName').value.trim();", app_js)
        self.assertIn("const cardName = document.getElementById('editCardName').value.trim();", app_js)

    def test_realtime_order_cache_matching_uses_account_id(self):
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const normalizedAccountId = String(order.account_id || '').trim();", app_js)
        self.assertIn("const existingIndex = allOrdersData.findIndex(item => item.order_id === order.order_id && String(item.account_id || '').trim() === normalizedAccountId);", app_js)
        self.assertNotIn("const existingIndex = allOrdersData.findIndex(item => item.order_id === order.order_id);", app_js)

    def test_risk_log_dom_and_fetch_use_account_id(self):
        index_html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="riskLogAccountFilter"', index_html)
        self.assertNotIn('id="riskLogCookieFilter"', index_html)
        self.assertIn("riskLogAccountFilter", app_js)
        self.assertIn("params.set('account_id', accountId)", app_js)
        self.assertNotIn("log.account_id || log.cookie_id", app_js)

    def test_ai_reply_and_qr_requests_use_account_id_values(self):
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const encodedAccountId = encodeURIComponent(accountId);", app_js)
        self.assertIn("fetch(`${apiBase}/ai-reply-settings/${encodedAccountId}`", app_js)
        self.assertIn("fetch(`${apiBase}/ai-reply-test/${encodedAccountId}`", app_js)
        self.assertIn("fetch(`${apiBase}/qr-login/cooldown-status/${encodedAccountId}`", app_js)
        self.assertIn("fetch(`${apiBase}/qr-login/reset-cooldown/${encodedAccountId}`", app_js)
        self.assertIn("account_id: selectedAccountId", app_js)
        self.assertIn("document.getElementById('accountId')", app_js)
        self.assertIn("if (!accountResponse.ok) {", app_js)
        self.assertIn("const accountErrorMessage = await readResponseErrorMessage(accountResponse, `HTTP ${accountResponse.status}`);", app_js)

    def test_account_management_uses_account_id_response_fields(self):
        index_html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="accountTable"', index_html)
        self.assertNotIn('id="cookieTable"', index_html)
        self.assertIn("function getCookieDetailsAccountId(account)", app_js)
        self.assertIn("return String(account?.account_id || '').trim();", app_js)
        self.assertIn("String(cookie.account_id)", app_js)
        self.assertIn("const accountId = String(cookie.account_id || '');", app_js)
        self.assertIn("option.value = account.account_id;", app_js)
        self.assertIn("option.textContent = `${account.account_id} ${hasCredentials}`;", app_js)
        self.assertIn("async function deleteKeyword(accountId, index)", app_js)
        self.assertIn("function editRemark(accountId, currentRemark)", app_js)
        self.assertIn("function editPauseDuration(accountId, currentDuration)", app_js)
        self.assertNotIn("fetchDashboardResource(`/keywords/${encodeURIComponent(account.id)}`", app_js)
        self.assertNotIn("fetch(`${apiBase}/keywords/${account.id}`", app_js)
        self.assertNotIn("const accountId = String(account.id || '');", app_js)
        self.assertNotIn("const accountId = String(account?.id || '').trim();", app_js)
        self.assertNotIn("option.value = account.id;", app_js)
        self.assertNotIn("option.textContent = account.id;", app_js)
        self.assertNotIn("aboutDiagnosticsAccounts.find(account => account.id === normalizedAccountId)", app_js)
        self.assertNotIn("accounts.some(account => account.id === previousValue)", app_js)
        self.assertNotIn("accounts.find(acc => acc.id === accountId)", app_js)
        self.assertIn("const accountId = getCookieDetailsAccountId(accountData);", app_js)
        self.assertIn("document.getElementById('accountEditId').value = accountId;", app_js)
        self.assertIn("document.getElementById('accountEditIdDisplay').textContent = accountId;", app_js)
        self.assertIn("async function loadAccounts()", app_js)
        self.assertIn("async function loadAccountOptions(id, emptyLabel = '所有账号')", app_js)
        self.assertIn("async function loadOrderAccountFilterOptions()", app_js)
        self.assertIn("async function loadRiskLogAccountFilterOptions()", app_js)
        self.assertIn("document.querySelector('#accountTable tbody')", app_js)
        self.assertIn("document.querySelectorAll('#accountTable tbody tr[data-account-id]')", app_js)
        self.assertIn("async function openAccountEditor(id)", app_js)
        self.assertIn("async function deleteAccount(id)", app_js)
        self.assertIn("loadAccountOptions('itemAccountFilter')", app_js)
        self.assertIn("loadAccountOptions('itemReplayAccountFilter')", app_js)
        self.assertIn("loadAccountOptions('editReplyAccountIdSelect', '选择账号')", app_js)
        self.assertIn("const selectedAccountId = document.getElementById('itemAccountFilter').value;", app_js)
        self.assertIn("const selectedAccountId = document.getElementById('itemReplayAccountFilter').value;", app_js)
        self.assertIn("fetch(`${apiBase}/accounts/details?include_runtime_status=false&summary_only=true`", app_js)
        self.assertIn("fetchJSON(apiBase + '/accounts/details', {", app_js)
        self.assertIn("fetchJSON(`${apiBase}/accounts/details`, {", app_js)
        self.assertIn("fetchJSONWithoutGlobalLoading(`${apiBase}/accounts/${encodeURIComponent(normalizedAccountId)}/runtime-status`, {", app_js)
        self.assertIn("fetchJSONWithoutGlobalLoading(`${apiBase}/accounts/${encodeURIComponent(accountId)}/session-keepalive`, {", app_js)
        self.assertIn("`${apiBase}/accounts/${encodeURIComponent(accountId)}/conversations/${encodeURIComponent(conversationId)}/history`", app_js)
        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodeURIComponent(id)}/details?include_secrets=true`, {", app_js)
        self.assertIn("`${apiBase}/accounts/${encodeURIComponent(accountId)}/details?include_secrets=true&include_runtime_status=false`", app_js)
        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodedAccountId}/proxy?include_secret=true`, {", app_js)
        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodedAccountId}/account-info`, {", app_js)
        self.assertIn("await fetchJSON(`${apiBase}/accounts/${encodedAccountId}/proxy`, {", app_js)
        self.assertIn("await fetchJSON(`${apiBase}/accounts/${encodedAccountId}`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${encodedAccountId}/status`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${encodedAccountId}/auto-confirm`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${encodedAccountId}/auto-comment`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${encodedAccountId}/comment-templates`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${encodedAccountId}/comment-templates`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${encodedAccountId}/comment-templates/${templateId}`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${encodedAccountId}/comment-templates/${templateId}`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${encodedAccountId}/comment-templates/${templateId}/activate`, {", app_js)
        self.assertIn("const accountsResponse = await fetch(`${apiBase}/accounts`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${encodedAccountId}/remark`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${encodedAccountId}/pause-duration`, {", app_js)
        self.assertIn("const response = await fetch('/admin/accounts', {", app_js)
        self.assertIn("const accountsCheckResponse = await fetch(`/accounts/check?account_id=${encodeURIComponent(accountId)}`, {", app_js)
        self.assertIn("if (data.success && data.accounts) {", app_js)
        self.assertIn("data.accounts.forEach(account => {", app_js)
        self.assertIn("body: JSON.stringify({\n                value: cookie,\n                username: username,\n                password: password", app_js)
        self.assertNotIn("show_browser: showBrowser", app_js)
        self.assertNotIn("async function loadCookies()", app_js)
        self.assertNotIn("async function loadCookieFilter(id)", app_js)
        self.assertNotIn("async function loadCookieFilterPlus(id)", app_js)
        self.assertNotIn("async function loadOrderCookieFilter()", app_js)
        self.assertNotIn("async function loadOrdersByCookie()", app_js)
        self.assertNotIn("async function loadCookieFilterOptions()", app_js)
        self.assertNotIn("async function editCookieInline(id, currentValue)", app_js)
        self.assertNotIn("async function delCookie(id)", app_js)
        self.assertNotIn("async function saveAccountCookieValue(id)", app_js)
        self.assertNotIn("function cancelAccountCookieEdit(id)", app_js)
        self.assertNotIn("window.accountEditState", app_js)
        self.assertNotIn("window.editingAccountData", app_js)
        self.assertNotIn("const selectedCookie = document.getElementById('itemAccountFilter').value;", app_js)
        self.assertNotIn("const selectedCookie = document.getElementById('itemReplayAccountFilter').value;", app_js)
        self.assertNotIn("fetch(`${apiBase}/cookies/details`", app_js)
        self.assertNotIn("fetchJSON(`${apiBase}/cookies/details`)", app_js)
        self.assertNotIn("fetchJSON(`${apiBase}/cookies/${encodeURIComponent(normalizedAccountId)}/runtime-status`)", app_js)
        self.assertNotIn("Cookie筛选", index_html)
        self.assertNotIn("fetchJSON(`${apiBase}/cookie/${encodeURIComponent(id)}/details?include_secrets=true`)", app_js)
        self.assertNotIn("document.getElementById('accountEditId').value = accountData.id;", app_js)
        self.assertNotIn("document.getElementById('accountEditIdDisplay').textContent = accountData.id;", app_js)
        self.assertNotIn("option.value = cookie.id;", app_js)
        self.assertNotIn("toggleAccountStatus('${cookie.id}', this.checked)", app_js)
        self.assertNotIn("await fetchJSON(apiBase + `/cookies/${id}`, { method: 'DELETE' });", app_js)
        self.assertNotIn("const response = await fetch(`${apiBase}/cookies/${accountId}/status`, {", app_js)
        self.assertNotIn("const response = await fetch(`${apiBase}/cookies/${accountId}/auto-confirm`, {", app_js)
        self.assertNotIn("const response = await fetch(`${apiBase}/cookies/${accountId}/auto-comment`, {", app_js)
        self.assertNotIn("const response = await fetch(`${apiBase}/cookies/${accountId}/comment-templates`, {", app_js)
        self.assertNotIn("const response = await fetch(`${apiBase}/cookies/${currentCommentTemplateAccountId}/comment-templates`, {", app_js)
        self.assertNotIn("const response = await fetch(`${apiBase}/cookies/${currentCommentTemplateAccountId}/comment-templates/${templateId}`, {", app_js)
        self.assertNotIn("const response = await fetch(`${apiBase}/cookies/${accountId}/comment-templates/${templateId}`, {", app_js)
        self.assertNotIn("const response = await fetch(`${apiBase}/cookies/${accountId}/comment-templates/${templateId}/activate`, {", app_js)
        self.assertNotIn("const accountsResponse = await fetch(`${apiBase}/cookies`, {", app_js)
        self.assertNotIn("const response = await fetch(`${apiBase}/cookies/${accountId}/remark`, {", app_js)
        self.assertNotIn("const response = await fetch(`${apiBase}/cookies/${accountId}/pause-duration`, {", app_js)
        self.assertNotIn("const response = await fetch('/admin/cookies', {", app_js)
        self.assertNotIn("const cookiesCheckResponse = await fetch('/cookies/check', {", app_js)
        self.assertNotIn("if (data.success && data.cookies) {", app_js)
        self.assertNotIn("data.cookies.forEach(cookie => {", app_js)
        self.assertNotIn("body: JSON.stringify({\n        id: id,\n        value: newValue", app_js)
        self.assertNotIn("window.editingCookieData", app_js)

    def test_admin_dashboard_stats_uses_supported_stats_endpoint(self):
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("async function loadUserSystemStats()", app_js)
        self.assertIn("fetch('/admin/stats', {", app_js)
        self.assertIn("document.getElementById('totalUsers').textContent = statsData.users.total;", app_js)
        self.assertIn("document.getElementById('totalUserCookies').textContent = statsData.cookies.total;", app_js)
        self.assertIn("document.getElementById('totalUserCards').textContent = statsData.cards.total;", app_js)
        self.assertNotIn("fetch(`${apiBase}/admin/data/cookies`, {", app_js)
        self.assertNotIn("fetch(`${apiBase}/admin/data/cards`, {", app_js)
        self.assertIn("@app.get('/admin/stats')", source)


class ReplyServerOrderAccountScopeRuntimeTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_save_history_order_candidate_passes_account_id_to_order_upsert(self):
        candidate = {
            "order_id": "order-history-helper-1",
            "item_id": "item-history-helper-1",
            "buyer_id": "buyer-history-helper-1",
            "buyer_nick": "buyer-history-helper-nick-1",
            "sid": "sid-history-helper-1",
            "amount": "19.80",
            "order_status": "shipped",
        }

        with mock.patch.object(
            reply_server.db_manager,
            "insert_or_update_order",
            return_value=True,
        ) as insert_or_update_order:
            saved = reply_server._save_history_order_candidate("acc-history-helper-1", candidate)

        self.assertTrue(saved)
        insert_or_update_order.assert_called_once()
        call_kwargs = insert_or_update_order.call_args.kwargs
        self.assertEqual(call_kwargs["account_id"], "acc-history-helper-1")
        self.assertEqual(call_kwargs["order_id"], "order-history-helper-1")
        self.assertEqual(call_kwargs["item_id"], "item-history-helper-1")
        self.assertEqual(call_kwargs["order_status"], "shipped")

    def test_save_history_order_detail_result_uses_candidate_fallback_fields_with_account_scope(self):
        candidate = {
            "order_id": "order-history-helper-2",
            "item_id": "item-history-helper-2",
            "buyer_id": "buyer-history-helper-2",
            "buyer_nick": "buyer-history-helper-nick-2",
            "sid": "sid-history-helper-2",
            "amount": "28.50",
            "platform_created_at": "2026-05-01 10:00:00",
        }
        detail_result = {
            "order_id": "   ",
            "item_id": "",
            "order_status": "unknown",
            "amount": "",
        }

        with mock.patch.object(
            reply_server.db_manager,
            "insert_or_update_order",
            return_value=True,
        ) as insert_or_update_order:
            saved = reply_server._save_history_order_detail_result(
                "acc-history-helper-2",
                candidate,
                detail_result,
            )

        self.assertTrue(saved)
        insert_or_update_order.assert_called_once()
        call_kwargs = insert_or_update_order.call_args.kwargs
        self.assertEqual(call_kwargs["account_id"], "acc-history-helper-2")
        self.assertEqual(call_kwargs["order_id"], "order-history-helper-2")
        self.assertEqual(call_kwargs["item_id"], "item-history-helper-2")
        self.assertEqual(call_kwargs["buyer_id"], "buyer-history-helper-2")
        self.assertEqual(call_kwargs["sid"], "sid-history-helper-2")
        self.assertIsNone(call_kwargs["order_status"])
        self.assertEqual(call_kwargs["amount"], "28.50")
        self.assertEqual(call_kwargs["platform_created_at"], "2026-05-01 10:00:00")

    def test_delete_user_order_rejects_missing_account_id(self):
        fake_db = mock.Mock()

        with mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"acc-order-1": "cookie"}), \
             mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.delete_user_order(
                    "order-delete-1",
                    current_user={"user_id": 1, "username": "admin"},
                    account_id=None,
                )

        self.assertEqual(raised.exception.status_code, 400)
        fake_db.get_order_by_id.assert_not_called()

    def test_get_user_orders_uses_module_bound_database_for_current_user_scope(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-order-1", "acc-order-2"]
        fake_db.get_all_cookies.side_effect = AssertionError("订单列表不该解密整包 Cookie")
        fake_db.get_orders_by_account.side_effect = [
            [{"order_id": "order-1", "platform_created_at": "2026-05-01 10:00:00"}],
            [{"order_id": "order-2", "platform_created_at": "2026-05-02 10:00:00"}],
        ]

        shadow_db = mock.Mock()
        shadow_db.get_account_ids.side_effect = AssertionError("订单列表不该绕开 reply_server.db_manager")
        shadow_db.get_orders_by_account.side_effect = AssertionError("订单列表不该绕开 reply_server.db_manager")
        db_manager_module = sys.modules["db_manager"]
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        def normalize_orders(rows, account_id=None):
            return [
                {
                    "order_id": row["order_id"],
                    "account_id": account_id,
                    "platform_created_at": row["platform_created_at"],
                }
                for row in rows
            ]

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(db_manager_module, "db_manager", shadow_db), \
             mock.patch.object(reply_server, "_normalize_order_records", side_effect=normalize_orders), \
             mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.get_user_orders(current_user=current_user)

        self.assertEqual(
            result,
            {
                "success": True,
                "data": [
                    {
                        "order_id": "order-2",
                        "account_id": "acc-order-2",
                        "platform_created_at": "2026-05-02 10:00:00",
                    },
                    {
                        "order_id": "order-1",
                        "account_id": "acc-order-1",
                        "platform_created_at": "2026-05-01 10:00:00",
                    },
                ],
            },
        )
        fake_db.get_account_ids.assert_called_once_with(7)
        fake_db.get_orders_by_account.assert_has_calls(
            [mock.call("acc-order-1", limit=None), mock.call("acc-order-2", limit=None)]
        )

    def test_get_user_orders_stops_masking_database_failures_as_empty_results(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-order-1"]
        fake_db.get_all_cookies.side_effect = AssertionError("订单列表不该解密整包 Cookie")
        fake_db.get_orders_by_account.side_effect = RuntimeError("orders exploded")

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.get_user_orders(current_user={"user_id": 7, "username": "admin"})

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "查询订单失败: orders exploded")
        fake_db.get_account_ids.assert_called_once_with(7)
        fake_db.get_orders_by_account.assert_called_once_with("acc-order-1", limit=None)

    def test_delete_user_order_rejects_unowned_account_id_before_db_lookup(self):
        fake_db = mock.Mock()

        with mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"acc-order-1": "cookie"}), \
             mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.delete_user_order(
                    "order-delete-foreign",
                    current_user={"user_id": 1, "username": "admin"},
                    account_id="acc-order-foreign",
                )

        self.assertEqual(raised.exception.status_code, 403)
        fake_db.get_order_by_id.assert_not_called()

    def test_delete_user_order_scopes_lookup_by_account_id(self):
        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-delete-1",
            "account_id": "acc-order-1",
        }
        fake_db.delete_order.return_value = True

        with mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"acc-order-1": "cookie"}), \
             mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.delete_user_order(
                "order-delete-1",
                account_id="acc-order-1",
                current_user={"user_id": 1, "username": "admin"},
            )

        self.assertTrue(result["success"])
        fake_db.get_order_by_id.assert_called_once_with(
            "order-delete-1",
            account_id="acc-order-1",
            user_id=1,
        )
        fake_db.delete_order.assert_called_once_with("order-delete-1", account_id="acc-order-1")

    def test_delete_user_order_stops_masking_order_lookup_failures_as_not_found(self):
        fake_db = mock.Mock()
        fake_db.get_order_by_id.side_effect = RuntimeError("order lookup exploded")

        with mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"acc-order-1": "cookie"}), \
             mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.delete_user_order(
                    "order-delete-1",
                    account_id="acc-order-1",
                    current_user={"user_id": 1, "username": "admin"},
                )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "删除订单失败，请稍后重试")
        fake_db.get_order_by_id.assert_called_once_with(
            "order-delete-1",
            account_id="acc-order-1",
            user_id=1,
        )
        fake_db.delete_order.assert_not_called()

    def test_delete_user_order_stops_masking_delete_failures_as_bad_request(self):
        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-delete-1",
            "account_id": "acc-order-1",
        }
        fake_db.delete_order.side_effect = RuntimeError("order delete exploded")

        with mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"acc-order-1": "cookie"}), \
             mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.delete_user_order(
                    "order-delete-1",
                    account_id="acc-order-1",
                    current_user={"user_id": 1, "username": "admin"},
                )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "删除订单失败，请稍后重试")
        fake_db.get_order_by_id.assert_called_once_with(
            "order-delete-1",
            account_id="acc-order-1",
            user_id=1,
        )
        fake_db.delete_order.assert_called_once_with("order-delete-1", account_id="acc-order-1")

    def test_manual_deliver_order_scopes_lookup_by_account_id(self):
        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-deliver-1",
            "account_id": "acc-order-1",
            "item_id": "item-deliver-1",
            "buyer_id": "buyer-deliver-1",
        }
        managed_call = mock.AsyncMock(
            side_effect=reply_server.HTTPException(
                status_code=400,
                detail="账号 acc-order-1 未运行，请先启动账号",
            )
        )

        async def invoke():
            return await reply_server.manual_deliver_order(
                "order-deliver-1",
                account_id="acc-order-1",
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"acc-order-1": "cookie"}), \
             mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertFalse(result["delivered"])
        self.assertIn("未运行", result["message"])
        fake_db.get_order_by_id.assert_called_once_with(
            "order-deliver-1",
            account_id="acc-order-1",
            user_id=1,
        )

    def test_manual_deliver_order_stops_masking_runtime_failures_as_business_payload(self):
        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-deliver-1",
            "account_id": "acc-order-1",
            "item_id": "item-deliver-1",
            "buyer_id": "buyer-deliver-1",
        }
        fake_db.get_item_info.return_value = {"item_title": "demo"}
        managed_call = mock.AsyncMock(side_effect=RuntimeError("manual deliver exploded"))

        async def invoke():
            return await reply_server.manual_deliver_order(
                "order-deliver-1",
                account_id="acc-order-1",
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"acc-order-1": "cookie"}), \
             mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "手动发货失败，请稍后重试")
        fake_db.get_order_by_id.assert_called_once_with(
            "order-deliver-1",
            account_id="acc-order-1",
            user_id=1,
        )
        fake_db.get_item_info.assert_called_once_with("acc-order-1", "item-deliver-1")

    def test_refresh_order_status_scopes_lookup_by_account_id(self):
        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-refresh-1",
            "account_id": "acc-order-1",
            "order_status": "pending_ship",
            "item_id": "item-refresh-1",
            "buyer_id": "buyer-refresh-1",
            "sid": "sid-refresh-1",
        }
        managed_call = mock.AsyncMock(
            side_effect=reply_server.HTTPException(
                status_code=400,
                detail="账号 acc-order-1 未运行，请先启动账号",
            )
        )

        async def invoke():
            return await reply_server.refresh_order_status(
                "order-refresh-1",
                account_id="acc-order-1",
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"acc-order-1": "cookie"}), \
             mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
             mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(invoke())

        self.assertFalse(result["updated"])
        self.assertIn("未运行", result["message"])
        fake_db.get_order_by_id.assert_called_once_with(
            "order-refresh-1",
            account_id="acc-order-1",
            user_id=1,
        )

    def test_refresh_order_status_stops_masking_runtime_failures_as_business_payload(self):
        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-refresh-1",
            "account_id": "acc-order-1",
            "order_status": "pending_ship",
            "item_id": "item-refresh-1",
            "buyer_id": "buyer-refresh-1",
            "sid": "sid-refresh-1",
        }
        managed_call = mock.AsyncMock(side_effect=RuntimeError("order refresh exploded"))

        async def invoke():
            return await reply_server.refresh_order_status(
                "order-refresh-1",
                account_id="acc-order-1",
                current_user={"user_id": 1, "username": "admin"},
            )

        with mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"acc-order-1": "cookie"}), \
             mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
             mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "刷新订单状态失败，请稍后重试")
        fake_db.get_order_by_id.assert_called_once_with(
            "order-refresh-1",
            account_id="acc-order-1",
            user_id=1,
        )


class ReplyServerAccountBrowserRuntimeJanitorTest(_ReplyServerModuleBindingMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        task = getattr(reply_server.app.state, "account_browser_runtime_janitor_task", None)
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        reply_server.app.state.account_browser_runtime_janitor_task = None

    async def test_account_browser_runtime_janitor_runs_async_and_sync_cleanup_each_tick(self):
        cleanup_async = mock.AsyncMock(return_value=1)
        cleanup_sync = mock.Mock(return_value=1)

        with mock.patch.object(
            reply_server.account_browser_runtime_manager,
            "cleanup_idle_runtimes",
            cleanup_async,
        ), mock.patch.object(
            reply_server.account_browser_runtime_manager,
            "cleanup_idle_runtimes_sync",
            cleanup_sync,
        ), mock.patch.object(
            reply_server.asyncio,
            "sleep",
            new=mock.AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await reply_server.account_browser_runtime_janitor(interval_seconds=1)

        cleanup_async.assert_awaited_once_with()
        cleanup_sync.assert_called_once_with()

    async def test_start_account_browser_runtime_janitor_stores_created_task_on_app_state(self):
        async def fake_janitor(interval_seconds=60):
            await asyncio.sleep(3600)

        with mock.patch.object(reply_server, "account_browser_runtime_janitor", new=fake_janitor):
            await reply_server.start_account_browser_runtime_janitor()

        task = getattr(reply_server.app.state, "account_browser_runtime_janitor_task", None)
        self.assertIsInstance(task, asyncio.Task)
        self.assertFalse(task.done())

    async def test_stop_account_browser_runtime_janitor_cancels_and_clears_task(self):
        task = asyncio.create_task(asyncio.sleep(3600))
        reply_server.app.state.account_browser_runtime_janitor_task = task

        await reply_server.stop_account_browser_runtime_janitor()

        self.assertTrue(task.cancelled())
        self.assertIsNone(getattr(reply_server.app.state, "account_browser_runtime_janitor_task", None))


class ReplyServerScheduledTaskLifecycleTest(_ReplyServerModuleBindingMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        task = getattr(reply_server.app.state, "scheduled_task_checker_task", None)
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        reply_server.app.state.scheduled_task_checker_task = None

    async def test_start_scheduled_task_checker_reuses_existing_running_task(self):
        existing_task = asyncio.create_task(asyncio.sleep(3600))
        reply_server.app.state.scheduled_task_checker_task = existing_task

        with mock.patch("asyncio.create_task", side_effect=AssertionError("startup must not spawn duplicate scheduled task checker")):
            await reply_server.start_scheduled_task_checker()

        self.assertIs(reply_server.app.state.scheduled_task_checker_task, existing_task)

    async def test_stop_scheduled_task_checker_cancels_and_clears_task(self):
        task = asyncio.create_task(asyncio.sleep(3600))
        reply_server.app.state.scheduled_task_checker_task = task

        await reply_server.stop_scheduled_task_checker()

        self.assertTrue(task.cancelled())
        self.assertIsNone(getattr(reply_server.app.state, "scheduled_task_checker_task", None))


class ReplyServerRestartApplicationTest(_ReplyServerModuleBindingMixin, unittest.IsolatedAsyncioTestCase):
    async def test_restart_application_uses_hidden_helper_instead_of_spawning_second_start_process_immediately(self):
        scheduled = {}

        def fake_create_task(coro):
            scheduled["coro"] = coro
            return mock.Mock()

        popen = mock.Mock()

        with mock.patch.object(reply_server, "log_with_user"), \
             mock.patch.object(reply_server.asyncio, "create_task", side_effect=fake_create_task), \
             mock.patch.object(reply_server.asyncio, "sleep", new=mock.AsyncMock()), \
             mock.patch.object(reply_server.sys, "platform", "win32"), \
             mock.patch.object(reply_server.sys, "executable", "C:\\Python\\python.exe"), \
             mock.patch.object(reply_server.sys, "argv", ["Start.py"]), \
             mock.patch.object(reply_server.os, "getcwd", return_value="C:\\repo"), \
             mock.patch.object(reply_server.os, "getpid", return_value=4321), \
             mock.patch.object(reply_server.subprocess, "Popen", popen), \
             mock.patch.object(reply_server.os, "_exit", side_effect=SystemExit(0)):
            result = await reply_server.restart_application(
                current_user={"user_id": 1, "username": "admin", "is_admin": True}
            )

            self.assertTrue(result["success"])
            self.assertIn("coro", scheduled)

            with self.assertRaises(SystemExit):
                await scheduled["coro"]

        popen.assert_called_once()
        command = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(command[0], "powershell.exe")
        self.assertIn("-WindowStyle", command)
        self.assertIn("Hidden", command)
        self.assertIn("Start-Process -FilePath $python -ArgumentList @($script)", command[-1])
        self.assertIn("Get-Process -Id $parentPid", command[-1])
        self.assertNotIn("CREATE_NEW_CONSOLE", command[-1])
        self.assertEqual(kwargs.get("cwd"), "C:\\repo")

    async def test_restart_application_rejects_admin_username_when_is_admin_snapshot_is_false(self):
        with mock.patch.object(reply_server, "log_with_user"), \
             mock.patch.object(reply_server.asyncio, "create_task") as create_task:
            with self.assertRaises(reply_server.HTTPException) as raised:
                await reply_server.restart_application(
                    current_user={"user_id": 1, "username": "admin", "is_admin": False}
                )

        self.assertEqual(403, raised.exception.status_code)
        self.assertEqual("只有管理员可以重启应用", raised.exception.detail)
        create_task.assert_not_called()


class ReplyServerFaceVerificationScreenshotAccessTest(_ReplyServerModuleBindingMixin, unittest.IsolatedAsyncioTestCase):
    async def test_get_account_face_verification_screenshot_accepts_string_user_id_snapshot(self):
        current_user = {"user_id": "7", "username": "demo-user", "is_admin": False}
        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"user_id": 7}
        original_sessions = dict(reply_server.password_login_sessions)
        reply_server.password_login_sessions.clear()

        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot_path = Path(tmpdir) / "face_verify_acc-1_latest.jpg"
            screenshot_path.write_bytes(b"jpg")
            screenshot_info = {
                "path": "static/uploads/images/face_verify_acc-1_latest.jpg",
                "created_time_str": "2026-05-27 10:00:00",
            }
            reply_server.password_login_sessions["face-shot-string-user"] = {
                "account_id": "acc-1",
                "status": "verification_required",
                "screenshot_path": str(screenshot_path),
                "timestamp": reply_server.time.time(),
                "completed_at": None,
                "user_id": 7,
            }

            try:
                with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
                    reply_server,
                    "_build_face_verification_screenshot_info",
                    return_value=screenshot_info,
                ) as build_screenshot_info, mock.patch.object(reply_server, "log_with_user"):
                    result = await reply_server.get_account_face_verification_screenshot(
                        "acc-1",
                        current_user=current_user,
                    )
            finally:
                reply_server.password_login_sessions.clear()
                reply_server.password_login_sessions.update(original_sessions)

        self.assertEqual({"success": True, "screenshot": screenshot_info}, result)
        fake_db.get_cookie_details.assert_called_once_with("acc-1")
        build_screenshot_info.assert_called_once_with("acc-1", str(screenshot_path))

    async def test_get_account_face_verification_screenshot_allows_non_default_admin_to_bypass_ownership_check(self):
        current_user = {"user_id": 99, "username": "ops-admin", "is_admin": True}
        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"user_id": 1}

        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot_path = Path(tmpdir) / "face_verify_acc-1_latest.jpg"
            screenshot_path.write_bytes(b"jpg")
            screenshot_info = {
                "path": "static/uploads/images/face_verify_acc-1_latest.jpg",
                "created_time_str": "2026-05-27 10:00:00",
            }

            with mock.patch.object(reply_server, "db_manager", fake_db), \
                 mock.patch.object(
                     reply_server,
                     "_get_latest_password_login_session_for_account",
                     return_value={
                         "status": "verification_required",
                         "screenshot_path": str(screenshot_path),
                     },
                 ) as get_session, \
                 mock.patch.object(
                     reply_server,
                     "_build_face_verification_screenshot_info",
                     return_value=screenshot_info,
                 ) as build_screenshot_info, \
                 mock.patch.object(reply_server, "log_with_user"):
                result = await reply_server.get_account_face_verification_screenshot(
                    "acc-1",
                    current_user=current_user,
                )

        self.assertEqual({"success": True, "screenshot": screenshot_info}, result)
        fake_db.get_cookie_details.assert_not_called()
        get_session.assert_called_once_with("acc-1", user_id=None)
        build_screenshot_info.assert_called_once_with("acc-1", str(screenshot_path))

    async def test_delete_account_face_verification_screenshot_accepts_string_user_id_snapshot(self):
        current_user = {"user_id": "7", "username": "demo-user", "is_admin": False}
        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"user_id": 7}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_delete_account_face_verification_screenshots",
            return_value=1,
        ) as delete_screenshots, mock.patch.object(reply_server, "log_with_user"):
            result = await reply_server.delete_account_face_verification_screenshot(
                "acc-1",
                current_user=current_user,
            )

        self.assertEqual(
            {"success": True, "message": "已删除 1 个验证截图", "deleted_count": 1},
            result,
        )
        fake_db.get_cookie_details.assert_called_once_with("acc-1")
        delete_screenshots.assert_called_once_with("acc-1", current_user=current_user)

    async def test_delete_account_face_verification_screenshot_allows_non_default_admin_to_bypass_ownership_check(self):
        current_user = {"user_id": 99, "username": "ops-admin", "is_admin": True}
        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"user_id": 1}

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(
                 reply_server,
                 "_delete_account_face_verification_screenshots",
                 return_value=2,
             ) as delete_screenshots, \
             mock.patch.object(reply_server, "log_with_user"):
            result = await reply_server.delete_account_face_verification_screenshot(
                "acc-1",
                current_user=current_user,
            )

        self.assertEqual(
            {"success": True, "message": "已删除 2 个验证截图", "deleted_count": 2},
            result,
        )
        fake_db.get_cookie_details.assert_not_called()
        delete_screenshots.assert_called_once_with("acc-1", current_user=current_user)


class ReplyServerDashboardSalesScopeRuntimeTest(_ReplyServerModuleBindingMixin, unittest.IsolatedAsyncioTestCase):
    async def test_sales_routes_use_module_bound_database_for_current_user_scope(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-demo-1"]
        fake_db.execute_query.side_effect = [
            [("12.50", "sales-ts-1", "finished")],
            [("12.50", "summary-ts-1", "finished")],
        ]

        shadow_db = mock.Mock()
        shadow_db.get_account_ids.side_effect = AssertionError("销售额接口不该绕开 reply_server.db_manager")
        shadow_db.execute_query.side_effect = AssertionError("销售额查询不该绕开 reply_server.db_manager")
        db_manager_module = sys.modules["db_manager"]
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        fixed_now = datetime(2026, 5, 27, 12, 0, 0)

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(db_manager_module, "db_manager", shadow_db), \
             mock.patch.object(reply_server, "local_date_to_utc_start", side_effect=lambda value: f"utc-start:{value}"), \
             mock.patch.object(reply_server, "local_date_to_utc_end_exclusive", side_effect=lambda value: f"utc-end:{value}"), \
             mock.patch.object(reply_server, "utc_timestamp_to_local_date_string", return_value="2026-05-01"), \
             mock.patch.object(reply_server, "utc_timestamp_to_local_datetime", return_value=fixed_now), \
             mock.patch.object(reply_server, "get_local_now", return_value=fixed_now), \
             mock.patch.object(reply_server, "is_sales_eligible_order_status", return_value=True):
            sales_result = await reply_server.get_sales_data(
                start_date="2026-05-01",
                end_date="2026-05-31",
                user_info=current_user,
            )
            summary_result = await reply_server.get_sales_summary(user_info=current_user)

        self.assertEqual(
            sales_result,
            {
                "success": True,
                "data": {
                    "sales": [{"date": "2026-05-01", "amount": 12.5}],
                    "total": 12.5,
                    "count": 1,
                },
                "message": "获取销售额数据成功",
            },
        )
        self.assertEqual(
            summary_result,
            {
                "success": True,
                "data": {
                    "today_sales": 12.5,
                    "week_sales": 12.5,
                    "month_sales": 12.5,
                    "update_time": "2026-05-27 12:00:00",
                },
                "message": "获取销售额摘要成功",
            },
        )
        self.assertEqual([mock.call(7), mock.call(7)], fake_db.get_account_ids.call_args_list)
        self.assertEqual(2, fake_db.execute_query.call_count)

        sales_query, sales_params = fake_db.execute_query.call_args_list[0].args
        summary_query, summary_params = fake_db.execute_query.call_args_list[1].args
        self.assertIn("FROM orders WHERE account_id IN (?)", sales_query)
        self.assertEqual(["acc-demo-1", "utc-start:2026-05-01", "utc-end:2026-05-31"], sales_params)
        self.assertIn("FROM orders WHERE", summary_query)
        self.assertEqual(["utc-start:2026-05-01", "acc-demo-1"], summary_params)


class ReplyServerQrLoginSessionIsolationTest(_ReplyServerModuleBindingMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self._original_qr_sessions = dict(reply_server.qr_login_manager.sessions)
        reply_server.qr_login_manager.sessions.clear()

        def restore_sessions():
            reply_server.qr_login_manager.sessions.clear()
            reply_server.qr_login_manager.sessions.update(self._original_qr_sessions)

        self.addCleanup(restore_sessions)

    async def test_generate_qr_code_clears_previous_same_account_session_tracking_before_regeneration(self):
        old_session_id = "old-qr-session"
        reply_server.qr_check_processed.clear()
        reply_server.qr_check_locks.clear()
        reply_server.qr_check_processed[old_session_id] = {
            "processed": False,
            "processing": True,
            "timestamp": 123.0,
        }
        reply_server.qr_check_locks[old_session_id] = asyncio.Lock()
        self.addCleanup(reply_server.qr_check_processed.clear)
        self.addCleanup(reply_server.qr_check_locks.clear)

        current_user = {
            "user_id": 1,
            "username": "admin",
            "is_admin": True,
        }

        with mock.patch.object(
            reply_server.db_manager,
            "get_cookie_binding_info",
            return_value={"id": "1"},
        ), mock.patch.object(
            reply_server.db_manager,
            "assert_cookie_belongs_to_user",
            return_value=None,
        ), mock.patch.object(
            reply_server.qr_login_manager,
            "cleanup_expired_sessions",
        ), mock.patch.object(
            reply_server.qr_login_manager,
            "invalidate_account_sessions",
            return_value=[old_session_id],
        ) as invalidate_sessions, mock.patch.object(
            reply_server.qr_login_manager,
            "generate_qr_code",
            new=mock.AsyncMock(
                return_value={
                    "success": True,
                    "session_id": "new-qr-session",
                    "qr_code_url": "data:image/png;base64,ZmFrZQ==",
                }
            ),
        ):
            response = await reply_server.generate_qr_code(
                request=reply_server.QRLoginGenerateRequest(account_id="1"),
                current_user=current_user,
            )

        invalidate_sessions.assert_called_once_with(
            account_id="1",
            user_id=1,
            reason="qr_login_regenerated_same_account",
        )
        self.assertNotIn(old_session_id, reply_server.qr_check_processed)
        self.assertNotIn(old_session_id, reply_server.qr_check_locks)
        self.assertTrue(response["success"])
        self.assertEqual("1", response["account_id"])
        self.assertEqual("new-qr-session", response["session_id"])

    async def test_generate_qr_code_cleans_stale_pending_placeholder_before_ownership_check(self):
        stale_placeholder = {"present": True}
        current_user = {
            "user_id": 1,
            "username": "admin",
            "is_admin": True,
        }

        def fake_get_cookie_binding_info(_account_id):
            if stale_placeholder["present"]:
                return {"account_id": "acc-stale-1", "user_id": 2, "bind_status": "pending_bind"}
            return None

        def fake_delete_placeholder(_account_id, user_id=None):
            self.assertEqual("acc-stale-1", _account_id)
            self.assertEqual(2, user_id)
            stale_placeholder["present"] = False
            return True

        with mock.patch.object(
            reply_server.db_manager,
            "get_cookie_binding_info",
            side_effect=fake_get_cookie_binding_info,
        ), mock.patch.object(
            reply_server.db_manager,
            "assert_cookie_belongs_to_user",
            side_effect=PermissionError("stale placeholder should have been cleaned first"),
        ) as assert_ownership, mock.patch.object(
            reply_server.db_manager,
            "delete_pending_cookie_placeholder",
            side_effect=fake_delete_placeholder,
        ) as delete_placeholder, mock.patch.object(
            reply_server,
            "_has_active_qr_binding_session",
            return_value=False,
        ), mock.patch.object(
            reply_server.db_manager,
            "create_cookie_account_placeholder",
        return_value=True,
        ) as create_placeholder, mock.patch.object(
            reply_server.qr_login_manager,
            "cleanup_expired_sessions",
        ), mock.patch.object(
            reply_server.qr_login_manager,
            "invalidate_account_sessions",
            return_value=[],
        ), mock.patch.object(
            reply_server.qr_login_manager,
            "generate_qr_code",
            new=mock.AsyncMock(
                return_value={
                    "success": True,
                    "session_id": "new-stale-session",
                    "qr_code_url": "data:image/png;base64,ZmFrZQ==",
                }
            ),
        ), mock.patch.object(reply_server, "log_with_user"):
            response = await reply_server.generate_qr_code(
                request=reply_server.QRLoginGenerateRequest(account_id="acc-stale-1"),
                current_user=current_user,
            )

        self.assertTrue(response["success"])
        delete_placeholder.assert_called_once_with("acc-stale-1", user_id=2)
        assert_ownership.assert_not_called()
        create_placeholder.assert_called_once_with(
            "acc-stale-1",
            1,
            bind_status="pending_bind",
        )

    async def test_cancelled_qr_session_releases_pending_placeholder(self):
        from utils.qr_login import QRLoginSession

        session = QRLoginSession("qr-cancel-1", user_id=7, account_id="acc-cancel-1")
        reply_server.qr_login_manager.sessions[session.session_id] = session

        with mock.patch.object(
            reply_server.db_manager,
            "delete_pending_cookie_placeholder",
            return_value=True,
            create=True,
        ) as delete_placeholder:
            updated = reply_server.qr_login_manager.update_session_fields(
                session.session_id,
                status="cancelled",
                error_message="用户取消登录",
            )

        self.assertEqual("cancelled", updated.status)
        delete_placeholder.assert_called_once_with("acc-cancel-1", user_id=7)

    async def test_cleanup_expired_sessions_releases_pending_placeholder_before_drop(self):
        from utils.qr_login import QRLoginSession

        session = QRLoginSession("qr-expired-1", user_id=8, account_id="acc-expired-1")
        session.created_time -= session.expire_time + 1
        reply_server.qr_login_manager.sessions[session.session_id] = session

        with mock.patch.object(
            reply_server.qr_login_manager,
            "_cleanup_session_assets",
        ), mock.patch.object(
            reply_server.db_manager,
            "delete_pending_cookie_placeholder",
            return_value=True,
            create=True,
        ) as delete_placeholder:
            reply_server.qr_login_manager.cleanup_expired_sessions()

        self.assertNotIn(session.session_id, reply_server.qr_login_manager.sessions)
        delete_placeholder.assert_called_once_with("acc-expired-1", user_id=8)

    async def test_invalidate_account_sessions_releases_pending_placeholder_for_replaced_session(self):
        from utils.qr_login import QRLoginSession

        session = QRLoginSession("qr-old-1", user_id=9, account_id="acc-replaced-1")
        reply_server.qr_login_manager.sessions[session.session_id] = session

        with mock.patch.object(
            reply_server.qr_login_manager,
            "_cleanup_session_assets",
        ), mock.patch.object(
            reply_server.db_manager,
            "delete_pending_cookie_placeholder",
            return_value=True,
            create=True,
        ) as delete_placeholder:
            replaced_session_ids = reply_server.qr_login_manager.invalidate_account_sessions(
                account_id="acc-replaced-1",
                user_id=9,
                reason="unit-test-replaced",
            )

        self.assertEqual(["qr-old-1"], replaced_session_ids)
        self.assertNotIn(session.session_id, reply_server.qr_login_manager.sessions)
        delete_placeholder.assert_called_once_with("acc-replaced-1", user_id=9)


class ReplyServerVerificationMaterialStateTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_resolve_session_verification_material_keeps_existing_screenshot_for_same_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot_path = Path(tmpdir) / "face_verify_1_latest.jpg"
            screenshot_path.write_bytes(b"fake-image")

            session = {
                "verification_type": "人脸验证",
                "screenshot_path": str(screenshot_path),
                "verification_url": "https://passport.example/face",
                "qr_code_url": None,
            }

            resolved = reply_server._resolve_session_verification_material(
                session,
                verification_type="人脸验证",
                screenshot_path=None,
                verification_url=None,
            )

            self.assertEqual(str(screenshot_path), resolved["screenshot_path"])
            self.assertEqual("https://passport.example/face", resolved["verification_url"])

    def test_resolve_session_verification_material_does_not_keep_old_screenshot_for_new_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot_path = Path(tmpdir) / "face_verify_1_latest.jpg"
            screenshot_path.write_bytes(b"fake-image")

            session = {
                "verification_type": "人脸验证",
                "screenshot_path": str(screenshot_path),
                "verification_url": "https://passport.example/face",
                "qr_code_url": None,
            }

            resolved = reply_server._resolve_session_verification_material(
                session,
                verification_type="二维码验证",
                screenshot_path=None,
                verification_url="https://passport.example/qr",
            )

            self.assertIsNone(resolved["screenshot_path"])
            self.assertEqual("https://passport.example/qr", resolved["verification_url"])

    def test_build_verification_required_status_payload_uses_processing_state_after_screenshot_is_consumed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            consumed_screenshot_path = Path(tmpdir) / "face_verify_1_latest.jpg"

            session = {
                "verification_type": "face_verify",
                "screenshot_path": str(consumed_screenshot_path),
                "verification_url": "https://passport.example/fallback",
                "qr_code_url": "https://passport.example/qr",
            }

            payload = reply_server._build_verification_required_status_payload(
                session,
                pending_completion_message="验证已提交，正在等待登录完成，请勿关闭当前页面",
            )

            self.assertEqual("verification_processing", payload["status"])
            self.assertTrue(payload["verification_pending_completion"])
            self.assertIsNone(payload["screenshot_path"])
            self.assertIsNone(payload["verification_url"])
            self.assertEqual(
                "验证已提交，正在等待登录完成，请勿关闭当前页面",
                payload["message"],
            )


    def test_build_verification_required_status_payload_hides_fallback_link_when_screenshot_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot_path = Path(tmpdir) / "face_verify_1_latest.jpg"
            screenshot_path.write_bytes(b"fake-image")

            session = {
                "verification_type": "face_verify",
                "screenshot_path": str(screenshot_path),
                "verification_url": "https://passport.example/fallback",
                "qr_code_url": None,
            }

            payload = reply_server._build_verification_required_status_payload(session)

            self.assertEqual("verification_required", payload["status"])
            self.assertEqual(str(screenshot_path), payload["screenshot_path"])
            self.assertFalse(payload["show_verification_link_button"])

    def test_build_verification_required_status_payload_shows_fallback_link_only_without_screenshot(self):
        session = {
            "verification_type": "face_verify",
            "screenshot_path": None,
            "verification_url": "https://passport.example/fallback",
            "qr_code_url": None,
        }

        payload = reply_server._build_verification_required_status_payload(session)

        self.assertEqual("verification_required", payload["status"])
        self.assertTrue(payload["show_verification_link_button"])
        self.assertEqual("https://passport.example/fallback", payload["verification_url"])

    def test_build_verification_required_status_payload_treats_auto_captcha_link_as_processing(self):
        session = {
            "verification_type": "unknown",
            "screenshot_path": None,
            "verification_url": (
                "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
                "_____tmd_____/punish?x5step=2&action=captcha&pureCaptcha="
            ),
            "qr_code_url": None,
        }

        payload = reply_server._build_verification_required_status_payload(session)

        self.assertEqual("verification_processing", payload["status"])
        self.assertFalse(payload["show_verification_link_button"])
        self.assertTrue(payload["verification_pending_completion"])
        self.assertIn("自动处理", payload["message"])

    def test_build_verification_required_status_payload_honors_explicit_processing_marker(self):
        session = {
            "verification_type": "face_verify",
            "screenshot_path": None,
            "verification_url": "https://passport.example/fallback",
            "qr_code_url": "https://passport.example/qr",
            "verification_pending_completion": True,
        }

        payload = reply_server._build_verification_required_status_payload(
            session,
            pending_completion_message="验证已提交，正在等待登录完成，请勿关闭当前页面",
        )

        self.assertEqual("verification_processing", payload["status"])
        self.assertTrue(payload["verification_pending_completion"])
        self.assertFalse(payload["show_verification_link_button"])
        self.assertIsNone(payload["verification_url"])
        self.assertEqual(
            "验证已提交，正在等待登录完成，请勿关闭当前页面",
            payload["message"],
        )


class ReplyServerVerificationUiContractTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_password_login_modal_handles_verification_processing_without_fallback_button(self):
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn("case 'verification_processing':", app_js)
        self.assertIn("showPasswordLoginQRCode(", app_js)
        self.assertIn("Boolean(data.verification_pending_completion)", app_js)
        self.assertIn("data.status === 'verification_processing'", app_js)
        self.assertIn("showVerificationLinkButton", app_js)
        self.assertIn("passwordLoginLinkContainer.style.display = showVerificationLinkButton ? 'block' : 'none';", app_js)

    def test_password_login_modal_uses_large_modal_and_screenshot_preview(self):
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn('modal-dialog modal-lg modal-dialog-centered', app_js)
        self.assertIn('max-width: min(100%, 560px); width: 100%;', app_js)


class ReplyServerPasswordLoginStabilizationTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_cna_is_restored_to_required_cookie_gate(self):
        xianyu_async = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")
        slider_source = (REPO_ROOT / "utils" / "xianyu_slider_stealth.py").read_text(encoding="utf-8")

        self.assertIn(
            "REQUIRED_SESSION_COOKIE_FIELDS = (\n"
            "    'unb',\n"
            "    'sgcookie',\n"
            "    'cookie2',\n"
            "    '_m_h5_tk',\n"
            "    '_m_h5_tk_enc',\n"
            "    't',\n"
            "    'cna',\n"
            ")",
            xianyu_async,
        )
        self.assertIn(
            "_REQUIRED_SESSION_COOKIE_FIELDS = (\n"
            "        'unb',\n"
            "        'sgcookie',\n"
            "        'cookie2',\n"
            "        '_m_h5_tk',\n"
            "        '_m_h5_tk_enc',\n"
            "        't',\n"
            "        'cna',\n"
            "    )",
            slider_source,
        )

    def test_password_login_http_success_still_reuses_runtime_when_protected_fields_missing(self):
        import XianyuAutoAsync
        import utils.xianyu_slider_stealth as slider_stealth

        class FakeLive:
            def __init__(self, cookies_str, account_id, user_id, register_instance=False):
                self.cookies_str = cookies_str
                self.account_id = account_id
                self.user_id = user_id
                self.register_instance = register_instance
                self.current_token = None

            async def preflight_token_after_password_login(self):
                return "prewarmed-token"

        def fake_run_coroutine_threadsafe(coro, loop):
            coro.close()
            return object()

        slider_instance = SimpleNamespace(
            context=object(),
            page=object(),
            _stabilize_logged_in_context_cookies=mock.Mock(
                return_value={
                    "unb": "u1",
                    "sgcookie": "sg1",
                    "cookie2": "c2",
                    "_m_h5_tk": "tk_1",
                    "_m_h5_tk_enc": "enc1",
                    "t": "t1",
                    "cna": "cna1",
                    "havana_lgc2_77": "hv1",
                }
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "PROTECTED_SESSION_COOKIE_FIELDS",
            ("unb", "sgcookie", "cookie2", "_m_h5_tk", "_m_h5_tk_enc", "t", "cna", "havana_lgc2_77"),
        ), mock.patch.object(
            XianyuAutoAsync,
            "XianyuLive",
            FakeLive,
        ), mock.patch.object(
            slider_stealth,
            "probe_cookie_verification_from_cookie",
            side_effect=[
                {
                    "status": "cookie_valid",
                    "session_cookies": {
                        "unb": "u1",
                        "sgcookie": "sg1",
                        "cookie2": "c2",
                        "_m_h5_tk": "tk_1",
                        "_m_h5_tk_enc": "enc1",
                        "t": "t1",
                    },
                },
                {
                    "status": "cookie_valid",
                    "session_cookies": {
                        "unb": "u1",
                        "sgcookie": "sg1",
                        "cookie2": "c2",
                        "_m_h5_tk": "tk_1",
                        "_m_h5_tk_enc": "enc1",
                        "t": "t1",
                        "cna": "cna1",
                        "havana_lgc2_77": "hv1",
                    },
                },
            ],
        ) as probe_mock, mock.patch.object(
            reply_server.asyncio,
            "run_coroutine_threadsafe",
            side_effect=fake_run_coroutine_threadsafe,
        ), mock.patch.object(
            reply_server,
            "_wait_threadsafe_future_result",
            return_value="prewarmed-token",
        ), mock.patch.object(reply_server, "log_with_user"):
            cookies_str, meta = reply_server._stabilize_password_login_cookies_after_login(
                cookies_str="unb=u1; sgcookie=sg1; cookie2=c2; _m_h5_tk=tk_1; _m_h5_tk_enc=enc1; t=t1",
                account_id="1",
                user_id=1,
                current_user={"user_id": 1},
                slider_instance=slider_instance,
                proxy_config=None,
                request_loop=mock.Mock(),
                preflight_timeout=10.0,
            )

        slider_instance._stabilize_logged_in_context_cookies.assert_called_once()
        self.assertEqual(2, probe_mock.call_count)
        self.assertTrue(meta["token_prewarmed"])
        self.assertTrue(meta["real_cookie_refreshed"])
        self.assertIn("cna=cna1", cookies_str)
        self.assertIn("havana_lgc2_77=hv1", cookies_str)

    def test_password_login_http_success_skips_runtime_stabilization_when_protected_fields_complete(self):
        import XianyuAutoAsync
        import utils.xianyu_slider_stealth as slider_stealth

        class FakeLive:
            def __init__(self, cookies_str, account_id, user_id, register_instance=False):
                self.cookies_str = cookies_str
                self.account_id = account_id
                self.user_id = user_id
                self.register_instance = register_instance
                self.current_token = None

            async def preflight_token_after_password_login(self):
                return "prewarmed-token"

        def fake_run_coroutine_threadsafe(coro, loop):
            coro.close()
            return object()

        slider_instance = SimpleNamespace(
            context=object(),
            page=object(),
            _stabilize_logged_in_context_cookies=mock.Mock(),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "PROTECTED_SESSION_COOKIE_FIELDS",
            ("unb", "sgcookie", "cookie2", "_m_h5_tk", "_m_h5_tk_enc", "t", "cna"),
        ), mock.patch.object(
            XianyuAutoAsync,
            "XianyuLive",
            FakeLive,
        ), mock.patch.object(
            slider_stealth,
            "probe_cookie_verification_from_cookie",
            return_value={
                "status": "cookie_valid",
                "session_cookies": {
                    "unb": "u1",
                    "sgcookie": "sg1",
                    "cookie2": "c2",
                    "_m_h5_tk": "tk_1",
                    "_m_h5_tk_enc": "enc1",
                    "t": "t1",
                    "cna": "cna1",
                },
            },
        ), mock.patch.object(
            reply_server.asyncio,
            "run_coroutine_threadsafe",
            side_effect=fake_run_coroutine_threadsafe,
        ), mock.patch.object(
            reply_server,
            "_wait_threadsafe_future_result",
            return_value="prewarmed-token",
        ), mock.patch.object(reply_server, "log_with_user"):
            _, meta = reply_server._stabilize_password_login_cookies_after_login(
                cookies_str="unb=u1; sgcookie=sg1; cookie2=c2; _m_h5_tk=tk_1; _m_h5_tk_enc=enc1; t=t1; cna=cna1",
                account_id="1",
                user_id=1,
                current_user={"user_id": 1},
                slider_instance=slider_instance,
                proxy_config=None,
                request_loop=mock.Mock(),
                preflight_timeout=10.0,
            )

        slider_instance._stabilize_logged_in_context_cookies.assert_not_called()
        self.assertTrue(meta["token_prewarmed"])
        self.assertFalse(meta["real_cookie_refreshed"])

    def test_password_login_http_success_skips_runtime_stabilization_when_only_havana_missing(self):
        import XianyuAutoAsync
        import utils.xianyu_slider_stealth as slider_stealth

        class FakeLive:
            def __init__(self, cookies_str, account_id, user_id, register_instance=False):
                self.cookies_str = cookies_str
                self.account_id = account_id
                self.user_id = user_id
                self.register_instance = register_instance
                self.current_token = None

            async def preflight_token_after_password_login(self):
                return "prewarmed-token"

        def fake_run_coroutine_threadsafe(coro, loop):
            coro.close()
            return object()

        slider_instance = SimpleNamespace(
            context=object(),
            page=object(),
            _stabilize_logged_in_context_cookies=mock.Mock(),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "PROTECTED_SESSION_COOKIE_FIELDS",
            ("unb", "sgcookie", "cookie2", "_m_h5_tk", "_m_h5_tk_enc", "t", "cna", "havana_lgc2_77"),
        ), mock.patch.object(
            XianyuAutoAsync,
            "REQUIRED_SESSION_COOKIE_FIELDS",
            ("unb", "sgcookie", "cookie2", "_m_h5_tk", "_m_h5_tk_enc", "t", "cna"),
        ), mock.patch.object(
            XianyuAutoAsync,
            "XianyuLive",
            FakeLive,
        ), mock.patch.object(
            slider_stealth,
            "probe_cookie_verification_from_cookie",
            return_value={
                "status": "cookie_valid",
                "session_cookies": {
                    "unb": "u1",
                    "sgcookie": "sg1",
                    "cookie2": "c2",
                    "_m_h5_tk": "tk_1",
                    "_m_h5_tk_enc": "enc1",
                    "t": "t1",
                    "cna": "cna1",
                    "_tb_token_": "tb1",
                    "x5sec": "x5_1",
                },
            },
        ), mock.patch.object(
            reply_server.asyncio,
            "run_coroutine_threadsafe",
            side_effect=fake_run_coroutine_threadsafe,
        ), mock.patch.object(
            reply_server,
            "_wait_threadsafe_future_result",
            return_value="prewarmed-token",
        ), mock.patch.object(reply_server, "log_with_user"):
            cookies_str, meta = reply_server._stabilize_password_login_cookies_after_login(
                cookies_str="unb=u1; sgcookie=sg1; cookie2=c2; _m_h5_tk=tk_1; _m_h5_tk_enc=enc1; t=t1; cna=cna1; _tb_token_=tb1; x5sec=x5_1",
                account_id="1",
                user_id=1,
                current_user={"user_id": 1},
                slider_instance=slider_instance,
                proxy_config=None,
                request_loop=mock.Mock(),
                preflight_timeout=10.0,
            )

        slider_instance._stabilize_logged_in_context_cookies.assert_not_called()
        self.assertTrue(meta["token_prewarmed"])
        self.assertFalse(meta["real_cookie_refreshed"])
        self.assertIn("x5sec=x5_1", cookies_str)

    def test_wait_for_context_login_hands_off_when_verification_page_disappears_but_logged_in_ui_is_visible(self):
        import utils.xianyu_slider_stealth as slider_stealth

        monitor_page = object()
        slider_like = SimpleNamespace(
            pure_user_id="2",
            last_login_error=None,
            _select_monitor_page=mock.Mock(return_value=monitor_page),
            _ensure_active_verification_session=mock.Mock(return_value=None),
            _attempt_solve_slider_on_page=mock.Mock(),
            _detect_qr_code_verification=mock.Mock(return_value=(False, None)),
            _check_login_success_by_element=mock.Mock(return_value=True),
            _probe_context_login_success=mock.Mock(
                side_effect=AssertionError("logged-in UI fast path should short-circuit before probe")
            ),
            _resolve_special_captcha_block_with_recovery=mock.Mock(return_value=None),
            _safe_page_url=mock.Mock(return_value="https://www.goofish.com/im"),
        )

        login_success, success_page = slider_stealth.XianyuSliderStealth._wait_for_context_login(
            slider_like,
            context=object(),
            fallback_page=monitor_page,
            max_wait_time=10,
            check_interval=1,
            verification_type="face_verify",
            verification_url="https://passport.goofish.com/iv/mini/identity_verify.htm",
            verification_screenshot_path="static/uploads/images/face_verify_2_latest.jpg",
        )

        self.assertTrue(login_success)
        self.assertIs(success_page, monitor_page)
        slider_like._check_login_success_by_element.assert_called_once_with(monitor_page)
        slider_like._probe_context_login_success.assert_not_called()

    def test_pending_identity_handoff_skips_reopening_verification_when_browser_warmup_already_proved_business_ready(self):
        import utils.xianyu_slider_stealth as slider_stealth

        monitor_page = object()
        slider_like = SimpleNamespace(
            pure_user_id="3",
            last_browser_cookie_warmup_probe_status={
                "login_token_fetch": True,
                "login_user_fetch": True,
            },
            last_browser_cookie_warmup_session_unready=False,
            _REQUIRED_SESSION_COOKIE_FIELDS=(
                "unb",
                "sgcookie",
                "cookie2",
                "_m_h5_tk",
                "_m_h5_tk_enc",
                "t",
                "cna",
            ),
            _IDENTITY_VERIFY_PENDING_COOKIE_FIELDS=(
                "ivActionType",
                "tmp0",
                "siv20",
                "last_u_xianyu_web",
            ),
            _detect_pending_identity_verification_cookie_state=mock.Mock(
                return_value=["ivActionType", "tmp0"]
            ),
            _select_monitor_page=mock.Mock(return_value=monitor_page),
            _detect_qr_code_verification=mock.Mock(return_value=(False, None)),
            _resolve_pending_identity_verification_url=mock.Mock(
                side_effect=AssertionError("business-ready handoff should not reopen verification url")
            ),
            _fail_login=mock.Mock(
                side_effect=AssertionError("business-ready handoff should not fail")
            ),
        )
        slider_like._has_business_ready_cookie_shape = (
            slider_stealth.XianyuSliderStealth._has_business_ready_cookie_shape.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._has_browser_cookie_warmup_probe_business_ready = (
            slider_stealth.XianyuSliderStealth._has_browser_cookie_warmup_probe_business_ready.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_accept_business_ready_cookie_handoff = (
            slider_stealth.XianyuSliderStealth._should_accept_business_ready_cookie_handoff.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._has_browser_cookie_warmup_business_ready_signal = (
            slider_stealth.XianyuSliderStealth._has_browser_cookie_warmup_business_ready_signal.__get__(
                slider_like, SimpleNamespace
            )
        )

        result = slider_stealth.XianyuSliderStealth._handle_pending_identity_verification_state(
            slider_like,
            context=object(),
            fallback_page=monitor_page,
            cookies_dict={
                "unb": "u1",
                "sgcookie": "sg1",
                "cookie2": "c2",
                "_m_h5_tk": "tk_1",
                "_m_h5_tk_enc": "enc1",
                "t": "t1",
                "_tb_token_": "tb1",
                "x5sec": "x5_1",
                "ivActionType": "face",
                "tmp0": "1",
            },
            notification_callback=None,
            notification_scene="账号密码登录",
        )

        self.assertEqual("u1", result["unb"])
        self.assertNotIn("ivActionType", result)
        self.assertNotIn("tmp0", result)
        slider_like._resolve_pending_identity_verification_url.assert_not_called()
        slider_like._fail_login.assert_not_called()

    def test_password_login_business_ready_handoff_allows_only_cna_missing(self):
        import utils.xianyu_slider_stealth as slider_stealth

        slider_instance = SimpleNamespace(
            last_cookie_business_ready=True,
            last_browser_cookie_warmup_probe_status={
                "login_token_fetch": True,
                "login_user_fetch": True,
            },
            last_browser_cookie_warmup_session_unready=False,
        )
        slider_instance._has_business_ready_cookie_shape = (
            slider_stealth.XianyuSliderStealth._has_business_ready_cookie_shape.__get__(
                slider_instance, SimpleNamespace
            )
        )
        slider_instance._has_browser_cookie_warmup_probe_business_ready = (
            slider_stealth.XianyuSliderStealth._has_browser_cookie_warmup_probe_business_ready.__get__(
                slider_instance, SimpleNamespace
            )
        )
        slider_instance._should_accept_business_ready_cookie_handoff = (
            slider_stealth.XianyuSliderStealth._should_accept_business_ready_cookie_handoff.__get__(
                slider_instance, SimpleNamespace
            )
        )

        accepted = reply_server._should_accept_password_login_business_ready_handoff(
            {
                "missing_required_fields": ["cna"],
                "incoming_cookies_dict": {
                    "unb": "u1",
                    "sgcookie": "sg1",
                    "cookie2": "c2",
                    "_m_h5_tk": "tk_1",
                    "_m_h5_tk_enc": "enc1",
                    "t": "t1",
                    "_tb_token_": "tb1",
                    "x5sec": "x5_1",
                },
            },
            slider_instance,
        )

        self.assertTrue(accepted)

    def test_pending_identity_verification_state_accepts_native_browser_success_signal(self):
        import utils.xianyu_slider_stealth as slider_stealth

        monitor_page = object()
        slider_like = SimpleNamespace(
            pure_user_id="3",
            last_cookie_business_ready=True,
            last_browser_cookie_warmup_probe_status={},
            last_live_browser_business_probe_status={
                "login_token_native": True,
                "login_user_native": True,
            },
            last_browser_cookie_warmup_session_unready=False,
            _PROTECTED_SESSION_COOKIE_FIELDS=(
                "unb",
                "sgcookie",
                "cookie2",
                "_m_h5_tk",
                "_m_h5_tk_enc",
                "t",
                "cna",
                "havana_lgc2_77",
                "_tb_token_",
            ),
            _REQUIRED_SESSION_COOKIE_FIELDS=(
                "unb",
                "sgcookie",
                "cookie2",
                "_m_h5_tk",
                "_m_h5_tk_enc",
                "t",
                "cna",
            ),
            _IDENTITY_VERIFY_PENDING_COOKIE_FIELDS=(
                "ivActionType",
                "tmp0",
            ),
            _detect_pending_identity_verification_cookie_state=mock.Mock(
                return_value=["ivActionType"]
            ),
            _select_monitor_page=mock.Mock(return_value=monitor_page),
            _detect_qr_code_verification=mock.Mock(return_value=(False, None)),
            _resolve_pending_identity_verification_url=mock.Mock(
                side_effect=AssertionError("native business-ready signal should not reopen verification url")
            ),
            _fail_login=mock.Mock(
                side_effect=AssertionError("native business-ready signal should not fail")
            ),
        )
        slider_like._has_business_ready_cookie_shape = (
            slider_stealth.XianyuSliderStealth._has_business_ready_cookie_shape.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._has_browser_cookie_warmup_probe_business_ready = (
            slider_stealth.XianyuSliderStealth._has_browser_cookie_warmup_probe_business_ready.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._has_live_browser_business_probe_ready = (
            slider_stealth.XianyuSliderStealth._has_live_browser_business_probe_ready.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_accept_business_ready_cookie_handoff = (
            slider_stealth.XianyuSliderStealth._should_accept_business_ready_cookie_handoff.__get__(
                slider_like, SimpleNamespace
            )
        )

        result = slider_stealth.XianyuSliderStealth._handle_pending_identity_verification_state(
            slider_like,
            context=object(),
            fallback_page=monitor_page,
            cookies_dict={
                "unb": "u1",
                "sgcookie": "sg1",
                "cookie2": "c2",
                "_m_h5_tk": "tk_1",
                "_m_h5_tk_enc": "enc1",
                "t": "t1",
                "_tb_token_": "tb1",
                "x5sec": "x5_1",
                "ivActionType": "face",
            },
            notification_callback=None,
            notification_scene="账号密码登录",
        )

        self.assertEqual("u1", result["unb"])
        self.assertNotIn("ivActionType", result)
        slider_like._resolve_pending_identity_verification_url.assert_not_called()
        slider_like._fail_login.assert_not_called()

    def test_finalize_logged_in_cookies_skips_duplicate_browser_warmup_after_stabilization(self):
        import utils.xianyu_slider_stealth as slider_stealth

        target_page = object()
        cookies_after_stabilize = {
            "unb": "u1",
            "sgcookie": "sg1",
            "cookie2": "c2",
            "_m_h5_tk": "tk_1",
            "_m_h5_tk_enc": "enc1",
            "t": "t1",
            "_tb_token_": "tb1",
            "x5sec": "x5_1",
        }
        slider_like = SimpleNamespace(
            pure_user_id="3",
            last_login_error=None,
            last_browser_cookie_warmup_verification_hint={
                "verification_url": "https://example.invalid/punish"
            },
            last_browser_cookie_warmup_probe_status={"login_token_fetch": True},
            last_browser_cookie_warmup_session_unready=False,
            _PROTECTED_SESSION_COOKIE_FIELDS=("cna", "havana_lgc2_77"),
            _REQUIRED_SESSION_COOKIE_FIELDS=("unb", "sgcookie", "cookie2", "_m_h5_tk", "_m_h5_tk_enc", "t"),
            _IDENTITY_VERIFY_PENDING_COOKIE_FIELDS=(),
            _snapshot_context_cookies=mock.Mock(return_value=cookies_after_stabilize),
            _stabilize_logged_in_context_cookies=mock.Mock(return_value=cookies_after_stabilize),
            _perform_browser_cookie_warmup_probes=mock.Mock(
                side_effect=AssertionError("should skip duplicate warmup")
            ),
            _consume_browser_cookie_warmup_verification_hint=mock.Mock(return_value={"status": "handled"}),
            _handle_pending_identity_verification_state=mock.Mock(return_value=None),
        )
        slider_like._has_business_ready_cookie_shape = (
            slider_stealth.XianyuSliderStealth._has_business_ready_cookie_shape.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._has_browser_cookie_warmup_probe_business_ready = (
            slider_stealth.XianyuSliderStealth._has_browser_cookie_warmup_probe_business_ready.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_accept_business_ready_cookie_handoff = (
            slider_stealth.XianyuSliderStealth._should_accept_business_ready_cookie_handoff.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._has_browser_cookie_warmup_business_ready_signal = (
            slider_stealth.XianyuSliderStealth._has_browser_cookie_warmup_business_ready_signal.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_defer_havana_cookie_chase = (
            slider_stealth.XianyuSliderStealth._should_defer_havana_cookie_chase.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_skip_redundant_browser_cookie_warmup = (
            slider_stealth.XianyuSliderStealth._should_skip_redundant_browser_cookie_warmup.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._collect_logged_in_cookie_snapshot = (
            slider_stealth.XianyuSliderStealth._collect_logged_in_cookie_snapshot.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._stabilize_and_warmup_logged_in_cookies = (
            slider_stealth.XianyuSliderStealth._stabilize_and_warmup_logged_in_cookies.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._finalize_cookie_handoff_or_fail = (
            slider_stealth.XianyuSliderStealth._finalize_cookie_handoff_or_fail.__get__(
                slider_like, SimpleNamespace
            )
        )

        result = slider_stealth.XianyuSliderStealth._finalize_logged_in_cookies(
            slider_like,
            context=object(),
            page=target_page,
            scene="账号密码登录",
        )

        self.assertEqual({"status": "handled"}, result)
        slider_like._perform_browser_cookie_warmup_probes.assert_not_called()

    def test_stabilize_and_warmup_logged_in_cookies_skips_heavy_steps_when_only_havana_missing(self):
        import utils.xianyu_slider_stealth as slider_stealth

        cookies_dict = {
            "unb": "u1",
            "sgcookie": "sg1",
            "cookie2": "c2",
            "_m_h5_tk": "tk_1",
            "_m_h5_tk_enc": "enc1",
            "t": "t1",
            "cna": "cna1",
            "_tb_token_": "tb1",
            "x5sec": "x5_1",
        }
        slider_like = SimpleNamespace(
            pure_user_id="3",
            last_cookie_business_ready=False,
            _PROTECTED_SESSION_COOKIE_FIELDS=(
                "unb",
                "sgcookie",
                "cookie2",
                "_m_h5_tk",
                "_m_h5_tk_enc",
                "t",
                "cna",
                "havana_lgc2_77",
                "_tb_token_",
            ),
            _REQUIRED_SESSION_COOKIE_FIELDS=(
                "unb",
                "sgcookie",
                "cookie2",
                "_m_h5_tk",
                "_m_h5_tk_enc",
                "t",
                "cna",
            ),
            _stabilize_logged_in_context_cookies=mock.Mock(
                side_effect=AssertionError("should not run heavy stabilization when only havana_lgc2_77 is missing")
            ),
            _perform_browser_cookie_warmup_probes=mock.Mock(
                side_effect=AssertionError("should not run browser warmup when only havana_lgc2_77 is missing")
            ),
        )
        slider_like._has_business_ready_cookie_shape = (
            slider_stealth.XianyuSliderStealth._has_business_ready_cookie_shape.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_defer_havana_cookie_chase = (
            slider_stealth.XianyuSliderStealth._should_defer_havana_cookie_chase.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._stabilize_and_warmup_logged_in_cookies = (
            slider_stealth.XianyuSliderStealth._stabilize_and_warmup_logged_in_cookies.__get__(
                slider_like, SimpleNamespace
            )
        )

        result = slider_like._stabilize_and_warmup_logged_in_cookies(
            context=object(),
            target_page=object(),
            cookies_dict=cookies_dict,
            scene="账号密码登录",
        )

        self.assertEqual(cookies_dict, result)
        self.assertTrue(slider_like.last_cookie_business_ready)

    def test_stabilize_and_warmup_logged_in_cookies_token_refresh_keeps_chasing_havana(self):
        import utils.xianyu_slider_stealth as slider_stealth

        cookies_dict = {
            "unb": "u1",
            "sgcookie": "sg1",
            "cookie2": "c2",
            "_m_h5_tk": "tk_1",
            "_m_h5_tk_enc": "enc1",
            "t": "t1",
            "cna": "cna1",
            "_tb_token_": "tb1",
            "x5sec": "x5_1",
        }
        stabilized_cookies = dict(cookies_dict)
        stabilized_cookies["havana_lgc2_77"] = "hv1"
        slider_like = SimpleNamespace(
            pure_user_id="3",
            risk_trigger_scene="token_refresh",
            last_cookie_business_ready=False,
            _PROTECTED_SESSION_COOKIE_FIELDS=(
                "unb",
                "sgcookie",
                "cookie2",
                "_m_h5_tk",
                "_m_h5_tk_enc",
                "t",
                "cna",
                "havana_lgc2_77",
                "_tb_token_",
            ),
            _REQUIRED_SESSION_COOKIE_FIELDS=(
                "unb",
                "sgcookie",
                "cookie2",
                "_m_h5_tk",
                "_m_h5_tk_enc",
                "t",
                "cna",
            ),
            _stabilize_logged_in_context_cookies=mock.Mock(return_value=stabilized_cookies),
            _perform_browser_cookie_warmup_probes=mock.Mock(
                side_effect=AssertionError("should not need browser warmup after stabilization fills havana_lgc2_77")
            ),
            _should_skip_redundant_browser_cookie_warmup=mock.Mock(return_value=False),
        )
        slider_like._has_business_ready_cookie_shape = (
            slider_stealth.XianyuSliderStealth._has_business_ready_cookie_shape.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_defer_havana_cookie_chase = (
            slider_stealth.XianyuSliderStealth._should_defer_havana_cookie_chase.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._stabilize_and_warmup_logged_in_cookies = (
            slider_stealth.XianyuSliderStealth._stabilize_and_warmup_logged_in_cookies.__get__(
                slider_like, SimpleNamespace
            )
        )

        result = slider_like._stabilize_and_warmup_logged_in_cookies(
            context=object(),
            target_page=object(),
            cookies_dict=cookies_dict,
            scene="滑块通过后验证页补处理",
        )

        self.assertEqual(stabilized_cookies, result)
        slider_like._stabilize_logged_in_context_cookies.assert_called_once()
        slider_like._perform_browser_cookie_warmup_probes.assert_not_called()

    def test_provider_humanize_drag_is_forced_for_token_refresh_even_if_env_requests_disable(self):
        import os
        import utils.xianyu_slider_stealth as slider_stealth

        slider_like = SimpleNamespace(
            pure_user_id="3",
            automation_backend="cloakbrowser",
            risk_trigger_scene="token_refresh",
        )
        slider_like._provider_owns_browser_identity = (
            slider_stealth.XianyuSliderStealth._provider_owns_browser_identity.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._is_high_risk_browser_scene = (
            slider_stealth.XianyuSliderStealth._is_high_risk_browser_scene.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_use_provider_humanize_drag = (
            slider_stealth.XianyuSliderStealth._should_use_provider_humanize_drag.__get__(
                slider_like, SimpleNamespace
            )
        )

        with mock.patch.dict(os.environ, {"XY_SLIDER_USE_PROVIDER_HUMANIZE_DRAG": "0"}, clear=False):
            self.assertTrue(slider_like._should_use_provider_humanize_drag())

    def test_provider_humanize_drag_can_still_be_disabled_for_low_risk_scene(self):
        import os
        import utils.xianyu_slider_stealth as slider_stealth

        slider_like = SimpleNamespace(
            pure_user_id="3",
            automation_backend="cloakbrowser",
            risk_trigger_scene="manual_cookie_import",
        )
        slider_like._provider_owns_browser_identity = (
            slider_stealth.XianyuSliderStealth._provider_owns_browser_identity.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._is_high_risk_browser_scene = (
            slider_stealth.XianyuSliderStealth._is_high_risk_browser_scene.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_use_provider_humanize_drag = (
            slider_stealth.XianyuSliderStealth._should_use_provider_humanize_drag.__get__(
                slider_like, SimpleNamespace
            )
        )

        with mock.patch.dict(os.environ, {"XY_SLIDER_USE_PROVIDER_HUMANIZE_DRAG": "0"}, clear=False):
            self.assertFalse(slider_like._should_use_provider_humanize_drag())

    def test_finalize_cookie_handoff_or_fail_accepts_business_ready_cookie_when_only_cna_missing(self):
        import utils.xianyu_slider_stealth as slider_stealth

        slider_like = SimpleNamespace(
            pure_user_id="3",
            _PROTECTED_SESSION_COOKIE_FIELDS=(
                "unb",
                "sgcookie",
                "cookie2",
                "_m_h5_tk",
                "_m_h5_tk_enc",
                "t",
                "cna",
                "havana_lgc2_77",
            ),
            _REQUIRED_SESSION_COOKIE_FIELDS=(
                "unb",
                "sgcookie",
                "cookie2",
                "_m_h5_tk",
                "_m_h5_tk_enc",
                "t",
                "cna",
            ),
            _IDENTITY_VERIFY_PENDING_COOKIE_FIELDS=(),
            last_browser_cookie_warmup_session_unready=False,
            last_browser_cookie_warmup_probe_status={
                "login_token_fetch": True,
                "login_user_fetch": True,
            },
            _log_cookie_snapshot_integrity=mock.Mock(),
            _fail_login=mock.Mock(return_value=False),
        )
        slider_like._has_business_ready_cookie_shape = (
            slider_stealth.XianyuSliderStealth._has_business_ready_cookie_shape.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._has_browser_cookie_warmup_probe_business_ready = (
            slider_stealth.XianyuSliderStealth._has_browser_cookie_warmup_probe_business_ready.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_accept_business_ready_cookie_handoff = (
            slider_stealth.XianyuSliderStealth._should_accept_business_ready_cookie_handoff.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._finalize_cookie_handoff_or_fail = (
            slider_stealth.XianyuSliderStealth._finalize_cookie_handoff_or_fail.__get__(
                slider_like, SimpleNamespace
            )
        )

        result = slider_like._finalize_cookie_handoff_or_fail(
            {
                "unb": "u1",
                "sgcookie": "sg1",
                "cookie2": "c2",
                "_m_h5_tk": "tk_1",
                "_m_h5_tk_enc": "enc1",
                "t": "t1",
                "_tb_token_": "tb1",
                "x5sec": "x5_1",
            },
            scene="滑块通过后验证页补处理",
        )

        self.assertIsInstance(result, dict)
        self.assertEqual("u1", result["unb"])
        slider_like._fail_login.assert_not_called()

    def test_detect_post_slider_blocking_state_allows_transient_punish_shell_to_clear(self):
        import utils.xianyu_slider_stealth as slider_stealth

        stale_frame = object()
        live_page = object()
        slider_like = SimpleNamespace(
            pure_user_id="3",
            page=live_page,
            context=None,
            _detected_slider_frame=None,
            last_verification_feedback={},
            _resolve_special_captcha_block_with_recovery=mock.Mock(
                side_effect=[
                    {
                        "kind": "punish_captcha",
                        "message": "pure captcha shell",
                        "url": "https://example.invalid/punish",
                        "title": "captcha",
                    },
                    None,
                    None,
                ]
            ),
            check_page_changed=mock.Mock(return_value=False),
            _check_login_success_by_element=mock.Mock(return_value=False),
            _probe_context_login_success=mock.Mock(return_value=(False, None, {})),
            _merge_runtime_feedback=mock.Mock(),
        )
        slider_like._confirm_post_slider_success_transition = (
            slider_stealth.XianyuSliderStealth._confirm_post_slider_success_transition.__get__(
                slider_like, SimpleNamespace
            )
        )

        with mock.patch.object(slider_stealth.time, "sleep", lambda *_args, **_kwargs: None):
            result = slider_stealth.XianyuSliderStealth._detect_post_slider_blocking_state(
                slider_like,
                primary_target=stale_frame,
            )

        self.assertIsNone(result)
        self.assertEqual(
            "post_slider_transition_cleared",
            slider_like.last_verification_feedback.get("source"),
        )
        slider_like._merge_runtime_feedback.assert_not_called()

    def test_visible_login_ui_can_handoff_to_cookie_finalize_when_context_probe_lags(self):
        import utils.xianyu_slider_stealth as slider_stealth

        monitor_page = object()
        slider_like = SimpleNamespace(
            pure_user_id="3",
            _select_monitor_page=mock.Mock(return_value=monitor_page),
            _check_login_success_by_element=mock.Mock(return_value=True),
        )

        should_handoff, handoff_page = (
            slider_stealth.XianyuSliderStealth._should_handoff_visible_login_ui_for_cookie_finalize(
                slider_like,
                context=object(),
                fallback_page=monitor_page,
            )
        )

        self.assertTrue(should_handoff)
        self.assertIs(handoff_page, monitor_page)
        slider_like._check_login_success_by_element.assert_called_once_with(monitor_page)

    def test_browser_cookie_warmup_hint_auto_slider_success_can_handoff_back_to_finalize(self):
        import utils.xianyu_slider_stealth as slider_stealth

        verify_page = SimpleNamespace()
        fallback_page = object()
        refreshed_cookies = {
            "unb": "u1",
            "sgcookie": "sg1",
            "cookie2": "c2",
            "_m_h5_tk": "tk_2",
            "_m_h5_tk_enc": "enc2",
            "t": "t1",
            "x5sec": "x5_new",
        }
        slider_like = SimpleNamespace(
            pure_user_id="3",
            last_browser_cookie_warmup_verification_hint={
                "verification_url": "https://example.invalid/punish?x5step=2",
                "verification_type": "unknown",
            },
            last_browser_cookie_warmup_session_unready=True,
            _PROTECTED_SESSION_COOKIE_FIELDS=("cna", "havana_lgc2_77"),
            _infer_browser_cookie_warmup_risk_trigger_scene=mock.Mock(return_value="token_refresh"),
            _page_has_slider=mock.Mock(return_value=True),
            _attempt_solve_slider_on_page=mock.Mock(return_value=True),
            _probe_context_login_success=mock.Mock(return_value=(False, None, {})),
            _snapshot_context_cookies=mock.Mock(return_value=refreshed_cookies),
            _select_monitor_page=mock.Mock(return_value=fallback_page),
            _detect_qr_code_verification=mock.Mock(return_value=(False, None)),
            _has_meaningful_cookie_refresh=mock.Mock(return_value=True),
            _finalize_logged_in_cookies=mock.Mock(return_value={"ok": True}),
        )
        fake_context = SimpleNamespace(new_page=mock.Mock(return_value=verify_page))
        verify_page.goto = mock.Mock()
        slider_like._recover_verification_url_with_auto_slider_then_finalize = (
            slider_stealth.XianyuSliderStealth._recover_verification_url_with_auto_slider_then_finalize.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_defer_havana_cookie_chase = (
            slider_stealth.XianyuSliderStealth._should_defer_havana_cookie_chase.__get__(
                slider_like, SimpleNamespace
            )
        )

        result = slider_stealth.XianyuSliderStealth._consume_browser_cookie_warmup_verification_hint(
            slider_like,
            fake_context,
            fallback_page,
            {
                "unb": "u1",
                "sgcookie": "sg1",
                "cookie2": "c2",
                "_m_h5_tk": "tk_1",
                "_m_h5_tk_enc": "enc1",
                "t": "t1",
                "x5sec": "x5_old",
            },
            notification_callback=None,
            notification_scene="账号密码登录",
        )

        self.assertEqual({"ok": True}, result)
        self.assertIsNone(slider_like.last_browser_cookie_warmup_verification_hint)
        self.assertFalse(slider_like.last_browser_cookie_warmup_session_unready)
        slider_like._finalize_logged_in_cookies.assert_called_once_with(
            fake_context,
            fallback_page,
            scene="浏览器业务预热验证页自动续解",
            notification_callback=None,
            notification_scene="账号密码登录",
            extra_cookie_updates=refreshed_cookies,
        )

    def test_browser_cookie_warmup_hint_skips_punish_recovery_when_only_havana_missing(self):
        import utils.xianyu_slider_stealth as slider_stealth

        slider_like = SimpleNamespace(
            pure_user_id="3",
            last_browser_cookie_warmup_verification_hint={
                "verification_url": "https://example.invalid/punish?x5step=2",
                "verification_type": "unknown",
            },
            last_browser_cookie_warmup_session_unready=True,
            last_cookie_business_ready=False,
            _PROTECTED_SESSION_COOKIE_FIELDS=(
                "unb",
                "sgcookie",
                "cookie2",
                "_m_h5_tk",
                "_m_h5_tk_enc",
                "t",
                "cna",
                "havana_lgc2_77",
                "_tb_token_",
            ),
            _REQUIRED_SESSION_COOKIE_FIELDS=(
                "unb",
                "sgcookie",
                "cookie2",
                "_m_h5_tk",
                "_m_h5_tk_enc",
                "t",
                "cna",
            ),
            _recover_verification_url_with_auto_slider_then_finalize=mock.Mock(
                side_effect=AssertionError("should not reopen punish recovery when only havana_lgc2_77 is missing")
            ),
        )
        slider_like._has_business_ready_cookie_shape = (
            slider_stealth.XianyuSliderStealth._has_business_ready_cookie_shape.__get__(
                slider_like, SimpleNamespace
            )
        )
        slider_like._should_defer_havana_cookie_chase = (
            slider_stealth.XianyuSliderStealth._should_defer_havana_cookie_chase.__get__(
                slider_like, SimpleNamespace
            )
        )

        result = slider_stealth.XianyuSliderStealth._consume_browser_cookie_warmup_verification_hint(
            slider_like,
            context=object(),
            fallback_page=object(),
            cookies_dict={
                "unb": "u1",
                "sgcookie": "sg1",
                "cookie2": "c2",
                "_m_h5_tk": "tk_1",
                "_m_h5_tk_enc": "enc1",
                "t": "t1",
                "cna": "cna1",
                "_tb_token_": "tb1",
                "x5sec": "x5_1",
            },
            notification_callback=None,
            notification_scene="账号密码登录",
        )

        self.assertIsNone(result)
        self.assertIsNone(slider_like.last_browser_cookie_warmup_verification_hint)
        self.assertFalse(slider_like.last_browser_cookie_warmup_session_unready)
        self.assertTrue(slider_like.last_cookie_business_ready)

    def test_check_login_success_by_element_accepts_empty_im_conversation_state(self):
        import utils.xianyu_slider_stealth as slider_stealth

        page = mock.Mock()
        page.query_selector.return_value = None

        slider_like = SimpleNamespace(
            pure_user_id="3",
            _collect_page_text_for_detection=mock.Mock(
                return_value="暂无会话，先休息下吧 尚未选择任何联系人 快点左侧列表聊起来吧"
            ),
            _safe_page_url=mock.Mock(return_value="https://www.goofish.com/im"),
            _is_logged_in_url=mock.Mock(return_value=True),
            _page_has_login_form=mock.Mock(return_value=False),
        )

        result = slider_stealth.XianyuSliderStealth._check_login_success_by_element(
            slider_like,
            page,
        )

        self.assertTrue(result)
        slider_like._collect_page_text_for_detection.assert_called_once_with(page)

    def test_run_defers_hard_block_feedback_until_inline_slider_recovery_fails(self):
        import utils.xianyu_slider_stealth as slider_stealth

        monitor_page = object()
        page = mock.Mock()
        page.title.return_value = "验证码拦截"
        page.content.return_value = "验证码拦截"
        page.mouse = SimpleNamespace(move=mock.Mock())

        slider_like = SimpleNamespace(
            pure_user_id="3",
            headless=True,
            disable_headless_warmup=True,
            context=object(),
            page=page,
            _managed_runtime_binding=object(),
            last_verification_feedback={},
            last_login_error=None,
            _check_date_validity=mock.Mock(return_value=True),
            _is_hard_block_page=mock.Mock(return_value=True),
            _select_monitor_page=mock.Mock(return_value=monitor_page),
            _detect_qr_code_verification=mock.Mock(return_value=(False, None)),
            _consume_inline_slider_success_after_verification_probe=mock.Mock(
                return_value=(True, {"x5sec": "ok"})
            ),
            _process_verification_requirement=mock.Mock(),
            _save_debug_snapshot=mock.Mock(),
            close_browser=mock.Mock(),
        )

        with mock.patch.object(slider_stealth.random, "uniform", return_value=0.3), \
             mock.patch.object(slider_stealth.time, "sleep", return_value=None):
            success, cookies = slider_stealth.XianyuSliderStealth.run(
                slider_like,
                "https://example.invalid/punish?x5step=2",
                require_managed_runtime=True,
            )

        self.assertTrue(success)
        self.assertEqual({"x5sec": "ok"}, cookies)
        self.assertEqual({}, slider_like.last_verification_feedback)
        slider_like._save_debug_snapshot.assert_not_called()
        slider_like._process_verification_requirement.assert_not_called()
        slider_like.close_browser.assert_called_once()

    def test_wait_for_context_login_marks_processing_when_verification_ui_exits(self):
        import utils.xianyu_slider_stealth as slider_stealth

        monitor_page = object()
        notification_callback = mock.Mock()
        slider_like = SimpleNamespace(
            pure_user_id="3",
            _REQUIRED_SESSION_COOKIE_FIELDS=(),
            _PROTECTED_SESSION_COOKIE_FIELDS=(),
            _select_monitor_page=mock.Mock(return_value=monitor_page),
            _ensure_active_verification_session=mock.Mock(return_value=None),
            _attempt_solve_slider_on_page=mock.Mock(),
            _detect_qr_code_verification=mock.Mock(return_value=(False, None)),
            _check_login_success_by_element=mock.Mock(return_value=False),
            _resolve_special_captcha_block_with_recovery=mock.Mock(return_value=None),
            _probe_context_login_success=mock.Mock(return_value=(True, monitor_page, {})),
            _detect_pending_identity_verification_cookie_state=mock.Mock(return_value=[]),
        )
        slider_like._notify_verification_processing = (
            slider_stealth.XianyuSliderStealth._notify_verification_processing.__get__(
                slider_like, SimpleNamespace
            )
        )

        login_success, success_page = slider_stealth.XianyuSliderStealth._wait_for_context_login(
            slider_like,
            context=object(),
            fallback_page=monitor_page,
            max_wait_time=10,
            check_interval=1,
            verification_type='face_verify',
            verification_url='https://passport.example/verify',
            verification_screenshot_path='static/uploads/images/face_verify_3_latest.jpg',
            notification_callback=notification_callback,
            notification_scene='账号密码登录',
        )

        self.assertTrue(login_success)
        self.assertIs(success_page, monitor_page)
        notification_callback.assert_called_once()
        self.assertEqual(
            '验证已提交，正在等待登录完成，请勿关闭当前页面',
            notification_callback.call_args.args[0],
        )
        self.assertTrue(notification_callback.call_args.kwargs['verification_pending_completion'])

    def test_wait_for_context_login_handoffs_visible_login_ui_even_if_pending_markers_remain(self):
        import utils.xianyu_slider_stealth as slider_stealth

        monitor_page = object()
        slider_like = SimpleNamespace(
            pure_user_id="3",
            _REQUIRED_SESSION_COOKIE_FIELDS=(),
            _PROTECTED_SESSION_COOKIE_FIELDS=(),
            _select_monitor_page=mock.Mock(return_value=monitor_page),
            _ensure_active_verification_session=mock.Mock(return_value=None),
            _attempt_solve_slider_on_page=mock.Mock(),
            _detect_qr_code_verification=mock.Mock(return_value=(False, None)),
            _check_login_success_by_element=mock.Mock(return_value=False),
            _resolve_special_captcha_block_with_recovery=mock.Mock(return_value=None),
            _probe_context_login_success=mock.Mock(return_value=(True, monitor_page, {"ivActionType": "1"})),
            _detect_pending_identity_verification_cookie_state=mock.Mock(return_value=["ivActionType"]),
            _should_handoff_visible_login_ui_for_cookie_finalize=mock.Mock(return_value=(True, monitor_page)),
        )
        slider_like._notify_verification_processing = (
            slider_stealth.XianyuSliderStealth._notify_verification_processing.__get__(
                slider_like, SimpleNamespace
            )
        )

        login_success, success_page = slider_stealth.XianyuSliderStealth._wait_for_context_login(
            slider_like,
            context=object(),
            fallback_page=monitor_page,
            max_wait_time=10,
            check_interval=1,
            verification_type='face_verify',
            verification_url='https://passport.example/verify',
            verification_screenshot_path='static/uploads/images/face_verify_3_latest.jpg',
            notification_callback=None,
            notification_scene='账号密码登录',
        )

        self.assertTrue(login_success)
        self.assertIs(success_page, monitor_page)
        slider_like._should_handoff_visible_login_ui_for_cookie_finalize.assert_called_once()


class ReplyServerRiskControlAdminScopeRuntimeTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_slider_verification_stats_accepts_selected_account_from_admin_global_scope(self):
        fake_stats = {
            "has_data": True,
            "total_sessions": 4,
            "total_attempts": 4,
            "success_count": 3,
            "failure_count": 1,
            "processing_count": 0,
            "completed_sessions": 4,
            "success_rate": 75.0,
            "recent_success": "2026-05-25 12:34",
            "recent_failure": "2026-05-24 10:12",
            "accounts_with_sessions": 1,
            "accounts_with_failures": 1,
            "stats_mode": "session",
            "summary_text": "已包含全部时间的滑块成功/失败统计",
            "selected_range": "all",
            "range_label": "所有",
        }

        with mock.patch.object(
            reply_server.db_manager,
            "get_account_ids",
            return_value=["acc-other-user-1"],
        ) as get_account_ids, mock.patch.object(
            reply_server.db_manager,
            "get_slider_verification_session_stats",
            return_value=dict(fake_stats),
        ) as get_stats, mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(
                reply_server.get_slider_verification_stats(
                    account_id="acc-other-user-1",
                    range_key="all",
                    admin_user={"user_id": 1, "username": "admin"},
                )
            )

        self.assertTrue(result["success"])
        get_account_ids.assert_called_once_with()
        get_stats.assert_called_once_with(account_ids=["acc-other-user-1"], range_key="all")
        self.assertEqual(result["data"]["scope_label"], "acc-other-user-1")
        self.assertEqual(result["data"]["selected_account_id"], "acc-other-user-1")
        self.assertEqual(result["data"]["total_sessions"], 4)

    def test_slider_verification_stats_aggregates_all_accounts_for_admin_default_view(self):
        fake_stats = {
            "has_data": True,
            "total_sessions": 7,
            "total_attempts": 7,
            "success_count": 5,
            "failure_count": 2,
            "processing_count": 0,
            "completed_sessions": 7,
            "success_rate": 71.4,
            "recent_success": "2026-05-26 09:00",
            "recent_failure": "2026-05-26 08:30",
            "accounts_with_sessions": 2,
            "accounts_with_failures": 1,
            "stats_mode": "session",
            "summary_text": "已按近 7 天范围统计滑块成功/失败",
            "selected_range": "7d",
            "range_label": "近 7 天",
        }

        with mock.patch.object(
            reply_server.db_manager,
            "get_account_ids",
            return_value=["acc-admin-2", "acc-admin-1"],
        ) as get_account_ids, mock.patch.object(
            reply_server.db_manager,
            "get_slider_verification_session_stats",
            return_value=dict(fake_stats),
        ) as get_stats, mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(
                reply_server.get_slider_verification_stats(
                    account_id=None,
                    range_key="7d",
                    admin_user={"user_id": 1, "username": "admin"},
                )
            )

        self.assertTrue(result["success"])
        get_account_ids.assert_called_once_with()
        get_stats.assert_called_once_with(account_ids=["acc-admin-1", "acc-admin-2"], range_key="7d")
        self.assertEqual(result["data"]["scope_label"], "全部账号")
        self.assertEqual(result["data"]["selected_account_id"], "")
        self.assertEqual(result["data"]["accounts_with_sessions"], 2)

    def test_risk_control_routes_stop_masking_database_failures_as_empty_or_normal_failures(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}

        stats_db = mock.Mock()
        stats_db.get_all_cookies.return_value = {"acc-demo-1": "cookie-value"}
        stats_db.get_slider_verification_session_stats.side_effect = RuntimeError("risk stats exploded")

        with mock.patch.object(reply_server, "db_manager", stats_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as stats_raised:
                asyncio.run(
                    reply_server.get_slider_verification_stats(
                        account_id="acc-demo-1",
                        range_key="all",
                        admin_user=admin_user,
                    )
                )
        self.assertEqual(500, stats_raised.exception.status_code)
        self.assertEqual("获取滑块验证统计失败，请稍后重试", stats_raised.exception.detail)
        stats_db.get_all_cookies.assert_called_once_with()
        stats_db.get_slider_verification_session_stats.assert_called_once_with(
            account_ids=["acc-demo-1"],
            range_key="all",
        )

        legacy_logs_db = mock.Mock()
        legacy_logs_db.get_risk_control_logs.side_effect = RuntimeError("risk logs exploded")

        with mock.patch.object(reply_server, "db_manager", legacy_logs_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as legacy_logs_raised:
                asyncio.run(
                    reply_server.get_risk_control_logs(
                        admin_user=admin_user,
                    )
                )
        self.assertEqual(500, legacy_logs_raised.exception.status_code)
        self.assertEqual("获取风控日志失败，请稍后重试", legacy_logs_raised.exception.detail)
        legacy_logs_db.get_risk_control_logs.assert_called_once_with(
            account_id=None,
            processing_status=None,
            event_type=None,
            trigger_scene=None,
            session_id=None,
            result_code=None,
            date_from=None,
            date_to=None,
            limit=100,
            offset=0,
        )

        admin_logs_db = mock.Mock()
        admin_logs_db.get_risk_control_logs.return_value = [{"id": 1, "account_id": "acc-demo-1"}]
        admin_logs_db.get_risk_control_logs_count.side_effect = RuntimeError("risk log total exploded")

        with mock.patch.object(reply_server, "db_manager", admin_logs_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as admin_logs_raised:
                asyncio.run(
                    reply_server.get_admin_risk_control_logs(
                        account_id="acc-demo-1",
                        limit=50,
                        offset=10,
                        admin_user=admin_user,
                    )
                )
        self.assertEqual(500, admin_logs_raised.exception.status_code)
        self.assertEqual("查询风控日志失败，请稍后重试", admin_logs_raised.exception.detail)
        admin_logs_db.get_risk_control_logs.assert_called_once_with(
            account_id="acc-demo-1",
            processing_status=None,
            event_type=None,
            trigger_scene=None,
            session_id=None,
            result_code=None,
            date_from=None,
            date_to=None,
            limit=50,
            offset=10,
        )
        admin_logs_db.get_risk_control_logs_count.assert_called_once_with(
            account_id="acc-demo-1",
            processing_status=None,
            event_type=None,
            trigger_scene=None,
            session_id=None,
            result_code=None,
            date_from=None,
            date_to=None,
        )

        delete_db = mock.Mock()
        delete_db.delete_risk_control_log.side_effect = RuntimeError("risk log delete exploded")

        with mock.patch.object(reply_server, "db_manager", delete_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as delete_raised:
                asyncio.run(
                    reply_server.delete_risk_control_log(
                        11,
                        admin_user=admin_user,
                    )
                )
        self.assertEqual(500, delete_raised.exception.status_code)
        self.assertEqual("删除风控日志失败，请稍后重试", delete_raised.exception.detail)
        delete_db.delete_risk_control_log.assert_called_once_with(11)

    def test_admin_accounts_fallbacks_to_database_status_when_cookie_manager_unready(self):
        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server.db_manager,
            "get_all_users",
            return_value=[{"id": 7, "username": "demo-user"}],
        ) as get_all_users, mock.patch.object(
            reply_server.db_manager,
            "get_account_ids",
            return_value=["acc-demo-1"],
        ) as get_account_ids, mock.patch.object(
            reply_server.db_manager,
            "get_cookie_list_metadata",
            return_value={"account_id": "acc-demo-1", "remark": "主账号"},
        ) as get_cookie_list_metadata, mock.patch.object(
            reply_server.db_manager,
            "get_cookie_status",
            return_value=False,
        ) as get_cookie_status, mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.get_admin_cookies(
                admin_user={"user_id": 1, "username": "admin", "is_admin": True}
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(
            result["accounts"],
            [
                {
                    "account_id": "acc-demo-1",
                    "user_id": 7,
                    "username": "demo-user",
                    "nickname": "主账号",
                    "enabled": False,
                }
            ],
        )
        get_all_users.assert_called_once_with()
        get_account_ids.assert_called_once_with(7)
        get_cookie_list_metadata.assert_called_once_with("acc-demo-1")
        get_cookie_status.assert_called_once_with("acc-demo-1")

    def test_admin_accounts_route_uses_module_bound_database(self):
        fake_db = mock.Mock()
        fake_db.get_all_users.return_value = [{"id": 7, "username": "demo-user"}]
        fake_db.get_account_ids.return_value = ["acc-demo-1"]
        fake_db.get_cookie_list_metadata.return_value = {"account_id": "acc-demo-1", "remark": "主账号"}
        fake_db.get_all_cookies.side_effect = AssertionError("管理员账号列表不该解密整包 Cookie")
        fake_db.get_cookie_details.side_effect = AssertionError("管理员账号列表不该读取完整账号详情")
        fake_db.get_cookie_status.return_value = False

        shadow_db = mock.Mock()
        for method_name in (
            "get_all_users",
            "get_account_ids",
            "get_cookie_list_metadata",
            "get_cookie_status",
        ):
            getattr(shadow_db, method_name).side_effect = AssertionError(
                "管理员账号列表不该绕开 reply_server.db_manager"
            )

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ), mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.get_admin_cookies(
                admin_user={"user_id": 1, "username": "admin", "is_admin": True}
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(
            result["accounts"],
            [
                {
                    "account_id": "acc-demo-1",
                    "user_id": 7,
                    "username": "demo-user",
                    "nickname": "主账号",
                    "enabled": False,
                }
            ],
        )
        fake_db.get_all_users.assert_called_once_with()
        fake_db.get_account_ids.assert_called_once_with(7)
        fake_db.get_cookie_list_metadata.assert_called_once_with("acc-demo-1")
        fake_db.get_cookie_status.assert_called_once_with("acc-demo-1")

    def test_admin_accounts_route_stops_masking_database_failures_as_success_false_payload(self):
        failing_db = mock.Mock()
        failing_db.get_all_users.side_effect = RuntimeError("admin accounts exploded")

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", failing_db
        ), mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.get_admin_cookies(
                    admin_user={"user_id": 1, "username": "admin", "is_admin": True}
                )

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("admin accounts exploded", raised.exception.detail)
        failing_db.get_all_users.assert_called_once_with()

    def test_admin_accounts_route_degrades_single_account_runtime_failures_instead_of_500(self):
        fake_runtime_manager = mock.Mock()
        fake_runtime_manager.get_cookie_status.side_effect = [True, RuntimeError("runtime status exploded")]
        fake_db = mock.Mock()
        fake_db.get_all_users.return_value = [{"id": 7, "username": "demo-user"}]
        fake_db.get_account_ids.return_value = ["acc-good", "acc-bad"]
        fake_db.get_cookie_list_metadata.side_effect = [
            {"account_id": "acc-good", "remark": "主账号"},
            {"account_id": "acc-bad", "remark": "次账号"},
        ]

        with mock.patch.object(reply_server.cookie_manager, "manager", fake_runtime_manager), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.get_admin_cookies(
                admin_user={"user_id": 1, "username": "admin", "is_admin": True}
            )

        self.assertTrue(result["success"])
        self.assertEqual(2, result["total"])
        self.assertEqual(
            [
                {
                    "account_id": "acc-good",
                    "user_id": 7,
                    "username": "demo-user",
                    "nickname": "主账号",
                    "enabled": True,
                },
                {
                    "account_id": "acc-bad",
                    "user_id": 7,
                    "username": "demo-user",
                    "nickname": "读取失败",
                    "enabled": False,
                    "load_error": True,
                },
            ],
            result["accounts"],
        )
        fake_db.get_all_users.assert_called_once_with()
        fake_db.get_account_ids.assert_called_once_with(7)
        self.assertEqual(
            [mock.call("acc-good"), mock.call("acc-bad")],
            fake_db.get_cookie_list_metadata.call_args_list,
        )
        self.assertEqual(
            [mock.call("acc-good"), mock.call("acc-bad")],
            fake_runtime_manager.get_cookie_status.call_args_list,
        )


class ReplyServerLogManagementRuntimeTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_legacy_log_routes_require_admin_dependency_and_keep_runtime_payloads(self):
        route_expectations = [
            ("/logs", "GET"),
            ("/logs/stats", "GET"),
            ("/logs/clear", "POST"),
        ]
        for path, method in route_expectations:
            route = next(
                route
                for route in reply_server.app.routes
                if getattr(route, "path", None) == path
                and method in getattr(route, "methods", set())
            )
            dependency_calls = [getattr(dep.call, "__name__", str(dep.call)) for dep in route.dependant.dependencies]
            self.assertEqual(["require_admin"], dependency_calls, msg=f"{method} {path} 应仅允许管理员访问")

        fake_collector = mock.Mock()
        fake_collector.get_logs.return_value = ["2026-05-27 | INFO | hello"]
        fake_collector.get_stats.return_value = {"total_files": 2, "total_size": 128}
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}

        async def invoke():
            logs_result = await reply_server.get_logs(
                lines=5,
                level="info",
                source="worker",
                admin_user=admin_user,
            )
            stats_result = await reply_server.get_log_stats(admin_user=admin_user)
            clear_result = await reply_server.clear_logs(admin_user=admin_user)
            return logs_result, stats_result, clear_result

        with mock.patch.object(reply_server, "get_file_log_collector", return_value=fake_collector):
            logs_result, stats_result, clear_result = asyncio.run(invoke())

        self.assertEqual({"success": True, "logs": ["2026-05-27 | INFO | hello"]}, logs_result)
        self.assertEqual({"success": True, "stats": {"total_files": 2, "total_size": 128}}, stats_result)
        self.assertEqual({"success": True, "message": "日志已清空"}, clear_result)
        fake_collector.get_logs.assert_called_once_with(lines=5, level_filter="info", source_filter="worker")
        fake_collector.get_stats.assert_called_once_with()
        fake_collector.clear_logs.assert_called_once_with()

    def test_system_logs_returns_empty_success_state_when_no_log_files_exist(self):
        with mock.patch.object(
            reply_server,
            "_collect_admin_log_file_paths",
            return_value=[],
        ), mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.get_system_logs(
                admin_user={"user_id": 1, "username": "admin", "is_admin": True}
            )

        self.assertEqual(
            result,
            {
                "logs": [],
                "message": "未找到日志文件",
                "log_file": "-",
                "total_lines": 0,
                "success": True,
            },
        )

    def test_system_logs_preserve_failure_payload_when_log_read_fails(self):
        log_path = str(REPO_ROOT / "logs" / "xianyu_demo.log")

        with mock.patch.object(
            reply_server,
            "_collect_admin_log_file_paths",
            return_value=[log_path],
        ), mock.patch(
            "os.path.getmtime",
            return_value=123.0,
        ), mock.patch(
            "builtins.open",
            side_effect=OSError("boom"),
        ), mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.get_system_logs(
                admin_user={"user_id": 1, "username": "admin", "is_admin": True}
            )

        self.assertFalse(result["success"])
        self.assertEqual([], result["logs"])
        self.assertIn("读取日志文件失败", result["message"])
        self.assertIn("boom", result["message"])


class ReplyServerAccountListRuntimeFallbackTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_accounts_route_keeps_database_account_ids_when_cookie_manager_unready(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-demo-1", "acc-demo-2"]
        shadow_db = mock.Mock()
        shadow_db.get_account_ids.side_effect = AssertionError("账号列表路由不该绕开 reply_server.db_manager")

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ):
            result = reply_server.list_cookies(
                current_user={"user_id": 7, "username": "demo-user", "is_admin": False}
            )

        self.assertEqual(result, ["acc-demo-1", "acc-demo-2"])
        fake_db.get_account_ids.assert_called_once_with(7)

    def test_account_list_routes_stop_masking_database_failures_as_empty_results(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("account list exploded")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.db_manager, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                reply_server.list_cookies(current_user=current_user)

            with self.assertRaises(sqlite3.OperationalError):
                asyncio.run(reply_server.get_cookies_details(current_user=current_user))

    def test_account_details_route_stops_masking_status_and_setting_read_failures_as_default_values(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("account settings exploded")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server.db_manager,
            "get_all_cookies",
            return_value={"acc-demo-1": "cookie-value"},
        ), mock.patch.object(
            reply_server.db_manager,
            "conn",
            broken_conn,
        ):
            result = asyncio.run(reply_server.get_cookies_details(current_user=current_user))

        self.assertEqual(1, len(result))
        self.assertEqual("acc-demo-1", result[0]["account_id"])
        self.assertTrue(result[0]["load_error"])
        self.assertFalse(result[0]["enabled"])

    def test_account_details_route_fallbacks_to_database_status_when_cookie_manager_unready(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-demo-1"]
        fake_db.get_all_cookies.return_value = {"acc-demo-1": "cookie-value"}
        fake_db.get_cookie_status.return_value = False
        fake_db.get_auto_confirm.return_value = True
        fake_db.get_auto_comment.return_value = False
        fake_db.get_cookie_list_metadata.return_value = {
            "remark": "主账号",
            "username": "seller-demo",
            "has_password": True,
            "pause_duration": 17,
        }

        runtime_status = {"running": False, "error": "runtime_unavailable: CookieManager 未就绪"}
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch.object(
            reply_server,
            "_build_live_runtime_status",
            new=mock.AsyncMock(return_value=runtime_status),
        ), mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(reply_server.get_cookies_details(current_user=current_user))

        self.assertEqual(
            result,
            [
                {
                    "account_id": "acc-demo-1",
                    "value": reply_server.mask_cookie_value("cookie-value"),
                    "has_cookie_value": True,
                    "enabled": False,
                    "auto_confirm": True,
                    "auto_comment": False,
                    "remark": "主账号",
                    "username": "seller-demo",
                    "has_password": True,
                    "pause_duration": 17,
                    "runtime_status": runtime_status,
                }
            ],
        )
        fake_db.get_all_cookies.assert_called_once_with(7)
        fake_db.get_cookie_status.assert_called_once_with("acc-demo-1")
        fake_db.get_auto_confirm.assert_called_once_with("acc-demo-1")
        fake_db.get_auto_comment.assert_called_once_with("acc-demo-1")
        fake_db.get_cookie_list_metadata.assert_called_once_with("acc-demo-1")

    def test_account_details_list_route_skips_runtime_snapshot_when_disabled_for_account_filters(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-demo-1"]
        fake_db.get_cookie_status.return_value = True
        fake_db.get_auto_confirm.return_value = False
        fake_db.get_auto_comment.return_value = False
        fake_db.get_cookie_list_metadata.return_value = {
            "remark": "",
            "username": "seller-demo",
            "has_password": False,
            "pause_duration": 10,
        }
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch.object(
            reply_server,
            "_build_live_runtime_status",
            side_effect=AssertionError("account filter details fetch should skip runtime snapshot"),
        ), mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(
                reply_server.get_cookies_details(
                    include_runtime_status=False,
                    current_user=current_user,
                )
            )

        self.assertEqual(
            result,
            [
                {
                    "account_id": "acc-demo-1",
                    "value": reply_server.mask_cookie_value("cookie-value"),
                    "has_cookie_value": True,
                    "enabled": True,
                    "auto_confirm": False,
                    "auto_comment": False,
                    "remark": "",
                    "username": "seller-demo",
                    "has_password": False,
                    "pause_duration": 10,
                }
            ],
        )
        fake_db.get_account_ids.assert_called_once_with(7)
        fake_db.get_cookie_list_metadata.assert_called_once_with("acc-demo-1")

    def test_account_details_list_route_summary_only_omits_unneeded_cookie_payload_fields(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-demo-1"]
        fake_db.get_cookie_status.return_value = True
        fake_db.get_auto_confirm.return_value = True
        fake_db.get_auto_comment.return_value = True
        fake_db.get_cookie_list_metadata.return_value = {
            "remark": "主账号",
            "username": "seller-demo",
            "has_password": True,
            "pause_duration": 17,
        }
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(
                reply_server.get_cookies_details(
                    include_runtime_status=False,
                    summary_only=True,
                    current_user=current_user,
                )
            )

        self.assertEqual(
            result,
            [
                {
                    "account_id": "acc-demo-1",
                    "enabled": True,
                    "remark": "主账号",
                    "username": "seller-demo",
                    "has_password": True,
                }
            ],
        )
        fake_db.get_account_ids.assert_called_once_with(7)
        fake_db.get_cookie_list_metadata.assert_called_once_with("acc-demo-1")
        fake_db.get_auto_confirm.assert_not_called()
        fake_db.get_auto_comment.assert_not_called()

    def test_account_details_list_route_summary_with_behavior_settings_keeps_flags_without_cookie_payload(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-demo-1"]
        fake_db.get_cookie_status.return_value = True
        fake_db.get_auto_confirm.return_value = False
        fake_db.get_auto_comment.return_value = True
        fake_db.get_cookie_list_metadata.return_value = {
            "remark": "主账号",
            "username": "seller-demo",
            "has_password": True,
            "pause_duration": 17,
        }
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch.object(reply_server, "log_with_user"):
            result = asyncio.run(
                reply_server.get_cookies_details(
                    include_runtime_status=False,
                    summary_only=True,
                    include_behavior_settings=True,
                    current_user=current_user,
                )
            )

        self.assertEqual(
            result,
            [
                {
                    "account_id": "acc-demo-1",
                    "enabled": True,
                    "remark": "主账号",
                    "username": "seller-demo",
                    "has_password": True,
                    "auto_confirm": False,
                    "auto_comment": True,
                    "pause_duration": 17,
                }
            ],
        )
        fake_db.get_account_ids.assert_called_once_with(7)
        fake_db.get_cookie_list_metadata.assert_called_once_with("acc-demo-1")
        fake_db.get_auto_confirm.assert_called_once_with("acc-demo-1")
        fake_db.get_auto_comment.assert_called_once_with("acc-demo-1")

    def test_single_account_detail_route_stops_faking_not_found_when_database_read_fails(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("account detail exploded")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-demo-1"), mock.patch.object(
            reply_server.db_manager,
            "conn",
            broken_conn,
        ):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(
                    reply_server.get_cookie_account_details(
                        "acc-demo-1",
                        include_secrets=True,
                        current_user=current_user,
                    )
                )

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("获取账号详情失败，请稍后重试", raised.exception.detail)

    def test_single_account_detail_route_skips_runtime_snapshot_when_disabled_for_online_im(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {
            "account_id": "acc-demo-1",
            "value": "cookie-value",
            "username": "seller-demo",
            "password": "pw-demo",
        }
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-demo-1"), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch.object(
            reply_server,
            "_build_live_runtime_status",
            side_effect=AssertionError("online-im secrets fetch should not build runtime snapshot"),
        ):
            result = asyncio.run(
                reply_server.get_cookie_account_details(
                    "acc-demo-1",
                    include_secrets=True,
                    include_runtime_status=False,
                    current_user=current_user,
                )
            )

        self.assertEqual(fake_db.get_cookie_details.return_value, result)
        self.assertNotIn("runtime_status", result)
        fake_db.get_cookie_details.assert_called_once_with("acc-demo-1")

    def test_account_settings_and_comment_template_routes_surface_database_failures_as_server_errors(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("account settings exploded")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        def assert_http_exception(call, expected_status, expected_detail):
            with self.assertRaises(reply_server.HTTPException) as raised:
                call()
            self.assertEqual(expected_status, raised.exception.status_code)
            self.assertEqual(expected_detail, raised.exception.detail)

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-demo-1"), mock.patch.object(
            reply_server.db_manager,
            "conn",
            broken_conn,
        ):
            assert_http_exception(
                lambda: reply_server.get_auto_confirm("acc-demo-1", current_user=current_user),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.get_auto_comment("acc-demo-1", current_user=current_user),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.get_cookie_pause_duration("acc-demo-1", current_user=current_user),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.get_cookie_remark("acc-demo-1", current_user=current_user),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.get_cookie_proxy_config(
                    "acc-demo-1",
                    include_secret=True,
                    current_user=current_user,
                ),
                500,
                "获取代理配置失败，请稍后重试",
            )
            assert_http_exception(
                lambda: reply_server.update_cookie_proxy_config(
                    "acc-demo-1",
                    reply_server.ProxyConfig(
                        proxy_type="http",
                        proxy_host="127.0.0.1",
                        proxy_port=7890,
                        proxy_user="proxy-user",
                        proxy_pass="proxy-pass",
                    ),
                    current_user=current_user,
                ),
                500,
                "更新代理配置失败",
            )
            assert_http_exception(
                lambda: reply_server.update_auto_confirm(
                    "acc-demo-1",
                    reply_server.AutoConfirmUpdate(auto_confirm=True),
                    current_user=current_user,
                ),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.update_auto_comment(
                    "acc-demo-1",
                    reply_server.AutoCommentUpdate(auto_comment=False),
                    current_user=current_user,
                ),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.update_cookie_pause_duration(
                    "acc-demo-1",
                    reply_server.PauseDurationUpdate(pause_duration=15),
                    current_user=current_user,
                ),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.update_cookie_remark(
                    "acc-demo-1",
                    reply_server.RemarkUpdate(remark="主账号"),
                    current_user=current_user,
                ),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.get_comment_templates("acc-demo-1", current_user=current_user),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.add_comment_template(
                    "acc-demo-1",
                    reply_server.CommentTemplateCreate(name="tmpl", content="content", is_active=False),
                    current_user=current_user,
                ),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.update_comment_template(
                    "acc-demo-1",
                    1,
                    reply_server.CommentTemplateUpdate(name="tmpl-2", content="content-2", is_active=False),
                    current_user=current_user,
                ),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.delete_comment_template("acc-demo-1", 1, current_user=current_user),
                500,
                "account settings exploded",
            )
            assert_http_exception(
                lambda: reply_server.activate_comment_template("acc-demo-1", 1, current_user=current_user),
                500,
                "account settings exploded",
            )

    def test_account_setting_routes_use_database_when_cookie_manager_unready(self):
        fake_db = mock.Mock()
        fake_db.update_auto_confirm.return_value = True
        fake_db.get_auto_confirm.return_value = True
        fake_db.update_auto_comment.return_value = True
        fake_db.get_auto_comment.return_value = False
        fake_db.get_comment_templates.return_value = [
            {"id": 1, "name": "默认模板", "content": "感谢支持", "is_active": True}
        ]
        fake_db.update_cookie_remark.return_value = True
        fake_db.get_cookie_details.return_value = {"remark": "主账号"}
        fake_db.update_cookie_pause_duration.return_value = True
        fake_db.get_cookie_pause_duration.return_value = 17
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch.object(
            reply_server, "_ensure_account_access", return_value="acc-demo-1"
        ), mock.patch.object(reply_server, "log_with_user"):
            update_auto_confirm_result = reply_server.update_auto_confirm(
                "acc-demo-1",
                reply_server.AutoConfirmUpdate(auto_confirm=True),
                current_user=current_user,
            )
            get_auto_confirm_result = reply_server.get_auto_confirm(
                "acc-demo-1",
                current_user=current_user,
            )
            update_auto_comment_result = reply_server.update_auto_comment(
                "acc-demo-1",
                reply_server.AutoCommentUpdate(auto_comment=False),
                current_user=current_user,
            )
            get_auto_comment_result = reply_server.get_auto_comment(
                "acc-demo-1",
                current_user=current_user,
            )
            comment_templates_result = reply_server.get_comment_templates(
                "acc-demo-1",
                current_user=current_user,
            )
            update_remark_result = reply_server.update_cookie_remark(
                "acc-demo-1",
                reply_server.RemarkUpdate(remark="主账号"),
                current_user=current_user,
            )
            get_remark_result = reply_server.get_cookie_remark(
                "acc-demo-1",
                current_user=current_user,
            )
            update_pause_duration_result = reply_server.update_cookie_pause_duration(
                "acc-demo-1",
                reply_server.PauseDurationUpdate(pause_duration=17),
                current_user=current_user,
            )
            get_pause_duration_result = reply_server.get_cookie_pause_duration(
                "acc-demo-1",
                current_user=current_user,
            )

        self.assertEqual(update_auto_confirm_result["auto_confirm"], True)
        self.assertEqual(get_auto_confirm_result["auto_confirm"], True)
        self.assertEqual(update_auto_comment_result["auto_comment"], False)
        self.assertEqual(get_auto_comment_result["auto_comment"], False)
        self.assertEqual(comment_templates_result["templates"], fake_db.get_comment_templates.return_value)
        self.assertEqual(update_remark_result["remark"], "主账号")
        self.assertEqual(get_remark_result["remark"], "主账号")
        self.assertEqual(update_pause_duration_result["pause_duration"], 17)
        self.assertEqual(get_pause_duration_result["pause_duration"], 17)
        fake_db.update_auto_confirm.assert_called_once_with("acc-demo-1", True)
        fake_db.get_auto_confirm.assert_called_once_with("acc-demo-1")
        fake_db.update_auto_comment.assert_called_once_with("acc-demo-1", False)
        fake_db.get_auto_comment.assert_called_once_with("acc-demo-1")
        fake_db.get_comment_templates.assert_called_once_with("acc-demo-1")
        fake_db.update_cookie_remark.assert_called_once_with("acc-demo-1", "主账号")
        self.assertEqual(fake_db.get_cookie_details.call_count, 1)
        fake_db.update_cookie_pause_duration.assert_called_once_with("acc-demo-1", 17)
        fake_db.get_cookie_pause_duration.assert_called_once_with("acc-demo-1")

    def test_account_edit_routes_use_database_when_cookie_manager_unready(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"value": "old-cookie"}
        fake_db.update_cookie_account_info.return_value = True
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch.object(
            reply_server, "_ensure_account_access", return_value="acc-demo-1"
        ):
            update_cookie_result = reply_server.update_cookie(
                "acc-demo-1",
                reply_server.AccountCookieUpsertIn(account_id="acc-demo-1", value="new-cookie"),
                current_user=current_user,
            )
            update_account_info_result = reply_server.update_cookie_account_info(
                "acc-demo-1",
                reply_server.CookieAccountInfo(value="new-cookie", username="seller-demo", password="pw-demo"),
                current_user=current_user,
            )

        self.assertEqual(update_cookie_result, {"msg": "updated", "task_restarted": False})
        self.assertEqual(
            update_account_info_result,
            {"msg": "updated", "success": True, "task_restarted": False},
        )
        self.assertEqual(fake_db.get_cookie_details.call_count, 2)
        fake_db.update_cookie_account_info.assert_has_calls(
            [
                mock.call("acc-demo-1", cookie_value="new-cookie"),
                mock.call("acc-demo-1", cookie_value="new-cookie", username="seller-demo", password="pw-demo"),
            ]
        )

    def test_account_mutation_routes_stop_masking_database_failures_as_bad_request_or_success(self):
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        add_cookie_db = mock.Mock()
        add_cookie_db.get_cookie_binding_info.return_value = None
        add_cookie_db.get_all_cookies.side_effect = AssertionError("添加账号失败链路不该全量解密 Cookie")
        add_cookie_db.save_cookie.side_effect = RuntimeError("save cookie exploded")

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", add_cookie_db
        ), mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as add_raised:
                reply_server.add_cookie(
                    reply_server.AccountCookieUpsertIn(account_id="acc-demo-1", value="cookie-value"),
                    current_user=current_user,
                )

        self.assertEqual(500, add_raised.exception.status_code)
        self.assertEqual("添加Cookie失败，请稍后重试", add_raised.exception.detail)
        add_cookie_db.save_cookie.assert_called_once_with("acc-demo-1", "cookie-value", 7)

        update_db = mock.Mock()
        update_db.get_cookie_details.return_value = {"value": "old-cookie"}
        update_db.update_cookie_account_info.side_effect = RuntimeError("update cookie exploded")

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", update_db
        ), mock.patch.object(
            reply_server, "_ensure_account_access", return_value="acc-demo-1"
        ):
            with self.assertRaises(reply_server.HTTPException) as update_cookie_raised:
                reply_server.update_cookie(
                    "acc-demo-1",
                    reply_server.AccountCookieUpsertIn(account_id="acc-demo-1", value="new-cookie"),
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as update_account_info_raised:
                reply_server.update_cookie_account_info(
                    "acc-demo-1",
                    reply_server.CookieAccountInfo(value="new-cookie", username="seller-demo", password="pw-demo"),
                    current_user=current_user,
                )

        self.assertEqual(500, update_cookie_raised.exception.status_code)
        self.assertEqual("更新Cookie失败，请稍后重试", update_cookie_raised.exception.detail)
        self.assertEqual(500, update_account_info_raised.exception.status_code)
        self.assertEqual("更新账号信息失败，请稍后重试", update_account_info_raised.exception.detail)

        remove_db = mock.Mock()
        remove_db.delete_cookie.side_effect = RuntimeError("delete cookie exploded")

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", remove_db
        ), mock.patch.object(
            reply_server, "_ensure_account_access", return_value="acc-demo-1"
        ):
            with self.assertRaises(reply_server.HTTPException) as remove_raised:
                reply_server.remove_cookie("acc-demo-1", current_user=current_user)

        self.assertEqual(500, remove_raised.exception.status_code)
        self.assertEqual("删除账号失败，请稍后重试", remove_raised.exception.detail)
        remove_db.delete_cookie.assert_called_once_with("acc-demo-1")

    def test_account_status_route_uses_database_when_cookie_manager_unready(self):
        fake_db = mock.Mock()
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch.object(
            reply_server, "_ensure_account_access", return_value="acc-demo-1"
        ):
            result = reply_server.update_cookie_status(
                "acc-demo-1",
                reply_server.CookieStatusIn(enabled=False),
                current_user=current_user,
            )

        self.assertEqual(
            result,
            {"msg": "status updated", "enabled": False, "runtime_synced": False},
        )
        fake_db.save_cookie_status.assert_called_once_with("acc-demo-1", False)

    def test_add_cookie_route_writes_database_when_cookie_manager_unready(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_binding_info.return_value = None
        fake_db.get_all_cookies.side_effect = AssertionError("账号创建路由不该全量解密 Cookie")
        shadow_db = mock.Mock()
        shadow_db.get_cookie_binding_info.side_effect = AssertionError("账号创建路由不该绕开 reply_server.db_manager")
        shadow_db.save_cookie.side_effect = AssertionError("账号创建路由不该绕开 reply_server.db_manager")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ), mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.add_cookie(
                reply_server.AccountCookieUpsertIn(account_id="acc-demo-1", value="cookie-value"),
                current_user=current_user,
            )

        self.assertEqual(result, {"msg": "success", "task_started": False})
        fake_db.get_cookie_binding_info.assert_called_once_with("acc-demo-1")
        fake_db.save_cookie.assert_called_once_with("acc-demo-1", "cookie-value", 7)

    def test_add_cookie_route_rejects_foreign_binding_without_loading_cookie_maps(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_binding_info.return_value = {"account_id": "acc-foreign-1", "user_id": 9}
        fake_db.get_all_cookies.side_effect = AssertionError("跨用户账号冲突检查不该全量解密 Cookie")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.add_cookie(
                    reply_server.AccountCookieUpsertIn(account_id="acc-foreign-1", value="cookie-value"),
                    current_user=current_user,
                )

        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("该账号ID(account_id)已被其他用户使用", raised.exception.detail)
        fake_db.get_cookie_binding_info.assert_called_once_with("acc-foreign-1")
        fake_db.save_cookie.assert_not_called()

    def test_persist_password_login_success_uses_binding_info_instead_of_full_cookie_scan(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_binding_info.return_value = {
            "account_id": "acc-login-1",
            "user_id": 7,
            "bound_unb": "",
            "bind_status": "pending_bind",
        }
        fake_db.get_all_cookies.side_effect = AssertionError("账密登录落库不该全量解密 Cookie")
        fake_db.update_cookie_account_info.return_value = True

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "_bind_cookie_account_unb_or_raise", return_value={}) as bind_unb_mock, \
             mock.patch.object(reply_server.cookie_manager, "manager", None):
            is_new_account = reply_server._persist_password_login_success(
                account_id="acc-login-1",
                account="demo-user",
                password="pw-demo",
                user_id=7,
                cookies_str="unb=demo; cna=token",
                merged_cookies_dict={"unb": "demo"},
                is_refresh_mode=False,
                current_user={"user_id": 7, "username": "demo-user"},
            )

        self.assertFalse(is_new_account)
        fake_db.get_cookie_binding_info.assert_called_once_with("acc-login-1")
        bind_unb_mock.assert_called_once_with("acc-login-1", "demo", 7)
        fake_db.update_cookie_account_info.assert_called_once_with(
            "acc-login-1",
            cookie_value="unb=demo; cna=token",
            username="demo-user",
            password="pw-demo",
            user_id=7,
        )

    def test_persist_manual_cookie_import_success_treats_same_user_placeholder_as_existing_without_full_cookie_scan(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_binding_info.return_value = {
            "account_id": "acc-import-1",
            "user_id": 7,
            "bound_unb": "",
            "bind_status": "pending_bind",
        }
        fake_db.get_all_cookies.side_effect = AssertionError("手动导入不该全量解密 Cookie")

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "_bind_cookie_account_unb_or_raise", return_value={}) as bind_unb_mock, \
             mock.patch.object(reply_server.cookie_manager, "manager", None):
            is_new_account = reply_server._persist_manual_cookie_import_success(
                account_id="acc-import-1",
                user_id=7,
                cookies_str="unb=demo; cna=token",
            )

        self.assertFalse(is_new_account)
        fake_db.get_cookie_binding_info.assert_called_once_with("acc-import-1")
        bind_unb_mock.assert_called_once_with("acc-import-1", "demo", 7)
        fake_db.save_cookie.assert_not_called()
        fake_db.update_cookie_account_info.assert_called_once_with("acc-import-1", cookie_value="unb=demo; cna=token")

    def test_persist_manual_cookie_import_success_rolls_back_with_user_scoped_delete(self):
        fake_db = mock.Mock()

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "trans_cookies", return_value={"unb": "demo"}), \
             mock.patch.object(reply_server, "_get_same_user_account_binding_info_or_raise", return_value=None), \
             mock.patch.object(reply_server, "_get_user_cookie_snapshot_for_account", return_value=""), \
             mock.patch.object(reply_server, "_is_cookie_account_new_or_unmaterialized", return_value=True), \
             mock.patch.object(reply_server, "_bind_cookie_account_unb_or_raise", side_effect=RuntimeError("bind failed")), \
             mock.patch.object(reply_server.cookie_manager, "manager", None):
            with self.assertRaises(RuntimeError):
                reply_server._persist_manual_cookie_import_success(
                    account_id="acc-import-rollback",
                    user_id=7,
                    cookies_str="unb=demo; cna=token",
                )

        fake_db.save_cookie.assert_called_once_with("acc-import-rollback", "unb=demo; cna=token", 7)
        fake_db.delete_cookie.assert_called_once_with("acc-import-rollback", user_id=7)

    def test_keyword_routes_use_database_when_cookie_manager_unready(self):
        fake_db = mock.Mock()
        fake_db.get_keyword_counts.return_value = {"acc-demo-1": 2}
        fake_db.save_keywords.return_value = True
        fake_db.get_keywords_with_item_id.return_value = [
            ("你好", "您好", None),
            ("价格", "99", "item-1"),
        ]
        fake_db.get_keywords_with_type.return_value = [
            {
                "keyword": "图片问候",
                "reply": "",
                "item_id": "",
                "type": "image",
                "image_url": "/static/uploads/images/demo.png",
                "item_title": "",
            }
        ]
        fake_db.get_cookie_details.return_value = {"user_id": 7}
        fake_db.delete_keyword_by_index.return_value = True
        fake_db.count_keywords_by_image_url.return_value = 0
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server.cookie_manager, "manager", None), mock.patch.object(
            reply_server, "db_manager", fake_db
        ), mock.patch.object(
            reply_server, "_ensure_account_access", return_value="acc-demo-1"
        ), mock.patch.object(reply_server, "log_with_user"), mock.patch.object(
            reply_server.image_manager, "delete_image"
        ) as delete_image:
            get_keyword_counts_result = reply_server.get_keyword_counts(current_user=current_user)
            update_keywords_result = reply_server.update_keywords(
                "acc-demo-1",
                reply_server.KeywordIn(keywords={"你好": "您好", "价格": "99"}),
                current_user=current_user,
            )
            get_keywords_result = reply_server.get_keywords(
                "acc-demo-1",
                current_user=current_user,
            )
            get_keywords_with_item_id_result = reply_server.get_keywords_with_item_id(
                "acc-demo-1",
                current_user=current_user,
            )
            get_keywords_with_type_result = reply_server.get_keywords_with_type(
                "acc-demo-1",
                current_user=current_user,
            )
            delete_keyword_result = reply_server.delete_keyword_by_index(
                "acc-demo-1",
                0,
                current_user=current_user,
            )

        self.assertEqual(get_keyword_counts_result, {"acc-demo-1": 2})
        self.assertEqual(update_keywords_result, {"msg": "updated", "count": 2})
        self.assertEqual(
            get_keywords_result,
            [
                {"keyword": "你好", "reply": "您好", "item_id": None, "type": "normal"},
                {"keyword": "价格", "reply": "99", "item_id": "item-1", "type": "item"},
            ],
        )
        self.assertEqual(
            get_keywords_with_item_id_result,
            [
                {
                    "keyword": "图片问候",
                    "reply": "",
                    "item_id": "",
                    "type": "image",
                    "image_url": "/static/uploads/images/demo.png",
                    "item_title": "",
                }
            ],
        )
        self.assertEqual(get_keywords_with_type_result, fake_db.get_keywords_with_type.return_value)
        self.assertEqual(delete_keyword_result, {"msg": "删除成功"})
        fake_db.get_keyword_counts.assert_called_once_with(user_id=7)
        fake_db.save_keywords.assert_called_once_with("acc-demo-1", [("你好", "您好"), ("价格", "99")])
        self.assertEqual(fake_db.get_keywords_with_item_id.call_count, 1)
        self.assertEqual(fake_db.get_keywords_with_type.call_count, 3)
        self.assertEqual(fake_db.get_cookie_details.call_count, 2)
        fake_db.delete_keyword_by_index.assert_called_once_with("acc-demo-1", 0)
        fake_db.count_keywords_by_image_url.assert_called_once_with("acc-demo-1", "/static/uploads/images/demo.png")
        delete_image.assert_called_once_with("/static/uploads/images/demo.png")

    def test_delete_keyword_route_keeps_shared_image_file_when_other_keywords_still_reference_it(self):
        fake_db = mock.Mock()
        fake_db.get_keywords_with_type.return_value = [
            {
                "keyword": "图片问候",
                "reply": "",
                "item_id": "",
                "type": "image",
                "image_url": "/static/uploads/images/shared-demo.png",
                "item_title": "",
            }
        ]
        fake_db.get_cookie_details.return_value = {"user_id": 7}
        fake_db.delete_keyword_by_index.return_value = True
        fake_db.count_keywords_by_image_url.return_value = 2
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server, "_ensure_account_access", return_value="acc-demo-1"
        ), mock.patch.object(reply_server, "log_with_user"), mock.patch.object(
            reply_server.image_manager, "delete_image"
        ) as delete_image:
            result = reply_server.delete_keyword_by_index(
                "acc-demo-1",
                0,
                current_user=current_user,
            )

        self.assertEqual(result, {"msg": "删除成功"})
        fake_db.delete_keyword_by_index.assert_called_once_with("acc-demo-1", 0)
        fake_db.count_keywords_by_image_url.assert_called_once_with(
            "acc-demo-1",
            "/static/uploads/images/shared-demo.png",
        )
        delete_image.assert_not_called()

    def test_image_keyword_batch_route_deletes_unused_uploaded_image_when_no_row_is_saved(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"user_id": 7}
        fake_db.check_keyword_duplicate.return_value = True
        fake_db.count_keywords_by_image_url.return_value = 0
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        request = SimpleNamespace(
            json=mock.AsyncMock(
                return_value={
                    "image_url": "/static/uploads/images/demo.png",
                    "keywords": ["图片问候"],
                    "item_ids": [""],
                }
            )
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server, "_ensure_account_access", return_value="acc-demo-1"
        ), mock.patch.object(reply_server, "log_with_user"), mock.patch.object(
            reply_server.image_manager, "delete_image"
        ) as delete_image:
            result = asyncio.run(
                reply_server.add_image_keyword_batch(
                    "acc-demo-1",
                    request,
                    current_user=current_user,
                )
            )

        self.assertEqual(
            result,
            {
                "msg": "批量添加完成",
                "success_count": 0,
                "fail_count": 1,
                "duplicates": ['"图片问候" （通用关键词）'],
                "image_url": "/static/uploads/images/demo.png",
            },
        )
        fake_db.check_keyword_duplicate.assert_called_once_with("acc-demo-1", "图片问候", None)
        fake_db.save_image_keyword.assert_not_called()
        fake_db.count_keywords_by_image_url.assert_called_once_with(
            "acc-demo-1",
            "/static/uploads/images/demo.png",
        )
        delete_image.assert_called_once_with("/static/uploads/images/demo.png")

    def test_add_image_keyword_duplicate_uses_reference_aware_image_cleanup(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"user_id": 7}
        fake_db.check_keyword_duplicate.return_value = True
        fake_db.count_keywords_by_image_url.return_value = 2
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        upload = SimpleNamespace(
            filename="demo.png",
            content_type="image/png",
            read=mock.AsyncMock(return_value=b"png"),
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server, "_ensure_account_access", return_value="acc-demo-1"
        ), mock.patch.object(
            reply_server.image_manager, "save_image", return_value="/static/uploads/images/shared-demo.png"
        ), mock.patch.object(
            reply_server.image_manager, "delete_image"
        ) as delete_image, mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(
                    reply_server.add_image_keyword(
                        "acc-demo-1",
                        keyword="图片问候",
                        item_id="",
                        image=upload,
                        current_user=current_user,
                    )
                )

        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("通用关键词 '图片问候' 已存在", raised.exception.detail)
        fake_db.count_keywords_by_image_url.assert_called_once_with(
            "acc-demo-1",
            "/static/uploads/images/shared-demo.png",
        )
        delete_image.assert_not_called()

    def test_add_image_keyword_save_failure_keeps_shared_image_when_other_keywords_still_reference_it(self):
        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"user_id": 7}
        fake_db.check_keyword_duplicate.return_value = False
        fake_db.save_image_keyword.return_value = False
        fake_db.count_keywords_by_image_url.return_value = 3
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        upload = SimpleNamespace(
            filename="demo.png",
            content_type="image/png",
            read=mock.AsyncMock(return_value=b"png"),
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server, "_ensure_account_access", return_value="acc-demo-1"
        ), mock.patch.object(
            reply_server.image_manager, "save_image", return_value="/static/uploads/images/shared-demo.png"
        ), mock.patch.object(
            reply_server.image_manager, "delete_image"
        ) as delete_image, mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(
                    reply_server.add_image_keyword(
                        "acc-demo-1",
                        keyword="图片问候",
                        item_id="",
                        image=upload,
                        current_user=current_user,
                    )
                )

        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("图片关键词保存失败，请稍后重试", raised.exception.detail)
        fake_db.save_image_keyword.assert_called_once_with(
            "acc-demo-1",
            "图片问候",
            "/static/uploads/images/shared-demo.png",
            None,
        )
        fake_db.count_keywords_by_image_url.assert_called_once_with(
            "acc-demo-1",
            "/static/uploads/images/shared-demo.png",
        )
        delete_image.assert_not_called()

    def test_ai_reply_settings_and_presets_routes_use_module_bound_database_for_current_user_scope(self):
        fake_db = mock.Mock()
        fake_db.get_all_ai_reply_settings.return_value = {
            "acc-demo-1": {"ai_enabled": True, "model_name": "qwen-plus"},
        }
        fake_db.get_ai_config_presets.return_value = [
            {"id": 3, "preset_name": "默认预设", "model_name": "qwen-plus", "api_key": "", "base_url": "", "api_type": ""}
        ]
        fake_db.save_ai_config_preset.return_value = 11
        fake_db.delete_ai_config_preset.return_value = True

        shadow_db = mock.Mock()
        shadow_db.get_all_ai_reply_settings.side_effect = AssertionError("AI回复设置列表不该绕开 reply_server.db_manager")
        shadow_db.get_ai_config_presets.side_effect = AssertionError("AI预设列表不该绕开 reply_server.db_manager")
        shadow_db.save_ai_config_preset.side_effect = AssertionError("AI预设保存不该绕开 reply_server.db_manager")
        shadow_db.delete_ai_config_preset.side_effect = AssertionError("AI预设删除不该绕开 reply_server.db_manager")
        db_manager_module = sys.modules["db_manager"]
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(db_manager_module, "db_manager", shadow_db):
            settings_result = reply_server.get_all_ai_reply_settings(current_user=current_user)
            presets_result = reply_server.list_ai_config_presets(current_user=current_user)
            save_result = reply_server.save_ai_config_preset(
                reply_server.AIConfigPreset(
                    preset_name="自定义预设",
                    model_name="gpt-4o-mini",
                    api_key="demo-key",
                    base_url="https://example.invalid/v1",
                    api_type="openai",
                ),
                current_user=current_user,
            )
            delete_result = reply_server.delete_ai_config_preset(11, current_user=current_user)

        self.assertEqual(
            settings_result,
            {"acc-demo-1": {"ai_enabled": True, "model_name": "qwen-plus"}},
        )
        self.assertEqual(presets_result, fake_db.get_ai_config_presets.return_value)
        self.assertEqual(save_result, {"message": "预设保存成功", "preset_id": 11})
        self.assertEqual(delete_result, {"message": "预设删除成功"})
        fake_db.get_all_ai_reply_settings.assert_called_once_with(user_id=7)
        self.assertEqual(2, fake_db.get_ai_config_presets.call_count)
        fake_db.save_ai_config_preset.assert_called_once_with(
            user_id=7,
            preset_name="自定义预设",
            model_name="gpt-4o-mini",
            api_key="demo-key",
            base_url="https://example.invalid/v1",
            api_type="openai",
        )
        fake_db.delete_ai_config_preset.assert_called_once_with(7, 11)

    def test_ai_reply_settings_and_presets_routes_surface_database_failures_as_server_errors(self):
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        preset_lookup_db = mock.Mock()
        preset_lookup_db.get_ai_config_presets.side_effect = RuntimeError("ai preset list exploded")

        with mock.patch.object(reply_server, "db_manager", preset_lookup_db):
            with self.assertRaises(reply_server.HTTPException) as list_raised:
                reply_server.list_ai_config_presets(current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as save_raised:
                reply_server.save_ai_config_preset(
                    reply_server.AIConfigPreset(
                        preset_name="自定义预设",
                        model_name="gpt-4o-mini",
                        api_key="demo-key",
                        base_url="https://example.invalid/v1",
                        api_type="openai",
                    ),
                    current_user=current_user,
                )

        self.assertEqual(500, list_raised.exception.status_code)
        self.assertEqual("服务器错误: ai preset list exploded", list_raised.exception.detail)
        self.assertEqual(500, save_raised.exception.status_code)
        self.assertEqual("服务器错误: ai preset list exploded", save_raised.exception.detail)
        self.assertEqual([mock.call(7), mock.call(7)], preset_lookup_db.get_ai_config_presets.call_args_list)
        preset_lookup_db.save_ai_config_preset.assert_not_called()

        delete_db = mock.Mock()
        delete_db.delete_ai_config_preset.side_effect = RuntimeError("ai preset delete exploded")

        with mock.patch.object(reply_server, "db_manager", delete_db):
            with self.assertRaises(reply_server.HTTPException) as delete_raised:
                reply_server.delete_ai_config_preset(11, current_user=current_user)

        self.assertEqual(500, delete_raised.exception.status_code)
        self.assertEqual("服务器错误: ai preset delete exploded", delete_raised.exception.detail)
        delete_db.delete_ai_config_preset.assert_called_once_with(7, 11)

    def test_ai_reply_settings_routes_stop_masking_database_failures_as_default_values_or_bad_request(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("ai settings exploded")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(
            reply_server.db_manager,
            "get_all_cookies",
            return_value={"acc-demo-1": "cookie-a"},
        ), mock.patch.object(reply_server.db_manager, "conn", broken_conn):
            with self.assertRaises(reply_server.HTTPException) as all_raised:
                reply_server.get_all_ai_reply_settings(current_user=current_user)

        self.assertEqual(500, all_raised.exception.status_code)
        self.assertEqual("服务器错误: ai settings exploded", all_raised.exception.detail)

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-demo-1"), mock.patch.object(
            reply_server.db_manager,
            "conn",
            broken_conn,
        ):
            with self.assertRaises(reply_server.HTTPException) as detail_raised:
                reply_server.get_ai_reply_settings("acc-demo-1", current_user=current_user)

        self.assertEqual(500, detail_raised.exception.status_code)
        self.assertEqual("服务器错误: ai settings exploded", detail_raised.exception.detail)

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-demo-1"), mock.patch.object(
            reply_server.cookie_manager,
            "manager",
            mock.Mock(),
        ), mock.patch.object(
            reply_server.db_manager,
            "conn",
            broken_conn,
        ):
            with self.assertRaises(reply_server.HTTPException) as save_raised:
                reply_server.update_ai_reply_settings(
                    "acc-demo-1",
                    reply_server.AIReplySettings(ai_enabled=True),
                    current_user=current_user,
                )

        self.assertEqual(500, save_raised.exception.status_code)
        self.assertEqual("服务器错误: ai settings exploded", save_raised.exception.detail)

    def test_ai_reply_update_and_test_routes_no_longer_require_runtime_manager(self):
        fake_db = mock.Mock()
        fake_db.save_ai_reply_settings.return_value = True
        fake_db.get_cookie_by_id.return_value = {"account_id": "acc-demo-1"}
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-demo-1"), \
             mock.patch.object(reply_server.cookie_manager, "manager", None), \
             mock.patch.object(reply_server.ai_reply_engine, "is_ai_enabled", return_value=True), \
             mock.patch.object(reply_server.ai_reply_engine, "generate_reply", return_value="demo-reply"):
            update_result = reply_server.update_ai_reply_settings(
                "acc-demo-1",
                reply_server.AIReplySettings(ai_enabled=True, model_name="qwen-plus"),
                current_user=current_user,
            )
            test_result = reply_server.test_ai_reply(
                "acc-demo-1",
                {"message": "你好", "item_title": "测试商品"},
                current_user=current_user,
            )

        self.assertEqual({"message": "AI回复设置更新成功"}, update_result)
        self.assertEqual(
            {"message": "测试成功", "reply": "demo-reply", "account_id": "acc-demo-1"},
            test_result,
        )
        fake_db.save_ai_reply_settings.assert_called_once()
        fake_db.get_cookie_by_id.assert_called_once_with("acc-demo-1")

    def test_default_reply_routes_use_module_bound_database_for_current_user_scope(self):
        fake_db = mock.Mock()
        fake_db.get_default_reply.return_value = {
            "enabled": True,
            "reply_content": "您好，在的",
            "reply_once": True,
        }
        fake_db.get_all_default_replies.return_value = {
            "acc-demo-1": {"enabled": True, "reply_content": "您好，在的", "reply_once": True},
        }
        fake_db.delete_default_reply.return_value = True

        shadow_db = mock.Mock()
        shadow_db.get_default_reply.side_effect = AssertionError("默认回复详情不该绕开 reply_server.db_manager")
        shadow_db.save_default_reply.side_effect = AssertionError("默认回复保存不该绕开 reply_server.db_manager")
        shadow_db.get_all_default_replies.side_effect = AssertionError("默认回复列表不该绕开 reply_server.db_manager")
        shadow_db.delete_default_reply.side_effect = AssertionError("默认回复删除不该绕开 reply_server.db_manager")
        shadow_db.clear_default_reply_records.side_effect = AssertionError("默认回复记录清理不该绕开 reply_server.db_manager")
        db_manager_module = sys.modules["db_manager"]
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(db_manager_module, "db_manager", shadow_db), \
             mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-demo-1") as ensure_access:
            detail_result = reply_server.get_default_reply("acc-demo-1", current_user=current_user)
            update_result = reply_server.update_default_reply(
                "acc-demo-1",
                reply_server.DefaultReplyIn(enabled=True, reply_content="已收到，稍后回复", reply_once=True),
                current_user=current_user,
            )
            list_result = reply_server.get_all_default_replies(current_user=current_user)
            delete_result = reply_server.delete_default_reply("acc-demo-1", current_user=current_user)
            clear_result = reply_server.clear_default_reply_records("acc-demo-1", current_user=current_user)

        self.assertEqual(detail_result, fake_db.get_default_reply.return_value)
        self.assertEqual(
            update_result,
            {"msg": "default reply updated", "enabled": True, "reply_once": True},
        )
        self.assertEqual(
            list_result,
            {"acc-demo-1": {"enabled": True, "reply_content": "您好，在的", "reply_once": True}},
        )
        self.assertEqual(delete_result, {"msg": "default reply deleted"})
        self.assertEqual(clear_result, {"msg": "default reply records cleared"})
        ensure_access.assert_has_calls(
            [
                mock.call("acc-demo-1", current_user, "访问"),
                mock.call("acc-demo-1", current_user, "操作"),
                mock.call("acc-demo-1", current_user, "操作"),
                mock.call("acc-demo-1", current_user, "操作"),
            ]
        )
        fake_db.get_default_reply.assert_called_once_with("acc-demo-1")
        fake_db.save_default_reply.assert_called_once_with("acc-demo-1", True, "已收到，稍后回复", True)
        fake_db.get_all_default_replies.assert_called_once_with(user_id=7)
        fake_db.delete_default_reply.assert_called_once_with("acc-demo-1")
        fake_db.clear_default_reply_records.assert_called_once_with("acc-demo-1")

    def test_default_reply_routes_stop_masking_database_failures_as_empty_results_or_bad_request(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("default reply exploded")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-demo-1"), \
             mock.patch.object(reply_server.db_manager, "conn", broken_conn):
            with self.assertRaises(reply_server.HTTPException) as detail_raised:
                reply_server.get_default_reply("acc-demo-1", current_user=current_user)

        self.assertEqual(500, detail_raised.exception.status_code)
        self.assertEqual("default reply exploded", detail_raised.exception.detail)

        with mock.patch.object(reply_server.db_manager, "get_all_default_replies", side_effect=sqlite3.OperationalError("default reply exploded")):
            with self.assertRaises(reply_server.HTTPException) as list_raised:
                reply_server.get_all_default_replies(current_user=current_user)

        self.assertEqual(500, list_raised.exception.status_code)
        self.assertEqual("default reply exploded", list_raised.exception.detail)

        with mock.patch.object(reply_server, "_ensure_account_access", return_value="acc-demo-1"), \
             mock.patch.object(reply_server.db_manager, "conn", broken_conn):
            with self.assertRaises(reply_server.HTTPException) as delete_raised:
                reply_server.delete_default_reply("acc-demo-1", current_user=current_user)

        self.assertEqual(500, delete_raised.exception.status_code)
        self.assertEqual("default reply exploded", delete_raised.exception.detail)

    def test_card_routes_use_module_bound_database_for_crud_calls(self):
        fake_db = mock.Mock()
        fake_db.get_all_cards.return_value = [{"id": 1, "name": "Demo Card", "type": "text"}]
        fake_db.create_card.return_value = 55
        fake_db.get_card_by_id.return_value = {"id": 1, "name": "Demo Card", "type": "text"}
        fake_db.update_card.return_value = True
        fake_db.delete_card.return_value = True

        shadow_db = mock.Mock()
        shadow_db.get_all_cards.side_effect = AssertionError("卡券列表路由不该绕开 reply_server.db_manager")
        shadow_db.create_card.side_effect = AssertionError("卡券创建路由不该绕开 reply_server.db_manager")
        shadow_db.get_card_by_id.side_effect = AssertionError("卡券详情路由不该绕开 reply_server.db_manager")
        shadow_db.update_card.side_effect = AssertionError("卡券更新路由不该绕开 reply_server.db_manager")
        shadow_db.delete_card.side_effect = AssertionError("卡券删除路由不该绕开 reply_server.db_manager")

        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ), mock.patch.object(reply_server, "log_with_user"):
            list_result = reply_server.get_cards(current_user=current_user)
            create_result = reply_server.create_card(
                {"name": "Demo Card", "type": "text", "enabled": True},
                current_user=current_user,
            )
            detail_result = reply_server.get_card(1, current_user=current_user)
            update_result = reply_server.update_card(
                1,
                {"name": "Updated Demo Card", "type": "text"},
                current_user=current_user,
            )
            delete_result = reply_server.delete_card(1, current_user=current_user)

        self.assertEqual(
            list_result,
            [
                {
                    "id": 1,
                    "name": "Demo Card",
                    "type": "text",
                    "data_count": 0,
                    "api_config": None,
                    "text_content": None,
                    "data_content": None,
                    "image_url": None,
                }
            ],
        )
        self.assertEqual(create_result, {"id": 55, "message": "卡券创建成功"})
        self.assertEqual(detail_result, fake_db.get_card_by_id.return_value)
        self.assertEqual(update_result, {"message": "卡券更新成功"})
        self.assertEqual(delete_result, {"message": "卡券删除成功"})
        fake_db.get_all_cards.assert_called_once_with(7, summary_only=True)
        fake_db.create_card.assert_called_once_with(
            name="Demo Card",
            card_type="text",
            api_config=None,
            text_content=None,
            data_content=None,
            image_url=None,
            description=None,
            enabled=True,
            delay_seconds=0,
            is_multi_spec=False,
            spec_name=None,
            spec_value=None,
            spec_name_2=None,
            spec_value_2=None,
            user_id=7,
        )
        fake_db.get_card_by_id.assert_called_once_with(1, 7)
        fake_db.update_card.assert_called_once_with(
            card_id=1,
            name="Updated Demo Card",
            card_type="text",
            api_config=None,
            text_content=None,
            data_content=None,
            image_url=None,
            description=None,
            enabled=True,
            delay_seconds=None,
            is_multi_spec=None,
            spec_name=None,
            spec_value=None,
            spec_name_2=None,
            spec_value_2=None,
            user_id=7,
        )
        fake_db.delete_card.assert_called_once_with(1, 7)

    def test_cards_list_route_redacts_bulk_card_payload_fields_not_needed_for_list(self):
        fake_db = mock.Mock()
        fake_db.get_all_cards.return_value = [
            {
                "id": 1,
                "name": "API Card",
                "type": "api",
                "api_config": {"token": "demo-token", "url": "https://example.invalid"},
                "text_content": "secret-text",
                "data_content": "line-1\nline-2",
                "enabled": True,
            }
        ]
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.get_cards(current_user=current_user)

        self.assertEqual(1, len(result))
        self.assertIsNone(result[0]["api_config"])
        self.assertIsNone(result[0]["text_content"])
        self.assertIsNone(result[0]["data_content"])
        self.assertIsNone(result[0]["image_url"])
        self.assertEqual(2, result[0]["data_count"])
        fake_db.get_all_cards.assert_called_once_with(7, summary_only=True)

    def test_card_create_surfaces_auto_delivery_rule_generation_failure_without_rolling_back_card(self):
        fake_db = mock.Mock()
        fake_db.create_card.return_value = 55
        fake_db.create_delivery_rule.side_effect = RuntimeError("delivery rule exploded")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            result = reply_server.create_card(
                {
                    "name": "Demo Card",
                    "type": "text",
                    "enabled": True,
                    "generate_delivery_rule": True,
                },
                current_user=current_user,
            )

        self.assertEqual(
            {
                "id": 55,
                "message": "卡券创建成功",
                "delivery_rule_generated": False,
                "delivery_rule_error": "对应发货规则生成失败，请稍后在自动发货中手动创建",
            },
            result,
        )
        fake_db.create_card.assert_called_once_with(
            name="Demo Card",
            card_type="text",
            api_config=None,
            text_content=None,
            data_content=None,
            image_url=None,
            description=None,
            enabled=True,
            delay_seconds=0,
            is_multi_spec=False,
            spec_name=None,
            spec_value=None,
            spec_name_2=None,
            spec_value_2=None,
            user_id=7,
        )
        fake_db.create_delivery_rule.assert_called_once_with(
            keyword="Demo Card",
            card_id=55,
            delivery_count=1,
            enabled=True,
            description="自动生成的发货规则 - 对应卡券: Demo Card",
            user_id=7,
        )

    def test_card_create_and_detail_re_raise_validation_and_not_found_http_exception(self):
        fake_db = mock.Mock()
        fake_db.get_card_by_id.return_value = None
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as create_raised:
                reply_server.create_card(
                    {"name": "多规格卡券", "type": "text", "is_multi_spec": True},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as detail_raised:
                reply_server.get_card(404, current_user=current_user)

        self.assertEqual(create_raised.exception.status_code, 400)
        self.assertEqual(create_raised.exception.detail, "多规格卡券必须提供规格名称和规格值")
        self.assertEqual(detail_raised.exception.status_code, 404)
        self.assertEqual(detail_raised.exception.detail, "卡券不存在")
        fake_db.create_card.assert_not_called()
        fake_db.get_card_by_id.assert_called_once_with(404, 7)

    def test_card_routes_stop_masking_database_failures_as_empty_list_or_not_found(self):
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        list_db = mock.Mock()
        list_db.get_all_cards.side_effect = RuntimeError("card list exploded")

        with mock.patch.object(reply_server, "db_manager", list_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as list_raised:
                reply_server.get_cards(current_user=current_user)

        self.assertEqual(500, list_raised.exception.status_code)
        self.assertEqual("card list exploded", list_raised.exception.detail)
        list_db.get_all_cards.assert_called_once_with(7, summary_only=True)

        detail_db = mock.Mock()
        detail_db.get_card_by_id.side_effect = RuntimeError("card detail exploded")

        with mock.patch.object(reply_server, "db_manager", detail_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as detail_raised:
                reply_server.get_card(404, current_user=current_user)

        self.assertEqual(500, detail_raised.exception.status_code)
        self.assertEqual("card detail exploded", detail_raised.exception.detail)
        detail_db.get_card_by_id.assert_called_once_with(404, 7)

    def test_card_create_and_update_surface_duplicate_conflicts_as_bad_request(self):
        fake_db = mock.Mock()
        fake_db.create_card.side_effect = ValueError("卡券名称已存在：Demo Card")
        fake_db.update_card.side_effect = ValueError("卡券已存在：Demo Card - 面额:10元")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as create_raised:
                reply_server.create_card(
                    {"name": "Demo Card", "type": "text", "enabled": True},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as update_raised:
                reply_server.update_card(
                    7,
                    {
                        "name": "Demo Card",
                        "type": "text",
                        "is_multi_spec": True,
                        "spec_name": "面额",
                        "spec_value": "10元",
                    },
                    current_user=current_user,
                )

        self.assertEqual(create_raised.exception.status_code, 400)
        self.assertEqual(create_raised.exception.detail, "卡券名称已存在：Demo Card")
        self.assertEqual(update_raised.exception.status_code, 400)
        self.assertEqual(update_raised.exception.detail, "卡券已存在：Demo Card - 面额:10元")
        fake_db.create_card.assert_called_once()
        fake_db.update_card.assert_called_once()

    def test_card_routes_reject_blank_name_after_trimming_as_bad_request(self):
        fake_db = mock.Mock()
        fake_db.create_card.side_effect = ValueError("卡券名称不能为空")
        fake_db.update_card.side_effect = ValueError("卡券名称不能为空")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as create_raised:
                reply_server.create_card(
                    {"name": "   ", "type": "text", "enabled": True},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as update_raised:
                reply_server.update_card(
                    7,
                    {"name": "   ", "type": "text"},
                    current_user=current_user,
                )

        self.assertEqual(create_raised.exception.status_code, 400)
        self.assertEqual(create_raised.exception.detail, "卡券名称不能为空")
        self.assertEqual(update_raised.exception.status_code, 400)
        self.assertEqual(update_raised.exception.detail, "卡券名称不能为空")
        fake_db.create_card.assert_called_once()
        fake_db.update_card.assert_called_once()

    def test_card_routes_reject_invalid_card_type_as_bad_request(self):
        fake_db = mock.Mock()
        fake_db.create_card.side_effect = ValueError("卡券类型无效")
        fake_db.update_card.side_effect = ValueError("卡券类型无效")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as create_raised:
                reply_server.create_card(
                    {"name": "Demo Card", "type": "bad-type", "enabled": True},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as update_raised:
                reply_server.update_card(
                    7,
                    {"name": "Demo Card", "type": "bad-type"},
                    current_user=current_user,
                )

        self.assertEqual(create_raised.exception.status_code, 400)
        self.assertEqual(create_raised.exception.detail, "卡券类型无效")
        self.assertEqual(update_raised.exception.status_code, 400)
        self.assertEqual(update_raised.exception.detail, "卡券类型无效")
        fake_db.create_card.assert_called_once()
        fake_db.update_card.assert_called_once()

    def test_update_card_with_image_cleans_up_saved_image_when_database_update_raises(self):
        fake_db = mock.Mock()
        fake_db.get_card_by_id.return_value = {"id": 7, "image_url": "/static/uploads/images/old-demo.png"}
        fake_db.update_card.side_effect = ValueError("卡券名称已存在：Demo Card")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        fake_image = SimpleNamespace(
            content_type="image/png",
            filename="demo.png",
            read=mock.AsyncMock(return_value=b"image-bytes"),
        )

        async def invoke():
            return await reply_server.update_card_with_image(
                7,
                image=fake_image,
                name="Demo Card",
                type="image",
                description="",
                delay_seconds=0,
                enabled=True,
                is_multi_spec=False,
                spec_name="",
                spec_value="",
                spec_name_2="",
                spec_value_2="",
                current_user=current_user,
            )

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server.image_manager, "save_image", return_value="/static/uploads/images/demo.png") as save_image, \
             mock.patch.object(reply_server.image_manager, "delete_image") as delete_image:
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(invoke())

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "卡券名称已存在：Demo Card")
        save_image.assert_called_once_with(b"image-bytes", "demo.png")
        delete_image.assert_called_once_with("/static/uploads/images/demo.png")
        fake_db.get_card_by_id.assert_called_once_with(7, 7)
        fake_db.update_card.assert_called_once_with(
            card_id=7,
            name="Demo Card",
            card_type="image",
            image_url="/static/uploads/images/demo.png",
            description="",
            enabled=True,
            delay_seconds=0,
            is_multi_spec=False,
            spec_name=None,
            spec_value=None,
            spec_name_2=None,
            spec_value_2=None,
            user_id=7,
        )

    def test_update_card_with_image_deletes_previous_image_after_successful_update(self):
        fake_db = mock.Mock()
        fake_db.get_card_by_id.return_value = {"id": 7, "image_url": "/static/uploads/images/old-demo.png"}
        fake_db.update_card.return_value = True
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        fake_image = SimpleNamespace(
            content_type="image/png",
            filename="demo-new.png",
            read=mock.AsyncMock(return_value=b"image-bytes-new"),
        )

        async def invoke():
            return await reply_server.update_card_with_image(
                7,
                image=fake_image,
                name="Demo Card",
                type="image",
                description="",
                delay_seconds=0,
                enabled=True,
                is_multi_spec=False,
                spec_name="",
                spec_value="",
                spec_name_2="",
                spec_value_2="",
                current_user=current_user,
            )

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server.image_manager, "save_image", return_value="/static/uploads/images/demo-new.png") as save_image, \
             mock.patch.object(reply_server.image_manager, "delete_image") as delete_image:
            result = asyncio.run(invoke())

        self.assertEqual(
            result,
            {"message": "卡券更新成功", "image_url": "/static/uploads/images/demo-new.png"},
        )
        fake_db.get_card_by_id.assert_called_once_with(7, 7)
        save_image.assert_called_once_with(b"image-bytes-new", "demo-new.png")
        delete_image.assert_called_once_with("/static/uploads/images/old-demo.png")
        fake_db.update_card.assert_called_once_with(
            card_id=7,
            name="Demo Card",
            card_type="image",
            image_url="/static/uploads/images/demo-new.png",
            description="",
            enabled=True,
            delay_seconds=0,
            is_multi_spec=False,
            spec_name=None,
            spec_value=None,
            spec_name_2=None,
            spec_value_2=None,
            user_id=7,
        )

    def test_delivery_rule_routes_use_module_bound_database_and_normalize_recent_logs(self):
        fake_db = mock.Mock()
        fake_db.get_all_delivery_rules.return_value = [
            {
                "id": 71,
                "keyword": "demo",
                "card_id": 9,
                "enabled": True,
            }
        ]
        fake_db.get_today_delivery_count.return_value = 6
        fake_db.get_recent_delivery_logs.return_value = [
            {
                "status": "success",
                "reason": "发货成功 [order_spec_mode=one_spec, rule_spec_mode=one_spec, item_config_mode=plain]",
                "order_id": "order-1",
                "account_id": " acc-demo-1 ",
            }
        ]
        fake_db.get_card_by_id.return_value = {"id": 9, "name": "Demo Card"}
        fake_db.create_delivery_rule.return_value = 71
        fake_db.get_delivery_rule_by_id.return_value = {
            "id": 71,
            "keyword": "demo",
            "card_id": 9,
            "enabled": True,
            "description": "demo rule",
            "delivery_times": 0,
        }
        fake_db.update_delivery_rule.return_value = True
        fake_db.delete_delivery_rule.return_value = True

        shadow_db = mock.Mock()
        shadow_db.get_all_delivery_rules.side_effect = AssertionError("发货规则列表路由不该绕开 reply_server.db_manager")
        shadow_db.get_today_delivery_count.side_effect = AssertionError("发货统计路由不该绕开 reply_server.db_manager")
        shadow_db.get_recent_delivery_logs.side_effect = AssertionError("最近发货日志路由不该绕开 reply_server.db_manager")
        shadow_db.get_card_by_id.side_effect = AssertionError("发货规则卡券校验不该绕开 reply_server.db_manager")
        shadow_db.create_delivery_rule.side_effect = AssertionError("发货规则创建路由不该绕开 reply_server.db_manager")
        shadow_db.get_delivery_rule_by_id.side_effect = AssertionError("发货规则详情路由不该绕开 reply_server.db_manager")
        shadow_db.update_delivery_rule.side_effect = AssertionError("发货规则更新路由不该绕开 reply_server.db_manager")
        shadow_db.delete_delivery_rule.side_effect = AssertionError("发货规则删除路由不该绕开 reply_server.db_manager")

        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ):
            list_result = reply_server.get_delivery_rules(current_user=current_user)
            stats_result = reply_server.get_delivery_stats(current_user=current_user)
            logs_result = reply_server.get_recent_delivery_logs(limit=20, current_user=current_user)
            create_result = reply_server.create_delivery_rule(
                {"keyword": "demo", "card_id": 9, "description": "demo rule"},
                current_user=current_user,
            )
            detail_result = reply_server.get_delivery_rule(71, current_user=current_user)
            update_result = reply_server.update_delivery_rule(
                71,
                {"keyword": "demo-updated", "card_id": 9, "enabled": False},
                current_user=current_user,
            )
            delete_result = reply_server.delete_delivery_rule(71, current_user=current_user)

        self.assertEqual(list_result, fake_db.get_all_delivery_rules.return_value)
        self.assertEqual(stats_result, {"today_delivery_count": 6})
        self.assertEqual(
            logs_result,
            {
                "logs": [
                    {
                        "status": "success",
                        "reason": "发货成功",
                        "order_id": "order-1",
                        "account_id": "acc-demo-1",
                        "order_spec_mode": "one_spec",
                        "rule_spec_mode": "one_spec",
                        "item_config_mode": "plain",
                    }
                ]
            },
        )
        self.assertEqual(create_result, {"id": 71, "message": "发货规则创建成功"})
        self.assertEqual(detail_result, fake_db.get_delivery_rule_by_id.return_value)
        self.assertEqual(update_result, {"message": "发货规则更新成功"})
        self.assertEqual(delete_result, {"message": "发货规则删除成功"})
        fake_db.get_all_delivery_rules.assert_called_once_with(7)
        fake_db.get_today_delivery_count.assert_called_once_with(7)
        fake_db.get_recent_delivery_logs.assert_called_once_with(user_id=7, limit=60)
        fake_db.get_card_by_id.assert_has_calls([mock.call(9, 7), mock.call(9, 7)])
        fake_db.create_delivery_rule.assert_called_once_with(
            keyword="demo",
            card_id=9,
            delivery_count=1,
            enabled=True,
            description="demo rule",
            user_id=7,
        )
        fake_db.get_delivery_rule_by_id.assert_called_once_with(71, 7)
        fake_db.update_delivery_rule.assert_called_once_with(
            rule_id=71,
            keyword="demo-updated",
            card_id=9,
            delivery_count=1,
            enabled=False,
            description=None,
            user_id=7,
        )
        fake_db.delete_delivery_rule.assert_called_once_with(71, 7)

    def test_delivery_rule_routes_re_raise_not_found_and_card_lookup_failures(self):
        fake_db = mock.Mock()
        fake_db.get_delivery_rule_by_id.return_value = None
        fake_db.get_card_by_id.return_value = {"id": 9, "name": "Demo Card"}
        fake_db.create_delivery_rule.side_effect = ValueError("卡券不存在或无权限访问: 9")
        fake_db.update_delivery_rule.side_effect = ValueError("卡券不存在或无权限访问: 9")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as detail_raised:
                reply_server.get_delivery_rule(404, current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as create_raised:
                reply_server.create_delivery_rule(
                    {"keyword": "demo", "card_id": 9},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as update_raised:
                reply_server.update_delivery_rule(
                    404,
                    {"keyword": "demo", "card_id": 9},
                    current_user=current_user,
                )

        self.assertEqual(detail_raised.exception.status_code, 404)
        self.assertEqual(detail_raised.exception.detail, "发货规则不存在")
        self.assertEqual(create_raised.exception.status_code, 404)
        self.assertEqual(create_raised.exception.detail, "卡券不存在")
        self.assertEqual(update_raised.exception.status_code, 404)
        self.assertEqual(update_raised.exception.detail, "卡券不存在")
        fake_db.get_delivery_rule_by_id.assert_called_once_with(404, 7)
        fake_db.get_card_by_id.assert_has_calls([mock.call(9, 7), mock.call(9, 7)])
        fake_db.create_delivery_rule.assert_called_once()
        fake_db.update_delivery_rule.assert_called_once()

    def test_delivery_rule_routes_reject_blank_keyword_after_trimming_as_bad_request(self):
        fake_db = mock.Mock()
        fake_db.get_card_by_id.return_value = {"id": 9, "name": "Demo Card"}
        fake_db.create_delivery_rule.side_effect = ValueError("发货规则关键词不能为空")
        fake_db.update_delivery_rule.side_effect = ValueError("发货规则关键词不能为空")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as create_raised:
                reply_server.create_delivery_rule(
                    {"keyword": "   ", "card_id": 9, "delivery_count": 1},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as update_raised:
                reply_server.update_delivery_rule(
                    71,
                    {"keyword": "   ", "card_id": 9, "delivery_count": 1},
                    current_user=current_user,
                )

        self.assertEqual(create_raised.exception.status_code, 400)
        self.assertEqual(create_raised.exception.detail, "发货规则关键词不能为空")
        self.assertEqual(update_raised.exception.status_code, 400)
        self.assertEqual(update_raised.exception.detail, "发货规则关键词不能为空")
        fake_db.get_card_by_id.assert_not_called()
        fake_db.create_delivery_rule.assert_not_called()
        fake_db.update_delivery_rule.assert_not_called()

    def test_delivery_rule_routes_reject_missing_or_invalid_card_id_as_bad_request(self):
        fake_db = mock.Mock()
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as create_missing_raised:
                reply_server.create_delivery_rule(
                    {"keyword": "demo", "card_id": ""},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as create_invalid_raised:
                reply_server.create_delivery_rule(
                    {"keyword": "demo", "card_id": "abc"},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as update_missing_raised:
                reply_server.update_delivery_rule(
                    71,
                    {"keyword": "demo", "card_id": ""},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as update_invalid_raised:
                reply_server.update_delivery_rule(
                    71,
                    {"keyword": "demo", "card_id": "abc"},
                    current_user=current_user,
                )

        self.assertEqual(create_missing_raised.exception.status_code, 400)
        self.assertEqual(create_missing_raised.exception.detail, "卡券ID不能为空")
        self.assertEqual(create_invalid_raised.exception.status_code, 400)
        self.assertEqual(create_invalid_raised.exception.detail, "卡券ID必须为整数")
        self.assertEqual(update_missing_raised.exception.status_code, 400)
        self.assertEqual(update_missing_raised.exception.detail, "卡券ID不能为空")
        self.assertEqual(update_invalid_raised.exception.status_code, 400)
        self.assertEqual(update_invalid_raised.exception.detail, "卡券ID必须为整数")
        fake_db.get_card_by_id.assert_not_called()
        fake_db.create_delivery_rule.assert_not_called()
        fake_db.update_delivery_rule.assert_not_called()

    def test_delivery_rule_routes_reject_non_positive_delivery_count_as_bad_request(self):
        fake_db = mock.Mock()
        fake_db.get_card_by_id.return_value = {"id": 9, "name": "Demo Card"}
        fake_db.create_delivery_rule.side_effect = ValueError("发货数量必须为大于等于 1 的整数")
        fake_db.update_delivery_rule.side_effect = ValueError("发货数量必须为大于等于 1 的整数")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as create_raised:
                reply_server.create_delivery_rule(
                    {"keyword": "demo", "card_id": 9, "delivery_count": 0},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as update_raised:
                reply_server.update_delivery_rule(
                    71,
                    {"keyword": "demo", "card_id": 9, "delivery_count": -1},
                    current_user=current_user,
                )

        self.assertEqual(create_raised.exception.status_code, 400)
        self.assertEqual(create_raised.exception.detail, "发货数量必须为大于等于 1 的整数")
        self.assertEqual(update_raised.exception.status_code, 400)
        self.assertEqual(update_raised.exception.detail, "发货数量必须为大于等于 1 的整数")
        fake_db.get_card_by_id.assert_has_calls([mock.call(9, 7), mock.call(9, 7)])
        fake_db.create_delivery_rule.assert_called_once()
        fake_db.update_delivery_rule.assert_called_once()

    def test_delivery_rule_routes_surface_database_read_failures_as_server_errors(self):
        fake_db = mock.Mock()
        fake_db.get_all_delivery_rules.side_effect = RuntimeError("delivery rules exploded")
        fake_db.get_today_delivery_count.side_effect = RuntimeError("delivery stats exploded")
        fake_db.get_recent_delivery_logs.side_effect = RuntimeError("delivery logs exploded")
        fake_db.get_delivery_rule_by_id.side_effect = RuntimeError("delivery detail exploded")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as list_raised:
                reply_server.get_delivery_rules(current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as stats_raised:
                reply_server.get_delivery_stats(current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as logs_raised:
                reply_server.get_recent_delivery_logs(limit=20, current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as detail_raised:
                reply_server.get_delivery_rule(71, current_user=current_user)

        self.assertEqual(list_raised.exception.status_code, 500)
        self.assertEqual(list_raised.exception.detail, "delivery rules exploded")
        self.assertEqual(stats_raised.exception.status_code, 500)
        self.assertEqual(stats_raised.exception.detail, "delivery stats exploded")
        self.assertEqual(logs_raised.exception.status_code, 500)
        self.assertEqual(logs_raised.exception.detail, "delivery logs exploded")
        self.assertEqual(detail_raised.exception.status_code, 500)
        self.assertEqual(detail_raised.exception.detail, "delivery detail exploded")
        fake_db.get_all_delivery_rules.assert_called_once_with(7)
        fake_db.get_today_delivery_count.assert_called_once_with(7)
        fake_db.get_recent_delivery_logs.assert_called_once_with(user_id=7, limit=60)
        fake_db.get_delivery_rule_by_id.assert_called_once_with(71, 7)

    def test_item_reply_routes_use_module_bound_database_and_surface_save_failures(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-demo-1"]
        fake_db.get_item_replays_by_account.return_value = [
            {
                "account_id": "acc-demo-1",
                "item_id": "item-1",
                "reply_content": "hello",
                "item_title": "Demo item",
                "item_detail": "",
                "updated_at": "2026-01-01 00:00:00",
            }
        ]
        fake_db.update_item_reply.return_value = False
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ):
            all_items_result = reply_server.get_all_items(current_user=current_user)
            account_items_result = reply_server.get_item_replays_by_account(
                "acc-demo-1",
                current_user=current_user,
            )
            get_item_reply_result = reply_server.get_item_reply(
                "acc-demo-1",
                "item-1",
                current_user=current_user,
            )

            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.update_item_reply(
                    "acc-demo-1",
                    "item-1",
                    {"reply_content": "  updated reply  "},
                    current_user=current_user,
                )

        self.assertEqual(all_items_result, {"items": fake_db.get_item_replays_by_account.return_value})
        self.assertEqual(account_items_result, {"items": fake_db.get_item_replays_by_account.return_value})
        self.assertEqual(
            get_item_reply_result,
            {
                **fake_db.get_item_replays_by_account.return_value[0],
                "account_id": "acc-demo-1",
            },
        )
        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "商品回复保存失败")
        fake_db.get_account_ids.assert_called_once_with(7)
        self.assertGreaterEqual(fake_db.get_item_replays_by_account.call_count, 3)
        fake_db.update_item_reply.assert_called_once_with(
            account_id="acc-demo-1",
            item_id="item-1",
            reply_content="updated reply",
        )

    def test_item_reply_all_accounts_route_sorts_rows_by_latest_update_globally(self):
        fake_db = mock.Mock()
        fake_db.get_all_cookies.return_value = {
            "acc-demo-1": "cookie-a",
            "acc-demo-2": "cookie-b",
        }
        fake_db.get_item_replays_by_account.side_effect = [
            [
                {
                    "account_id": "acc-demo-1",
                    "item_id": "item-old",
                    "reply_content": "old",
                    "item_title": "Old item",
                    "item_detail": "",
                    "updated_at": "2026-01-01 00:00:00",
                }
            ],
            [
                {
                    "account_id": "acc-demo-2",
                    "item_id": "item-new",
                    "reply_content": "new",
                    "item_title": "New item",
                    "item_detail": "",
                    "updated_at": "2026-01-02 00:00:00",
                }
            ],
        ]
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            result = reply_server.get_all_items(current_user=current_user)

        self.assertEqual(
            [item["item_id"] for item in result["items"]],
            ["item-new", "item-old"],
            "商品回复全量列表不该按账号分组糊出来，得按最新更新时间全局排序",
        )
        fake_db.get_account_ids.assert_called_once_with(7)
        fake_db.get_item_replays_by_account.assert_has_calls(
            [mock.call("acc-demo-1"), mock.call("acc-demo-2")]
        )

    def test_items_route_uses_module_bound_database_for_current_user_scope(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-demo-1", "acc-demo-2"]
        fake_db.get_items_by_account.side_effect = [
            [{"item_id": "item-1", "item_title": "Demo item 1"}],
            [{"item_id": "item-2", "item_title": "Demo item 2", "account_id": "acc-demo-2"}],
        ]
        shadow_db = mock.Mock()
        shadow_db.get_account_ids.side_effect = AssertionError("商品列表不该绕开 reply_server.db_manager")
        shadow_db.get_items_by_account.side_effect = AssertionError("商品列表不该绕开 reply_server.db_manager")
        db_manager_module = sys.modules["db_manager"]
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        items_endpoint = next(
            route.endpoint
            for route in reply_server.app.routes
            if getattr(route, "path", None) == "/items" and "GET" in getattr(route, "methods", set())
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(db_manager_module, "db_manager", shadow_db):
            result = items_endpoint(current_user=current_user)

        self.assertEqual(
            result,
            {
                "items": [
                    {"item_id": "item-1", "item_title": "Demo item 1", "account_id": "acc-demo-1"},
                    {"item_id": "item-2", "item_title": "Demo item 2", "account_id": "acc-demo-2"},
                ]
            },
        )
        fake_db.get_account_ids.assert_called_once_with(7)
        fake_db.get_items_by_account.assert_has_calls([mock.call("acc-demo-1"), mock.call("acc-demo-2")])

    def test_items_all_accounts_route_sorts_rows_by_latest_update_globally(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-demo-1", "acc-demo-2"]
        fake_db.get_items_by_account.side_effect = [
            [
                {
                    "account_id": "acc-demo-1",
                    "item_id": "item-old",
                    "item_title": "Old item",
                    "item_detail": "",
                    "updated_at": "2026-01-01 00:00:00",
                }
            ],
            [
                {
                    "account_id": "acc-demo-2",
                    "item_id": "item-new",
                    "item_title": "New item",
                    "item_detail": "",
                    "updated_at": "2026-01-02 00:00:00",
                }
            ],
        ]
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        items_endpoint = next(
            route.endpoint
            for route in reply_server.app.routes
            if getattr(route, "path", None) == "/items" and "GET" in getattr(route, "methods", set())
        )

        with mock.patch.object(reply_server, "db_manager", fake_db):
            result = items_endpoint(current_user=current_user)

        self.assertEqual(
            [item["item_id"] for item in result["items"]],
            ["item-new", "item-old"],
            "商品全量列表不该按账号分组硬拼，得按最新更新时间全局排序",
        )
        fake_db.get_account_ids.assert_called_once_with(7)
        fake_db.get_items_by_account.assert_has_calls([mock.call("acc-demo-1"), mock.call("acc-demo-2")])

    def test_items_count_route_uses_module_bound_database_for_current_user_scope(self):
        fake_db = mock.Mock()
        fake_db.get_account_ids.return_value = ["acc-demo-1", "acc-demo-2"]
        fake_db.count_items_by_account.side_effect = [2, 5]
        shadow_db = mock.Mock()
        shadow_db.get_account_ids.side_effect = AssertionError("商品数量路由不该绕开 reply_server.db_manager")
        shadow_db.count_items_by_account.side_effect = AssertionError("商品数量路由不该绕开 reply_server.db_manager")
        db_manager_module = sys.modules["db_manager"]
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        count_endpoint = next(
            route.endpoint
            for route in reply_server.app.routes
            if getattr(route, "path", None) == "/items/count" and "GET" in getattr(route, "methods", set())
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(db_manager_module, "db_manager", shadow_db):
            result = count_endpoint(current_user=current_user)

        self.assertEqual(result, {"count": 7})
        fake_db.get_account_ids.assert_called_once_with(7)
        fake_db.count_items_by_account.assert_has_calls([mock.call("acc-demo-1"), mock.call("acc-demo-2")])

    def test_item_routes_stop_masking_database_failures_as_empty_results_or_not_found(self):
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        items_endpoint = next(
            route.endpoint
            for route in reply_server.app.routes
            if getattr(route, "path", None) == "/items" and "GET" in getattr(route, "methods", set())
        )

        list_db = mock.Mock()
        list_db.get_all_cookies.return_value = {"acc-demo-1": "cookie-a"}
        list_db.get_items_by_account.side_effect = RuntimeError("item list exploded")
        with mock.patch.object(reply_server, "db_manager", list_db):
            with self.assertRaises(reply_server.HTTPException) as all_items_raised:
                items_endpoint(current_user=current_user)

        self.assertEqual(all_items_raised.exception.status_code, 500)
        self.assertEqual(all_items_raised.exception.detail, "获取商品信息失败: item list exploded")
        list_db.get_all_cookies.assert_called_once_with(7)
        list_db.get_items_by_account.assert_called_once_with("acc-demo-1")

        scoped_list_db = mock.Mock()
        scoped_list_db.get_items_by_account.side_effect = RuntimeError("scoped item list exploded")
        with mock.patch.object(reply_server, "db_manager", scoped_list_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ):
            with self.assertRaises(reply_server.HTTPException) as scoped_items_raised:
                reply_server.get_items_by_account("acc-demo-1", current_user=current_user)

        self.assertEqual(scoped_items_raised.exception.status_code, 500)
        self.assertEqual(scoped_items_raised.exception.detail, "获取商品信息失败: scoped item list exploded")
        scoped_list_db.get_items_by_account.assert_called_once_with("acc-demo-1")

        detail_db = mock.Mock()
        detail_db.get_item_info.side_effect = RuntimeError("item detail exploded")
        with mock.patch.object(reply_server, "db_manager", detail_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ):
            with self.assertRaises(reply_server.HTTPException) as detail_raised:
                reply_server.get_item_detail("acc-demo-1", "item-1", current_user=current_user)

        self.assertEqual(detail_raised.exception.status_code, 500)
        self.assertEqual(detail_raised.exception.detail, "获取商品详情失败: item detail exploded")
        detail_db.get_item_info.assert_called_once_with("acc-demo-1", "item-1")

    def test_item_mutation_routes_stop_masking_database_failures_as_not_found_or_partial_success(self):
        fake_db = mock.Mock()
        fake_db.update_item_detail.side_effect = RuntimeError("item detail update exploded")
        fake_db.delete_item_info.side_effect = RuntimeError("item delete exploded")
        fake_db.batch_delete_item_info.side_effect = RuntimeError("item batch delete exploded")
        fake_db.update_item_multi_spec_status.side_effect = RuntimeError("item multi spec exploded")
        fake_db.update_item_multi_quantity_delivery_status.side_effect = RuntimeError("item multi quantity exploded")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        def assert_http_exception(call, expected_status, expected_detail):
            with self.assertRaises(reply_server.HTTPException) as raised:
                call()
            self.assertEqual(expected_status, raised.exception.status_code)
            self.assertEqual(expected_detail, raised.exception.detail)

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ):
            assert_http_exception(
                lambda: reply_server.update_item_detail(
                    "acc-demo-1",
                    "item-1",
                    reply_server.ItemDetailUpdate(item_detail='{"demo": true}'),
                    current_user=current_user,
                ),
                500,
                "更新商品详情失败: item detail update exploded",
            )
            assert_http_exception(
                lambda: reply_server.delete_item_info(
                    "acc-demo-1",
                    "item-1",
                    current_user=current_user,
                ),
                500,
                "服务器错误: item delete exploded",
            )
            assert_http_exception(
                lambda: reply_server.batch_delete_items(
                    reply_server.ItemBatchDeleteRequest(
                        items=[reply_server.ItemBatchDeleteItem(account_id="acc-demo-1", item_id="item-1")]
                    ),
                    current_user=current_user,
                ),
                500,
                "服务器错误: item batch delete exploded",
            )
            assert_http_exception(
                lambda: reply_server.update_item_multi_spec(
                    "acc-demo-1",
                    "item-1",
                    {"is_multi_spec": True},
                    current_user=current_user,
                ),
                500,
                "item multi spec exploded",
            )
            assert_http_exception(
                lambda: reply_server.update_item_multi_quantity_delivery(
                    "acc-demo-1",
                    "item-1",
                    {"multi_quantity_delivery": True},
                    current_user=current_user,
                ),
                500,
                "item multi quantity exploded",
            )

    def test_item_detail_update_route_preserves_not_found_http_exception(self):
        fake_db = mock.Mock()
        fake_db.update_item_detail.return_value = False
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.update_item_detail(
                    "acc-demo-1",
                    "item-404",
                    reply_server.ItemDetailUpdate(item_detail='{"demo": true}'),
                    current_user=current_user,
                )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, "商品不存在")
        fake_db.update_item_detail.assert_called_once_with("acc-demo-1", "item-404", '{"demo": true}')

    def test_item_sync_routes_stop_masking_runtime_failures_as_business_failures(self):
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        async def invoke_get_all_items():
            return await reply_server.get_all_items_from_account(
                {"account_id": "acc-demo-1"},
                current_user=current_user,
            )

        async def invoke_get_items_by_page():
            return await reply_server.get_items_by_page(
                {"account_id": "acc-demo-1", "page_number": 1, "page_size": 20},
                current_user=current_user,
            )

        async def invoke_polish_items():
            return await reply_server.polish_account_items(
                "acc-demo-1",
                current_user=current_user,
            )

        scenarios = [
            (
                "all-items",
                invoke_get_all_items,
                SimpleNamespace(
                    get_all_items=mock.AsyncMock(side_effect=RuntimeError("sync all items exploded")),
                    close_session=mock.AsyncMock(),
                ),
                "获取商品信息失败: sync all items exploded",
            ),
            (
                "page-items",
                invoke_get_items_by_page,
                SimpleNamespace(
                    get_item_list_info=mock.AsyncMock(side_effect=RuntimeError("sync page items exploded")),
                    close_session=mock.AsyncMock(),
                ),
                "获取商品信息失败: sync page items exploded",
            ),
            (
                "polish-items",
                invoke_polish_items,
                SimpleNamespace(
                    polish_all_items=mock.AsyncMock(side_effect=RuntimeError("polish items exploded")),
                    close_session=mock.AsyncMock(),
                ),
                "擦亮商品失败: polish items exploded",
            ),
        ]

        for scenario_name, invoke, fake_live, expected_detail in scenarios:
            with self.subTest(scenario=scenario_name):
                fake_db = mock.Mock()
                fake_db.get_cookie_by_id.return_value = {"cookies_str": "cookie-demo-1"}

                with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
                    reply_server,
                    "_ensure_account_access",
                    return_value="acc-demo-1",
                ), mock.patch(
                    "XianyuAutoAsync.XianyuLive",
                    return_value=fake_live,
                ):
                    with self.assertRaises(reply_server.HTTPException) as raised:
                        asyncio.run(invoke())

                self.assertEqual(raised.exception.status_code, 500)
                self.assertEqual(raised.exception.detail, expected_detail)
                fake_db.get_cookie_by_id.assert_called_once_with("acc-demo-1")
                fake_live.close_session.assert_awaited_once_with()

    def test_item_sync_routes_do_not_override_success_when_close_session_fails(self):
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        async def invoke_get_all_items():
            return await reply_server.get_all_items_from_account(
                {"account_id": "acc-demo-1"},
                current_user=current_user,
            )

        async def invoke_get_items_by_page():
            return await reply_server.get_items_by_page(
                {"account_id": "acc-demo-1", "page_number": 1, "page_size": 20},
                current_user=current_user,
            )

        async def invoke_polish_items():
            return await reply_server.polish_account_items(
                "acc-demo-1",
                current_user=current_user,
            )

        scenarios = [
            (
                "all-items",
                invoke_get_all_items,
                SimpleNamespace(
                    get_all_items=mock.AsyncMock(
                        return_value={"total_count": 3, "total_pages": 2}
                    ),
                    close_session=mock.AsyncMock(side_effect=RuntimeError("close all items exploded")),
                ),
                {
                    "success": True,
                    "message": "成功同步 3 个商品（共2页），最新商品详情已更新",
                    "total_count": 3,
                    "total_pages": 2,
                },
            ),
            (
                "page-items",
                invoke_get_items_by_page,
                SimpleNamespace(
                    get_item_list_info=mock.AsyncMock(return_value={"current_count": 5}),
                    close_session=mock.AsyncMock(side_effect=RuntimeError("close page items exploded")),
                ),
                {
                    "success": True,
                    "message": "成功同步第1页 5 个商品，最新商品详情已更新",
                    "page_number": 1,
                    "page_size": 20,
                    "current_count": 5,
                },
            ),
            (
                "polish-items",
                invoke_polish_items,
                SimpleNamespace(
                    polish_all_items=mock.AsyncMock(return_value={"success": True, "count": 9}),
                    close_session=mock.AsyncMock(side_effect=RuntimeError("close polish exploded")),
                ),
                {"success": True, "count": 9},
            ),
        ]

        for scenario_name, invoke, fake_live, expected_result in scenarios:
            with self.subTest(scenario=scenario_name):
                fake_db = mock.Mock()
                fake_db.get_cookie_by_id.return_value = {"cookies_str": "cookie-demo-1"}

                with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
                    reply_server,
                    "_ensure_account_access",
                    return_value="acc-demo-1",
                ), mock.patch(
                    "XianyuAutoAsync.XianyuLive",
                    return_value=fake_live,
                ):
                    result = asyncio.run(invoke())

                self.assertEqual(result, expected_result)
                fake_db.get_cookie_by_id.assert_called_once_with("acc-demo-1")
                fake_live.close_session.assert_awaited_once_with()

    def test_item_multi_spec_routes_preserve_not_found_http_exception(self):
        fake_db = mock.Mock()
        fake_db.update_item_multi_spec_status.return_value = False
        fake_db.update_item_multi_quantity_delivery_status.return_value = False
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ):
            with self.assertRaises(reply_server.HTTPException) as multi_spec_raised:
                reply_server.update_item_multi_spec(
                    "acc-demo-1",
                    "item-404",
                    {"is_multi_spec": True},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as multi_delivery_raised:
                reply_server.update_item_multi_quantity_delivery(
                    "acc-demo-1",
                    "item-404",
                    {"multi_quantity_delivery": True},
                    current_user=current_user,
                )

        self.assertEqual(multi_spec_raised.exception.status_code, 404)
        self.assertEqual(multi_spec_raised.exception.detail, "商品不存在")
        self.assertEqual(multi_delivery_raised.exception.status_code, 404)
        self.assertEqual(multi_delivery_raised.exception.detail, "商品不存在")
        fake_db.update_item_multi_spec_status.assert_called_once_with("acc-demo-1", "item-404", True)
        fake_db.update_item_multi_quantity_delivery_status.assert_called_once_with("acc-demo-1", "item-404", True)

    def test_batch_delete_item_reply_rejects_empty_requests_before_db_call(self):
        fake_db = mock.Mock()
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(
                    reply_server.batch_delete_item_reply(
                        reply_server.ItemReplyBatchDeleteRequest(items=[]),
                        current_user=current_user,
                    )
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "删除列表不能为空")
        fake_db.batch_delete_item_replies.assert_not_called()

    def test_batch_delete_items_rejects_empty_requests_before_db_call(self):
        fake_db = mock.Mock()
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.batch_delete_items(
                    reply_server.ItemBatchDeleteRequest(items=[]),
                    current_user=current_user,
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "删除列表不能为空")
        fake_db.batch_delete_item_info.assert_not_called()


class ReplyServerScheduledTaskSecurityRuntimeTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_create_scheduled_task_rejects_account_outside_current_user_scope(self):
        fake_db = mock.Mock()
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            side_effect=reply_server.HTTPException(status_code=403, detail="无权限操作该账号"),
        ), mock.patch.object(reply_server.logger, "warning"):
            result = asyncio.run(
                reply_server.create_scheduled_task(
                    {
                        "account_id": "acc-other-user-1",
                        "run_hour": 8,
                        "random_delay_max": 10,
                        "enabled": True,
                    },
                    current_user=current_user,
                )
            )

        self.assertEqual({"success": False, "message": "无权限操作该账号"}, result)
        fake_db.get_scheduled_task_by_account.assert_not_called()
        fake_db.create_scheduled_task.assert_not_called()

    def test_scheduled_task_mutations_accept_string_user_id_snapshot(self):
        task_record = {
            "id": 61,
            "user_id": 7,
            "enabled": True,
            "task_type": "item_polish",
            "delay_minutes": 8,
            "random_delay_max": 10,
        }
        updated_task_record = {
            **task_record,
            "enabled": False,
        }
        fake_db = mock.Mock()
        fake_db.get_scheduled_task.side_effect = [
            task_record,
            updated_task_record,
            task_record,
            task_record,
            updated_task_record,
        ]
        fake_db.update_scheduled_task.return_value = True
        fake_db.delete_scheduled_task.return_value = True
        current_user = {"user_id": "7", "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            update_result = asyncio.run(
                reply_server.update_scheduled_task(
                    61,
                    {"enabled": False},
                    current_user=current_user,
                )
            )
            delete_result = asyncio.run(
                reply_server.delete_scheduled_task(
                    61,
                    current_user=current_user,
                )
            )
            toggle_result = asyncio.run(
                reply_server.toggle_scheduled_task(
                    61,
                    current_user=current_user,
                )
            )

        self.assertTrue(update_result["success"])
        self.assertEqual("定时任务更新成功", update_result["message"])
        self.assertTrue(delete_result["success"])
        self.assertEqual("定时任务已删除", delete_result["message"])
        self.assertTrue(toggle_result["success"])
        self.assertEqual("定时任务已禁用", toggle_result["message"])
        fake_db.update_scheduled_task.assert_has_calls(
            [
                mock.call(61, enabled=0),
                mock.call(61, enabled=0),
            ]
        )
        fake_db.delete_scheduled_task.assert_called_once_with(61)

    def test_scheduled_task_checker_skips_cross_user_account_binding(self):
        fake_db = mock.Mock()
        fake_db.get_due_tasks.return_value = [
            {
                "id": 51,
                "name": "每日擦亮-acc-other-user-1",
                "account_id": "acc-other-user-1",
                "user_id": 7,
                "task_type": "item_polish",
                "delay_minutes": 8,
                "random_delay_max": 10,
            }
        ]
        fake_db.get_cookie_details.return_value = {
            "account_id": "acc-other-user-1",
            "user_id": 999,
            "cookies_str": "cookie-cross-user",
            "value": "cookie-cross-user",
        }
        fake_db.calculate_next_daily_run.return_value = "2026-05-27 08:00:00"

        async def _stop_after_first_loop(_seconds):
            raise RuntimeError("stop-loop")

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server.asyncio,
            "sleep",
            new=mock.AsyncMock(side_effect=_stop_after_first_loop),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-loop"):
                asyncio.run(reply_server.scheduled_task_checker())

        fake_db.get_due_tasks.assert_called_once_with()
        fake_db.get_cookie_details.assert_called_once_with("acc-other-user-1")
        fake_db.calculate_next_daily_run.assert_called_once_with(8, 10, include_today=False)
        fake_db.update_task_run_result.assert_called_once_with(
            51,
            {"success": False, "message": "账号不存在或不属于任务创建者"},
            "2026-05-27 08:00:00",
        )

    def test_scheduled_task_checker_records_failed_run_and_closes_session_when_polish_raises(self):
        fake_db = mock.Mock()
        fake_db.get_due_tasks.return_value = [
            {
                "id": 52,
                "name": "每日擦亮-acc-demo-1",
                "account_id": "acc-demo-1",
                "user_id": 7,
                "task_type": "item_polish",
                "delay_minutes": 8,
                "random_delay_max": 10,
            }
        ]
        fake_db.get_cookie_details.return_value = {
            "account_id": "acc-demo-1",
            "user_id": 7,
            "cookies_str": "cookie-demo-1",
        }
        fake_db.calculate_next_daily_run.return_value = "2026-05-27 08:00:00"
        fake_live = SimpleNamespace(
            polish_all_items=mock.AsyncMock(side_effect=RuntimeError("polish exploded")),
            close_session=mock.AsyncMock(),
        )

        async def _stop_after_first_loop(_seconds):
            raise RuntimeError("stop-loop")

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            sys.modules["XianyuAutoAsync"],
            "XianyuLive",
            return_value=fake_live,
        ) as xianyu_live_cls, mock.patch.object(
            reply_server.asyncio,
            "sleep",
            new=mock.AsyncMock(side_effect=_stop_after_first_loop),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-loop"):
                asyncio.run(reply_server.scheduled_task_checker())

        fake_db.get_due_tasks.assert_called_once_with()
        fake_db.get_cookie_details.assert_called_once_with("acc-demo-1")
        xianyu_live_cls.assert_called_once_with(
            "cookie-demo-1",
            account_id="acc-demo-1",
            register_instance=False,
        )
        fake_live.polish_all_items.assert_awaited_once_with()
        fake_live.close_session.assert_awaited_once_with()
        fake_db.calculate_next_daily_run.assert_called_once_with(8, 10, include_today=False)
        fake_db.update_task_run_result.assert_called_once_with(
            52,
            {"success": False, "message": "执行异常: polish exploded"},
            "2026-05-27 08:00:00",
        )

    def test_scheduled_task_checker_keeps_success_result_when_close_session_fails(self):
        fake_db = mock.Mock()
        fake_db.get_due_tasks.return_value = [
            {
                "id": 54,
                "name": "每日擦亮-acc-demo-3",
                "account_id": "acc-demo-3",
                "user_id": 7,
                "task_type": "item_polish",
                "delay_minutes": 8,
                "random_delay_max": 10,
            }
        ]
        fake_db.get_cookie_details.return_value = {
            "account_id": "acc-demo-3",
            "user_id": 7,
            "cookies_str": "cookie-demo-3",
        }
        fake_db.calculate_next_daily_run.return_value = "2026-05-27 08:00:00"
        fake_live = SimpleNamespace(
            polish_all_items=mock.AsyncMock(return_value={"success": True, "count": 6}),
            close_session=mock.AsyncMock(side_effect=RuntimeError("close scheduled polish exploded")),
        )

        async def _stop_after_first_loop(_seconds):
            raise RuntimeError("stop-loop")

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            sys.modules["XianyuAutoAsync"],
            "XianyuLive",
            return_value=fake_live,
        ) as xianyu_live_cls, mock.patch.object(
            reply_server.asyncio,
            "sleep",
            new=mock.AsyncMock(side_effect=_stop_after_first_loop),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-loop"):
                asyncio.run(reply_server.scheduled_task_checker())

        fake_db.get_due_tasks.assert_called_once_with()
        fake_db.get_cookie_details.assert_called_once_with("acc-demo-3")
        xianyu_live_cls.assert_called_once_with(
            "cookie-demo-3",
            account_id="acc-demo-3",
            register_instance=False,
        )
        fake_live.polish_all_items.assert_awaited_once_with()
        fake_live.close_session.assert_awaited_once_with()
        fake_db.calculate_next_daily_run.assert_called_once_with(8, 10, include_today=False)
        fake_db.update_task_run_result.assert_called_once_with(
            54,
            {"success": True, "count": 6},
            "2026-05-27 08:00:00",
        )

    def test_scheduled_task_checker_accepts_string_user_id_match_for_owned_account(self):
        fake_db = mock.Mock()
        fake_db.get_due_tasks.return_value = [
            {
                "id": 53,
                "name": "每日擦亮-acc-demo-2",
                "account_id": "acc-demo-2",
                "user_id": "7",
                "task_type": "item_polish",
                "delay_minutes": 8,
                "random_delay_max": 10,
            }
        ]
        fake_db.get_cookie_details.return_value = {
            "account_id": "acc-demo-2",
            "user_id": 7,
            "cookies_str": "cookie-demo-2",
        }
        fake_db.calculate_next_daily_run.return_value = "2026-05-27 08:00:00"
        fake_live = SimpleNamespace(
            polish_all_items=mock.AsyncMock(return_value={"success": True, "message": "ok"}),
            close_session=mock.AsyncMock(),
        )

        async def _stop_after_first_loop(_seconds):
            raise RuntimeError("stop-loop")

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            sys.modules["XianyuAutoAsync"],
            "XianyuLive",
            return_value=fake_live,
        ) as xianyu_live_cls, mock.patch.object(
            reply_server.asyncio,
            "sleep",
            new=mock.AsyncMock(side_effect=_stop_after_first_loop),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-loop"):
                asyncio.run(reply_server.scheduled_task_checker())

        fake_db.get_due_tasks.assert_called_once_with()
        fake_db.get_cookie_details.assert_called_once_with("acc-demo-2")
        xianyu_live_cls.assert_called_once_with(
            "cookie-demo-2",
            account_id="acc-demo-2",
            register_instance=False,
        )
        fake_live.polish_all_items.assert_awaited_once_with()
        fake_live.close_session.assert_awaited_once_with()
        fake_db.update_task_run_result.assert_called_once_with(
            53,
            {"success": True, "message": "ok"},
            "2026-05-27 08:00:00",
        )

class ReplyServerKeywordImportExportValidationTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_export_keywords_re_raises_account_access_http_exception(self):
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        fake_db = mock.Mock()
        fake_db.get_keywords_with_type.side_effect = AssertionError(
            "access check should fail before keyword export queries the database"
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            side_effect=reply_server.HTTPException(status_code=403, detail="无权限访问该账号"),
        ):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.export_keywords("acc-foreign-1", current_user=current_user)

        self.assertEqual(403, raised.exception.status_code)
        self.assertEqual("无权限访问该账号", raised.exception.detail)
        fake_db.get_keywords_with_type.assert_not_called()

    def test_update_keywords_with_item_id_returns_bad_request_for_image_keyword_conflict(self):
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        fake_db = mock.Mock()
        fake_db.save_text_keywords_only.side_effect = ValueError(
            "关键词 '图片问候' 与已有图片关键词冲突: item_id=<general>"
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ), mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.update_keywords_with_item_id(
                    "acc-demo-1",
                    reply_server.KeywordWithItemIdIn(
                        keywords=[{"keyword": "图片问候", "reply": "您好", "item_id": ""}]
                    ),
                    current_user=current_user,
                )

        self.assertEqual(400, raised.exception.status_code)
        self.assertIn('关键词 "图片问候"', raised.exception.detail)
        self.assertIn("图片关键词", raised.exception.detail)
        fake_db.save_text_keywords_only.assert_called_once_with(
            "acc-demo-1",
            [("图片问候", "您好", None)],
        )

    def test_import_keywords_preserves_http_validation_status_for_missing_columns(self):
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        fake_db = mock.Mock()
        upload_file = SimpleNamespace(
            filename="keywords.xlsx",
            read=mock.AsyncMock(return_value=b"fake-excel-content"),
        )
        missing_column_df = reply_server.pd.DataFrame(
            [{"关键词": "你好", "关键词内容": "您好"}]
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ), mock.patch.object(reply_server.pd, "read_excel", return_value=missing_column_df):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(
                    reply_server.import_keywords(
                        "acc-demo-1",
                        file=upload_file,
                        current_user=current_user,
                    )
                )

        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("Excel文件缺少必要的列", raised.exception.detail)
        fake_db.get_keywords_with_type.assert_not_called()
        fake_db.save_text_keywords_only.assert_not_called()

    def test_import_keywords_accepts_uppercase_excel_extensions(self):
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        fake_db = mock.Mock()
        fake_db.get_keywords_with_type.return_value = []
        fake_db.save_text_keywords_only.return_value = True
        upload_file = SimpleNamespace(
            filename="KEYWORDS.XLSX",
            read=mock.AsyncMock(return_value=b"fake-excel-content"),
        )
        imported_df = reply_server.pd.DataFrame(
            [{"关键词": "你好", "商品ID": "", "关键词内容": "您好"}]
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ), mock.patch.object(reply_server.pd, "read_excel", return_value=imported_df):
            result = asyncio.run(
                reply_server.import_keywords(
                    "acc-demo-1",
                    file=upload_file,
                    current_user=current_user,
                )
            )

        self.assertEqual(
            {
                "msg": "导入成功",
                "total": 1,
                "added": 1,
                "updated": 0,
            },
            result,
        )
        fake_db.get_keywords_with_type.assert_called_once_with("acc-demo-1")
        fake_db.save_text_keywords_only.assert_called_once_with(
            "acc-demo-1",
            [("你好", "您好", None)],
        )

    def test_import_keywords_rejects_duplicate_excel_rows_before_database_write(self):
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}
        fake_db = mock.Mock()
        fake_db.get_keywords_with_type.return_value = []
        upload_file = SimpleNamespace(
            filename="keywords.xlsx",
            read=mock.AsyncMock(return_value=b"fake-excel-content"),
        )
        duplicate_rows_df = reply_server.pd.DataFrame(
            [
                {"关键词": "你好", "商品ID": "", "关键词内容": "您好"},
                {"关键词": "你好", "商品ID": "", "关键词内容": "在吗"},
            ]
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ), mock.patch.object(reply_server.pd, "read_excel", return_value=duplicate_rows_df):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(
                    reply_server.import_keywords(
                        "acc-demo-1",
                        file=upload_file,
                        current_user=current_user,
                    )
                )

        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("Excel文件中存在重复关键词", raised.exception.detail)
        self.assertIn("第2行与第3行", raised.exception.detail)
        fake_db.save_text_keywords_only.assert_not_called()


class ReplyServerUserManagementSessionRevocationRuntimeTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_user_management_routes_use_module_bound_database_for_list_and_mutations(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}
        fake_db = mock.Mock()
        fake_db.get_all_users.return_value = [
            {"id": 7, "username": "demo-user", "password_hash": "hash-demo", "is_admin": False},
            {"id": 8, "username": "staff-user", "password_hash": "hash-staff", "is_admin": False},
        ]
        fake_db.get_account_ids.side_effect = [
            ["acc-demo-1"],
            ["acc-staff-1", "acc-staff-2"],
        ]
        fake_db.get_all_cards.side_effect = [
            [{"id": 101, "name": "Demo Card"}],
            [],
        ]
        fake_db.get_user_by_id.side_effect = [
            {"id": 7, "username": "demo-user"},
            {"id": 8, "username": "staff-user", "is_admin": False},
        ]
        fake_db.delete_user_and_data.return_value = True
        fake_db.update_user_admin_status.return_value = True

        shadow_db = mock.Mock()
        for method_name in (
            "get_all_users",
            "get_account_ids",
            "get_all_cards",
            "get_user_by_id",
            "delete_user_and_data",
            "update_user_admin_status",
        ):
            getattr(shadow_db, method_name).side_effect = AssertionError(
                "用户管理路由不该绕开 reply_server.db_manager"
            )

        db_manager_module = sys.modules["db_manager"]

        with mock.patch.dict(
            reply_server.SESSION_TOKENS,
            {
                "delete-target-session": {
                    "user_id": 7,
                    "username": "demo-user",
                    "is_admin": False,
                    "timestamp": 1.0,
                },
                "promote-target-session": {
                    "user_id": 8,
                    "username": "staff-user",
                    "is_admin": False,
                    "timestamp": 2.0,
                },
                "other-session": {
                    "user_id": 9,
                    "username": "other-user",
                    "is_admin": False,
                    "timestamp": 3.0,
                },
            },
            clear=True,
        ), mock.patch.object(
            reply_server,
            "db_manager",
            fake_db,
        ), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ), mock.patch.object(reply_server, "log_with_user"):
            list_result = reply_server.get_all_users(admin_user=admin_user)
            delete_result = reply_server.delete_user(7, admin_user=admin_user)
            update_result = reply_server.update_user_admin_status(8, True, admin_user=admin_user)
            self.assertNotIn("delete-target-session", reply_server.SESSION_TOKENS)
            self.assertTrue(reply_server.SESSION_TOKENS["promote-target-session"]["is_admin"])
            self.assertFalse(reply_server.SESSION_TOKENS["other-session"]["is_admin"])

        self.assertEqual(
            list_result,
            {
                "users": [
                    {
                        "id": 7,
                        "username": "demo-user",
                        "is_admin": False,
                        "cookie_count": 1,
                        "card_count": 1,
                    },
                    {
                        "id": 8,
                        "username": "staff-user",
                        "is_admin": False,
                        "cookie_count": 2,
                        "card_count": 0,
                    },
                ]
            },
        )
        self.assertEqual(
            delete_result,
            {"message": "用户 demo-user 删除成功", "revoked_sessions": 1},
        )
        self.assertEqual(
            update_result,
            {
                "success": True,
                "message": "用户 staff-user 已设置为管理员",
                "user_id": 8,
                "is_admin": True,
                "updated_sessions": 1,
            },
        )
        fake_db.get_all_users.assert_called_once_with()
        fake_db.get_account_ids.assert_has_calls([mock.call(7), mock.call(8)])
        fake_db.get_all_cards.assert_has_calls([mock.call(7, summary_only=True), mock.call(8, summary_only=True)])
        fake_db.get_user_by_id.assert_has_calls([mock.call(7), mock.call(8)])
        fake_db.delete_user_and_data.assert_called_once_with(7)
        fake_db.update_user_admin_status.assert_called_once_with(8, True)

    def test_user_management_self_protection_accepts_string_admin_user_id_snapshot(self):
        admin_user = {"user_id": "7", "username": "admin", "is_admin": True}
        fake_db = mock.Mock()

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as delete_raised:
                reply_server.delete_user(7, admin_user=admin_user)

            with self.assertRaises(reply_server.HTTPException) as update_raised:
                reply_server.update_user_admin_status(7, False, admin_user=admin_user)

        self.assertEqual(400, delete_raised.exception.status_code)
        self.assertEqual("不能删除管理员自己", delete_raised.exception.detail)
        self.assertEqual(400, update_raised.exception.status_code)
        self.assertEqual("不能修改自己的管理员状态", update_raised.exception.detail)
        fake_db.get_user_by_id.assert_not_called()
        fake_db.delete_user_and_data.assert_not_called()
        fake_db.update_user_admin_status.assert_not_called()

    def test_user_management_routes_surface_database_failures_as_server_errors(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}

        list_db = mock.Mock()
        list_db.get_all_users.side_effect = RuntimeError("user list exploded")

        with mock.patch.object(reply_server, "db_manager", list_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as list_raised:
                reply_server.get_all_users(admin_user=admin_user)

        self.assertEqual(500, list_raised.exception.status_code)
        self.assertEqual("user list exploded", list_raised.exception.detail)
        list_db.get_all_users.assert_called_once_with()

        card_count_db = mock.Mock()
        card_count_db.get_all_users.return_value = [{"id": 7, "username": "demo-user", "password_hash": "hash"}]
        card_count_db.get_account_ids.return_value = ["acc-demo-1"]
        card_count_db.get_all_cards.side_effect = RuntimeError("user card count exploded")

        with mock.patch.object(reply_server, "db_manager", card_count_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as card_count_raised:
                reply_server.get_all_users(admin_user=admin_user)

        self.assertEqual(500, card_count_raised.exception.status_code)
        self.assertEqual("user card count exploded", card_count_raised.exception.detail)
        card_count_db.get_all_users.assert_called_once_with()
        card_count_db.get_account_ids.assert_called_once_with(7)
        card_count_db.get_all_cards.assert_called_once_with(7, summary_only=True)

        lookup_db = mock.Mock()
        lookup_db.get_user_by_id.side_effect = RuntimeError("user lookup exploded")

        with mock.patch.object(reply_server, "db_manager", lookup_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as delete_lookup_raised:
                reply_server.delete_user(7, admin_user=admin_user)

            with self.assertRaises(reply_server.HTTPException) as update_lookup_raised:
                reply_server.update_user_admin_status(8, True, admin_user=admin_user)

        self.assertEqual(500, delete_lookup_raised.exception.status_code)
        self.assertEqual("user lookup exploded", delete_lookup_raised.exception.detail)
        self.assertEqual(500, update_lookup_raised.exception.status_code)
        self.assertEqual("user lookup exploded", update_lookup_raised.exception.detail)
        self.assertEqual(
            lookup_db.get_user_by_id.call_args_list,
            [mock.call(7), mock.call(8)],
        )
        lookup_db.delete_user_and_data.assert_not_called()
        lookup_db.update_user_admin_status.assert_not_called()

        mutation_db = mock.Mock()
        mutation_db.get_user_by_id.side_effect = [
            {"id": 7, "username": "demo-user"},
            {"id": 8, "username": "staff-user", "is_admin": False},
        ]
        mutation_db.delete_user_and_data.side_effect = RuntimeError("user delete exploded")
        mutation_db.update_user_admin_status.side_effect = RuntimeError("user admin update exploded")

        with mock.patch.object(reply_server, "db_manager", mutation_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as delete_mutation_raised:
                reply_server.delete_user(7, admin_user=admin_user)

            with self.assertRaises(reply_server.HTTPException) as update_mutation_raised:
                reply_server.update_user_admin_status(8, True, admin_user=admin_user)

        self.assertEqual(500, delete_mutation_raised.exception.status_code)
        self.assertEqual("user delete exploded", delete_mutation_raised.exception.detail)
        self.assertEqual(500, update_mutation_raised.exception.status_code)
        self.assertEqual("user admin update exploded", update_mutation_raised.exception.detail)
        self.assertEqual(
            mutation_db.get_user_by_id.call_args_list,
            [mock.call(7), mock.call(8)],
        )
        mutation_db.delete_user_and_data.assert_called_once_with(7)
        mutation_db.update_user_admin_status.assert_called_once_with(8, True)

    def test_delete_user_revokes_deleted_users_active_sessions(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}
        target_user = {"id": 7, "username": "deleted-user"}

        with mock.patch.dict(
            reply_server.SESSION_TOKENS,
            {
                "target-session-1": {
                    "user_id": 7,
                    "username": "deleted-user",
                    "is_admin": False,
                    "timestamp": 1.0,
                },
                "target-session-2": {
                    "user_id": "7",
                    "username": "deleted-user",
                    "is_admin": False,
                    "timestamp": 2.0,
                },
                "other-session": {
                    "user_id": 8,
                    "username": "other-user",
                    "is_admin": False,
                    "timestamp": 3.0,
                },
            },
            clear=True,
        ), mock.patch.object(
            reply_server.db_manager,
            "get_user_by_id",
            return_value=target_user,
        ) as get_user_by_id, mock.patch.object(
            reply_server.db_manager,
            "delete_user_and_data",
            return_value=True,
        ) as delete_user_and_data, mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.delete_user(7, admin_user=admin_user)

            self.assertEqual(result["message"], "用户 deleted-user 删除成功")
            self.assertEqual(result["revoked_sessions"], 2)
            self.assertNotIn("target-session-1", reply_server.SESSION_TOKENS)
            self.assertNotIn("target-session-2", reply_server.SESSION_TOKENS)
            self.assertIn("other-session", reply_server.SESSION_TOKENS)

        get_user_by_id.assert_called_once_with(7)
        delete_user_and_data.assert_called_once_with(7)

    def test_admin_data_user_delete_revokes_deleted_users_active_sessions(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}

        with mock.patch.dict(
            reply_server.SESSION_TOKENS,
            {
                "target-session-1": {
                    "user_id": 7,
                    "username": "deleted-user",
                    "is_admin": False,
                    "timestamp": 1.0,
                },
                "target-session-2": {
                    "user_id": "7",
                    "username": "deleted-user",
                    "is_admin": False,
                    "timestamp": 2.0,
                },
                "other-session": {
                    "user_id": 8,
                    "username": "other-user",
                    "is_admin": False,
                    "timestamp": 3.0,
                },
            },
            clear=True,
        ), mock.patch.object(
            reply_server.db_manager,
            "get_user_id_by_rowid",
            return_value=7,
        ) as get_user_id_by_rowid, mock.patch.object(
            reply_server.db_manager,
            "delete_table_record",
            return_value=True,
        ) as delete_table_record, mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.delete_table_record("users", "42", admin_user=admin_user)

            self.assertTrue(result["success"])
            self.assertEqual(result["message"], "删除成功")
            self.assertEqual(result["revoked_sessions"], 2)
            self.assertNotIn("target-session-1", reply_server.SESSION_TOKENS)
            self.assertNotIn("target-session-2", reply_server.SESSION_TOKENS)
            self.assertIn("other-session", reply_server.SESSION_TOKENS)

        get_user_id_by_rowid.assert_called_once_with("42")
        delete_table_record.assert_called_once_with("users", "42")

    def test_admin_data_user_delete_refreshes_cookie_manager_cache_after_revoking_sessions(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}
        fake_cookie_manager = mock.Mock()

        with mock.patch.dict(
            reply_server.SESSION_TOKENS,
            {
                "target-session": {
                    "user_id": 7,
                    "username": "target-user",
                    "is_admin": False,
                    "timestamp": 1.0,
                },
                "other-session": {
                    "user_id": 8,
                    "username": "other-user",
                    "is_admin": False,
                    "timestamp": 2.0,
                },
            },
            clear=True,
        ), mock.patch.object(
            reply_server.db_manager,
            "get_user_id_by_rowid",
            return_value=7,
        ), mock.patch.object(
            reply_server.db_manager,
            "delete_table_record",
            return_value=True,
        ), mock.patch.object(
            reply_server.cookie_manager,
            "manager",
            fake_cookie_manager,
        ), mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.delete_table_record("users", "42", admin_user=admin_user)

        self.assertTrue(result["success"])
        self.assertEqual(1, result["revoked_sessions"])
        fake_cookie_manager.reload_from_db.assert_called_once_with()

    def test_admin_data_cookie_delete_refreshes_cookie_manager_cache(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}
        fake_cookie_manager = mock.Mock()

        with mock.patch.object(
            reply_server.db_manager,
            "delete_table_record",
            return_value=True,
        ) as delete_table_record, mock.patch.object(
            reply_server.cookie_manager,
            "manager",
            fake_cookie_manager,
        ), mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.delete_table_record("cookies", "55", admin_user=admin_user)

        self.assertEqual({"success": True, "message": "删除成功"}, result)
        delete_table_record.assert_called_once_with("cookies", "55")
        fake_cookie_manager.reload_from_db.assert_called_once_with()

    def test_admin_data_user_delete_blocks_self_deletion_by_rowid_lookup(self):
        admin_user = {"user_id": 7, "username": "admin", "is_admin": True}

        with mock.patch.object(
            reply_server.db_manager,
            "get_user_id_by_rowid",
            return_value=7,
        ) as get_user_id_by_rowid, mock.patch.object(
            reply_server.db_manager,
            "delete_table_record",
            return_value=True,
        ) as delete_table_record, mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.delete_table_record("users", "42", admin_user=admin_user)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "不能删除管理员自己")
        get_user_id_by_rowid.assert_called_once_with("42")
        delete_table_record.assert_not_called()

    def test_admin_data_user_delete_blocks_self_deletion_with_string_user_id_snapshot(self):
        admin_user = {"user_id": "7", "username": "admin", "is_admin": True}

        with mock.patch.object(
            reply_server.db_manager,
            "get_user_id_by_rowid",
            return_value=7,
        ) as get_user_id_by_rowid, mock.patch.object(
            reply_server.db_manager,
            "delete_table_record",
            return_value=True,
        ) as delete_table_record, mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.delete_table_record("users", "42", admin_user=admin_user)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "不能删除管理员自己")
        get_user_id_by_rowid.assert_called_once_with("42")
        delete_table_record.assert_not_called()

    def test_admin_data_system_settings_delete_blocks_admin_password_hash_by_rowid_lookup(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}

        with mock.patch.object(
            reply_server.db_manager,
            "get_system_setting_key_by_rowid",
            return_value="admin_password_hash",
        ) as get_system_setting_key_by_rowid, mock.patch.object(
            reply_server.db_manager,
            "delete_table_record",
            return_value=True,
        ) as delete_table_record, mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.delete_table_record("system_settings", "42", admin_user=admin_user)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "不能删除管理员密码配置")
        get_system_setting_key_by_rowid.assert_called_once_with("42")
        delete_table_record.assert_not_called()

    def test_update_user_admin_status_syncs_cached_admin_flags_for_active_sessions(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}
        target_user = {"id": 7, "username": "demoted-admin", "is_admin": True}

        with mock.patch.dict(
            reply_server.SESSION_TOKENS,
            {
                "target-admin-session-1": {
                    "user_id": 7,
                    "username": "demoted-admin",
                    "is_admin": True,
                    "timestamp": 1.0,
                },
                "target-admin-session-2": {
                    "user_id": "7",
                    "username": "demoted-admin",
                    "is_admin": True,
                    "timestamp": 2.0,
                },
                "other-session": {
                    "user_id": 8,
                    "username": "other-user",
                    "is_admin": False,
                    "timestamp": 3.0,
                },
            },
            clear=True,
        ), mock.patch.object(
            reply_server.db_manager,
            "get_user_by_id",
            return_value=target_user,
        ) as get_user_by_id, mock.patch.object(
            reply_server.db_manager,
            "update_user_admin_status",
            return_value=True,
        ) as update_user_admin_status, mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.update_user_admin_status(7, False, admin_user=admin_user)

            self.assertTrue(result["success"])
            self.assertEqual(result["message"], "用户 demoted-admin 已取消管理员权限")
            self.assertEqual(result["updated_sessions"], 2)
            self.assertFalse(reply_server.SESSION_TOKENS["target-admin-session-1"]["is_admin"])
            self.assertFalse(reply_server.SESSION_TOKENS["target-admin-session-2"]["is_admin"])
            self.assertFalse(reply_server.SESSION_TOKENS["other-session"]["is_admin"])

        get_user_by_id.assert_called_once_with(7)
        update_user_admin_status.assert_called_once_with(7, False)


class ReplyServerSystemSettingsAdminRuntimeTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_get_system_settings_keeps_admin_path_but_strips_password_hash(self):
        with mock.patch.object(
            reply_server.db_manager,
            "get_all_system_settings",
            return_value={
                "theme_color": "#4f46e5",
                "qq_reply_secret_key": "secret-key",
                "admin_password_hash": "hash-value",
            },
        ) as get_all_system_settings:
            result = reply_server.get_system_settings(
                admin_user={"user_id": 1, "username": "admin", "is_admin": True}
            )

        get_all_system_settings.assert_called_once_with()
        self.assertEqual(result["theme_color"], "#4f46e5")
        self.assertEqual(result["qq_reply_secret_key"], "secret-key")
        self.assertNotIn("admin_password_hash", result)

    def test_reload_cache_route_requires_admin_dependency_and_preserves_runtime_messages(self):
        route = next(
            route
            for route in reply_server.app.routes
            if getattr(route, "path", None) == "/system/reload-cache"
            and "POST" in getattr(route, "methods", set())
        )
        dependency_calls = [getattr(dep.call, "__name__", str(dep.call)) for dep in route.dependant.dependencies]
        self.assertEqual(["require_admin"], dependency_calls)

        fake_manager = mock.Mock()
        fake_manager.reload_from_db.return_value = True

        with mock.patch.object(reply_server.cookie_manager, "manager", fake_manager):
            result = reply_server.reload_cache(
                admin_user={"user_id": 1, "username": "admin", "is_admin": True}
            )

        self.assertEqual({"message": "系统缓存已刷新", "success": True}, result)
        fake_manager.reload_from_db.assert_called_once_with()

        failing_manager = mock.Mock()
        failing_manager.reload_from_db.side_effect = RuntimeError("reload exploded")

        with mock.patch.object(reply_server.cookie_manager, "manager", failing_manager):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.reload_cache(
                    admin_user={"user_id": 1, "username": "admin", "is_admin": True}
                )

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("刷新缓存失败: reload exploded", raised.exception.detail)

        with mock.patch.object(reply_server.cookie_manager, "manager", None):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.reload_cache(
                    admin_user={"user_id": 1, "username": "admin", "is_admin": True}
                )

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("CookieManager 未初始化", raised.exception.detail)

    def test_user_settings_routes_use_module_bound_database_and_normalize_theme_color(self):
        fake_db = mock.Mock()
        fake_db.get_user_settings.return_value = {
            "theme_color": {"value": "#111827", "description": "主题色"},
            "menu_visibility": {"value": "{}", "description": "菜单显示"},
        }
        fake_db.set_user_setting.return_value = True
        fake_db.get_user_setting.return_value = {
            "key": "theme_color",
            "value": "#111827",
            "description": "主题色",
        }

        shadow_db = mock.Mock()
        shadow_db.get_user_settings.side_effect = AssertionError("用户设置列表不该绕开 reply_server.db_manager")
        shadow_db.set_user_setting.side_effect = AssertionError("用户设置更新不该绕开 reply_server.db_manager")
        shadow_db.get_user_setting.side_effect = AssertionError("用户设置详情不该绕开 reply_server.db_manager")

        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ), mock.patch.object(reply_server, "log_with_user"):
            settings_result = reply_server.get_user_settings(current_user=current_user)
            update_result = reply_server.update_user_setting(
                "theme_color",
                {"value": "  #0f172a  ", "description": "主题色"},
                current_user=current_user,
            )
            setting_result = reply_server.get_user_setting("theme_color", current_user=current_user)

        self.assertEqual(settings_result, fake_db.get_user_settings.return_value)
        self.assertEqual(
            update_result,
            {"msg": "setting updated", "key": "theme_color", "value": "#0f172a"},
        )
        self.assertEqual(setting_result, fake_db.get_user_setting.return_value)
        fake_db.get_user_settings.assert_called_once_with(7)
        fake_db.set_user_setting.assert_called_once_with(7, "theme_color", "#0f172a", "主题色")
        fake_db.get_user_setting.assert_called_once_with(7, "theme_color")

    def test_user_menu_settings_routes_validate_payloads_and_replace_atomically(self):
        fake_db = mock.Mock()
        fake_db.set_user_setting.return_value = True
        fake_db.replace_user_menu_settings.return_value = 2
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(reply_server, "log_with_user"):
            visibility_result = reply_server.update_user_setting(
                "menu_visibility",
                {"value": {"orders": False, "items": True}, "description": "菜单显示"},
                current_user=current_user,
            )
            order_result = reply_server.update_user_setting(
                "menu_order",
                {"value": ["dashboard", "orders", "orders"], "description": "菜单顺序"},
                current_user=current_user,
            )
            replace_result = reply_server.replace_user_menu_settings(
                reply_server.MenuSettingsReplaceIn(
                    visibility={"orders": False, "items": True},
                    order=["dashboard", "orders", "orders"],
                ),
                current_user=current_user,
            )

            with self.assertRaises(reply_server.HTTPException) as bad_visibility_raised:
                reply_server.update_user_setting(
                    "menu_visibility",
                    {"value": "oops", "description": "菜单显示"},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as bad_order_raised:
                reply_server.update_user_setting(
                    "menu_order",
                    {"value": ["dashboard", "bad menu"], "description": "菜单顺序"},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as bad_replace_raised:
                reply_server.replace_user_menu_settings(
                    reply_server.MenuSettingsReplaceIn(
                        visibility={"orders": "yes"},
                        order=["dashboard"],
                    ),
                    current_user=current_user,
                )

        self.assertEqual(
            {"msg": "setting updated", "key": "menu_visibility", "value": '{"orders":false,"items":true}'},
            visibility_result,
        )
        self.assertEqual(
            {"msg": "setting updated", "key": "menu_order", "value": '["dashboard","orders"]'},
            order_result,
        )
        self.assertEqual({"msg": "menu settings replaced", "count": 2}, replace_result)
        self.assertEqual(bad_visibility_raised.exception.status_code, 400)
        self.assertEqual(bad_visibility_raised.exception.detail, "菜单显示设置必须是JSON对象")
        self.assertEqual(bad_order_raised.exception.status_code, 400)
        self.assertEqual(bad_order_raised.exception.detail, "菜单顺序设置包含无效菜单ID")
        self.assertEqual(bad_replace_raised.exception.status_code, 400)
        self.assertEqual(bad_replace_raised.exception.detail, "菜单显示设置的值必须为布尔值")
        fake_db.set_user_setting.assert_has_calls(
            [
                mock.call(7, "menu_visibility", '{"orders":false,"items":true}', "菜单显示"),
                mock.call(7, "menu_order", '["dashboard","orders"]', "菜单顺序"),
            ]
        )
        fake_db.replace_user_menu_settings.assert_called_once_with(
            7,
            '{"orders":false,"items":true}',
            '["dashboard","orders"]',
        )

    def test_user_menu_settings_replace_route_surfaces_database_failures_as_server_errors(self):
        fake_db = mock.Mock()
        fake_db.replace_user_menu_settings.side_effect = RuntimeError("menu settings replace exploded")
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(reply_server, "log_with_user"):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.replace_user_menu_settings(
                    reply_server.MenuSettingsReplaceIn(
                        visibility={"orders": False},
                        order=["dashboard", "orders"],
                    ),
                    current_user=current_user,
                )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "menu settings replace exploded")
        fake_db.replace_user_menu_settings.assert_called_once_with(
            7,
            '{"orders":false}',
            '["dashboard","orders"]',
        )

    def test_get_user_setting_preserves_not_found_http_exception(self):
        fake_db = mock.Mock()
        fake_db.get_user_setting.return_value = None
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.get_user_setting("missing_key", current_user=current_user)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, "设置不存在")
        fake_db.get_user_setting.assert_called_once_with(7, "missing_key")

    def test_change_admin_password_uses_module_bound_database_and_current_admin_username(self):
        fake_db = mock.Mock()
        fake_db.verify_user_password.return_value = True
        fake_db.update_user_password.return_value = True

        shadow_db = mock.Mock()
        shadow_db.verify_user_password.side_effect = AssertionError("管理员密码校验不该绕开 reply_server.db_manager")
        shadow_db.update_user_password.side_effect = AssertionError("管理员密码更新不该绕开 reply_server.db_manager")

        admin_user = {"user_id": 9, "username": "boss-admin", "is_admin": True}
        password_request = reply_server.ChangePasswordRequest(
            current_password="old-secret",
            new_password="new-secret-123",
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ), mock.patch.object(reply_server.logger, "info"):
            result = asyncio.run(
                reply_server.change_admin_password(
                    password_request,
                    admin_user=admin_user,
                )
            )

        self.assertEqual({"success": True, "message": "密码修改成功"}, result)
        fake_db.verify_user_password.assert_called_once_with("boss-admin", "old-secret")
        fake_db.update_user_password.assert_called_once_with("boss-admin", "new-secret-123")

    def test_system_settings_and_login_captcha_routes_use_module_bound_database(self):
        fake_db = mock.Mock()
        fake_db.get_all_system_settings.return_value = {
            "theme_color": "#111827",
            "admin_password_hash": "hash-value",
        }
        fake_db.set_system_setting.return_value = True
        fake_db.get_system_setting.side_effect = lambda key: {
            "registration_enabled": "false",
            "show_default_login_info": "false",
            "login_captcha_enabled": "false",
        }.get(key)

        shadow_db = mock.Mock()
        shadow_db.get_all_system_settings.side_effect = AssertionError("系统设置读取不该绕开 reply_server.db_manager")
        shadow_db.set_system_setting.side_effect = AssertionError("系统设置更新不该绕开 reply_server.db_manager")
        shadow_db.get_system_setting.side_effect = AssertionError("系统设置状态读取不该绕开 reply_server.db_manager")

        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ), mock.patch.object(reply_server, "log_with_user"):
            settings_result = reply_server.get_system_settings(admin_user=admin_user)
            update_setting_result = reply_server.update_system_setting(
                "theme_color",
                reply_server.SystemSettingIn(value="#0f172a", description="主题色"),
                admin_user=admin_user,
            )
            registration_status = reply_server.get_registration_status()
            login_info_status = reply_server.get_login_info_status()
            registration_update_result = reply_server.update_registration_settings(
                reply_server.RegistrationSettingUpdate(enabled=False),
                admin_user=admin_user,
            )
            login_info_update_result = reply_server.update_login_info_settings(
                reply_server.LoginInfoSettingUpdate(enabled=False),
                admin_user=admin_user,
            )
            login_captcha_settings = reply_server.get_login_captcha_settings(admin_user=admin_user)
            login_captcha_update_result = reply_server.update_login_captcha_settings(
                reply_server.LoginInfoSettingUpdate(enabled=False),
                admin_user=admin_user,
            )
            login_captcha_enabled = reply_server.get_login_captcha_enabled()

        self.assertEqual({"theme_color": "#111827"}, settings_result)
        self.assertEqual({"msg": "system setting updated"}, update_setting_result)
        self.assertEqual({"enabled": False, "message": "注册功能已关闭"}, registration_status)
        self.assertEqual({"enabled": False}, login_info_status)
        self.assertEqual(
            {"success": True, "enabled": False, "message": "注册功能已关闭"},
            registration_update_result,
        )
        self.assertEqual(
            {"success": True, "enabled": False, "message": "默认登录信息显示已关闭"},
            login_info_update_result,
        )
        self.assertEqual({"enabled": False}, login_captcha_settings)
        self.assertEqual(
            {"success": True, "enabled": False, "message": "登录验证码已关闭"},
            login_captcha_update_result,
        )
        self.assertEqual({"enabled": False}, login_captcha_enabled)

        fake_db.get_all_system_settings.assert_called_once_with()
        fake_db.set_system_setting.assert_has_calls(
            [
                mock.call("theme_color", "#0f172a", "主题色"),
                mock.call("registration_enabled", "false", "是否开启用户注册"),
                mock.call("show_default_login_info", "false", "是否显示默认登录信息"),
                mock.call("login_captcha_enabled", "false", "是否开启登录验证码"),
            ]
        )
        fake_db.get_system_setting.assert_has_calls(
            [
                mock.call("registration_enabled"),
                mock.call("show_default_login_info"),
                mock.call("login_captcha_enabled"),
                mock.call("login_captcha_enabled"),
            ]
        )

    def test_system_and_user_settings_routes_surface_database_failures_as_server_errors(self):
        fake_db = mock.Mock()
        fake_db.get_all_system_settings.side_effect = RuntimeError("system settings exploded")
        fake_db.set_system_setting.side_effect = RuntimeError("system settings update exploded")
        fake_db.get_system_setting.side_effect = RuntimeError("registration status exploded")
        fake_db.get_user_settings.side_effect = RuntimeError("user settings exploded")
        fake_db.set_user_setting.side_effect = RuntimeError("user settings update exploded")
        fake_db.get_user_setting.side_effect = RuntimeError("user setting detail exploded")

        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}
        current_user = {"user_id": 7, "username": "demo-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "log_with_user",
        ):
            with self.assertRaises(reply_server.HTTPException) as system_list_raised:
                reply_server.get_system_settings(admin_user=admin_user)

            with self.assertRaises(reply_server.HTTPException) as system_update_raised:
                reply_server.update_system_setting(
                    "theme_color",
                    reply_server.SystemSettingIn(value="#0f172a", description="主题色"),
                    admin_user=admin_user,
                )

            with self.assertRaises(reply_server.HTTPException) as registration_raised:
                reply_server.get_registration_status()

            with self.assertRaises(reply_server.HTTPException) as login_info_raised:
                reply_server.get_login_info_status()

            with self.assertRaises(reply_server.HTTPException) as user_list_raised:
                reply_server.get_user_settings(current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as user_update_raised:
                reply_server.update_user_setting(
                    "theme_color",
                    {"value": "#0f172a", "description": "主题色"},
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as user_detail_raised:
                reply_server.get_user_setting("theme_color", current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as login_captcha_enabled_raised:
                reply_server.get_login_captcha_enabled()

        self.assertEqual(system_list_raised.exception.status_code, 500)
        self.assertEqual(system_list_raised.exception.detail, "system settings exploded")
        self.assertEqual(system_update_raised.exception.status_code, 500)
        self.assertEqual(system_update_raised.exception.detail, "system settings update exploded")
        self.assertEqual(registration_raised.exception.status_code, 500)
        self.assertEqual(registration_raised.exception.detail, "registration status exploded")
        self.assertEqual(login_info_raised.exception.status_code, 500)
        self.assertEqual(login_info_raised.exception.detail, "registration status exploded")
        self.assertEqual(user_list_raised.exception.status_code, 500)
        self.assertEqual(user_list_raised.exception.detail, "user settings exploded")
        self.assertEqual(user_update_raised.exception.status_code, 500)
        self.assertEqual(user_update_raised.exception.detail, "user settings update exploded")
        self.assertEqual(user_detail_raised.exception.status_code, 500)
        self.assertEqual(user_detail_raised.exception.detail, "user setting detail exploded")
        self.assertEqual(login_captcha_enabled_raised.exception.status_code, 500)
        self.assertEqual(login_captcha_enabled_raised.exception.detail, "registration status exploded")
        fake_db.get_all_system_settings.assert_called_once_with()
        fake_db.set_system_setting.assert_called_once_with("theme_color", "#0f172a", "主题色")
        self.assertEqual(
            fake_db.get_system_setting.call_args_list,
            [
                mock.call("registration_enabled"),
                mock.call("show_default_login_info"),
                mock.call("login_captcha_enabled"),
            ],
        )
        fake_db.get_user_settings.assert_called_once_with(7)
        fake_db.set_user_setting.assert_called_once_with(7, "theme_color", "#0f172a", "主题色")
        fake_db.get_user_setting.assert_called_once_with(7, "theme_color")


class ReplyServerAdminDataProtectionTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_clear_table_data_blocks_protected_system_tables(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}

        for table_name, expected_message in (
            ("users", "不允许清空用户表"),
            ("system_settings", "不允许清空系统设置表"),
            ("notification_channels", "不允许清空通知渠道表"),
            ("notification_templates", "不允许清空通知模板表"),
            ("scheduled_tasks", "不允许清空定时任务表"),
        ):
            with self.subTest(table_name=table_name):
                with mock.patch.object(reply_server.db_manager, "clear_table_data", return_value=True) as clear_table_data, \
                     mock.patch.object(reply_server, "log_with_user"):
                    with self.assertRaises(reply_server.HTTPException) as raised:
                        reply_server.clear_table_data(table_name, admin_user=admin_user)

                self.assertEqual(400, raised.exception.status_code)
                self.assertEqual(expected_message, raised.exception.detail)
                clear_table_data.assert_not_called()


class ReplyServerAdminStatsAndDataManagementRuntimeTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_clear_cookies_refreshes_cookie_manager_cache(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}
        fake_db = mock.Mock()
        fake_db.clear_table_data.return_value = True
        fake_cookie_manager = mock.Mock()

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server.cookie_manager,
            "manager",
            fake_cookie_manager,
        ), mock.patch.object(reply_server, "log_with_user"):
            result = reply_server.clear_table_data("cookies", admin_user=admin_user)

        self.assertEqual({"success": True, "message": "清空成功"}, result)
        fake_db.clear_table_data.assert_called_once_with("cookies")
        fake_cookie_manager.reload_from_db.assert_called_once_with()

    def test_admin_stats_and_data_management_routes_use_module_bound_database(self):
        fake_db = mock.Mock()
        fake_db.get_all_users.return_value = [{"id": 1}, {"id": 2}]
        fake_db.get_account_ids.return_value = ["acc-demo-1", "acc-demo-2"]
        fake_db.get_all_cards.return_value = [
            {"id": 1, "enabled": True},
            {"id": 2, "enabled": False},
        ]
        fake_db.get_table_data.return_value = (
            [{"__admin_rowid": 11, "order_id": "order-demo-1"}],
            ["order_id"],
        )
        fake_db.delete_table_record.return_value = True
        fake_db.clear_table_data.return_value = True

        shadow_db = mock.Mock()
        shadow_db.get_all_users.side_effect = AssertionError("管理员统计不该绕开 reply_server.db_manager")
        shadow_db.get_account_ids.side_effect = AssertionError("管理员统计不该绕开 reply_server.db_manager")
        shadow_db.get_all_cards.side_effect = AssertionError("管理员统计不该绕开 reply_server.db_manager")
        shadow_db.get_table_data.side_effect = AssertionError("数据管理读取不该绕开 reply_server.db_manager")
        shadow_db.delete_table_record.side_effect = AssertionError("数据管理删除不该绕开 reply_server.db_manager")
        shadow_db.clear_table_data.side_effect = AssertionError("数据管理清空不该绕开 reply_server.db_manager")

        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ), mock.patch.object(reply_server, "log_with_user"):
            stats_result = reply_server.get_system_stats(admin_user=admin_user)
            table_result = reply_server.get_table_data("orders", admin_user=admin_user)
            export_result = reply_server.export_table_data("orders", admin_user=admin_user)
            delete_result = reply_server.delete_table_record("orders", "11", admin_user=admin_user)
            clear_result = reply_server.clear_table_data("orders", admin_user=admin_user)

        self.assertEqual(2, stats_result["users"]["total"])
        self.assertEqual(2, stats_result["cookies"]["total"])
        self.assertEqual(2, stats_result["cards"]["total"])
        self.assertEqual(1, stats_result["cards"]["enabled"])
        self.assertEqual(
            {
                "success": True,
                "data": fake_db.get_table_data.return_value[0],
                "columns": fake_db.get_table_data.return_value[1],
                "count": 1,
            },
            table_result,
        )
        self.assertEqual(
            "attachment; filename=orders_export.xlsx",
            export_result.headers.get("content-disposition"),
        )
        self.assertEqual({"success": True, "message": "删除成功"}, delete_result)
        self.assertEqual({"success": True, "message": "清空成功"}, clear_result)

        fake_db.get_all_users.assert_called_once_with()
        fake_db.get_account_ids.assert_called_once_with()
        fake_db.get_all_cards.assert_called_once_with(summary_only=True)
        fake_db.get_table_data.assert_has_calls([mock.call("orders"), mock.call("orders")])
        fake_db.delete_table_record.assert_called_once_with("orders", "11")
        fake_db.clear_table_data.assert_called_once_with("orders")

    def test_data_management_routes_include_risk_control_logs_table(self):
        fake_db = mock.Mock()
        fake_db.get_table_data.return_value = (
            [{"__admin_rowid": 21, "account_id": "acc-risk-1", "event_type": "slider_captcha"}],
            ["account_id", "event_type"],
        )
        fake_db.delete_table_record.return_value = True
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            table_result = reply_server.get_table_data("risk_control_logs", admin_user=admin_user)
            delete_result = reply_server.delete_table_record("risk_control_logs", "21", admin_user=admin_user)

        self.assertEqual(
            {
                "success": True,
                "data": fake_db.get_table_data.return_value[0],
                "columns": fake_db.get_table_data.return_value[1],
                "count": 1,
            },
            table_result,
        )
        self.assertEqual({"success": True, "message": "删除成功"}, delete_result)
        fake_db.get_table_data.assert_called_once_with("risk_control_logs")
        fake_db.delete_table_record.assert_called_once_with("risk_control_logs", "21")

    def test_data_management_routes_surface_database_failures_as_server_errors(self):
        admin_user = {"user_id": 1, "username": "admin", "is_admin": True}

        stats_db = mock.Mock()
        stats_db.get_all_users.return_value = [{"id": 1}]
        stats_db.get_account_ids.return_value = ["acc-demo-1"]
        stats_db.get_all_cards.side_effect = RuntimeError("admin cards exploded")

        with mock.patch.object(reply_server, "db_manager", stats_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as stats_raised:
                reply_server.get_system_stats(admin_user=admin_user)

        self.assertEqual(500, stats_raised.exception.status_code)
        self.assertEqual("admin cards exploded", stats_raised.exception.detail)
        stats_db.get_all_users.assert_called_once_with()
        stats_db.get_account_ids.assert_called_once_with()
        stats_db.get_all_cards.assert_called_once_with(summary_only=True)

        list_db = mock.Mock()
        list_db.get_table_data.side_effect = RuntimeError("table data exploded")

        with mock.patch.object(reply_server, "db_manager", list_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as list_raised:
                reply_server.get_table_data("orders", admin_user=admin_user)

        self.assertEqual(500, list_raised.exception.status_code)
        self.assertEqual("查询表数据失败，请稍后重试", list_raised.exception.detail)
        list_db.get_table_data.assert_called_once_with("orders")

        export_db = mock.Mock()
        export_db.get_table_data.side_effect = RuntimeError("table export exploded")

        with mock.patch.object(reply_server, "db_manager", export_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as export_raised:
                reply_server.export_table_data("orders", admin_user=admin_user)

        self.assertEqual(500, export_raised.exception.status_code)
        self.assertEqual("导出表数据失败，请稍后重试", export_raised.exception.detail)
        export_db.get_table_data.assert_called_once_with("orders")

        delete_db = mock.Mock()
        delete_db.delete_table_record.side_effect = RuntimeError("table delete exploded")

        with mock.patch.object(reply_server, "db_manager", delete_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as delete_raised:
                reply_server.delete_table_record("orders", "11", admin_user=admin_user)

        self.assertEqual(500, delete_raised.exception.status_code)
        self.assertEqual("删除表记录失败，请稍后重试", delete_raised.exception.detail)
        delete_db.delete_table_record.assert_called_once_with("orders", "11")

        clear_db = mock.Mock()
        clear_db.clear_table_data.side_effect = RuntimeError("table clear exploded")

        with mock.patch.object(reply_server, "db_manager", clear_db), mock.patch.object(
            reply_server, "log_with_user"
        ):
            with self.assertRaises(reply_server.HTTPException) as clear_raised:
                reply_server.clear_table_data("orders", admin_user=admin_user)

        self.assertEqual(500, clear_raised.exception.status_code)
        self.assertEqual("清空表数据失败，请稍后重试", clear_raised.exception.detail)
        clear_db.clear_table_data.assert_called_once_with("orders")


class ReplyServerNotificationTemplateUserScopeRuntimeTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_notification_channel_message_and_template_routes_use_module_bound_database(self):
        fake_db = mock.Mock()
        fake_db.get_all_message_notifications.return_value = {
            "acc-demo-1": [{"id": 11, "channel_name": "Webhook", "enabled": True}],
        }
        fake_db.get_account_notifications.return_value = [
            {"id": 11, "account_id": "acc-demo-1", "channel_id": 5, "enabled": True}
        ]
        fake_db.get_notification_channel.return_value = {
            "id": 5,
            "name": "Webhook",
            "type": "webhook",
            "config": "{}",
            "enabled": True,
        }
        fake_db.set_message_notification.return_value = True
        fake_db.delete_account_notifications.return_value = True
        fake_db.delete_message_notification.return_value = True
        fake_db.get_notification_channels.return_value = [
            {"id": 5, "name": "Webhook", "type": "webhook", "config": "{}", "enabled": True}
        ]
        fake_db.create_notification_channel.return_value = 19
        fake_db.update_notification_channel.return_value = True
        fake_db.delete_notification_channel.return_value = True
        fake_db.get_all_notification_templates.return_value = [
            {"type": "message", "template": "hello", "user_id": 7}
        ]
        fake_db.get_notification_template.return_value = {
            "type": "message",
            "template": "hello",
            "user_id": 7,
        }
        fake_db.update_notification_template.return_value = True
        fake_db.reset_notification_template.return_value = True
        fake_db.get_default_notification_template.return_value = "default message"

        shadow_db = mock.Mock()
        shadow_db.get_all_message_notifications.side_effect = AssertionError("消息通知列表不该绕开 reply_server.db_manager")
        shadow_db.get_account_notifications.side_effect = AssertionError("消息通知详情不该绕开 reply_server.db_manager")
        shadow_db.get_notification_channel.side_effect = AssertionError("通知渠道读取不该绕开 reply_server.db_manager")
        shadow_db.set_message_notification.side_effect = AssertionError("消息通知保存不该绕开 reply_server.db_manager")
        shadow_db.delete_account_notifications.side_effect = AssertionError("消息通知批量删除不该绕开 reply_server.db_manager")
        shadow_db.delete_message_notification.side_effect = AssertionError("消息通知删除不该绕开 reply_server.db_manager")
        shadow_db.get_notification_channels.side_effect = AssertionError("通知渠道列表不该绕开 reply_server.db_manager")
        shadow_db.create_notification_channel.side_effect = AssertionError("通知渠道创建不该绕开 reply_server.db_manager")
        shadow_db.update_notification_channel.side_effect = AssertionError("通知渠道更新不该绕开 reply_server.db_manager")
        shadow_db.delete_notification_channel.side_effect = AssertionError("通知渠道删除不该绕开 reply_server.db_manager")
        shadow_db.get_all_notification_templates.side_effect = AssertionError("通知模板列表不该绕开 reply_server.db_manager")
        shadow_db.get_notification_template.side_effect = AssertionError("通知模板读取不该绕开 reply_server.db_manager")
        shadow_db.update_notification_template.side_effect = AssertionError("通知模板更新不该绕开 reply_server.db_manager")
        shadow_db.reset_notification_template.side_effect = AssertionError("通知模板重置不该绕开 reply_server.db_manager")
        shadow_db.get_default_notification_template.side_effect = AssertionError("默认通知模板读取不该绕开 reply_server.db_manager")

        current_user = {"user_id": 7, "username": "scope-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ):
            notifications_result = reply_server.get_all_message_notifications(current_user=current_user)
            account_notifications_result = reply_server.get_account_notifications(
                "acc-demo-1",
                current_user=current_user,
            )
            set_result = reply_server.set_message_notification(
                "acc-demo-1",
                reply_server.MessageNotificationIn(channel_id=5, enabled=True),
                current_user=current_user,
            )
            delete_account_result = reply_server.delete_account_notifications(
                "acc-demo-1",
                current_user=current_user,
            )
            delete_notification_result = reply_server.delete_message_notification(
                11,
                current_user=current_user,
            )
            channel_list_result = reply_server.get_notification_channels(current_user=current_user)
            channel_create_result = reply_server.create_notification_channel(
                reply_server.NotificationChannelIn(name="Webhook", type="webhook", config="{}", enabled=False),
                current_user=current_user,
            )
            channel_detail_result = reply_server.get_notification_channel(5, current_user=current_user)
            channel_update_result = reply_server.update_notification_channel(
                5,
                reply_server.NotificationChannelUpdate(name="Webhook v2", config="{}", enabled=False),
                current_user=current_user,
            )
            channel_delete_result = reply_server.delete_notification_channel(5, current_user=current_user)
            template_list_result = reply_server.get_notification_templates(current_user=current_user)
            template_detail_result = reply_server.get_notification_template("message", current_user=current_user)
            template_update_result = reply_server.update_notification_template(
                "message",
                reply_server.NotificationTemplateIn(template="patched"),
                current_user=current_user,
            )
            template_reset_result = reply_server.reset_notification_template("message", current_user=current_user)
            template_default_result = reply_server.get_default_notification_template(
                "message",
                current_user=current_user,
            )

        self.assertEqual(
            notifications_result,
            {"acc-demo-1": fake_db.get_all_message_notifications.return_value["acc-demo-1"]},
        )
        self.assertEqual(account_notifications_result, fake_db.get_account_notifications.return_value)
        self.assertEqual(set_result, {"msg": "message notification set"})
        self.assertEqual(delete_account_result, {"msg": "account notifications deleted"})
        self.assertEqual(delete_notification_result, {"msg": "message notification deleted"})
        self.assertEqual(channel_list_result, fake_db.get_notification_channels.return_value)
        self.assertEqual(channel_create_result, {"msg": "notification channel created", "id": 19})
        self.assertEqual(channel_detail_result, fake_db.get_notification_channel.return_value)
        self.assertEqual(channel_update_result, {"msg": "notification channel updated"})
        self.assertEqual(channel_delete_result, {"msg": "notification channel deleted"})
        self.assertEqual(template_list_result, {"templates": fake_db.get_all_notification_templates.return_value})
        self.assertEqual(template_detail_result, fake_db.get_notification_template.return_value)
        self.assertEqual(template_update_result, {"msg": "notification template updated"})
        self.assertEqual(template_reset_result, {"msg": "notification template reset", "template": fake_db.get_notification_template.return_value})
        self.assertEqual(template_default_result, {"type": "message", "template": "default message"})

        fake_db.get_all_message_notifications.assert_called_once_with(user_id=7)
        fake_db.get_account_notifications.assert_called_once_with("acc-demo-1")
        fake_db.get_notification_channel.assert_has_calls([mock.call(5, user_id=7), mock.call(5, user_id=7)])
        fake_db.set_message_notification.assert_called_once_with("acc-demo-1", 5, True)
        fake_db.delete_account_notifications.assert_called_once_with("acc-demo-1", user_id=7)
        fake_db.delete_message_notification.assert_called_once_with(11, user_id=7)
        fake_db.get_notification_channels.assert_called_once_with(7)
        fake_db.create_notification_channel.assert_called_once_with(
            "Webhook",
            "webhook",
            "{}",
            7,
            enabled=False,
        )
        fake_db.update_notification_channel.assert_called_once_with(5, "Webhook v2", "{}", False, user_id=7)
        fake_db.delete_notification_channel.assert_called_once_with(5, user_id=7)
        fake_db.get_all_notification_templates.assert_called_once_with(7)
        fake_db.get_notification_template.assert_has_calls(
            [
                mock.call("message", user_id=7),
                mock.call("message", user_id=7),
            ]
        )
        fake_db.update_notification_template.assert_called_once_with("message", "patched", user_id=7)
        fake_db.reset_notification_template.assert_called_once_with("message", user_id=7)
        fake_db.get_default_notification_template.assert_called_once_with("message")

    def test_notification_channel_list_route_redacts_secret_config_fields(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {
                "id": 5,
                "name": "Webhook",
                "type": "webhook",
                "config": '{"webhook_url":"https://example.invalid/hook","secret":"demo-secret","bot_token":"demo-bot","recipient_email":"demo@example.com"}',
                "enabled": True,
            }
        ]
        current_user = {"user_id": 7, "username": "scope-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            result = reply_server.get_notification_channels(current_user=current_user)

        self.assertEqual(1, len(result))
        sanitized_config = json.loads(result[0]["config"])
        self.assertEqual("****", sanitized_config["webhook_url"])
        self.assertEqual("****", sanitized_config["secret"])
        self.assertEqual("****", sanitized_config["bot_token"])
        self.assertEqual("demo@example.com", sanitized_config["recipient_email"])
        fake_db.get_notification_channels.assert_called_once_with(7)

    def test_message_notification_routes_redact_channel_config_before_returning_to_browser(self):
        fake_db = mock.Mock()
        fake_db.get_all_message_notifications.return_value = {
            "acc-demo-1": [
                {
                    "id": 11,
                    "account_id": "acc-demo-1",
                    "channel_id": 5,
                    "enabled": True,
                    "channel_name": "Webhook",
                    "channel_type": "webhook",
                    "channel_config": '{"webhook_url":"https://example.invalid/hook?token=demo","secret":"demo-secret"}',
                    "channel_enabled": True,
                }
            ]
        }
        fake_db.get_account_notifications.return_value = [
            {
                "id": 11,
                "account_id": "acc-demo-1",
                "channel_id": 5,
                "enabled": True,
                "channel_name": "Webhook",
                "channel_type": "webhook",
                "channel_config": '{"webhook_url":"https://example.invalid/hook?token=demo","secret":"demo-secret"}',
                "channel_enabled": True,
            }
        ]
        current_user = {"user_id": 7, "username": "scope-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ):
            list_result = reply_server.get_all_message_notifications(current_user=current_user)
            detail_result = reply_server.get_account_notifications("acc-demo-1", current_user=current_user)

        list_config = json.loads(list_result["acc-demo-1"][0]["channel_config"])
        detail_config = json.loads(detail_result[0]["channel_config"])
        self.assertEqual("****", list_config["webhook_url"])
        self.assertEqual("****", list_config["secret"])
        self.assertEqual("****", detail_config["webhook_url"])
        self.assertEqual("****", detail_config["secret"])
        fake_db.get_all_message_notifications.assert_called_once_with(user_id=7)
        fake_db.get_account_notifications.assert_called_once_with("acc-demo-1")

    def test_replace_account_notifications_route_uses_module_bound_database_and_validates_channel_scope(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {"id": 5, "name": "Webhook", "type": "webhook", "config": "{}", "enabled": True},
            {"id": 7, "name": "Email", "type": "email", "config": "{}", "enabled": True},
        ]
        fake_db.replace_account_notifications.return_value = 2
        current_user = {"user_id": 7, "username": "scope-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ):
            result = reply_server.replace_account_notifications(
                "acc-demo-1",
                reply_server.AccountMessageNotificationsReplaceIn(channel_ids=[5, 7], enabled=False),
                current_user=current_user,
            )

            with self.assertRaises(reply_server.HTTPException) as missing_raised:
                reply_server.replace_account_notifications(
                    "acc-demo-1",
                    reply_server.AccountMessageNotificationsReplaceIn(channel_ids=[], enabled=True),
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as foreign_raised:
                reply_server.replace_account_notifications(
                    "acc-demo-1",
                    reply_server.AccountMessageNotificationsReplaceIn(channel_ids=[5, 9], enabled=True),
                    current_user=current_user,
                )

        self.assertEqual({"msg": "account notifications replaced", "count": 2}, result)
        self.assertEqual(missing_raised.exception.status_code, 400)
        self.assertEqual(missing_raised.exception.detail, "请选择通知渠道")
        self.assertEqual(foreign_raised.exception.status_code, 404)
        self.assertEqual(foreign_raised.exception.detail, "通知渠道不存在")
        fake_db.get_notification_channels.assert_has_calls([mock.call(7), mock.call(7)])
        fake_db.replace_account_notifications.assert_called_once_with(
            "acc-demo-1",
            [5, 7],
            enabled=False,
            user_id=7,
        )

    def test_notification_channel_routes_surface_database_failures_as_server_errors(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.side_effect = RuntimeError("notification channel list exploded")
        fake_db.create_notification_channel.side_effect = RuntimeError("notification channel create exploded")
        fake_db.get_notification_channel.side_effect = RuntimeError("notification channel detail exploded")
        fake_db.update_notification_channel.side_effect = RuntimeError("notification channel update exploded")
        fake_db.delete_notification_channel.side_effect = RuntimeError("notification channel delete exploded")
        current_user = {"user_id": 7, "username": "scope-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as list_raised:
                reply_server.get_notification_channels(current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as create_raised:
                reply_server.create_notification_channel(
                    reply_server.NotificationChannelIn(name="Webhook", type="webhook", config="{}", enabled=True),
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as detail_raised:
                reply_server.get_notification_channel(5, current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as update_raised:
                reply_server.update_notification_channel(
                    5,
                    reply_server.NotificationChannelUpdate(name="Webhook v2", config="{}", enabled=False),
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as delete_raised:
                reply_server.delete_notification_channel(5, current_user=current_user)

        self.assertEqual(list_raised.exception.status_code, 500)
        self.assertEqual(list_raised.exception.detail, "notification channel list exploded")
        self.assertEqual(create_raised.exception.status_code, 500)
        self.assertEqual(create_raised.exception.detail, "notification channel create exploded")
        self.assertEqual(detail_raised.exception.status_code, 500)
        self.assertEqual(detail_raised.exception.detail, "notification channel detail exploded")
        self.assertEqual(update_raised.exception.status_code, 500)
        self.assertEqual(update_raised.exception.detail, "notification channel update exploded")
        self.assertEqual(delete_raised.exception.status_code, 500)
        self.assertEqual(delete_raised.exception.detail, "notification channel delete exploded")
        fake_db.get_notification_channels.assert_called_once_with(7)
        fake_db.create_notification_channel.assert_called_once_with("Webhook", "webhook", "{}", 7, enabled=True)
        fake_db.get_notification_channel.assert_called_once_with(5, user_id=7)
        fake_db.update_notification_channel.assert_called_once_with(5, "Webhook v2", "{}", False, user_id=7)
        fake_db.delete_notification_channel.assert_called_once_with(5, user_id=7)

    def test_notification_channel_routes_reject_blank_name_and_invalid_type_as_bad_request(self):
        fake_db = mock.Mock()
        fake_db.create_notification_channel.side_effect = [
            ValueError("通知渠道名称不能为空"),
            ValueError("通知渠道类型无效"),
        ]
        fake_db.update_notification_channel.side_effect = ValueError("通知渠道名称不能为空")
        current_user = {"user_id": 7, "username": "scope-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as blank_name_raised:
                reply_server.create_notification_channel(
                    reply_server.NotificationChannelIn(name="   ", type="webhook", config="{}", enabled=True),
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as invalid_type_raised:
                reply_server.create_notification_channel(
                    reply_server.NotificationChannelIn(name="Webhook", type="bad-type", config="{}", enabled=True),
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as update_raised:
                reply_server.update_notification_channel(
                    5,
                    reply_server.NotificationChannelUpdate(name="   ", config="{}", enabled=False),
                    current_user=current_user,
                )

        self.assertEqual(blank_name_raised.exception.status_code, 400)
        self.assertEqual(blank_name_raised.exception.detail, "通知渠道名称不能为空")
        self.assertEqual(invalid_type_raised.exception.status_code, 400)
        self.assertEqual(invalid_type_raised.exception.detail, "通知渠道类型无效")
        self.assertEqual(update_raised.exception.status_code, 400)
        self.assertEqual(update_raised.exception.detail, "通知渠道名称不能为空")
        fake_db.create_notification_channel.assert_has_calls(
            [
                mock.call("   ", "webhook", "{}", 7, enabled=True),
                mock.call("Webhook", "bad-type", "{}", 7, enabled=True),
            ]
        )
        fake_db.update_notification_channel.assert_called_once_with(5, "   ", "{}", False, user_id=7)

    def test_notification_template_update_rejects_blank_content_as_bad_request(self):
        fake_db = mock.Mock()
        fake_db.update_notification_template.side_effect = ValueError("通知模板内容不能为空")
        current_user = {"user_id": 7, "username": "scope-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.update_notification_template(
                    "message",
                    reply_server.NotificationTemplateIn(template="   "),
                    current_user=current_user,
                )

        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("通知模板内容不能为空", raised.exception.detail)
        fake_db.update_notification_template.assert_called_once_with("message", "   ", user_id=7)

    def test_message_notification_and_template_routes_surface_database_failures_as_server_errors(self):
        fake_db = mock.Mock()
        fake_db.get_all_cookies.return_value = {"acc-demo-1": "cookie-value"}
        fake_db.get_all_message_notifications.side_effect = RuntimeError("message notification list exploded")
        fake_db.get_account_notifications.side_effect = RuntimeError("message notification detail exploded")
        fake_db.get_notification_channel.return_value = {
            "id": 5,
            "name": "Webhook",
            "type": "webhook",
            "config": "{}",
            "enabled": True,
        }
        fake_db.set_message_notification.side_effect = RuntimeError("message notification set exploded")
        fake_db.delete_account_notifications.side_effect = RuntimeError("message notification bulk delete exploded")
        fake_db.delete_message_notification.side_effect = RuntimeError("message notification delete exploded")
        fake_db.get_all_notification_templates.side_effect = RuntimeError("notification template list exploded")
        fake_db.get_notification_template.side_effect = RuntimeError("notification template detail exploded")
        fake_db.update_notification_template.side_effect = RuntimeError("notification template update exploded")
        fake_db.reset_notification_template.side_effect = RuntimeError("notification template reset exploded")
        current_user = {"user_id": 7, "username": "scope-user", "is_admin": False}

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ):
            with self.assertRaises(reply_server.HTTPException) as list_raised:
                reply_server.get_all_message_notifications(current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as detail_raised:
                reply_server.get_account_notifications("acc-demo-1", current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as set_raised:
                reply_server.set_message_notification(
                    "acc-demo-1",
                    reply_server.MessageNotificationIn(channel_id=5, enabled=True),
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as delete_account_raised:
                reply_server.delete_account_notifications("acc-demo-1", current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as delete_notification_raised:
                reply_server.delete_message_notification(11, current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as template_list_raised:
                reply_server.get_notification_templates(current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as template_detail_raised:
                reply_server.get_notification_template("message", current_user=current_user)

            with self.assertRaises(reply_server.HTTPException) as template_update_raised:
                reply_server.update_notification_template(
                    "message",
                    reply_server.NotificationTemplateIn(template="patched"),
                    current_user=current_user,
                )

            with self.assertRaises(reply_server.HTTPException) as template_reset_raised:
                reply_server.reset_notification_template("message", current_user=current_user)

        self.assertEqual(list_raised.exception.status_code, 500)
        self.assertEqual(list_raised.exception.detail, "message notification list exploded")
        self.assertEqual(detail_raised.exception.status_code, 500)
        self.assertEqual(detail_raised.exception.detail, "message notification detail exploded")
        self.assertEqual(set_raised.exception.status_code, 500)
        self.assertEqual(set_raised.exception.detail, "message notification set exploded")
        self.assertEqual(delete_account_raised.exception.status_code, 500)
        self.assertEqual(delete_account_raised.exception.detail, "message notification bulk delete exploded")
        self.assertEqual(delete_notification_raised.exception.status_code, 500)
        self.assertEqual(delete_notification_raised.exception.detail, "message notification delete exploded")
        self.assertEqual(template_list_raised.exception.status_code, 500)
        self.assertEqual(template_list_raised.exception.detail, "notification template list exploded")
        self.assertEqual(template_detail_raised.exception.status_code, 500)
        self.assertEqual(template_detail_raised.exception.detail, "notification template detail exploded")
        self.assertEqual(template_update_raised.exception.status_code, 500)
        self.assertEqual(template_update_raised.exception.detail, "notification template update exploded")
        self.assertEqual(template_reset_raised.exception.status_code, 500)
        self.assertEqual(template_reset_raised.exception.detail, "notification template reset exploded")
        fake_db.get_account_ids.assert_called_once_with(7)
        self.assertEqual(
            [mock.call(user_id=7), mock.call()],
            fake_db.get_all_message_notifications.call_args_list,
        )
        fake_db.get_account_notifications.assert_called_once_with("acc-demo-1")
        fake_db.get_notification_channel.assert_called_once_with(5, user_id=7)
        fake_db.set_message_notification.assert_called_once_with("acc-demo-1", 5, True)
        fake_db.delete_account_notifications.assert_called_once_with("acc-demo-1", user_id=7)
        fake_db.delete_message_notification.assert_called_once_with(11, user_id=7)
        fake_db.get_all_notification_templates.assert_called_once_with(7)
        fake_db.get_notification_template.assert_called_once_with("message", user_id=7)
        fake_db.update_notification_template.assert_called_once_with("message", "patched", user_id=7)
        fake_db.reset_notification_template.assert_called_once_with("message", user_id=7)
        fake_db.get_default_notification_template.assert_not_called()

        replace_db = mock.Mock()
        replace_db.get_notification_channels.return_value = [{"id": 5, "name": "Webhook", "type": "webhook", "config": "{}", "enabled": True}]
        replace_db.replace_account_notifications.side_effect = RuntimeError("message notification replace exploded")

        with mock.patch.object(reply_server, "db_manager", replace_db), mock.patch.object(
            reply_server,
            "_ensure_account_access",
            return_value="acc-demo-1",
        ):
            with self.assertRaises(reply_server.HTTPException) as replace_raised:
                reply_server.replace_account_notifications(
                    "acc-demo-1",
                    reply_server.AccountMessageNotificationsReplaceIn(channel_ids=[5], enabled=True),
                    current_user=current_user,
                )

        self.assertEqual(replace_raised.exception.status_code, 500)
        self.assertEqual(replace_raised.exception.detail, "message notification replace exploded")
        replace_db.get_notification_channels.assert_called_once_with(7)
        replace_db.replace_account_notifications.assert_called_once_with(
            "acc-demo-1",
            [5],
            enabled=True,
            user_id=7,
        )

    def test_notification_template_routes_pass_current_user_scope_to_db_layer(self):
        current_user = {"user_id": 7, "username": "scope-user"}

        with mock.patch.object(
            reply_server.db_manager,
            "get_all_notification_templates",
            return_value=[{"type": "message", "template": "hello", "user_id": 7}],
        ) as get_all_templates, mock.patch.object(
            reply_server.db_manager,
            "get_notification_template",
            return_value={"type": "message", "template": "hello", "user_id": 7},
        ) as get_template, mock.patch.object(
            reply_server.db_manager,
            "update_notification_template",
            return_value=True,
        ) as update_template, mock.patch.object(
            reply_server.db_manager,
            "reset_notification_template",
            return_value=True,
        ) as reset_template:
            list_result = reply_server.get_notification_templates(current_user=current_user)
            detail_result = reply_server.get_notification_template("message", current_user=current_user)
            update_result = reply_server.update_notification_template(
                "message",
                reply_server.NotificationTemplateIn(template="patched"),
                current_user=current_user,
            )
            reset_result = reply_server.reset_notification_template("message", current_user=current_user)

        self.assertEqual(list_result["templates"][0]["user_id"], 7)
        self.assertEqual(detail_result["user_id"], 7)
        self.assertEqual(update_result["msg"], "notification template updated")
        self.assertEqual(reset_result["msg"], "notification template reset")
        get_all_templates.assert_called_once_with(7)
        get_template.assert_has_calls(
            [
                mock.call("message", user_id=7),
                mock.call("message", user_id=7),
            ]
        )
        update_template.assert_called_once_with("message", "patched", user_id=7)
        reset_template.assert_called_once_with("message", user_id=7)

    def test_notification_dispatcher_renders_templates_with_owner_scope(self):
        from utils import notification_dispatcher

        with mock.patch.object(
            reply_server.db_manager,
            "get_cookie_details",
            return_value={"user_id": 9},
        ) as get_cookie_details, mock.patch.object(
            reply_server.db_manager,
            "get_notification_template",
            return_value={"template": "scoped template {account_id}"},
        ) as get_template:
            rendered = notification_dispatcher.render_notification_template(
                "message",
                owner_account_id="acc-template-1",
                account_id="acc-template-1",
            )

        get_cookie_details.assert_called_once_with("acc-template-1")
        get_template.assert_called_once_with("message", user_id=9)
        self.assertEqual(rendered, "scoped template acc-template-1")


class ReplyServerAdminPageAssetVersioningTest(_ReplyServerModuleBindingMixin, unittest.IsolatedAsyncioTestCase):
    async def test_admin_page_css_version_tracks_imported_stylesheet_updates(self):
        with tempfile.TemporaryDirectory(prefix="admin_page_assets_") as temp_dir:
            static_root = Path(temp_dir)
            (static_root / "js").mkdir(parents=True, exist_ok=True)
            (static_root / "css").mkdir(parents=True, exist_ok=True)

            index_path = static_root / "index.html"
            index_path.write_text(
                '<link rel="stylesheet" href="/static/css/app.css">\n'
                '<script src="/static/js/app.js?v=stale"></script>\n',
                encoding="utf-8",
            )
            app_js_path = static_root / "js" / "app.js"
            app_js_path.write_text("console.log('app');\n", encoding="utf-8")
            app_css_path = static_root / "css" / "app.css"
            app_css_path.write_text("@import url('variables.css');\n", encoding="utf-8")
            imported_css_path = static_root / "css" / "variables.css"
            imported_css_path.write_text(":root { --demo: 1; }\n", encoding="utf-8")

            os.utime(app_js_path, (3000, 3000))
            os.utime(app_css_path, (1000, 1000))
            os.utime(imported_css_path, (2000, 2000))

            with mock.patch.object(reply_server, "static_dir", str(static_root)):
                response = await reply_server.admin_page()

        body = response.body.decode("utf-8")
        self.assertIn('/static/js/app.js?v=3000', body)
        self.assertIn('/static/css/app.css?v=2000', body)
        self.assertNotIn('/static/css/app.css?v=1000', body)

    async def test_admin_page_adds_js_version_even_when_index_html_lacks_query_param(self):
        with tempfile.TemporaryDirectory(prefix="admin_page_js_assets_") as temp_dir:
            static_root = Path(temp_dir)
            (static_root / "js").mkdir(parents=True, exist_ok=True)
            (static_root / "css").mkdir(parents=True, exist_ok=True)

            index_path = static_root / "index.html"
            index_path.write_text(
                '<link rel="stylesheet" href="/static/css/app.css">\n'
                '<script src="/static/js/app.js"></script>\n',
                encoding="utf-8",
            )
            app_js_path = static_root / "js" / "app.js"
            app_js_path.write_text("console.log('app');\n", encoding="utf-8")
            app_css_path = static_root / "css" / "app.css"
            app_css_path.write_text("body { color: #000; }\n", encoding="utf-8")

            os.utime(app_js_path, (3456, 3456))
            os.utime(app_css_path, (1234, 1234))

            with mock.patch.object(reply_server, "static_dir", str(static_root)):
                response = await reply_server.admin_page()

        body = response.body.decode("utf-8")
        self.assertIn('/static/js/app.js?v=3456', body)
        self.assertNotIn('<script src="/static/js/app.js"></script>', body)


class ReplyServerPublicPageRouteCompatibilityTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_login_and_register_get_routes_serve_public_pages(self):
        client = TestClient(reply_server.app)

        login_response = client.get("/login")
        register_response = client.get("/register")

        self.assertEqual(200, login_response.status_code)
        self.assertIn('id="loginForm"', login_response.text)
        self.assertEqual(200, register_response.status_code)
        self.assertIn('id="registerForm"', register_response.text)


class ReplyServerNotificationDeliverySignatureTest(_ReplyServerModuleBindingMixin, unittest.IsolatedAsyncioTestCase):
    async def test_notification_template_test_route_uses_module_bound_database_and_correct_feishu_signature(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {
                "id": 5,
                "name": "飞书渠道",
                "type": "feishu",
                "enabled": True,
                "config": '{"webhook_url": "https://example.invalid/feishu", "secret": "demo-secret"}',
            }
        ]
        shadow_db = mock.Mock()
        shadow_db.get_notification_channels.side_effect = AssertionError("测试通知路由不该绕开 reply_server.db_manager")
        payload_capture = {}
        expected_timestamp = "1716800000"

        session_factory = lambda *args, **kwargs: _FakeAiohttpClientSession(  # noqa: E731
            payload_capture,
            client_session_kwargs=kwargs,
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "db_manager.db_manager",
            shadow_db,
        ), mock.patch(
            "aiohttp.ClientSession",
            side_effect=session_factory,
        ), mock.patch(
            "time.time",
            return_value=int(expected_timestamp),
        ):
            result = await reply_server.test_notification_template(
                reply_server.TestNotificationIn(
                    template_type="message",
                    template="通知给 {account_id}",
                ),
                current_user={"user_id": 7, "username": "scope-user"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["success_channels"], ["飞书渠道"])
        self.assertEqual(payload_capture["post_url"], "https://example.invalid/feishu")
        self.assertEqual(payload_capture["post_json"]["timestamp"], expected_timestamp)
        self.assertEqual(
            payload_capture["post_json"]["sign"],
            _expected_feishu_sign(expected_timestamp, "demo-secret"),
        )
        self.assertIn("通知给 测试账号", payload_capture["post_json"]["content"]["text"])
        fake_db.get_notification_channels.assert_called_once_with(7)

    async def test_notification_template_test_route_supports_qq_channel_with_configured_api_url(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {
                "id": 6,
                "name": "QQ渠道",
                "type": "qq",
                "enabled": True,
                "config": '{"qq_number": "123456", "api_url": "https://example.invalid/sendPrivateMsg"}',
            }
        ]
        payload_capture = {}

        session_factory = lambda *args, **kwargs: _FakeAiohttpClientSession(  # noqa: E731
            payload_capture,
            client_session_kwargs=kwargs,
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "aiohttp.ClientSession",
            side_effect=session_factory,
        ):
            result = await reply_server.test_notification_template(
                reply_server.TestNotificationIn(
                    template_type="message",
                    template="通知给 {account_id}",
                ),
                current_user={"user_id": 7, "username": "scope-user"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["success_channels"], ["QQ渠道"])
        self.assertEqual(payload_capture["get_url"], "https://example.invalid/sendPrivateMsg")
        self.assertEqual(payload_capture["get_params"]["qq"], "123456")
        self.assertIn("通知给 测试账号", payload_capture["get_params"]["msg"])
        fake_db.get_notification_channels.assert_called_once_with(7)
        fake_db.get_system_setting.assert_not_called()

    async def test_notification_template_test_route_supports_qq_channel_via_env_api_url_fallback(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {
                "id": 16,
                "name": "QQ渠道",
                "type": "qq",
                "enabled": True,
                "config": '{"qq_number": "123456"}',
            }
        ]
        payload_capture = {}

        session_factory = lambda *args, **kwargs: _FakeAiohttpClientSession(  # noqa: E731
            payload_capture,
            client_session_kwargs=kwargs,
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "aiohttp.ClientSession",
            side_effect=session_factory,
        ), mock.patch.dict(
            os.environ,
            {"QQ_NOTIFICATION_API_URL": "https://example.invalid/env-sendPrivateMsg"},
            clear=False,
        ):
            result = await reply_server.test_notification_template(
                reply_server.TestNotificationIn(
                    template_type="message",
                    template="通知给 {account_id}",
                ),
                current_user={"user_id": 7, "username": "scope-user"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["success_channels"], ["QQ渠道"])
        self.assertEqual(payload_capture["get_url"], "https://example.invalid/env-sendPrivateMsg")
        self.assertEqual(payload_capture["get_params"]["qq"], "123456")
        self.assertIn("通知给 测试账号", payload_capture["get_params"]["msg"])
        fake_db.get_notification_channels.assert_called_once_with(7)
        fake_db.get_system_setting.assert_not_called()

    async def test_notification_template_test_route_uses_dingtalk_signature_when_secret_is_configured(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {
                "id": 7,
                "name": "钉钉渠道",
                "type": "dingtalk",
                "enabled": True,
                "config": '{"webhook_url": "https://example.invalid/dingtalk?access_token=demo-token", "secret": "demo-secret"}',
            }
        ]
        payload_capture = {}
        expected_timestamp = "1716800000000"

        session_factory = lambda *args, **kwargs: _FakeAiohttpClientSession(  # noqa: E731
            payload_capture,
            client_session_kwargs=kwargs,
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "aiohttp.ClientSession",
            side_effect=session_factory,
        ), mock.patch(
            "time.time",
            return_value=1716800000,
        ):
            result = await reply_server.test_notification_template(
                reply_server.TestNotificationIn(
                    template_type="message",
                    template="通知给 {account_id}",
                ),
                current_user={"user_id": 7, "username": "scope-user"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["success_channels"], ["钉钉渠道"])
        self.assertIn("timestamp=1716800000000", payload_capture["post_url"])
        self.assertIn(
            f"sign={_expected_dingtalk_sign(expected_timestamp, 'demo-secret')}",
            payload_capture["post_url"],
        )
        self.assertEqual("markdown", payload_capture["post_json"]["msgtype"])
        self.assertIn("通知给 测试账号", payload_capture["post_json"]["markdown"]["text"])
        fake_db.get_notification_channels.assert_called_once_with(7)

    async def test_notification_template_test_route_honors_webhook_method_and_headers(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {
                "id": 8,
                "name": "Webhook渠道",
                "type": "webhook",
                "enabled": True,
                "config": '{"webhook_url": "https://example.invalid/hook", "http_method": "PUT", "headers": "{\\"Authorization\\": \\"Bearer demo-token\\"}"}',
            }
        ]
        payload_capture = {}

        session_factory = lambda *args, **kwargs: _FakeAiohttpClientSession(  # noqa: E731
            payload_capture,
            client_session_kwargs=kwargs,
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch(
            "aiohttp.ClientSession",
            side_effect=session_factory,
        ):
            result = await reply_server.test_notification_template(
                reply_server.TestNotificationIn(
                    template_type="delivery",
                    template="通知给 {account_id}",
                ),
                current_user={"user_id": 7, "username": "scope-user"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["success_channels"], ["Webhook渠道"])
        self.assertEqual(payload_capture["put_url"], "https://example.invalid/hook")
        self.assertEqual(payload_capture["put_headers"]["Authorization"], "Bearer demo-token")
        self.assertEqual(payload_capture["put_headers"]["Content-Type"], "application/json")
        self.assertEqual(payload_capture["put_json"]["title"], "测试通知")
        self.assertEqual(payload_capture["put_json"]["type"], "delivery")
        self.assertEqual(payload_capture["put_json"]["notification_type"], "delivery")
        self.assertEqual(payload_capture["put_json"]["source"], "xianyu-auto-reply")
        self.assertIn("通知给 测试账号", payload_capture["put_json"]["message"])
        fake_db.get_notification_channels.assert_called_once_with(7)

    async def test_notification_template_test_route_supports_email_channel_via_shared_dispatch_helper(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {
                "id": 9,
                "name": "邮件渠道",
                "type": "email",
                "enabled": True,
                "config": '{"smtp_server":"smtp.example.com","smtp_port":"587","email_user":"sender@example.com","email_password":"secret","recipient_email":"receiver@example.com"}',
            }
        ]

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "send_channel_notification",
            new=mock.AsyncMock(return_value=True),
        ) as send_channel_notification:
            result = await reply_server.test_notification_template(
                reply_server.TestNotificationIn(
                    template_type="message",
                    template="通知给 {account_id}",
                ),
                current_user={"user_id": 7, "username": "scope-user"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["success_channels"], ["邮件渠道"])
        send_channel_notification.assert_awaited_once_with(
            "email",
            {
                "smtp_server": "smtp.example.com",
                "smtp_port": "587",
                "email_user": "sender@example.com",
                "email_password": "secret",
                "recipient_email": "receiver@example.com",
            },
            "【测试通知】\n\n通知给 测试账号",
            title="测试通知",
            notification_type="message",
            account_id="测试账号",
        )
        fake_db.get_notification_channels.assert_called_once_with(7)

    async def test_notification_template_test_route_supports_bark_channel_via_shared_dispatch_helper(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {
                "id": 10,
                "name": "Bark渠道",
                "type": "bark",
                "enabled": True,
                "config": '{"device_key":"demo-device","server_url":"https://example.invalid/bark","sound":"bell"}',
            }
        ]

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "send_channel_notification",
            new=mock.AsyncMock(return_value=True),
        ) as send_channel_notification:
            result = await reply_server.test_notification_template(
                reply_server.TestNotificationIn(
                    template_type="message",
                    template="通知给 {account_id}",
                ),
                current_user={"user_id": 7, "username": "scope-user"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["success_channels"], ["Bark渠道"])
        send_channel_notification.assert_awaited_once_with(
            "bark",
            {
                "device_key": "demo-device",
                "server_url": "https://example.invalid/bark",
                "sound": "bell",
            },
            "【测试通知】\n\n通知给 测试账号",
            title="测试通知",
            notification_type="message",
            account_id="测试账号",
        )
        fake_db.get_notification_channels.assert_called_once_with(7)

    async def test_notification_template_test_route_supports_telegram_channel_via_shared_dispatch_helper(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {
                "id": 11,
                "name": "Telegram渠道",
                "type": "telegram",
                "enabled": True,
                "config": '{"bot_token":"demo-bot-token","chat_id":"123456"}',
            }
        ]

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "send_channel_notification",
            new=mock.AsyncMock(return_value=True),
        ) as send_channel_notification:
            result = await reply_server.test_notification_template(
                reply_server.TestNotificationIn(
                    template_type="message",
                    template="通知给 {account_id}",
                ),
                current_user={"user_id": 7, "username": "scope-user"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["success_channels"], ["Telegram渠道"])
        send_channel_notification.assert_awaited_once_with(
            "telegram",
            {
                "bot_token": "demo-bot-token",
                "chat_id": "123456",
            },
            "【测试通知】\n\n通知给 测试账号",
            title="测试通知",
            notification_type="message",
            account_id="测试账号",
        )
        fake_db.get_notification_channels.assert_called_once_with(7)

    async def test_notification_template_test_route_supports_tg_alias_via_shared_dispatch_helper(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {
                "id": 111,
                "name": "TG别名渠道",
                "type": "tg",
                "enabled": True,
                "config": '{"bot_token":"demo-bot-token","chat_id":"654321"}',
            }
        ]

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "send_channel_notification",
            new=mock.AsyncMock(return_value=True),
        ) as send_channel_notification:
            result = await reply_server.test_notification_template(
                reply_server.TestNotificationIn(
                    template_type="message",
                    template="通知给 {account_id}",
                ),
                current_user={"user_id": 7, "username": "scope-user"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["success_channels"], ["TG别名渠道"])
        send_channel_notification.assert_awaited_once_with(
            "tg",
            {
                "bot_token": "demo-bot-token",
                "chat_id": "654321",
            },
            "【测试通知】\n\n通知给 测试账号",
            title="测试通知",
            notification_type="message",
            account_id="测试账号",
        )
        fake_db.get_notification_channels.assert_called_once_with(7)

    async def test_notification_template_test_route_supports_weixin_alias_via_shared_dispatch_helper(self):
        fake_db = mock.Mock()
        fake_db.get_notification_channels.return_value = [
            {
                "id": 112,
                "name": "微信别名渠道",
                "type": "weixin",
                "enabled": True,
                "config": '{"webhook_url":"https://example.invalid/wechat-hook"}',
            }
        ]

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server,
            "send_channel_notification",
            new=mock.AsyncMock(return_value=True),
        ) as send_channel_notification:
            result = await reply_server.test_notification_template(
                reply_server.TestNotificationIn(
                    template_type="message",
                    template="通知给 {account_id}",
                ),
                current_user={"user_id": 7, "username": "scope-user"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["success_channels"], ["微信别名渠道"])
        send_channel_notification.assert_awaited_once_with(
            "weixin",
            {
                "webhook_url": "https://example.invalid/wechat-hook",
            },
            "【测试通知】\n\n通知给 测试账号",
            title="测试通知",
            notification_type="message",
            account_id="测试账号",
        )
        fake_db.get_notification_channels.assert_called_once_with(7)

    async def test_notification_dispatcher_feishu_signature_uses_secret_as_hmac_key(self):
        from utils import notification_dispatcher

        payload_capture = {}
        expected_timestamp = "1716800000"
        session_factory = lambda *args, **kwargs: _FakeAiohttpClientSession(  # noqa: E731
            payload_capture,
            client_session_kwargs=kwargs,
        )

        with mock.patch.object(
            notification_dispatcher.aiohttp,
            "ClientSession",
            side_effect=session_factory,
        ), mock.patch.object(
            notification_dispatcher.time,
            "time",
            return_value=int(expected_timestamp),
        ):
            result = await notification_dispatcher._send_feishu_notification(
                {"webhook_url": "https://example.invalid/feishu", "secret": "demo-secret"},
                "dispatcher message",
                account_id="acc-demo-1",
            )

        self.assertTrue(result)
        self.assertEqual(payload_capture["post_json"]["timestamp"], expected_timestamp)
        self.assertEqual(
            payload_capture["post_json"]["sign"],
            _expected_feishu_sign(expected_timestamp, "demo-secret"),
        )

    async def test_xianyu_live_feishu_signature_uses_secret_as_hmac_key(self):
        xianyu_async = importlib.import_module("XianyuAutoAsync")
        payload_capture = {}
        expected_timestamp = "1716800000"
        session_factory = lambda *args, **kwargs: _FakeAiohttpClientSession(  # noqa: E731
            payload_capture,
            client_session_kwargs=kwargs,
        )
        fake_live = SimpleNamespace(_safe_str=lambda value: str(value))

        with mock.patch(
            "aiohttp.ClientSession",
            side_effect=session_factory,
        ), mock.patch.object(
            xianyu_async.time,
            "time",
            return_value=int(expected_timestamp),
        ):
            result = await xianyu_async.XianyuLive._send_feishu_notification(
                fake_live,
                {"webhook_url": "https://example.invalid/feishu", "secret": "demo-secret"},
                "runtime message",
            )

        self.assertTrue(result)
        self.assertEqual(payload_capture["post_json"]["timestamp"], expected_timestamp)
        self.assertEqual(
            payload_capture["post_json"]["sign"],
            _expected_feishu_sign(expected_timestamp, "demo-secret"),
        )


class XianyuConversationHistoryParsingTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_all_conversations_uses_sender_nick_and_reminder_content_fallbacks(self):
        xianyu_async = importlib.import_module("XianyuAutoAsync")
        fake_websocket = _FakeAsyncWebsocketConnection(
            [
                json.dumps(
                    {
                        "lwp": "/s/vulcan",
                        "headers": {
                            "mid": "server-mid",
                            "sid": "server-sid",
                        },
                    }
                ),
                json.dumps(
                    {
                        "headers": {
                            "mid": "history-mid",
                            "sid": "server-sid",
                        },
                        "body": {
                            "hasMore": 0,
                            "nextCursor": 0,
                            "userMessageModels": [
                                {
                                    "message": {
                                        "extension": {
                                            "senderUserId": "buyer-1",
                                            "senderNick": "买家甲",
                                            "reminderContent": "你好，在吗？",
                                        },
                                        "content": {},
                                    }
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        fake_live = SimpleNamespace(
            account_id="acc-history-parse-1",
            _build_websocket_headers=mock.Mock(return_value={"Authorization": "Bearer demo"}),
            _create_websocket_connection=mock.AsyncMock(return_value=fake_websocket),
            init=mock.AsyncMock(),
            _safe_str=lambda value: str(value),
        )

        with mock.patch.object(xianyu_async, "generate_mid", return_value="history-mid"):
            result = await xianyu_async.XianyuLive.list_all_conversations(
                fake_live,
                "cid-history-1",
                page_size=20,
            )

        self.assertEqual(
            result,
            [
                {
                    "send_user_id": "buyer-1",
                    "send_user_name": "买家甲",
                    "message": "你好，在吗？",
                }
            ],
        )
        fake_live._build_websocket_headers.assert_called_once_with()
        fake_live._create_websocket_connection.assert_awaited_once_with({"Authorization": "Bearer demo"})
        fake_live.init.assert_awaited_once_with(fake_websocket)
        self.assertGreaterEqual(len(fake_websocket.sent_messages), 2)
        request_payload = json.loads(fake_websocket.sent_messages[1])
        self.assertEqual(request_payload["lwp"], "/r/MessageManager/listUserMessages")
        self.assertEqual(request_payload["body"][0], "cid-history-1@goofish")
        self.assertEqual(request_payload["body"][3], 20)


class ReplyServerAccountDeletionCleanupTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_purge_account_local_artifacts_removes_profile_and_face_verification_images(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_profile_dir = Path(temp_dir) / "browser_data" / "user_1"
            browser_profile_dir.mkdir(parents=True, exist_ok=True)
            (browser_profile_dir / "Preferences").write_text("{}", encoding="utf-8")

            static_root = Path(temp_dir) / "static"
            screenshots_dir = static_root / "uploads" / "images"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            screenshot_jpg = screenshots_dir / "face_verify_1_test.jpg"
            screenshot_png = screenshots_dir / "face_verify_1_test.png"
            screenshot_jpg.write_bytes(b"jpg")
            screenshot_png.write_bytes(b"png")

            with mock.patch.object(reply_server, "static_dir", str(static_root)), \
                 mock.patch.object(reply_server, "account_browser_runtime_manager", SimpleNamespace(base_dir=temp_dir)), \
                 mock.patch.object(reply_server, "log_with_user"):
                result = reply_server._purge_account_local_artifacts(
                    "1",
                    current_user={"user_id": 1, "username": "admin"},
                )

            self.assertEqual(result["deleted_face_verification_screenshots"], 2)
            self.assertTrue(result["browser_profile_deleted"])
            self.assertFalse(browser_profile_dir.exists())
            self.assertFalse(screenshot_jpg.exists())
            self.assertFalse(screenshot_png.exists())

    def test_remove_cookie_route_also_purges_account_local_artifacts(self):
        fake_manager = SimpleNamespace(remove_cookie=mock.Mock())
        current_user = {"user_id": 1, "username": "admin"}

        with mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(reply_server, "_ensure_account_access", return_value="1"), \
             mock.patch.object(
                 reply_server,
                 "_purge_account_local_artifacts",
                 return_value={"browser_profile_deleted": True},
             ) as purge_mock:
            result = reply_server.remove_cookie("1", current_user=current_user)

        fake_manager.remove_cookie.assert_called_once_with("1")
        purge_mock.assert_called_once_with("1", current_user=current_user)
        self.assertEqual(result["msg"], "removed")
        self.assertTrue(result["artifacts"]["browser_profile_deleted"])

    def test_remove_cookie_route_deletes_database_when_cookie_manager_unready(self):
        current_user = {"user_id": 1, "username": "admin"}
        fake_db = mock.Mock()
        fake_db.delete_cookie.return_value = True

        with mock.patch.object(reply_server.cookie_manager, "manager", None), \
             mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(reply_server, "_ensure_account_access", return_value="1"), \
             mock.patch.object(
                 reply_server,
                 "_purge_account_local_artifacts",
                 return_value={"browser_profile_deleted": True},
             ) as purge_mock:
            result = reply_server.remove_cookie("1", current_user=current_user)

        fake_db.delete_cookie.assert_called_once_with("1")
        purge_mock.assert_called_once_with("1", current_user=current_user)
        self.assertEqual(result["msg"], "removed")
        self.assertTrue(result["artifacts"]["browser_profile_deleted"])


class ReplyServerBackupManagementTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
    def test_list_backup_files_reads_from_active_database_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as isolated_cwd:
            original_cwd = os.getcwd()
            try:
                os.chdir(isolated_cwd)

                backup_dir = Path(temp_dir) / "custom-db-dir"
                backup_dir.mkdir(parents=True, exist_ok=True)
                db_path = backup_dir / "custom.db"
                db_path.write_bytes(b"")
                backup_path = backup_dir / "xianyu_data_backup_20260526_223500.db"
                backup_path.write_bytes(b"backup")

                db_manager_module = sys.modules["db_manager"]
                fake_db_manager = SimpleNamespace(db_path=str(db_path))
                shadow_dir = Path(temp_dir) / "shadow-db-dir"
                shadow_dir.mkdir(parents=True, exist_ok=True)
                shadow_db_path = shadow_dir / "shadow.db"
                shadow_db_path.write_bytes(b"")
                shadow_db_manager = SimpleNamespace(db_path=str(shadow_db_path))

                with mock.patch.object(reply_server, "db_manager", fake_db_manager), \
                     mock.patch.object(db_manager_module, "db_manager", shadow_db_manager), \
                     mock.patch.object(reply_server, "log_with_user"):
                    result = reply_server.list_backup_files(
                        admin_user={"user_id": 1, "username": "admin"},
                    )
            finally:
                os.chdir(original_cwd)

        self.assertEqual(1, result["total"])
        self.assertEqual(backup_path.name, result["backups"][0]["filename"])

    def test_export_backup_uses_module_bound_database_manager_for_current_user_scope(self):
        fake_db = mock.Mock()
        fake_db.export_backup.return_value = {"accounts": ["acc-1"]}
        shadow_db = mock.Mock()
        shadow_db.export_backup.side_effect = AssertionError("导出备份不该绕开 reply_server.db_manager")
        db_manager_module = sys.modules["db_manager"]

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(db_manager_module, "db_manager", shadow_db):
            response = reply_server.export_backup(
                current_user={"user_id": 7, "username": "scope-user"},
            )

        fake_db.export_backup.assert_called_once_with(7)
        self.assertTrue(
            response.headers["Content-Disposition"].startswith(
                "attachment; filename=xianyu_backup_scope-user_"
            )
        )
        self.assertEqual(b'{"accounts":["acc-1"]}', response.body)

    def test_import_backup_uses_module_bound_database_manager_for_current_user_scope(self):
        fake_db = mock.Mock()
        fake_db.import_backup.return_value = True
        shadow_db = mock.Mock()
        shadow_db.import_backup.side_effect = AssertionError("导入备份不该绕开 reply_server.db_manager")
        db_manager_module = sys.modules["db_manager"]
        backup_file = SimpleNamespace(
            filename="backup.json",
            file=SimpleNamespace(read=mock.Mock(return_value=b'{"demo": true}')),
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), \
             mock.patch.object(db_manager_module, "db_manager", shadow_db), \
             mock.patch.object(reply_server.cookie_manager, "manager", None):
            result = reply_server.import_backup(
                file=backup_file,
                current_user={"user_id": 7, "username": "scope-user"},
            )

        fake_db.import_backup.assert_called_once_with({"demo": True}, 7)
        self.assertEqual({"message": "备份导入成功"}, result)

    def test_import_backup_stops_masking_database_failures_as_bad_request(self):
        fake_db = mock.Mock()
        fake_db.import_backup.side_effect = RuntimeError("backup import exploded")
        backup_file = SimpleNamespace(
            filename="backup.json",
            file=SimpleNamespace(read=mock.Mock(return_value=b'{"demo": true}')),
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server.cookie_manager,
            "manager",
            None,
        ):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.import_backup(
                    file=backup_file,
                    current_user={"user_id": 7, "username": "scope-user"},
                )

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("导入备份失败: backup import exploded", raised.exception.detail)
        fake_db.import_backup.assert_called_once_with({"demo": True}, 7)

    def test_import_backup_reports_cookie_manager_reload_failure_as_server_error(self):
        fake_db = mock.Mock()
        fake_db.import_backup.return_value = True
        fake_cookie_manager = mock.Mock()
        fake_cookie_manager.reload_from_db.side_effect = RuntimeError("reload exploded")
        backup_file = SimpleNamespace(
            filename="backup.json",
            file=SimpleNamespace(read=mock.Mock(return_value=b'{"demo": true}')),
        )

        with mock.patch.object(reply_server, "db_manager", fake_db), mock.patch.object(
            reply_server.cookie_manager,
            "manager",
            fake_cookie_manager,
        ):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.import_backup(
                    file=backup_file,
                    current_user={"user_id": 7, "username": "scope-user"},
                )

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("备份导入成功，但刷新 CookieManager 缓存失败，请重启系统", raised.exception.detail)
        fake_db.import_backup.assert_called_once_with({"demo": True}, 7)
        fake_cookie_manager.reload_from_db.assert_called_once_with()

    def test_upload_database_backup_rollback_preserves_active_database_path(self):
        import sqlite3

        class FakeDbManager:
            def __init__(self, db_path=None):
                resolved_path = db_path if db_path is not None else os.getenv("DB_PATH", "data/xianyu_data.db")
                if not hasattr(self, "reinit_calls"):
                    self.reinit_calls = []
                    self.conn_close_count = 0
                    self.get_all_users_calls = 0
                self.reinit_calls.append(resolved_path)
                self.db_path = resolved_path
                self.conn = SimpleNamespace(close=self._close_conn)

            def _close_conn(self):
                self.conn_close_count += 1
                self.conn = None

            def get_all_users(self):
                self.get_all_users_calls += 1
                raise RuntimeError("restored database validation failed")

        def fake_move(src, dst):
            shutil.copy2(src, dst)
            os.remove(src)
            return dst

        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)

                current_db_dir = Path(temp_dir) / "custom-db-dir"
                current_db_dir.mkdir(parents=True, exist_ok=True)
                current_db_path = current_db_dir / "custom.db"
                original_db_bytes = b"original-db-content"
                current_db_path.write_bytes(original_db_bytes)

                valid_backup_source = Path(temp_dir) / "valid_backup_source.db"
                conn = sqlite3.connect(valid_backup_source)
                try:
                    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
                    conn.execute("CREATE TABLE cookies (id INTEGER PRIMARY KEY)")
                    conn.commit()
                finally:
                    conn.close()

                backup_file = SimpleNamespace(
                    filename="restore.db",
                    read=mock.AsyncMock(return_value=valid_backup_source.read_bytes()),
                )

                fake_db = FakeDbManager(str(current_db_path))
                shadow_db = SimpleNamespace(
                    db_path=str(Path(temp_dir) / "shadow.db"),
                    conn=None,
                    get_all_users=mock.Mock(side_effect=AssertionError("数据库恢复不该绕开 reply_server.db_manager")),
                    __init__=mock.Mock(side_effect=AssertionError("数据库恢复重连不该绕开 reply_server.db_manager")),
                )
                db_manager_module = sys.modules["db_manager"]

                with mock.patch.object(reply_server, "db_manager", fake_db), \
                     mock.patch.object(db_manager_module, "db_manager", shadow_db), \
                     mock.patch("shutil.move", side_effect=fake_move), \
                     mock.patch.object(reply_server, "log_with_user"):
                    with self.assertRaises(reply_server.HTTPException) as raised:
                        asyncio.run(
                            reply_server.upload_database_backup(
                                admin_user={"user_id": 1, "username": "admin"},
                                backup_file=backup_file,
                            )
                        )

                self.assertEqual(500, raised.exception.status_code)
                self.assertEqual("数据库恢复失败，已回滚到原数据库", raised.exception.detail)
                self.assertEqual(
                    [str(current_db_path), str(current_db_path)],
                    fake_db.reinit_calls[1:],
                )
                self.assertEqual(1, fake_db.conn_close_count)
                self.assertEqual(1, fake_db.get_all_users_calls)
                self.assertEqual(original_db_bytes, current_db_path.read_bytes())
            finally:
                os.chdir(original_cwd)

    def test_upload_database_backup_refreshes_cookie_manager_cache_after_successful_restore(self):
        import asyncio
        import sqlite3

        class FakeDbManager:
            def __init__(self, db_path=None):
                resolved_path = db_path if db_path is not None else os.getenv("DB_PATH", "data/xianyu_data.db")
                if not hasattr(self, "reinit_calls"):
                    self.reinit_calls = []
                    self.conn_close_count = 0
                    self.get_all_users_calls = 0
                self.reinit_calls.append(resolved_path)
                self.db_path = resolved_path
                self.conn = SimpleNamespace(close=self._close_conn)

            def _close_conn(self):
                self.conn_close_count += 1
                self.conn = None

            def get_all_users(self):
                self.get_all_users_calls += 1
                return [{"id": 1}, {"id": 2}]

        def fake_move(src, dst):
            shutil.copy2(src, dst)
            os.remove(src)
            return dst

        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)

                current_db_dir = Path(temp_dir) / "custom-db-dir"
                current_db_dir.mkdir(parents=True, exist_ok=True)
                current_db_path = current_db_dir / "custom.db"
                current_db_path.write_bytes(b"original-db-content")

                valid_backup_source = Path(temp_dir) / "valid_backup_source.db"
                conn = sqlite3.connect(valid_backup_source)
                try:
                    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
                    conn.execute("CREATE TABLE cookies (id INTEGER PRIMARY KEY)")
                    conn.commit()
                finally:
                    conn.close()

                backup_file = SimpleNamespace(
                    filename="restore.db",
                    read=mock.AsyncMock(return_value=valid_backup_source.read_bytes()),
                )

                fake_db = FakeDbManager(str(current_db_path))
                shadow_db = SimpleNamespace(
                    db_path=str(Path(temp_dir) / "shadow.db"),
                    conn=None,
                    get_all_users=mock.Mock(side_effect=AssertionError("数据库恢复不该绕开 reply_server.db_manager")),
                    __init__=mock.Mock(side_effect=AssertionError("数据库恢复重连不该绕开 reply_server.db_manager")),
                )
                fake_cookie_manager = mock.Mock()
                db_manager_module = sys.modules["db_manager"]

                with mock.patch.object(reply_server, "db_manager", fake_db), \
                     mock.patch.object(db_manager_module, "db_manager", shadow_db), \
                     mock.patch("shutil.move", side_effect=fake_move), \
                     mock.patch.object(reply_server.cookie_manager, "manager", fake_cookie_manager), \
                     mock.patch.object(reply_server, "log_with_user"):
                    result = asyncio.run(
                        reply_server.upload_database_backup(
                            admin_user={"user_id": 1, "username": "admin"},
                            backup_file=backup_file,
                        )
                    )

                self.assertTrue(result["success"])
                self.assertEqual("数据库恢复成功", result["message"])
                self.assertEqual(2, result["user_count"])
                self.assertTrue(Path(result["backup_file"]).exists())
                self.assertEqual([str(current_db_path)], fake_db.reinit_calls[1:])
                self.assertEqual(1, fake_db.conn_close_count)
                self.assertEqual(1, fake_db.get_all_users_calls)
                fake_cookie_manager.reload_from_db.assert_called_once_with()
            finally:
                os.chdir(original_cwd)

    def test_upload_database_backup_stops_reporting_success_when_cookie_manager_reload_fails(self):
        import asyncio
        import sqlite3

        class FakeDbManager:
            def __init__(self, db_path=None):
                resolved_path = db_path if db_path is not None else os.getenv("DB_PATH", "data/xianyu_data.db")
                if not hasattr(self, "reinit_calls"):
                    self.reinit_calls = []
                    self.conn_close_count = 0
                    self.get_all_users_calls = 0
                self.reinit_calls.append(resolved_path)
                self.db_path = resolved_path
                self.conn = SimpleNamespace(close=self._close_conn)

            def _close_conn(self):
                self.conn_close_count += 1
                self.conn = None

            def get_all_users(self):
                self.get_all_users_calls += 1
                return [{"id": 1}, {"id": 2}]

        def fake_move(src, dst):
            shutil.copy2(src, dst)
            os.remove(src)
            return dst

        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)

                current_db_dir = Path(temp_dir) / "custom-db-dir"
                current_db_dir.mkdir(parents=True, exist_ok=True)
                current_db_path = current_db_dir / "custom.db"
                current_db_path.write_bytes(b"original-db-content")

                valid_backup_source = Path(temp_dir) / "valid_backup_source.db"
                conn = sqlite3.connect(valid_backup_source)
                try:
                    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
                    conn.execute("CREATE TABLE cookies (id INTEGER PRIMARY KEY)")
                    conn.commit()
                finally:
                    conn.close()

                backup_file = SimpleNamespace(
                    filename="restore.db",
                    read=mock.AsyncMock(return_value=valid_backup_source.read_bytes()),
                )

                fake_db = FakeDbManager(str(current_db_path))
                shadow_db = SimpleNamespace(
                    db_path=str(Path(temp_dir) / "shadow.db"),
                    conn=None,
                    get_all_users=mock.Mock(side_effect=AssertionError("数据库恢复不该绕开 reply_server.db_manager")),
                    __init__=mock.Mock(side_effect=AssertionError("数据库恢复重连不该绕开 reply_server.db_manager")),
                )
                fake_cookie_manager = mock.Mock()
                fake_cookie_manager.reload_from_db.side_effect = RuntimeError("reload exploded")
                db_manager_module = sys.modules["db_manager"]

                with mock.patch.object(reply_server, "db_manager", fake_db), \
                     mock.patch.object(db_manager_module, "db_manager", shadow_db), \
                     mock.patch("shutil.move", side_effect=fake_move), \
                     mock.patch.object(reply_server.cookie_manager, "manager", fake_cookie_manager), \
                     mock.patch.object(reply_server, "log_with_user"):
                    with self.assertRaises(reply_server.HTTPException) as raised:
                        asyncio.run(
                            reply_server.upload_database_backup(
                                admin_user={"user_id": 1, "username": "admin"},
                                backup_file=backup_file,
                            )
                        )

                self.assertEqual(500, raised.exception.status_code)
                self.assertEqual("数据库恢复成功，但刷新 CookieManager 缓存失败，请重启系统", raised.exception.detail)
                self.assertEqual([str(current_db_path)], fake_db.reinit_calls[1:])
                self.assertEqual(1, fake_db.conn_close_count)
                self.assertEqual(1, fake_db.get_all_users_calls)
                fake_cookie_manager.reload_from_db.assert_called_once_with()
            finally:
                os.chdir(original_cwd)

    def test_upload_database_backup_reconciles_sessions_with_restored_users_before_cookie_reload(self):
        import asyncio
        import sqlite3

        class FakeDbManager:
            def __init__(self, db_path=None):
                resolved_path = db_path if db_path is not None else os.getenv("DB_PATH", "data/xianyu_data.db")
                if not hasattr(self, "reinit_calls"):
                    self.reinit_calls = []
                    self.conn_close_count = 0
                    self.get_all_users_calls = 0
                self.reinit_calls.append(resolved_path)
                self.db_path = resolved_path
                self.conn = SimpleNamespace(close=self._close_conn)

            def _close_conn(self):
                self.conn_close_count += 1
                self.conn = None

            def get_all_users(self):
                self.get_all_users_calls += 1
                return [
                    {"id": 1, "username": "restored-admin", "is_admin": True},
                    {"id": 2, "username": "restored-user", "is_admin": False},
                ]

        def fake_move(src, dst):
            shutil.copy2(src, dst)
            os.remove(src)
            return dst

        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = os.getcwd()
            original_session_tokens = dict(reply_server.SESSION_TOKENS)
            try:
                os.chdir(temp_dir)

                current_db_dir = Path(temp_dir) / "custom-db-dir"
                current_db_dir.mkdir(parents=True, exist_ok=True)
                current_db_path = current_db_dir / "custom.db"
                current_db_path.write_bytes(b"original-db-content")

                valid_backup_source = Path(temp_dir) / "valid_backup_source.db"
                conn = sqlite3.connect(valid_backup_source)
                try:
                    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
                    conn.execute("CREATE TABLE cookies (id INTEGER PRIMARY KEY)")
                    conn.commit()
                finally:
                    conn.close()

                backup_file = SimpleNamespace(
                    filename="restore.db",
                    read=mock.AsyncMock(return_value=valid_backup_source.read_bytes()),
                )
                fake_db = FakeDbManager(str(current_db_path))
                shadow_db = mock.Mock()
                fake_cookie_manager = mock.Mock()
                reply_server.SESSION_TOKENS.clear()
                reply_server.SESSION_TOKENS.update(
                    {
                        "token-admin": {"user_id": 1, "is_admin": False},
                        "token-user": {"user_id": 2, "is_admin": True},
                        "token-stale": {"user_id": 999, "is_admin": False},
                    }
                )
                db_manager_module = sys.modules["db_manager"]

                with mock.patch.object(reply_server, "db_manager", fake_db), \
                     mock.patch.object(db_manager_module, "db_manager", shadow_db), \
                     mock.patch("shutil.move", side_effect=fake_move), \
                     mock.patch.object(reply_server.cookie_manager, "manager", fake_cookie_manager), \
                     mock.patch.object(reply_server, "log_with_user"):
                    result = asyncio.run(
                        reply_server.upload_database_backup(
                            admin_user={"user_id": 1, "username": "admin"},
                            backup_file=backup_file,
                        )
                    )

                self.assertTrue(result["success"])
                self.assertEqual(2, result["user_count"])
                self.assertEqual({"user_id": 1, "is_admin": True}, reply_server.SESSION_TOKENS["token-admin"])
                self.assertEqual({"user_id": 2, "is_admin": False}, reply_server.SESSION_TOKENS["token-user"])
                self.assertNotIn("token-stale", reply_server.SESSION_TOKENS)
                fake_cookie_manager.reload_from_db.assert_called_once_with()
            finally:
                reply_server.SESSION_TOKENS.clear()
                reply_server.SESSION_TOKENS.update(original_session_tokens)
                os.chdir(original_cwd)

    def test_upload_database_backup_cleans_temp_file_when_validation_raises_http_error(self):
        import asyncio
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)

                invalid_backup_path = Path(temp_dir) / "invalid_backup.db"
                conn = sqlite3.connect(invalid_backup_path)
                try:
                    conn.execute("CREATE TABLE demo_only (id INTEGER PRIMARY KEY)")
                    conn.commit()
                finally:
                    conn.close()

                backup_file = SimpleNamespace(
                    filename="INVALID.DB",
                    read=mock.AsyncMock(return_value=invalid_backup_path.read_bytes()),
                )

                with mock.patch.object(reply_server, "log_with_user"):
                    with self.assertRaises(reply_server.HTTPException) as raised:
                        asyncio.run(
                            reply_server.upload_database_backup(
                                admin_user={"user_id": 1, "username": "admin"},
                                backup_file=backup_file,
                            )
                        )
            finally:
                os.chdir(original_cwd)

        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual([], list(Path(temp_dir).glob("temp_backup_*.db")))

    def test_upload_database_backup_uses_unique_named_temp_file_for_restore_source(self):
        import asyncio

        backup_file = SimpleNamespace(
            filename="restore.db",
            read=mock.AsyncMock(return_value=b"not-a-real-sqlite-db"),
        )
        fake_temp_file = SimpleNamespace(name="temp_backup_unique_case.db", close=mock.Mock())

        with mock.patch.object(reply_server, "log_with_user"), \
             mock.patch("tempfile.NamedTemporaryFile", return_value=fake_temp_file) as named_temp_file, \
             mock.patch("os.path.exists", return_value=False):
            with self.assertRaises(reply_server.HTTPException) as raised:
                asyncio.run(
                    reply_server.upload_database_backup(
                        admin_user={"user_id": 1, "username": "admin", "is_admin": True},
                        backup_file=backup_file,
                    )
                )

        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("无效的数据库文件", raised.exception.detail)
        named_temp_file.assert_called_once_with(
            prefix="temp_backup_",
            suffix=".db",
            delete=False,
        )
        fake_temp_file.close.assert_called_once_with()

    def test_import_backup_preserves_http_validation_status_code_for_bad_extension(self):
        file_reader = mock.Mock(return_value=b"{}")
        invalid_file = SimpleNamespace(
            filename="backup.txt",
            file=SimpleNamespace(read=file_reader),
        )

        with self.assertRaises(reply_server.HTTPException) as raised:
            reply_server.import_backup(
                file=invalid_file,
                current_user={"user_id": 1, "username": "tester"},
            )

        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("只支持JSON格式的备份文件", raised.exception.detail)
        file_reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
