import ast
import asyncio
import base64
import json
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

_STUBBED_MODULE_NAMES = (
    "loguru",
    "websockets",
    "aiohttp",
    "blackboxprotobuf",
    "execjs",
    "cloakbrowser",
)
_IMPORTED_MODULE_NAMES = (
    "XianyuAutoAsync",
)
_MODULES_THAT_MUST_BE_REAL = (
    "db_manager",
    "utils.notification_dispatcher",
    "utils.browser_provider",
    "utils.order_detail_fetcher",
    "utils.account_browser_runtime",
    "utils.xianyu_utils",
    "XianyuAutoAsync",
)
_MODULE_SNAPSHOT = {
    name: sys.modules.get(name)
    for name in _STUBBED_MODULE_NAMES + _IMPORTED_MODULE_NAMES
}

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _purge_stubbed_project_module(module_name):
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


def _restore_module_snapshot():
    for name in _IMPORTED_MODULE_NAMES + _STUBBED_MODULE_NAMES:
        original = _MODULE_SNAPSHOT.get(name)
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


for _module_name in _MODULES_THAT_MUST_BE_REAL:
    _purge_stubbed_project_module(_module_name)

sys.modules.pop("XianyuAutoAsync", None)

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

import XianyuAutoAsync
from XianyuAutoAsync import XianyuLive

_PROJECT_MODULE_REFERENCES = {
    "XianyuAutoAsync": XianyuAutoAsync,
    "db_manager": sys.modules.get("db_manager"),
}
_PROJECT_DB_MANAGER_REF = getattr(XianyuAutoAsync, "db_manager", None)

# unittest discovery imports all test modules up front, so restore import-time
# stubs now instead of waiting until this module's tests finish running.
_restore_module_snapshot()


def tearDownModule():
    _restore_module_snapshot()


class XianyuAsyncBrowserRuntimeTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _reset_shared_runtime_state():
        for attr_name in (
            "_item_detail_cache",
            "_instances",
            "_last_password_login_time",
            "_password_login_failure_backoff",
            "_manual_refresh_state",
            "_auth_recovery_locks",
            "_auth_prewarmed_tokens",
            "_last_risk_log_cleanup_times",
            "_risk_log_cleanup_locks",
            "_init_auth_failure_state",
            "_qr_prewarmed_tokens",
            "_qr_login_grace_state",
            "_order_locks",
            "_lock_usage_times",
            "_lock_hold_info",
            "_order_detail_locks",
            "_order_detail_lock_times",
        ):
            state = getattr(XianyuLive, attr_name, None)
            if hasattr(state, "clear"):
                state.clear()

        pause_manager = getattr(XianyuAutoAsync, "pause_manager", None)
        paused_chats = getattr(pause_manager, "paused_chats", None)
        if hasattr(paused_chats, "clear"):
            paused_chats.clear()

    def setUp(self):
        # Discovery imports other test modules first, so re-bind the project
        # modules this file was authored against before each test starts.
        original_modules = {
            name: sys.modules.get(name)
            for name in _PROJECT_MODULE_REFERENCES
        }
        for name, module in _PROJECT_MODULE_REFERENCES.items():
            if module is not None:
                sys.modules[name] = module

        original_db_manager_ref = getattr(XianyuAutoAsync, "db_manager", None)
        if _PROJECT_DB_MANAGER_REF is not None:
            XianyuAutoAsync.db_manager = _PROJECT_DB_MANAGER_REF

        self._reset_shared_runtime_state()

        def _restore_module_binding():
            for name, original_module in original_modules.items():
                if original_module is None:
                    expected_module = _PROJECT_MODULE_REFERENCES.get(name)
                    if expected_module is not None and sys.modules.get(name) is expected_module:
                        sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original_module

            XianyuAutoAsync.db_manager = original_db_manager_ref

        self.addCleanup(_restore_module_binding)

    def test_prime_cookie_refresh_schedule_on_startup_seeds_baseline_once(self):
        live = XianyuLive(
            "unb=test-unb; cookie2=test-cookie2",
            account_id="1",
            register_instance=False,
        )

        with mock.patch("XianyuAutoAsync.time.time", return_value=1234.5):
            seeded = live._prime_cookie_refresh_schedule_on_startup()

        self.assertTrue(seeded)
        self.assertEqual(live.last_cookie_refresh_time, 1234.5)

        with mock.patch("XianyuAutoAsync.time.time", return_value=5678.9):
            seeded_again = live._prime_cookie_refresh_schedule_on_startup()

        self.assertFalse(seeded_again)
        self.assertEqual(live.last_cookie_refresh_time, 1234.5)

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

    @staticmethod
    def _build_sync_package(message):
        encoded = base64.b64encode(
            json.dumps(message, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        return {
            "headers": {
                "mid": "mid-test",
                "sid": "sid-test",
            },
            "body": {
                "syncPushPackage": {
                    "data": [
                        {
                            "data": encoded,
                        }
                    ]
                }
            },
        }

    @staticmethod
    def _build_cookie_manager_module(enabled=True):
        cookie_manager_module = types.ModuleType("cookie_manager")
        cookie_manager_module.manager = mock.Mock()
        cookie_manager_module.manager.get_cookie_status.return_value = enabled
        return cookie_manager_module

    @staticmethod
    def _build_runtime_lease(account_id: str, browser=None, context=None):
        runtime = types.SimpleNamespace(
            browser=browser,
            context=context,
            page=None,
            playwright=None,
        )
        lease = types.SimpleNamespace(
            account_id=account_id,
            runtime=runtime,
            pages=[],
            released=False,
        )
        return lease

    @staticmethod
    def _collect_logger_messages(mock_logger):
        messages = []
        for method_name in ("debug", "info", "warning", "error"):
            method = getattr(mock_logger, method_name, None)
            if method is None:
                continue
            for call in method.call_args_list:
                if call.args:
                    messages.append(str(call.args[0]))
        return "\n".join(messages)

    class _FakeAsyncResponse:
        def __init__(self, status, payload_text):
            self.status = status
            self._payload_text = payload_text

        async def text(self):
            return self._payload_text

    class _FakeAsyncPostContext:
        def __init__(self, response):
            self._response = response

        async def __aenter__(self):
            return self._response

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeAsyncSession:
        def __init__(self, response):
            self._response = response
            self.calls = []

        def post(self, url, data=None, timeout=None):
            self.calls.append(
                {
                    "url": url,
                    "data": data,
                    "timeout": timeout,
                }
            )
            return XianyuAsyncBrowserRuntimeTest._FakeAsyncPostContext(self._response)

    def test_legacy_launch_browser_safe_helper_removed(self):
        self.assertFalse(hasattr(XianyuAutoAsync, "_launch_browser_safe"))
        self.assertFalse(hasattr(XianyuAutoAsync, "_is_docker_env"))
        self.assertFalse(hasattr(XianyuAutoAsync, "_DockerEventLoopPolicy"))

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

    def test_init_uses_account_id_as_canonical_identity(self):
        with mock.patch.object(XianyuAutoAsync, "trans_cookies", return_value={"unb": "user-init"}), \
             mock.patch.object(XianyuAutoAsync, "generate_device_id", return_value="device-init"), \
             mock.patch.object(XianyuLive, "_load_proxy_config", return_value={"proxy_type": "none"}), \
             mock.patch.object(XianyuLive, "_init_order_status_handler", return_value=None), \
             mock.patch.object(XianyuLive, "pop_auth_prewarmed_token", return_value=None), \
             mock.patch.object(XianyuLive, "pop_qr_prewarmed_token", return_value=None):
            live = XianyuLive(
                "unb=user-init",
                account_id="acc-init-1",
                user_id=9,
                register_instance=False,
            )

        self.assertEqual(live.account_id, "acc-init-1")
        self.assertEqual(live.user_id, 9)

    def test_init_rejects_legacy_cookie_id_keyword(self):
        with mock.patch.object(XianyuAutoAsync, "trans_cookies", return_value={"unb": "user-init"}), \
             mock.patch.object(XianyuAutoAsync, "generate_device_id", return_value="device-init"), \
             mock.patch.object(XianyuLive, "_load_proxy_config", return_value={"proxy_type": "none"}), \
             mock.patch.object(XianyuLive, "_init_order_status_handler", return_value=None), \
             mock.patch.object(XianyuLive, "pop_auth_prewarmed_token", return_value=None), \
             mock.patch.object(XianyuLive, "pop_qr_prewarmed_token", return_value=None):
            with self.assertRaises(TypeError):
                XianyuLive(
                    "unb=user-init",
                    cookie_id="legacy-conflict",
                    account_id="acc-init-conflict-1",
                    register_instance=False,
                )

    def test_init_leaves_canonical_account_id_blank_when_not_provided(self):
        with mock.patch.object(XianyuAutoAsync, "trans_cookies", return_value={"unb": "user-init"}), \
             mock.patch.object(XianyuAutoAsync, "generate_device_id", return_value="device-init"), \
             mock.patch.object(XianyuLive, "_load_proxy_config", return_value={"proxy_type": "none"}), \
             mock.patch.object(XianyuLive, "_init_order_status_handler", return_value=None), \
             mock.patch.object(XianyuLive, "pop_auth_prewarmed_token", return_value=None), \
             mock.patch.object(XianyuLive, "pop_qr_prewarmed_token", return_value=None):
            live = XianyuLive(
                "unb=user-init",
                account_id="   ",
                user_id=10,
                register_instance=False,
            )

        self.assertEqual(live.account_id, "")
        self.assertEqual(live._canonical_account_id(), "")
        self.assertEqual(live._current_account_id(), "")
        self.assertEqual(live.user_id, 10)

    def test_init_does_not_consume_prewarmed_tokens_without_explicit_account_id(self):
        with mock.patch.object(XianyuAutoAsync, "trans_cookies", return_value={"unb": "user-init"}), \
             mock.patch.object(XianyuAutoAsync, "generate_device_id", return_value="device-init"), \
             mock.patch.object(XianyuLive, "_load_proxy_config", return_value={"proxy_type": "none"}), \
             mock.patch.object(XianyuLive, "_init_order_status_handler", return_value=None), \
             mock.patch.dict(XianyuLive._auth_prewarmed_tokens, {}, clear=True), \
             mock.patch.dict(XianyuLive._qr_prewarmed_tokens, {}, clear=True):
            XianyuLive.cache_auth_prewarmed_token(
                account_id="acc-init-fallback",
                token="auth-token-1",
                source="unit-test",
            )
            XianyuLive.cache_qr_prewarmed_token(
                account_id="acc-init-fallback",
                token="qr-token-1",
            )

            live = XianyuLive(
                "unb=user-init",
                account_id="   ",
                register_instance=False,
            )

            self.assertIsNone(live.current_token)
            self.assertIn("acc-init-fallback", XianyuLive._auth_prewarmed_tokens)
            self.assertIn("acc-init-fallback", XianyuLive._qr_prewarmed_tokens)

    def test_init_does_not_load_proxy_config_without_canonical_account_id(self):
        with mock.patch.object(XianyuAutoAsync, "trans_cookies", return_value={"unb": "user-init"}), \
             mock.patch.object(XianyuAutoAsync, "generate_device_id", return_value="device-init"), \
             mock.patch.object(XianyuLive, "_init_order_status_handler", return_value=None), \
             mock.patch.object(XianyuLive, "pop_auth_prewarmed_token", return_value=None), \
             mock.patch.object(XianyuLive, "pop_qr_prewarmed_token", return_value=None), \
             mock.patch.object(
                 XianyuAutoAsync.db_manager,
                 "get_cookie_proxy_config",
                 side_effect=AssertionError(
                     "should not load proxy config from legacy cookie_id alias without canonical account_id"
                 ),
              ) as get_cookie_proxy_config:
            live = XianyuLive(
                "unb=user-init",
                account_id="   ",
                register_instance=False,
            )

        self.assertEqual(live.proxy_config["proxy_type"], "none")
        get_cookie_proxy_config.assert_not_called()

    def test_init_rejects_register_instance_without_canonical_account_id(self):
        with mock.patch.object(XianyuAutoAsync, "trans_cookies", return_value={"unb": "user-init"}), \
             mock.patch.object(XianyuAutoAsync, "generate_device_id", return_value="device-init"), \
             mock.patch.object(XianyuLive, "_load_proxy_config", return_value={"proxy_type": "none"}), \
             mock.patch.object(XianyuLive, "_init_order_status_handler", return_value=None), \
             mock.patch.object(XianyuLive, "pop_auth_prewarmed_token", return_value=None), \
             mock.patch.object(XianyuLive, "pop_qr_prewarmed_token", return_value=None), \
             mock.patch.dict(XianyuLive._instances, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "account_id"):
                XianyuLive(
                    "unb=user-init",
                    register_instance=True,
                )

        self.assertNotIn("", XianyuLive._instances)

    def test_init_rejects_register_instance_with_legacy_cookie_id_keyword(self):
        with mock.patch.object(XianyuAutoAsync, "trans_cookies", return_value={"unb": "user-init"}), \
             mock.patch.object(XianyuAutoAsync, "generate_device_id", return_value="device-init"), \
             mock.patch.object(XianyuLive, "_load_proxy_config", return_value={"proxy_type": "none"}), \
             mock.patch.object(XianyuLive, "_init_order_status_handler", return_value=None), \
             mock.patch.object(XianyuLive, "pop_auth_prewarmed_token", return_value=None), \
             mock.patch.object(XianyuLive, "pop_qr_prewarmed_token", return_value=None), \
             mock.patch.dict(XianyuLive._instances, {}, clear=True):
            with self.assertRaises(TypeError):
                XianyuLive(
                    "unb=user-init",
                    cookie_id="legacy-managed-fallback",
                    account_id="   ",
                    register_instance=True,
                )

        self.assertNotIn("legacy-managed-fallback", XianyuLive._instances)

    def test_main_entry_reads_account_id_env_and_passes_it_to_xianyu_live(self):
        source_path = Path(__file__).resolve().parents[1] / "XianyuAutoAsync.py"
        source = source_path.read_text(encoding="utf-8")
        module_ast = ast.parse(source, filename=str(source_path))
        main_block = None

        for node in module_ast.body:
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "__main__"
            ):
                main_block = node.body
                break

        self.assertIsNotNone(main_block, "XianyuAutoAsync.py 缺少 __main__ 入口")

        compiled_main = compile(
            ast.Module(body=main_block, type_ignores=[]),
            filename=str(source_path),
            mode="exec",
        )
        getenv = mock.Mock(
            side_effect=lambda key: {
                "COOKIES_STR": "cookie-string-from-env",
                "ACCOUNT_ID": "account-from-env",
            }.get(key)
        )
        fake_os = types.SimpleNamespace(getenv=getenv)
        fake_asyncio = types.SimpleNamespace(run=mock.Mock())
        fake_live = mock.Mock()
        fake_live.main.return_value = "main-coro"
        fake_constructor = mock.Mock(return_value=fake_live)

        exec(
            compiled_main,
            {
                "__name__": "__main__",
                "os": fake_os,
                "asyncio": fake_asyncio,
                "XianyuLive": fake_constructor,
            },
        )

        getenv.assert_has_calls([mock.call("COOKIES_STR"), mock.call("ACCOUNT_ID")])
        fake_constructor.assert_called_once_with(
            "cookie-string-from-env",
            account_id="account-from-env",
        )
        fake_asyncio.run.assert_called_once_with("main-coro")

    def test_register_instance_rejects_blank_canonical_account_id_without_cookie_id_fallback(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-register-stale-1"
        live.account_id = "   "
        live._safe_str = str

        with mock.patch.dict(XianyuLive._instances, {}, clear=True):
            live._register_instance()
            self.assertNotIn("legacy-register-stale-1", XianyuLive._instances)

    def test_is_current_account_enabled_rejects_blank_canonical_account_id_without_cookie_id_fallback(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-enabled-stale-1"
        live.account_id = "   "
        live._safe_str = str

        cookie_manager_module = self._build_cookie_manager_module(enabled=False)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}):
            enabled = live._is_current_account_enabled()

        self.assertTrue(enabled)
        cookie_manager_module.manager.get_cookie_status.assert_not_called()

    def test_live_instance_no_longer_exposes_cookie_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "acc-no-cookie-alias-1"

        self.assertFalse(hasattr(XianyuLive, "cookie_id"))

        with self.assertRaises(AttributeError):
            _ = live.cookie_id

        live.cookie_id = "legacy-cookie-alias"
        self.assertEqual(live.cookie_id, "legacy-cookie-alias")
        self.assertEqual(live._current_account_id(), "acc-no-cookie-alias-1")

    def test_account_scoped_token_and_grace_helpers_require_account_id_contract(self):
        with mock.patch.dict(XianyuLive._auth_prewarmed_tokens, {}, clear=True), \
             mock.patch.dict(XianyuLive._qr_prewarmed_tokens, {}, clear=True), \
             mock.patch.dict(XianyuLive._qr_login_grace_state, {}, clear=True), \
             mock.patch.dict(XianyuLive._password_login_failure_backoff, {}, clear=True):
            XianyuLive.cache_auth_prewarmed_token(
                account_id="acc-helper-auth-1",
                token="auth-token-1",
                source="unit-test",
            )
            auth_token_info = XianyuLive.pop_auth_prewarmed_token(
                account_id="acc-helper-auth-1"
            )
            self.assertEqual(auth_token_info["token"], "auth-token-1")

            with self.assertRaises(TypeError):
                XianyuLive.cache_auth_prewarmed_token(
                    cookie_id="acc-helper-auth-legacy-1",
                    token="auth-token-legacy-1",
                )

            XianyuLive.cache_qr_prewarmed_token(
                account_id="acc-helper-qr-1",
                token="qr-token-1",
            )
            qr_token_info = XianyuLive.pop_qr_prewarmed_token(
                account_id="acc-helper-qr-1"
            )
            self.assertEqual(qr_token_info["token"], "qr-token-1")

            XianyuLive.mark_qr_login_grace(
                account_id="acc-helper-grace-1",
                stage="real_cookie_ready",
            )
            grace_state = XianyuLive.update_qr_login_grace(
                account_id="acc-helper-grace-1",
                browser_stabilized=True,
            )
            self.assertEqual(grace_state["stage"], "real_cookie_ready")
            self.assertTrue(grace_state["browser_stabilized"])
            self.assertEqual(
                XianyuLive.get_qr_login_grace(account_id="acc-helper-grace-1")["stage"],
                "real_cookie_ready",
            )
            XianyuLive.clear_qr_login_grace(account_id="acc-helper-grace-1")
            self.assertIsNone(
                XianyuLive.get_qr_login_grace(account_id="acc-helper-grace-1")
            )

            XianyuLive.set_password_login_failure_backoff(
                account_id="acc-helper-backoff-1",
                reason="risk_control",
                seconds=60,
            )
            self.assertEqual(
                XianyuLive.get_password_login_failure_backoff(
                    account_id="acc-helper-backoff-1"
                )["reason"],
                "risk_control",
            )
            XianyuLive.clear_password_login_failure_backoff(
                account_id="acc-helper-backoff-1"
            )
            self.assertIsNone(
                XianyuLive.get_password_login_failure_backoff(
                    account_id="acc-helper-backoff-1"
                )
            )

            with self.assertRaises(TypeError):
                XianyuLive.mark_qr_login_grace(cookie_id="acc-helper-grace-legacy-1")
            with self.assertRaises(TypeError):
                XianyuLive.update_qr_login_grace(cookie_id="acc-helper-grace-legacy-1")
            with self.assertRaises(TypeError):
                XianyuLive.get_password_login_failure_backoff(
                    cookie_id="acc-helper-backoff-legacy-1"
                )

    def test_manual_refresh_helpers_require_account_id_contract(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "acc-manual-helper-1"
        live._legacy_cookie_id = "legacy-manual-helper-1"
        live.cookie_refresh_enabled = True
        live.enable_cookie_refresh = mock.Mock()
        live.last_cookie_refresh_time = 0

        with mock.patch.dict(
            XianyuLive._instances,
            {"acc-manual-helper-1": live},
            clear=True,
        ), mock.patch.dict(XianyuLive._manual_refresh_state, {}, clear=True):
            self.assertIs(
                XianyuLive.get_instance(account_id="acc-manual-helper-1"),
                live,
            )
            with self.assertRaises(TypeError):
                XianyuLive.get_instance(cookie_id="acc-manual-helper-1")

            begin_result = XianyuLive.begin_manual_refresh(
                account_id="acc-manual-helper-1",
                source="unit-test",
            )
            self.assertTrue(begin_result["started"])
            self.assertTrue(
                XianyuLive.is_manual_refresh_active(
                    account_id="acc-manual-helper-1"
                )
            )
            self.assertEqual(
                XianyuLive.get_manual_refresh_state(
                    account_id="acc-manual-helper-1"
                )["source"],
                "unit-test",
            )

            handoff_result = XianyuLive.mark_manual_refresh_handoff(
                account_id="acc-manual-helper-1",
                source="handoff-test",
            )
            self.assertTrue(handoff_result["updated"])
            self.assertTrue(
                XianyuLive.consume_manual_refresh_slider_failed_bypass(
                    account_id="acc-manual-helper-1"
                )
            )
            self.assertFalse(
                XianyuLive.consume_manual_refresh_slider_failed_bypass(
                    account_id="acc-manual-helper-1"
                )
            )
            self.assertFalse(
                XianyuLive.is_manual_refresh_active(
                    account_id="acc-manual-helper-1",
                    allow_handoff_recovery=True,
                )
            )

            self.assertTrue(
                XianyuLive.end_manual_refresh(
                    account_id="acc-manual-helper-1",
                    source="unit-test",
                )
            )
            self.assertFalse(
                XianyuLive.is_manual_refresh_active(
                    account_id="acc-manual-helper-1"
                )
            )
            self.assertGreater(live.last_cookie_refresh_time, 0)
            live.enable_cookie_refresh.assert_has_calls(
                [mock.call(False), mock.call(True)]
            )

            with self.assertRaises(TypeError):
                XianyuLive.is_manual_refresh_active(
                    cookie_id="acc-manual-helper-1"
                )
            with self.assertRaises(TypeError):
                XianyuLive.consume_manual_refresh_slider_failed_bypass(
                    cookie_id="acc-manual-helper-1"
                )
            with self.assertRaises(TypeError):
                XianyuLive.end_manual_refresh(
                    cookie_id="acc-manual-helper-1",
                    source="unit-test",
                )

    def test_manual_refresh_helpers_report_empty_account_id_reason_for_empty_or_default_scope(self):
        for invalid_account_id in ("", "default"):
            with self.subTest(account_id=invalid_account_id):
                self.assertEqual(
                    XianyuLive.begin_manual_refresh(
                        account_id=invalid_account_id,
                        source="unit-test",
                    )["reason"],
                    "empty_account_id",
                )
                self.assertEqual(
                    XianyuLive.mark_manual_refresh_handoff(
                        account_id=invalid_account_id,
                        source="unit-test",
                    )["reason"],
                    "empty_account_id",
                )

    def test_manual_refresh_helpers_ignore_blank_or_default_account_id_without_cookie_id_fallback(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "acc-manual-blank-fallback"
        live._legacy_cookie_id = "legacy-manual-blank-fallback"
        live.cookie_refresh_enabled = True
        live.enable_cookie_refresh = mock.Mock()
        live.last_cookie_refresh_time = 0

        with mock.patch.dict(
            XianyuLive._instances,
            {"acc-manual-blank-fallback": live},
            clear=True,
        ), mock.patch.dict(XianyuLive._manual_refresh_state, {}, clear=True):
            for invalid_account_id in ("   ", "default"):
                with self.subTest(account_id=invalid_account_id):
                    begin_result = XianyuLive.begin_manual_refresh(
                        account_id=invalid_account_id,
                        source="unit-test",
                    )

                    self.assertFalse(begin_result["started"])
                    self.assertEqual(begin_result["reason"], "empty_account_id")
                    self.assertFalse(
                        XianyuLive.is_manual_refresh_active(account_id=invalid_account_id)
                    )
                    self.assertIsNone(
                        XianyuLive.get_manual_refresh_state(account_id=invalid_account_id)
                    )
                    self.assertFalse(
                        XianyuLive.consume_manual_refresh_slider_failed_bypass(
                            account_id=invalid_account_id
                        )
                    )
                    self.assertFalse(
                        XianyuLive.end_manual_refresh(
                            account_id=invalid_account_id,
                            source="unit-test",
                        )
                    )

    def test_account_scoped_recovery_helpers_require_account_id_contract(self):
        with mock.patch.dict(XianyuLive._auth_recovery_locks, {}, clear=True), \
             mock.patch.dict(XianyuLive._init_auth_failure_state, {}, clear=True):
            acquired, existing = XianyuLive.acquire_auth_recovery_lock(
                account_id="acc-lock-helper-1",
                owner="owner-1",
                ttl=60,
            )
            self.assertTrue(acquired)
            self.assertIsNone(existing)

            with self.assertRaises(TypeError):
                XianyuLive.acquire_auth_recovery_lock(
                    cookie_id="acc-lock-helper-1",
                    owner="owner-2",
                    ttl=60,
                )

            XianyuLive.release_auth_recovery_lock(
                account_id="acc-lock-helper-1",
                owner="owner-1",
            )
            acquired, existing = XianyuLive.acquire_auth_recovery_lock(
                account_id="acc-lock-helper-1",
                owner="owner-2",
                ttl=60,
            )
            self.assertTrue(acquired)
            self.assertIsNone(existing)

            failure_state = XianyuLive.record_init_auth_failure(
                account_id="acc-init-failure-helper-1",
                reason="boom",
            )
            self.assertEqual(failure_state["count"], 1)
            self.assertEqual(
                XianyuLive.get_init_auth_failure_state(
                    account_id="acc-init-failure-helper-1"
                )["last_reason"],
                "boom",
            )
            XianyuLive.clear_init_auth_failure_state(
                account_id="acc-init-failure-helper-1"
            )
            self.assertIsNone(
                XianyuLive.get_init_auth_failure_state(
                    account_id="acc-init-failure-helper-1"
                )
            )

            with self.assertRaises(TypeError):
                XianyuLive.get_init_auth_failure_state(
                    cookie_id="acc-init-failure-helper-1"
                )

    async def test_get_yifan_api_card_content_records_order_updates_with_account_id(self):
        response_payload = json.dumps(
            {
                "code": 1,
                "data": {
                    "orderno": "yf-order-1",
                    "usorderno": "merchant-order-1",
                },
            },
            ensure_ascii=False,
        )
        fake_session = self._FakeAsyncSession(
            self._FakeAsyncResponse(200, response_payload)
        )
        fake_db = mock.Mock()
        fake_db.update_order_yifan_status.return_value = True
        fake_db.update_order_chat_id.return_value = True

        live = XianyuLive.__new__(XianyuLive)
        live.session = fake_session
        live.account_id = "acc-yifan-runtime-1"
        live._legacy_cookie_id = "legacy-cookie-yifan-runtime-1"
        live.create_session = mock.AsyncMock()
        live.send_notification = mock.AsyncMock()

        rule = {
            "id": 1,
            "card_name": "demo card",
            "api_config": {
                "user_id": "merchant-1",
                "user_key": "secret-1",
                "goods_id": "goods-1",
            },
        }

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "YIFAN_API", {}):
            result = await live._get_yifan_api_card_content(
                rule,
                order_id="order-yifan-runtime-1",
                item_id="item-yifan-runtime-1",
                buyer_id="buyer-yifan-runtime-1",
                chat_id="chat-yifan-runtime-1",
            )

        self.assertIn("自动发货订单已提交成功", result)
        fake_db.update_order_yifan_status.assert_called_once_with(
            order_id="order-yifan-runtime-1",
            account_id="acc-yifan-runtime-1",
            yifan_orderno="yf-order-1",
            delivery_status="processing",
        )
        fake_db.update_order_chat_id.assert_called_once_with(
            "order-yifan-runtime-1",
            "chat-yifan-runtime-1",
            account_id="acc-yifan-runtime-1",
        )

    async def test_get_yifan_api_card_content_skips_order_updates_without_canonical_account_id(self):
        response_payload = json.dumps(
            {
                "code": 1,
                "data": {
                    "orderno": "yf-order-blank-account",
                    "usorderno": "merchant-order-blank-account",
                },
            },
            ensure_ascii=False,
        )
        fake_session = self._FakeAsyncSession(
            self._FakeAsyncResponse(200, response_payload)
        )
        fake_db = mock.Mock()
        mock_logger = mock.Mock()

        live = XianyuLive.__new__(XianyuLive)
        live.session = fake_session
        live.account_id = "   "
        live._legacy_cookie_id = "legacy-cookie-yifan-runtime-blank"
        live.create_session = mock.AsyncMock()
        live.send_notification = mock.AsyncMock()
        live._safe_str = str

        rule = {
            "id": 1,
            "card_name": "demo card",
            "api_config": {
                "user_id": "merchant-1",
                "user_key": "secret-1",
                "goods_id": "goods-1",
            },
        }

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "YIFAN_API", {}), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._get_yifan_api_card_content(
                rule,
                order_id="order-yifan-runtime-blank",
                item_id="item-yifan-runtime-blank",
                buyer_id="buyer-yifan-runtime-blank",
                chat_id="chat-yifan-runtime-blank",
            )

        self.assertIsNone(result)
        self.assertEqual(fake_session.calls, [])
        live.create_session.assert_not_awaited()
        fake_db.update_order_yifan_status.assert_not_called()
        fake_db.update_order_chat_id.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("缺少 canonical account_id", messages)

    def test_resolve_account_browser_profile_dir_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-2095002164"
        live.account_id = "account-2095002164"
        live.cookies_str = "unb=qr-unb"

        profile_dir = live._resolve_account_browser_profile_dir()

        self.assertEqual(
            profile_dir,
            os.path.join(os.getcwd(), "browser_data", "user_account-2095002164"),
        )

    def test_resolve_account_browser_profile_dir_uses_runtime_manager_for_exact_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-2095002164"
        live.account_id = "shop_202605130001"
        live.cookies_str = "unb=qr-unb"

        expected = os.path.join(os.getcwd(), "browser_data", "user_shop_202605130001")
        runtime_manager = types.SimpleNamespace(
            resolve_profile_dir=mock.Mock(return_value=expected),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ):
            profile_dir = live._resolve_account_browser_profile_dir()

        self.assertEqual(profile_dir, expected)
        runtime_manager.resolve_profile_dir.assert_called_once_with("shop_202605130001")

    def test_resolve_account_browser_profile_dir_does_not_fallback_to_unb(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = ""
        live._legacy_cookie_id = ""
        live.cookies_str = "unb=legacy-unb-only"

        with self.assertRaisesRegex(RuntimeError, "缺少 canonical account_id"):
            live._resolve_account_browser_profile_dir()

    def test_resolve_account_browser_profile_dir_does_not_fallback_to_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-profile-key"
        live.account_id = "   "
        live.cookies_str = "unb=legacy-unb-only"

        with self.assertRaisesRegex(RuntimeError, "缺少 canonical account_id"):
            live._resolve_account_browser_profile_dir()

    def test_should_prefer_account_persistent_profile_looks_up_qr_grace_with_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-recovery-1"
        live.account_id = "account-recovery-1"
        live.get_qr_login_grace = mock.Mock(
            side_effect=lambda key: {"stage": "real_cookie_ready"} if key == "account-recovery-1" else None
        )
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=False)

        should_reuse, reuse_reason = live._should_prefer_account_persistent_profile_for_browser_recovery()

        self.assertTrue(should_reuse)
        self.assertEqual(reuse_reason, "扫码登录缓冲期")
        live.get_qr_login_grace.assert_called_once_with("account-recovery-1")
        live.get_manual_refresh_state.assert_not_called()

    def test_should_prefer_account_persistent_profile_does_not_probe_stale_cookie_id_when_account_id_present(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-recovery-stale"
        live.account_id = "account-recovery-current"
        live.get_qr_login_grace = mock.Mock(
            side_effect=lambda key: {"stage": "stale-only"} if key == "legacy-cookie-recovery-stale" else None
        )
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=False)

        should_reuse, _ = live._should_prefer_account_persistent_profile_for_browser_recovery()

        self.assertTrue(should_reuse)
        self.assertEqual(
            live.get_qr_login_grace.call_args_list,
            [mock.call("account-recovery-current")],
        )
        live.get_manual_refresh_state.assert_called_once_with("account-recovery-current")

    def test_should_prefer_account_persistent_profile_skips_stale_cookie_id_when_account_id_blank(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-recovery-blank"
        live.account_id = "   "
        live.get_qr_login_grace = mock.Mock(return_value={"stage": "stale-only"})
        live.get_manual_refresh_state = mock.Mock(return_value={"phase": "manual_refresh"})
        live._has_recent_slider_success = mock.Mock(return_value=False)

        should_reuse, reuse_reason = live._should_prefer_account_persistent_profile_for_browser_recovery()

        self.assertFalse(should_reuse)
        self.assertIn("account_id", reuse_reason)
        live.get_qr_login_grace.assert_not_called()
        live.get_manual_refresh_state.assert_not_called()

    def test_build_browser_refresh_launch_args_skip_enable_automation_in_docker(self):
        live = XianyuLive.__new__(XianyuLive)

        with mock.patch.object(
            XianyuAutoAsync.os,
            "getenv",
            side_effect=lambda key: "1" if key == "DOCKER_ENV" else None,
        ):
            browser_args = live._build_browser_refresh_launch_args()

        self.assertNotIn("--enable-automation", browser_args)

    def test_pause_chat_uses_account_scoped_pause_duration(self):
        pause_manager = XianyuAutoAsync.AutoReplyPauseManager()
        fake_db = mock.Mock()
        fake_db.get_cookie_pause_duration.return_value = 3

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("XianyuAutoAsync.time.time", return_value=100.0):
            pause_manager.pause_chat("chat-pause-1", "acc-pause-1")

        fake_db.get_cookie_pause_duration.assert_called_once_with("acc-pause-1")
        self.assertEqual(
            pause_manager.paused_chats[("acc-pause-1", "chat-pause-1")],
            280.0,
        )

    def test_pause_chat_rejects_legacy_cookie_id_keyword(self):
        pause_manager = XianyuAutoAsync.AutoReplyPauseManager()

        with self.assertRaises(TypeError):
            pause_manager.pause_chat(
                "chat-pause-alias-1",
                account_id="acc-pause-alias-1",
                cookie_id="legacy-cookie-pause-1",
            )

    def test_pause_lookup_rejects_legacy_cookie_id_keyword(self):
        pause_manager = XianyuAutoAsync.AutoReplyPauseManager()
        pause_manager.paused_chats = {
            ("legacy-cookie-only-pause-lookup-1", "chat-cookie-only-pause-lookup-1"): 180.0,
        }

        with mock.patch("XianyuAutoAsync.time.time", return_value=100.0), \
             self.assertRaises(TypeError):
            pause_manager.is_chat_paused(
                "chat-cookie-only-pause-lookup-1",
                cookie_id="legacy-cookie-only-pause-lookup-1",
            )
        with mock.patch("XianyuAutoAsync.time.time", return_value=100.0), \
             self.assertRaises(TypeError):
            pause_manager.get_remaining_pause_time(
                "chat-cookie-only-pause-lookup-1",
                cookie_id="legacy-cookie-only-pause-lookup-1",
            )

    def test_pause_chat_keeps_same_chat_id_isolated_by_account_id(self):
        pause_manager = XianyuAutoAsync.AutoReplyPauseManager()
        fake_db = mock.Mock()
        fake_db.get_cookie_pause_duration.return_value = 3

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("XianyuAutoAsync.time.time", return_value=100.0):
            pause_manager.pause_chat(
                "chat-same-id",
                account_id="acc-pause-a",
            )
            pause_manager.pause_chat(
                "chat-same-id",
                account_id="acc-pause-b",
            )

        self.assertIn(("acc-pause-a", "chat-same-id"), pause_manager.paused_chats)
        self.assertIn(("acc-pause-b", "chat-same-id"), pause_manager.paused_chats)

    def test_pause_chat_scopes_same_chat_id_by_account_id(self):
        pause_manager = XianyuAutoAsync.AutoReplyPauseManager()
        fake_db = mock.Mock()
        fake_db.get_cookie_pause_duration.return_value = 3

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("XianyuAutoAsync.time.time", return_value=100.0):
            self.assertFalse(
                pause_manager.is_chat_paused(
                    "chat-not-paused",
                    account_id="acc-pause-a",
                )
            )

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("XianyuAutoAsync.time.time", return_value=100.0):
            pause_manager.pause_chat("chat-shared-1", "acc-pause-a")

            self.assertTrue(
                pause_manager.is_chat_paused(
                    "chat-shared-1",
                    account_id="acc-pause-a",
                )
            )
            self.assertEqual(
                pause_manager.get_remaining_pause_time(
                    "chat-shared-1",
                    account_id="acc-pause-a",
                ),
                180,
            )
            self.assertFalse(
                pause_manager.is_chat_paused(
                    "chat-shared-1",
                    account_id="acc-pause-b",
                )
            )
            self.assertEqual(
                pause_manager.get_remaining_pause_time(
                    "chat-shared-1",
                    account_id="acc-pause-b",
                ),
                0,
            )

    def test_pause_chat_rejects_default_or_blank_account_scope(self):
        pause_manager = XianyuAutoAsync.AutoReplyPauseManager()
        fake_db = mock.Mock()
        fake_db.get_cookie_pause_duration.side_effect = AssertionError(
            "should not read pause duration without canonical account_id"
        )

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("XianyuAutoAsync.time.time", return_value=100.0):
            for invalid_account_id in ("default", "   "):
                with self.subTest(account_id=invalid_account_id):
                    pause_manager.pause_chat("chat-invalid-pause-1", account_id=invalid_account_id)
                    self.assertFalse(
                        pause_manager.is_chat_paused(
                            "chat-invalid-pause-1",
                            account_id=invalid_account_id,
                        )
                    )
                    self.assertEqual(
                        pause_manager.get_remaining_pause_time(
                            "chat-invalid-pause-1",
                            account_id=invalid_account_id,
                        ),
                        0,
                    )

        fake_db.get_cookie_pause_duration.assert_not_called()
        self.assertEqual(pause_manager.paused_chats, {})

    def test_cleanup_expired_pauses_keeps_other_account_scope_for_same_chat_id(self):
        pause_manager = XianyuAutoAsync.AutoReplyPauseManager()
        pause_manager.paused_chats = {
            ("acc-cleanup-a", "chat-shared-cleanup"): 90.0,
            ("acc-cleanup-b", "chat-shared-cleanup"): 150.0,
        }

        with mock.patch("XianyuAutoAsync.time.time", return_value=100.0):
            pause_manager.cleanup_expired_pauses()

        self.assertNotIn(
            ("acc-cleanup-a", "chat-shared-cleanup"),
            pause_manager.paused_chats,
        )
        self.assertEqual(
            pause_manager.paused_chats[("acc-cleanup-b", "chat-shared-cleanup")],
            150.0,
        )

    def test_log_captcha_event_uses_account_id_scope(self):
        file_handle = mock.mock_open()

        with mock.patch("XianyuAutoAsync.os.makedirs"), \
             mock.patch("builtins.open", file_handle), \
             mock.patch("XianyuAutoAsync.time.strftime", return_value="2026-05-11 00:00:00"):
            XianyuAutoAsync.log_captcha_event(
                account_id="acc-captcha-log-1",
                event_type="滑块验证成功",
                success=True,
                details="unit test",
            )

        file_handle.assert_called_once_with(
            os.path.join("logs", "captcha_verification.txt"),
            "a",
            encoding="utf-8",
        )
        file_handle().write.assert_called_once()
        log_entry = file_handle().write.call_args.args[0]
        self.assertIn("【acc-captcha-log-1】", log_entry)
        self.assertNotIn("legacy-captcha-log-1", log_entry)

    def test_need_captcha_verification_prefers_account_id_alias_for_captcha_log(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-captcha-check-1"
        live.account_id = "acc-captcha-check-1"
        live._safe_str = str

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()) as log_captcha_event:
            result = live._need_captcha_verification(
                {"ret": ["FAIL_SYS_USER_VALIDATE"], "data": {"url": "https://verify.example.com/slider"}}
            )

        self.assertTrue(result)
        self.assertEqual(log_captcha_event.call_args.args[0], "acc-captcha-check-1")
        self.assertEqual(log_captcha_event.call_args.args[0], live.account_id)

    def test_need_captcha_verification_rejects_blank_canonical_account_id_for_captcha_log(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-captcha-blank"
        live.account_id = "   "
        live._safe_str = str

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()) as log_captcha_event:
            result = live._need_captcha_verification(
                {"ret": ["FAIL_SYS_USER_VALIDATE"], "data": {"url": "https://verify.example.com/slider"}}
            )

        self.assertTrue(result)
        log_captcha_event.assert_called_once()
        self.assertEqual(log_captcha_event.call_args.args[0], "")
        self.assertNotEqual(log_captcha_event.call_args.args[0], live._legacy_cookie_id)

    def test_need_captcha_verification_detects_verification_url_without_ret_payload(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-captcha-url-only"
        live.account_id = "acc-captcha-url-only"
        live._safe_str = str

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()) as log_captcha_event:
            result = live._need_captcha_verification(
                {"data": {"url": "https://verify.example.com/punish?x5secdata=slider"}}
            )

        self.assertTrue(result)
        log_captcha_event.assert_called_once()
        self.assertEqual(log_captcha_event.call_args.args[0], "acc-captcha-url-only")

    def test_calculate_retry_delay_uses_init_auth_failures_when_connection_failures_reset(self):
        live = XianyuLive.__new__(XianyuLive)
        live.connection_failures = 0
        live.init_auth_failures = 2

        retry_delay = live._calculate_retry_delay("Token获取失败(status=captcha_verification_failed)")

        self.assertEqual(retry_delay, 10)

    def test_classify_password_login_failure_keeps_legacy_garbled_keywords_compatible(self):
        cases = [
            ("滑块验证失败", ("slider_failed", 600)),
            (XianyuLive._legacy_gbk_mojibake("滑块验证失败"), ("slider_failed", 600)),
            ("未找到滑块容器", ("slider_failed", 600)),
            (XianyuLive._legacy_missing_tail("未找到滑块容器"), ("slider_failed", 600)),
            ("未找到登录表单", ("login_form_missing", 90)),
            (XianyuLive._legacy_missing_tail("未找到登录表单"), ("login_form_missing", 90)),
            ("session过期且清理会话状态后未找到登录表单", ("login_form_missing", 90)),
            (XianyuLive._legacy_missing_tail("session过期且清理会话状态后未找到登录表单"), ("login_form_missing", 90)),
            ("session验证异常且清理会话状态后未找到登录表单", ("login_form_missing", 90)),
            (XianyuLive._legacy_missing_tail("session验证异常且清理会话状态后未找到登录表单"), ("login_form_missing", 90)),
            ("页面会话已失效", ("unknown", 180)),
            (XianyuLive._legacy_missing_tail("页面会话已失效"), ("unknown", 180)),
        ]

        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(XianyuLive.classify_password_login_failure(message), expected)

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
        live.account_id = "runtime-close-test"

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
        live.account_id = "managed-runtime-close-test"
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

    def test_record_delivery_log_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-delivery-live-1"
        live.user_id = 7
        live._resolve_delivery_log_buyer_nick = mock.Mock(return_value="buyer-nick")
        live._format_delivery_log_reason = mock.Mock(return_value="formatted-reason")
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.create_delivery_log.return_value = 11

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            live._record_delivery_log(
                order_id="order-live-1",
                item_id="item-live-1",
                buyer_id="buyer-live-1",
                buyer_nick="raw-buyer",
                status="success",
                reason="ok",
                channel="manual",
                rule_meta={"rule_id": 3, "rule_keyword": "kw", "card_type": "text", "match_mode": "exact"},
            )

        fake_db.create_delivery_log.assert_called_once_with(
            user_id=7,
            account_id="acc-delivery-live-1",
            order_id="order-live-1",
            item_id="item-live-1",
            buyer_id="buyer-live-1",
            buyer_nick="buyer-nick",
            rule_id=3,
            rule_keyword="kw",
            card_type="text",
            match_mode="exact",
            channel="manual",
            status="success",
            reason="formatted-reason",
        )

    def test_record_delivery_log_skips_db_write_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-log-blank"
        live.account_id = "   "
        live.user_id = 7
        live._resolve_delivery_log_buyer_nick = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip buyer nick resolution")
        )
        live._format_delivery_log_reason = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip reason formatting")
        )
        live._safe_str = str
        fake_db = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = live._record_delivery_log(
                order_id="order-delivery-log-blank",
                item_id="item-delivery-log-blank",
                buyer_id="buyer-delivery-log-blank",
                buyer_nick="raw-buyer",
                status="success",
                reason="ok",
                channel="manual",
            )

        self.assertFalse(result)
        fake_db.create_delivery_log.assert_not_called()
        live._resolve_delivery_log_buyer_nick.assert_not_called()
        live._format_delivery_log_reason.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    def test_resolve_delivery_log_buyer_nick_rejects_cross_account_order_record(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "acc-delivery-live-guard"
        live._legacy_cookie_id = "acc-delivery-live-guard"
        live._sanitize_buyer_nick = XianyuLive._sanitize_buyer_nick.__get__(live, XianyuLive)
        live._safe_str = str
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-cross-account",
            "account_id": "other-account",
            "buyer_id": "buyer-guard-1",
            "buyer_nick": "wrong-buyer",
        }
        fake_db.get_recent_order_by_buyer_id.return_value = None

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            resolved = live._resolve_delivery_log_buyer_nick(
                "raw-buyer",
                order_id="order-cross-account",
                log_prefix="[unit-test]",
            )

        self.assertEqual(resolved, "raw-buyer")
        fake_db.get_recent_order_by_buyer_id.assert_called_once_with(
            "buyer-guard-1",
            account_id="acc-delivery-live-guard",
            minutes=60,
        )

    def test_resolve_delivery_log_buyer_nick_skips_db_lookup_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-buyer-nick"
        live.account_id = "   "
        live._sanitize_buyer_nick = XianyuLive._sanitize_buyer_nick.__get__(live, XianyuLive)
        live._safe_str = str

        fake_db = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            resolved = live._resolve_delivery_log_buyer_nick(
                "raw-buyer",
                order_id="order-no-canonical",
                buyer_id="buyer-no-canonical",
                log_prefix="[unit-test]",
            )

        self.assertEqual(resolved, "raw-buyer")
        fake_db.get_order_by_id.assert_not_called()
        fake_db.get_recent_order_by_buyer_id.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    def test_resolve_delivery_log_buyer_nick_prefers_account_id_alias_for_account_match(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-delivery-live-alias"
        live._sanitize_buyer_nick = XianyuLive._sanitize_buyer_nick.__get__(live, XianyuLive)
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-alias",
            "account_id": "acc-delivery-live-alias",
            "buyer_id": "buyer-alias-1",
            "buyer_nick": "right-buyer",
        }

        with mock.patch("db_manager.db_manager", fake_db):
            resolved = live._resolve_delivery_log_buyer_nick(
                "raw-buyer",
                order_id="order-alias",
                log_prefix="[unit-test]",
            )

        self.assertEqual(resolved, "right-buyer")
        fake_db.get_order_by_id.assert_called_once_with(
            "order-alias",
            account_id="acc-delivery-live-alias",
        )

    def test_lookup_delivery_order_by_sid_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-sid-lookup"
        live.account_id = "acc-sid-lookup-1"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.find_recent_orders_by_match_context.side_effect = [
            [{"order_id": "order-sid-1", "order_status": "pending_ship"}],
        ]

        with mock.patch.dict(
            XianyuLive._lookup_delivery_order_by_sid.__globals__,
            {"db_manager": fake_db},
        ):
            result = live._lookup_delivery_order_by_sid("sid-lookup-1", log_prefix="[unit-test]")

        self.assertEqual(result["match_type"], "pending_ship")
        self.assertEqual(result["order"]["order_id"], "order-sid-1")
        fake_db.find_recent_orders_by_match_context.assert_called_once_with(
            sid="sid-lookup-1",
            account_id="acc-sid-lookup-1",
            statuses=[
                "pending_ship",
                "pending_delivery",
                "partial_success",
                "partial_pending_finalize",
            ],
            minutes=10,
            limit=5,
        )

    def test_lookup_delivery_order_by_sid_skips_db_lookup_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-sid-lookup"
        live.account_id = ""
        live._safe_str = str

        fake_db = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch.dict(
            XianyuLive._lookup_delivery_order_by_sid.__globals__,
            {"db_manager": fake_db},
        ), mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = live._lookup_delivery_order_by_sid("sid-lookup-blank", log_prefix="[unit-test]")

        self.assertEqual(result, {"match_type": "missing", "order": None})
        fake_db.find_recent_orders_by_match_context.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    def test_lookup_delivery_order_by_sid_prefers_account_id_alias_for_recent_order_branch(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-sid-lookup"
        live.account_id = "acc-sid-lookup-2"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.find_recent_orders_by_match_context.side_effect = [
            [],
            [{"order_id": "order-sid-2", "order_status": "processing"}],
        ]

        with mock.patch.dict(
            XianyuLive._lookup_delivery_order_by_sid.__globals__,
            {"db_manager": fake_db},
        ):
            result = live._lookup_delivery_order_by_sid("sid-lookup-2", log_prefix="[unit-test]")

        self.assertEqual(result["match_type"], "not_ready")
        self.assertEqual(result["order"]["order_id"], "order-sid-2")
        fake_db.find_recent_orders_by_match_context.assert_has_calls(
            [
                mock.call(
                    sid="sid-lookup-2",
                    account_id="acc-sid-lookup-2",
                    statuses=[
                        "pending_ship",
                        "pending_delivery",
                        "partial_success",
                        "partial_pending_finalize",
                    ],
                    minutes=10,
                    limit=5,
                ),
                mock.call(
                    sid="sid-lookup-2",
                    account_id="acc-sid-lookup-2",
                    statuses=[
                        "processing",
                        "pending_payment",
                        "shipped",
                        "completed",
                        "cancelled",
                    ],
                    minutes=10,
                    limit=5,
                ),
            ]
        )

    def test_lookup_delivery_order_by_sid_rejects_ambiguous_pending_candidates_without_context(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-sid-lookup"
        live.account_id = "acc-sid-lookup-ambiguous-pending"
        live._safe_str = str
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.find_recent_orders_by_match_context.side_effect = [
            [
                {
                    "order_id": "order-sid-pending-1",
                    "order_status": "pending_ship",
                    "item_id": "item-pending-1",
                    "buyer_id": "buyer-pending-1",
                },
                {
                    "order_id": "order-sid-pending-2",
                    "order_status": "pending_ship",
                    "item_id": "item-pending-2",
                    "buyer_id": "buyer-pending-2",
                },
            ],
        ]

        with mock.patch.dict(
            XianyuLive._lookup_delivery_order_by_sid.__globals__,
            {"db_manager": fake_db},
        ), mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = live._lookup_delivery_order_by_sid("sid-lookup-ambiguous", log_prefix="[unit-test]")

        self.assertEqual(result, {"match_type": "ambiguous_pending_ship", "order": None})
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("多个候选", messages)
        self.assertIn("sid-lookup-ambiguous", messages)

    def test_lookup_delivery_order_by_sid_uses_item_and_buyer_context_to_select_unique_pending_candidate(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-sid-lookup"
        live.account_id = "acc-sid-lookup-context-pending"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.find_recent_orders_by_match_context.side_effect = [
            [
                {
                    "order_id": "order-sid-pending-1",
                    "order_status": "pending_ship",
                    "item_id": "item-pending-1",
                    "buyer_id": "buyer-pending-1",
                },
                {
                    "order_id": "order-sid-pending-2",
                    "order_status": "pending_ship",
                    "item_id": "item-pending-2",
                    "buyer_id": "buyer-pending-2",
                },
            ],
        ]

        with mock.patch.dict(
            XianyuLive._lookup_delivery_order_by_sid.__globals__,
            {"db_manager": fake_db},
        ):
            result = live._lookup_delivery_order_by_sid(
                "sid-lookup-context",
                item_id="item-pending-2",
                buyer_id="buyer-pending-2",
                log_prefix="[unit-test]",
            )

        self.assertEqual(result["match_type"], "pending_ship")
        self.assertEqual(result["order"]["order_id"], "order-sid-pending-2")

    def test_lookup_delivery_order_by_sid_rejects_ambiguous_recent_candidates_after_context_filter(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-sid-lookup"
        live.account_id = "acc-sid-lookup-ambiguous-recent"
        live._safe_str = str
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.find_recent_orders_by_match_context.side_effect = [
            [],
            [
                {
                    "order_id": "order-sid-recent-1",
                    "order_status": "processing",
                    "item_id": "item-recent-same",
                    "buyer_id": "buyer-recent-same",
                },
                {
                    "order_id": "order-sid-recent-2",
                    "order_status": "processing",
                    "item_id": "item-recent-same",
                    "buyer_id": "buyer-recent-same",
                },
            ],
        ]

        with mock.patch.dict(
            XianyuLive._lookup_delivery_order_by_sid.__globals__,
            {"db_manager": fake_db},
        ), mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = live._lookup_delivery_order_by_sid(
                "sid-lookup-recent-ambiguous",
                item_id="item-recent-same",
                buyer_id="buyer-recent-same",
                log_prefix="[unit-test]",
            )

        self.assertEqual(result, {"match_type": "ambiguous_recent", "order": None})
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("多个候选", messages)
        self.assertIn("sid-lookup-recent-ambiguous", messages)

    async def test_refresh_sid_lookup_if_needed_keeps_ambiguous_result_after_forced_refresh(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-sid-refresh"
        live.account_id = "acc-sid-refresh-ambiguous"
        live._safe_str = str
        live._reserve_order_detail_force_refresh = mock.Mock(return_value=True)
        live.fetch_order_detail_info = mock.AsyncMock(return_value={"ok": True})
        live._lookup_delivery_order_by_sid = mock.Mock(
            return_value={"match_type": "ambiguous_recent", "order": None}
        )

        sid_lookup = {
            "match_type": "not_ready",
            "order": {
                "order_id": "order-refresh-1",
                "order_status": "processing",
                "item_id": "item-refresh-1",
                "buyer_id": "buyer-refresh-1",
            },
        }

        result = await live._refresh_sid_lookup_if_needed(
            "sid-refresh-1",
            sid_lookup,
            item_id="item-refresh-1",
            buyer_id="buyer-refresh-1",
            minutes=5,
            allow_bargain_ready=True,
            log_prefix="[unit-test]",
        )

        self.assertEqual(result, {"match_type": "ambiguous_recent", "order": None})
        live._reserve_order_detail_force_refresh.assert_called_once_with(
            "order-refresh-1",
            reason="sid_not_ready",
            log_prefix="[unit-test]",
        )
        live.fetch_order_detail_info.assert_awaited_once_with(
            "order-refresh-1",
            "item-refresh-1",
            "buyer-refresh-1",
            sid="sid-refresh-1",
            force_refresh=True,
        )
        live._lookup_delivery_order_by_sid.assert_called_once_with(
            "sid-refresh-1",
            item_id="item-refresh-1",
            buyer_id="buyer-refresh-1",
            minutes=5,
            log_prefix="[unit-test]",
        )

    async def test_ensure_item_owned_by_current_account_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-item-live-alias"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_item_info.return_value = {"item_id": "item-owned"}

        with mock.patch("XianyuAutoAsync.db_manager", fake_db), \
             mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.dict(
                 XianyuLive._ensure_item_owned_by_current_account.__globals__,
                 {"db_manager": fake_db},
             ):
            result = await live._ensure_item_owned_by_current_account("item-owned", log_prefix="[unit-test]")

        self.assertTrue(result)
        fake_db.get_item_info.assert_called_once_with("acc-item-live-alias", "item-owned")

    async def test_ensure_item_owned_by_current_account_rejects_blank_canonical_account_id_before_db_lookup(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-item-owned"
        live.account_id = "   "
        live._safe_str = str
        live.get_item_list_info = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip item ownership refresh")
        )

        fake_db = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch("XianyuAutoAsync.db_manager", fake_db), \
             mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.dict(
                 XianyuLive._ensure_item_owned_by_current_account.__globals__,
                 {"db_manager": fake_db},
             ), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._ensure_item_owned_by_current_account("item-owned", log_prefix="[unit-test]")

        self.assertFalse(result)
        fake_db.get_item_info.assert_not_called()
        live.get_item_list_info.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    async def test_ensure_item_owned_by_current_account_prefers_account_id_alias_after_refresh_branch(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-item-owned"
        live.account_id = "acc-item-live-refresh"
        live._safe_str = str
        live.get_item_list_info = mock.AsyncMock(
            return_value={
                "success": True,
                "items": [{"id": "other-item"}],
            }
        )

        fake_db = mock.Mock()
        fake_db.get_item_info.side_effect = [
            None,
            {"item_id": "item-owned"},
        ]

        with mock.patch("XianyuAutoAsync.db_manager", fake_db), \
             mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.dict(
                 XianyuLive._ensure_item_owned_by_current_account.__globals__,
                 {"db_manager": fake_db},
             ):
            result = await live._ensure_item_owned_by_current_account("item-owned", log_prefix="[unit-test]")

        self.assertTrue(result)
        live.get_item_list_info.assert_awaited_once_with(page_number=1, page_size=50)
        fake_db.get_item_info.assert_has_calls(
            [
                mock.call("acc-item-live-refresh", "item-owned"),
                mock.call("acc-item-live-refresh", "item-owned"),
            ]
        )

    async def test_replace_api_dynamic_params_uses_account_id_for_order_and_item_reads(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-api-dynamic"
        live.account_id = "acc-api-dynamic-1"
        live._safe_str = str
        live.fetch_order_detail_info = mock.AsyncMock(
            side_effect=AssertionError("canonical account_id path should not fallback to fetch_order_detail_info")
        )

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "amount": "18.80",
            "quantity": 2,
        }
        fake_db.get_item_info.return_value = {
            "item_detail": "{\"detail\": \"real detail\"}",
        }

        params = {
            "body": (
                "scope={account_id}|account={account_id}|"
                "order={order_id}|item={item_id}|buyer={buyer_id}|"
                "amount={order_amount}|qty={order_quantity}|detail={item_detail}|"
                "spec={spec_name}:{spec_value}"
            )
        }

        with mock.patch("db_manager.db_manager", fake_db):
            result = await live._replace_api_dynamic_params(
                params,
                order_id="order-api-1",
                item_id="item-api-1",
                buyer_id="buyer-api-1",
                spec_name="size",
                spec_value="XL",
            )

        self.assertEqual(
            result["body"],
            "scope=acc-api-dynamic-1|account=acc-api-dynamic-1|"
            "order=order-api-1|item=item-api-1|buyer=buyer-api-1|"
            "amount=18.80|qty=2|detail=real detail|spec=size:XL",
        )
        fake_db.get_order_by_id.assert_called_once_with(
            "order-api-1",
            account_id="acc-api-dynamic-1",
        )
        fake_db.get_item_info.assert_called_once_with("acc-api-dynamic-1", "item-api-1")
        live.fetch_order_detail_info.assert_not_awaited()

    async def test_replace_api_dynamic_params_leaves_legacy_cookie_id_placeholder_untouched(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-api-dynamic"
        live.account_id = "acc-api-dynamic-legacy-placeholder"
        live._safe_str = str

        result = await live._replace_api_dynamic_params(
            {"body": "legacy={cookie_id}|account={account_id}"},
            buyer_id="buyer-placeholder-1",
        )

        self.assertEqual(
            result["body"],
            "legacy={cookie_id}|account=acc-api-dynamic-legacy-placeholder",
        )

    async def test_replace_api_dynamic_params_skips_order_and_item_lookups_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-api-dynamic"
        live.account_id = "   "
        live._safe_str = str
        live.fetch_order_detail_info = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip fetch_order_detail_info fallback")
        )
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        params = {
            "body": (
                "scope={account_id}|account={account_id}|"
                "order={order_id}|item={item_id}|buyer={buyer_id}|"
                "amount={order_amount}|qty={order_quantity}|detail={item_detail}|"
                "spec={spec_name}:{spec_value}"
            )
        }

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._replace_api_dynamic_params(
                params,
                order_id="order-api-blank",
                item_id="item-api-blank",
                buyer_id="buyer-api-blank",
                spec_name="color",
                spec_value="blue",
            )

        self.assertEqual(
            result["body"],
            "scope=|account=|"
            "order=order-api-blank|item=item-api-blank|buyer=buyer-api-blank|"
            "amount={order_amount}|qty={order_quantity}|detail={item_detail}|"
            "spec=color:blue",
        )
        fake_db.get_order_by_id.assert_not_called()
        fake_db.get_item_info.assert_not_called()
        live.fetch_order_detail_info.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    async def test_maybe_force_refresh_order_detail_for_signal_skips_order_reads_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-force-refresh"
        live.account_id = "   "
        live._safe_str = str
        live._should_force_refresh_after_status_signal = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip status signal evaluation")
        )
        live._reserve_order_detail_force_refresh = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip force refresh reservation")
        )
        live.fetch_order_detail_info = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip order detail refresh")
        )
        fake_db = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch.object(XianyuAutoAsync, "db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._maybe_force_refresh_order_detail_for_signal(
                "order-force-refresh-blank",
                status_signal="pending_ship",
                reason="unit_test_signal",
                log_prefix="[unit-test]",
            )

        self.assertFalse(result)
        fake_db.get_order_by_id.assert_not_called()
        live._should_force_refresh_after_status_signal.assert_not_called()
        live._reserve_order_detail_force_refresh.assert_not_called()
        live.fetch_order_detail_info.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    async def test_maybe_force_refresh_order_detail_for_signal_uses_canonical_account_id_for_both_order_reads(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-force-refresh"
        live.account_id = "acc-force-refresh-1"
        live._safe_str = str
        live._should_force_refresh_after_status_signal = mock.Mock(side_effect=[True, True])
        live._reserve_order_detail_force_refresh = mock.Mock(return_value=True)
        live.fetch_order_detail_info = mock.AsyncMock(return_value={"ok": True})

        fake_db = mock.Mock()
        fake_db.get_order_by_id.side_effect = [
            {"order_status": "processing"},
            {
                "order_status": "pending_payment",
                "item_id": "item-force-refresh-1",
                "buyer_id": "buyer-force-refresh-1",
            },
        ]

        with mock.patch.object(XianyuAutoAsync, "db_manager", fake_db):
            result = await live._maybe_force_refresh_order_detail_for_signal(
                "order-force-refresh-1",
                status_signal="pending_ship",
                reason="unit_test_signal",
                log_prefix="[unit-test]",
            )

        self.assertTrue(result)
        fake_db.get_order_by_id.assert_has_calls(
            [
                mock.call("order-force-refresh-1", account_id="acc-force-refresh-1"),
                mock.call("order-force-refresh-1", account_id="acc-force-refresh-1"),
            ]
        )
        live._reserve_order_detail_force_refresh.assert_called_once_with(
            "order-force-refresh-1",
            reason="unit_test_signal",
            log_prefix="[unit-test]",
        )
        live.fetch_order_detail_info.assert_awaited_once_with(
            order_id="order-force-refresh-1",
            item_id="item-force-refresh-1",
            buyer_id="buyer-force-refresh-1",
            sid=None,
            buyer_nick=None,
            force_refresh=True,
        )

    async def test_maybe_force_refresh_order_detail_for_signal_skips_refresh_when_latest_status_no_longer_requires_it(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-force-refresh"
        live.account_id = "acc-force-refresh-2"
        live._safe_str = str
        live._should_force_refresh_after_status_signal = mock.Mock(side_effect=[True, False])
        live._reserve_order_detail_force_refresh = mock.Mock(return_value=True)
        live.fetch_order_detail_info = mock.AsyncMock(
            side_effect=AssertionError("latest status settled should skip order detail refresh")
        )

        fake_db = mock.Mock()
        fake_db.get_order_by_id.side_effect = [
            {"order_status": "processing"},
            {"order_status": "shipped"},
        ]

        with mock.patch.object(XianyuAutoAsync, "db_manager", fake_db):
            result = await live._maybe_force_refresh_order_detail_for_signal(
                "order-force-refresh-2",
                status_signal="pending_ship",
                reason="unit_test_signal",
                log_prefix="[unit-test]",
            )

        self.assertFalse(result)
        fake_db.get_order_by_id.assert_has_calls(
            [
                mock.call("order-force-refresh-2", account_id="acc-force-refresh-2"),
                mock.call("order-force-refresh-2", account_id="acc-force-refresh-2"),
            ]
        )
        live._reserve_order_detail_force_refresh.assert_called_once_with(
            "order-force-refresh-2",
            reason="unit_test_signal",
            log_prefix="[unit-test]",
        )
        live.fetch_order_detail_info.assert_not_awaited()

    async def test_maybe_force_refresh_order_detail_for_signal_stops_after_reservation_cooldown(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-force-refresh"
        live.account_id = "acc-force-refresh-3"
        live._safe_str = str
        live._should_force_refresh_after_status_signal = mock.Mock(return_value=True)
        live._reserve_order_detail_force_refresh = mock.Mock(return_value=False)
        live.fetch_order_detail_info = mock.AsyncMock(
            side_effect=AssertionError("reservation cooldown should skip order detail refresh")
        )

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {"order_status": "processing"}

        with mock.patch.object(XianyuAutoAsync, "db_manager", fake_db):
            result = await live._maybe_force_refresh_order_detail_for_signal(
                "order-force-refresh-3",
                status_signal="pending_ship",
                reason="unit_test_signal",
                log_prefix="[unit-test]",
            )

        self.assertFalse(result)
        fake_db.get_order_by_id.assert_called_once_with(
            "order-force-refresh-3",
            account_id="acc-force-refresh-3",
        )
        live._reserve_order_detail_force_refresh.assert_called_once_with(
            "order-force-refresh-3",
            reason="unit_test_signal",
            log_prefix="[unit-test]",
        )
        live.fetch_order_detail_info.assert_not_awaited()

    def test_preload_basic_order_info_rejects_first_write_without_existing_scoped_order(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-preload-order"
        live.account_id = "acc-preload-order-1"
        live._safe_str = str
        live._select_buyer_identity_for_order_write = mock.Mock(
            return_value=("buyer-preload-1", "buyer-preload-1", False)
        )
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = None
        fake_db.insert_or_update_order.return_value = True

        with mock.patch.object(XianyuAutoAsync, "db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = live._preload_basic_order_info(
                "order-preload-1",
                item_id="item-preload-1",
                buyer_id="buyer-preload-1",
                sid="sid-preload-1",
                buyer_nick="buyer-preload-1",
                buyer_id_source="message",
            )

        self.assertFalse(result)
        fake_db.get_order_by_id.assert_called_once_with(
            "order-preload-1",
            account_id="acc-preload-order-1",
        )
        live._select_buyer_identity_for_order_write.assert_not_called()
        fake_db.insert_or_update_order.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("未验证归属", messages)

    def test_preload_basic_order_info_updates_existing_scoped_order(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-preload-order"
        live.account_id = "acc-preload-order-2"
        live._safe_str = str
        live._select_buyer_identity_for_order_write = mock.Mock(
            return_value=("buyer-preload-2", "buyer-preload-2", False)
        )

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-preload-2",
            "account_id": "acc-preload-order-2",
            "buyer_id": "buyer-existing-2",
            "buyer_nick": "buyer-existing-2",
        }
        fake_db.insert_or_update_order.return_value = True

        with mock.patch.object(XianyuAutoAsync, "db_manager", fake_db):
            result = live._preload_basic_order_info(
                "order-preload-2",
                item_id="item-preload-2",
                buyer_id="buyer-incoming-2",
                sid="sid-preload-2",
                buyer_nick="buyer-incoming-2",
                buyer_id_source="message",
            )

        self.assertTrue(result)
        fake_db.get_order_by_id.assert_called_once_with(
            "order-preload-2",
            account_id="acc-preload-order-2",
        )
        live._select_buyer_identity_for_order_write.assert_called_once()
        fake_db.insert_or_update_order.assert_called_once_with(
            order_id="order-preload-2",
            item_id="item-preload-2",
            buyer_id="buyer-preload-2",
            buyer_nick="buyer-preload-2",
            sid="sid-preload-2",
            account_id="acc-preload-order-2",
            order_status=None,
        )

    def test_mark_order_bargain_flow_rejects_first_write_without_existing_scoped_order(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-bargain-order"
        live.account_id = "acc-bargain-order-1"
        live._safe_str = str
        live._normalize_order_amount_text = XianyuLive._normalize_order_amount_text.__get__(live, XianyuLive)
        live._parse_order_amount_float = XianyuLive._parse_order_amount_float.__get__(live, XianyuLive)
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = None
        fake_db.get_item_info.return_value = {"item_price": "9.90"}
        fake_db.insert_or_update_order.return_value = True

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = live._mark_order_bargain_flow(
                "order-bargain-1",
                item_id="item-bargain-1",
                buyer_id="buyer-bargain-1",
                sid="sid-bargain-1",
                apply_configured_price=True,
                success_detected=True,
                context="unit-test",
            )

        self.assertFalse(result)
        fake_db.get_order_by_id.assert_called_once_with(
            "order-bargain-1",
            account_id="acc-bargain-order-1",
        )
        fake_db.get_item_info.assert_not_called()
        fake_db.insert_or_update_order.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("未验证归属", messages)

    async def test_save_item_detail_only_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-item-detail-save-1"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.update_item_detail.return_value = True

        with mock.patch("db_manager.db_manager", fake_db):
            result = await live.save_item_detail_only("item-1", "detail-body")

        self.assertTrue(result)
        fake_db.update_item_detail.assert_called_once_with(
            "acc-item-detail-save-1",
            "item-1",
            "detail-body",
        )

    async def test_save_item_detail_only_rejects_blank_canonical_account_id_before_db_write(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "   "
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.update_item_detail.side_effect = AssertionError(
            "should not write item detail without canonical account_id"
        )

        with mock.patch("db_manager.db_manager", fake_db):
            result = await live.save_item_detail_only("item-1", "detail-body")

        self.assertFalse(result)
        fake_db.update_item_detail.assert_not_called()

    async def test_save_item_info_to_db_rejects_default_or_blank_canonical_account_id_before_db_write(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live._safe_str = str

                fake_db = mock.Mock()
                fake_db.save_item_info.side_effect = AssertionError(
                    "should not write item info without canonical account_id"
                )

                with mock.patch("db_manager.db_manager", fake_db):
                    result = await live.save_item_info_to_db(
                        "item-save-1",
                        item_detail="detail-body",
                        item_title="Item title",
                    )

                self.assertIsNone(result)
                fake_db.save_item_info.assert_not_called()

    async def test_save_items_list_to_db_prefers_account_id_alias_for_batch_writes(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-item-batch-1"
        live._safe_str = str
        live._fetch_item_details = mock.AsyncMock(return_value=0)

        fake_db = mock.Mock()
        fake_db.get_item_info.side_effect = [
            {"item_id": "item-existing"},
            None,
        ]
        fake_db.batch_update_item_title_price.return_value = 1
        fake_db.batch_save_item_basic_info.return_value = 1

        items_list = [
            {
                "id": "item-existing",
                "title": "Existing title",
                "price_text": "10",
                "category_id": "cat-1",
                "price": "10",
            },
            {
                "id": "item-new",
                "title": "New title",
                "price_text": "20",
                "category_id": "cat-2",
                "price": "20",
            },
        ]

        with mock.patch("db_manager.db_manager", fake_db):
            saved_count = await live.save_items_list_to_db(items_list, sync_item_details=False)

        self.assertEqual(saved_count, 2)
        fake_db.get_item_info.assert_has_calls(
            [
                mock.call("acc-item-batch-1", "item-existing"),
                mock.call("acc-item-batch-1", "item-new"),
            ]
        )
        fake_db.batch_update_item_title_price.assert_called_once()
        fake_db.batch_save_item_basic_info.assert_called_once()

        update_payload = fake_db.batch_update_item_title_price.call_args.args[0]
        new_payload = fake_db.batch_save_item_basic_info.call_args.args[0]
        self.assertEqual(update_payload[0]["account_id"], "acc-item-batch-1")
        self.assertEqual(new_payload[0]["account_id"], "acc-item-batch-1")

    async def test_save_items_list_to_db_rejects_blank_canonical_account_id_before_batch_writes(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "   "
        live._safe_str = str
        live._fetch_item_details = mock.AsyncMock(
            side_effect=AssertionError("should not fetch item details without canonical account_id")
        )

        fake_db = mock.Mock()
        fake_db.get_item_info.side_effect = AssertionError(
            "should not read item info without canonical account_id"
        )
        fake_db.batch_update_item_title_price.side_effect = AssertionError(
            "should not batch update without canonical account_id"
        )
        fake_db.batch_save_item_basic_info.side_effect = AssertionError(
            "should not batch save without canonical account_id"
        )

        items_list = [
            {
                "id": "item-existing",
                "title": "Existing title",
                "price_text": "10",
                "category_id": "cat-1",
                "price": "10",
            }
        ]

        with mock.patch("db_manager.db_manager", fake_db):
            saved_count = await live.save_items_list_to_db(items_list, sync_item_details=False)

        self.assertEqual(saved_count, 0)
        fake_db.get_item_info.assert_not_called()
        fake_db.batch_update_item_title_price.assert_not_called()
        fake_db.batch_save_item_basic_info.assert_not_called()
        live._fetch_item_details.assert_not_awaited()

    async def test_get_item_specific_reply_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-item-reply-1"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_item_reply.return_value = {
            "reply_content": "hello {item_id}",
        }

        with mock.patch("db_manager.db_manager", fake_db):
            result = await live.get_item_specific_reply(
                "buyer-name",
                "buyer-id",
                "raw-message",
                item_id="item-9",
            )

        self.assertEqual(result, "hello item-9")
        fake_db.get_item_reply.assert_called_once_with("acc-item-reply-1", "item-9")

    async def test_get_item_specific_reply_rejects_default_or_blank_canonical_account_id_before_db_lookup(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live._safe_str = str

                fake_db = mock.Mock()
                fake_db.get_item_reply.side_effect = AssertionError(
                    "should not read item specific reply without canonical account_id"
                )

                with mock.patch("db_manager.db_manager", fake_db):
                    result = await live.get_item_specific_reply(
                        "buyer-name",
                        "buyer-id",
                        "raw-message",
                        item_id="item-9",
                    )

                self.assertIsNone(result)
                fake_db.get_item_reply.assert_not_called()

    async def test_auto_confirm_prefers_account_id_alias_for_secure_confirm(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-secure-confirm-1"
        live.session = object()
        live.cookies_str = "cookie-a=1"
        live.current_token = "token-a"
        live.last_token_refresh_time = 123
        live.token_refresh_interval = 456
        live._safe_str = str
        live._set_runtime_cookie_state = mock.Mock()

        captured = {}

        class _FakeSecureConfirm:
            def __init__(self, session, cookies_str, account_id, live_instance):
                captured["init_args"] = (session, cookies_str, account_id, live_instance)
                self.cookies_str = cookies_str
                self.cookies = {"cookie-a": "1"}
                self.current_token = live.current_token
                self.last_token_refresh_time = live.last_token_refresh_time
                self.token_refresh_interval = live.token_refresh_interval

            async def auto_confirm(self, order_id, item_id, retry_count):
                captured["auto_confirm_args"] = (order_id, item_id, retry_count)
                return {"success": True, "order_id": order_id}

        secure_confirm_module = types.ModuleType("secure_confirm_decrypted")
        secure_confirm_module.SecureConfirm = _FakeSecureConfirm

        with mock.patch.dict(sys.modules, {"secure_confirm_decrypted": secure_confirm_module}):
            result = await live.auto_confirm("order-confirm-1", item_id="item-confirm-1", retry_count=2)

        self.assertEqual(result, {"success": True, "order_id": "order-confirm-1"})
        self.assertEqual(
            captured["init_args"],
            (live.session, "cookie-a=1", "acc-secure-confirm-1", live),
        )
        self.assertEqual(
            captured["auto_confirm_args"],
            ("order-confirm-1", "item-confirm-1", 2),
        )
        live._set_runtime_cookie_state.assert_not_called()

    async def test_auto_confirm_rejects_blank_canonical_account_id_before_secure_confirm(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-secure-confirm-blank"
        live.account_id = "   "
        live.session = object()
        live.cookies_str = "cookie-a=1"
        live.current_token = "token-a"
        live.last_token_refresh_time = 123
        live.token_refresh_interval = 456
        live._safe_str = str
        live._set_runtime_cookie_state = mock.Mock()
        mock_logger = mock.Mock()

        class _UnexpectedSecureConfirm:
            def __init__(self, *args, **kwargs):
                raise AssertionError("missing canonical account_id should skip secure confirm construction")

        secure_confirm_module = types.ModuleType("secure_confirm_decrypted")
        secure_confirm_module.SecureConfirm = _UnexpectedSecureConfirm

        with mock.patch.dict(sys.modules, {"secure_confirm_decrypted": secure_confirm_module}), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live.auto_confirm("order-confirm-blank", item_id="item-confirm-blank", retry_count=2)

        self.assertFalse(result["success"])
        self.assertIn("canonical account_id", result["error"])
        live._set_runtime_cookie_state.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    async def test_auto_freeshipping_prefers_account_id_alias_for_secure_module(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-secure-free-1"
        live.session = object()
        live.cookies_str = "cookie-b=2"
        live.current_token = "token-b"
        live.last_token_refresh_time = 789
        live.token_refresh_interval = 654
        live._safe_str = str
        live._set_runtime_cookie_state = mock.Mock()

        captured = {}

        class _FakeSecureFreeshipping:
            def __init__(self, session, cookies_str, account_id):
                captured["init_args"] = (session, cookies_str, account_id)
                self.cookies_str = cookies_str
                self.cookies = {"cookie-b": "2"}
                self.current_token = live.current_token
                self.last_token_refresh_time = live.last_token_refresh_time
                self.token_refresh_interval = live.token_refresh_interval

            async def auto_freeshipping(self, order_id, item_id, buyer_id, retry_count):
                captured["auto_freeshipping_args"] = (order_id, item_id, buyer_id, retry_count)
                return {"success": True, "order_id": order_id}

        secure_freeshipping_module = types.ModuleType("secure_freeshipping_decrypted")
        secure_freeshipping_module.SecureFreeshipping = _FakeSecureFreeshipping

        with mock.patch.dict(sys.modules, {"secure_freeshipping_decrypted": secure_freeshipping_module}):
            result = await live.auto_freeshipping(
                "order-free-1",
                "item-free-1",
                "buyer-free-1",
                retry_count=3,
            )

        self.assertEqual(result, {"success": True, "order_id": "order-free-1"})
        self.assertEqual(
            captured["init_args"],
            (live.session, "cookie-b=2", "acc-secure-free-1"),
        )
        self.assertEqual(
            captured["auto_freeshipping_args"],
            ("order-free-1", "item-free-1", "buyer-free-1", 3),
        )
        live._set_runtime_cookie_state.assert_not_called()

    async def test_auto_freeshipping_rejects_blank_canonical_account_id_before_secure_module(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-secure-free-blank"
        live.account_id = "   "
        live.session = object()
        live.cookies_str = "cookie-b=2"
        live.current_token = "token-b"
        live.last_token_refresh_time = 789
        live.token_refresh_interval = 654
        live._safe_str = str
        live._set_runtime_cookie_state = mock.Mock()
        mock_logger = mock.Mock()

        class _UnexpectedSecureFreeshipping:
            def __init__(self, *args, **kwargs):
                raise AssertionError("missing canonical account_id should skip secure freeshipping construction")

        secure_freeshipping_module = types.ModuleType("secure_freeshipping_decrypted")
        secure_freeshipping_module.SecureFreeshipping = _UnexpectedSecureFreeshipping

        with mock.patch.dict(sys.modules, {"secure_freeshipping_decrypted": secure_freeshipping_module}), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live.auto_freeshipping(
                "order-free-blank",
                "item-free-blank",
                "buyer-free-blank",
                retry_count=3,
            )

        self.assertFalse(result["success"])
        self.assertIn("canonical account_id", result["error"])
        live._set_runtime_cookie_state.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    async def test_finalize_delivery_after_send_rejects_blank_canonical_account_id_before_side_effects(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-finalize-blank"
        live.account_id = "   "
        live.confirmed_orders = {}
        live.order_confirm_cooldown = 30
        live._safe_str = str
        live.is_auto_confirm_enabled = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip auto confirm gate")
        )
        live.auto_confirm = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip auto confirm execution")
        )
        fake_db = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._finalize_delivery_after_send(
                delivery_meta={
                    "success": True,
                    "rule_id": 9,
                    "data_card_pending_consume": True,
                    "data_reservation_id": "reservation-blank",
                },
                order_id="order-finalize-blank",
                item_id="item-finalize-blank",
            )

        self.assertFalse(result["success"])
        self.assertIn("canonical account_id", result["error"])
        fake_db.finalize_batch_data_reservation.assert_not_called()
        fake_db.consume_specific_batch_data.assert_not_called()
        fake_db.increment_delivery_times.assert_not_called()
        live.is_auto_confirm_enabled.assert_not_called()
        live.auto_confirm.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    async def test_finalize_delivery_after_send_rejects_blank_order_id_before_side_effects_even_when_skip_confirm_true(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-finalize-blank-order"
        live.account_id = "acc-finalize-blank-order-1"
        live.confirmed_orders = {}
        live.order_confirm_cooldown = 30
        live._safe_str = str
        live.is_auto_confirm_enabled = mock.Mock(
            side_effect=AssertionError("blank order_id should fail before auto confirm gate even when skip_confirm=True")
        )
        live.auto_confirm = mock.AsyncMock(
            side_effect=AssertionError("blank order_id should fail before auto confirm execution even when skip_confirm=True")
        )
        fake_db = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._finalize_delivery_after_send(
                delivery_meta={
                    "success": True,
                    "rule_id": 10,
                },
                order_id="   ",
                item_id="item-finalize-blank-order-1",
                skip_confirm=True,
            )

        self.assertFalse(result["success"])
        self.assertIn("order_id", result["error"])
        fake_db.get_order_by_id.assert_not_called()
        fake_db.finalize_batch_data_reservation.assert_not_called()
        fake_db.consume_specific_batch_data.assert_not_called()
        fake_db.increment_delivery_times.assert_not_called()
        live.is_auto_confirm_enabled.assert_not_called()
        live.auto_confirm.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("order_id", messages)

    async def test_finalize_delivery_after_send_rejects_order_outside_current_account_before_side_effects(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-finalize-foreign-order"
        live.account_id = "acc-finalize-foreign-order-1"
        live.confirmed_orders = {}
        live.order_confirm_cooldown = 30
        live._safe_str = str
        live.is_auto_confirm_enabled = mock.Mock(
            side_effect=AssertionError("foreign account order should skip auto confirm gate")
        )
        live.auto_confirm = mock.AsyncMock(
            side_effect=AssertionError("foreign account order should skip auto confirm execution")
        )
        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = None
        mock_logger = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._finalize_delivery_after_send(
                delivery_meta={
                    "success": True,
                    "rule_id": 11,
                    "data_card_pending_consume": True,
                    "data_reservation_id": "reservation-foreign-order",
                },
                order_id="order-foreign-scope-1",
                item_id="item-foreign-scope-1",
            )

        self.assertFalse(result["success"])
        self.assertIn("account scope", result["error"])
        fake_db.get_order_by_id.assert_called_once_with(
            "order-foreign-scope-1",
            account_id="acc-finalize-foreign-order-1",
        )
        fake_db.finalize_batch_data_reservation.assert_not_called()
        fake_db.consume_specific_batch_data.assert_not_called()
        fake_db.increment_delivery_times.assert_not_called()
        live.is_auto_confirm_enabled.assert_not_called()
        live.auto_confirm.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("order-foreign-scope-1", messages)

    async def test_finalize_delivery_after_send_rejects_unowned_order_before_side_effects_even_when_skip_confirm_true(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-finalize-skip-confirm-foreign-order"
        live.account_id = "acc-finalize-skip-confirm-foreign-order-1"
        live.confirmed_orders = {}
        live.order_confirm_cooldown = 30
        live._safe_str = str
        live.is_auto_confirm_enabled = mock.Mock(
            side_effect=AssertionError("foreign account order should fail before auto confirm gate even when skip_confirm=True")
        )
        live.auto_confirm = mock.AsyncMock(
            side_effect=AssertionError("foreign account order should fail before auto confirm execution even when skip_confirm=True")
        )
        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = None
        mock_logger = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._finalize_delivery_after_send(
                delivery_meta={
                    "success": True,
                    "rule_id": 13,
                },
                order_id="order-foreign-skip-confirm-1",
                item_id="item-foreign-skip-confirm-1",
                skip_confirm=True,
            )

        self.assertFalse(result["success"])
        self.assertIn("account scope", result["error"])
        fake_db.get_order_by_id.assert_called_once_with(
            "order-foreign-skip-confirm-1",
            account_id="acc-finalize-skip-confirm-foreign-order-1",
        )
        fake_db.finalize_batch_data_reservation.assert_not_called()
        fake_db.consume_specific_batch_data.assert_not_called()
        fake_db.increment_delivery_times.assert_not_called()
        live.is_auto_confirm_enabled.assert_not_called()
        live.auto_confirm.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("order-foreign-skip-confirm-1", messages)

    async def test_finalize_delivery_after_send_scopes_confirm_cooldown_by_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-finalize-scope"
        live.account_id = "acc-finalize-scope-1"
        live.confirmed_orders = {
            ("acc-foreign-scope-1", "order-finalize-shared-1"): 995.0,
        }
        live.order_confirm_cooldown = 30
        live._safe_str = str
        live.is_auto_confirm_enabled = mock.Mock(return_value=True)
        live.auto_confirm = mock.AsyncMock(return_value={"success": True})
        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-finalize-shared-1",
            "account_id": "acc-finalize-scope-1",
        }

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("XianyuAutoAsync.time.time", return_value=1000.0):
            result = await live._finalize_delivery_after_send(
                delivery_meta={"success": True},
                order_id="order-finalize-shared-1",
                item_id="item-finalize-shared-1",
            )

        self.assertTrue(result["success"])
        live.auto_confirm.assert_awaited_once_with(
            "order-finalize-shared-1",
            "item-finalize-shared-1",
        )
        fake_db.get_order_by_id.assert_called_once_with(
            "order-finalize-shared-1",
            account_id="acc-finalize-scope-1",
        )
        self.assertEqual(
            {
                ("acc-foreign-scope-1", "order-finalize-shared-1"): 995.0,
                ("acc-finalize-scope-1", "order-finalize-shared-1"): 1000.0,
            },
            live.confirmed_orders,
        )

    async def test_finalize_delivery_after_send_respects_same_account_confirm_cooldown_scope(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-finalize-cooldown"
        live.account_id = "acc-finalize-cooldown-1"
        live.confirmed_orders = {
            ("acc-finalize-cooldown-1", "order-finalize-cooldown-1"): 995.0,
        }
        live.order_confirm_cooldown = 30
        live._safe_str = str
        live.is_auto_confirm_enabled = mock.Mock(return_value=True)
        live.auto_confirm = mock.AsyncMock(return_value={"success": True})
        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-finalize-cooldown-1",
            "account_id": "acc-finalize-cooldown-1",
        }

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("XianyuAutoAsync.time.time", return_value=1000.0):
            result = await live._finalize_delivery_after_send(
                delivery_meta={"success": True},
                order_id="order-finalize-cooldown-1",
                item_id="item-finalize-cooldown-1",
            )

        self.assertTrue(result["success"])
        live.auto_confirm.assert_not_awaited()
        fake_db.get_order_by_id.assert_called_once_with(
            "order-finalize-cooldown-1",
            account_id="acc-finalize-cooldown-1",
        )
        self.assertEqual(
            {("acc-finalize-cooldown-1", "order-finalize-cooldown-1"): 995.0},
            live.confirmed_orders,
        )

    async def test_send_notification_prefers_account_id_alias_for_dispatch(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-notify-1"
        live.notification_lock = asyncio.Lock()
        live.last_notification_time = {}
        live.pending_notification_keys = set()
        live.notification_cooldown = 300
        live._safe_str = str

        dispatch_mock = mock.AsyncMock(return_value=True)
        render_mock = mock.Mock(return_value="rendered-message")

        with mock.patch("XianyuAutoAsync.dispatch_account_notifications", new=dispatch_mock), \
             mock.patch("XianyuAutoAsync.render_notification_template", new=render_mock):
            await live.send_notification(
                send_user_name="buyer-name",
                send_user_id="buyer-1",
                send_message="hello world",
                item_id="item-1",
                chat_id="chat-1",
            )

        render_mock.assert_called_once_with(
            "message",
            account_id="acc-notify-1",
            buyer_name="buyer-name",
            buyer_id="buyer-1",
            item_id="item-1",
            chat_id="chat-1",
            message="hello world",
            time=mock.ANY,
        )
        dispatch_mock.assert_awaited_once_with(
            "acc-notify-1",
            "rendered-message",
            title="接收消息通知",
            notification_type="message",
        )

    async def test_send_notification_rejects_default_or_blank_canonical_account_id_before_dispatch(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live.notification_lock = asyncio.Lock()
                live.last_notification_time = {}
                live.pending_notification_keys = set()
                live.notification_cooldown = 300
                live._safe_str = str

                dispatch_mock = mock.AsyncMock(
                    side_effect=AssertionError("should not dispatch notification without canonical account_id")
                )
                render_mock = mock.Mock(
                    side_effect=AssertionError("should not render notification without canonical account_id")
                )

                with mock.patch("XianyuAutoAsync.dispatch_account_notifications", new=dispatch_mock), \
                     mock.patch("XianyuAutoAsync.render_notification_template", new=render_mock):
                    await live.send_notification(
                        send_user_name="buyer-name",
                        send_user_id="buyer-1",
                        send_message="hello world",
                        item_id="item-1",
                        chat_id="chat-1",
                    )

                render_mock.assert_not_called()
                dispatch_mock.assert_not_awaited()
                self.assertEqual({}, live.last_notification_time)
                self.assertEqual(set(), live.pending_notification_keys)

    async def test_send_delivery_failure_notification_prefers_account_id_alias_for_dispatch(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-notify-delivery-1"
        live._safe_str = str

        dispatch_mock = mock.AsyncMock(return_value=True)
        render_mock = mock.Mock(return_value="rendered-delivery-message")

        with mock.patch("XianyuAutoAsync.dispatch_account_notifications", new=dispatch_mock), \
             mock.patch("XianyuAutoAsync.render_notification_template", new=render_mock):
            await live.send_delivery_failure_notification(
                send_user_name="buyer-name",
                send_user_id="buyer-2",
                item_id="item-2",
                error_message="failed",
                chat_id="chat-2",
            )

        render_mock.assert_called_once_with(
            "delivery",
            account_id="acc-notify-delivery-1",
            buyer_name="buyer-name",
            buyer_id="buyer-2",
            item_id="item-2",
            chat_id="chat-2",
            result="failed",
            time=mock.ANY,
        )
        dispatch_mock.assert_awaited_once_with(
            "acc-notify-delivery-1",
            "rendered-delivery-message",
            title="自动发货通知",
            notification_type="delivery",
        )

    async def test_send_delivery_failure_notification_rejects_default_or_blank_canonical_account_id_before_dispatch(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live._safe_str = str

                dispatch_mock = mock.AsyncMock(
                    side_effect=AssertionError("should not dispatch delivery notification without canonical account_id")
                )
                render_mock = mock.Mock(
                    side_effect=AssertionError("should not render delivery notification without canonical account_id")
                )

                with mock.patch("XianyuAutoAsync.dispatch_account_notifications", new=dispatch_mock), \
                     mock.patch("XianyuAutoAsync.render_notification_template", new=render_mock):
                    await live.send_delivery_failure_notification(
                        send_user_name="buyer-name",
                        send_user_id="buyer-2",
                        item_id="item-2",
                        error_message="failed",
                        chat_id="chat-2",
                    )

                render_mock.assert_not_called()
                dispatch_mock.assert_not_awaited()

    async def test_send_token_refresh_notification_prefers_account_id_alias_for_default_branch(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-token-notify-1"
        live.notification_lock = asyncio.Lock()
        live.last_notification_time = {}
        live.pending_notification_keys = set()
        live.notification_cooldown = 300
        live.token_refresh_notification_cooldown = 10800
        live.message_stream_notification_cooldown = 600
        live._safe_str = str
        live._is_normal_token_expiry = mock.Mock(return_value=False)
        live._is_token_related_error = mock.Mock(return_value=False)

        dispatch_mock = mock.AsyncMock(return_value=True)
        render_mock = mock.Mock(return_value="rendered-token-message")

        with mock.patch("XianyuAutoAsync.dispatch_account_notifications", new=dispatch_mock), \
             mock.patch("XianyuAutoAsync.render_notification_template", new=render_mock):
            await live.send_token_refresh_notification(
                error_message="token refresh failed",
                notification_type="token_refresh",
            )

        render_mock.assert_called_once_with(
            "token_refresh",
            account_id="acc-token-notify-1",
            time=mock.ANY,
            error_message="token refresh failed",
            verification_url="无",
        )
        dispatch_mock.assert_awaited_once_with(
            "acc-token-notify-1",
            "rendered-token-message",
            title="闲鱼管理系统通知",
            notification_type="token_refresh",
            attachment_path=None,
        )

    async def test_send_token_refresh_notification_rejects_default_or_blank_canonical_account_id_before_dispatch(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live.notification_lock = asyncio.Lock()
                live.last_notification_time = {}
                live.pending_notification_keys = set()
                live.notification_cooldown = 300
                live.token_refresh_notification_cooldown = 10800
                live.message_stream_notification_cooldown = 600
                live._safe_str = str
                live._is_normal_token_expiry = mock.Mock(return_value=False)
                live._is_token_related_error = mock.Mock(return_value=False)

                dispatch_mock = mock.AsyncMock(
                    side_effect=AssertionError("should not dispatch token refresh notification without canonical account_id")
                )
                render_mock = mock.Mock(
                    side_effect=AssertionError("should not render token refresh notification without canonical account_id")
                )

                with mock.patch("XianyuAutoAsync.dispatch_account_notifications", new=dispatch_mock), \
                     mock.patch("XianyuAutoAsync.render_notification_template", new=render_mock):
                    await live.send_token_refresh_notification(
                        error_message="token refresh failed",
                        notification_type="token_refresh",
                    )

                render_mock.assert_not_called()
                dispatch_mock.assert_not_awaited()
                self.assertEqual({}, live.last_notification_time)
                self.assertEqual(set(), live.pending_notification_keys)

    async def test_send_token_refresh_notification_prefers_account_id_alias_for_slider_success_branch(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-token-notify-2"
        live.notification_lock = asyncio.Lock()
        live.last_notification_time = {}
        live.pending_notification_keys = set()
        live.notification_cooldown = 300
        live.token_refresh_notification_cooldown = 10800
        live.message_stream_notification_cooldown = 600
        live._safe_str = str
        live._is_normal_token_expiry = mock.Mock(return_value=False)
        live._is_token_related_error = mock.Mock(return_value=False)

        dispatch_mock = mock.AsyncMock(return_value=True)
        render_mock = mock.Mock(return_value="rendered-slider-message")

        with mock.patch("XianyuAutoAsync.dispatch_account_notifications", new=dispatch_mock), \
             mock.patch("XianyuAutoAsync.render_notification_template", new=render_mock):
            await live.send_token_refresh_notification(
                error_message="slider ok",
                notification_type="slider_success",
            )

        render_mock.assert_called_once_with(
            "slider_success",
            account_id="acc-token-notify-2",
            time=mock.ANY,
            status_text="cookies已自动更新到数据库",
        )
        dispatch_mock.assert_awaited_once_with(
            "acc-token-notify-2",
            "rendered-slider-message",
            title="闲鱼管理系统通知",
            notification_type="slider_success",
            attachment_path=None,
        )

    async def test_handle_captcha_verification_prefers_account_id_alias_for_slider_runtime(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-captcha-runtime-1"
        live.cookies_str = "unb=user1; cookie2=v2"
        live.proxy_config = {}
        live._safe_str = str
        live.is_manual_refresh_active = mock.Mock(return_value=False)
        live.send_token_refresh_notification = mock.AsyncMock()
        live.connection_state = XianyuAutoAsync.ConnectionState.CONNECTED
        live.ws = types.SimpleNamespace(closed=False)

        captured = {}
        page = mock.Mock()
        context = mock.Mock()
        context.browser = mock.Mock()
        lease = self._build_runtime_lease(
            "acc-captcha-runtime-1",
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime_sync=mock.Mock(return_value=lease),
            get_fresh_page_sync=mock.Mock(return_value=(page, context)),
            release_runtime_sync=mock.Mock(return_value=None),
        )

        class _FakeSlider:
            def __init__(self, **kwargs):
                captured["init_kwargs"] = kwargs
                self.last_login_error = "slider failed"
                self.browser = None
                self.context = None
                self.page = None

            def build_managed_runtime_request(self, **kwargs):
                captured["request_kwargs"] = kwargs
                return {
                    "browser_features": {"feature": "managed"},
                    "profile_id": "captcha-profile",
                    "launch_options": {"args": ["--managed"]},
                    "use_persistent_context": True,
                    "profile_dir": os.path.join(
                        os.getcwd(),
                        "browser_data",
                        "user_acc-captcha-runtime-1",
                    ),
                }

            def attach_managed_runtime(self, **kwargs):
                captured["attach_kwargs"] = kwargs
                self.browser = kwargs.get("browser")
                self.context = kwargs.get("context")
                self.page = kwargs.get("page")

            def run(self, verification_url, **kwargs):
                captured["verification_url"] = verification_url
                captured["run_kwargs"] = kwargs
                return False, {}

            async def _run_sync_method_on_fresh_thread(self, func, *args, **kwargs):
                return func(*args, **kwargs)

        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"show_browser": False}

        with mock.patch.object(XianyuAutoAsync, "db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "account_browser_runtime_manager", new=runtime_manager), \
             mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()) as log_captcha_event, \
             mock.patch.object(XianyuAutoAsync.os, "getenv", return_value=""), \
             mock.patch.dict(sys.modules, {
                  "utils.xianyu_slider_stealth": types.SimpleNamespace(XianyuSliderStealth=_FakeSlider),
              }):
            result = await live._handle_captcha_verification(
                {"data": {"url": "https://verify.example.com/slider"}}
            )

        self.assertIsNone(result)
        live.is_manual_refresh_active.assert_called_once_with(
            "acc-captcha-runtime-1",
            allow_handoff_recovery=True,
        )
        fake_db.get_cookie_details.assert_called_once_with("acc-captcha-runtime-1")
        self.assertEqual(
            captured["request_kwargs"],
            {"account_id": "acc-captcha-runtime-1", "purpose": "token_refresh_slider"},
        )
        runtime_manager.acquire_runtime_sync.assert_called_once_with(
            "acc-captcha-runtime-1",
            "token_refresh_slider",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_manager.get_fresh_page_sync.assert_called_once_with(lease)
        runtime_manager.release_runtime_sync.assert_called_once_with(
            lease,
            reason="token_refresh_slider_completed",
        )
        self.assertIs(captured["attach_kwargs"]["lease"], lease)
        self.assertIs(captured["attach_kwargs"]["runtime"], lease.runtime)
        self.assertIs(captured["attach_kwargs"]["browser"], context.browser)
        self.assertIs(captured["attach_kwargs"]["context"], context)
        self.assertIs(captured["attach_kwargs"]["page"], page)
        self.assertEqual(captured["init_kwargs"]["user_id"], "acc-captcha-runtime-1")
        self.assertTrue(captured["init_kwargs"]["use_account_persistent_profile"])
        self.assertEqual(captured["verification_url"], "https://verify.example.com/slider")
        self.assertTrue(captured["run_kwargs"]["require_managed_runtime"])
        logged_accounts = [
            call.kwargs.get("account_id", call.args[0] if call.args else None)
            for call in log_captcha_event.call_args_list
        ]
        self.assertIn("acc-captcha-runtime-1", logged_accounts)
        self.assertNotIn("legacy-cookie-name", logged_accounts)
        live.send_token_refresh_notification.assert_not_awaited()

    async def test_handle_captcha_verification_rejects_blank_canonical_account_id_before_manual_gate(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "   "
        live.cookies_str = "unb=user1; cookie2=v2"
        live.proxy_config = {}
        live._safe_str = str
        live.is_manual_refresh_active = mock.Mock(return_value=False)
        live.send_token_refresh_notification = mock.AsyncMock()
        live.connection_state = XianyuAutoAsync.ConnectionState.CONNECTED
        live.ws = types.SimpleNamespace(closed=False)

        class _FakeSlider:
            def __init__(self, **kwargs):
                raise AssertionError("should not construct slider runtime without canonical account_id")

        fake_db = mock.Mock()
        fake_db.get_cookie_details.side_effect = AssertionError(
            "should not load cookie details without canonical account_id"
        )

        with mock.patch.object(XianyuAutoAsync, "db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()), \
             mock.patch.object(XianyuAutoAsync.os, "getenv", return_value=""), \
             mock.patch.dict(sys.modules, {
                 "utils.xianyu_slider_stealth": types.SimpleNamespace(XianyuSliderStealth=_FakeSlider),
             }):
            result = await live._handle_captcha_verification(
                {"data": {"url": "https://verify.example.com/slider"}}
            )

        self.assertIsNone(result)
        live.is_manual_refresh_active.assert_not_called()
        fake_db.get_cookie_details.assert_not_called()
        live.send_token_refresh_notification.assert_not_awaited()

    async def test_handle_captcha_verification_avoids_legacy_current_account_id_fallback(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-captcha-fallback"
        live.account_id = "acc-captcha-canonical-only"
        live.cookies_str = "unb=user1; cookie2=v2"
        live.proxy_config = {}
        live._safe_str = str
        live._current_account_id = mock.Mock(
            side_effect=AssertionError("should not read legacy current account fallback")
        )
        live.is_manual_refresh_active = mock.Mock(return_value=True)

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()) as log_captcha_event:
            result = await live._handle_captcha_verification(
                {"data": {"url": "https://verify.example.com/slider"}}
            )

        self.assertIsNone(result)
        live.is_manual_refresh_active.assert_called_once_with(
            "acc-captcha-canonical-only",
            allow_handoff_recovery=True,
        )
        logged_accounts = [
            call.kwargs.get("account_id", call.args[0] if call.args else None)
            for call in log_captcha_event.call_args_list
        ]
        self.assertIn("acc-captcha-canonical-only", logged_accounts)

    async def test_handle_captcha_verification_prefers_account_id_alias_for_manual_refresh_skip_log(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-captcha-manual-1"
        live.cookies_str = "unb=user1; cookie2=v2"
        live.proxy_config = {}
        live._safe_str = str
        live.is_manual_refresh_active = mock.Mock(return_value=True)
        live.send_token_refresh_notification = mock.AsyncMock()
        live.connection_state = XianyuAutoAsync.ConnectionState.CONNECTED
        live.ws = types.SimpleNamespace(closed=False)

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()) as log_captcha_event:
            result = await live._handle_captcha_verification(
                {"data": {"url": "https://verify.example.com/slider"}}
            )

        self.assertIsNone(result)
        live.is_manual_refresh_active.assert_called_once_with(
            "acc-captcha-manual-1",
            allow_handoff_recovery=True,
        )
        log_captcha_event.assert_called_once_with(
            "acc-captcha-manual-1",
            "手动刷新进行中，取消自动滑块处理",
            None,
            "自动滑块处理已跳过",
        )

    async def test_handle_captcha_verification_prefers_account_id_alias_for_recovery_state_success_branch(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-captcha-success-1"
        live.cookies_str = "unb=user1; cookie2=v2"
        live.cookies = {}
        live.proxy_config = {}
        live._safe_str = str
        live.is_manual_refresh_active = mock.Mock(return_value=False)
        live.send_token_refresh_notification = mock.AsyncMock()
        live.connection_state = XianyuAutoAsync.ConnectionState.CONNECTED
        live.ws = types.SimpleNamespace(closed=False)
        live.protected_merge_cookie_dicts = mock.Mock(
            return_value=self._build_merge_result(
                {
                    "unb": "user1",
                    "cookie2": "v2",
                    "x5sec": "x5-token",
                }
            )
        )
        live._log_protected_merge_event = mock.Mock()
        live._log_cookie_merge_summary = mock.Mock()
        live.update_config_cookies = mock.AsyncMock()
        live._set_runtime_cookie_state = mock.Mock()
        live._mark_slider_success_recovery = mock.Mock()
        live._mark_pending_slider_success_notice = mock.Mock()
        live.get_qr_login_grace = mock.Mock(return_value={"captcha_buffer_used": False})
        page = mock.Mock()
        context = mock.Mock()
        context.browser = mock.Mock()
        lease = self._build_runtime_lease(
            "acc-captcha-success-1",
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime_sync=mock.Mock(return_value=lease),
            get_fresh_page_sync=mock.Mock(return_value=(page, context)),
            release_runtime_sync=mock.Mock(return_value=None),
        )

        class _FakeSlider:
            def __init__(self, **kwargs):
                self.last_login_error = ""
                self.browser = None
                self.context = None
                self.page = None

            def build_managed_runtime_request(self, **kwargs):
                return {
                    "browser_features": {"feature": "managed"},
                    "profile_id": "captcha-profile-success",
                    "launch_options": {"args": ["--managed"]},
                    "use_persistent_context": True,
                    **kwargs,
                }

            def attach_managed_runtime(self, **kwargs):
                self.browser = kwargs.get("browser")
                self.context = kwargs.get("context")
                self.page = kwargs.get("page")

            def run(self, verification_url, **kwargs):
                return True, {
                    "unb": "user1",
                    "cookie2": "v2",
                    "x5sec": "x5-token",
                }

            async def _run_sync_method_on_fresh_thread(self, func, *args, **kwargs):
                return func(*args, **kwargs)

        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"show_browser": False}

        with mock.patch.object(XianyuAutoAsync, "db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "account_browser_runtime_manager", new=runtime_manager), \
             mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()) as log_captcha_event, \
             mock.patch.object(XianyuAutoAsync.os, "getenv", return_value=""), \
             mock.patch.object(XianyuAutoAsync.XianyuLive, "clear_password_login_failure_backoff") as clear_backoff_mock, \
             mock.patch.dict(sys.modules, {
                  "utils.xianyu_slider_stealth": types.SimpleNamespace(XianyuSliderStealth=_FakeSlider),
             }):
            result = await live._handle_captcha_verification(
                {"data": {"url": "https://verify.example.com/slider-success"}}
            )

        self.assertIsNotNone(result)
        live.get_qr_login_grace.assert_called_once_with("acc-captcha-success-1")
        live._log_protected_merge_event.assert_called_once()
        self.assertEqual(
            live._log_protected_merge_event.call_args.args[0],
            "slider_post_qr_protected_merge",
        )
        clear_backoff_mock.assert_called_once_with("acc-captcha-success-1")
        log_captcha_event.assert_any_call(
            "acc-captcha-success-1",
            "滑块验证成功并自动更新数据库",
            True,
            mock.ANY,
        )
        live.send_token_refresh_notification.assert_not_awaited()

    def test_create_risk_log_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-risk-log-1"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.add_risk_control_log.return_value = 321

        with mock.patch.object(XianyuAutoAsync, "db_manager", fake_db):
            log_id = live._create_risk_log(
                event_type="slider_captcha",
                event_description="risk detected",
            )

        self.assertEqual(log_id, 321)
        fake_db.add_risk_control_log.assert_called_once()
        self.assertEqual(
            fake_db.add_risk_control_log.call_args.kwargs["account_id"],
            "acc-risk-log-1",
        )

    def test_create_risk_log_skips_db_write_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-risk-log-blank"
        live.account_id = "   "
        live._safe_str = str
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.add_risk_control_log.side_effect = AssertionError(
            "missing canonical account_id should skip risk log db writes"
        )

        with mock.patch.object(XianyuAutoAsync, "db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            log_id = live._create_risk_log(
                event_type="slider_captcha",
                event_description="should not persist",
            )

        self.assertIsNone(log_id)
        fake_db.add_risk_control_log.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    async def test_refresh_token_impl_prefers_account_id_alias_for_recovery_gates(self):
        class _FakeJsonResponse:
            def __init__(self, payload):
                self.status = 200
                self.headers = {}
                self.payload = payload

            async def json(self, content_type=None):
                return self.payload

        class _FakeSession:
            def __init__(self, response):
                self.response = response
                self.calls = []

            def post(self, url, params=None, data=None, headers=None, timeout=None, **kwargs):
                self.calls.append(
                    {
                        "url": url,
                        "params": params,
                        "data": data,
                        "headers": headers,
                        "timeout": timeout,
                        "kwargs": kwargs,
                    }
                )
                return XianyuAsyncBrowserRuntimeTest._FakeAsyncPostContext(self.response)

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-token-gates-1"
        live.cookies_str = "unb=user1; _m_h5_tk=token_123; cookie2=v2"
        live.device_id = "device-token-gates-1"
        live._safe_str = str
        live.max_captcha_verification_count = 3
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live._skip_db_cookie_reload_for_token_refresh = True
        live.restarted_in_browser_refresh = False
        live._extract_set_cookie_updates = mock.Mock(return_value={})
        live._need_captcha_verification = mock.Mock(return_value=True)
        live.get_qr_login_grace = mock.Mock(return_value={"captcha_buffer_used": True})
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live.is_manual_refresh_active = mock.Mock(return_value=True)
        live._clear_pending_slider_success_notice = mock.Mock()
        live.send_token_refresh_notification = mock.AsyncMock()
        live.create_session = mock.AsyncMock()
        live.session = _FakeSession(
            _FakeJsonResponse(
                {
                    "ret": ["FAIL_SYS::risk"],
                    "data": {"url": "https://verify.example.com/token-refresh"},
                }
            )
        )

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()) as log_captcha_event:
            result = await live._refresh_token_impl()

        self.assertIsNone(result)
        live.get_qr_login_grace.assert_called_once_with("acc-token-gates-1")
        live.get_manual_refresh_state.assert_called_once_with("acc-token-gates-1")
        live.is_manual_refresh_active.assert_called_once_with(
            "acc-token-gates-1",
            allow_handoff_recovery=True,
        )
        live._clear_pending_slider_success_notice.assert_called_once_with("手动刷新进行中")
        logged_accounts = [
            call.kwargs.get("account_id", call.args[0] if call.args else None)
            for call in log_captcha_event.call_args_list
        ]
        self.assertIn("acc-token-gates-1", logged_accounts)
        self.assertNotIn("legacy-cookie-name", logged_accounts)
        live.send_token_refresh_notification.assert_not_awaited()

    async def test_refresh_token_impl_recovery_gate_logs_account_id_alias_instead_of_stale_cookie_id(self):
        class _FakeJsonResponse:
            def __init__(self, payload):
                self.status = 200
                self.headers = {}
                self.payload = payload

            async def json(self, content_type=None):
                return self.payload

        class _FakeSession:
            def __init__(self, response):
                self.response = response

            def post(self, url, params=None, data=None, headers=None, timeout=None, **kwargs):
                return XianyuAsyncBrowserRuntimeTest._FakeAsyncPostContext(self.response)

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-token-gates-log-1"
        live.cookies_str = "unb=user1; _m_h5_tk=token_123; cookie2=v2"
        live.device_id = "device-token-gates-log-1"
        live._safe_str = str
        live.max_captcha_verification_count = 3
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live._skip_db_cookie_reload_for_token_refresh = True
        live.restarted_in_browser_refresh = False
        live._extract_set_cookie_updates = mock.Mock(return_value={})
        live._need_captcha_verification = mock.Mock(return_value=True)
        live.get_qr_login_grace = mock.Mock(return_value={"captcha_buffer_used": True})
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live.is_manual_refresh_active = mock.Mock(return_value=True)
        live._clear_pending_slider_success_notice = mock.Mock()
        live.send_token_refresh_notification = mock.AsyncMock()
        live.create_session = mock.AsyncMock()
        live.session = _FakeSession(
            _FakeJsonResponse(
                {
                    "ret": ["FAIL_SYS::risk"],
                    "data": {"url": "https://verify.example.com/token-refresh"},
                }
            )
        )
        mock_logger = mock.Mock()

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._refresh_token_impl()

        self.assertIsNone(result)
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("acc-token-gates-log-1", messages)
        self.assertNotIn("legacy-cookie-name", messages)

    async def test_refresh_token_impl_prefers_account_id_alias_when_clearing_recovery_state(self):
        class _FakeJsonResponse:
            def __init__(self, payload):
                self.status = 200
                self.headers = {}
                self.payload = payload

            async def json(self, content_type=None):
                return self.payload

        class _FakeSession:
            def __init__(self, response):
                self.response = response

            def post(self, url, params=None, data=None, headers=None, timeout=None, **kwargs):
                return XianyuAsyncBrowserRuntimeTest._FakeAsyncPostContext(self.response)

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-token-success-1"
        live.cookies_str = "unb=user1; _m_h5_tk=token_123; cookie2=v2"
        live.device_id = "device-token-success-1"
        live._safe_str = str
        live.max_captcha_verification_count = 3
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live._skip_db_cookie_reload_for_token_refresh = True
        live.restarted_in_browser_refresh = False
        live._extract_set_cookie_updates = mock.Mock(return_value={})
        live._consume_pending_slider_success_notice = mock.Mock(return_value=None)
        live.pending_slider_success_notice = None
        live.clear_qr_login_grace = mock.Mock()
        live.clear_init_auth_failure_state = mock.Mock()
        live.send_token_refresh_notification = mock.AsyncMock()
        live.create_session = mock.AsyncMock()
        live.connection_state = None
        live.ws = None
        live.session = _FakeSession(
            _FakeJsonResponse(
                {
                    "ret": ["SUCCESS::调用成功"],
                    "data": {"accessToken": "token-new-1"},
                }
            )
        )

        result = await live._refresh_token_impl()

        self.assertEqual(result, "token-new-1")
        live.clear_qr_login_grace.assert_called_once_with("acc-token-success-1")
        live.clear_init_auth_failure_state.assert_called_once_with("acc-token-success-1")
        live.send_token_refresh_notification.assert_not_awaited()

    async def test_refresh_token_impl_success_logs_account_id_alias_instead_of_stale_cookie_id(self):
        class _FakeJsonResponse:
            def __init__(self, payload):
                self.status = 200
                self.headers = {}
                self.payload = payload

            async def json(self, content_type=None):
                return self.payload

        class _FakeSession:
            def __init__(self, response):
                self.response = response

            def post(self, url, params=None, data=None, headers=None, timeout=None, **kwargs):
                return XianyuAsyncBrowserRuntimeTest._FakeAsyncPostContext(self.response)

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-token-success-log-1"
        live.cookies_str = "unb=user1; _m_h5_tk=token_123; cookie2=v2"
        live.device_id = "device-token-success-log-1"
        live._safe_str = str
        live.max_captcha_verification_count = 3
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live._skip_db_cookie_reload_for_token_refresh = True
        live.restarted_in_browser_refresh = False
        live._extract_set_cookie_updates = mock.Mock(return_value={})
        live._apply_response_cookie_updates = mock.AsyncMock()
        live._consume_pending_slider_success_notice = mock.Mock(return_value=None)
        live.pending_slider_success_notice = None
        live.clear_qr_login_grace = mock.Mock()
        live.clear_init_auth_failure_state = mock.Mock()
        live.send_token_refresh_notification = mock.AsyncMock()
        live.create_session = mock.AsyncMock()
        live.connection_state = None
        live.ws = None
        live.session = _FakeSession(
            _FakeJsonResponse(
                {
                    "ret": ["SUCCESS::调用成功"],
                    "data": {"accessToken": "token-new-log-1"},
                }
            )
        )
        mock_logger = mock.Mock()

        with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._refresh_token_impl()

        self.assertEqual(result, "token-new-log-1")
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("acc-token-success-log-1", messages)
        self.assertNotIn("legacy-cookie-name", messages)

    async def test_refresh_token_impl_rejects_blank_canonical_account_id_before_cookie_reload(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-token-blank"
        live.account_id = "   "
        live._safe_str = str
        live.max_captcha_verification_count = 3
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live._skip_db_cookie_reload_for_token_refresh = False
        live.restarted_in_browser_refresh = False
        live._reload_latest_cookies_from_db = mock.Mock(
            side_effect=AssertionError("should not reload cookies without canonical account_id")
        )
        live.create_session = mock.AsyncMock(
            side_effect=AssertionError("should not create session without canonical account_id")
        )
        live.send_token_refresh_notification = mock.AsyncMock()
        live._clear_pending_slider_success_notice = mock.Mock()

        result = await live._refresh_token_impl()

        self.assertIsNone(result)
        live._reload_latest_cookies_from_db.assert_not_called()
        live.create_session.assert_not_awaited()
        live.send_token_refresh_notification.assert_not_awaited()
        live._clear_pending_slider_success_notice.assert_not_called()
        self.assertEqual(live.last_token_refresh_status, "missing_account_id")

    async def test_refresh_token_impl_exception_logs_account_id_alias_instead_of_stale_cookie_id(self):
        class _ExplodingSession:
            def post(self, *args, **kwargs):
                raise RuntimeError("boom")

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-token-exception-log"
        live.account_id = "acc-token-exception-log"
        live.cookies_str = "unb=user1; _m_h5_tk=token_123; cookie2=v2"
        live.device_id = "device-token-exception-log"
        live._safe_str = str
        live.max_captcha_verification_count = 3
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live._skip_db_cookie_reload_for_token_refresh = True
        live.restarted_in_browser_refresh = False
        live._clear_pending_slider_success_notice = mock.Mock()
        live.send_token_refresh_notification = mock.AsyncMock()
        live.create_session = mock.AsyncMock()
        live.session = _ExplodingSession()
        live.connection_state = None
        live.ws = None
        mock_logger = mock.Mock()

        with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._refresh_token_impl()

        self.assertIsNone(result)
        live.send_token_refresh_notification.assert_awaited_once_with(
            "Token刷新异常: boom",
            "token_refresh_exception",
        )
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("acc-token-exception-log", messages)
        self.assertNotIn("legacy-token-exception-log", messages)

    def test_refresh_token_impl_prefers_account_id_alias_for_token_expired_risk_meta(self):
        source = Path("XianyuAutoAsync.py").read_text(encoding="utf-8-sig")

        self.assertIn("mark_stale_risk_control_logs_failed(", source)
        self.assertIn("account_id=current_account_id", source)
        self.assertIn("result_code='token_expired_detected'", source)
        self.assertIn(
            "extra={'expire_type': expire_type, 'account_id': current_account_id}",
            source,
        )

    def test_refresh_token_impl_prefers_account_id_alias_for_token_expired_update_risk_meta(self):
        source = Path("XianyuAutoAsync.py").read_text(encoding="utf-8-sig")

        self.assertIn(
            "result_code='token_refresh_recovered' if refresh_success else 'token_refresh_recovery_failed'",
            source,
        )
        self.assertIn(
            "extra={'account_id': current_account_id, 'expire_type': expire_type}",
            source,
        )

    async def test_refresh_token_impl_prefers_account_id_alias_for_slider_captcha_detected_risk_meta(self):
        class _FakeJsonResponse:
            def __init__(self, payload):
                self.status = 200
                self.headers = {}
                self.payload = payload

            async def json(self, content_type=None):
                return self.payload

        class _FakeSession:
            def __init__(self, response):
                self.response = response

            def post(self, url, params=None, data=None, headers=None, timeout=None, **kwargs):
                return XianyuAsyncBrowserRuntimeTest._FakeAsyncPostContext(self.response)

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-slider-detected"
        live.account_id = "acc-slider-detected-1"
        live.cookies_str = "unb=user1; _m_h5_tk=token_123; cookie2=v2"
        live.device_id = "device-slider-detected-1"
        live._safe_str = str
        live.max_captcha_verification_count = 3
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live._skip_db_cookie_reload_for_token_refresh = True
        live.restarted_in_browser_refresh = False
        live._extract_set_cookie_updates = mock.Mock(return_value={})
        live._need_captcha_verification = mock.Mock(return_value=True)
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live.is_manual_refresh_active = mock.Mock(return_value=False)
        live._clear_pending_slider_success_notice = mock.Mock()
        live._new_risk_session_id = mock.Mock(return_value="risk-session-detected")
        live._create_risk_log = mock.Mock(return_value=51)
        live._update_risk_log = mock.Mock()
        live._handle_captcha_verification = mock.AsyncMock(return_value=None)
        live.send_token_refresh_notification = mock.AsyncMock()
        live.create_session = mock.AsyncMock()
        live.session = _FakeSession(
            _FakeJsonResponse(
                {
                    "ret": ["FAIL_SYS::risk"],
                    "data": {"url": "https://verify.example.com/slider-detected"},
                }
            )
        )

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()):
            result = await live._refresh_token_impl(allow_password_login_recovery=False)

        self.assertIsNone(result)
        live._create_risk_log.assert_called_once()
        event_meta = live._create_risk_log.call_args.kwargs["event_meta"]
        self.assertEqual(event_meta["account_id"], "acc-slider-detected-1")
        self.assertNotIn("cookie_id", event_meta)

    async def test_refresh_token_impl_prefers_account_id_alias_for_slider_captcha_success_risk_meta(self):
        class _FakeJsonResponse:
            def __init__(self, payload):
                self.status = 200
                self.headers = {}
                self.payload = payload

            async def json(self, content_type=None):
                return self.payload

        class _FakeSession:
            def __init__(self, response):
                self.response = response

            def post(self, url, params=None, data=None, headers=None, timeout=None, **kwargs):
                return XianyuAsyncBrowserRuntimeTest._FakeAsyncPostContext(self.response)

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-slider-success"
        live.account_id = "acc-slider-success-1"
        live.cookies_str = "unb=user1; _m_h5_tk=token_123; cookie2=v2"
        live.device_id = "device-slider-success-1"
        live._safe_str = str
        live.max_captcha_verification_count = 3
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live._skip_db_cookie_reload_for_token_refresh = True
        live.restarted_in_browser_refresh = False
        live.post_slider_token_retry_delay = (0, 0)
        live._extract_set_cookie_updates = mock.Mock(return_value={})
        live._need_captcha_verification = mock.Mock(return_value=True)
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live.is_manual_refresh_active = mock.Mock(return_value=False)
        live._clear_pending_slider_success_notice = mock.Mock()
        live._new_risk_session_id = mock.Mock(return_value="risk-session-success")
        live._create_risk_log = mock.Mock(return_value=52)
        live._update_risk_log = mock.Mock()
        live._reload_latest_cookies_from_db = mock.Mock()
        live.send_token_refresh_notification = mock.AsyncMock()
        live.create_session = mock.AsyncMock()
        live.session = _FakeSession(
            _FakeJsonResponse(
                {
                    "ret": ["FAIL_SYS::risk"],
                    "data": {"url": "https://verify.example.com/slider-success"},
                }
            )
        )

        async def _fake_handle_captcha(_res_json):
            live._refresh_token_impl = mock.AsyncMock(return_value="token-recursed")
            return "unb=user1; cookie2=new-cookie2"

        live._handle_captcha_verification = mock.AsyncMock(side_effect=_fake_handle_captcha)

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()), \
             mock.patch("XianyuAutoAsync.random.uniform", return_value=0):
            result = await XianyuLive._refresh_token_impl(live, allow_password_login_recovery=False)

        self.assertEqual(result, "token-recursed")
        live._update_risk_log.assert_called_once()
        event_meta = live._update_risk_log.call_args.kwargs["event_meta"]
        self.assertEqual(event_meta["account_id"], "acc-slider-success-1")
        self.assertEqual(event_meta["cookie_length"], len("unb=user1; cookie2=new-cookie2"))
        self.assertNotIn("cookie_id", event_meta)

    async def test_refresh_token_impl_prefers_account_id_alias_for_slider_captcha_failed_risk_meta(self):
        class _FakeJsonResponse:
            def __init__(self, payload):
                self.status = 200
                self.headers = {}
                self.payload = payload

            async def json(self, content_type=None):
                return self.payload

        class _FakeSession:
            def __init__(self, response):
                self.response = response

            def post(self, url, params=None, data=None, headers=None, timeout=None, **kwargs):
                return XianyuAsyncBrowserRuntimeTest._FakeAsyncPostContext(self.response)

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-slider-failed"
        live.account_id = "acc-slider-failed-1"
        live.cookies_str = "unb=user1; _m_h5_tk=token_123; cookie2=v2"
        live.device_id = "device-slider-failed-1"
        live._safe_str = str
        live.max_captcha_verification_count = 3
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live._skip_db_cookie_reload_for_token_refresh = True
        live.restarted_in_browser_refresh = False
        live._extract_set_cookie_updates = mock.Mock(return_value={})
        live._need_captcha_verification = mock.Mock(return_value=True)
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live.is_manual_refresh_active = mock.Mock(return_value=False)
        live._clear_pending_slider_success_notice = mock.Mock()
        live._new_risk_session_id = mock.Mock(return_value="risk-session-failed")
        live._create_risk_log = mock.Mock(return_value=53)
        live._update_risk_log = mock.Mock()
        live._handle_captcha_verification = mock.AsyncMock(return_value=None)
        live.send_token_refresh_notification = mock.AsyncMock()
        live.create_session = mock.AsyncMock()
        live.session = _FakeSession(
            _FakeJsonResponse(
                {
                    "ret": ["FAIL_SYS::risk"],
                    "data": {"url": "https://verify.example.com/slider-failed"},
                }
            )
        )

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()):
            result = await live._refresh_token_impl(allow_password_login_recovery=False)

        self.assertIsNone(result)
        live._update_risk_log.assert_called_once()
        event_meta = live._update_risk_log.call_args.kwargs["event_meta"]
        self.assertEqual(event_meta["account_id"], "acc-slider-failed-1")
        self.assertNotIn("cookie_id", event_meta)

    async def test_refresh_token_impl_prefers_account_id_alias_for_slider_captcha_exception_risk_meta(self):
        class _FakeJsonResponse:
            def __init__(self, payload):
                self.status = 200
                self.headers = {}
                self.payload = payload

            async def json(self, content_type=None):
                return self.payload

        class _FakeSession:
            def __init__(self, response):
                self.response = response

            def post(self, url, params=None, data=None, headers=None, timeout=None, **kwargs):
                return XianyuAsyncBrowserRuntimeTest._FakeAsyncPostContext(self.response)

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-slider-exception"
        live.account_id = "acc-slider-exception-1"
        live.cookies_str = "unb=user1; _m_h5_tk=token_123; cookie2=v2"
        live.device_id = "device-slider-exception-1"
        live._safe_str = str
        live.max_captcha_verification_count = 3
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live._skip_db_cookie_reload_for_token_refresh = True
        live.restarted_in_browser_refresh = False
        live._extract_set_cookie_updates = mock.Mock(return_value={})
        live._need_captcha_verification = mock.Mock(return_value=True)
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live.is_manual_refresh_active = mock.Mock(return_value=False)
        live._clear_pending_slider_success_notice = mock.Mock()
        live._new_risk_session_id = mock.Mock(return_value="risk-session-exception")
        live._create_risk_log = mock.Mock(return_value=54)
        live._update_risk_log = mock.Mock()
        live._handle_captcha_verification = mock.AsyncMock(side_effect=RuntimeError("slider boom"))
        live.send_token_refresh_notification = mock.AsyncMock()
        live.create_session = mock.AsyncMock()
        live.session = _FakeSession(
            _FakeJsonResponse(
                {
                    "ret": ["FAIL_SYS::risk"],
                    "data": {"url": "https://verify.example.com/slider-exception"},
                }
            )
        )

        with mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()):
            result = await live._refresh_token_impl(allow_password_login_recovery=False)

        self.assertIsNone(result)
        live._update_risk_log.assert_called_once()
        event_meta = live._update_risk_log.call_args.kwargs["event_meta"]
        self.assertEqual(event_meta["account_id"], "acc-slider-exception-1")
        self.assertNotIn("cookie_id", event_meta)

    async def test_preflight_token_for_fresh_auth_cookies_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-preflight-1"
        live.last_message_received_time = 123
        live._skip_db_cookie_reload_for_token_refresh = False
        live.refresh_token = mock.AsyncMock(return_value="token-preflight-1")
        live.cache_auth_prewarmed_token = mock.Mock()

        token = await live._preflight_token_for_fresh_auth_cookies(
            label="fresh token preflight",
            token_source="manual_refresh_handoff",
        )

        self.assertEqual(token, "token-preflight-1")
        live.refresh_token.assert_awaited_once_with(allow_password_login_recovery=False)
        live.cache_auth_prewarmed_token.assert_called_once_with(
            "acc-preflight-1",
            "token-preflight-1",
            source="manual_refresh_handoff",
        )
        self.assertFalse(live._skip_db_cookie_reload_for_token_refresh)

    async def test_preflight_token_for_fresh_auth_cookies_logs_account_id_alias_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-preflight-log-1"
        live.last_message_received_time = 123
        live._skip_db_cookie_reload_for_token_refresh = False
        live.refresh_token = mock.AsyncMock(return_value="token-preflight-log-1")
        live.cache_auth_prewarmed_token = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            token = await live._preflight_token_for_fresh_auth_cookies(
                label="fresh token preflight",
                token_source="manual_refresh_handoff",
            )

        self.assertEqual(token, "token-preflight-log-1")
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("acc-preflight-log-1", messages)
        self.assertNotIn("legacy-cookie-name", messages)

    async def test_preflight_token_for_fresh_auth_cookies_rejects_blank_canonical_account_id_without_cookie_id_fallback(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-preflight-blank"
        live.account_id = "   "
        live.last_message_received_time = 123
        live._skip_db_cookie_reload_for_token_refresh = False
        live.refresh_token = mock.AsyncMock(
            side_effect=AssertionError("should not refresh token without canonical account_id")
        )
        live.cache_auth_prewarmed_token = mock.Mock()

        token = await live._preflight_token_for_fresh_auth_cookies(
            label="fresh token preflight",
            token_source="manual_refresh_handoff",
        )

        self.assertEqual(token, "")
        live.refresh_token.assert_not_awaited()
        live.cache_auth_prewarmed_token.assert_not_called()
        self.assertFalse(live._skip_db_cookie_reload_for_token_refresh)

    async def test_preflight_token_for_fresh_auth_cookies_sets_missing_account_status_when_canonical_blank(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-preflight-status"
        live.account_id = "   "
        live.last_message_received_time = 123
        live._skip_db_cookie_reload_for_token_refresh = False
        live.refresh_token = mock.AsyncMock(
            side_effect=AssertionError("should not refresh token without canonical account_id")
        )
        live.cache_auth_prewarmed_token = mock.Mock()
        live.last_token_refresh_status = "stale-status"
        live.last_token_refresh_error_message = "stale-error"

        token = await live._preflight_token_for_fresh_auth_cookies(
            label="fresh token preflight",
            token_source="manual_refresh_handoff",
        )

        self.assertEqual(token, "")
        self.assertEqual(live.last_token_refresh_status, "missing_account_id")
        self.assertEqual(
            live.last_token_refresh_error_message,
            "missing canonical account_id for token preflight",
        )

    async def test_init_prefers_account_id_alias_when_clearing_init_auth_failure_state(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-init-clear-1"
        live.current_token = "token-init-1"
        live.last_token_refresh_time = time.time()
        live.token_refresh_interval = 3600
        live.last_init_failure_type = "old"
        live.last_init_failure_reason = "old-reason"
        live.clear_init_auth_failure_state = mock.Mock()
        live.init_auth_failures = 3
        live.device_id = "device-init-1"

        ws = types.SimpleNamespace(send=mock.AsyncMock())

        await live.init(ws)

        live.clear_init_auth_failure_state.assert_called_once_with("acc-init-clear-1")
        self.assertEqual(ws.send.await_count, 2)
        self.assertEqual(live.init_auth_failures, 0)

    async def test_init_rejects_default_or_blank_canonical_account_id_before_clearing_init_auth_failure_state(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live.current_token = "token-init-invalid"
                live.last_token_refresh_time = time.time()
                live.token_refresh_interval = 3600
                live.last_init_failure_type = "old"
                live.last_init_failure_reason = "old-reason"
                live.clear_init_auth_failure_state = mock.Mock(
                    side_effect=AssertionError(
                        "should not clear init auth failure state without canonical account_id"
                    )
                )
                live.init_auth_failures = 3
                live.device_id = "device-init-invalid"

                ws = types.SimpleNamespace(send=mock.AsyncMock())

                await live.init(ws)

                live.clear_init_auth_failure_state.assert_not_called()
                self.assertEqual(ws.send.await_count, 2)
                self.assertEqual(live.init_auth_failures, 0)

    async def test_main_prefers_account_id_alias_for_init_auth_failure_state_lookup(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-main-lookup-1"
        live.base_url = "wss://example.com/ws"
        live._safe_str = str
        live.create_session = mock.AsyncMock()
        live.get_init_auth_failure_state = mock.Mock(
            return_value={"circuit_until": time.time() + 60}
        )
        live._interruptible_sleep = mock.AsyncMock(side_effect=asyncio.CancelledError())
        live._set_connection_state = mock.Mock()
        live.current_token = None
        live.heartbeat_task = None
        live.token_refresh_task = None
        live.cleanup_task = None
        live.cookie_refresh_task = None
        live.stream_watchdog_task = None
        live.background_tasks = set()
        live.close_session = mock.AsyncMock()
        live._unregister_instance = mock.Mock()

        with mock.patch.dict(
            sys.modules,
            {"cookie_manager": self._build_cookie_manager_module(enabled=True)},
        ):
            with self.assertRaises(asyncio.CancelledError):
                await live.main()

        live.get_init_auth_failure_state.assert_called_once_with("acc-main-lookup-1")

    async def test_main_rejects_default_or_blank_canonical_account_id_before_init_auth_failure_state_lookup(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live.base_url = "wss://example.com/ws"
                live._safe_str = str
                live.create_session = mock.AsyncMock()
                live.get_init_auth_failure_state = mock.Mock(
                    side_effect=AssertionError(
                        "should not read init auth failure state without canonical account_id"
                    )
                )
                live._build_websocket_headers = mock.Mock(return_value={"header": "value"})
                live._create_websocket_connection = mock.AsyncMock(side_effect=asyncio.CancelledError())
                live._set_connection_state = mock.Mock()
                live.current_token = None
                live.heartbeat_task = None
                live.token_refresh_task = None
                live.cleanup_task = None
                live.cookie_refresh_task = None
                live.stream_watchdog_task = None
                live.background_tasks = set()
                live.close_session = mock.AsyncMock()
                live._unregister_instance = mock.Mock()

                with mock.patch.dict(
                    sys.modules,
                    {"cookie_manager": self._build_cookie_manager_module(enabled=True)},
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await live.main()

                live.get_init_auth_failure_state.assert_not_called()
                live._build_websocket_headers.assert_called_once_with()

    async def test_main_prefers_account_id_alias_for_cookie_status_gate(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-main-gate-1"
        live._safe_str = str
        live.create_session = mock.AsyncMock()
        live.get_init_auth_failure_state = mock.Mock()
        live.current_token = None
        live._set_connection_state = mock.Mock()
        live.heartbeat_task = None
        live.token_refresh_task = None
        live.cleanup_task = None
        live.cookie_refresh_task = None
        live.stream_watchdog_task = None
        live.background_tasks = set()
        live.close_session = mock.AsyncMock()
        live._unregister_instance = mock.Mock()

        cookie_manager_module = self._build_cookie_manager_module(enabled=False)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}):
            await live.main()

        cookie_manager_module.manager.get_cookie_status.assert_called_once_with(
            "acc-main-gate-1"
        )
        live.get_init_auth_failure_state.assert_not_called()

    async def test_main_prefers_account_id_alias_when_recording_init_auth_failure(self):
        class _FakeWebSocketContext:
            def __init__(self, websocket):
                self.websocket = websocket

            async def __aenter__(self):
                return self.websocket

            async def __aexit__(self, exc_type, exc, tb):
                return False

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-main-record-1"
        live.base_url = "wss://example.com/ws"
        live._safe_str = str
        live.create_session = mock.AsyncMock()
        live.get_init_auth_failure_state = mock.Mock(return_value={})
        live._build_websocket_headers = mock.Mock(return_value={"header": "value"})
        live._create_websocket_connection = mock.AsyncMock(
            return_value=_FakeWebSocketContext(types.SimpleNamespace())
        )
        live.init = mock.AsyncMock(side_effect=XianyuAutoAsync.InitAuthError("boom"))
        live.record_init_auth_failure = mock.Mock(return_value={"count": 1, "circuit_until": 0})
        live._calculate_retry_delay = mock.Mock(return_value=1)
        live._reset_background_tasks = mock.Mock()
        live._interruptible_sleep = mock.AsyncMock(side_effect=asyncio.CancelledError())
        live._set_connection_state = mock.Mock()
        live.connection_failures = 0
        live.max_connection_failures = 5
        live.message_queue_enabled = False
        live.message_queue_running = False
        live.ws = None
        live.background_tasks = set()
        live.close_session = mock.AsyncMock()
        live._unregister_instance = mock.Mock()
        live.heartbeat_task = None
        live.token_refresh_task = None
        live.cleanup_task = None
        live.cookie_refresh_task = None
        live.stream_watchdog_task = None

        with mock.patch.dict(
            sys.modules,
            {"cookie_manager": self._build_cookie_manager_module(enabled=True)},
        ):
            with self.assertRaises(asyncio.CancelledError):
                await live.main()

        live.record_init_auth_failure.assert_called_once_with(
            "acc-main-record-1",
            "boom",
        )

    async def test_main_rejects_default_or_blank_canonical_account_id_when_recording_init_auth_failure(self):
        class _FakeWebSocketContext:
            def __init__(self, websocket):
                self.websocket = websocket

            async def __aenter__(self):
                return self.websocket

            async def __aexit__(self, exc_type, exc, tb):
                return False

        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live.base_url = "wss://example.com/ws"
                live._safe_str = str
                live.create_session = mock.AsyncMock()
                live.get_init_auth_failure_state = mock.Mock(return_value={})
                live._build_websocket_headers = mock.Mock(return_value={"header": "value"})
                live._create_websocket_connection = mock.AsyncMock(
                    return_value=_FakeWebSocketContext(types.SimpleNamespace())
                )
                live.init = mock.AsyncMock(side_effect=XianyuAutoAsync.InitAuthError("boom"))
                live.record_init_auth_failure = mock.Mock(
                    side_effect=AssertionError(
                        "should not record init auth failure without canonical account_id"
                    )
                )
                live._calculate_retry_delay = mock.Mock(return_value=1)
                live._reset_background_tasks = mock.Mock()
                live._interruptible_sleep = mock.AsyncMock(side_effect=asyncio.CancelledError())
                live._set_connection_state = mock.Mock()
                live.connection_failures = 0
                live.max_connection_failures = 5
                live.message_queue_enabled = False
                live.message_queue_running = False
                live.ws = None
                live.background_tasks = set()
                live.close_session = mock.AsyncMock()
                live._unregister_instance = mock.Mock()
                live.heartbeat_task = None
                live.token_refresh_task = None
                live.cleanup_task = None
                live.cookie_refresh_task = None
                live.stream_watchdog_task = None

                with mock.patch.dict(
                    sys.modules,
                    {"cookie_manager": self._build_cookie_manager_module(enabled=True)},
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await live.main()

                live.record_init_auth_failure.assert_not_called()

    async def test_get_api_reply_prefers_account_id_alias_in_payload(self):
        class _FakeJsonResponse:
            async def json(self):
                return {
                    "code": 200,
                    "data": {"send_msg": "hi {send_user_name}"},
                }

        class _FakeSession:
            def __init__(self, response):
                self.response = response
                self.calls = []

            def post(self, url, json=None, timeout=None):
                self.calls.append(
                    {
                        "url": url,
                        "json": json,
                        "timeout": timeout,
                    }
                )
                return XianyuAsyncBrowserRuntimeTest._FakeAsyncPostContext(self.response)

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-api-reply-1"
        live.session = _FakeSession(_FakeJsonResponse())
        live.create_session = mock.AsyncMock()
        live._safe_str = str

        with mock.patch.dict(
            XianyuAutoAsync.AUTO_REPLY,
            {"api": {"url": "http://localhost:8080/xianyu/reply", "timeout": 9}},
            clear=False,
        ):
            reply = await live.get_api_reply(
                "2026-05-10 12:00:00",
                "https://example.com/user/1",
                "user-1",
                "buyer-a",
                "item-1",
                "hello",
                "chat-1",
            )

        self.assertEqual(reply, "hi buyer-a")
        self.assertEqual(
            live.session.calls[0]["json"]["account_id"],
            "acc-api-reply-1",
        )

    async def test_get_api_reply_rejects_default_or_blank_canonical_account_id_before_request(self):
        class _FakeJsonResponse:
            async def json(self):
                return {
                    "code": 200,
                    "data": {"send_msg": "hi {send_user_name}"},
                }

        class _FakeSession:
            def __init__(self, response):
                self.response = response
                self.calls = []

            def post(self, *args, **kwargs):
                self.calls.append({"args": args, "kwargs": kwargs})
                return XianyuAsyncBrowserRuntimeTest._FakeAsyncPostContext(self.response)

        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live.session = _FakeSession(_FakeJsonResponse())
                live.create_session = mock.AsyncMock()
                live._safe_str = str

                with mock.patch.dict(
                    XianyuAutoAsync.AUTO_REPLY,
                    {"api": {"url": "http://localhost:8080/xianyu/reply", "timeout": 9}},
                    clear=False,
                ):
                    reply = await live.get_api_reply(
                        "2026-05-10 12:00:00",
                        "https://example.com/user/1",
                        "user-1",
                        "buyer-a",
                        "item-1",
                        "hello",
                        "chat-1",
                    )

                self.assertIsNone(reply)
                self.assertEqual(live.session.calls, [])

    def test_is_auto_confirm_enabled_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-auto-confirm-1"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_auto_confirm.return_value = False

        with mock.patch("db_manager.db_manager", fake_db):
            enabled = live.is_auto_confirm_enabled()

        self.assertFalse(enabled)
        fake_db.get_auto_confirm.assert_called_once_with("acc-auto-confirm-1")

    def test_is_auto_confirm_enabled_rejects_blank_canonical_account_id_before_db_lookup(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "   "
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_auto_confirm.side_effect = AssertionError(
            "should not read auto confirm config without canonical account_id"
        )

        with mock.patch("db_manager.db_manager", fake_db):
            enabled = live.is_auto_confirm_enabled()

        self.assertFalse(enabled)
        fake_db.get_auto_confirm.assert_not_called()

    def test_is_auto_comment_enabled_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-auto-comment-1"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_auto_comment.return_value = True

        with mock.patch("db_manager.db_manager", fake_db):
            enabled = live.is_auto_comment_enabled()

        self.assertTrue(enabled)
        fake_db.get_auto_comment.assert_called_once_with("acc-auto-comment-1")

    def test_is_auto_comment_enabled_rejects_blank_canonical_account_id_before_db_lookup(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "   "
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_auto_comment.side_effect = AssertionError(
            "should not read auto comment config without canonical account_id"
        )

        with mock.patch("db_manager.db_manager", fake_db):
            enabled = live.is_auto_comment_enabled()

        self.assertFalse(enabled)
        fake_db.get_auto_comment.assert_not_called()

    async def test_handle_auto_comment_prefers_account_id_alias_for_active_template(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-auto-template-1"
        live._safe_str = str
        live.is_auto_comment_enabled = mock.Mock(return_value=True)
        live._extract_order_id_for_comment = mock.Mock(return_value="order-comment-1")
        live._call_comment_api = mock.AsyncMock(return_value={"success": True})

        fake_db = mock.Mock()
        fake_db.get_active_comment_template.return_value = {
            "name": "default-template",
            "content": "great buyer",
        }

        with mock.patch("db_manager.db_manager", fake_db):
            result = await live.handle_auto_comment({}, "2026-05-10 12:00:00", "msg-1")

        self.assertTrue(result)
        fake_db.get_active_comment_template.assert_called_once_with("acc-auto-template-1")
        live._call_comment_api.assert_awaited_once_with("order-comment-1", "great buyer")

    async def test_handle_auto_comment_rejects_default_or_blank_canonical_account_id_before_template_lookup(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live._safe_str = str
                live.is_auto_comment_enabled = mock.Mock(return_value=True)
                live._extract_order_id_for_comment = mock.Mock(return_value="order-comment-1")
                live._call_comment_api = mock.AsyncMock(
                    side_effect=AssertionError("missing canonical account_id should skip comment API")
                )

                fake_db = mock.Mock()
                fake_db.get_active_comment_template.side_effect = AssertionError(
                    "should not read active comment template without canonical account_id"
                )

                with mock.patch("db_manager.db_manager", fake_db):
                    result = await live.handle_auto_comment({}, "2026-05-10 12:00:00", "msg-1")

                self.assertFalse(result)
                fake_db.get_active_comment_template.assert_not_called()
                live._call_comment_api.assert_not_awaited()

    async def test_update_keyword_image_url_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-keyword-image-1"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.update_keyword_image_url.return_value = True

        with mock.patch("db_manager.db_manager", fake_db):
            await live._update_keyword_image_url("hello", "https://img.example.com/a.png")

        fake_db.update_keyword_image_url.assert_called_once_with(
            "acc-keyword-image-1",
            "hello",
            "https://img.example.com/a.png",
        )

    async def test_update_keyword_image_url_rejects_default_or_blank_canonical_account_id_before_db_write(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live._safe_str = str

                fake_db = mock.Mock()
                fake_db.update_keyword_image_url.side_effect = AssertionError(
                    "should not update keyword image url without canonical account_id"
                )

                with mock.patch("db_manager.db_manager", fake_db):
                    await live._update_keyword_image_url("hello", "https://img.example.com/a.png")

                fake_db.update_keyword_image_url.assert_not_called()

    async def test_get_default_reply_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-default-reply-1"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_default_reply.return_value = {
            "enabled": True,
            "reply_content": "你好 {send_user_name}",
            "reply_once": True,
        }
        fake_db.has_default_reply_record.return_value = False

        with mock.patch("db_manager.db_manager", fake_db):
            reply = await live.get_default_reply(
                send_user_name="张三",
                send_user_id="buyer-1",
                send_message="你好",
                chat_id="chat-1",
            )

        self.assertEqual(reply, "你好 张三")
        fake_db.get_default_reply.assert_called_once_with("acc-default-reply-1")
        fake_db.has_default_reply_record.assert_called_once_with("acc-default-reply-1", "chat-1")

    async def test_get_default_reply_rejects_blank_canonical_account_id_before_db_lookup(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "   "
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_default_reply.side_effect = AssertionError(
            "should not read default reply config without canonical account_id"
        )

        with mock.patch("db_manager.db_manager", fake_db):
            reply = await live.get_default_reply(
                send_user_name="张三",
                send_user_id="buyer-1",
                send_message="你好",
                chat_id="chat-1",
            )

        self.assertIsNone(reply)
        fake_db.get_default_reply.assert_not_called()
        fake_db.has_default_reply_record.assert_not_called()

    def test_record_default_reply_once_after_success_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-default-reply-2"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_default_reply.return_value = {
            "enabled": True,
            "reply_once": True,
        }

        with mock.patch("db_manager.db_manager", fake_db):
            live._record_default_reply_once_after_success("chat-2")

        fake_db.get_default_reply.assert_called_once_with("acc-default-reply-2")
        fake_db.add_default_reply_record.assert_called_once_with("acc-default-reply-2", "chat-2")

    def test_record_default_reply_once_after_success_rejects_blank_canonical_account_id_before_db_write(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "   "
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_default_reply.side_effect = AssertionError(
            "should not read default reply config without canonical account_id"
        )

        with mock.patch("db_manager.db_manager", fake_db):
            live._record_default_reply_once_after_success("chat-2")

        fake_db.get_default_reply.assert_not_called()
        fake_db.add_default_reply_record.assert_not_called()

    async def test_get_keyword_reply_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-keyword-reply-1"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_keywords_with_type.return_value = [
            {
                "keyword": "发货",
                "reply": "好的 {send_user_name}",
                "item_id": None,
                "type": "text",
                "image_url": None,
            }
        ]

        with mock.patch("db_manager.db_manager", fake_db):
            reply = await live.get_keyword_reply(
                send_user_name="李四",
                send_user_id="buyer-2",
                send_message="什么时候发货?",
            )

        self.assertEqual(reply, "好的 李四")
        fake_db.get_keywords_with_type.assert_called_once_with("acc-keyword-reply-1")

    async def test_get_keyword_reply_rejects_default_or_blank_canonical_account_id_before_db_lookup(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live._safe_str = str

                fake_db = mock.Mock()
                fake_db.get_keywords_with_type.side_effect = AssertionError(
                    "should not read keyword replies without canonical account_id"
                )

                with mock.patch("db_manager.db_manager", fake_db):
                    reply = await live.get_keyword_reply(
                        send_user_name="李四",
                        send_user_id="buyer-2",
                        send_message="什么时候发货?",
                    )

                self.assertIsNone(reply)
                fake_db.get_keywords_with_type.assert_not_called()

    async def test_get_ai_reply_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-ai-reply-1"
        live._safe_str = str
        live._parse_price = mock.Mock(return_value=88.5)

        fake_db = mock.Mock()
        fake_db.get_item_info.return_value = {
            "item_title": "AI Item",
            "item_price": "88.5",
            "item_detail": "AI detail",
        }

        fake_engine = mock.Mock()
        fake_engine.is_ai_enabled.return_value = True
        fake_engine.generate_reply.return_value = "AI reply ok"

        ai_reply_engine_module = types.ModuleType("ai_reply_engine")
        ai_reply_engine_module.ai_reply_engine = fake_engine

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.dict(sys.modules, {"ai_reply_engine": ai_reply_engine_module}):
            reply = await live.get_ai_reply(
                send_user_name="buyer-ai",
                send_user_id="buyer-ai-1",
                send_message="能便宜点吗?",
                item_id="item-ai-1",
                chat_id="chat-ai-1",
            )

        self.assertEqual(reply, "AI reply ok")
        fake_engine.is_ai_enabled.assert_called_once_with("acc-ai-reply-1")
        fake_db.get_item_info.assert_called_once_with("acc-ai-reply-1", "item-ai-1")
        fake_engine.generate_reply.assert_called_once_with(
            message="能便宜点吗?",
            item_info={
                "title": "AI Item",
                "price": 88.5,
                "desc": "AI detail",
            },
            chat_id="chat-ai-1",
            account_id="acc-ai-reply-1",
            user_id="buyer-ai-1",
            item_id="item-ai-1",
            skip_wait=True,
        )

    async def test_get_ai_reply_rejects_default_or_blank_canonical_account_id_before_scope_lookup(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live._safe_str = str
                live._parse_price = mock.Mock(
                    side_effect=AssertionError("missing canonical account_id should skip price parsing")
                )

                fake_db = mock.Mock()
                fake_db.get_item_info.side_effect = AssertionError(
                    "should not load item info without canonical account_id"
                )

                fake_engine = mock.Mock()
                fake_engine.is_ai_enabled.side_effect = AssertionError(
                    "should not check AI switch without canonical account_id"
                )

                ai_reply_engine_module = types.ModuleType("ai_reply_engine")
                ai_reply_engine_module.ai_reply_engine = fake_engine

                with mock.patch("db_manager.db_manager", fake_db), \
                     mock.patch.dict(sys.modules, {"ai_reply_engine": ai_reply_engine_module}):
                    reply = await live.get_ai_reply(
                        send_user_name="buyer-ai",
                        send_user_id="buyer-ai-1",
                        send_message="能便宜点吗?",
                        item_id="item-ai-1",
                        chat_id="chat-ai-1",
                    )

                self.assertIsNone(reply)
                fake_engine.is_ai_enabled.assert_not_called()
                fake_db.get_item_info.assert_not_called()
                live._parse_price.assert_not_called()

    def test_persist_delivery_finalization_state_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-delivery-live-2"

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-live-2",
            "account_id": "acc-delivery-live-2",
        }
        fake_db.upsert_delivery_finalization_state.return_value = True

        with mock.patch("db_manager.db_manager", fake_db):
            result = live._persist_delivery_finalization_state(
                order_id="order-live-2",
                item_id="item-live-2",
                buyer_id="buyer-live-2",
                delivery_meta={"delivery_unit_index": 2, "flag": "x"},
                channel="manual",
                status="sent",
                last_error="pending",
            )

        self.assertTrue(result)
        fake_db.get_order_by_id.assert_called_once_with(
            "order-live-2",
            account_id="acc-delivery-live-2",
        )
        fake_db.upsert_delivery_finalization_state.assert_called_once_with(
            order_id="order-live-2",
            unit_index=2,
            account_id="acc-delivery-live-2",
            item_id="item-live-2",
            buyer_id="buyer-live-2",
            channel="manual",
            status="sent",
            delivery_meta={"delivery_unit_index": 2, "flag": "x"},
            last_error="pending",
        )

    def test_persist_delivery_finalization_state_skips_db_write_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-finalize-blank"
        live.account_id = "   "
        fake_db = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = live._persist_delivery_finalization_state(
                order_id="order-live-blank",
                item_id="item-live-blank",
                buyer_id="buyer-live-blank",
                delivery_meta={"delivery_unit_index": 2, "flag": "x"},
                channel="manual",
                status="sent",
                last_error="pending",
            )

        self.assertFalse(result)
        fake_db.upsert_delivery_finalization_state.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    def test_persist_delivery_finalization_state_keeps_sent_marker_when_scoped_order_missing(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-finalize-sent-missing-order"
        live.account_id = "acc-delivery-finalize-sent-missing-order-1"

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = None
        fake_db.upsert_delivery_finalization_state.return_value = True

        with mock.patch("db_manager.db_manager", fake_db):
            result = live._persist_delivery_finalization_state(
                order_id=" order-live-sent-missing ",
                item_id="item-live-sent-missing",
                buyer_id="buyer-live-sent-missing",
                delivery_meta={"delivery_unit_index": 3},
                channel="manual",
                status="sent",
                last_error="pending finalize",
            )

        self.assertTrue(result)
        fake_db.get_order_by_id.assert_called_once_with(
            "order-live-sent-missing",
            account_id="acc-delivery-finalize-sent-missing-order-1",
        )
        fake_db.upsert_delivery_finalization_state.assert_called_once_with(
            order_id="order-live-sent-missing",
            unit_index=3,
            account_id="acc-delivery-finalize-sent-missing-order-1",
            item_id="item-live-sent-missing",
            buyer_id="buyer-live-sent-missing",
            channel="manual",
            status="sent",
            delivery_meta={"delivery_unit_index": 3},
            last_error="pending finalize",
        )

    def test_persist_delivery_finalization_state_rejects_finalized_write_when_scoped_order_missing(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-finalize-finalized-missing-order"
        live.account_id = "acc-delivery-finalize-finalized-missing-order-1"

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = None

        with mock.patch("db_manager.db_manager", fake_db):
            result = live._persist_delivery_finalization_state(
                order_id="order-live-finalized-missing",
                item_id="item-live-finalized-missing",
                buyer_id="buyer-live-finalized-missing",
                delivery_meta={"delivery_unit_index": 1},
                channel="manual",
                status="finalized",
            )

        self.assertFalse(result)
        fake_db.get_order_by_id.assert_called_once_with(
            "order-live-finalized-missing",
            account_id="acc-delivery-finalize-finalized-missing-order-1",
        )
        fake_db.upsert_delivery_finalization_state.assert_not_called()

    def test_persist_delivery_finalization_state_rejects_blank_order_id_after_normalization(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-finalize-blank-order"
        live.account_id = "acc-delivery-finalize-blank-order-1"

        fake_db = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db):
            result = live._persist_delivery_finalization_state(
                order_id="   ",
                item_id="item-live-blank-order",
                buyer_id="buyer-live-blank-order",
                delivery_meta={"delivery_unit_index": 1},
                channel="manual",
                status="sent",
            )

        self.assertFalse(result)
        fake_db.upsert_delivery_finalization_state.assert_not_called()

    def test_get_pending_delivery_finalization_meta_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-pending"
        live.account_id = "acc-delivery-pending-1"

        fake_db = mock.Mock()
        fake_db.get_delivery_finalization_state.return_value = {
            "status": "sent",
            "delivery_meta": {"flag": "pending"},
        }

        with mock.patch("db_manager.db_manager", fake_db):
            meta = live._get_pending_delivery_finalization_meta("order-pending-1", 2)

        self.assertEqual("pending", meta["flag"])
        self.assertTrue(meta["success"])
        self.assertEqual(2, meta["delivery_unit_index"])
        fake_db.get_delivery_finalization_state.assert_called_once_with(
            "order-pending-1",
            2,
            account_id="acc-delivery-pending-1",
        )

    def test_get_pending_delivery_finalization_meta_rejects_blank_order_id_after_normalization(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-pending-blank-order"
        live.account_id = "acc-delivery-pending-blank-order-1"

        fake_db = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db):
            meta = live._get_pending_delivery_finalization_meta("   ", 2)

        self.assertIsNone(meta)
        fake_db.get_delivery_finalization_state.assert_not_called()

    def test_summarize_delivery_progress_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-summary"
        live.account_id = "acc-delivery-summary-1"

        fake_db = mock.Mock()
        fake_db.get_delivery_progress_summary.return_value = {
            "order_id": "order-summary-1",
            "expected_quantity": 2,
            "finalized_count": 1,
            "pending_finalize_count": 1,
            "remaining_count": 0,
            "aggregate_status": "partial_pending_finalize",
            "states": [],
        }

        with mock.patch("db_manager.db_manager", fake_db):
            summary = live._summarize_delivery_progress("order-summary-1", expected_quantity=2)

        self.assertEqual("partial_pending_finalize", summary["aggregate_status"])
        fake_db.get_delivery_progress_summary.assert_called_once_with(
            "order-summary-1",
            account_id="acc-delivery-summary-1",
            expected_quantity=2,
        )

    def test_mark_order_bargain_flow_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-bargain-live-1"

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-bargain-live-1",
            "item_id": "existing-item",
            "buyer_id": "existing-buyer",
            "sid": "existing-sid",
        }
        fake_db.insert_or_update_order.return_value = True

        with mock.patch("db_manager.db_manager", fake_db):
            result = live._mark_order_bargain_flow(
                order_id="order-bargain-live-1",
                item_id="item-bargain-live-1",
                buyer_id="buyer-bargain-live-1",
                sid="sid-bargain-live-1",
                context="unit-test",
            )

        self.assertTrue(result)
        fake_db.get_order_by_id.assert_called_once_with(
            "order-bargain-live-1",
            account_id="acc-bargain-live-1",
        )
        fake_db.insert_or_update_order.assert_called_once_with(
            order_id="order-bargain-live-1",
            item_id="item-bargain-live-1",
            buyer_id="buyer-bargain-live-1",
            sid="sid-bargain-live-1",
            amount=None,
            account_id="acc-bargain-live-1",
            bargain_flow_detected=True,
            bargain_success_detected=Ellipsis,
        )

    def test_mark_order_bargain_flow_normalizes_order_id_before_scoped_reads_and_writes(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-bargain-normalized-order"
        live.account_id = "acc-bargain-normalized-order-1"

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-bargain-normalized-1",
            "item_id": "existing-item",
            "buyer_id": "existing-buyer",
            "sid": "existing-sid",
        }
        fake_db.insert_or_update_order.return_value = True

        with mock.patch("db_manager.db_manager", fake_db):
            result = live._mark_order_bargain_flow(
                order_id="  order-bargain-normalized-1  ",
                item_id="item-bargain-normalized-1",
                buyer_id="buyer-bargain-normalized-1",
                sid="sid-bargain-normalized-1",
                context="unit-test-normalized-order",
            )

        self.assertTrue(result)
        fake_db.get_order_by_id.assert_called_once_with(
            "order-bargain-normalized-1",
            account_id="acc-bargain-normalized-order-1",
        )
        fake_db.insert_or_update_order.assert_called_once_with(
            order_id="order-bargain-normalized-1",
            item_id="item-bargain-normalized-1",
            buyer_id="buyer-bargain-normalized-1",
            sid="sid-bargain-normalized-1",
            amount=None,
            account_id="acc-bargain-normalized-order-1",
            bargain_flow_detected=True,
            bargain_success_detected=Ellipsis,
        )

    def test_mark_order_bargain_flow_skips_db_write_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-bargain-blank"
        live.account_id = "   "
        fake_db = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = live._mark_order_bargain_flow(
                order_id="order-bargain-blank",
                item_id="item-bargain-blank",
                buyer_id="buyer-bargain-blank",
                sid="sid-bargain-blank",
                context="unit-test-blank",
            )

        self.assertFalse(result)
        fake_db.get_order_by_id.assert_not_called()
        fake_db.get_item_info.assert_not_called()
        fake_db.insert_or_update_order.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("缺少 canonical account_id", messages)

    def test_mark_order_bargain_flow_rejects_blank_order_id_after_normalization(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-bargain-blank-order"
        live.account_id = "acc-bargain-blank-order-1"

        fake_db = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db):
            result = live._mark_order_bargain_flow(
                order_id="   ",
                item_id="item-bargain-blank-order",
                buyer_id="buyer-bargain-blank-order",
                sid="sid-bargain-blank-order",
                context="unit-test-blank-order",
            )

        self.assertFalse(result)
        fake_db.get_order_by_id.assert_not_called()
        fake_db.get_item_info.assert_not_called()
        fake_db.insert_or_update_order.assert_not_called()

    async def test_auto_delivery_data_rule_prefers_account_id_alias_with_existing_scoped_order(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-auto-delivery-1"
        live.user_id = 7
        live.myid = "seller-self"
        live._safe_str = str
        live.fetch_order_detail_info = mock.AsyncMock(return_value=None)
        live._build_delivery_steps = mock.Mock(return_value=[{"type": "text", "content": "reserved-content"}])
        live.order_status_handler = mock.Mock()
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_item_info.return_value = {
            "item_title": "Item title",
            "item_detail": "Detail text",
        }
        fake_db.get_item_multi_spec_status.return_value = False
        fake_db.get_delivery_rules_by_keyword.return_value = [{
            "id": 1,
            "keyword": "Item title",
            "card_name": "Data Card",
            "card_type": "data",
            "card_id": 77,
            "card_description": "desc",
        }]
        fake_db.get_cookie_by_id.return_value = {"id": "acc-auto-delivery-1"}
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-data-1",
            "account_id": "acc-auto-delivery-1",
        }
        fake_db.insert_or_update_order.return_value = True
        fake_db.reserve_batch_data.return_value = {
            "id": 99,
            "status": "reserved",
            "reserved_content": "reserved-content",
        }

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._auto_delivery(
                item_id="item-data-1",
                item_title="Ignored title",
                order_id="order-data-1",
                send_user_id="buyer-data-1",
                send_user_name="Buyer Data",
                include_meta=True,
            )

        self.assertTrue(result["success"])
        fake_db.get_item_multi_spec_status.assert_called_once_with(
            "acc-auto-delivery-1",
            "item-data-1",
        )
        fake_db.get_cookie_by_id.assert_called_once_with("acc-auto-delivery-1")
        expected_order_scope_call = mock.call(
            "order-data-1",
            account_id="acc-auto-delivery-1",
        )
        self.assertIn(expected_order_scope_call, fake_db.get_order_by_id.call_args_list)
        fake_db.insert_or_update_order.assert_not_called()
        live.order_status_handler.handle_order_basic_info_status.assert_not_called()
        fake_db.reserve_batch_data.assert_called_once_with(
            card_id=77,
            order_id="order-data-1",
            unit_index=1,
            account_id="acc-auto-delivery-1",
            buyer_id="buyer-data-1",
        )
        messages = self._collect_logger_messages(mock_logger)
        self.assertNotIn("未验证归属", messages)

    async def test_auto_delivery_rejects_first_write_without_existing_scoped_order(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-auto-delivery-ghost-1"
        live.user_id = 7
        live.myid = "seller-self"
        live._safe_str = str
        live.fetch_order_detail_info = mock.AsyncMock(return_value=None)
        live._build_delivery_steps = mock.Mock(return_value=[{"type": "text", "content": "reserved-content"}])
        live.order_status_handler = mock.Mock()
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_item_info.return_value = {
            "item_title": "Item title",
            "item_detail": "Detail text",
        }
        fake_db.get_item_multi_spec_status.return_value = False
        fake_db.get_delivery_rules_by_keyword.return_value = [{
            "id": 1,
            "keyword": "Item title",
            "card_name": "Data Card",
            "card_type": "data",
            "card_id": 77,
            "card_description": "desc",
        }]
        fake_db.get_cookie_by_id.return_value = {"id": "acc-auto-delivery-ghost-1"}
        fake_db.get_order_by_id.return_value = None
        fake_db.insert_or_update_order.return_value = True
        fake_db.reserve_batch_data.return_value = {
            "id": 99,
            "status": "reserved",
            "reserved_content": "reserved-content",
        }

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._auto_delivery(
                item_id="item-data-ghost-1",
                item_title="Ignored title",
                order_id="order-data-ghost-1",
                send_user_id="buyer-data-ghost-1",
                send_user_name="Buyer Ghost",
                include_meta=True,
            )

        self.assertTrue(result["success"])
        fake_db.get_item_multi_spec_status.assert_called_once_with(
            "acc-auto-delivery-ghost-1",
            "item-data-ghost-1",
        )
        fake_db.get_cookie_by_id.assert_called_once_with("acc-auto-delivery-ghost-1")
        expected_order_scope_call = mock.call(
            "order-data-ghost-1",
            account_id="acc-auto-delivery-ghost-1",
        )
        self.assertIn(expected_order_scope_call, fake_db.get_order_by_id.call_args_list)
        fake_db.insert_or_update_order.assert_not_called()
        live.order_status_handler.handle_order_basic_info_status.assert_not_called()
        fake_db.reserve_batch_data.assert_called_once_with(
            card_id=77,
            order_id="order-data-ghost-1",
            unit_index=1,
            account_id="acc-auto-delivery-ghost-1",
            buyer_id="buyer-data-ghost-1",
        )
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("未验证归属", messages)

    async def test_auto_delivery_rejects_blank_canonical_account_id_before_db_reads(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = ""
        live.user_id = 7
        live.myid = "seller-self"
        live._safe_str = str
        live.fetch_order_detail_info = mock.AsyncMock()
        live._build_delivery_steps = mock.Mock()
        live.order_status_handler = mock.Mock()
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_item_info.side_effect = AssertionError(
            "auto delivery should fail closed before item reads when canonical account_id is blank"
        )
        fake_db.get_item_multi_spec_status.side_effect = AssertionError(
            "auto delivery should fail closed before item config reads when canonical account_id is blank"
        )

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = await live._auto_delivery(
                item_id="item-data-blank-account",
                item_title="Ignored title",
                order_id="order-data-blank-account",
                send_user_id="buyer-data-blank-account",
                send_user_name="Buyer Data",
                include_meta=True,
            )

        self.assertFalse(result["success"])
        self.assertIn("account_id", result["error"])
        fake_db.get_item_info.assert_not_called()
        fake_db.get_item_multi_spec_status.assert_not_called()
        live.fetch_order_detail_info.assert_not_awaited()
        live.order_status_handler.handle_order_basic_info_status.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    def test_mark_delivery_sent_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-delivery-status-1"
        live.delivery_sent_orders = set()
        live.last_delivery_time = {}
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_auto_delivery_order_status.return_value = True
        live._safe_str = str

        with mock.patch("XianyuAutoAsync.time.time", return_value=123.0):
            live.mark_delivery_sent("order-status-1", context="unit-test-status")

        live.order_status_handler.handle_auto_delivery_order_status.assert_called_once_with(
            order_id="order-status-1",
            account_id="acc-delivery-status-1",
            context="unit-test-status",
        )
        self.assertEqual(
            {("acc-delivery-status-1", "order-status-1")},
            live.delivery_sent_orders,
        )
        self.assertEqual(
            {("acc-delivery-status-1", "order-status-1"): 123.0},
            live.last_delivery_time,
        )

    def test_mark_delivery_sent_logs_account_id_alias_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-status-log"
        live.account_id = "acc-delivery-status-log-1"
        live.delivery_sent_orders = set()
        live.last_delivery_time = {}
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_auto_delivery_order_status.return_value = True
        live._safe_str = str
        mock_logger = mock.Mock()

        with mock.patch("XianyuAutoAsync.time.time", return_value=123.0), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = live.mark_delivery_sent("order-status-log-1", context="unit-test-status")

        self.assertTrue(result)
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("acc-delivery-status-log-1", messages)
        self.assertNotIn("legacy-delivery-status-log", messages)

    def test_mark_delivery_sent_skips_status_handler_without_canonical_account_id(self):
        for account_id in ("default", "   "):
            with self.subTest(account_id=account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-delivery-status-blank"
                live.account_id = account_id
                live.delivery_sent_orders = set()
                live.last_delivery_time = {}
                live.order_status_handler = mock.Mock()
                live.order_status_handler.handle_auto_delivery_order_status.side_effect = AssertionError(
                    "missing canonical account_id should skip status handler update"
                )
                live._safe_str = str
                mock_logger = mock.Mock()

                with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
                    result = live.mark_delivery_sent("order-status-blank", context="unit-test-status")

                self.assertFalse(result)
                self.assertEqual(set(), live.delivery_sent_orders)
                self.assertEqual({}, live.last_delivery_time)
                live.order_status_handler.handle_auto_delivery_order_status.assert_not_called()
                messages = self._collect_logger_messages(mock_logger)
                self.assertIn("canonical account_id", messages)

    def test_compose_order_delivery_scope_key_rejects_default_account_scope(self):
        self.assertIsNone(
            XianyuLive._compose_order_delivery_scope_key("default", "order-delivery-scope-1")
        )
        self.assertEqual(
            ("acc-delivery-scope-1", "order-delivery-scope-1"),
            XianyuLive._compose_order_delivery_scope_key("acc-delivery-scope-1", "order-delivery-scope-1"),
        )

    def test_can_auto_delivery_rejects_blank_canonical_account_id_before_cooldown_lookup(self):
        for account_id in ("default", "   "):
            with self.subTest(account_id=account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-delivery-cooldown-blank"
                live.account_id = account_id
                live.last_delivery_time = {}
                live.delivery_cooldown = 600
                live._safe_str = str
                mock_logger = mock.Mock()

                with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
                    result = live.can_auto_delivery("order-delivery-blank")

                self.assertFalse(result)
                self.assertEqual({}, live.last_delivery_time)
                messages = self._collect_logger_messages(mock_logger)
                self.assertIn("canonical account_id", messages)

    def test_can_auto_delivery_rejects_blank_order_id_after_normalization(self):
        for raw_order_id in ("", "   "):
            with self.subTest(order_id=raw_order_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-delivery-cooldown-blank-order"
                live.account_id = "acc-delivery-cooldown-blank-order-1"
                live.last_delivery_time = {}
                live.delivery_cooldown = 600
                live._safe_str = str
                mock_logger = mock.Mock()

                with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
                    result = live.can_auto_delivery(raw_order_id)

                self.assertFalse(result)
                self.assertEqual({}, live.last_delivery_time)
                messages = self._collect_logger_messages(mock_logger)
                self.assertIn("order_id", messages)

    def test_can_auto_delivery_scopes_cooldown_by_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-cooldown-scope"
        live.account_id = "acc-delivery-cooldown-1"
        live.last_delivery_time = {
            ("acc-delivery-cooldown-2", "order-delivery-scope-1"): 950.0,
        }
        live.delivery_cooldown = 600

        with mock.patch("XianyuAutoAsync.time.time", return_value=1000.0):
            self.assertTrue(live.can_auto_delivery("order-delivery-scope-1"))
            live.last_delivery_time[("acc-delivery-cooldown-1", "order-delivery-scope-1")] = 950.0
            self.assertFalse(live.can_auto_delivery("order-delivery-scope-1"))

    def test_can_auto_delivery_cooldown_logs_account_id_alias_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-delivery-cooldown-log"
        live.account_id = "acc-delivery-cooldown-log-1"
        live.last_delivery_time = {
            ("acc-delivery-cooldown-log-1", "order-delivery-cooldown-log-1"): 950.0,
        }
        live.delivery_cooldown = 600
        live._safe_str = str
        mock_logger = mock.Mock()

        with mock.patch("XianyuAutoAsync.time.time", return_value=1000.0), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            result = live.can_auto_delivery("order-delivery-cooldown-log-1")

        self.assertFalse(result)
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("acc-delivery-cooldown-log-1", messages)
        self.assertNotIn("legacy-delivery-cooldown-log", messages)

    def test_cleanup_instance_caches_prunes_scoped_delivery_state(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "legacy-delivery-cleanup-1"
        live.last_notification_time = {}
        live.last_delivery_time = {
            ("acc-delivery-cleanup-1", "order-expired"): 100.0,
            ("acc-delivery-cleanup-1", "order-fresh"): 1200.0,
        }
        live.delivery_sent_orders = {
            ("acc-delivery-cleanup-1", "order-expired"),
            ("acc-delivery-cleanup-1", "order-fresh"),
        }
        live.confirmed_orders = {}
        live._safe_str = str

        with mock.patch("XianyuAutoAsync.time.time", return_value=2000.0):
            live._cleanup_instance_caches()

        self.assertEqual(
            {("acc-delivery-cleanup-1", "order-fresh"): 1200.0},
            live.last_delivery_time,
        )
        self.assertEqual(
            {("acc-delivery-cleanup-1", "order-fresh")},
            live.delivery_sent_orders,
        )

    def test_cleanup_instance_caches_prunes_scoped_confirm_state(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "legacy-confirm-cleanup-1"
        live.last_notification_time = {}
        live.last_delivery_time = {}
        live.delivery_sent_orders = set()
        live.confirmed_orders = {
            ("acc-confirm-cleanup-1", "order-expired"): 100.0,
            ("acc-confirm-cleanup-1", "order-fresh"): 1200.0,
        }
        live._safe_str = str

        with mock.patch("XianyuAutoAsync.time.time", return_value=2000.0):
            live._cleanup_instance_caches()

        self.assertEqual(
            {("acc-confirm-cleanup-1", "order-fresh"): 1200.0},
            live.confirmed_orders,
        )

    def test_load_proxy_config_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-proxy-config-1"
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_cookie_proxy_config.return_value = {
            "proxy_type": "http",
            "proxy_host": "127.0.0.1",
            "proxy_port": 8080,
            "proxy_user": "",
            "proxy_pass": "",
        }

        with mock.patch("XianyuAutoAsync.db_manager", fake_db), \
             mock.patch("db_manager.db_manager", fake_db):
            proxy_config = live._load_proxy_config()

        self.assertEqual(proxy_config["proxy_type"], "http")
        fake_db.get_cookie_proxy_config.assert_called_once_with("acc-proxy-config-1")

    def test_load_proxy_config_rejects_blank_canonical_account_id_without_cookie_id_fallback(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-proxy-config-blank"
        live.account_id = "   "
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_cookie_proxy_config.side_effect = AssertionError(
            "should not query proxy config without canonical account_id"
        )

        with mock.patch("XianyuAutoAsync.db_manager", fake_db), \
             mock.patch("db_manager.db_manager", fake_db):
            proxy_config = live._load_proxy_config()

        self.assertEqual(proxy_config["proxy_type"], "none")
        fake_db.get_cookie_proxy_config.assert_not_called()

    def test_load_proxy_config_exception_logs_account_id_alias_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-proxy-config-log"
        live.account_id = "account-proxy-config-log"
        live._safe_str = str
        fake_db = mock.Mock()
        fake_db.get_cookie_proxy_config.side_effect = RuntimeError("proxy boom")
        mock_logger = mock.Mock()

        with mock.patch("XianyuAutoAsync.db_manager", fake_db), \
             mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            proxy_config = live._load_proxy_config()

        self.assertEqual(proxy_config["proxy_type"], "none")
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("account-proxy-config-log", messages)
        self.assertNotIn("legacy-proxy-config-log", messages)

    def test_reload_latest_cookies_from_db_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-cookie-reload-1"
        live.cookies_str = "old-cookie=1"
        live._safe_str = str
        live._extract_cookie_value = mock.Mock(return_value="new-cookie=2")
        live._set_runtime_cookie_state = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"cookie": "new-cookie=2"}

        with mock.patch("XianyuAutoAsync.db_manager", fake_db), \
             mock.patch("db_manager.db_manager", fake_db):
            changed = live._reload_latest_cookies_from_db(reason="unit-test")

        self.assertTrue(changed)
        fake_db.get_cookie_details.assert_called_once_with("acc-cookie-reload-1")
        live._set_runtime_cookie_state.assert_called_once_with(
            cookies_str="new-cookie=2",
            source="db_reload (unit-test)",
        )

    def test_reload_latest_cookies_from_db_rejects_blank_canonical_account_id_without_cookie_id_fallback(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-reload-blank"
        live.account_id = "   "
        live.cookies_str = "old-cookie=1"
        live._safe_str = str
        live._extract_cookie_value = mock.Mock()
        live._set_runtime_cookie_state = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_cookie_details.side_effect = AssertionError(
            "should not query cookie details without canonical account_id"
        )

        with mock.patch("XianyuAutoAsync.db_manager", fake_db), \
             mock.patch("db_manager.db_manager", fake_db):
            changed = live._reload_latest_cookies_from_db(reason="unit-test")

        self.assertFalse(changed)
        fake_db.get_cookie_details.assert_not_called()
        live._extract_cookie_value.assert_not_called()
        live._set_runtime_cookie_state.assert_not_called()

    def test_reload_latest_cookies_from_db_logs_account_id_alias_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-reload-log"
        live.account_id = "account-cookie-reload-log"
        live.cookies_str = "old-cookie=1"
        live._safe_str = str
        live._extract_cookie_value = mock.Mock(side_effect=RuntimeError("extract boom"))
        live._set_runtime_cookie_state = mock.Mock()
        fake_db = mock.Mock()
        fake_db.get_cookie_details.return_value = {"cookie": "new-cookie=2"}
        mock_logger = mock.Mock()

        with mock.patch("XianyuAutoAsync.db_manager", fake_db), \
             mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            changed = live._reload_latest_cookies_from_db(reason="unit-test")

        self.assertFalse(changed)
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("account-cookie-reload-log", messages)
        self.assertNotIn("legacy-cookie-reload-log", messages)

    def test_reserve_order_detail_force_refresh_rejects_blank_canonical_account_id(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-force-refresh-mark"
                live.account_id = invalid_account_id
                live._safe_str = str
                live.order_detail_force_refresh_marks = {}
                live.order_detail_force_refresh_cooldown = 5
                mock_logger = mock.Mock()

                with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
                    reserved = live._reserve_order_detail_force_refresh(
                        "order-force-refresh-mark-1",
                        reason="unit_test_mark",
                        log_prefix="[unit-test]",
                    )

                self.assertFalse(reserved)
                self.assertEqual(live.order_detail_force_refresh_marks, {})
                messages = self._collect_logger_messages(mock_logger)
                self.assertIn("canonical account_id", messages)

    def test_reserve_order_detail_force_refresh_scopes_cooldown_by_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-force-refresh-mark"
        live.account_id = "acc-force-refresh-mark-a"
        live._safe_str = str
        live.order_detail_force_refresh_marks = {}
        live.order_detail_force_refresh_cooldown = 60

        first_reserved = live._reserve_order_detail_force_refresh(
            "order-force-refresh-mark-2",
            reason="unit_test_mark_a",
            log_prefix="[unit-test]",
        )

        live.account_id = "acc-force-refresh-mark-b"
        second_reserved = live._reserve_order_detail_force_refresh(
            "order-force-refresh-mark-2",
            reason="unit_test_mark_b",
            log_prefix="[unit-test]",
        )

        self.assertTrue(first_reserved)
        self.assertTrue(second_reserved)
        self.assertIn(
            ("acc-force-refresh-mark-a", "order-force-refresh-mark-2"),
            live.order_detail_force_refresh_marks,
        )
        self.assertIn(
            ("acc-force-refresh-mark-b", "order-force-refresh-mark-2"),
            live.order_detail_force_refresh_marks,
        )

    def test_schedule_order_detail_retry_rejects_blank_canonical_account_id_before_task_creation(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-retry-schedule"
                live.account_id = invalid_account_id
                live._safe_str = str
                live.order_detail_retry_tasks = {}
                live._create_tracked_task = mock.Mock(
                    side_effect=AssertionError("missing canonical account_id should skip retry task creation")
                )
                mock_logger = mock.Mock()

                with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
                    live._schedule_order_detail_retry(
                        "order-retry-schedule-blank",
                        item_id="item-retry-schedule-blank",
                        buyer_id="buyer-retry-schedule-blank",
                        delay_seconds=30,
                    )

                self.assertEqual(live.order_detail_retry_tasks, {})
                live._create_tracked_task.assert_not_called()
                messages = self._collect_logger_messages(mock_logger)
                self.assertIn("canonical account_id", messages)

    def test_schedule_order_detail_retry_scopes_tasks_by_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-retry-schedule"
        live.account_id = "acc-retry-schedule-a"
        live._safe_str = str
        live.order_detail_retry_tasks = {}

        task_a = mock.Mock()
        task_a.done.return_value = False
        task_b = mock.Mock()
        task_b.done.return_value = False
        created_coroutines = []

        def create_task_with_cleanup(task_to_return):
            def _factory(coro):
                created_coroutines.append(coro)
                coro.close()
                return task_to_return
            return _factory

        live._create_tracked_task = mock.Mock(side_effect=create_task_with_cleanup(task_a))
        live._schedule_order_detail_retry(
            "order-retry-schedule-1",
            item_id="item-retry-schedule-1",
            buyer_id="buyer-retry-schedule-1",
            delay_seconds=30,
        )

        live.account_id = "acc-retry-schedule-b"
        live._create_tracked_task = mock.Mock(side_effect=create_task_with_cleanup(task_b))
        live._schedule_order_detail_retry(
            "order-retry-schedule-1",
            item_id="item-retry-schedule-1",
            buyer_id="buyer-retry-schedule-1",
            delay_seconds=30,
        )

        self.assertEqual(len(created_coroutines), 2)
        self.assertIs(
            live.order_detail_retry_tasks[
                ("acc-retry-schedule-a", "order-retry-schedule-1")
            ],
            task_a,
        )
        self.assertIs(
            live.order_detail_retry_tasks[
                ("acc-retry-schedule-b", "order-retry-schedule-1")
            ],
            task_b,
        )

    async def test_fetch_order_detail_info_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-order-detail-1"
        live.cookies_str = "unb=user1; cookie2=v2"
        live._safe_str = str
        live._order_detail_locks = {"order-detail-1": asyncio.Lock()}
        live._order_detail_lock_times = {}
        live.order_detail_retry_tasks = {}
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_order_detail_fetched_status.return_value = True
        live._apply_bargain_amount_override = mock.Mock(return_value=(None, "unknown"))
        live._should_reject_order_detail_status_update = mock.Mock(return_value=False)
        live._should_accept_order_detail_status_correction = mock.Mock(return_value=False)
        live._resolve_external_order_status = mock.Mock(return_value="pending_ship")
        live._select_buyer_identity_for_order_write = mock.Mock(
            return_value=("buyer-detail-1", "Buyer Detail", False)
        )

        fake_db = mock.Mock()
        fake_db.get_item_info.return_value = None
        fake_db.get_order_by_id.return_value = None
        fake_db.get_cookie_by_id.return_value = {"id": "acc-order-detail-1"}
        fake_db._normalize_order_status.side_effect = lambda value: value
        fake_db.insert_or_update_order.return_value = True

        detail_result = {
            "title": "detail-title",
            "order_status": "pending_ship",
            "order_status_source": "structured",
        }

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch(
                 "utils.order_detail_fetcher.fetch_order_detail_simple",
                 new=mock.AsyncMock(return_value=detail_result),
                 create=True,
             ) as fetch_order_detail_simple, \
             mock.patch("builtins.print"):
            result = await live.fetch_order_detail_info(
                "order-detail-1",
                item_id="item-detail-1",
                buyer_id="buyer-detail-1",
                sid="sid-detail-1",
                buyer_nick="Buyer Detail",
                buyer_id_source="message",
            )

        self.assertEqual(result, detail_result)
        fetch_order_detail_simple.assert_awaited_once_with(
            "order-detail-1",
            "unb=user1; cookie2=v2",
            headless=True,
            force_refresh=False,
            account_id="acc-order-detail-1",
        )
        fake_db.get_cookie_by_id.assert_called_once_with("acc-order-detail-1")
        fake_db.insert_or_update_order.assert_called_once_with(
            order_id="order-detail-1",
            item_id="item-detail-1",
            buyer_id="buyer-detail-1",
            buyer_nick="Buyer Detail",
            sid="sid-detail-1",
            spec_name=None,
            spec_value=None,
            spec_name_2=None,
            spec_value_2=None,
            quantity=None,
            amount=None,
            account_id="acc-order-detail-1",
            order_status="pending_ship",
            platform_created_at=None,
            platform_paid_at=None,
            platform_completed_at=None,
        )
        live.order_status_handler.handle_order_detail_fetched_status.assert_called_once_with(
            order_id="order-detail-1",
            account_id="acc-order-detail-1",
            context="订单详情已拉取",
        )
        live.order_status_handler.on_order_details_fetched.assert_called_once_with(
            "order-detail-1",
            account_id="acc-order-detail-1",
        )

    async def test_fetch_order_detail_info_rejects_default_or_blank_canonical_account_id_before_runtime(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-order-detail-only"
                live.account_id = invalid_account_id
                live.cookies_str = "unb=user1; cookie2=v2"
                live._safe_str = str
                live._order_detail_lock_times = {"preexisting": 1.0}
                live.order_status_handler = mock.Mock()
                live.order_status_handler.handle_order_detail_fetched_status.return_value = True
                live._apply_bargain_amount_override = mock.Mock(return_value=(None, "unknown"))
                live._should_reject_order_detail_status_update = mock.Mock(return_value=False)
                live._should_accept_order_detail_status_correction = mock.Mock(return_value=False)
                live._resolve_external_order_status = mock.Mock(return_value="pending_ship")
                live._select_buyer_identity_for_order_write = mock.Mock(
                    return_value=("buyer-detail-1", "Buyer Detail", False)
                )

                fake_db = mock.Mock()

                with mock.patch("db_manager.db_manager", fake_db), \
                     mock.patch(
                         "utils.order_detail_fetcher.fetch_order_detail_simple",
                         new=mock.AsyncMock(
                             side_effect=AssertionError(
                                 "missing canonical account_id should not fetch order detail via browser runtime"
                             )
                         ),
                         create=True,
                     ) as fetch_order_detail_simple:
                    result = await live.fetch_order_detail_info(
                        "order-detail-blocked-1",
                        item_id="item-detail-1",
                        buyer_id="buyer-detail-1",
                        sid="sid-detail-1",
                        buyer_nick="Buyer Detail",
                        buyer_id_source="message",
                    )

                self.assertIsNone(result)
                self.assertEqual(live._order_detail_lock_times, {"preexisting": 1.0})
                fetch_order_detail_simple.assert_not_awaited()
                fake_db.get_item_info.assert_not_called()
                fake_db.get_order_by_id.assert_not_called()
                fake_db.get_cookie_by_id.assert_not_called()
                fake_db.insert_or_update_order.assert_not_called()
                live.order_status_handler.handle_order_detail_fetched_status.assert_not_called()
                live.order_status_handler.on_order_details_fetched.assert_not_called()

    async def test_fetch_order_detail_info_scopes_detail_locks_by_canonical_account_id(self):
        shared_locks = {}
        shared_lock_times = {}

        def build_live(account_id):
            live = XianyuLive.__new__(XianyuLive)
            live._legacy_cookie_id = f"legacy-{account_id}"
            live.account_id = account_id
            live.cookies_str = "unb=user1; cookie2=v2"
            live._safe_str = str
            live._order_detail_locks = shared_locks
            live._order_detail_lock_times = shared_lock_times
            live.order_detail_retry_tasks = {}
            live.order_status_handler = mock.Mock()
            live.order_status_handler.handle_order_detail_fetched_status.return_value = True
            live._apply_bargain_amount_override = mock.Mock(return_value=(None, "unknown"))
            live._should_reject_order_detail_status_update = mock.Mock(return_value=False)
            live._should_accept_order_detail_status_correction = mock.Mock(return_value=False)
            live._resolve_external_order_status = mock.Mock(return_value="pending_ship")
            live._select_buyer_identity_for_order_write = mock.Mock(
                return_value=("buyer-detail-1", "Buyer Detail", False)
            )
            return live

        live_a = build_live("acc-order-detail-scope-a")
        live_b = build_live("acc-order-detail-scope-b")

        fake_db = mock.Mock()
        fake_db.get_item_info.return_value = None
        fake_db.get_order_by_id.return_value = None
        fake_db.get_cookie_by_id.return_value = {"id": "shared"}
        fake_db._normalize_order_status.side_effect = lambda value: value
        fake_db.insert_or_update_order.return_value = True
        detail_result = {
            "title": "detail-title",
            "order_status": "pending_ship",
            "order_status_source": "structured",
        }

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch(
                 "utils.order_detail_fetcher.fetch_order_detail_simple",
                 new=mock.AsyncMock(return_value=detail_result),
                 create=True,
             ), \
             mock.patch("builtins.print"):
            await live_a.fetch_order_detail_info("order-detail-shared", item_id="item-a", buyer_id="buyer-a")
            await live_b.fetch_order_detail_info("order-detail-shared", item_id="item-b", buyer_id="buyer-b")

        self.assertIn(
            ("acc-order-detail-scope-a", "order-detail-shared"),
            shared_locks,
        )
        self.assertIn(
            ("acc-order-detail-scope-b", "order-detail-shared"),
            shared_locks,
        )
        self.assertIn(
            ("acc-order-detail-scope-a", "order-detail-shared"),
            shared_lock_times,
        )
        self.assertIn(
            ("acc-order-detail-scope-b", "order-detail-shared"),
            shared_lock_times,
        )

    async def test_fetch_order_detail_info_cancels_scoped_retry_task_for_current_account_only(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-order-detail-cancel"
        live.account_id = "acc-order-detail-cancel"
        live.cookies_str = "unb=user1; cookie2=v2"
        live._safe_str = str
        live._order_detail_locks = {}
        live._order_detail_lock_times = {}
        current_scope = ("acc-order-detail-cancel", "order-detail-cancel-1")
        foreign_scope = ("acc-order-detail-other", "order-detail-cancel-1")
        current_retry_task = mock.Mock()
        current_retry_task.done.return_value = False
        foreign_retry_task = mock.Mock()
        foreign_retry_task.done.return_value = False
        live.order_detail_retry_tasks = {
            current_scope: current_retry_task,
            foreign_scope: foreign_retry_task,
        }
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_order_detail_fetched_status.return_value = True
        live._apply_bargain_amount_override = mock.Mock(return_value=(None, "unknown"))
        live._should_reject_order_detail_status_update = mock.Mock(return_value=False)
        live._should_accept_order_detail_status_correction = mock.Mock(return_value=False)
        live._resolve_external_order_status = mock.Mock(return_value="pending_ship")
        live._select_buyer_identity_for_order_write = mock.Mock(
            return_value=("buyer-detail-1", "Buyer Detail", False)
        )

        fake_db = mock.Mock()
        fake_db.get_item_info.return_value = None
        fake_db.get_order_by_id.return_value = None
        fake_db.get_cookie_by_id.return_value = {"id": "acc-order-detail-cancel"}
        fake_db._normalize_order_status.side_effect = lambda value: value
        fake_db.insert_or_update_order.return_value = True
        detail_result = {
            "title": "detail-title",
            "order_status": "pending_ship",
            "order_status_source": "structured",
        }

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch(
                 "utils.order_detail_fetcher.fetch_order_detail_simple",
                 new=mock.AsyncMock(return_value=detail_result),
                 create=True,
             ), \
             mock.patch("builtins.print"):
            result = await live.fetch_order_detail_info(
                "order-detail-cancel-1",
                item_id="item-detail-1",
                buyer_id="buyer-detail-1",
            )

        self.assertEqual(result, detail_result)
        current_retry_task.cancel.assert_called_once_with()
        self.assertNotIn(current_scope, live.order_detail_retry_tasks)
        self.assertIn(foreign_scope, live.order_detail_retry_tasks)
        foreign_retry_task.cancel.assert_not_called()

    async def test_handle_auto_delivery_prefers_account_id_alias_for_multi_quantity_lookup(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-auto-delivery-qty-1"
        live._safe_str = str
        live._ensure_item_owned_by_current_account = mock.AsyncMock(return_value=True)
        live._extract_order_id = mock.Mock(return_value="order-auto-qty-1")
        live.is_lock_held = mock.Mock(return_value=False)
        live.can_auto_delivery = mock.Mock(return_value=True)
        live._order_locks = {
            ("acc-auto-delivery-qty-1", "order-auto-qty-1"): asyncio.Lock()
        }
        live._lock_usage_times = {}
        live._get_pending_delivery_finalization_meta = mock.Mock(return_value=None)
        live._auto_delivery = mock.AsyncMock(
            return_value={
                "content": None,
                "error": "no delivery content",
                "delivery_steps": [],
            }
        )
        live._build_delivery_send_groups = mock.Mock(return_value=[])
        live._sync_order_delivery_progress = mock.Mock(return_value=None)
        live._activate_delivery_lock = mock.Mock()
        live._record_delivery_log = mock.Mock()
        live.send_delivery_failure_notification = mock.AsyncMock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-auto-qty-1",
            "account_id": "acc-auto-delivery-qty-1",
            "item_id": "item-qty-1",
            "buyer_id": "buyer-qty-1",
        }
        fake_db.get_item_multi_quantity_delivery_status.return_value = False

        with mock.patch("db_manager.db_manager", fake_db):
            await live._handle_auto_delivery(
                websocket=mock.Mock(),
                message={},
                send_user_name="buyer-qty-1",
                send_user_id="buyer-qty-1",
                item_id="item-qty-1",
                chat_id="chat-qty-1",
                msg_time="2026-05-10 12:34:56",
            )

        fake_db.get_item_multi_quantity_delivery_status.assert_called_once_with(
            "acc-auto-delivery-qty-1",
            "item-qty-1",
        )
        fake_db.get_order_by_id.assert_called_once_with(
            "order-auto-qty-1",
            account_id="acc-auto-delivery-qty-1",
        )

    async def test_handle_auto_delivery_scopes_order_lock_by_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-auto-lock"
        live.account_id = "acc-auto-lock-1"
        live._safe_str = str
        live._ensure_item_owned_by_current_account = mock.AsyncMock(return_value=True)
        live._extract_order_id = mock.Mock(return_value="order-auto-lock-1")
        live.is_lock_held = mock.Mock(return_value=False)
        live.can_auto_delivery = mock.Mock(return_value=True)
        live._order_locks = {
            ("acc-auto-lock-1", "order-auto-lock-1"): asyncio.Lock(),
        }
        live._lock_usage_times = {}
        live._get_pending_delivery_finalization_meta = mock.Mock(return_value=None)
        live._auto_delivery = mock.AsyncMock(
            return_value={
                "content": None,
                "error": "no delivery content",
                "delivery_steps": [],
            }
        )
        live._build_delivery_send_groups = mock.Mock(return_value=[])
        live._sync_order_delivery_progress = mock.Mock(return_value=None)
        live._activate_delivery_lock = mock.Mock()
        live._record_delivery_log = mock.Mock()
        live.send_delivery_failure_notification = mock.AsyncMock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-auto-lock-1",
            "account_id": "acc-auto-lock-1",
            "item_id": "item-auto-lock-1",
            "buyer_id": "buyer-auto-lock-1",
        }
        fake_db.get_item_multi_quantity_delivery_status.return_value = False

        with mock.patch("db_manager.db_manager", fake_db):
            await live._handle_auto_delivery(
                websocket=mock.Mock(),
                message={},
                send_user_name="buyer-auto-lock-1",
                send_user_id="buyer-auto-lock-1",
                item_id="item-auto-lock-1",
                chat_id="chat-auto-lock-1",
                msg_time="2026-05-11 15:10:00",
            )

        expected_lock_key = ("acc-auto-lock-1", "order-auto-lock-1")
        self.assertEqual(
            [mock.call(expected_lock_key), mock.call(expected_lock_key)],
            live.is_lock_held.call_args_list,
        )
        self.assertIn(expected_lock_key, live._lock_usage_times)
        self.assertNotIn("order-auto-lock-1", live._lock_usage_times)
        fake_db.get_order_by_id.assert_called_once_with(
            "order-auto-lock-1",
            account_id="acc-auto-lock-1",
        )

    async def test_handle_auto_delivery_rejects_missing_scoped_order_before_lock_and_send(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-auto-missing-scope"
        live.account_id = "acc-auto-missing-scope-1"
        live._safe_str = str
        live._ensure_item_owned_by_current_account = mock.AsyncMock(return_value=True)
        live._extract_order_id = mock.Mock(return_value="order-auto-missing-scope-1")
        live.is_lock_held = mock.Mock(return_value=False)
        live.can_auto_delivery = mock.Mock(return_value=True)
        live._order_locks = {
            ("acc-auto-missing-scope-1", "order-auto-missing-scope-1"): asyncio.Lock(),
        }
        live._lock_usage_times = {}
        live._get_pending_delivery_finalization_meta = mock.Mock(return_value=None)
        live._auto_delivery = mock.AsyncMock(
            return_value={
                "content": None,
                "error": "should not reach auto_delivery without scoped order",
                "delivery_steps": [],
            }
        )
        live._build_delivery_send_groups = mock.Mock(return_value=[])
        live._sync_order_delivery_progress = mock.Mock(return_value=None)
        live._activate_delivery_lock = mock.Mock()
        live._record_delivery_log = mock.Mock()
        live.send_delivery_failure_notification = mock.AsyncMock()
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = None
        fake_db.get_item_multi_quantity_delivery_status.return_value = False

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live._handle_auto_delivery(
                websocket=mock.Mock(),
                message={},
                send_user_name="buyer-missing-scope-1",
                send_user_id="buyer-missing-scope-1",
                item_id="item-missing-scope-1",
                chat_id="chat-missing-scope-1",
                msg_time="2026-05-12 02:10:00",
                message_data={},
            )

        fake_db.get_order_by_id.assert_called_once_with(
            "order-auto-missing-scope-1",
            account_id="acc-auto-missing-scope-1",
        )
        live._ensure_item_owned_by_current_account.assert_not_awaited()
        live.can_auto_delivery.assert_not_called()
        live.is_lock_held.assert_not_called()
        fake_db.get_item_multi_quantity_delivery_status.assert_not_called()
        live._get_pending_delivery_finalization_meta.assert_not_called()
        live._auto_delivery.assert_not_awaited()
        live._record_delivery_log.assert_called_once()
        self.assertEqual("failed", live._record_delivery_log.call_args.kwargs["status"])
        self.assertIn("未验证归属", live._record_delivery_log.call_args.kwargs["reason"])
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("未验证归属", messages)

    async def test_handle_auto_delivery_rejects_blank_canonical_account_id_before_scope_lookup(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-auto-delivery-blank"
        live.account_id = "   "
        live._safe_str = str
        live._ensure_item_owned_by_current_account = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip item ownership lookup")
        )
        live._extract_order_id = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip order extraction")
        )
        live._record_delivery_log = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live._handle_auto_delivery(
                websocket=mock.Mock(),
                message={},
                send_user_name="buyer-blank-1",
                send_user_id="buyer-blank-1",
                item_id="item-blank-1",
                chat_id="chat-blank-1",
                msg_time="2026-05-11 12:00:00",
                message_data={},
            )

        live._ensure_item_owned_by_current_account.assert_not_awaited()
        live._extract_order_id.assert_not_called()
        live._record_delivery_log.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("缺少 canonical account_id", messages)

    async def test_handle_auto_delivery_rejects_ambiguous_sid_lookup_without_reporting_missing_order(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-auto-delivery-ambiguous-sid"
        live.account_id = "acc-auto-delivery-ambiguous-sid"
        live._safe_str = str
        live._extract_order_id = mock.Mock(return_value=None)
        live._lookup_delivery_order_by_sid = mock.Mock(
            return_value={"match_type": "ambiguous_pending_ship", "order": None}
        )
        live._refresh_sid_lookup_if_needed = mock.AsyncMock(
            side_effect=lambda *args, **kwargs: args[1]
        )
        live._record_delivery_log = mock.Mock()
        mock_logger = mock.Mock()

        message = {
            "1": {
                "2": "sid-auto-ambiguous-full@goofish",
            }
        }

        with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live._handle_auto_delivery(
                websocket=mock.Mock(),
                message=message,
                send_user_name="buyer-auto-ambiguous-sid",
                send_user_id="buyer-auto-ambiguous-sid",
                item_id="item-auto-ambiguous-sid",
                chat_id="chat-auto-ambiguous-sid",
                msg_time="2026-05-12 07:20:00",
                message_data={},
            )

        live._lookup_delivery_order_by_sid.assert_called_once_with(
            "sid-auto-ambiguous-full@goofish",
            item_id="item-auto-ambiguous-sid",
            buyer_id="buyer-auto-ambiguous-sid",
            minutes=5,
            log_prefix=mock.ANY,
        )
        live._refresh_sid_lookup_if_needed.assert_awaited_once()
        live._record_delivery_log.assert_called_once()
        self.assertEqual("failed", live._record_delivery_log.call_args.kwargs["status"])
        self.assertIn("多个候选订单", live._record_delivery_log.call_args.kwargs["reason"])
        self.assertNotIn("鏈懡涓緟鍙戣揣璁㈠崟", live._record_delivery_log.call_args.kwargs["reason"])
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("多个候选", messages)
        self.assertNotIn("鏈懡涓緟鍙戣揣璁㈠崟", messages)

    async def test_handle_auto_delivery_preserves_account_scope_for_multi_quantity_pending_finalize_mix(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-auto-multi-finalize"
        live.account_id = "acc-auto-multi-finalize-1"
        live._safe_str = str
        live._ensure_item_owned_by_current_account = mock.AsyncMock(return_value=True)
        live._extract_order_id = mock.Mock(return_value="order-auto-multi-finalize-1")
        live.is_lock_held = mock.Mock(return_value=False)
        live.can_auto_delivery = mock.Mock(return_value=True)
        live._order_locks = {
            ("acc-auto-multi-finalize-1", "order-auto-multi-finalize-1"): asyncio.Lock(),
        }
        live._lock_usage_times = {}
        live.fetch_order_detail_info = mock.AsyncMock(return_value={"quantity": "2"})
        pending_finalize_meta = {
            "delivery_unit_index": 1,
            "rule_id": 11,
            "card_type": "text",
            "success": True,
        }
        live._get_pending_delivery_finalization_meta = mock.Mock(
            side_effect=[pending_finalize_meta, None]
        )
        live._auto_delivery = mock.AsyncMock(
            return_value={
                "content": "card-line-2",
                "delivery_steps": [{"type": "text", "content": "card-line-2"}],
                "rule_id": 22,
                "rule_keyword": "kw-2",
                "card_type": "text",
                "delivery_unit_index": 2,
            }
        )
        build_groups_capture = {}

        def build_delivery_send_groups(prepared_units, total_units):
            build_groups_capture["prepared_units"] = prepared_units
            build_groups_capture["total_units"] = total_units
            return [
                {
                    "mode": "single",
                    "delivery_steps": prepared_units[0]["delivery_steps"],
                    "units": prepared_units,
                }
            ]

        live._build_delivery_send_groups = mock.Mock(side_effect=build_delivery_send_groups)
        live._send_delivery_steps = mock.AsyncMock()
        live._mark_data_reservation_sent_if_needed = mock.Mock(return_value=True)
        live._release_data_reservation_if_needed = mock.Mock()
        real_persist_delivery_finalization_state = XianyuLive._persist_delivery_finalization_state.__get__(
            live, XianyuLive
        )
        live._persist_delivery_finalization_state = mock.Mock(
            wraps=real_persist_delivery_finalization_state
        )
        real_finalize_delivery_after_send = XianyuLive._finalize_delivery_after_send.__get__(
            live, XianyuLive
        )
        live._finalize_delivery_after_send = mock.AsyncMock(
            wraps=real_finalize_delivery_after_send
        )
        live._sync_order_delivery_progress = mock.Mock(
            return_value={
                "aggregate_status": "shipped",
                "finalized_count": 2,
                "pending_finalize_count": 0,
                "remaining_count": 0,
            }
        )
        live._activate_delivery_lock = mock.Mock()
        live._record_delivery_log = mock.Mock()
        live.send_delivery_failure_notification = mock.AsyncMock()
        live.is_auto_confirm_enabled = mock.Mock(return_value=False)

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-auto-multi-finalize-1",
            "account_id": "acc-auto-multi-finalize-1",
            "item_id": "item-auto-multi-finalize-1",
            "buyer_id": "buyer-auto-multi-finalize-1",
        }
        fake_db.get_item_multi_quantity_delivery_status.return_value = True
        fake_db.upsert_delivery_finalization_state.return_value = True

        with mock.patch("db_manager.db_manager", fake_db):
            await live._handle_auto_delivery(
                websocket=mock.Mock(),
                message={},
                send_user_name="Buyer Multi Finalize",
                send_user_id="buyer-auto-multi-finalize-1",
                item_id="item-auto-multi-finalize-1",
                chat_id="chat-auto-multi-finalize-1",
                msg_time="2026-05-12 14:20:00",
                message_data={},
            )

        self.assertGreater(len(fake_db.get_order_by_id.call_args_list), 0)
        for get_order_call in fake_db.get_order_by_id.call_args_list:
            self.assertEqual("order-auto-multi-finalize-1", get_order_call.args[0])
            self.assertEqual(
                "acc-auto-multi-finalize-1",
                get_order_call.kwargs["account_id"],
            )
        fake_db.get_item_multi_quantity_delivery_status.assert_called_once_with(
            "acc-auto-multi-finalize-1",
            "item-auto-multi-finalize-1",
        )
        live.fetch_order_detail_info.assert_awaited_once_with(
            "order-auto-multi-finalize-1",
            "item-auto-multi-finalize-1",
            "buyer-auto-multi-finalize-1",
        )
        live._get_pending_delivery_finalization_meta.assert_has_calls(
            [
                mock.call("order-auto-multi-finalize-1", 1),
                mock.call("order-auto-multi-finalize-1", 2),
            ]
        )
        self.assertEqual(2, build_groups_capture["total_units"])
        self.assertEqual([2], [unit["unit_index"] for unit in build_groups_capture["prepared_units"]])
        live._auto_delivery.assert_awaited_once_with(
            "item-auto-multi-finalize-1",
            "待获取商品信息",
            "order-auto-multi-finalize-1",
            "buyer-auto-multi-finalize-1",
            "chat-auto-multi-finalize-1",
            "Buyer Multi Finalize",
            include_meta=True,
            delivery_unit_index=2,
        )
        live._send_delivery_steps.assert_awaited_once()
        send_delivery_call = live._send_delivery_steps.await_args
        self.assertEqual("buyer-auto-multi-finalize-1", send_delivery_call.args[2])
        self.assertIn("2/2", send_delivery_call.kwargs["log_prefix"])
        live._finalize_delivery_after_send.assert_has_awaits(
            [
                mock.call(
                    delivery_meta=pending_finalize_meta,
                    order_id="order-auto-multi-finalize-1",
                    item_id="item-auto-multi-finalize-1",
                ),
                mock.call(
                    delivery_meta=mock.ANY,
                    order_id="order-auto-multi-finalize-1",
                    item_id="item-auto-multi-finalize-1",
                ),
            ]
        )
        post_send_delivery_meta = live._finalize_delivery_after_send.await_args_list[1].kwargs["delivery_meta"]
        self.assertEqual(2, post_send_delivery_meta["delivery_unit_index"])
        self.assertEqual(22, post_send_delivery_meta["rule_id"])
        live._sync_order_delivery_progress.assert_called_once_with(
            order_id="order-auto-multi-finalize-1",
            account_id="acc-auto-multi-finalize-1",
            expected_quantity=2,
            context="自动发货进度同步",
        )
        live._persist_delivery_finalization_state.assert_has_calls(
            [
                mock.call(
                    order_id="order-auto-multi-finalize-1",
                    item_id="item-auto-multi-finalize-1",
                    buyer_id="buyer-auto-multi-finalize-1",
                    delivery_meta=pending_finalize_meta,
                    channel="auto",
                    status="finalized",
                ),
                mock.call(
                    order_id="order-auto-multi-finalize-1",
                    item_id="item-auto-multi-finalize-1",
                    buyer_id="buyer-auto-multi-finalize-1",
                    delivery_meta=mock.ANY,
                    channel="auto",
                    status="sent",
                ),
                mock.call(
                    order_id="order-auto-multi-finalize-1",
                    item_id="item-auto-multi-finalize-1",
                    buyer_id="buyer-auto-multi-finalize-1",
                    delivery_meta=mock.ANY,
                    channel="auto",
                    status="finalized",
                ),
            ]
        )
        fake_db.upsert_delivery_finalization_state.assert_has_calls(
            [
                mock.call(
                    order_id="order-auto-multi-finalize-1",
                    unit_index=1,
                    account_id="acc-auto-multi-finalize-1",
                    item_id="item-auto-multi-finalize-1",
                    buyer_id="buyer-auto-multi-finalize-1",
                    channel="auto",
                    status="finalized",
                    delivery_meta=pending_finalize_meta,
                    last_error=None,
                ),
                mock.call(
                    order_id="order-auto-multi-finalize-1",
                    unit_index=2,
                    account_id="acc-auto-multi-finalize-1",
                    item_id="item-auto-multi-finalize-1",
                    buyer_id="buyer-auto-multi-finalize-1",
                    channel="auto",
                    status="sent",
                    delivery_meta=mock.ANY,
                    last_error=None,
                ),
                mock.call(
                    order_id="order-auto-multi-finalize-1",
                    unit_index=2,
                    account_id="acc-auto-multi-finalize-1",
                    item_id="item-auto-multi-finalize-1",
                    buyer_id="buyer-auto-multi-finalize-1",
                    channel="auto",
                    status="finalized",
                    delivery_meta=mock.ANY,
                    last_error=None,
                ),
            ]
        )
        fake_db.increment_delivery_times.assert_has_calls(
            [
                mock.call(11),
                mock.call(22),
            ]
        )
        live._activate_delivery_lock.assert_called_once_with(
            ("acc-auto-multi-finalize-1", "order-auto-multi-finalize-1"),
            delay_minutes=10,
        )
        live.send_delivery_failure_notification.assert_awaited_once()
        notify_call = live.send_delivery_failure_notification.await_args
        self.assertEqual(
            (
                "Buyer Multi Finalize",
                "buyer-auto-multi-finalize-1",
                "item-auto-multi-finalize-1",
            ),
            notify_call.args[:3],
        )
        self.assertIn("2/2", notify_call.args[3])
        self.assertEqual("chat-auto-multi-finalize-1", notify_call.args[4])

    async def test_handle_message_forwards_current_account_id_to_on_order_id_extracted_and_buyer_nick_update(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-msg-handle-1"
        live._safe_str = str
        live.myid = "seller-self"
        live.yifan_account_lock = asyncio.Lock()
        live.yifan_account_waiting = {}
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_system_message.return_value = False
        live._extract_order_id = mock.Mock(return_value="order-msg-1")
        live._extract_order_message_context = mock.Mock(
            return_value={
                "buyer_id": "buyer-msg-1",
                "buyer_id_source": "message",
                "item_id": "item-msg-1",
                "sid": "chat-msg-1@goofish",
                "buyer_nick": "Buyer Alias",
            }
        )
        live._preload_basic_order_info = mock.Mock(return_value=False)
        live.fetch_order_detail_info = mock.AsyncMock(return_value={"order_id": "order-msg-1"})
        live.extract_item_id_from_message = mock.Mock(return_value="item-msg-1")
        live._extract_message_id_from_chat_payload = mock.Mock(return_value="dedupe-msg-1")
        live._classify_message_route = mock.Mock(
            return_value={
                "route": "user_chat",
                "order_status_signal": None,
                "should_notify": False,
                "allow_auto_reply": False,
                "is_system_message": False,
                "is_group_message": False,
                "message_direction": 2,
                "content_type": 1,
            }
        )
        live._sanitize_buyer_nick = mock.Mock(return_value="Buyer Alias")

        message = {
            "1": {
                "2": "chat-msg-1@goofish",
                "5": 1710000000000,
                "7": 2,
                "10": {
                    "senderNick": "Buyer Alias",
                    "senderUserId": "buyer-msg-1",
                    "reminderContent": "你好，在吗？",
                },
            }
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        fake_db = mock.Mock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch("db_manager.db_manager", fake_db):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-handle-1",
            )

        live.order_status_handler.on_order_id_extracted.assert_called_once_with(
            order_id="order-msg-1",
            account_id="acc-msg-handle-1",
            message=message,
            match_context={
                "sid": "chat-msg-1@goofish",
                "buyer_id": "buyer-msg-1",
                "item_id": "item-msg-1",
            },
        )
        fake_db.update_buyer_nick_by_buyer_id.assert_called_once_with(
            "buyer-msg-1",
            "Buyer Alias",
            account_id="acc-msg-handle-1",
        )

    async def test_handle_message_skips_order_side_effects_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-msg-scope-blank"
        live.account_id = "   "
        live._safe_str = str
        live.myid = "seller-self"
        live.yifan_account_lock = asyncio.Lock()
        live.yifan_account_waiting = {}
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_system_message.return_value = False
        live._extract_order_id = mock.Mock(return_value="order-msg-scope-blank")
        live._extract_order_message_context = mock.Mock(
            return_value={
                "buyer_id": "buyer-msg-scope-blank",
                "buyer_id_source": "message",
                "item_id": "item-msg-scope-blank",
                "sid": "chat-msg-scope-blank@goofish",
                "buyer_nick": "Buyer Blank",
            }
        )
        live._preload_basic_order_info = mock.Mock(return_value=False)
        live.fetch_order_detail_info = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip order detail fetch")
        )
        live.extract_item_id_from_message = mock.Mock(return_value="item-msg-scope-blank")
        live._extract_message_id_from_chat_payload = mock.Mock(return_value="dedupe-msg-scope-blank")
        live._classify_message_route = mock.Mock(
            return_value={
                "route": "user_chat",
                "order_status_signal": None,
                "should_notify": False,
                "allow_auto_reply": False,
                "is_system_message": False,
                "is_group_message": False,
                "message_direction": 2,
                "content_type": 1,
            }
        )
        live._sanitize_buyer_nick = mock.Mock(return_value="Buyer Blank")
        mock_logger = mock.Mock()

        message = {
            "1": {
                "2": "chat-msg-scope-blank@goofish",
                "5": 1710000000000,
                "7": 2,
                "10": {
                    "senderNick": "Buyer Blank",
                    "senderUserId": "buyer-msg-scope-blank",
                    "reminderContent": "你好，在吗？",
                },
            }
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        fake_db = mock.Mock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-scope-blank",
            )

        live.order_status_handler.on_order_id_extracted.assert_not_called()
        live._preload_basic_order_info.assert_not_called()
        live.fetch_order_detail_info.assert_not_awaited()
        fake_db.update_buyer_nick_by_buyer_id.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("缺少 canonical account_id", messages)

    async def test_handle_message_avoids_legacy_current_account_id_fallback(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-msg-fallback"
        live.account_id = "acc-msg-canonical-only"
        live._safe_str = str
        live._current_account_id = mock.Mock(
            side_effect=AssertionError("should not read legacy current account fallback")
        )
        live._is_current_account_enabled = mock.Mock(return_value=False)
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()

        await live.handle_message({}, websocket, msg_id="msg-no-legacy-fallback")

        live._is_current_account_enabled.assert_called_once_with()
        websocket.send.assert_not_awaited()

    async def test_handle_message_transaction_closed_red_reminder_uses_current_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-msg-handle-2"
        live._safe_str = str
        live.order_status_handler = mock.Mock()
        live._extract_order_id = mock.Mock(return_value=None)
        live._extract_order_message_context = mock.Mock(return_value={})
        live.extract_item_id_from_message = mock.Mock(return_value="item-closed-1")

        message = {
            "1": {
                "5": 1710000000000,
                "10": {
                    "senderUserId": "buyer-closed-1",
                },
            },
            "3": {
                "redReminder": "交易关闭",
            },
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-handle-2",
            )

        live.order_status_handler.handle_red_reminder_order_status.assert_called_once_with(
            red_reminder="交易关闭",
            message=message,
            user_id="buyer-closed-1",
            account_id="acc-msg-handle-2",
            msg_time=mock.ANY,
            match_context={
                "sid": None,
                "buyer_id": "buyer-closed-1",
                "item_id": "item-closed-1",
            },
        )

    async def test_handle_message_skips_order_status_handlers_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-msg-handler-blank"
        live.account_id = "   "
        live._safe_str = str
        live.myid = "seller-self"
        live.yifan_account_lock = asyncio.Lock()
        live.yifan_account_waiting = {}
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_system_message.return_value = False
        live._extract_order_id = mock.Mock(return_value=None)
        live.extract_item_id_from_message = mock.Mock(return_value="item-msg-handler-blank")
        live._extract_message_id_from_chat_payload = mock.Mock(return_value="dedupe-msg-handler-blank")
        live._classify_message_route = mock.Mock(
            return_value={
                "route": "system_notice",
                "order_status_signal": None,
                "should_notify": False,
                "allow_auto_reply": False,
                "is_system_message": True,
                "is_group_message": False,
                "message_direction": 1,
                "content_type": 6,
            }
        )
        mock_logger = mock.Mock()

        message = {
            "1": {
                "2": "chat-system-blank@goofish",
                "5": 1710000000000,
                "7": 1,
                "10": {
                    "senderNick": "系统消息",
                    "senderUserId": "buyer-system-blank",
                    "reminderContent": "[系统提示]",
                },
            },
            "3": {
                "redReminder": "交易关闭",
            },
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-handler-blank",
            )

        live.order_status_handler.handle_system_message.assert_not_called()
        live.order_status_handler.handle_red_reminder_message.assert_not_called()
        live.order_status_handler.handle_red_reminder_order_status.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("缺少 canonical account_id", messages)

    async def test_handle_message_waiting_seller_ship_skips_simple_auto_delivery_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-msg-simple-delivery-blank"
        live.account_id = "   "
        live._safe_str = str
        live.order_status_handler = None
        live.extract_item_id_from_message = mock.Mock(return_value="item-simple-delivery-blank")
        live._lookup_delivery_order_by_sid = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip sid lookup")
        )
        live._refresh_sid_lookup_if_needed = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip sid refresh")
        )
        live._handle_simple_message_auto_delivery = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip simple auto delivery")
        )
        live.is_auto_confirm_enabled = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip auto confirm gate")
        )
        mock_logger = mock.Mock()

        message = {
            "1": "chat-simple-delivery-blank@goofish",
            "3": {
                "redReminder": "等待卖家发货",
            },
            "4": {
                "senderUserId": "buyer-simple-delivery-blank",
            },
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-simple-delivery-blank",
            )

        live.extract_item_id_from_message.assert_called_once_with(message)
        live.is_auto_confirm_enabled.assert_not_called()
        live._lookup_delivery_order_by_sid.assert_not_called()
        live._refresh_sid_lookup_if_needed.assert_not_awaited()
        live._handle_simple_message_auto_delivery.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("缺少 canonical account_id", messages)

    async def test_handle_message_simple_pending_ship_uses_account_scoped_lock_key(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-msg-simple-scope"
        live.account_id = "acc-msg-simple-scope-1"
        live._safe_str = str
        live.order_status_handler = None
        live.extract_item_id_from_message = mock.Mock(return_value="item-msg-simple-scope-fallback")
        live._lookup_delivery_order_by_sid = mock.Mock(
            return_value={
                "match_type": "pending_ship",
                "order": {
                    "order_id": "order-msg-simple-scope-1",
                    "item_id": "item-msg-simple-scope-1",
                    "buyer_id": "buyer-msg-simple-scope-1",
                },
            }
        )
        live._refresh_sid_lookup_if_needed = mock.AsyncMock(
            side_effect=lambda *args, **kwargs: args[1]
        )
        live.can_auto_delivery = mock.Mock(return_value=True)
        live.is_lock_held = mock.Mock(return_value=False)
        live._handle_simple_message_auto_delivery = mock.AsyncMock()
        live.is_auto_confirm_enabled = mock.Mock(return_value=True)

        message = {
            "1": "chat-simple-scope-1@goofish",
            "3": {
                "redReminder": "等待卖家发货",
            },
            "4": {
                "senderUserId": "buyer-msg-simple-scope-1",
            },
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-simple-scope-1",
            )

        live._lookup_delivery_order_by_sid.assert_called_once_with(
            "chat-simple-scope-1@goofish",
            item_id="item-msg-simple-scope-fallback",
            buyer_id="buyer-msg-simple-scope-1",
            minutes=5,
            log_prefix=mock.ANY,
        )
        expected_lock_key = ("acc-msg-simple-scope-1", "order-msg-simple-scope-1")
        live.is_lock_held.assert_called_once_with(expected_lock_key)
        live._handle_simple_message_auto_delivery.assert_awaited_once_with(
            websocket=websocket,
            order_id="order-msg-simple-scope-1",
            item_id="item-msg-simple-scope-1",
            user_id="buyer-msg-simple-scope-1",
            chat_id="chat-simple-scope-1",
            msg_time=mock.ANY,
            msg_id="msg-simple-scope-1",
        )

    async def test_handle_message_simple_pending_ship_rejects_ambiguous_sid_lookup_without_missing_order_log(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-msg-simple-ambiguous"
        live.account_id = "acc-msg-simple-ambiguous-1"
        live._safe_str = str
        live.order_status_handler = None
        live.extract_item_id_from_message = mock.Mock(return_value="item-msg-simple-ambiguous")
        live._lookup_delivery_order_by_sid = mock.Mock(
            return_value={"match_type": "ambiguous_pending_ship", "order": None}
        )
        live._refresh_sid_lookup_if_needed = mock.AsyncMock(
            side_effect=lambda *args, **kwargs: args[1]
        )
        live.can_auto_delivery = mock.Mock(
            side_effect=AssertionError("ambiguous sid lookup should fail closed before cooldown checks")
        )
        live.is_lock_held = mock.Mock(
            side_effect=AssertionError("ambiguous sid lookup should fail closed before lock checks")
        )
        live._handle_simple_message_auto_delivery = mock.AsyncMock(
            side_effect=AssertionError("ambiguous sid lookup should not continue into simple auto delivery")
        )
        live.is_auto_confirm_enabled = mock.Mock(return_value=True)
        mock_logger = mock.Mock()

        message = {
            "1": "chat-simple-ambiguous@goofish",
            "3": {
                "redReminder": "等待卖家发货",
            },
            "4": {
                "senderUserId": "buyer-msg-simple-ambiguous",
            },
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-simple-ambiguous-1",
            )

        live._lookup_delivery_order_by_sid.assert_called_once_with(
            "chat-simple-ambiguous@goofish",
            item_id="item-msg-simple-ambiguous",
            buyer_id="buyer-msg-simple-ambiguous",
            minutes=5,
            log_prefix=mock.ANY,
        )
        live._refresh_sid_lookup_if_needed.assert_awaited_once()
        live._handle_simple_message_auto_delivery.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("多个候选", messages)
        self.assertNotIn("鏈壘鍒皊id", messages)

    async def test_handle_message_order_status_trigger_skips_auto_delivery_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-msg-auto-delivery-blank"
        live.account_id = "   "
        live._safe_str = str
        live.myid = "seller-self"
        live.yifan_account_lock = asyncio.Lock()
        live.yifan_account_waiting = {}
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_system_message.return_value = False
        live._extract_order_id = mock.Mock(return_value=None)
        live.extract_item_id_from_message = mock.Mock(return_value="item-auto-delivery-blank")
        live._extract_message_id_from_chat_payload = mock.Mock(return_value="dedupe-msg-auto-delivery-blank")
        live._classify_message_route = mock.Mock(
            return_value={
                "route": "order_status",
                "order_status_signal": None,
                "should_notify": False,
                "allow_auto_reply": False,
                "is_system_message": True,
                "is_group_message": False,
                "message_direction": 1,
                "content_type": 6,
            }
        )
        live._is_auto_delivery_trigger = mock.Mock(return_value=True)
        live._handle_auto_delivery = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip auto delivery")
        )
        live.is_auto_confirm_enabled = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip auto confirm gate")
        )
        mock_logger = mock.Mock()

        message = {
            "1": {
                "2": "chat-auto-delivery-blank@goofish",
                "5": 1710000000000,
                "7": 1,
                "10": {
                    "senderNick": "系统提醒",
                    "senderUserId": "buyer-auto-delivery-blank",
                    "reminderContent": "我已付款，等待你发货",
                },
            }
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-auto-delivery-blank",
            )

        live.order_status_handler.handle_system_message.assert_not_called()
        live._is_auto_delivery_trigger.assert_called_once_with("我已付款，等待你发货")
        live.is_auto_confirm_enabled.assert_not_called()
        live._handle_auto_delivery.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("缺少 canonical account_id", messages)

    async def test_handle_message_waiting_bargain_card_skips_freeshipping_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-msg-bargain-card-blank"
        live.account_id = "   "
        live._safe_str = str
        live.myid = "seller-self"
        live.yifan_account_lock = asyncio.Lock()
        live.yifan_account_waiting = {}
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_system_message.return_value = False
        live.extract_item_id_from_message = mock.Mock(return_value="item-bargain-card-blank")
        live._extract_message_id_from_chat_payload = mock.Mock(return_value="dedupe-msg-bargain-card-blank")
        live._classify_message_route = mock.Mock(
            return_value={
                "route": "user_chat",
                "order_status_signal": None,
                "should_notify": False,
                "allow_auto_reply": False,
                "is_system_message": True,
                "is_group_message": False,
                "message_direction": 1,
                "content_type": 6,
            }
        )
        live._is_auto_delivery_trigger = mock.Mock(return_value=False)
        live.is_auto_confirm_enabled = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip bargain card auto confirm gate")
        )
        live._ensure_item_owned_by_current_account = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip bargain card item ownership lookup")
        )
        live._extract_order_id = mock.Mock(return_value="order-bargain-card-blank")
        live._mark_order_bargain_flow = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip bargain flow marking")
        )
        live.auto_freeshipping = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip auto freeshipping")
        )
        mock_logger = mock.Mock()

        card_payload = json.dumps(
            {
                "dxCard": {
                    "item": {
                        "main": {
                            "exContent": {
                                "title": "我已小刀，待刀成",
                            }
                        }
                    }
                }
            },
            ensure_ascii=False,
        )
        message = {
            "1": {
                "2": "chat-bargain-card-blank@goofish",
                "5": 1710000000000,
                "7": 1,
                "10": {
                    "senderNick": "系统提醒",
                    "senderUserId": "buyer-bargain-card-blank",
                    "reminderContent": "[卡片消息]",
                },
                "6": {
                    "3": {
                        "4": 6,
                        "5": card_payload,
                    }
                },
            }
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-bargain-card-blank",
            )

        live.is_auto_confirm_enabled.assert_not_called()
        live._ensure_item_owned_by_current_account.assert_not_awaited()
        live._mark_order_bargain_flow.assert_not_called()
        live.auto_freeshipping.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    async def test_handle_message_ready_to_ship_card_skips_auto_delivery_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-msg-ready-ship-card-blank"
        live.account_id = "   "
        live._safe_str = str
        live.myid = "seller-self"
        live.yifan_account_lock = asyncio.Lock()
        live.yifan_account_waiting = {}
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_system_message.return_value = False
        live.extract_item_id_from_message = mock.Mock(return_value="item-ready-ship-card-blank")
        live._extract_message_id_from_chat_payload = mock.Mock(return_value="dedupe-msg-ready-ship-card-blank")
        live._classify_message_route = mock.Mock(
            return_value={
                "route": "user_chat",
                "order_status_signal": None,
                "should_notify": False,
                "allow_auto_reply": False,
                "is_system_message": True,
                "is_group_message": False,
                "message_direction": 1,
                "content_type": 6,
            }
        )
        live._is_auto_delivery_trigger = mock.Mock(return_value=False)
        live._extract_order_id = mock.Mock(return_value="order-ready-ship-card-blank")
        live._mark_order_bargain_flow = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip ready-to-ship bargain marking")
        )
        live.is_auto_confirm_enabled = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip ready-to-ship auto confirm gate")
        )
        live._handle_auto_delivery = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip ready-to-ship auto delivery")
        )
        mock_logger = mock.Mock()

        card_payload = json.dumps(
            {
                "dxCard": {
                    "item": {
                        "main": {
                            "exContent": {
                                "title": "我已成功小刀，待发货",
                            }
                        }
                    }
                }
            },
            ensure_ascii=False,
        )
        message = {
            "1": {
                "2": "chat-ready-ship-card-blank@goofish",
                "5": 1710000000000,
                "7": 1,
                "10": {
                    "senderNick": "系统提醒",
                    "senderUserId": "buyer-ready-ship-card-blank",
                    "reminderContent": "[卡片消息]",
                },
                "6": {
                    "3": {
                        "4": 6,
                        "5": card_payload,
                    }
                },
            }
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-ready-ship-card-blank",
            )

        live._mark_order_bargain_flow.assert_not_called()
        live.is_auto_confirm_enabled.assert_not_called()
        live._handle_auto_delivery.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    async def test_handle_message_system_message_uses_current_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-msg-handle-3"
        live._safe_str = str
        live.myid = "seller-self"
        live.yifan_account_lock = asyncio.Lock()
        live.yifan_account_waiting = {}
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_system_message.return_value = True
        live._extract_order_id = mock.Mock(return_value=None)
        live.extract_item_id_from_message = mock.Mock(return_value="item-system-1")
        live._extract_message_id_from_chat_payload = mock.Mock(return_value="dedupe-msg-3")
        live._classify_message_route = mock.Mock(
            return_value={
                "route": "system_notice",
                "order_status_signal": None,
                "should_notify": False,
                "allow_auto_reply": False,
                "is_system_message": True,
                "is_group_message": False,
                "message_direction": 1,
                "content_type": 6,
            }
        )

        message = {
            "1": {
                "2": "chat-system-1@goofish",
                "5": 1710000000000,
                "7": 1,
                "10": {
                    "senderNick": "系统消息",
                    "senderUserId": "buyer-system-1",
                    "reminderContent": "[系统提示]",
                },
            }
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-handle-3",
            )

        live.order_status_handler.handle_system_message.assert_called_once_with(
            message=message,
            send_message="[系统提示]",
            account_id="acc-msg-handle-3",
            msg_time=mock.ANY,
            match_context={
                "sid": "chat-system-1@goofish",
                "buyer_id": "buyer-system-1",
                "item_id": "item-system-1",
            },
        )

    async def test_handle_message_manual_send_pauses_chat_with_current_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-msg-handle-4"
        live._safe_str = str
        live.myid = "seller-self"
        live.yifan_account_lock = asyncio.Lock()
        live.yifan_account_waiting = {}
        live.order_status_handler = mock.Mock()
        live._extract_order_id = mock.Mock(return_value=None)
        live.extract_item_id_from_message = mock.Mock(return_value="item-self-1")
        live._extract_message_id_from_chat_payload = mock.Mock(return_value="dedupe-msg-4")
        live._classify_message_route = mock.Mock(
            return_value={
                "route": "user_chat",
                "order_status_signal": None,
                "should_notify": False,
                "allow_auto_reply": True,
                "is_system_message": False,
                "is_group_message": False,
                "message_direction": 2,
                "content_type": 1,
            }
        )

        message = {
            "1": {
                "2": "chat-self-1@goofish",
                "5": 1710000000000,
                "7": 2,
                "10": {
                    "senderNick": "卖家自己",
                    "senderUserId": "seller-self",
                    "reminderContent": "手动回一句",
                },
            }
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch.object(XianyuAutoAsync.pause_manager, "pause_chat") as pause_chat:
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-handle-4",
            )

        pause_chat.assert_called_once_with("chat-self-1", "acc-msg-handle-4")

    async def test_process_chat_message_reply_checks_pause_with_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-pause-check"
        live.account_id = "acc-pause-check-1"
        live._safe_str = str
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()

        with mock.patch.object(
            XianyuAutoAsync,
            "AUTO_REPLY",
            {"enabled": True},
        ), \
             mock.patch.object(
                 XianyuAutoAsync.pause_manager,
                 "is_chat_paused",
                 return_value=True,
             ) as is_chat_paused, \
             mock.patch.object(
                 XianyuAutoAsync.pause_manager,
                 "get_remaining_pause_time",
                 return_value=90,
             ) as get_remaining_pause_time:
            result = await live._process_chat_message_reply(
                message_data={},
                websocket=websocket,
                send_user_name="Buyer Pause",
                send_user_id="buyer-pause-check-1",
                send_message="hello",
                item_id="item-pause-check-1",
                chat_id="chat-pause-check-1",
                msg_time="2026-05-12 00:00:00",
            )

        self.assertTrue(result)
        is_chat_paused.assert_called_once_with(
            "chat-pause-check-1",
            account_id="acc-pause-check-1",
        )
        get_remaining_pause_time.assert_called_once_with(
            "chat-pause-check-1",
            account_id="acc-pause-check-1",
        )

    async def test_handle_message_manual_send_rejects_default_or_blank_canonical_account_id_before_pause(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = invalid_account_id
                live._safe_str = str
                live.myid = "seller-self"
                live.yifan_account_lock = asyncio.Lock()
                live.yifan_account_waiting = {}
                live.order_status_handler = mock.Mock()
                live._extract_order_id = mock.Mock(return_value=None)
                live.extract_item_id_from_message = mock.Mock(return_value="item-self-1")
                live._extract_message_id_from_chat_payload = mock.Mock(return_value="dedupe-msg-4")
                live._classify_message_route = mock.Mock(
                    return_value={
                        "route": "user_chat",
                        "order_status_signal": None,
                        "should_notify": False,
                        "allow_auto_reply": True,
                        "is_system_message": False,
                        "is_group_message": False,
                        "message_direction": 2,
                        "content_type": 1,
                    }
                )

                message = {
                    "1": {
                        "2": "chat-self-1@goofish",
                        "5": 1710000000000,
                        "7": 2,
                        "10": {
                            "senderNick": "卖家自己",
                            "senderUserId": "seller-self",
                            "reminderContent": "手动回一句",
                        },
                    }
                }
                websocket = mock.Mock()
                websocket.send = mock.AsyncMock()
                cookie_manager_module = self._build_cookie_manager_module(enabled=True)

                with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
                     mock.patch.object(XianyuAutoAsync.pause_manager, "pause_chat") as pause_chat:
                    await live.handle_message(
                        self._build_sync_package(message),
                        websocket,
                        msg_id="msg-handle-4-invalid",
                    )

                pause_chat.assert_not_called()

    async def test_handle_message_prefers_account_id_alias_for_cookie_status_gate(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-msg-gate-1"
        live._safe_str = str

        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=False)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}):
            await live.handle_message(
                {"headers": {"mid": "mid-gate-1", "sid": "sid-gate-1"}},
                websocket,
                msg_id="msg-gate-1",
            )

        cookie_manager_module.manager.get_cookie_status.assert_called_once_with(
            "acc-msg-gate-1"
        )
        websocket.send.assert_not_awaited()

    async def test_handle_message_non_terminal_red_reminder_message_uses_current_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-msg-handle-5"
        live._safe_str = str
        live.myid = "seller-self"
        live.yifan_account_lock = asyncio.Lock()
        live.yifan_account_waiting = {}
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_system_message.return_value = False
        live._extract_order_id = mock.Mock(return_value=None)
        live.extract_item_id_from_message = mock.Mock(return_value="item-red-1")
        live._extract_message_id_from_chat_payload = mock.Mock(return_value="dedupe-msg-5")
        live._classify_message_route = mock.Mock(
            return_value={
                "route": "system_notice",
                "order_status_signal": None,
                "should_notify": False,
                "allow_auto_reply": False,
                "is_system_message": True,
                "is_group_message": False,
                "message_direction": 1,
                "content_type": 6,
            }
        )

        message = {
            "1": {
                "2": "chat-red-1@goofish",
                "5": 1710000000000,
                "7": 1,
                "10": {
                    "senderNick": "系统提醒",
                    "senderUserId": "buyer-red-1",
                    "reminderContent": "[提醒消息]",
                },
            },
            "3": {
                "redReminder": "卖家已发货",
                "userId": "buyer-red-1",
            },
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-handle-5",
            )

        live.order_status_handler.handle_red_reminder_message.assert_called_once_with(
            message=message,
            red_reminder="卖家已发货",
            user_id="buyer-red-1",
            account_id="acc-msg-handle-5",
            msg_time=mock.ANY,
            match_context={
                "sid": "chat-red-1@goofish",
                "buyer_id": "buyer-red-1",
                "item_id": "item-red-1",
            },
        )

    async def test_handle_simple_message_auto_delivery_syncs_progress_with_current_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-simple-progress-1"
        live._safe_str = str
        live._order_locks = {
            ("acc-simple-progress-1", "order-simple-1"): asyncio.Lock()
        }
        live._lock_usage_times = {}
        live._ensure_item_owned_by_current_account = mock.AsyncMock(return_value=True)
        live.can_auto_delivery = mock.Mock(return_value=True)
        live.is_lock_held = mock.Mock(return_value=False)
        live._get_pending_delivery_finalization_meta = mock.Mock(
            return_value={"delivery_unit_index": 1, "rule_id": 7}
        )
        live._finalize_delivery_after_send = mock.AsyncMock(return_value={"success": True})
        live._persist_delivery_finalization_state = mock.Mock()
        live._sync_order_delivery_progress = mock.Mock(return_value={"aggregate_status": "shipped"})
        live._activate_delivery_lock = mock.Mock()
        live._record_delivery_log = mock.Mock()
        live.send_delivery_failure_notification = mock.AsyncMock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-simple-1",
            "account_id": "acc-simple-progress-1",
            "item_id": "item-simple-1",
            "buyer_id": "buyer-simple-1",
        }

        with mock.patch("db_manager.db_manager", fake_db):
            await live._handle_simple_message_auto_delivery(
                websocket=mock.Mock(),
                order_id="order-simple-1",
                item_id="item-simple-1",
                user_id="buyer-simple-1",
                chat_id="chat-simple-1",
                msg_time="2026-05-09 21:05:00",
                msg_id="msg-simple-1",
            )

        live._sync_order_delivery_progress.assert_called_once_with(
            order_id="order-simple-1",
            account_id="acc-simple-progress-1",
            expected_quantity=1,
            context="自动发货补完成收尾成功",
        )
        fake_db.get_order_by_id.assert_called_once_with(
            "order-simple-1",
            account_id="acc-simple-progress-1",
        )

    async def test_handle_simple_message_auto_delivery_scopes_order_lock_by_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-simple-lock"
        live.account_id = "acc-simple-lock-1"
        live._safe_str = str
        live._order_locks = {
            ("acc-simple-lock-1", "order-simple-lock-1"): asyncio.Lock(),
        }
        live._lock_usage_times = {}
        live._ensure_item_owned_by_current_account = mock.AsyncMock(return_value=True)
        live.can_auto_delivery = mock.Mock(return_value=True)
        live.is_lock_held = mock.Mock(return_value=False)
        live._get_pending_delivery_finalization_meta = mock.Mock(
            return_value={"delivery_unit_index": 1, "rule_id": 9}
        )
        live._finalize_delivery_after_send = mock.AsyncMock(return_value={"success": True})
        live._persist_delivery_finalization_state = mock.Mock()
        live._sync_order_delivery_progress = mock.Mock(return_value={"aggregate_status": "shipped"})
        live._activate_delivery_lock = mock.Mock()
        live._record_delivery_log = mock.Mock()
        live.send_delivery_failure_notification = mock.AsyncMock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-simple-lock-1",
            "account_id": "acc-simple-lock-1",
            "item_id": "item-simple-lock-1",
            "buyer_id": "buyer-simple-lock-1",
        }

        with mock.patch("db_manager.db_manager", fake_db):
            await live._handle_simple_message_auto_delivery(
                websocket=mock.Mock(),
                order_id="order-simple-lock-1",
                item_id="item-simple-lock-1",
                user_id="buyer-simple-lock-1",
                chat_id="chat-simple-lock-1",
                msg_time="2026-05-11 15:20:00",
                msg_id="msg-simple-lock-1",
            )

        expected_lock_key = ("acc-simple-lock-1", "order-simple-lock-1")
        self.assertEqual(
            [mock.call(expected_lock_key), mock.call(expected_lock_key)],
            live.is_lock_held.call_args_list,
        )
        self.assertIn(expected_lock_key, live._lock_usage_times)
        self.assertNotIn("order-simple-lock-1", live._lock_usage_times)
        live._activate_delivery_lock.assert_called_once_with(expected_lock_key, delay_minutes=10)
        fake_db.get_order_by_id.assert_called_once_with(
            "order-simple-lock-1",
            account_id="acc-simple-lock-1",
        )

    async def test_handle_simple_message_auto_delivery_rejects_missing_scoped_order_before_lock_and_send(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-simple-missing-scope"
        live.account_id = "acc-simple-missing-scope-1"
        live._safe_str = str
        live._order_locks = {
            ("acc-simple-missing-scope-1", "order-simple-missing-scope-1"): asyncio.Lock(),
        }
        live._lock_usage_times = {}
        live._ensure_item_owned_by_current_account = mock.AsyncMock(return_value=True)
        live.can_auto_delivery = mock.Mock(return_value=True)
        live.is_lock_held = mock.Mock(return_value=False)
        live._get_pending_delivery_finalization_meta = mock.Mock(return_value=None)
        live._auto_delivery = mock.AsyncMock(
            return_value={
                "content": None,
                "error": "should not reach simple auto delivery without scoped order",
                "delivery_steps": [],
            }
        )
        live._persist_delivery_finalization_state = mock.Mock()
        live._sync_order_delivery_progress = mock.Mock(return_value={"aggregate_status": "shipped"})
        live._activate_delivery_lock = mock.Mock()
        live._record_delivery_log = mock.Mock()
        live.send_delivery_failure_notification = mock.AsyncMock()
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = None

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live._handle_simple_message_auto_delivery(
                websocket=mock.Mock(),
                order_id="order-simple-missing-scope-1",
                item_id="item-simple-missing-scope-1",
                user_id="buyer-simple-missing-scope-1",
                chat_id="chat-simple-missing-scope-1",
                msg_time="2026-05-12 02:20:00",
                msg_id="msg-simple-missing-scope-1",
            )

        fake_db.get_order_by_id.assert_called_once_with(
            "order-simple-missing-scope-1",
            account_id="acc-simple-missing-scope-1",
        )
        live._ensure_item_owned_by_current_account.assert_not_awaited()
        live.can_auto_delivery.assert_not_called()
        live.is_lock_held.assert_not_called()
        live._get_pending_delivery_finalization_meta.assert_not_called()
        live._auto_delivery.assert_not_awaited()
        live._record_delivery_log.assert_called_once()
        self.assertEqual("failed", live._record_delivery_log.call_args.kwargs["status"])
        self.assertIn("未验证归属", live._record_delivery_log.call_args.kwargs["reason"])
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("未验证归属", messages)

    async def test_handle_simple_message_auto_delivery_rejects_blank_canonical_account_id_before_scope_lookup(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-simple-progress-blank"
        live.account_id = "   "
        live._safe_str = str
        live._ensure_item_owned_by_current_account = mock.AsyncMock(
            side_effect=AssertionError("missing canonical account_id should skip item ownership lookup")
        )
        live.can_auto_delivery = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip delivery cooldown lookup")
        )
        live.is_lock_held = mock.Mock(
            side_effect=AssertionError("missing canonical account_id should skip lock lookup")
        )
        live._record_delivery_log = mock.Mock()
        mock_logger = mock.Mock()

        with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live._handle_simple_message_auto_delivery(
                websocket=mock.Mock(),
                order_id="order-simple-blank",
                item_id="item-simple-blank",
                user_id="buyer-simple-blank",
                chat_id="chat-simple-blank",
                msg_time="2026-05-11 12:30:00",
                msg_id="msg-simple-blank",
            )

        live._ensure_item_owned_by_current_account.assert_not_awaited()
        live.can_auto_delivery.assert_not_called()
        live.is_lock_held.assert_not_called()
        live._record_delivery_log.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("缺少 canonical account_id", messages)

    async def test_yifan_account_confirmation_activates_account_scoped_delivery_lock(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-yifan-confirm-lock"
        live.account_id = "acc-yifan-confirm-lock-1"
        live._safe_str = str
        live.myid = "seller-self"
        live.ws = mock.Mock()
        live.yifan_account_lock = asyncio.Lock()
        live.yifan_account_waiting = {
            "chat-yifan-confirm-lock-1": {
                "buyer_id": "buyer-yifan-confirm-lock-1",
                "rule": {
                    "id": 77,
                    "keyword": "yifan-keyword",
                    "card_type": "yifan",
                    "card_description": "Yifan Card",
                },
                "order_id": "order-yifan-confirm-lock-1",
                "item_id": "item-yifan-confirm-lock-1",
                "state": "waiting_confirm",
                "account": "13800138000",
                "create_time": time.time(),
                "retry_count": 0,
            }
        }
        live.order_status_handler = mock.Mock()
        live.order_status_handler.handle_system_message.return_value = False
        live._extract_order_id = mock.Mock(return_value=None)
        live.extract_item_id_from_message = mock.Mock(return_value="item-yifan-confirm-lock-fallback")
        live._extract_message_id_from_chat_payload = mock.Mock(return_value="dedupe-msg-yifan-confirm-lock-1")
        live._classify_message_route = mock.Mock(
            return_value={
                "route": "user_chat",
                "order_status_signal": None,
                "should_notify": False,
                "allow_auto_reply": True,
                "is_system_message": False,
                "is_group_message": False,
                "message_direction": 2,
                "content_type": 1,
            }
        )
        live._call_yifan_api_with_account = mock.AsyncMock(return_value="card-line-1")
        live._build_delivery_steps = mock.Mock(return_value=["card-line-1"])
        live._send_delivery_steps = mock.AsyncMock()
        live._finalize_delivery_after_send = mock.AsyncMock(return_value={"success": True})
        live.mark_delivery_sent = mock.Mock()
        live._activate_delivery_lock = mock.Mock()
        live._record_delivery_log = mock.Mock()
        live.send_msg = mock.AsyncMock()

        message = {
            "1": {
                "2": "chat-yifan-confirm-lock-1@goofish",
                "5": 1710000000000,
                "7": 2,
                "10": {
                    "senderNick": "Buyer Yifan",
                    "senderUserId": "buyer-yifan-confirm-lock-1",
                    "reminderContent": "是",
                },
                "6": {
                    "3": {
                        "4": 1,
                    }
                },
            }
        }
        websocket = mock.Mock()
        websocket.send = mock.AsyncMock()
        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}):
            await live.handle_message(
                self._build_sync_package(message),
                websocket,
                msg_id="msg-yifan-confirm-lock-1",
            )

        live.mark_delivery_sent.assert_called_once_with(
            "order-yifan-confirm-lock-1",
            context="亦凡账号确认发货发送成功",
        )
        live._activate_delivery_lock.assert_called_once_with(
            ("acc-yifan-confirm-lock-1", "order-yifan-confirm-lock-1"),
            delay_minutes=10,
        )

    async def test_cleanup_expired_locks_prunes_scoped_delivery_lock_state(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cleanup-lock"
        live.account_id = "acc-cleanup-lock-1"
        live._safe_str = str

        expired_scope = ("acc-cleanup-lock-1", "order-cleanup-expired-1")
        active_scope = ("acc-foreign-lock-1", "order-cleanup-active-1")
        expired_task = mock.Mock()
        active_task = mock.Mock()

        live._order_locks = {
            expired_scope: asyncio.Lock(),
            active_scope: asyncio.Lock(),
        }
        live._lock_usage_times = {
            expired_scope: 1000.0,
            active_scope: 150000.0,
        }
        live._lock_hold_info = {
            expired_scope: {"locked": True, "task": expired_task},
            active_scope: {"locked": True, "task": active_task},
        }
        live._order_detail_lock_times = {}
        live._order_detail_locks = {}

        with mock.patch("XianyuAutoAsync.time.time", return_value=200000.0):
            live.cleanup_expired_locks(max_age_hours=24)

        self.assertNotIn(expired_scope, live._order_locks)
        self.assertNotIn(expired_scope, live._lock_usage_times)
        self.assertNotIn(expired_scope, live._lock_hold_info)
        expired_task.cancel.assert_called_once_with()

        self.assertIn(active_scope, live._order_locks)
        self.assertIn(active_scope, live._lock_usage_times)
        self.assertIn(active_scope, live._lock_hold_info)
        active_task.cancel.assert_not_called()

    async def test_background_loops_prefer_account_id_alias_for_cookie_status_gate(self):
        loop_cases = (
            "message_stream_watchdog_loop",
            "token_refresh_loop",
            "heartbeat_loop",
            "pause_cleanup_loop",
            "cookie_refresh_loop",
        )

        for loop_name in loop_cases:
            with self.subTest(loop_name=loop_name):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-cookie-name"
                live.account_id = "acc-loop-gate-1"
                live._safe_str = str

                if loop_name == "message_stream_watchdog_loop":
                    live.heartbeat_timeout = 30
                    live.heartbeat_interval = 10

                cookie_manager_module = self._build_cookie_manager_module(enabled=False)

                with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}):
                    if loop_name == "heartbeat_loop":
                        await live.heartbeat_loop(types.SimpleNamespace(closed=False))
                    else:
                        await getattr(live, loop_name)()

                cookie_manager_module.manager.get_cookie_status.assert_called_once_with(
                    "acc-loop-gate-1"
                )

    async def test_pause_cleanup_loop_skips_risk_log_cleanup_without_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-risk-cleanup-blank"
        live.account_id = "   "
        live._safe_str = str
        live._is_current_account_enabled = mock.Mock(return_value=True)
        live.cleanup_expired_locks = mock.Mock()
        live._cleanup_item_cache = mock.AsyncMock(return_value=0)
        live._cleanup_instance_caches = mock.Mock()
        live._cleanup_playwright_cache = mock.AsyncMock(return_value=None)
        live._cleanup_old_logs = mock.AsyncMock(return_value=0)
        live._interruptible_sleep = mock.AsyncMock(side_effect=asyncio.CancelledError())

        qr_login_manager = types.SimpleNamespace(cleanup_expired_sessions=mock.Mock())

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with mock.patch.object(XianyuAutoAsync.pause_manager, "cleanup_expired_pauses"), \
             mock.patch.dict(sys.modules, {"utils.qr_login": types.SimpleNamespace(qr_login_manager=qr_login_manager)}), \
             mock.patch("XianyuAutoAsync.time.time", return_value=1000.0), \
             mock.patch("XianyuAutoAsync.asyncio.to_thread", new=fake_to_thread), \
             mock.patch.object(XianyuLive, "_last_risk_log_cleanup_times", {}, create=True), \
             mock.patch.object(XianyuLive, "_last_db_cleanup_time", 1000.0, create=True), \
             mock.patch.object(
                 XianyuAutoAsync.db_manager,
                 "mark_stale_risk_control_logs_failed",
                 side_effect=AssertionError("should not clean global risk logs without canonical account_id"),
             ) as mark_stale_risk_control_logs_failed:
            with self.assertRaises(asyncio.CancelledError):
                await live.pause_cleanup_loop()

        mark_stale_risk_control_logs_failed.assert_not_called()

    async def test_pause_cleanup_loop_scopes_risk_log_cleanup_by_canonical_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-risk-cleanup"
        live.account_id = "acc-risk-cleanup-1"
        live._safe_str = str
        live._is_current_account_enabled = mock.Mock(return_value=True)
        live.cleanup_expired_locks = mock.Mock()
        live._cleanup_item_cache = mock.AsyncMock(return_value=0)
        live._cleanup_instance_caches = mock.Mock()
        live._cleanup_playwright_cache = mock.AsyncMock(return_value=None)
        live._cleanup_old_logs = mock.AsyncMock(return_value=0)
        live._interruptible_sleep = mock.AsyncMock(side_effect=asyncio.CancelledError())

        qr_login_manager = types.SimpleNamespace(cleanup_expired_sessions=mock.Mock())

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with mock.patch.object(XianyuAutoAsync.pause_manager, "cleanup_expired_pauses"), \
             mock.patch.dict(sys.modules, {"utils.qr_login": types.SimpleNamespace(qr_login_manager=qr_login_manager)}), \
             mock.patch("XianyuAutoAsync.time.time", return_value=1000.0), \
             mock.patch("XianyuAutoAsync.asyncio.to_thread", new=fake_to_thread), \
             mock.patch.object(XianyuLive, "_last_risk_log_cleanup_times", {}, create=True), \
             mock.patch.object(XianyuLive, "_last_db_cleanup_time", 1000.0, create=True), \
             mock.patch.object(
                 XianyuAutoAsync.db_manager,
                 "mark_stale_risk_control_logs_failed",
                 return_value=3,
             ) as mark_stale_risk_control_logs_failed:
            with self.assertRaises(asyncio.CancelledError):
                await live.pause_cleanup_loop()

        mark_stale_risk_control_logs_failed.assert_called_once_with(
            timeout_minutes=15,
            account_id="acc-risk-cleanup-1",
        )

    async def test_pause_cleanup_loop_uses_per_account_risk_cleanup_throttle(self):
        live_a = XianyuLive.__new__(XianyuLive)
        live_a._legacy_cookie_id = "legacy-risk-cleanup-a"
        live_a.account_id = "acc-risk-cleanup-a"
        live_a._safe_str = str
        live_a._is_current_account_enabled = mock.Mock(return_value=True)
        live_a.cleanup_expired_locks = mock.Mock()
        live_a._cleanup_item_cache = mock.AsyncMock(return_value=0)
        live_a._cleanup_instance_caches = mock.Mock()
        live_a._cleanup_playwright_cache = mock.AsyncMock(return_value=None)
        live_a._cleanup_old_logs = mock.AsyncMock(return_value=0)
        live_a._interruptible_sleep = mock.AsyncMock(side_effect=asyncio.CancelledError())

        live_b = XianyuLive.__new__(XianyuLive)
        live_b._legacy_cookie_id = "legacy-risk-cleanup-b"
        live_b.account_id = "acc-risk-cleanup-b"
        live_b._safe_str = str
        live_b._is_current_account_enabled = mock.Mock(return_value=True)
        live_b.cleanup_expired_locks = mock.Mock()
        live_b._cleanup_item_cache = mock.AsyncMock(return_value=0)
        live_b._cleanup_instance_caches = mock.Mock()
        live_b._cleanup_playwright_cache = mock.AsyncMock(return_value=None)
        live_b._cleanup_old_logs = mock.AsyncMock(return_value=0)
        live_b._interruptible_sleep = mock.AsyncMock(side_effect=asyncio.CancelledError())

        qr_login_manager = types.SimpleNamespace(cleanup_expired_sessions=mock.Mock())

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with mock.patch.object(XianyuAutoAsync.pause_manager, "cleanup_expired_pauses"), \
             mock.patch.dict(sys.modules, {"utils.qr_login": types.SimpleNamespace(qr_login_manager=qr_login_manager)}), \
             mock.patch("XianyuAutoAsync.asyncio.to_thread", new=fake_to_thread), \
             mock.patch.object(XianyuLive, "_last_risk_log_cleanup_times", {}, create=True), \
             mock.patch.object(XianyuLive, "_last_db_cleanup_time", 1000.0, create=True), \
             mock.patch.object(
                 XianyuAutoAsync.db_manager,
                 "mark_stale_risk_control_logs_failed",
                 return_value=1,
             ) as mark_stale_risk_control_logs_failed:
            with mock.patch("XianyuAutoAsync.time.time", return_value=1000.0):
                with self.assertRaises(asyncio.CancelledError):
                    await live_a.pause_cleanup_loop()

            with mock.patch("XianyuAutoAsync.time.time", return_value=1001.0):
                with self.assertRaises(asyncio.CancelledError):
                    await live_b.pause_cleanup_loop()

        self.assertEqual(
            [
                mock.call(timeout_minutes=15, account_id="acc-risk-cleanup-a"),
                mock.call(timeout_minutes=15, account_id="acc-risk-cleanup-b"),
            ],
            mark_stale_risk_control_logs_failed.call_args_list,
        )

    async def test_pause_cleanup_loop_deduplicates_parallel_risk_cleanup_for_same_account(self):
        def _build_live():
            live = XianyuLive.__new__(XianyuLive)
            live._legacy_cookie_id = "legacy-risk-cleanup-shared"
            live.account_id = "acc-risk-cleanup-shared"
            live._safe_str = str
            live._is_current_account_enabled = mock.Mock(return_value=True)
            live.cleanup_expired_locks = mock.Mock()
            live._cleanup_item_cache = mock.AsyncMock(return_value=0)
            live._cleanup_instance_caches = mock.Mock()
            live._cleanup_playwright_cache = mock.AsyncMock(return_value=None)
            live._cleanup_old_logs = mock.AsyncMock(return_value=0)
            live._interruptible_sleep = mock.AsyncMock(side_effect=asyncio.CancelledError())
            return live

        live_a = _build_live()
        live_b = _build_live()
        qr_login_manager = types.SimpleNamespace(cleanup_expired_sessions=mock.Mock())
        first_cleanup_started = asyncio.Event()
        allow_first_cleanup_finish = asyncio.Event()
        second_cleanup_started = asyncio.Event()
        cleanup_call_count = 0

        async def fake_to_thread(fn, *args, **kwargs):
            nonlocal cleanup_call_count
            cleanup_call_count += 1
            if cleanup_call_count == 1:
                first_cleanup_started.set()
                await allow_first_cleanup_finish.wait()
            else:
                second_cleanup_started.set()
            return fn(*args, **kwargs)

        with mock.patch.object(XianyuAutoAsync.pause_manager, "cleanup_expired_pauses"), \
             mock.patch.dict(sys.modules, {"utils.qr_login": types.SimpleNamespace(qr_login_manager=qr_login_manager)}), \
             mock.patch("XianyuAutoAsync.time.time", return_value=1000.0), \
             mock.patch("XianyuAutoAsync.asyncio.to_thread", new=fake_to_thread), \
             mock.patch.object(XianyuLive, "_last_risk_log_cleanup_times", {}, create=True), \
             mock.patch.object(XianyuLive, "_last_db_cleanup_time", 1000.0, create=True), \
             mock.patch.object(
                 XianyuAutoAsync.db_manager,
                 "mark_stale_risk_control_logs_failed",
                 return_value=1,
             ) as mark_stale_risk_control_logs_failed:
            task_a = asyncio.create_task(live_a.pause_cleanup_loop())
            await first_cleanup_started.wait()

            task_b = asyncio.create_task(live_b.pause_cleanup_loop())
            for _ in range(5):
                await asyncio.sleep(0)

            self.assertFalse(
                second_cleanup_started.is_set(),
                "same-account risk cleanup should not race into a second concurrent cleanup call",
            )

            allow_first_cleanup_finish.set()

            with self.assertRaises(asyncio.CancelledError):
                await task_a
            with self.assertRaises(asyncio.CancelledError):
                await task_b

        mark_stale_risk_control_logs_failed.assert_called_once_with(
            timeout_minutes=15,
            account_id="acc-risk-cleanup-shared",
        )

    async def test_cookie_refresh_loop_manual_refresh_gate_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-cookie-refresh-loop-1"
        live._safe_str = str
        live.cookie_refresh_enabled = True
        live.last_cookie_refresh_time = 0
        live.cookie_refresh_interval = 3600
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live.cookie_refresh_lock = asyncio.Lock()
        live.is_manual_refresh_active = mock.Mock(return_value=True)
        live._interruptible_sleep = mock.AsyncMock(side_effect=asyncio.CancelledError())

        cookie_manager_module = self._build_cookie_manager_module(enabled=True)

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}):
            with self.assertRaises(asyncio.CancelledError):
                await live.cookie_refresh_loop()

        live.is_manual_refresh_active.assert_called_once_with("acc-cookie-refresh-loop-1")

    async def test_cookie_refresh_loop_rejects_blank_canonical_account_id_before_manual_gate(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "   "
        live._safe_str = str
        live.cookie_refresh_enabled = True
        live.last_cookie_refresh_time = 0
        live.cookie_refresh_interval = 3600
        live.last_message_received_time = 0
        live.message_cookie_refresh_cooldown = 0
        live.cookie_refresh_lock = asyncio.Lock()
        live.is_manual_refresh_active = mock.Mock(return_value=False)
        live._is_current_account_enabled = mock.Mock(return_value=True)
        async def stop_loop_after_first_tick(_seconds):
            live._is_current_account_enabled.return_value = False

        live._interruptible_sleep = mock.AsyncMock(side_effect=stop_loop_after_first_tick)

        def close_scheduled_refresh(coro):
            coro.close()
            return mock.Mock()

        with mock.patch("XianyuAutoAsync.asyncio.create_task", side_effect=close_scheduled_refresh) as create_task:
            await live.cookie_refresh_loop()

        live._is_current_account_enabled.assert_not_called()
        live.is_manual_refresh_active.assert_not_called()
        create_task.assert_not_called()

    async def test_execute_cookie_refresh_manual_refresh_gate_prefers_account_id_alias(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "acc-cookie-refresh-exec-1"
        live._safe_str = str
        live.cookie_refresh_lock = asyncio.Lock()
        live.is_manual_refresh_active = mock.Mock(return_value=True)
        live._refresh_cookies_via_browser = mock.AsyncMock()
        live.ws = None
        live.heartbeat_task = None

        await live._execute_cookie_refresh(current_time=123.0)

        live.is_manual_refresh_active.assert_called_once_with("acc-cookie-refresh-exec-1")
        live._refresh_cookies_via_browser.assert_not_awaited()

    async def test_execute_cookie_refresh_rejects_blank_canonical_account_id_before_manual_gate(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-name"
        live.account_id = "   "
        live._safe_str = str
        live.cookie_refresh_lock = asyncio.Lock()
        live.is_manual_refresh_active = mock.Mock(return_value=False)
        live._refresh_cookies_via_browser = mock.AsyncMock(return_value=True)
        live.ws = mock.Mock()
        live.ws.closed = False
        live.heartbeat_task = None
        live.last_message_received_time = 1
        live.last_cookie_refresh_time = 0

        def close_unexpected_task(coro):
            coro.close()
            return mock.Mock()

        with mock.patch("XianyuAutoAsync.asyncio.create_task", side_effect=close_unexpected_task) as create_task:
            await live._execute_cookie_refresh(current_time=123.0)

        live.is_manual_refresh_active.assert_not_called()
        live._refresh_cookies_via_browser.assert_not_awaited()
        create_task.assert_not_called()
        self.assertEqual(0, live.last_cookie_refresh_time)
        self.assertEqual(1, live.last_message_received_time)

    def test_sync_order_delivery_progress_forwards_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "acc-delivery-live-3"
        live._legacy_cookie_id = "acc-delivery-live-3"
        live.delivery_sent_orders = set()
        live.last_delivery_time = {}
        live._summarize_delivery_progress = mock.Mock(
            return_value={
                "order_id": "order-live-3",
                "expected_quantity": 1,
                "finalized_count": 1,
                "pending_finalize_count": 0,
                "remaining_count": 0,
                "aggregate_status": "shipped",
                "states": [],
            }
        )
        live._resolve_delivery_progress_order_status = mock.Mock(return_value="shipped")
        live.order_status_handler = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {"order_status": "pending_ship"}
        fake_db._normalize_order_status.side_effect = lambda value: value
        fake_db.insert_or_update_order.return_value = True

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("XianyuAutoAsync.time.time", return_value=321.0):
            summary = live._sync_order_delivery_progress(
                order_id="order-live-3",
                account_id="acc-delivery-live-3",
                expected_quantity=1,
                context="unit-test",
            )

        self.assertEqual(summary["aggregate_status"], "shipped")
        live.order_status_handler.handle_auto_delivery_order_status.assert_called_once_with(
            order_id="order-live-3",
            account_id="acc-delivery-live-3",
            context="unit-test",
        )
        fake_db.insert_or_update_order.assert_called_once_with(
            order_id="order-live-3",
            order_status="shipped",
            account_id="acc-delivery-live-3",
        )
        self.assertEqual(
            {("acc-delivery-live-3", "order-live-3")},
            live.delivery_sent_orders,
        )
        self.assertEqual(
            {("acc-delivery-live-3", "order-live-3"): 321.0},
            live.last_delivery_time,
        )

    def test_sync_order_delivery_progress_normalizes_account_id_before_downstream_side_effects(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "acc-delivery-live-5"
        live._legacy_cookie_id = "acc-delivery-live-5"
        live.delivery_sent_orders = set()
        live.last_delivery_time = {}
        live._summarize_delivery_progress = mock.Mock(
            return_value={
                "order_id": "order-live-5",
                "expected_quantity": 1,
                "finalized_count": 1,
                "pending_finalize_count": 0,
                "remaining_count": 0,
                "aggregate_status": "shipped",
                "states": [],
            }
        )
        live._resolve_delivery_progress_order_status = mock.Mock(return_value="shipped")
        live.order_status_handler = mock.Mock()
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {"order_status": "pending_ship"}
        fake_db._normalize_order_status.side_effect = lambda value: value
        fake_db.insert_or_update_order.return_value = True

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("XianyuAutoAsync.time.time", return_value=654.0):
            summary = live._sync_order_delivery_progress(
                order_id="order-live-5",
                account_id=" acc-delivery-live-5 ",
                expected_quantity=1,
                context="normalized-unit-test",
            )

        self.assertEqual(summary["aggregate_status"], "shipped")
        fake_db.get_order_by_id.assert_called_once_with(
            "order-live-5",
            account_id="acc-delivery-live-5",
        )
        live.order_status_handler.handle_auto_delivery_order_status.assert_called_once_with(
            order_id="order-live-5",
            account_id="acc-delivery-live-5",
            context="normalized-unit-test",
        )
        fake_db.insert_or_update_order.assert_called_once_with(
            order_id="order-live-5",
            order_status="shipped",
            account_id="acc-delivery-live-5",
        )

    def test_sync_order_delivery_progress_normalizes_order_id_before_side_effects(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "acc-delivery-live-7"
        live._legacy_cookie_id = "acc-delivery-live-7"
        live.delivery_sent_orders = set()
        live.last_delivery_time = {}
        live._summarize_delivery_progress = mock.Mock(
            return_value={
                "order_id": "order-live-7",
                "expected_quantity": 1,
                "finalized_count": 1,
                "pending_finalize_count": 0,
                "remaining_count": 0,
                "aggregate_status": "shipped",
                "states": [],
            }
        )
        live._resolve_delivery_progress_order_status = mock.Mock(return_value="shipped")
        live.order_status_handler = mock.Mock()
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {"order_status": "pending_ship"}
        fake_db._normalize_order_status.side_effect = lambda value: value
        fake_db.insert_or_update_order.return_value = True

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("XianyuAutoAsync.time.time", return_value=777.0):
            summary = live._sync_order_delivery_progress(
                order_id=" order-live-7 ",
                account_id="acc-delivery-live-7",
                expected_quantity=1,
                context="normalized-order-id-unit-test",
            )

        self.assertEqual(summary["aggregate_status"], "shipped")
        live._summarize_delivery_progress.assert_called_once_with(
            "order-live-7",
            expected_quantity=1,
        )
        fake_db.get_order_by_id.assert_called_once_with(
            "order-live-7",
            account_id="acc-delivery-live-7",
        )
        live.order_status_handler.handle_auto_delivery_order_status.assert_called_once_with(
            order_id="order-live-7",
            account_id="acc-delivery-live-7",
            context="normalized-order-id-unit-test",
        )
        fake_db.insert_or_update_order.assert_called_once_with(
            order_id="order-live-7",
            order_status="shipped",
            account_id="acc-delivery-live-7",
        )
        self.assertEqual(
            {("acc-delivery-live-7", "order-live-7")},
            live.delivery_sent_orders,
        )
        self.assertEqual(
            {("acc-delivery-live-7", "order-live-7"): 777.0},
            live.last_delivery_time,
        )

    def test_sync_order_delivery_progress_rejects_blank_order_id_after_normalization(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "acc-delivery-live-8"
        live._legacy_cookie_id = "acc-delivery-live-8"
        live.delivery_sent_orders = set()
        live.last_delivery_time = {}
        live._summarize_delivery_progress = mock.Mock(
            return_value={
                "order_id": "",
                "expected_quantity": 2,
                "finalized_count": 0,
                "pending_finalize_count": 0,
                "remaining_count": 2,
                "aggregate_status": "pending_ship",
                "states": [],
            }
        )
        live._resolve_delivery_progress_order_status = mock.Mock(return_value="pending_ship")
        live.order_status_handler = mock.Mock()
        live._safe_str = str
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.side_effect = AssertionError(
            "blank order_id should fail closed before scoped db reads"
        )
        fake_db.insert_or_update_order.side_effect = AssertionError(
            "blank order_id should fail closed before scoped db writes"
        )

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            summary = live._sync_order_delivery_progress(
                order_id="   ",
                account_id="acc-delivery-live-8",
                expected_quantity=2,
                context="blank-order-id-unit-test",
            )

        self.assertEqual("pending_ship", summary["aggregate_status"])
        live._summarize_delivery_progress.assert_called_once_with(
            "",
            expected_quantity=2,
        )
        fake_db.get_order_by_id.assert_not_called()
        fake_db.insert_or_update_order.assert_not_called()
        live.order_status_handler.handle_auto_delivery_order_status.assert_not_called()
        self.assertEqual(set(), live.delivery_sent_orders)
        self.assertEqual({}, live.last_delivery_time)
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("order_id", messages)

    def test_sync_order_delivery_progress_rejects_mismatched_account_scope_before_side_effects(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "acc-delivery-live-6"
        live._legacy_cookie_id = "acc-delivery-live-6"
        live.delivery_sent_orders = set()
        live.last_delivery_time = {}
        live._summarize_delivery_progress = mock.Mock(
            return_value={
                "order_id": "order-live-6",
                "expected_quantity": 1,
                "finalized_count": 0,
                "pending_finalize_count": 0,
                "remaining_count": 1,
                "aggregate_status": "pending_ship",
                "states": [],
            }
        )
        live._resolve_delivery_progress_order_status = mock.Mock(return_value="pending_ship")
        live.order_status_handler = mock.Mock()
        live._safe_str = str
        mock_logger = mock.Mock()

        fake_db = mock.Mock()
        fake_db.get_order_by_id.side_effect = AssertionError(
            "mismatched account scope should fail closed before db reads"
        )
        fake_db.insert_or_update_order.side_effect = AssertionError(
            "mismatched account scope should fail closed before db writes"
        )

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            summary = live._sync_order_delivery_progress(
                order_id="order-live-6",
                account_id="acc-delivery-live-foreign",
                expected_quantity=1,
                context="mismatch-unit-test",
            )

        self.assertEqual("pending_ship", summary["aggregate_status"])
        fake_db.get_order_by_id.assert_not_called()
        fake_db.insert_or_update_order.assert_not_called()
        live.order_status_handler.handle_auto_delivery_order_status.assert_not_called()
        self.assertEqual(set(), live.delivery_sent_orders)
        self.assertEqual({}, live.last_delivery_time)
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("canonical account_id", messages)

    def test_sync_order_delivery_progress_publishes_partial_status_with_account_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "acc-delivery-live-4"
        live._legacy_cookie_id = "acc-delivery-live-4"
        live.delivery_sent_orders = set()
        live.last_delivery_time = {}
        live._summarize_delivery_progress = mock.Mock(
            return_value={
                "order_id": "order-live-4",
                "expected_quantity": 2,
                "finalized_count": 1,
                "pending_finalize_count": 1,
                "remaining_count": 0,
                "aggregate_status": "partial_pending_finalize",
                "states": [],
            }
        )
        live._resolve_delivery_progress_order_status = mock.Mock(return_value="partial_pending_finalize")
        live.order_status_handler = mock.Mock()
        live._safe_str = str

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {"order_status": "pending_ship"}
        fake_db._normalize_order_status.side_effect = lambda value: value
        fake_db.insert_or_update_order.return_value = True

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("order_event_hub.publish_order_update_event") as publish_event, \
             mock.patch("XianyuAutoAsync.time.time", return_value=456.0):
            summary = live._sync_order_delivery_progress(
                order_id="order-live-4",
                account_id="acc-delivery-live-4",
                expected_quantity=2,
                context="partial-unit-test",
            )

        self.assertEqual(summary["aggregate_status"], "partial_pending_finalize")
        self.assertEqual(
            {("acc-delivery-live-4", "order-live-4")},
            live.delivery_sent_orders,
        )
        self.assertEqual(
            {("acc-delivery-live-4", "order-live-4"): 456.0},
            live.last_delivery_time,
        )
        live.order_status_handler.handle_auto_delivery_order_status.assert_not_called()
        fake_db.insert_or_update_order.assert_called_once_with(
            order_id="order-live-4",
            order_status="partial_pending_finalize",
            account_id="acc-delivery-live-4",
        )
        publish_event.assert_called_once_with(
            "order-live-4",
            account_id="acc-delivery-live-4",
            source="delivery_progress_sync",
        )

    async def test_async_close_browser_closes_external_browser_only_once_when_context_is_not_owned(self):
        close_order = []

        async def close_browser():
            close_order.append("browser.close")

        browser = mock.Mock()
        browser.close = mock.AsyncMock(side_effect=close_browser)

        context = mock.Mock()
        context.close = mock.AsyncMock(side_effect=AssertionError("context should stay open"))

        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "managed-runtime-browser-close-once-test"
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
        live.account_id = "force-close-order-test"
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

    async def test_open_browser_recovery_context_returns_failure_when_persistent_profile_launch_fails(self):
        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "persistent-launch-fail-test"
        live._legacy_cookie_id = "persistent-launch-fail-test"
        live._safe_str = str
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=False)

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=types.SimpleNamespace(
                acquire_runtime=mock.AsyncMock(side_effect=RuntimeError("persistent launch failed")),
                resolve_profile_dir=mock.Mock(return_value=os.path.join(os.getcwd(), "browser_data", "user_persistent-launch-fail-test")),
            ),
        ) as runtime_manager, \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("should not launch clean browser")),
                 create=True,
             ) as launch_browser_safe:
            lease, browser, context, reused_profile = await live._open_browser_recovery_context("浏览器恢复测试")

        self.assertIsNone(lease)
        self.assertIsNone(browser)
        self.assertIsNone(context)
        self.assertFalse(reused_profile)
        runtime_manager.acquire_runtime.assert_awaited_once()
        launch_browser_safe.assert_not_awaited()

    async def test_open_browser_recovery_context_releases_runtime_when_context_missing(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-context-missing"
        live.account_id = "account-context-missing"
        live._safe_str = str
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=False)

        lease = self._build_runtime_lease("account-context-missing", browser=None, context=None)
        lease.runtime.context = None
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(os.getcwd(), "browser_data", "user_account-context-missing")
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("should not launch clean browser")),
                 create=True,
             ) as launch_browser_safe:
            lease_result, browser, context, reused_profile = await live._open_browser_recovery_context(
                "浏览器恢复测试",
                target_account_id="account-context-missing",
            )

        self.assertIsNone(lease_result)
        self.assertIsNone(browser)
        self.assertIsNone(context)
        self.assertFalse(reused_profile)
        runtime_manager.acquire_runtime.assert_awaited_once_with(
            "account-context-missing",
            "verification_recovery",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="recovery_context_missing",
        )
        launch_browser_safe.assert_not_awaited()

    async def test_open_browser_recovery_context_rejects_legacy_target_cookie_id_contract(self):
        context = mock.Mock()
        context.browser = mock.Mock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-recovery-self"
        live.account_id = ""
        live._safe_str = str
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=False)

        lease = self._build_runtime_lease(
            "alias-recovery-account-1",
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(
                side_effect=AssertionError("should not acquire runtime without canonical account_id")
            ),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(
                    os.getcwd(),
                    "browser_data",
                    "user_alias-recovery-account-1",
                )
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(
                     side_effect=AssertionError("should not launch clean browser")
                 ),
                 create=True,
             ) as launch_browser_safe:
            with self.assertRaises(TypeError):
                await live._open_browser_recovery_context(
                    "浏览器恢复测试",
                    target_cookie_id="alias-recovery-account-1",
                )

        runtime_manager.acquire_runtime.assert_not_awaited()
        runtime_manager.resolve_profile_dir.assert_not_called()
        launch_browser_safe.assert_not_awaited()

    async def test_open_browser_recovery_context_rejects_default_canonical_account_id(self):
        context = mock.Mock()
        context.browser = mock.Mock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-recovery-self"
        live.account_id = "default"
        live._safe_str = str
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=False)

        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(
                side_effect=AssertionError("should not acquire runtime for default pseudo account")
            ),
            resolve_profile_dir=mock.Mock(
                side_effect=AssertionError("should not resolve profile dir for default pseudo account")
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(
                     side_effect=AssertionError("should not launch clean browser")
                 ),
                 create=True,
             ) as launch_browser_safe:
            lease_result, browser, context_result, reused_profile = (
                await live._open_browser_recovery_context(
                    "浏览器恢复测试",
                    target_account_id="default",
                )
            )

        self.assertIsNone(lease_result)
        self.assertIsNone(browser)
        self.assertIsNone(context_result)
        self.assertFalse(reused_profile)
        runtime_manager.acquire_runtime.assert_not_awaited()
        runtime_manager.resolve_profile_dir.assert_not_called()
        launch_browser_safe.assert_not_awaited()

    async def test_open_browser_recovery_context_rejects_foreign_target_scope_when_canonical_account_id_present(self):
        context = mock.Mock()
        context.browser = mock.Mock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-recovery-self"
        live.account_id = "current-recovery-account"
        live._safe_str = str
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=False)

        lease = self._build_runtime_lease(
            "alias-recovery-account-2",
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(
                side_effect=AssertionError("should not acquire runtime for foreign account scope")
            ),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(
                    os.getcwd(),
                    "browser_data",
                    "user_alias-recovery-account-2",
                )
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(
                     side_effect=AssertionError("should not launch clean browser")
                 ),
                 create=True,
             ) as launch_browser_safe:
            lease_result, browser, context_result, reused_profile = (
                await live._open_browser_recovery_context(
                    "浏览器恢复测试",
                    target_account_id="foreign-recovery-account",
                )
            )

        self.assertIsNone(lease_result)
        self.assertIsNone(browser)
        self.assertIsNone(context_result)
        self.assertFalse(reused_profile)
        runtime_manager.acquire_runtime.assert_not_awaited()
        runtime_manager.resolve_profile_dir.assert_not_called()
        launch_browser_safe.assert_not_awaited()

    async def test_open_browser_recovery_context_rejects_foreign_returned_runtime_lease_account_id(self):
        context = mock.Mock()
        context.browser = mock.Mock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-recovery-self"
        live.account_id = "current-recovery-account"
        live._safe_str = str
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=False)

        lease = self._build_runtime_lease(
            "foreign-recovery-account",
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(
                    os.getcwd(),
                    "browser_data",
                    "user_current-recovery-account",
                )
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(
                     side_effect=AssertionError("should not launch clean browser")
                 ),
                 create=True,
             ) as launch_browser_safe:
            lease_result, browser, context_result, reused_profile = (
                await live._open_browser_recovery_context(
                    "recovery-test",
                    target_account_id="current-recovery-account",
                )
            )

        self.assertIsNone(lease_result)
        self.assertIsNone(browser)
        self.assertIsNone(context_result)
        self.assertFalse(reused_profile)
        runtime_manager.acquire_runtime.assert_awaited_once()
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="recovery_context_account_mismatch",
        )
        launch_browser_safe.assert_not_awaited()

    async def test_refresh_cookies_from_qr_login_uses_account_persistent_profile_without_clean_browser_fallback(self):
        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()
        page.close = mock.AsyncMock()

        context = mock.Mock()
        context.browser = mock.Mock()
        context.browser.close = mock.AsyncMock()
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
        live.account_id = "fresh-context-test"
        live._legacy_cookie_id = "account-fresh-context-test"
        live.user_id = 7
        live.qr_cookie_refresh_cooldown = 180
        live.last_qr_cookie_refresh_time = 0
        live._safe_str = str
        live._extract_cookie_value = lambda cookie_record: (cookie_record or {}).get("cookie")
        live._mask_secret_value = lambda value, head=8, tail=6: value
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._set_runtime_cookie_state = mock.Mock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)

        qr_cookies_str = (
            "unb=qr-unb; sgcookie=qr-sg; cookie2=qr-cookie2; "
            "_m_h5_tk=qr-token_123; _m_h5_tk_enc=qr-enc; t=qr-t"
        )
        lease = self._build_runtime_lease(live.account_id, browser=context.browser, context=context)
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(os.getcwd(), "browser_data", "user_account-fresh-context-test")
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("should not launch clean browser")),
                 create=True,
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
        runtime_manager.acquire_runtime.assert_awaited_once_with(
            "fresh-context-test",
            "verification_recovery",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_request = runtime_manager.acquire_runtime.await_args.kwargs["runtime_request"]
        self.assertEqual(
            runtime_request["profile_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_account-fresh-context-test"),
        )
        self.assertTrue(runtime_request["use_persistent_context"])
        launch_args = runtime_request["launch_options"]["args"]
        self.assertNotIn("--enable-automation", launch_args)
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="qr_cookie_refresh_completed",
        )
        launch_browser_safe.assert_not_awaited()

    async def test_refresh_cookies_from_qr_login_rejects_missing_required_fields_without_persisting(self):
        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()
        page.wait_for_load_state = mock.AsyncMock()
        page.close = mock.AsyncMock()

        context = mock.Mock()
        context.browser = mock.Mock()
        context.browser.close = mock.AsyncMock()
        context.add_cookies = mock.AsyncMock()
        context.cookies = mock.AsyncMock(
            return_value=[
                {"name": "unb", "value": "new-unb"},
                {"name": "sgcookie", "value": "new-sg"},
                {"name": "_m_h5_tk", "value": "new-token_123"},
                {"name": "_m_h5_tk_enc", "value": "new-enc"},
                {"name": "t", "value": "new-t"},
                {"name": "cna", "value": "new-cna"},
            ]
        )
        context.close = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-qr-missing-required"
        live.account_id = "account-qr-missing-required"
        live.user_id = 7
        live.qr_cookie_refresh_cooldown = 180
        live.last_qr_cookie_refresh_time = 0
        live._safe_str = str
        live._extract_cookie_value = lambda cookie_record: (cookie_record or {}).get("cookie")
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._set_runtime_cookie_state = mock.Mock()

        def _merge_result(_existing, incoming):
            result = self._build_merge_result(incoming)
            result["missing_required_fields"] = ["cookie2"]
            return result

        live.protected_merge_cookie_dicts = _merge_result

        qr_cookies_str = (
            "unb=qr-unb; sgcookie=qr-sg; cookie2=qr-cookie2; "
            "_m_h5_tk=qr-token_123; _m_h5_tk_enc=qr-enc; t=qr-t"
        )
        lease = self._build_runtime_lease(live.account_id, browser=context.browser, context=context)
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(os.getcwd(), "browser_data", "user_account-qr-missing-required")
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()), \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info, \
             mock.patch("XianyuAutoAsync.db_manager.save_cookie", return_value=True) as save_cookie:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                account_id="account-qr-missing-required",
            )

        self.assertFalse(result)
        runtime_manager.acquire_runtime.assert_awaited_once_with(
            "account-qr-missing-required",
            "verification_recovery",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="qr_cookie_refresh_completed",
        )
        self.assertEqual(context.cookies.await_count, 4)
        update_cookie_account_info.assert_not_called()
        save_cookie.assert_not_called()
        live._set_runtime_cookie_state.assert_not_called()

    async def test_refresh_cookies_from_qr_login_uses_home_and_fresh_tab_to_fill_missing_required_fields(self):
        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()
        page.wait_for_load_state = mock.AsyncMock()
        page.close = mock.AsyncMock()

        fresh_page = mock.Mock()
        fresh_page.goto = mock.AsyncMock()
        fresh_page.reload = mock.AsyncMock()
        fresh_page.wait_for_load_state = mock.AsyncMock()
        fresh_page.close = mock.AsyncMock()

        qr_cookie_items = [
            {"name": "unb", "value": "new-unb"},
            {"name": "sgcookie", "value": "new-sg"},
            {"name": "cookie2", "value": "new-cookie2"},
            {"name": "t", "value": "new-t"},
            {"name": "_tb_token_", "value": "new-tb-token"},
        ]
        stabilized_cookie_items = qr_cookie_items + [
            {"name": "_m_h5_tk", "value": "new-token_123"},
            {"name": "_m_h5_tk_enc", "value": "new-enc"},
            {"name": "cna", "value": "new-cna"},
        ]

        context = mock.Mock()
        context.browser = mock.Mock()
        context.browser.close = mock.AsyncMock()
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=fresh_page)
        context.cookies = mock.AsyncMock(
            side_effect=[
                list(qr_cookie_items),
                list(qr_cookie_items),
                list(qr_cookie_items),
                list(qr_cookie_items),
                list(stabilized_cookie_items),
            ]
        )
        context.close = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-qr-fresh-tab-recovery"
        live.account_id = "account-qr-fresh-tab-recovery"
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
            "t=qr-t; _tb_token_=qr-tb-token"
        )
        lease = self._build_runtime_lease(
            live.account_id,
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(os.getcwd(), "browser_data", "user_account-qr-fresh-tab-recovery")
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()), \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": ""}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info, \
             mock.patch("XianyuAutoAsync.db_manager.save_cookie", return_value=True) as save_cookie:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                account_id="account-qr-fresh-tab-recovery",
            )

        self.assertTrue(result)
        runtime_manager.acquire_runtime.assert_awaited_once_with(
            "account-qr-fresh-tab-recovery",
            "verification_recovery",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="qr_cookie_refresh_completed",
        )
        context.add_cookies.assert_awaited_once()
        context.new_page.assert_awaited_once_with()
        self.assertEqual(
            page.goto.await_args_list,
            [
                mock.call("https://www.goofish.com/im", wait_until="domcontentloaded", timeout=15000),
                mock.call("https://www.goofish.com/", wait_until="domcontentloaded", timeout=15000),
                mock.call("https://www.goofish.com/im", wait_until="domcontentloaded", timeout=15000),
            ],
        )
        page.reload.assert_awaited()
        self.assertEqual(
            fresh_page.goto.await_args_list,
            [
                mock.call("https://www.goofish.com/", wait_until="domcontentloaded", timeout=15000),
            ],
        )
        fresh_page.close.assert_awaited_once_with()
        self.assertEqual(context.cookies.await_count, 5)
        get_cookie_details.assert_called()
        update_cookie_account_info.assert_called_once()
        save_cookie.assert_not_called()
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_rejects_legacy_cookie_id_contract(self):
        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()
        page.close = mock.AsyncMock()

        context = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=page)
        context.close = mock.AsyncMock()
        context.cookies = mock.AsyncMock(
            return_value=[
                {"name": "unb", "value": "new-unb"},
                {"name": "cookie2", "value": "new-cookie2"},
                {"name": "_m_h5_tk", "value": "new-token_123"},
                {"name": "_m_h5_tk_enc", "value": "new-enc"},
                {"name": "sgcookie", "value": "new-sg"},
                {"name": "t", "value": "new-t"},
                {"name": "cna", "value": "new-cna"},
            ]
        )
        context.browser = mock.Mock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-runtime-self"
        live.account_id = ""
        live.user_id = 17
        live.qr_cookie_refresh_cooldown = 180
        live.last_qr_cookie_refresh_time = 0
        live._safe_str = str
        live._extract_cookie_value = lambda cookie_record: (cookie_record or {}).get("cookie")
        live._mask_secret_value = lambda value, head=8, tail=6: value
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._set_runtime_cookie_state = mock.Mock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)

        qr_cookies_str = (
            "unb=qr-unb; sgcookie=qr-sg; cookie2=qr-cookie2; "
            "_m_h5_tk=qr-token_123; _m_h5_tk_enc=qr-enc; t=qr-t"
        )
        lease = self._build_runtime_lease(
            "alias-db-account-1",
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(
                    os.getcwd(),
                    "browser_data",
                    "user_alias-db-account-1",
                )
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(
                     side_effect=AssertionError("should not launch clean browser")
                 ),
                 create=True,
             ) as launch_browser_safe, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()), \
             mock.patch(
                 "XianyuAutoAsync.db_manager.get_cookie_details",
                 return_value={"cookie": qr_cookies_str},
             ) as get_cookie_details, \
             mock.patch(
                 "XianyuAutoAsync.db_manager.update_cookie_account_info",
                 return_value=True,
             ) as update_cookie_account_info:
            with self.assertRaises(TypeError):
                await live.refresh_cookies_from_qr_login(
                    qr_cookies_str,
                    cookie_id="alias-db-account-1",
                )

        runtime_manager.acquire_runtime.assert_not_awaited()
        get_cookie_details.assert_not_called()
        update_cookie_account_info.assert_not_called()
        live._set_runtime_cookie_state.assert_not_called()
        launch_browser_safe.assert_not_awaited()

    async def test_refresh_cookies_from_qr_login_rejects_blank_account_id_without_cookie_id_fallback(self):
        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()
        page.close = mock.AsyncMock()

        context = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=page)
        context.close = mock.AsyncMock()
        context.cookies = mock.AsyncMock(
            return_value=[
                {"name": "unb", "value": "new-unb"},
                {"name": "cookie2", "value": "new-cookie2"},
                {"name": "_m_h5_tk", "value": "new-token_123"},
                {"name": "_m_h5_tk_enc", "value": "new-enc"},
                {"name": "sgcookie", "value": "new-sg"},
                {"name": "t", "value": "new-t"},
                {"name": "cna", "value": "new-cna"},
            ]
        )
        context.browser = mock.Mock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-runtime-self"
        live.account_id = ""
        live.user_id = 19
        live.qr_cookie_refresh_cooldown = 180
        live.last_qr_cookie_refresh_time = 0
        live._safe_str = str
        live._extract_cookie_value = lambda cookie_record: (cookie_record or {}).get("cookie")
        live._mask_secret_value = lambda value, head=8, tail=6: value
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._set_runtime_cookie_state = mock.Mock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)

        qr_cookies_str = (
            "unb=qr-unb; sgcookie=qr-sg; cookie2=qr-cookie2; "
            "_m_h5_tk=qr-token_123; _m_h5_tk_enc=qr-enc; t=qr-t"
        )
        lease = self._build_runtime_lease(
            "alias-db-account-blank-1",
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(
                    os.getcwd(),
                    "browser_data",
                    "user_alias-db-account-blank-1",
                )
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(
                     side_effect=AssertionError("should not launch clean browser")
                 ),
                 create=True,
             ) as launch_browser_safe, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()), \
             mock.patch(
                 "XianyuAutoAsync.db_manager.get_cookie_details",
                 return_value={"cookie": qr_cookies_str},
             ) as get_cookie_details, \
             mock.patch(
                 "XianyuAutoAsync.db_manager.update_cookie_account_info",
                 return_value=True,
             ) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                account_id="   ",
            )

        self.assertFalse(result)
        runtime_manager.acquire_runtime.assert_not_awaited()
        get_cookie_details.assert_not_called()
        update_cookie_account_info.assert_not_called()
        live._set_runtime_cookie_state.assert_not_called()
        launch_browser_safe.assert_not_awaited()

    async def test_refresh_cookies_from_qr_login_rejects_foreign_account_id_when_canonical_account_exists(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-runtime-self"
        live.account_id = "current-db-account-1"
        live.user_id = 23
        live.qr_cookie_refresh_cooldown = 180
        live.last_qr_cookie_refresh_time = 0
        live._safe_str = str
        live._extract_cookie_value = lambda cookie_record: (cookie_record or {}).get("cookie")
        live._mask_secret_value = lambda value, head=8, tail=6: value
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._set_runtime_cookie_state = mock.Mock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)

        qr_cookies_str = (
            "unb=qr-unb; sgcookie=qr-sg; cookie2=qr-cookie2; "
            "_m_h5_tk=qr-token_123; _m_h5_tk_enc=qr-enc; t=qr-t"
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(
                side_effect=AssertionError("should not acquire runtime for foreign account_id scope")
            ),
            get_fresh_page=mock.AsyncMock(),
            release_runtime=mock.AsyncMock(),
            resolve_profile_dir=mock.Mock(),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(
                     side_effect=AssertionError("should not launch clean browser")
                 ),
                 create=True,
             ) as launch_browser_safe, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()), \
             mock.patch(
                 "XianyuAutoAsync.db_manager.get_cookie_details",
                 return_value={"cookie": qr_cookies_str},
             ) as get_cookie_details, \
             mock.patch(
                 "XianyuAutoAsync.db_manager.update_cookie_account_info",
                 return_value=True,
             ) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                account_id="foreign-db-account-1",
            )

        self.assertFalse(result)
        runtime_manager.acquire_runtime.assert_not_awaited()
        get_cookie_details.assert_not_called()
        update_cookie_account_info.assert_not_called()
        live._set_runtime_cookie_state.assert_not_called()
        launch_browser_safe.assert_not_awaited()

    async def test_refresh_cookies_from_qr_login_uses_account_id_boundary_for_db_updates_when_account_id_provided(self):
        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()
        page.close = mock.AsyncMock()

        context = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=page)
        context.close = mock.AsyncMock()
        context.cookies = mock.AsyncMock(
            return_value=[
                {"name": "unb", "value": "new-unb"},
                {"name": "cookie2", "value": "new-cookie2"},
                {"name": "_m_h5_tk", "value": "new-token_123"},
                {"name": "_m_h5_tk_enc", "value": "new-enc"},
                {"name": "sgcookie", "value": "new-sg"},
                {"name": "t", "value": "new-t"},
                {"name": "cna", "value": "new-cna"},
            ]
        )
        context.browser = mock.Mock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-runtime-self"
        live.account_id = "explicit-db-account-1"
        live.user_id = 18
        live.qr_cookie_refresh_cooldown = 180
        live.last_qr_cookie_refresh_time = 0
        live._safe_str = str
        live._extract_cookie_value = lambda cookie_record: (cookie_record or {}).get("cookie")
        live._mask_secret_value = lambda value, head=8, tail=6: value
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._set_runtime_cookie_state = mock.Mock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)

        qr_cookies_str = (
            "unb=qr-unb; sgcookie=qr-sg; cookie2=qr-cookie2; "
            "_m_h5_tk=qr-token_123; _m_h5_tk_enc=qr-enc; t=qr-t"
        )
        lease = self._build_runtime_lease(
            "explicit-db-account-1",
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(
                    os.getcwd(),
                    "browser_data",
                    "user_explicit-db-account-1",
                )
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(
                     side_effect=AssertionError("should not launch clean browser")
                 ),
                 create=True,
             ) as launch_browser_safe, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()), \
             mock.patch(
                 "XianyuAutoAsync.db_manager.get_cookie_details",
                 return_value={"cookie": qr_cookies_str},
             ) as get_cookie_details, \
             mock.patch(
                 "XianyuAutoAsync.db_manager.update_cookie_account_info",
                 return_value=True,
             ) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                account_id="explicit-db-account-1",
            )

        self.assertTrue(result)
        runtime_manager.acquire_runtime.assert_awaited_once_with(
            "explicit-db-account-1",
            "verification_recovery",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        self.assertEqual(
            get_cookie_details.call_args_list,
            [mock.call("explicit-db-account-1"), mock.call("explicit-db-account-1")],
        )
        update_cookie_account_info.assert_called_once_with(
            "explicit-db-account-1",
            cookie_value=mock.ANY,
        )
        live._set_runtime_cookie_state.assert_called_once()
        launch_browser_safe.assert_not_awaited()

    async def test_refresh_cookies_from_qr_login_rejects_managed_handoff_when_canonical_account_missing(self):
        managed_runtime = mock.Mock()
        managed_runtime.close = mock.AsyncMock()

        page = mock.Mock()
        page.goto = mock.AsyncMock()
        page.reload = mock.AsyncMock()
        page.close = mock.AsyncMock()

        context = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=page)
        context.cookies = mock.AsyncMock(return_value=[])
        context.close = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-managed-handoff-only"
        live.account_id = "   "
        live.user_id = 18
        live.qr_cookie_refresh_cooldown = 180
        live.last_qr_cookie_refresh_time = 0
        live._safe_str = str
        live._extract_cookie_value = lambda cookie_record: (cookie_record or {}).get("cookie")
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._set_runtime_cookie_state = mock.Mock()
        live._release_browser_recovery_runtime = mock.AsyncMock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)

        qr_cookies_str = (
            "unb=qr-unb; sgcookie=qr-sg; cookie2=qr-cookie2; "
            "_m_h5_tk=qr-token_123; _m_h5_tk_enc=qr-enc; t=qr-t"
        )
        lease = self._build_runtime_lease(
            "explicit-handoff-account-1",
            browser=managed_runtime,
            context=context,
        )
        lease.pages.append(page)

        with mock.patch(
            "utils.xianyu_utils.trans_cookies",
            side_effect=AssertionError("should not parse qr cookies when canonical account_id is missing"),
        ) as trans_cookies, \
             mock.patch(
                 "XianyuAutoAsync.db_manager.get_cookie_details",
                 return_value={"cookie": qr_cookies_str},
             ) as get_cookie_details, \
             mock.patch(
                 "XianyuAutoAsync.db_manager.update_cookie_account_info",
                 return_value=True,
             ) as update_cookie_account_info, \
             mock.patch(
                 "XianyuAutoAsync.db_manager.save_cookie",
                 return_value=True,
             ) as save_cookie:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                account_id="explicit-handoff-account-1",
                managed_runtime_lease=lease,
                managed_runtime=managed_runtime,
                managed_context=context,
                managed_page=page,
            )

        self.assertFalse(result)
        trans_cookies.assert_not_called()
        context.add_cookies.assert_not_awaited()
        context.new_page.assert_not_awaited()
        page.goto.assert_not_awaited()
        page.reload.assert_not_awaited()
        get_cookie_details.assert_not_called()
        update_cookie_account_info.assert_not_called()
        save_cookie.assert_not_called()
        live._set_runtime_cookie_state.assert_not_called()
        live._release_browser_recovery_runtime.assert_awaited_once_with(
            lease,
            browser=managed_runtime,
            context=context,
            page=page,
            reason="qr_cookie_refresh_completed",
        )

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
        live.account_id = "2095002164"
        live._legacy_cookie_id = "2095002164"
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
        lease = self._build_runtime_lease(live.account_id, browser=context.browser, context=context)
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            invalidate_runtime=mock.AsyncMock(return_value=True),
            resolve_profile_dir=mock.Mock(return_value=os.path.join(os.getcwd(), "browser_data", "user_2095002164")),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("should not launch clean browser")),
                 create=True,
             ):
            success = await live._refresh_cookies_via_browser_page(
                live.cookies_str,
                restart_on_success=False,
            )

        self.assertTrue(success)
        runtime_manager.acquire_runtime.assert_awaited_once_with(
            "2095002164",
            "cookie_refresh",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_request = runtime_manager.acquire_runtime.await_args.kwargs["runtime_request"]
        self.assertEqual(
            runtime_request["profile_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_2095002164"),
        )
        self.assertTrue(runtime_request["launch_options"]["headless"])
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="browser_stabilization_completed",
        )
        runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "2095002164",
            reason="browser_stabilization_completed_post_release_invalidate",
        )
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
        live.account_id = "recent-slider-user"
        live._legacy_cookie_id = "recent-slider-user"
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
        lease = self._build_runtime_lease(live.account_id, browser=context.browser, context=context)
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            invalidate_runtime=mock.AsyncMock(return_value=True),
            resolve_profile_dir=mock.Mock(return_value=os.path.join(os.getcwd(), "browser_data", "user_recent-slider-user")),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("should not launch clean browser")),
                 create=True,
             ):
            success = await live._refresh_cookies_via_browser_page(
                live.cookies_str,
                restart_on_success=False,
            )

        self.assertTrue(success)
        runtime_manager.acquire_runtime.assert_awaited_once_with(
            "recent-slider-user",
            "cookie_refresh",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_request = runtime_manager.acquire_runtime.await_args.kwargs["runtime_request"]
        self.assertEqual(
            runtime_request["profile_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_recent-slider-user"),
        )
        self.assertTrue(runtime_request["launch_options"]["headless"])
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="browser_stabilization_completed",
        )
        runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "recent-slider-user",
            reason="browser_stabilization_completed_post_release_invalidate",
        )
        live.update_config_cookies.assert_awaited_once_with()

    async def test_refresh_cookies_via_browser_page_rejects_default_or_blank_canonical_account_id_before_runtime(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-browser-page-invalid"
                live.account_id = invalid_account_id
                live.cookies_str = "unb=user1; cookie2=old"
                live.cookies = {"unb": "user1", "cookie2": "old"}
                live._safe_str = str
                live.update_config_cookies = mock.AsyncMock()
                live._set_runtime_cookie_state = mock.Mock()
                live._release_browser_recovery_runtime = mock.AsyncMock()

                with mock.patch(
                    "utils.xianyu_utils.trans_cookies",
                    side_effect=AssertionError("should not parse cookies without canonical account_id"),
                ) as trans_cookies, \
                     mock.patch.object(
                         live,
                         "_open_browser_recovery_context",
                         new=mock.AsyncMock(
                             side_effect=AssertionError("should not request recovery context without canonical account_id")
                         ),
                     ) as open_browser_recovery_context:
                    success = await live._refresh_cookies_via_browser_page(
                        live.cookies_str,
                        restart_on_success=False,
                    )

                self.assertFalse(success)
                trans_cookies.assert_not_called()
                open_browser_recovery_context.assert_not_awaited()
                live.update_config_cookies.assert_not_awaited()
                live._set_runtime_cookie_state.assert_not_called()
                live._release_browser_recovery_runtime.assert_not_awaited()

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
        live.account_id = "2095002164"
        live._legacy_cookie_id = "2095002164"
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

    async def test_try_password_login_refresh_disables_clean_context_during_manual_refresh_handoff(self):
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
        live.account_id = "manual-handoff-user"
        live._legacy_cookie_id = "manual-handoff-user"
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
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value={"phase": "handoff_recovery"})
        live._has_recent_slider_success = mock.Mock(return_value=False)

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
            success = await live._try_password_login_refresh("手动刷新交接恢复窗口", trigger_scene="token_refresh")

        self.assertFalse(success)
        self.assertTrue(captured["init_kwargs"]["use_account_persistent_profile"])
        self.assertEqual(
            captured["init_kwargs"]["account_persistent_profile_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_manual-handoff-user"),
        )
        self.assertFalse(captured["run_kwargs"]["force_clean_context"])

    async def test_try_password_login_refresh_disables_clean_context_after_recent_slider_success(self):
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
        live.account_id = "recent-slider-refresh-user"
        live._legacy_cookie_id = "recent-slider-refresh-user"
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
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=True)

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
            success = await live._try_password_login_refresh("最近刚通过滑块", trigger_scene="token_refresh")

        self.assertFalse(success)
        self.assertTrue(captured["init_kwargs"]["use_account_persistent_profile"])
        self.assertEqual(
            captured["init_kwargs"]["account_persistent_profile_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_recent-slider-refresh-user"),
        )
        self.assertFalse(captured["run_kwargs"]["force_clean_context"])

    async def test_try_password_login_refresh_wires_managed_runtime_with_account_id_alias(self):
        captured = {}
        page = mock.Mock()
        context = mock.Mock()
        context.browser = mock.Mock()
        lease = self._build_runtime_lease(
            "account-managed-id",
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime_sync=mock.Mock(return_value=lease),
            get_fresh_page_sync=mock.Mock(return_value=(page, context)),
            release_runtime_sync=mock.Mock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(os.getcwd(), "browser_data", "user_account-managed-id")
            ),
        )

        class _FakeSlider:
            def __init__(self, *args, **kwargs):
                captured["init_kwargs"] = kwargs

            def build_managed_runtime_request(self, **kwargs):
                captured["request_kwargs"] = kwargs
                return {
                    "browser_features": {"feature": "managed"},
                    "profile_id": "profile-managed",
                    "launch_options": {"args": ["--managed"]},
                    "use_persistent_context": True,
                    "profile_dir": os.path.join(os.getcwd(), "browser_data", "user_account-managed-id"),
                }

            def attach_managed_runtime(self, **kwargs):
                captured["attach_kwargs"] = kwargs

            def login_with_password_browser(self, *args, **kwargs):
                captured["login_kwargs"] = kwargs
                raise RuntimeError("sentinel-slider-stop")

            async def _run_sync_method_on_fresh_thread(self, func, **kwargs):
                captured["run_kwargs"] = kwargs
                return func(**kwargs)

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-id"
        live.account_id = "account-managed-id"
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
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=False)

        with mock.patch.object(XianyuAutoAsync, "account_browser_runtime_manager", new=runtime_manager), \
             mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()) as log_captcha_event, \
             mock.patch.object(XianyuAutoAsync.db_manager, "mark_stale_risk_control_logs_failed", return_value=0), \
             mock.patch.object(XianyuAutoAsync.db_manager, "get_cookie_details", return_value={
                 "cookie_value": live.cookies_str,
                 "username": "user@example.com",
                 "password": "secret",
                 "show_browser": False,
             }) as get_cookie_details_mock, \
             mock.patch.object(XianyuLive, "acquire_auth_recovery_lock", return_value=(True, None)) as acquire_auth_recovery_lock_mock, \
             mock.patch.object(XianyuLive, "release_auth_recovery_lock") as release_auth_recovery_lock_mock, \
             mock.patch.object(XianyuLive, "classify_password_login_failure", return_value=("unknown", 0)), \
             mock.patch.object(XianyuLive, "set_password_login_failure_backoff"), \
             mock.patch.object(XianyuAutoAsync.os, "getenv", return_value=""), \
             mock.patch.dict(sys.modules, {
                 "utils.xianyu_slider_stealth": types.SimpleNamespace(XianyuSliderStealth=_FakeSlider),
             }):
            success = await live._try_password_login_refresh("密码恢复需要受管runtime", trigger_scene="token_refresh")

        self.assertFalse(success)
        self.assertEqual(captured["init_kwargs"]["user_id"], "account-managed-id")
        self.assertEqual(
            captured["init_kwargs"]["account_persistent_profile_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_account-managed-id"),
        )
        self.assertEqual(
            captured["request_kwargs"],
            {"account_id": "account-managed-id", "purpose": "password_login"},
        )
        runtime_manager.acquire_runtime_sync.assert_called_once_with(
            "account-managed-id",
            "password_login",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_manager.get_fresh_page_sync.assert_called_once_with(lease)
        runtime_manager.release_runtime_sync.assert_called_once_with(
            lease,
            reason="password_login_refresh_completed",
        )
        acquire_auth_recovery_lock_mock.assert_called_once_with(
            "account-managed-id",
            mock.ANY,
        )
        release_auth_recovery_lock_mock.assert_called_once_with(
            "account-managed-id",
            mock.ANY,
        )
        get_cookie_details_mock.assert_called_once_with("account-managed-id")
        self.assertIs(captured["attach_kwargs"]["lease"], lease)
        self.assertIs(captured["attach_kwargs"]["runtime"], lease.runtime)
        self.assertIs(captured["attach_kwargs"]["browser"], context.browser)
        self.assertIs(captured["attach_kwargs"]["context"], context)
        self.assertIs(captured["attach_kwargs"]["page"], page)
        self.assertEqual(captured["attach_kwargs"]["profile_id"], "profile-managed")
        logged_accounts = [
            call.kwargs.get("account_id", call.args[0] if call.args else None)
            for call in log_captcha_event.call_args_list
        ]
        self.assertIn("account-managed-id", logged_accounts)
        self.assertNotIn("legacy-cookie-id", logged_accounts)
        self.assertFalse(captured["login_kwargs"]["force_clean_context"])
        self.assertTrue(captured["login_kwargs"]["require_managed_runtime"])

    async def test_try_password_login_refresh_success_logs_account_id_alias_instead_of_stale_cookie_id(self):
        captured = {}
        merged_cookies = {
            "unb": "new-unb",
            "cookie2": "new-cookie2",
            "_m_h5_tk": "new-token_123",
            "_m_h5_tk_enc": "new-enc",
            "sgcookie": "new-sg",
            "t": "new-t",
        }
        expected_cookie_string = "; ".join([f"{k}={v}" for k, v in merged_cookies.items()])

        class _FakeSlider:
            def __init__(self, *args, **kwargs):
                captured["init_kwargs"] = kwargs

            async def _run_sync_method_on_fresh_thread(self, func, **kwargs):
                captured["run_kwargs"] = kwargs
                return dict(merged_cookies)

        def _fake_preflight_init(
            self,
            cookies_str=None,
            account_id=None,
            user_id=None,
            register_instance=True,
            **kwargs,
        ):
            self.cookies_str = cookies_str
            self.account_id = account_id
            self.user_id = user_id

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-password-success-log"
        live.account_id = "account-password-success-log"
        live.cookies_str = (
            "unb=old-unb; cookie2=old-cookie2; _m_h5_tk=old-token_123; "
            "_m_h5_tk_enc=old-enc; sgcookie=old-sg; t=old-t"
        )
        live.cookies = {
            "unb": "old-unb",
            "cookie2": "old-cookie2",
            "_m_h5_tk": "old-token_123",
            "_m_h5_tk_enc": "old-enc",
            "sgcookie": "old-sg",
            "t": "old-t",
        }
        live.proxy_config = {}
        live.user_id = 88
        live.last_token_refresh_error_message = None
        live._safe_str = str
        live._normalize_risk_trigger_scene = XianyuLive._normalize_risk_trigger_scene.__get__(live, XianyuLive)
        live._new_risk_session_id = mock.Mock(return_value="risk-session-success")
        live._build_risk_event_meta = mock.Mock(return_value={"account_id": "account-password-success-log"})
        live._create_risk_log = mock.Mock(return_value=456)
        live._update_risk_log = mock.Mock()
        live.send_token_refresh_notification = mock.AsyncMock()
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live.is_manual_refresh_active = mock.Mock(return_value=False)
        live._has_recent_slider_success = mock.Mock(return_value=False)
        live._should_prefer_account_persistent_profile_for_browser_recovery = mock.Mock(
            return_value=(False, "no profile reuse")
        )
        live._resolve_account_browser_profile_dir = mock.Mock(
            return_value=os.path.join(os.getcwd(), "browser_data", "user_account-password-success-log")
        )
        live.protected_merge_cookie_dicts = mock.Mock(
            return_value={
                "merged_cookies_dict": dict(merged_cookies),
                "updated_fields": list(merged_cookies.keys()),
                "changed_fields": list(merged_cookies.keys()),
                "new_fields": [],
                "preserved_fields": [],
                "preserved_protected_fields": [],
                "would_remove_fields": [],
                "removed_fields": [],
                "missing_protected_fields": [],
                "missing_required_fields": [],
                "incoming_missing_protected_fields": [],
                "account_switched": False,
            }
        )
        live._log_protected_merge_event = mock.Mock()
        live._log_cookie_merge_summary = mock.Mock()
        live._summarize_cookie_string = mock.Mock(return_value="cookie-summary")
        live._update_cookies_and_restart = mock.AsyncMock(return_value=True)
        mock_logger = mock.Mock()

        with mock.patch.object(XianyuAutoAsync.db_manager, "mark_stale_risk_control_logs_failed", return_value=0), \
             mock.patch.object(
                 XianyuAutoAsync.db_manager,
                 "get_cookie_details",
                 return_value={
                     "cookie_value": live.cookies_str,
                     "username": "user@example.com",
                     "password": "secret",
                     "show_browser": False,
                 },
             ), \
             mock.patch.object(XianyuLive, "acquire_auth_recovery_lock", return_value=(True, None)), \
             mock.patch.object(XianyuLive, "release_auth_recovery_lock"), \
             mock.patch.object(XianyuLive, "classify_password_login_failure", return_value=("unknown", 0)), \
             mock.patch.object(XianyuLive, "set_password_login_failure_backoff"), \
             mock.patch.object(XianyuLive, "clear_password_login_failure_backoff") as clear_password_login_failure_backoff, \
             mock.patch.object(XianyuLive, "__init__", new=_fake_preflight_init), \
             mock.patch.object(
                 XianyuLive,
                 "preflight_token_after_password_login",
                 new=mock.AsyncMock(return_value=None),
             ), \
             mock.patch.object(XianyuAutoAsync.os, "getenv", return_value=""), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger), \
             mock.patch.dict(
                 sys.modules,
                 {"utils.xianyu_slider_stealth": types.SimpleNamespace(XianyuSliderStealth=_FakeSlider)},
             ):
            success = await live._try_password_login_refresh(
                "密码恢复成功日志验证",
                trigger_scene="token_refresh",
            )

        self.assertTrue(success)
        self.assertEqual(captured["run_kwargs"]["resolved_account_id"], "account-password-success-log")
        self.assertTrue(captured["run_kwargs"]["force_clean_context"])
        clear_password_login_failure_backoff.assert_called_once_with("account-password-success-log")
        live._update_cookies_and_restart.assert_awaited_once_with(expected_cookie_string)
        live.send_token_refresh_notification.assert_awaited_once_with(
            "账号密码登录成功，Cookie已获取，准备更新并重启",
            "cookie_refresh_success",
        )
        self.assertTrue(
            any(call.kwargs.get("result_code") == "cookie_refresh_success" for call in live._update_risk_log.call_args_list)
        )
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("account-password-success-log", messages)
        self.assertNotIn("legacy-password-success-log", messages)

    async def test_try_password_login_refresh_rejects_blank_canonical_account_id_before_runtime_setup(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-password-refresh"
        live.account_id = "   "
        live._normalize_risk_trigger_scene = XianyuLive._normalize_risk_trigger_scene.__get__(live, XianyuLive)
        live._new_risk_session_id = mock.Mock(return_value="risk-session")

        with mock.patch.object(
            XianyuAutoAsync.db_manager,
            "mark_stale_risk_control_logs_failed",
            side_effect=AssertionError("should not touch db without canonical account_id"),
        ) as mark_stale_risk_control_logs_failed, \
             mock.patch.object(
                 XianyuLive,
                 "acquire_auth_recovery_lock",
                 side_effect=AssertionError("should not acquire recovery lock without canonical account_id"),
             ) as acquire_auth_recovery_lock:
            success = await live._try_password_login_refresh(
                "密码恢复需要受管runtime",
                trigger_scene="token_refresh",
            )

        self.assertFalse(success)
        live._new_risk_session_id.assert_called_once_with("cookie")
        mark_stale_risk_control_logs_failed.assert_not_called()
        acquire_auth_recovery_lock.assert_not_called()

    async def test_try_password_login_refresh_missing_credentials_logs_account_id_alias_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-password-refresh-log"
        live.account_id = "account-password-refresh-log"
        live._safe_str = str
        live._normalize_risk_trigger_scene = XianyuLive._normalize_risk_trigger_scene.__get__(live, XianyuLive)
        live._new_risk_session_id = mock.Mock(return_value="risk-session-log")
        live._create_risk_log = mock.Mock(return_value=321)
        live._update_risk_log = mock.Mock()
        live._build_risk_event_meta = mock.Mock(return_value={"account_id": "account-password-refresh-log"})
        live.is_manual_refresh_active = mock.Mock(return_value=False)
        live.send_token_refresh_notification = mock.AsyncMock()
        mock_logger = mock.Mock()

        with mock.patch.object(
            XianyuAutoAsync.db_manager,
            "mark_stale_risk_control_logs_failed",
            return_value=0,
        ), mock.patch.object(
            XianyuAutoAsync.db_manager,
            "get_cookie_details",
            return_value={"username": "", "password": "", "show_browser": False},
        ), mock.patch.object(
            XianyuLive,
            "acquire_auth_recovery_lock",
            return_value=(True, None),
        ), mock.patch.object(
            XianyuLive,
            "release_auth_recovery_lock",
        ), mock.patch.object(
            XianyuAutoAsync,
            "log_captcha_event",
            mock.Mock(),
        ), mock.patch.object(
            XianyuAutoAsync,
            "logger",
            mock_logger,
        ):
            success = await live._try_password_login_refresh(
                "缺少账号密码触发密码登录刷新",
                trigger_scene="token_refresh",
            )

        self.assertFalse(success)
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("account-password-refresh-log", messages)
        self.assertNotIn("legacy-password-refresh-log", messages)
        self.assertEqual(live.last_token_refresh_status, "no_credentials")
        live.send_token_refresh_notification.assert_awaited_once_with(
            "检测到缺少账号密码触发密码登录刷新，但未配置用户名或密码，无法自动刷新Cookie",
            "no_credentials",
        )
        live._update_risk_log.assert_called_once()
        self.assertEqual(live._update_risk_log.call_args.kwargs["result_code"], "missing_credentials")

    def test_run_password_login_with_managed_runtime_uses_canonical_account_id_when_resolved_account_id_blank(self):
        captured = {}
        page = mock.Mock()
        context = mock.Mock()
        context.browser = mock.Mock()
        lease = self._build_runtime_lease(
            "managed-canonical-account",
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime_sync=mock.Mock(return_value=lease),
            get_fresh_page_sync=mock.Mock(return_value=(page, context)),
            release_runtime_sync=mock.Mock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(os.getcwd(), "browser_data", "user_attach-fail-account")
            ),
        )

        class _FakeSlider:
            def build_managed_runtime_request(self, **kwargs):
                captured["request_kwargs"] = kwargs
                return {
                    "browser_features": {"feature": "managed"},
                    "profile_id": "profile-managed-fallback",
                    "launch_options": {"args": ["--managed"]},
                }

            def attach_managed_runtime(self, **kwargs):
                captured["attach_kwargs"] = kwargs

            def login_with_password_browser(self, *args, **kwargs):
                captured["login_kwargs"] = kwargs
                return "ok"

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-managed-cookie"
        live.account_id = "managed-canonical-account"

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ):
            result = live._run_password_login_with_managed_runtime(
                slider=_FakeSlider(),
                resolved_account_id="   ",
                account="user@example.com",
                password="secret",
                show_browser=False,
                notification_callback=None,
                force_clean_context=False,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(
            captured["request_kwargs"],
            {"account_id": "managed-canonical-account", "purpose": "password_login"},
        )
        runtime_manager.acquire_runtime_sync.assert_called_once_with(
            "managed-canonical-account",
            "password_login",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_manager.get_fresh_page_sync.assert_called_once_with(lease)
        runtime_manager.release_runtime_sync.assert_called_once_with(
            lease,
            reason="password_login_refresh_completed",
        )
        self.assertIs(captured["attach_kwargs"]["lease"], lease)
        self.assertIs(captured["attach_kwargs"]["runtime"], lease.runtime)
        self.assertIs(captured["attach_kwargs"]["browser"], context.browser)
        self.assertIs(captured["attach_kwargs"]["context"], context)
        self.assertIs(captured["attach_kwargs"]["page"], page)
        self.assertEqual(captured["attach_kwargs"]["profile_id"], "profile-managed-fallback")
        self.assertFalse(captured["login_kwargs"]["force_clean_context"])
        self.assertTrue(captured["login_kwargs"]["require_managed_runtime"])

    def test_run_password_login_with_managed_runtime_rejects_missing_canonical_account_id(self):
        class _FakeSlider:
            def build_managed_runtime_request(self, **kwargs):
                raise AssertionError("should not build runtime request without canonical account_id")

            def attach_managed_runtime(self, **kwargs):
                raise AssertionError("should not attach runtime without canonical account_id")

        runtime_manager = types.SimpleNamespace(
            acquire_runtime_sync=mock.Mock(
                side_effect=AssertionError("should not acquire runtime without canonical account_id")
            ),
        )

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-managed-cookie"
        live.account_id = "   "

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ):
            with self.assertRaisesRegex(RuntimeError, "缺少 canonical account_id"):
                live._run_password_login_with_managed_runtime(
                    slider=_FakeSlider(),
                    resolved_account_id="   ",
                    account="user@example.com",
                    password="secret",
                    show_browser=False,
                    notification_callback=None,
                    force_clean_context=False,
                )

        runtime_manager.acquire_runtime_sync.assert_not_called()

    def test_run_password_login_with_managed_runtime_rejects_foreign_resolved_account_id(self):
        class _FakeSlider:
            def build_managed_runtime_request(self, **kwargs):
                raise AssertionError("should not build runtime request for foreign account scope")

            def attach_managed_runtime(self, **kwargs):
                raise AssertionError("should not attach runtime for foreign account scope")

        runtime_manager = types.SimpleNamespace(
            acquire_runtime_sync=mock.Mock(
                side_effect=AssertionError("should not acquire runtime for foreign account scope")
            ),
        )

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-managed-cookie"
        live.account_id = "managed-canonical-account"

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ):
            with self.assertRaisesRegex(RuntimeError, "拒绝跨账号 runtime 请求"):
                live._run_password_login_with_managed_runtime(
                    slider=_FakeSlider(),
                    resolved_account_id="foreign-account-id",
                    account="user@example.com",
                    password="secret",
                    show_browser=False,
                    notification_callback=None,
                    force_clean_context=False,
                )

        runtime_manager.acquire_runtime_sync.assert_not_called()

    async def test_try_password_login_refresh_releases_managed_runtime_when_attach_fails(self):
        page = mock.Mock()
        context = mock.Mock()
        context.browser = mock.Mock()
        lease = self._build_runtime_lease(
            "attach-fail-account",
            browser=context.browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime_sync=mock.Mock(return_value=lease),
            get_fresh_page_sync=mock.Mock(return_value=(page, context)),
            release_runtime_sync=mock.Mock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(os.getcwd(), "browser_data", "user_attach-fail-account")
            ),
        )
        captured = {"login_called": False}

        class _FakeSlider:
            def __init__(self, *args, **kwargs):
                pass

            def build_managed_runtime_request(self, **kwargs):
                return {
                    "browser_features": {"feature": "managed"},
                    "profile_id": "profile-attach-fail",
                    "launch_options": {"args": ["--managed"]},
                    "use_persistent_context": True,
                    "profile_dir": os.path.join(os.getcwd(), "browser_data", "user_attach-fail-account"),
                }

            def attach_managed_runtime(self, **kwargs):
                raise RuntimeError("attach failed")

            def login_with_password_browser(self, *args, **kwargs):
                captured["login_called"] = True
                raise AssertionError("attach 失败后不该继续登录")

            async def _run_sync_method_on_fresh_thread(self, func, **kwargs):
                return func(**kwargs)

        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "attach-fail-account"
        live._legacy_cookie_id = "attach-fail-account"
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
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=False)

        with mock.patch.object(XianyuAutoAsync, "account_browser_runtime_manager", new=runtime_manager), \
             mock.patch.object(XianyuAutoAsync, "log_captcha_event", mock.Mock()), \
             mock.patch.object(XianyuAutoAsync.db_manager, "mark_stale_risk_control_logs_failed", return_value=0), \
             mock.patch.object(XianyuAutoAsync.db_manager, "get_cookie_details", return_value={
                 "cookie_value": live.cookies_str,
                 "username": "user@example.com",
                 "password": "secret",
                 "show_browser": False,
             }), \
             mock.patch.object(XianyuLive, "acquire_auth_recovery_lock", return_value=(True, None)), \
             mock.patch.object(XianyuLive, "release_auth_recovery_lock"), \
             mock.patch.object(XianyuLive, "classify_password_login_failure", return_value=("unknown", 0)), \
             mock.patch.object(XianyuLive, "set_password_login_failure_backoff"), \
             mock.patch.object(XianyuAutoAsync.os, "getenv", return_value=""), \
             mock.patch.dict(sys.modules, {
                 "utils.xianyu_slider_stealth": types.SimpleNamespace(XianyuSliderStealth=_FakeSlider),
             }):
            success = await live._try_password_login_refresh("attach 失败也得释放 runtime", trigger_scene="token_refresh")

        self.assertFalse(success)
        self.assertFalse(captured["login_called"])
        runtime_manager.acquire_runtime_sync.assert_called_once()
        runtime_manager.get_fresh_page_sync.assert_called_once_with(lease)
        runtime_manager.release_runtime_sync.assert_called_once_with(
            lease,
            reason="password_login_refresh_attach_failed",
        )

    async def test_refresh_cookies_from_qr_login_falls_back_when_managed_runtime_lease_scope_is_foreign(self):
        foreign_runtime = mock.Mock()
        foreign_page = mock.Mock()
        foreign_page.goto = mock.AsyncMock()
        foreign_page.reload = mock.AsyncMock()
        foreign_page.close = mock.AsyncMock()
        foreign_context = mock.Mock()
        foreign_context.add_cookies = mock.AsyncMock()
        foreign_context.new_page = mock.AsyncMock()
        foreign_context.cookies = mock.AsyncMock()

        safe_page = mock.Mock()
        safe_page.goto = mock.AsyncMock()
        safe_page.reload = mock.AsyncMock()
        safe_page.close = mock.AsyncMock()
        safe_context = mock.Mock()
        safe_context.browser = mock.Mock()
        safe_context.add_cookies = mock.AsyncMock()
        safe_context.new_page = mock.AsyncMock(return_value=safe_page)
        safe_context.cookies = mock.AsyncMock(
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
        safe_context.close = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-managed-handoff"
        live.account_id = "managed-handoff-account"
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
        foreign_lease = self._build_runtime_lease(
            "foreign-managed-account",
            browser=foreign_runtime,
            context=foreign_context,
        )
        safe_lease = self._build_runtime_lease(
            "managed-handoff-account",
            browser=safe_context.browser,
            context=safe_context,
        )
        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(return_value=(safe_page, safe_context)),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 live,
                 "_open_browser_recovery_context",
                 new=mock.AsyncMock(return_value=(safe_lease, safe_context.browser, safe_context, True)),
             ) as open_browser_recovery_context, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime_lease=foreign_lease,
                managed_runtime=foreign_runtime,
                managed_context=foreign_context,
                managed_page=foreign_page,
            )

        self.assertTrue(result)
        open_browser_recovery_context.assert_awaited_once()
        foreign_context.add_cookies.assert_not_awaited()
        foreign_page.goto.assert_not_awaited()
        foreign_page.reload.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_awaited_once_with(safe_lease)
        runtime_manager.release_runtime.assert_awaited_once_with(
            safe_lease,
            reason="qr_cookie_refresh_completed",
        )
        safe_context.add_cookies.assert_awaited_once()
        safe_page.goto.assert_awaited_once_with(
            "https://www.goofish.com/im",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        safe_page.reload.assert_awaited_once_with(
            wait_until="domcontentloaded",
            timeout=12000,
        )
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_falls_back_when_managed_context_lacks_runtime_lease(self):
        upstream_runtime = mock.Mock()
        upstream_page = mock.Mock()
        upstream_page.goto = mock.AsyncMock()
        upstream_page.reload = mock.AsyncMock()
        upstream_page.close = mock.AsyncMock()
        upstream_context = mock.Mock()
        upstream_context.add_cookies = mock.AsyncMock()
        upstream_context.new_page = mock.AsyncMock()
        upstream_context.cookies = mock.AsyncMock()

        safe_page = mock.Mock()
        safe_page.goto = mock.AsyncMock()
        safe_page.reload = mock.AsyncMock()
        safe_page.close = mock.AsyncMock()
        safe_context = mock.Mock()
        safe_context.browser = mock.Mock()
        safe_context.add_cookies = mock.AsyncMock()
        safe_context.new_page = mock.AsyncMock(return_value=safe_page)
        safe_context.cookies = mock.AsyncMock(
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
        safe_context.close = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-unverified-handoff"
        live.account_id = "managed-handoff-account"
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
        safe_lease = self._build_runtime_lease(
            "managed-handoff-account",
            browser=safe_context.browser,
            context=safe_context,
        )
        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(return_value=(safe_page, safe_context)),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 live,
                 "_open_browser_recovery_context",
                 new=mock.AsyncMock(return_value=(safe_lease, safe_context.browser, safe_context, True)),
             ) as open_browser_recovery_context, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime=upstream_runtime,
                managed_context=upstream_context,
                managed_page=upstream_page,
            )

        self.assertTrue(result)
        open_browser_recovery_context.assert_awaited_once()
        upstream_context.add_cookies.assert_not_awaited()
        upstream_page.goto.assert_not_awaited()
        upstream_page.reload.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_awaited_once_with(safe_lease)
        runtime_manager.release_runtime.assert_awaited_once_with(
            safe_lease,
            reason="qr_cookie_refresh_completed",
        )
        safe_context.add_cookies.assert_awaited_once()
        safe_page.goto.assert_awaited_once_with(
            "https://www.goofish.com/im",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        safe_page.reload.assert_awaited_once_with(
            wait_until="domcontentloaded",
            timeout=12000,
        )
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_rejects_recovery_context_without_runtime_lease(self):
        recovery_context = mock.Mock()
        recovery_context.browser = mock.Mock()
        recovery_context.add_cookies = mock.AsyncMock(
            side_effect=AssertionError("lease-less recovery context should fail before cookie mutation")
        )
        recovery_context.new_page = mock.AsyncMock(
            side_effect=AssertionError("lease-less recovery context should not create page")
        )
        recovery_context.cookies = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-recovery-without-lease"
        live.account_id = "recovery-without-lease"
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
        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 live,
                 "_open_browser_recovery_context",
                 new=mock.AsyncMock(return_value=(None, recovery_context.browser, recovery_context, True)),
             ) as open_browser_recovery_context, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            result = await live.refresh_cookies_from_qr_login(qr_cookies_str)

        self.assertFalse(result)
        open_browser_recovery_context.assert_awaited_once()
        recovery_context.add_cookies.assert_not_awaited()
        recovery_context.new_page.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_not_awaited()

    async def test_refresh_cookies_from_qr_login_reuses_runtime_lease_page_when_managed_context_handoff_includes_lease(self):
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
        live.account_id = "managed-context-test"
        live._legacy_cookie_id = "managed-context-test"
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
        lease = self._build_runtime_lease("managed-context-test", browser=managed_runtime, context=context)
        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "_launch_browser_safe",
            new=mock.AsyncMock(side_effect=AssertionError("managed context path should not launch browser")),
            create=True,
        ) as launch_browser_safe, \
             mock.patch.object(
                 XianyuAutoAsync,
                 "account_browser_runtime_manager",
                 new=runtime_manager,
             ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime_lease=lease,
                managed_runtime=managed_runtime,
                managed_context=context,
            )

        self.assertTrue(result)
        launch_browser_safe.assert_not_awaited()
        context.add_cookies.assert_awaited_once()
        context.new_page.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        page.goto.assert_awaited_once_with(
            "https://www.goofish.com/im",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        page.reload.assert_awaited_once_with(
            wait_until="domcontentloaded",
            timeout=12000,
        )
        page.close.assert_not_awaited()
        context.close.assert_not_awaited()
        managed_runtime.close.assert_not_awaited()
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="qr_cookie_refresh_completed",
        )
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_reopens_account_runtime_when_managed_runtime_lease_already_released(self):
        stale_runtime = mock.Mock()
        stale_runtime.close = mock.AsyncMock()

        stale_page = mock.Mock()
        stale_page.goto = mock.AsyncMock(
            side_effect=AssertionError("released managed_page should not be reused")
        )
        stale_page.reload = mock.AsyncMock()
        stale_page.close = mock.AsyncMock()

        stale_context = mock.Mock()
        stale_context.browser = stale_runtime
        stale_context.add_cookies = mock.AsyncMock(
            side_effect=AssertionError("released managed_context should not receive cookie injection")
        )
        stale_context.new_page = mock.AsyncMock()
        stale_context.cookies = mock.AsyncMock()

        safe_page = mock.Mock()
        safe_page.goto = mock.AsyncMock()
        safe_page.reload = mock.AsyncMock()
        safe_page.close = mock.AsyncMock()

        safe_context = mock.Mock()
        safe_context.browser = mock.Mock()
        safe_context.add_cookies = mock.AsyncMock()
        safe_context.new_page = mock.AsyncMock(return_value=safe_page)
        safe_context.cookies = mock.AsyncMock(
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
        safe_context.close = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-released-handoff-lease"
        live.account_id = "managed-handoff-account"
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
        released_lease = self._build_runtime_lease(
            "managed-handoff-account",
            browser=stale_runtime,
            context=stale_context,
        )
        released_lease.released = True
        safe_lease = self._build_runtime_lease(
            "managed-handoff-account",
            browser=safe_context.browser,
            context=safe_context,
        )

        async def _get_fresh_page(lease):
            if lease is released_lease:
                raise AssertionError("should not reuse released runtime lease")
            return safe_page, safe_context

        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(side_effect=_get_fresh_page),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 live,
                 "_open_browser_recovery_context",
                 new=mock.AsyncMock(return_value=(safe_lease, safe_context.browser, safe_context, True)),
             ) as open_browser_recovery_context, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime_lease=released_lease,
                managed_runtime=stale_runtime,
                managed_context=stale_context,
                managed_page=stale_page,
            )

        self.assertTrue(result)
        open_browser_recovery_context.assert_awaited_once_with(
            "扫码登录Cookie刷新",
            profile_key="managed-handoff-account",
            target_account_id="managed-handoff-account",
            runtime_purpose="verification_recovery",
        )
        stale_context.add_cookies.assert_not_awaited()
        stale_page.goto.assert_not_awaited()
        stale_page.reload.assert_not_awaited()
        stale_page.close.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_awaited_once_with(safe_lease)
        runtime_manager.release_runtime.assert_awaited_once_with(
            safe_lease,
            reason="qr_cookie_refresh_completed",
        )
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_returns_false_when_reopen_after_released_lease_fails(self):
        stale_runtime = mock.Mock()
        stale_runtime.close = mock.AsyncMock()

        stale_context = mock.Mock()
        stale_context.browser = stale_runtime
        stale_context.add_cookies = mock.AsyncMock(
            side_effect=AssertionError("released managed_context should stay untouched when reopen fails")
        )
        stale_context.new_page = mock.AsyncMock()
        stale_context.cookies = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-released-handoff-reopen-fail"
        live.account_id = "managed-handoff-account"
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
        released_lease = self._build_runtime_lease(
            "managed-handoff-account",
            browser=stale_runtime,
            context=stale_context,
        )
        released_lease.released = True

        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(
                side_effect=AssertionError("reopen failure path should not request a fresh page")
            ),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 live,
                 "_open_browser_recovery_context",
                 new=mock.AsyncMock(return_value=(None, None, None, False)),
             ) as open_browser_recovery_context:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime_lease=released_lease,
                managed_runtime=stale_runtime,
                managed_context=stale_context,
            )

        self.assertFalse(result)
        open_browser_recovery_context.assert_awaited_once_with(
            "扫码登录Cookie刷新",
            profile_key="managed-handoff-account",
            target_account_id="managed-handoff-account",
            runtime_purpose="verification_recovery",
        )
        stale_context.add_cookies.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_not_awaited()
        runtime_manager.release_runtime.assert_not_awaited()
        live._set_runtime_cookie_state.assert_not_called()

    async def test_refresh_cookies_from_qr_login_falls_back_to_account_persistent_profile_when_managed_context_missing(self):
        managed_runtime = mock.Mock()
        managed_runtime.close = mock.AsyncMock()

        orphan_page = mock.Mock()
        orphan_page.goto = mock.AsyncMock()
        orphan_page.reload = mock.AsyncMock()
        orphan_page.close = mock.AsyncMock()

        persistent_page = mock.Mock()
        persistent_page.goto = mock.AsyncMock()
        persistent_page.reload = mock.AsyncMock()
        persistent_page.close = mock.AsyncMock()

        persistent_context = mock.Mock()
        persistent_context.browser = mock.Mock()
        persistent_context.browser.close = mock.AsyncMock()
        persistent_context.add_cookies = mock.AsyncMock()
        persistent_context.new_page = mock.AsyncMock(return_value=persistent_page)
        persistent_context.cookies = mock.AsyncMock(
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
        persistent_context.close = mock.AsyncMock()
        managed_runtime.new_context = mock.AsyncMock(side_effect=AssertionError("should not create new anonymous context"))

        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "managed-page-without-context-test"
        live._legacy_cookie_id = "managed-page-without-context-test"
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
        lease = self._build_runtime_lease(live.account_id, browser=persistent_context.browser, context=persistent_context)
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(persistent_page, persistent_context)),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(return_value=os.path.join(os.getcwd(), "browser_data", "user_managed-page-without-context-test")),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("managed runtime path should not launch browser")),
                 create=True,
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
        runtime_manager.acquire_runtime.assert_awaited_once_with(
            "managed-page-without-context-test",
            "verification_recovery",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_request = runtime_manager.acquire_runtime.await_args.kwargs["runtime_request"]
        self.assertEqual(
            runtime_request["profile_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_managed-page-without-context-test"),
        )
        managed_runtime.new_context.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        orphan_page.goto.assert_not_awaited()
        orphan_page.reload.assert_not_awaited()
        orphan_page.close.assert_not_awaited()
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="qr_cookie_refresh_completed",
        )
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
        live.account_id = "managed-page-test"
        live._legacy_cookie_id = "managed-page-test"
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
        lease = self._build_runtime_lease("managed-page-test", browser=managed_runtime, context=context)
        lease.pages.append(managed_page)
        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(return_value=(managed_page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "_launch_browser_safe",
            new=mock.AsyncMock(side_effect=AssertionError("managed page path should not launch browser")),
            create=True,
        ) as launch_browser_safe, \
             mock.patch.object(
                 XianyuAutoAsync,
                 "account_browser_runtime_manager",
                 new=runtime_manager,
             ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime_lease=lease,
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
        runtime_manager.get_fresh_page.assert_not_awaited()
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="qr_cookie_refresh_completed",
        )
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_ignores_foreign_managed_context_even_when_lease_scope_matches(self):
        managed_runtime = mock.Mock()
        managed_runtime.close = mock.AsyncMock()

        foreign_page = mock.Mock()
        foreign_page.goto = mock.AsyncMock()
        foreign_page.reload = mock.AsyncMock()
        foreign_page.close = mock.AsyncMock()

        foreign_context = mock.Mock()
        foreign_context.add_cookies = mock.AsyncMock()
        foreign_context.new_page = mock.AsyncMock()
        foreign_context.cookies = mock.AsyncMock()

        safe_page = mock.Mock()
        safe_page.goto = mock.AsyncMock()
        safe_page.reload = mock.AsyncMock()
        safe_page.close = mock.AsyncMock()

        safe_context = mock.Mock()
        safe_context.browser = managed_runtime
        safe_context.add_cookies = mock.AsyncMock()
        safe_context.new_page = mock.AsyncMock(return_value=safe_page)
        safe_context.cookies = mock.AsyncMock(
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
        safe_context.close = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-foreign-managed-context"
        live.account_id = "managed-context-guard"
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
        lease = self._build_runtime_lease("managed-context-guard", browser=managed_runtime, context=safe_context)
        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(return_value=(safe_page, safe_context)),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "_launch_browser_safe",
            new=mock.AsyncMock(side_effect=AssertionError("managed runtime path should not launch browser")),
            create=True,
        ) as launch_browser_safe, \
             mock.patch.object(
                 XianyuAutoAsync,
                 "account_browser_runtime_manager",
                 new=runtime_manager,
             ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime_lease=lease,
                managed_runtime=managed_runtime,
                managed_context=foreign_context,
                managed_page=foreign_page,
            )

        self.assertTrue(result)
        launch_browser_safe.assert_not_awaited()
        foreign_context.add_cookies.assert_not_awaited()
        foreign_page.goto.assert_not_awaited()
        foreign_page.reload.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        safe_context.add_cookies.assert_awaited_once()
        safe_page.goto.assert_awaited_once_with(
            "https://www.goofish.com/im",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        safe_page.reload.assert_awaited_once_with(
            wait_until="domcontentloaded",
            timeout=12000,
        )
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="qr_cookie_refresh_completed",
        )
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_replaces_untracked_managed_page_with_fresh_lease_page(self):
        managed_runtime = mock.Mock()
        managed_runtime.close = mock.AsyncMock()

        untracked_page = mock.Mock()
        untracked_page.goto = mock.AsyncMock()
        untracked_page.reload = mock.AsyncMock()
        untracked_page.close = mock.AsyncMock()

        safe_page = mock.Mock()
        safe_page.goto = mock.AsyncMock()
        safe_page.reload = mock.AsyncMock()
        safe_page.close = mock.AsyncMock()

        context = mock.Mock()
        context.browser = managed_runtime
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=safe_page)
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
        live._legacy_cookie_id = "legacy-untracked-managed-page"
        live.account_id = "managed-page-guard"
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
        lease = self._build_runtime_lease("managed-page-guard", browser=managed_runtime, context=context)
        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(return_value=(safe_page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "_launch_browser_safe",
            new=mock.AsyncMock(side_effect=AssertionError("managed runtime path should not launch browser")),
            create=True,
        ) as launch_browser_safe, \
             mock.patch.object(
                 XianyuAutoAsync,
                 "account_browser_runtime_manager",
                 new=runtime_manager,
             ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime_lease=lease,
                managed_runtime=managed_runtime,
                managed_context=context,
                managed_page=untracked_page,
            )

        self.assertTrue(result)
        launch_browser_safe.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        context.add_cookies.assert_awaited_once()
        untracked_page.goto.assert_not_awaited()
        untracked_page.reload.assert_not_awaited()
        safe_page.goto.assert_awaited_once_with(
            "https://www.goofish.com/im",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        safe_page.reload.assert_awaited_once_with(
            wait_until="domcontentloaded",
            timeout=12000,
        )
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="qr_cookie_refresh_completed",
        )
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_replaces_managed_page_when_lease_pages_missing(self):
        managed_runtime = mock.Mock()
        managed_runtime.close = mock.AsyncMock()

        untracked_page = mock.Mock()
        untracked_page.goto = mock.AsyncMock()
        untracked_page.reload = mock.AsyncMock()
        untracked_page.close = mock.AsyncMock()

        safe_page = mock.Mock()
        safe_page.goto = mock.AsyncMock()
        safe_page.reload = mock.AsyncMock()
        safe_page.close = mock.AsyncMock()

        context = mock.Mock()
        context.browser = managed_runtime
        context.add_cookies = mock.AsyncMock()
        context.new_page = mock.AsyncMock(return_value=safe_page)
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
        live._legacy_cookie_id = "legacy-pages-missing-managed-page"
        live.account_id = "managed-page-pages-missing"
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
        lease = self._build_runtime_lease("managed-page-pages-missing", browser=managed_runtime, context=context)
        lease.pages = None
        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(return_value=(safe_page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "_launch_browser_safe",
            new=mock.AsyncMock(side_effect=AssertionError("managed runtime path should not launch browser")),
            create=True,
        ) as launch_browser_safe, \
             mock.patch.object(
                 XianyuAutoAsync,
                 "account_browser_runtime_manager",
                 new=runtime_manager,
             ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime_lease=lease,
                managed_runtime=managed_runtime,
                managed_context=context,
                managed_page=untracked_page,
            )

        self.assertTrue(result)
        launch_browser_safe.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        context.add_cookies.assert_awaited_once()
        untracked_page.goto.assert_not_awaited()
        untracked_page.reload.assert_not_awaited()
        safe_page.goto.assert_awaited_once_with(
            "https://www.goofish.com/im",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        safe_page.reload.assert_awaited_once_with(
            wait_until="domcontentloaded",
            timeout=12000,
        )
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="qr_cookie_refresh_completed",
        )
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_releases_broken_handoff_lease_before_reopening_account_runtime(self):
        broken_runtime = mock.Mock()
        broken_runtime.close = mock.AsyncMock()

        safe_page = mock.Mock()
        safe_page.goto = mock.AsyncMock()
        safe_page.reload = mock.AsyncMock()
        safe_page.close = mock.AsyncMock()

        safe_context = mock.Mock()
        safe_context.browser = mock.Mock()
        safe_context.add_cookies = mock.AsyncMock()
        safe_context.new_page = mock.AsyncMock(return_value=safe_page)
        safe_context.cookies = mock.AsyncMock(
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
        safe_context.close = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-broken-handoff-lease"
        live.account_id = "managed-handoff-account"
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
        broken_lease = self._build_runtime_lease(
            "managed-handoff-account",
            browser=broken_runtime,
            context=None,
        )
        safe_lease = self._build_runtime_lease(
            "managed-handoff-account",
            browser=safe_context.browser,
            context=safe_context,
        )
        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(return_value=(safe_page, safe_context)),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 live,
                 "_open_browser_recovery_context",
                 new=mock.AsyncMock(return_value=(safe_lease, safe_context.browser, safe_context, True)),
             ) as open_browser_recovery_context, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime_lease=broken_lease,
                managed_runtime=broken_runtime,
            )

        self.assertTrue(result)
        open_browser_recovery_context.assert_awaited_once()
        runtime_manager.get_fresh_page.assert_awaited_once_with(safe_lease)
        self.assertEqual(
            runtime_manager.release_runtime.await_args_list,
            [
                mock.call(
                    broken_lease,
                    reason="qr_cookie_refresh_invalid_handoff_lease",
                ),
                mock.call(
                    safe_lease,
                    reason="qr_cookie_refresh_completed",
                ),
            ],
        )
        safe_context.add_cookies.assert_awaited_once()
        safe_page.goto.assert_awaited_once_with(
            "https://www.goofish.com/im",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        safe_page.reload.assert_awaited_once_with(
            wait_until="domcontentloaded",
            timeout=12000,
        )
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_refresh_cookies_from_qr_login_releases_broken_handoff_lease_when_managed_context_is_detached(self):
        broken_runtime = mock.Mock()
        broken_runtime.close = mock.AsyncMock()

        detached_context = mock.Mock()
        detached_context.browser = broken_runtime
        detached_context.add_cookies = mock.AsyncMock(
            side_effect=AssertionError("detached managed_context should not receive cookie injection")
        )
        detached_context.new_page = mock.AsyncMock()
        detached_context.cookies = mock.AsyncMock()

        safe_page = mock.Mock()
        safe_page.goto = mock.AsyncMock()
        safe_page.reload = mock.AsyncMock()
        safe_page.close = mock.AsyncMock()

        safe_context = mock.Mock()
        safe_context.browser = mock.Mock()
        safe_context.add_cookies = mock.AsyncMock()
        safe_context.new_page = mock.AsyncMock(return_value=safe_page)
        safe_context.cookies = mock.AsyncMock(
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
        safe_context.close = mock.AsyncMock()

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-broken-handoff-detached"
        live.account_id = "managed-handoff-account"
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
        broken_lease = self._build_runtime_lease(
            "managed-handoff-account",
            browser=broken_runtime,
            context=None,
        )
        safe_lease = self._build_runtime_lease(
            "managed-handoff-account",
            browser=safe_context.browser,
            context=safe_context,
        )
        runtime_manager = types.SimpleNamespace(
            get_fresh_page=mock.AsyncMock(return_value=(safe_page, safe_context)),
            release_runtime=mock.AsyncMock(return_value=None),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 live,
                 "_open_browser_recovery_context",
                 new=mock.AsyncMock(return_value=(safe_lease, safe_context.browser, safe_context, True)),
             ) as open_browser_recovery_context, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()) as sleep_mock, \
             mock.patch("XianyuAutoAsync.db_manager.get_cookie_details", return_value={"cookie": qr_cookies_str}) as get_cookie_details, \
             mock.patch("XianyuAutoAsync.db_manager.update_cookie_account_info", return_value=True) as update_cookie_account_info:
            result = await live.refresh_cookies_from_qr_login(
                qr_cookies_str,
                managed_runtime_lease=broken_lease,
                managed_runtime=broken_runtime,
                managed_context=detached_context,
            )

        self.assertTrue(result)
        open_browser_recovery_context.assert_awaited_once()
        detached_context.add_cookies.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_awaited_once_with(safe_lease)
        self.assertEqual(
            runtime_manager.release_runtime.await_args_list,
            [
                mock.call(
                    broken_lease,
                    reason="qr_cookie_refresh_invalid_handoff_lease",
                ),
                mock.call(
                    safe_lease,
                    reason="qr_cookie_refresh_completed",
                ),
            ],
        )
        update_cookie_account_info.assert_called_once()
        get_cookie_details.assert_called()
        self.assertGreaterEqual(sleep_mock.await_count, 3)
        live._set_runtime_cookie_state.assert_called_once()

    async def test_fetch_item_detail_from_browser_uses_account_runtime_with_account_id_alias(self):
        browser = mock.Mock()
        context = mock.Mock()
        page = mock.Mock()
        detail_element = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        detail_element.inner_text = mock.AsyncMock(return_value="detail text")
        page.goto = mock.AsyncMock()
        page.wait_for_selector = mock.AsyncMock(return_value=True)
        page.query_selector = mock.AsyncMock(return_value=detail_element)
        lease = self._build_runtime_lease(
            "acc-item-detail-browser",
            browser=browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(return_value=os.path.join(os.getcwd(), "browser_data", "user_acc-item-detail-browser")),
        )

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-item-detail-browser"
        live.account_id = "acc-item-detail-browser"
        live.cookies_str = "unb=1; cookie2=2"
        live._safe_str = str
        live._build_browser_refresh_launch_args = mock.Mock(return_value=["--cloak-flag"])
        live._build_browser_refresh_context_options = mock.Mock(return_value={})
        live._async_close_browser = mock.AsyncMock()
        live._release_browser_recovery_runtime = XianyuLive._release_browser_recovery_runtime.__get__(live, XianyuLive)

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("should use managed runtime instead of launching browser directly")),
                 create=True,
             ) as launch_browser_safe, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            result = await live._fetch_item_detail_from_browser("123456")

        self.assertEqual(result, "detail text")
        runtime_manager.acquire_runtime.assert_awaited_once_with(
            "acc-item-detail-browser",
            "item_detail_fetch",
            exclusive=False,
            runtime_request=mock.ANY,
        )
        runtime_request = runtime_manager.acquire_runtime.await_args.kwargs["runtime_request"]
        self.assertEqual(
            runtime_request["profile_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_acc-item-detail-browser"),
        )
        self.assertTrue(runtime_request["use_persistent_context"])
        self.assertEqual(runtime_request["launch_options"]["args"], ["--cloak-flag"])
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        context.add_cookies.assert_not_awaited()
        launch_browser_safe.assert_not_awaited()
        live._build_browser_refresh_context_options.assert_called_once_with()
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="item_detail_fetch_completed",
        )
        live._async_close_browser.assert_not_awaited()
        page.goto.assert_awaited_once_with(
            "https://www.goofish.com/item?id=123456",
            wait_until="domcontentloaded",
            timeout=30000,
        )

    async def test_fetch_item_detail_from_browser_releases_runtime_when_navigation_fails(self):
        browser = mock.Mock()
        context = mock.Mock()
        page = mock.Mock()
        context.add_cookies = mock.AsyncMock()
        page.goto = mock.AsyncMock(side_effect=RuntimeError("nav failed"))
        lease = self._build_runtime_lease(
            "acc-item-detail-browser-fail",
            browser=browser,
            context=context,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(return_value=os.path.join(os.getcwd(), "browser_data", "user_acc-item-detail-browser-fail")),
        )

        live = XianyuLive.__new__(XianyuLive)
        live.account_id = "acc-item-detail-browser-fail"
        live._legacy_cookie_id = "acc-item-detail-browser-fail"
        live.cookies_str = "unb=1; cookie2=2"
        live._safe_str = str
        live._build_browser_refresh_launch_args = mock.Mock(return_value=["--cloak-flag"])
        live._build_browser_refresh_context_options = mock.Mock(return_value={})
        live._async_close_browser = mock.AsyncMock()
        live._release_browser_recovery_runtime = XianyuLive._release_browser_recovery_runtime.__get__(live, XianyuLive)

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ) as launch_browser_safe, \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            result = await live._fetch_item_detail_from_browser("123456")

        self.assertEqual(result, "")
        runtime_manager.acquire_runtime.assert_awaited_once()
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        context.add_cookies.assert_not_awaited()
        page.goto.assert_awaited_once_with(
            "https://www.goofish.com/item?id=123456",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="item_detail_fetch_failed",
        )
        live._async_close_browser.assert_not_awaited()

    async def test_fetch_item_detail_from_browser_releases_runtime_when_get_fresh_page_fails(self):
        browser = mock.Mock()
        lease = self._build_runtime_lease(
            "acc-item-detail-browser-page-fail",
            browser=browser,
            context=None,
        )
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(side_effect=RuntimeError("fresh page failed")),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(return_value=os.path.join(os.getcwd(), "browser_data", "user_acc-item-detail-browser-page-fail")),
        )

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-item-detail-browser-page-fail"
        live.account_id = "acc-item-detail-browser-page-fail"
        live.cookies_str = "unb=1; cookie2=2"
        live._safe_str = str
        live._build_browser_refresh_launch_args = mock.Mock(return_value=["--cloak-flag"])
        live._build_browser_refresh_context_options = mock.Mock(return_value={})
        live._async_close_browser = mock.AsyncMock()
        live._release_browser_recovery_runtime = XianyuLive._release_browser_recovery_runtime.__get__(live, XianyuLive)

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            result = await live._fetch_item_detail_from_browser("123456")

        self.assertEqual(result, "")
        runtime_manager.acquire_runtime.assert_awaited_once()
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="item_detail_fetch_failed",
        )
        live._build_browser_refresh_launch_args.assert_called_once_with()
        live._build_browser_refresh_context_options.assert_called_once_with()
        live._async_close_browser.assert_not_awaited()

    async def test_fetch_item_detail_from_browser_rejects_blank_account_id_without_runtime_fallback(self):
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(
                side_effect=AssertionError("should not acquire runtime without canonical account_id")
            ),
            get_fresh_page=mock.AsyncMock(),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                side_effect=AssertionError("should not resolve profile dir without canonical account_id")
            ),
        )

        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-item-detail-browser"
        live.account_id = "   "
        live.cookies_str = "unb=1; cookie2=2"
        live._safe_str = str
        live._build_browser_refresh_launch_args = mock.Mock(return_value=["--cloak-flag"])
        live._build_browser_refresh_context_options = mock.Mock(return_value={})
        live._async_close_browser = mock.AsyncMock()
        live._release_browser_recovery_runtime = XianyuLive._release_browser_recovery_runtime.__get__(live, XianyuLive)

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            result = await live._fetch_item_detail_from_browser("123456")

        self.assertEqual(result, "")
        runtime_manager.acquire_runtime.assert_not_awaited()
        runtime_manager.get_fresh_page.assert_not_awaited()
        runtime_manager.release_runtime.assert_not_awaited()
        runtime_manager.resolve_profile_dir.assert_not_called()
        live._build_browser_refresh_launch_args.assert_not_called()
        live._build_browser_refresh_context_options.assert_not_called()
        live._async_close_browser.assert_not_awaited()

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
        live.account_id = "recent-slider-user"
        live._legacy_cookie_id = "recent-slider-user"
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
        lease = self._build_runtime_lease(live.account_id, browser=context.browser, context=context)
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(return_value=os.path.join(os.getcwd(), "browser_data", "user_recent-slider-user")),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("should not launch clean browser")),
                 create=True,
             ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            success = await live._refresh_cookies_via_browser(triggered_by_refresh_token=False)

        self.assertTrue(success)
        runtime_manager.acquire_runtime.assert_awaited_once_with(
            "recent-slider-user",
            "cookie_refresh",
            exclusive=True,
            runtime_request=mock.ANY,
        )
        runtime_request = runtime_manager.acquire_runtime.await_args.kwargs["runtime_request"]
        self.assertEqual(
            runtime_request["profile_dir"],
            os.path.join(os.getcwd(), "browser_data", "user_recent-slider-user"),
        )
        self.assertTrue(runtime_request["launch_options"]["headless"])
        runtime_manager.get_fresh_page.assert_awaited_once_with(lease)
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="browser_cookie_refresh_completed",
        )
        live.update_config_cookies.assert_awaited_once_with()

    async def test_refresh_cookies_via_browser_rolls_back_runtime_cookie_state_when_db_update_fails(self):
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
        live.account_id = "browser-refresh-rollback-user"
        live._legacy_cookie_id = "browser-refresh-rollback-user"
        old_cookies_str = (
            "unb=user1; cookie2=old; _m_h5_tk=old_123; "
            "_m_h5_tk_enc=oldenc; sgcookie=oldsg; t=oldt"
        )
        old_cookies_dict = {
            "unb": "user1",
            "cookie2": "old",
            "_m_h5_tk": "old_123",
            "_m_h5_tk_enc": "oldenc",
            "sgcookie": "oldsg",
            "t": "oldt",
        }
        live.cookies_str = old_cookies_str
        live.cookies = dict(old_cookies_dict)
        live.last_qr_cookie_refresh_time = 0
        live.qr_cookie_refresh_cooldown = 0
        live._safe_str = str
        live.session = None
        live._summarize_cookie_string = lambda cookie_string: cookie_string
        live._log_protected_merge_event = mock.Mock()
        live._log_cookie_merge_summary = mock.Mock()
        real_set_runtime_cookie_state = XianyuLive._set_runtime_cookie_state.__get__(live, XianyuLive)
        live._set_runtime_cookie_state = mock.Mock(wraps=real_set_runtime_cookie_state)
        live.update_config_cookies = mock.AsyncMock(side_effect=RuntimeError("db write failed"))
        live._async_close_browser = mock.AsyncMock()
        live.protected_merge_cookie_dicts = lambda existing, incoming: self._build_merge_result(incoming)
        live.get_qr_login_grace = mock.Mock(return_value=None)
        live.get_manual_refresh_state = mock.Mock(return_value=None)
        live._has_recent_slider_success = mock.Mock(return_value=False)
        live.browser_cookie_refreshed = False
        lease = self._build_runtime_lease(live.account_id, browser=context.browser, context=context)
        runtime_manager = types.SimpleNamespace(
            acquire_runtime=mock.AsyncMock(return_value=lease),
            get_fresh_page=mock.AsyncMock(return_value=(page, context)),
            release_runtime=mock.AsyncMock(return_value=None),
            resolve_profile_dir=mock.Mock(
                return_value=os.path.join(os.getcwd(), "browser_data", "user_browser-refresh-rollback-user")
            ),
        )

        with mock.patch.object(
            XianyuAutoAsync,
            "account_browser_runtime_manager",
            new=runtime_manager,
        ), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "_launch_browser_safe",
                 new=mock.AsyncMock(side_effect=AssertionError("should not launch clean browser")),
                 create=True,
             ), \
             mock.patch("XianyuAutoAsync.asyncio.sleep", new=mock.AsyncMock()):
            success = await live._refresh_cookies_via_browser(triggered_by_refresh_token=False)

        self.assertFalse(success)
        self.assertEqual(live.cookies_str, old_cookies_str)
        self.assertEqual(live.cookies, old_cookies_dict)
        live._set_runtime_cookie_state.assert_has_calls(
            [
                mock.call(
                    cookies_dict={
                        "unb": "user1",
                        "cookie2": "cookie2v",
                        "_m_h5_tk": "tokenv_123",
                        "_m_h5_tk_enc": "encv",
                        "sgcookie": "sgv",
                        "t": "tv",
                        "cna": "cnav",
                    },
                    source="browser_cookie_refresh",
                ),
                mock.call(
                    cookies_str=old_cookies_str,
                    cookies_dict=old_cookies_dict,
                    source="browser_cookie_refresh_rollback",
                ),
            ]
        )
        runtime_manager.release_runtime.assert_awaited_once_with(
            lease,
            reason="browser_cookie_refresh_completed",
        )

    async def test_refresh_cookies_via_browser_rejects_default_or_blank_canonical_account_id_before_runtime(self):
        for invalid_account_id in ("default", "   "):
            with self.subTest(account_id=invalid_account_id):
                live = XianyuLive.__new__(XianyuLive)
                live._legacy_cookie_id = "legacy-browser-refresh-invalid"
                live.account_id = invalid_account_id
                live.cookies_str = "unb=user1; cookie2=old"
                live.cookies = {"unb": "user1", "cookie2": "old"}
                live.last_qr_cookie_refresh_time = 0
                live.qr_cookie_refresh_cooldown = 0
                live.browser_cookie_refreshed = False
                live._safe_str = str
                live.update_config_cookies = mock.AsyncMock()
                live._set_runtime_cookie_state = mock.Mock()
                live._build_browser_cookie_payload = mock.Mock(
                    side_effect=AssertionError("should not build browser cookie payload without canonical account_id")
                )
                live._release_browser_recovery_runtime = mock.AsyncMock()

                with mock.patch.object(
                    live,
                    "_open_browser_recovery_context",
                    new=mock.AsyncMock(
                        side_effect=AssertionError("should not request recovery context without canonical account_id")
                    ),
                ) as open_browser_recovery_context:
                    success = await live._refresh_cookies_via_browser(
                        triggered_by_refresh_token=False
                    )

                self.assertFalse(success)
                open_browser_recovery_context.assert_not_awaited()
                live._build_browser_cookie_payload.assert_not_called()
                live.update_config_cookies.assert_not_awaited()
                live._set_runtime_cookie_state.assert_not_called()
                live._release_browser_recovery_runtime.assert_not_awaited()

    async def test_refresh_cookies_via_browser_cooldown_logs_account_id_alias_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-browser-refresh-log-1"
        live.account_id = "account-browser-refresh-log-1"
        live.cookies_str = "unb=user1; cookie2=old"
        live.cookies = {"unb": "user1", "cookie2": "old"}
        live.last_qr_cookie_refresh_time = time.time()
        live.qr_cookie_refresh_cooldown = 60
        live._safe_str = str
        mock_logger = mock.Mock()

        with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            success = await live._refresh_cookies_via_browser(
                triggered_by_refresh_token=False
            )

        self.assertFalse(success)
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("account-browser-refresh-log-1", messages)
        self.assertNotIn("legacy-browser-refresh-log-1", messages)

    async def test_update_config_cookies_uses_current_account_id_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-update-1"
        live.account_id = "account-update-1"
        live.cookies_str = "unb=user1; cookie2=new-cookie2"
        live.user_id = 88
        live._safe_str = str

        with mock.patch.object(
            XianyuAutoAsync.db_manager,
            "update_cookie_account_info",
            return_value=True,
        ) as update_cookie_account_info:
            await live.update_config_cookies()

        update_cookie_account_info.assert_called_once_with(
            "account-update-1",
            cookie_value="unb=user1; cookie2=new-cookie2",
            user_id=88,
        )

    async def test_update_config_cookies_logs_account_id_alias_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-update-log-1"
        live.account_id = "account-update-log-1"
        live.cookies_str = "unb=user1; cookie2=new-cookie2"
        live.user_id = 88
        live._safe_str = str
        mock_logger = mock.Mock()

        with mock.patch.object(
            XianyuAutoAsync.db_manager,
            "update_cookie_account_info",
            return_value=True,
        ), mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live.update_config_cookies()

        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("account-update-log-1", messages)
        self.assertNotIn("legacy-cookie-update-log-1", messages)

    async def test_restart_instance_uses_current_account_id_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-restart-1"
        live.account_id = "account-restart-1"
        live.cookies_str = "unb=user1; cookie2=restarted-cookie2"
        live._safe_str = str

        cookie_manager_module = self._build_cookie_manager_module()
        cookie_manager_module.manager.update_cookie = mock.Mock()

        class _ImmediateThread:
            def __init__(self, target=None, daemon=None):
                self._target = target
                self.daemon = daemon

            def start(self):
                if self._target is not None:
                    self._target()

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch("threading.Thread", _ImmediateThread), \
             mock.patch("time.sleep", return_value=None):
            await live._restart_instance()

        cookie_manager_module.manager.update_cookie.assert_called_once_with(
            "account-restart-1",
            "unb=user1; cookie2=restarted-cookie2",
            save_to_db=False,
        )

    async def test_restart_instance_logs_account_id_alias_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-restart-log-1"
        live.account_id = "account-restart-log-1"
        live.cookies_str = "unb=user1; cookie2=restarted-cookie2"
        live._safe_str = str
        mock_logger = mock.Mock()

        cookie_manager_module = self._build_cookie_manager_module()
        cookie_manager_module.manager.update_cookie = mock.Mock()

        class _ImmediateThread:
            def __init__(self, target=None, daemon=None):
                self._target = target
                self.daemon = daemon

            def start(self):
                if self._target is not None:
                    self._target()

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch("threading.Thread", _ImmediateThread), \
             mock.patch("time.sleep", return_value=None), \
             mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live._restart_instance()

        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("account-restart-log-1", messages)
        self.assertNotIn("legacy-cookie-restart-log-1", messages)

    async def test_update_config_cookies_skips_stale_cookie_id_when_account_id_blank(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-update-blank"
        live.account_id = "   "
        live.cookies_str = "unb=user1; cookie2=new-cookie2"
        live.user_id = 99
        live._safe_str = str
        live.send_token_refresh_notification = mock.AsyncMock()

        with mock.patch.object(
            XianyuAutoAsync.db_manager,
            "update_cookie_account_info",
            return_value=True,
        ) as update_cookie_account_info:
            await live.update_config_cookies()

        update_cookie_account_info.assert_not_called()

    async def test_update_config_cookies_blank_account_id_message_avoids_cookie_id_wording(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-update-blank-log"
        live.account_id = "   "
        live.cookies_str = "unb=user1; cookie2=new-cookie2"
        live.user_id = 99
        live._safe_str = str
        live.send_token_refresh_notification = mock.AsyncMock()
        mock_logger = mock.Mock()

        with mock.patch.object(
            XianyuAutoAsync.db_manager,
            "update_cookie_account_info",
            return_value=True,
        ) as update_cookie_account_info, mock.patch.object(
            XianyuAutoAsync, "logger", mock_logger
        ):
            await live.update_config_cookies()

        update_cookie_account_info.assert_not_called()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("account_id", messages)
        self.assertNotIn("Cookie ID", messages)
        notification_message = live.send_token_refresh_notification.call_args.args[0]
        self.assertIn("account_id", notification_message)
        self.assertNotIn("Cookie ID", notification_message)

    async def test_restart_instance_skips_stale_cookie_id_when_account_id_blank(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-restart-blank"
        live.account_id = "   "
        live.cookies_str = "unb=user1; cookie2=restarted-cookie2"
        live._safe_str = str

        cookie_manager_module = self._build_cookie_manager_module()
        cookie_manager_module.manager.update_cookie = mock.Mock()

        class _ImmediateThread:
            def __init__(self, target=None, daemon=None):
                self._target = target
                self.daemon = daemon

            def start(self):
                if self._target is not None:
                    self._target()

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}), \
             mock.patch("threading.Thread", _ImmediateThread), \
             mock.patch("time.sleep", return_value=None):
            await live._restart_instance()

        cookie_manager_module.manager.update_cookie.assert_not_called()

    async def test_update_cookies_and_restart_logs_account_id_alias_instead_of_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-cookie-update-restart-log-1"
        live.account_id = "account-update-restart-log-1"
        live._safe_str = str
        mock_logger = mock.Mock()

        with mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            success = await live._update_cookies_and_restart("   ")

        self.assertFalse(success)
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("account-update-restart-log-1", messages)
        self.assertNotIn("legacy-cookie-update-restart-log-1", messages)

    async def test_release_browser_recovery_runtime_logs_account_identity_without_stale_cookie_id(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-release-runtime-log-1"
        live.account_id = "account-release-runtime-log-1"
        live._safe_str = str
        live._async_close_browser = mock.AsyncMock()
        runtime_lease = self._build_runtime_lease("account-release-runtime-log-1")
        mock_logger = mock.Mock()

        with mock.patch.object(
            XianyuAutoAsync.account_browser_runtime_manager,
            "release_runtime",
            new=mock.AsyncMock(side_effect=RuntimeError("boom")),
        ), mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live._release_browser_recovery_runtime(
                runtime_lease,
                browser=object(),
                context=object(),
                page=object(),
                reason="unit_test_release_failed",
            )

        live._async_close_browser.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("account-release-runtime-log-1", messages)
        self.assertNotIn("legacy-release-runtime-log-1", messages)

    async def test_release_browser_recovery_runtime_invalidates_cached_runtime_when_requested(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-release-runtime-invalidate-1"
        live.account_id = "account-release-runtime-invalidate-1"
        live._safe_str = str
        live._async_close_browser = mock.AsyncMock()
        runtime_lease = self._build_runtime_lease("account-release-runtime-invalidate-1")

        with mock.patch.object(
            XianyuAutoAsync.account_browser_runtime_manager,
            "release_runtime",
            new=mock.AsyncMock(return_value=None),
        ) as release_runtime, mock.patch.object(
            XianyuAutoAsync.account_browser_runtime_manager,
            "invalidate_runtime",
            new=mock.AsyncMock(return_value=True),
        ) as invalidate_runtime:
            await live._release_browser_recovery_runtime(
                runtime_lease,
                browser=object(),
                context=object(),
                page=object(),
                reason="unit_test_release_invalidate",
                invalidate_after_release=True,
            )

        release_runtime.assert_awaited_once_with(
            runtime_lease,
            reason="unit_test_release_invalidate",
        )
        invalidate_runtime.assert_awaited_once_with(
            "account-release-runtime-invalidate-1",
            reason="unit_test_release_invalidate_post_release_invalidate",
        )
        live._async_close_browser.assert_not_awaited()

    async def test_release_browser_recovery_runtime_missing_lease_account_id_avoids_stale_cookie_id_fallback(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-release-runtime-log-blank"
        live.account_id = "   "
        live._safe_str = str
        live._async_close_browser = mock.AsyncMock()
        runtime_lease = types.SimpleNamespace()
        mock_logger = mock.Mock()

        with mock.patch.object(
            XianyuAutoAsync.account_browser_runtime_manager,
            "release_runtime",
            new=mock.AsyncMock(side_effect=RuntimeError("boom")),
        ), mock.patch.object(XianyuAutoAsync, "logger", mock_logger):
            await live._release_browser_recovery_runtime(
                runtime_lease,
                browser=object(),
                context=object(),
                page=object(),
                reason="unit_test_release_failed",
            )

        live._async_close_browser.assert_not_awaited()
        messages = self._collect_logger_messages(mock_logger)
        self.assertIn("default", messages)
        self.assertNotIn("legacy-release-runtime-log-blank", messages)

    async def test_release_browser_recovery_runtime_closes_local_resources_without_managed_lease(self):
        live = XianyuLive.__new__(XianyuLive)
        live._legacy_cookie_id = "legacy-release-runtime-local-1"
        live.account_id = "account-release-runtime-local-1"
        live._safe_str = str
        live._async_close_browser = mock.AsyncMock()

        await live._release_browser_recovery_runtime(
            None,
            browser=object(),
            context=object(),
            page=object(),
            reason="unit_test_local_cleanup",
        )

        live._async_close_browser.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
