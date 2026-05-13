import asyncio
import sys
import threading
import types
import unittest
from unittest import mock

_STUBBED_MODULE_NAMES = (
    "loguru",
    "db_manager",
    "aiohttp",
    "utils.order_detail_fetcher",
    "utils.xianyu_utils",
)
_IMPORTED_MODULE_NAMES = (
    "ai_reply_engine",
    "order_event_hub",
    "order_status_handler",
    "utils.order_history_sync",
)
_MODULE_SNAPSHOT = {
    name: sys.modules.get(name)
    for name in _STUBBED_MODULE_NAMES + _IMPORTED_MODULE_NAMES
}


def _restore_module_snapshot():
    for name in _IMPORTED_MODULE_NAMES + _STUBBED_MODULE_NAMES:
        original = _MODULE_SNAPSHOT.get(name)
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


class _LoggerStub:
    def add(self, *_args, **_kwargs):
        return 1

    def remove(self, *_args, **_kwargs):
        return None

    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None

    def debug(self, *_args, **_kwargs):
        return None


class _PreImportOrderDetailFetcher:
    def __init__(self, cookie_string, **_kwargs):
        self.cookie = cookie_string

    async def close(self):
        return None


async def _preimport_fetch_order_detail_simple(*_args, **_kwargs):
    raise AssertionError("fetch_order_detail_simple should be patched in tests")


if "loguru" not in sys.modules:
    sys.modules["loguru"] = types.SimpleNamespace(logger=_LoggerStub())

if "db_manager" not in sys.modules:
    db_manager_stub = types.ModuleType("db_manager")
    db_manager_stub.db_manager = mock.Mock()
    sys.modules["db_manager"] = db_manager_stub

if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.SimpleNamespace(
        ClientSession=object,
        ClientTimeout=lambda **_kwargs: None,
    )

if "utils.order_detail_fetcher" not in sys.modules:
    order_detail_fetcher_stub = types.ModuleType("utils.order_detail_fetcher")
    order_detail_fetcher_stub.OrderDetailFetcher = _PreImportOrderDetailFetcher
    order_detail_fetcher_stub.fetch_order_detail_simple = _preimport_fetch_order_detail_simple
    sys.modules["utils.order_detail_fetcher"] = order_detail_fetcher_stub

if "utils.xianyu_utils" not in sys.modules:
    sys.modules["utils.xianyu_utils"] = types.SimpleNamespace(
        decrypt=lambda value, *_args, **_kwargs: value,
        generate_mid=lambda *_args, **_kwargs: "mid",
        generate_sign=lambda *_args, **_kwargs: "sign",
        generate_uuid=lambda *_args, **_kwargs: "uuid",
        generate_device_id=lambda *_args, **_kwargs: "device-id",
        trans_cookies=lambda cookie_string: {
            item.split("=", 1)[0].strip(): item.split("=", 1)[1].strip()
            for item in str(cookie_string or "").split(";")
            if "=" in item
        },
    )

import ai_reply_engine
import order_event_hub
import order_status_handler
from utils import order_history_sync

# unittest discover imports every test module before executing any tests. Restore
# import-time stubs immediately so later modules see the real project modules.
_restore_module_snapshot()


def tearDownModule():
    _restore_module_snapshot()


class _FakeAICursor:
    def __init__(self):
        self.executions = []

    def execute(self, sql, params=()):
        self.executions.append((sql, params))
        return self

    def fetchone(self):
        return ("2024-01-01 00:00:00",)

    def fetchall(self):
        return []


class _FakeAIConnection:
    def __init__(self):
        self.cursor_instance = _FakeAICursor()

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        return None


class _FakeAIDBManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.conn = _FakeAIConnection()
        self.requested_account_id = None

    def get_ai_reply_settings(self, account_id):
        self.requested_account_id = account_id
        return {"ai_enabled": True, "custom_prompts": None}


class _FakeOrderEventDBManager:
    def __init__(self, order, user_id=7):
        self.order = dict(order)
        self.user_id = user_id
        self.requested_account_id = None

    def get_order_by_id(self, order_id, account_id=None, user_id=None):
        if self.order.get("order_id") != order_id:
            return None
        if account_id is not None and self.order.get("account_id") != account_id:
            return None
        if user_id is not None and user_id != self.user_id:
            return None
        return dict(self.order)

    def get_cookie_details(self, account_id):
        self.requested_account_id = account_id
        return {"user_id": self.user_id}


class _FakeOrderStatusDBManager:
    def __init__(self, order):
        self.order = dict(order)
        self.insert_calls = []
        self.get_order_calls = []
        self.get_pre_refund_calls = []

    def get_order_by_id(self, order_id, account_id=None, user_id=None):
        self.get_order_calls.append(
            {"order_id": order_id, "account_id": account_id, "user_id": user_id}
        )
        if self.order.get("order_id") != order_id:
            return None
        if account_id is not None and self.order.get("account_id") != account_id:
            return None
        return dict(self.order)

    def insert_or_update_order(
        self,
        order_id,
        order_status=None,
        account_id=None,
        cookie_id=None,
        pre_refund_status=...,
        clear_pre_refund_status=False,
        **_kwargs,
    ):
        self.insert_calls.append(
            {
                "order_id": order_id,
                "order_status": order_status,
                "account_id": account_id,
                "cookie_id": cookie_id,
                "pre_refund_status": pre_refund_status,
                "clear_pre_refund_status": clear_pre_refund_status,
            }
        )
        if self.order.get("order_id") != order_id:
            return False
        if order_status is not None:
            self.order["order_status"] = order_status
        if account_id is not None:
            self.order["account_id"] = account_id
        if cookie_id is not None:
            self.order["cookie_id"] = cookie_id
        return True

    def get_order_pre_refund_status(self, order_id, account_id=None):
        self.get_pre_refund_calls.append(
            {"order_id": order_id, "account_id": account_id}
        )
        if self.order.get("order_id") != order_id:
            return None
        if account_id is not None and self.order.get("account_id") != account_id:
            return None
        return self.order.get("pre_refund_status")

    def get_cookie_details(self, account_id):
        resolved_account_id = self.order.get("account_id") or self.order.get("cookie_id")
        if resolved_account_id != account_id:
            return None
        return {"user_id": 1}


