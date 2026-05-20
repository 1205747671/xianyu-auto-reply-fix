import asyncio
import concurrent.futures
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
import types
import unittest
from unittest import mock

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


class ReplyServerAccountScopeContractTest(_ReplyServerModuleBindingMixin, unittest.TestCase):
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
        self.assertIn("'account_id': account_id,\n                    'user_id': user_id,", source)
        self.assertIn("'account_id': account_id,\n            'value': mask_cookie_value(cookie_value),", source)
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
            "get_all_cookies",
            side_effect=AssertionError("invalid account_id should be rejected before cookie lookup"),
        ) as get_all_cookies, mock.patch.object(reply_server, "log_with_user"):
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

        get_all_cookies.assert_not_called()

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
        self.assertIn('@app.delete(\'/message-notifications/account/{account_id}\')', source)
        self.assertIn('@app.get("/keywords/{account_id}")', source)
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

        self.assertIn("db_manager.get_orders_by_account(account_id, limit=1000)", source)
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
        self.assertIn("document.getElementById('editReplyAccountIdSelect').value = data.account_id;", app_js)
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

    async def test_check_valid_accounts_scopes_counts_to_current_user(self):
        fake_manager = SimpleNamespace(
            get_cookie_status=mock.Mock(side_effect=lambda account_id: account_id == "acc-owned-valid"),
        )

        with mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(
                 reply_server.db_manager,
                 "get_all_cookies",
                 return_value={
                     "acc-owned-valid": "x" * 51,
                     "acc-owned-disabled": "y" * 51,
                 },
             ) as get_all_cookies:
            response = await reply_server.check_valid_accounts(
                current_user={"user_id": 321, "username": "owner"},
            )

        self.assertTrue(response["success"])
        self.assertTrue(response["hasValidAccounts"])
        self.assertEqual(response["validAccountCount"], 1)
        self.assertEqual(response["enabledAccountCount"], 1)
        self.assertEqual(response["totalAccountCount"], 2)
        get_all_cookies.assert_called_once_with(321)
        fake_manager.get_cookie_status.assert_has_calls(
            [mock.call("acc-owned-valid"), mock.call("acc-owned-disabled")]
        )

    async def test_check_valid_accounts_without_current_user_returns_empty_counts(self):
        fake_manager = SimpleNamespace(get_cookie_status=mock.Mock())

        with mock.patch.object(reply_server.cookie_manager, "manager", fake_manager), \
             mock.patch.object(
                 reply_server.db_manager,
                 "get_all_cookies",
                 side_effect=AssertionError("anonymous accounts check must not read global cookies"),
             ):
            response = await reply_server.check_valid_accounts(current_user=None)

        self.assertTrue(response["success"])
        self.assertFalse(response["hasValidAccounts"])
        self.assertEqual(response["validAccountCount"], 0)
        self.assertEqual(response["enabledAccountCount"], 0)
        self.assertEqual(response["totalAccountCount"], 0)
        fake_manager.get_cookie_status.assert_not_called()

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
             mock.patch("db_manager.db_manager", fake_db), \
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
             mock.patch("db_manager.db_manager", fake_db), \
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
            with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"acc-history-1": "cookie-value"}), \
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
        fetcher_cls.assert_called_once_with("cookie-value", account_id="acc-history-1", headless=True)
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
            with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"acc-history-list-1": "cookie-value"}), \
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
        fetcher_cls.assert_called_once_with("cookie-value", account_id="acc-history-list-1", headless=True)
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
            with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"acc-history-list-2": "cookie-value"}), \
                 mock.patch("utils.order_history_sync.OrderHistoryPageFetcher", return_value=fake_fetcher), \
                 mock.patch.object(reply_server, "_run_managed_live_instance_call", managed_call), \
                 mock.patch.object(reply_server, "_save_history_order_candidate", return_value=True) as save_candidate_mock, \
                 mock.patch.object(reply_server, "_cleanup_order_history_sync_jobs"):
                await reply_server._run_order_history_sync_job(job_id)
        finally:
            reply_server.order_history_sync_jobs.pop(job_id, None)
            reply_server.order_history_sync_tasks.pop(job_id, None)

        self.assertEqual(job["status"], "completed")
        managed_call.assert_awaited_once()
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
            with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"acc-history-2": "cookie-value"}), \
                 mock.patch("utils.order_history_sync.OrderHistoryPageFetcher", return_value=fake_fetcher), \
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
            with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"acc-history-owned": "cookie-value"}), \
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
            with mock.patch.object(reply_server.db_manager, "get_all_cookies", return_value={"acc-history-3": "cookie-value"}), \
                 mock.patch("utils.order_history_sync.OrderHistoryPageFetcher", return_value=fake_fetcher), \
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
        self.assertIn('data-account-id="${accountId}"', app_js)
        self.assertIn("actionButton.dataset.accountId", app_js)
        self.assertIn("showOrderDetail(orderId, accountId)", app_js)
        self.assertIn("allOrdersData.find(o => o.order_id === orderId && String(o.account_id || '').trim() === normalizedAccountId)", app_js)
        self.assertIn("params.set('account_id', normalizedAccountId)", app_js)
        self.assertIn("accountId: cb.dataset.accountId", app_js)

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

        self.assertIn("fetch(`${apiBase}/ai-reply-settings/${accountId}`", app_js)
        self.assertIn("fetch(`${apiBase}/ai-reply-test/${accountId}`", app_js)
        self.assertIn("fetch(`${apiBase}/qr-login/cooldown-status/${accountId}`", app_js)
        self.assertIn("fetch(`${apiBase}/qr-login/reset-cooldown/${accountId}`", app_js)
        self.assertIn("account_id: selectedAccountId", app_js)
        self.assertIn("document.getElementById('accountId')", app_js)

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
        self.assertIn("await loadAccountOptions('itemAccountFilter');", app_js)
        self.assertIn("await loadAccountOptions('itemReplayAccountFilter');", app_js)
        self.assertIn("await loadAccountOptions('editReplyAccountIdSelect', '选择账号');", app_js)
        self.assertIn("const selectedAccountId = document.getElementById('itemAccountFilter').value;", app_js)
        self.assertIn("const selectedAccountId = document.getElementById('itemReplayAccountFilter').value;", app_js)
        self.assertIn("fetch(`${apiBase}/accounts/details`", app_js)
        self.assertIn("fetchJSON(`${apiBase}/accounts/details`)", app_js)
        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodeURIComponent(normalizedAccountId)}/runtime-status`)", app_js)
        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodeURIComponent(accountId)}/session-keepalive`", app_js)
        self.assertIn("`${apiBase}/accounts/${encodeURIComponent(accountId)}/conversations/${encodeURIComponent(conversationId)}/history`", app_js)
        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodeURIComponent(id)}/details?include_secrets=true`)", app_js)
        self.assertIn("fetchJSON(`${apiBase}/accounts/${encodeURIComponent(accountId)}/details?include_secrets=true`)", app_js)
        self.assertIn("fetchJSON(apiBase + `/accounts/${accountId}/proxy?include_secret=true`)", app_js)
        self.assertIn("fetchJSON(apiBase + `/accounts/${id}/account-info`", app_js)
        self.assertIn("fetchJSON(apiBase + `/accounts/${id}/proxy`", app_js)
        self.assertIn("fetchJSON(apiBase + `/accounts/${id}`", app_js)
        self.assertIn("await fetchJSON(apiBase + `/accounts/${id}`, { method: 'DELETE' });", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${accountId}/status`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${accountId}/auto-confirm`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${accountId}/auto-comment`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${accountId}/comment-templates`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${currentCommentTemplateAccountId}/comment-templates`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${currentCommentTemplateAccountId}/comment-templates/${templateId}`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${accountId}/comment-templates/${templateId}`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${accountId}/comment-templates/${templateId}/activate`, {", app_js)
        self.assertIn("const accountsResponse = await fetch(`${apiBase}/accounts`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${accountId}/remark`, {", app_js)
        self.assertIn("const response = await fetch(`${apiBase}/accounts/${accountId}/pause-duration`, {", app_js)
        self.assertIn("const response = await fetch('/admin/accounts', {", app_js)
        self.assertIn("const accountsCheckResponse = await fetch('/accounts/check', {", app_js)
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
             mock.patch("db_manager.db_manager", fake_db):
            with self.assertRaises(reply_server.HTTPException) as raised:
                reply_server.delete_user_order(
                    "order-delete-1",
                    current_user={"user_id": 1, "username": "admin"},
                    account_id=None,
                )

        self.assertEqual(raised.exception.status_code, 400)
        fake_db.get_order_by_id.assert_not_called()

    def test_delete_user_order_rejects_unowned_account_id_before_db_lookup(self):
        fake_db = mock.Mock()

        with mock.patch.object(reply_server, "_get_user_cookies_map", return_value={"acc-order-1": "cookie"}), \
             mock.patch("db_manager.db_manager", fake_db):
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
             mock.patch("db_manager.db_manager", fake_db), \
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
             mock.patch("db_manager.db_manager", fake_db), \
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
             mock.patch("db_manager.db_manager", fake_db), \
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


class ReplyServerQrLoginSessionIsolationTest(_ReplyServerModuleBindingMixin, unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