class _FakeHistoryDBManager:
    def __init__(self):
        self.calls = []

    def update_cookie_account_info(self, account_id, cookie_value=None, **_kwargs):
        self.calls.append((account_id, cookie_value))
        return True


class _StubOrderDetailFetcher:
    def __init__(self, cookie_string, **_kwargs):
        self.cookie = cookie_string

    async def close(self):
        return None


class AccountIdMigrationBatchTest(unittest.TestCase):
    def test_ai_reply_engine_accepts_account_id_and_uses_account_id_db_column(self):
        fake_db = _FakeAIDBManager()
        engine = ai_reply_engine.AIReplyEngine()

        with mock.patch.object(ai_reply_engine, "db_manager", fake_db):
            self.assertTrue(engine.is_ai_enabled(account_id="acc-ai-1"))
            created_at = engine.save_conversation(
                chat_id="chat-1",
                account_id="acc-ai-1",
                user_id="user-1",
                item_id="item-1",
                role="user",
                content="hello",
            )

        self.assertEqual(fake_db.requested_account_id, "acc-ai-1")
        self.assertEqual(created_at, "2024-01-01 00:00:00")
        insert_sql, insert_params = fake_db.conn.cursor_instance.executions[0]
        self.assertIn("(account_id, chat_id, user_id, item_id, role, content, intent)", insert_sql)
        self.assertNotIn("cookie_id", insert_sql)
        self.assertEqual(insert_params[0], "acc-ai-1")

    def test_publish_order_update_event_supports_account_id_order_field(self):
        fake_db = _FakeOrderEventDBManager(
            {
                "order_id": "order-1",
                "account_id": "acc-event-1",
                "title": "demo",
            }
        )
        published = {}

        def _capture_publish(user_id, event):
            published["user_id"] = user_id
            published["event"] = event

        with mock.patch("db_manager.db_manager", fake_db):
            with mock.patch.object(order_event_hub.order_event_hub, "publish", side_effect=_capture_publish):
                event = order_event_hub.publish_order_update_event(
                    "order-1",
                    account_id="acc-event-1",
                    source="unit-test",
                )

        self.assertIsNotNone(event)
        self.assertEqual(fake_db.requested_account_id, "acc-event-1")
        self.assertEqual(event["order"]["account_id"], "acc-event-1")
        self.assertEqual(event["source"], "unit-test")
        self.assertEqual(published["user_id"], 7)

    def test_order_status_handler_uses_account_id_in_pending_updates_and_db_boundary(self):
        handler = order_status_handler.OrderStatusHandler()
        fake_db = _FakeOrderStatusDBManager(
            {
                "order_id": "order-2",
                "order_status": "processing",
                "account_id": "acc-status-1",
            }
        )

        with mock.patch("db_manager.db_manager", fake_db):
            result = handler.update_order_status(
                order_id="order-2",
                new_status="pending_ship",
                account_id="acc-status-1",
                context="unit test account_id",
            )

        self.assertTrue(result)
        self.assertEqual(fake_db.get_order_calls[0]["account_id"], "acc-status-1")
        self.assertEqual(fake_db.insert_calls[0]["account_id"], "acc-status-1")
        self.assertIsNone(fake_db.insert_calls[0]["cookie_id"])

        forwarded_calls = []

        def _fake_update_order_status(**kwargs):
            forwarded_calls.append(kwargs)
            return True

        handler.update_order_status = _fake_update_order_status
        handler._add_to_pending_updates(
            order_id="order-pending-1",
            new_status="shipped",
            account_id="acc-status-2",
            context="pending context",
        )
        pending_entry = next(iter(handler.pending_updates.values()))[0]
        self.assertEqual(pending_entry["account_id"], "acc-status-2")
        self.assertNotIn("cookie_id", pending_entry)

        processed = handler.process_pending_updates(
            "order-pending-1",
            account_id="acc-status-2",
        )
        self.assertTrue(processed)
        self.assertEqual(forwarded_calls[0]["account_id"], "acc-status-2")
        self.assertNotIn("cookie_id", forwarded_calls[0])

    def test_order_history_page_fetcher_accepts_account_id(self):
        fake_db = _FakeHistoryDBManager()

        with mock.patch("db_manager.db_manager", fake_db):
            with mock.patch.object(order_history_sync, "OrderDetailFetcher", _StubOrderDetailFetcher):
                fetcher = order_history_sync.OrderHistoryPageFetcher(
                    "a=1; b=2",
                    account_id="acc-history-1",
                    headless=True,
                )
                asyncio.run(fetcher._persist_cookie_update())

        self.assertEqual(fetcher.account_id, "acc-history-1")
        self.assertEqual(fake_db.calls, [("acc-history-1", "a=1; b=2")])


if __name__ == "__main__":
    unittest.main()
