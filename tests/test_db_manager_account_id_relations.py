import os
import inspect
import tempfile
import unittest
import ast
from pathlib import Path


_BOOTSTRAP_DIR = tempfile.mkdtemp(prefix="db_manager_account_id_bootstrap_")
os.environ.setdefault("DB_PATH", os.path.join(_BOOTSTRAP_DIR, "bootstrap.db"))

import db_manager as db_module


class DBManagerAccountIdRelationsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = db_module.DBManager(os.path.join(self.temp_dir.name, "test.db"))

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def _columns(self, table_name):
        cursor = self.db.conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        return [row[1] for row in cursor.fetchall()]

    def _save_account(self, account_id):
        self.assertTrue(self.db.save_cookie(account_id, "a=1; b=2", user_id=1))

    def test_orders_item_tables_and_risk_logs_use_account_id_columns(self):
        for table_name in (
            "orders",
            "item_info",
            "item_replay",
            "risk_control_logs",
            "ai_conversations",
            "ai_reply_settings",
            "comment_templates",
            "cookie_status",
            "default_replies",
            "default_reply_records",
            "keywords",
            "message_notifications",
        ):
            columns = self._columns(table_name)
            self.assertIn("account_id", columns, table_name)
            self.assertNotIn("cookie_id", columns, table_name)

    def test_db_manager_has_no_duplicate_method_definitions(self):
        source = Path("db_manager.py").read_text(encoding="utf-8")
        module = ast.parse(source)

        duplicate_methods = {}
        for node in module.body:
            if not isinstance(node, ast.ClassDef) or node.name != "DBManager":
                continue
            seen = {}
            for item in node.body:
                if not isinstance(item, ast.FunctionDef):
                    continue
                seen.setdefault(item.name, []).append(item.lineno)
            duplicate_methods = {
                name: lines for name, lines in seen.items() if len(lines) > 1
            }
            break

        self.assertEqual(duplicate_methods, {})

    def test_db_manager_source_has_no_cookie_id_residue(self):
        source = Path("db_manager.py").read_text(encoding="utf-8")
        self.assertNotIn("cookie_id", source)

    def test_delivery_tables_use_account_id_columns(self):
        for table_name in ("delivery_logs", "delivery_finalization_states", "data_card_reservations"):
            columns = self._columns(table_name)
            self.assertIn("account_id", columns, table_name)
            self.assertNotIn("cookie_id", columns, table_name)

    def test_order_crud_uses_account_id_boundary(self):
        self._save_account("acc-order-1")

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-1",
                item_id="item-1",
                buyer_id="buyer-1",
                buyer_nick="buyer",
                account_id="acc-order-1",
                order_status="processing",
            )
        )

        order = self.db.get_order_by_id("order-1", account_id="acc-order-1")
        self.assertEqual(order["account_id"], "acc-order-1")
        self.assertNotIn("cookie_id", order)
        self.assertEqual(
            self.db.get_order_by_id("order-1", account_id="acc-order-1", user_id=1)["account_id"],
            "acc-order-1",
        )
        self.assertIsNone(self.db.get_order_by_id("order-1", account_id="acc-order-2"))

        orders = self.db.get_orders_by_account("acc-order-1", limit=10)
        self.assertEqual([row["order_id"] for row in orders], ["order-1"])
        self.assertEqual(orders[0]["account_id"], "acc-order-1")
        self.assertNotIn("cookie_id", orders[0])

        recent_order = self.db.get_recent_order_by_buyer_id("buyer-1", account_id="acc-order-1", minutes=60)
        self.assertEqual(recent_order["account_id"], "acc-order-1")
        self.assertNotIn("cookie_id", recent_order)

        with self.assertRaises(TypeError):
            self.db.delete_order("order-1")
        self.assertTrue(self.db.delete_order("order-1", account_id="acc-order-1"))
        self.assertIsNone(self.db.get_order_by_id("order-1", account_id="acc-order-1"))

    def test_get_orders_by_account_rejects_blank_account_id(self):
        with self.assertRaises(ValueError):
            self.db.get_orders_by_account(None)
        with self.assertRaises(ValueError):
            self.db.get_orders_by_account("   ")

    def test_get_order_by_id_rejects_unscoped_lookup(self):
        self._save_account("acc-order-scope-1")

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-scope-1",
                item_id="item-scope-1",
                buyer_id="buyer-scope-1",
                buyer_nick="buyer-scope",
                account_id="acc-order-scope-1",
                order_status="pending_ship",
            )
        )

        with self.assertRaises(TypeError):
            self.db.get_order_by_id("order-scope-1")
        with self.assertRaises(TypeError):
            self.db.get_order_by_id("order-scope-1", user_id=1)

    def test_get_order_pre_refund_status_uses_account_id_boundary(self):
        self._save_account("acc-pre-1")

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-pre-1",
                item_id="item-pre-1",
                buyer_id="buyer-pre-1",
                buyer_nick="buyer-pre",
                account_id="acc-pre-1",
                order_status="refunding",
                pre_refund_status="pending_ship",
            )
        )

        self.assertEqual(
            self.db.get_order_pre_refund_status("order-pre-1", account_id="acc-pre-1"),
            "pending_ship",
        )
        with self.assertRaises(TypeError):
            self.db.get_order_pre_refund_status("order-pre-1")
        self.assertIsNone(
            self.db.get_order_pre_refund_status("order-pre-1", account_id="acc-pre-2")
        )

    def test_get_order_info_requires_account_id_scope(self):
        self._save_account("acc-info-1")

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-info-1",
                item_id="item-info-1",
                buyer_id="buyer-info-1",
                buyer_nick="buyer-info",
                account_id="acc-info-1",
                order_status="processing",
            )
        )
        self.assertTrue(
            self.db.update_order_yifan_status(
                "order-info-1",
                account_id="acc-info-1",
                yifan_orderno="yf-info-1",
                delivery_status="processing",
                callback_data='{"ok": true}',
            )
        )
        self.assertTrue(
            self.db.update_order_chat_id(
                "order-info-1",
                "chat-info-1",
                account_id="acc-info-1",
            )
        )

        with self.assertRaises(TypeError):
            self.db.get_order_info("order-info-1")
        self.assertIsNone(
            self.db.get_order_info("order-info-1", account_id="acc-info-2")
        )

        order_info = self.db.get_order_info("order-info-1", account_id="acc-info-1")
        self.assertEqual(order_info["account_id"], "acc-info-1")
        self.assertEqual(order_info["yifan_orderno"], "yf-info-1")
        self.assertEqual(order_info["chat_id"], "chat-info-1")

    def test_get_order_by_yifan_orderno_requires_account_id_scope(self):
        self._save_account("acc-yf-scope-1")

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-yf-scope-1",
                item_id="item-yf-scope-1",
                buyer_id="buyer-yf-scope-1",
                buyer_nick="buyer-yf-scope",
                account_id="acc-yf-scope-1",
                order_status="processing",
            )
        )
        self.assertTrue(
            self.db.update_order_yifan_status(
                "order-yf-scope-1",
                account_id="acc-yf-scope-1",
                yifan_orderno="yf-scope-1",
                delivery_status="processing",
            )
        )

        with self.assertRaises(TypeError):
            self.db.get_order_by_yifan_orderno("yf-scope-1")
        self.assertIsNone(
            self.db.get_order_by_yifan_orderno(
                "yf-scope-1",
                account_id="acc-yf-scope-2",
            )
        )

        order_info = self.db.get_order_by_yifan_orderno(
            "yf-scope-1",
            account_id="acc-yf-scope-1",
        )
        self.assertEqual(order_info["order_id"], "order-yf-scope-1")
        self.assertEqual(order_info["account_id"], "acc-yf-scope-1")

    def test_insert_or_update_order_requires_account_id_scope(self):
        self._save_account("acc-insert-scope-1")

        with self.assertRaises(TypeError):
            self.db.insert_or_update_order(
                order_id="order-missing-account-1",
                item_id="item-missing-account-1",
                buyer_id="buyer-missing-account-1",
                buyer_nick="buyer-missing-account",
                order_status="processing",
            )
        with self.assertRaises(ValueError):
            self.db.insert_or_update_order(
                order_id="order-blank-account-1",
                item_id="item-blank-account-1",
                buyer_id="buyer-blank-account-1",
                buyer_nick="buyer-blank-account",
                account_id="   ",
                order_status="processing",
            )
        self.assertIsNone(
            self.db.get_order_by_id(
                "order-missing-account-1",
                account_id="acc-insert-scope-1",
            )
        )
        self.assertIsNone(
            self.db.get_order_by_id(
                "order-blank-account-1",
                account_id="acc-insert-scope-1",
            )
        )

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-update-scope-1",
                item_id="item-update-scope-1",
                buyer_id="buyer-update-scope-1",
                buyer_nick="buyer-update-scope",
                account_id="acc-insert-scope-1",
                order_status="processing",
            )
        )

        with self.assertRaises(TypeError):
            self.db.insert_or_update_order(
                order_id="order-update-scope-1",
                order_status="shipped",
            )

        kept_order = self.db.get_order_by_id(
            "order-update-scope-1",
            account_id="acc-insert-scope-1",
        )
        self.assertEqual(kept_order["order_status"], "processing")

    def test_insert_or_update_order_rejects_cross_account_override(self):
        self._save_account("acc-order-owner-1")
        self._save_account("acc-order-owner-2")

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-owner-1",
                item_id="item-owner-1",
                buyer_id="buyer-owner-1",
                buyer_nick="buyer-owner-1",
                account_id="acc-order-owner-1",
                order_status="processing",
            )
        )

        self.assertFalse(
            self.db.insert_or_update_order(
                order_id="order-owner-1",
                item_id="item-owner-2",
                buyer_id="buyer-owner-2",
                buyer_nick="buyer-owner-2",
                account_id="acc-order-owner-2",
                order_status="shipped",
            )
        )

        kept_order = self.db.get_order_by_id(
            "order-owner-1",
            account_id="acc-order-owner-1",
        )
        self.assertEqual(kept_order["account_id"], "acc-order-owner-1")
        self.assertEqual(kept_order["item_id"], "item-owner-1")
        self.assertEqual(kept_order["buyer_id"], "buyer-owner-1")
        self.assertEqual(kept_order["order_status"], "processing")
        self.assertIsNone(
            self.db.get_order_by_id(
                "order-owner-1",
                account_id="acc-order-owner-2",
            )
        )

    def test_insert_or_update_order_rejects_claiming_legacy_unscoped_order_row(self):
        self._save_account("acc-order-legacy-1")
        self._save_account("acc-order-legacy-2")

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-legacy-unscoped-1",
                item_id="item-legacy-original",
                buyer_id="buyer-legacy-original",
                buyer_nick="buyer-legacy-original",
                account_id="acc-order-legacy-1",
                order_status="processing",
            )
        )

        for legacy_account_id in ("", None):
            with self.subTest(legacy_account_id=legacy_account_id):
                cursor = self.db.conn.cursor()
                cursor.execute(
                    """
                    UPDATE orders
                    SET account_id = ?, item_id = ?, buyer_id = ?, buyer_nick = ?, order_status = ?
                    WHERE order_id = ?
                    """,
                    (
                        legacy_account_id,
                        "item-legacy-original",
                        "buyer-legacy-original",
                        "buyer-legacy-original",
                        "processing",
                        "order-legacy-unscoped-1",
                    ),
                )
                self.db.conn.commit()

                self.assertFalse(
                    self.db.insert_or_update_order(
                        order_id="order-legacy-unscoped-1",
                        item_id="item-legacy-claimed",
                        buyer_id="buyer-legacy-claimed",
                        buyer_nick="buyer-legacy-claimed",
                        account_id="acc-order-legacy-2",
                        order_status="shipped",
                    )
                )

                row = self.db.conn.execute(
                    """
                    SELECT account_id, item_id, buyer_id, buyer_nick, order_status
                    FROM orders
                    WHERE order_id = ?
                    """,
                    ("order-legacy-unscoped-1",),
                ).fetchone()
                self.assertEqual(row[0], legacy_account_id)
                self.assertEqual(row[1], "item-legacy-original")
                self.assertEqual(row[2], "buyer-legacy-original")
                self.assertEqual(row[3], "buyer-legacy-original")
                self.assertEqual(row[4], "processing")
                self.assertIsNone(
                    self.db.get_order_by_id(
                        "order-legacy-unscoped-1",
                        account_id="acc-order-legacy-2",
                    )
                )

    def test_delete_order_rejects_mismatched_account_id(self):
        self._save_account("acc-order-keep-1")

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-keep-1",
                item_id="item-keep-1",
                buyer_id="buyer-keep-1",
                buyer_nick="buyer-keep",
                account_id="acc-order-keep-1",
                order_status="pending_ship",
            )
        )

        self.assertFalse(
            self.db.delete_order("order-keep-1", account_id="acc-order-keep-2")
        )
        self.assertFalse(self.db.conn.in_transaction)
        kept_order = self.db.get_order_by_id("order-keep-1", account_id="acc-order-keep-1")
        self.assertIsNotNone(kept_order)
        self.assertEqual(kept_order["account_id"], "acc-order-keep-1")

    def test_order_yifan_updates_require_account_id_boundary(self):
        self._save_account("acc-yifan-1")

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-yifan-1",
                item_id="item-yifan-1",
                buyer_id="buyer-yifan-1",
                buyer_nick="buyer-yifan",
                account_id="acc-yifan-1",
                order_status="processing",
            )
        )

        with self.assertRaises(TypeError):
            self.db.update_order_yifan_status(
                "order-yifan-1",
                yifan_orderno="yf-order-1",
                delivery_status="processing",
            )
        with self.assertRaises(TypeError):
            self.db.update_order_chat_id(
                "order-yifan-1",
                "chat-yifan-1",
            )
        self.assertFalse(
            self.db.update_order_yifan_status(
                "order-yifan-1",
                account_id="acc-yifan-2",
                yifan_orderno="yf-order-1",
                delivery_status="processing",
            )
        )
        self.assertFalse(self.db.conn.in_transaction)
        self.assertFalse(
            self.db.update_order_chat_id(
                "order-yifan-1",
                "chat-yifan-1",
                account_id="acc-yifan-2",
            )
        )
        self.assertFalse(self.db.conn.in_transaction)

        self.assertTrue(
            self.db.update_order_yifan_status(
                "order-yifan-1",
                account_id="acc-yifan-1",
                yifan_orderno="yf-order-1",
                delivery_status="processing",
            )
        )
        self.assertTrue(
            self.db.update_order_chat_id(
                "order-yifan-1",
                "chat-yifan-1",
                account_id="acc-yifan-1",
            )
        )

        cursor = self.db.conn.cursor()
        cursor.execute(
            "SELECT yifan_orderno, delivery_status, chat_id FROM orders WHERE order_id = ? AND account_id = ?",
            ("order-yifan-1", "acc-yifan-1"),
        )
        row = cursor.fetchone()
        self.assertEqual(row, ("yf-order-1", "processing", "chat-yifan-1"))

    def test_order_lookup_helpers_reject_unscoped_access(self):
        self._save_account("acc-order-helper-1")
        self._save_account("acc-order-helper-2")

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-helper-1",
                item_id="item-helper-1",
                buyer_id="buyer-helper-1",
                buyer_nick="buyer-helper-old-1",
                sid="sid-helper-1@goofish",
                account_id="acc-order-helper-1",
                order_status="pending_ship",
            )
        )
        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-helper-2",
                item_id="item-helper-2",
                buyer_id="buyer-helper-1",
                buyer_nick="buyer-helper-old-2",
                sid="sid-helper-1@goofish",
                account_id="acc-order-helper-2",
                order_status="pending_ship",
            )
        )

        with self.assertRaises(TypeError):
            self.db.get_recent_order_by_buyer_id("buyer-helper-1", minutes=60)
        with self.assertRaises(TypeError):
            self.db.get_recent_order_by_sid("sid-helper-1@goofish", minutes=60)
        with self.assertRaises(TypeError):
            self.db.find_recent_orders_by_match_context(
                sid="sid-helper-1@goofish",
                buyer_id="buyer-helper-1",
                minutes=60,
                limit=10,
            )

        scoped_buyer_order = self.db.get_recent_order_by_buyer_id(
            "buyer-helper-1",
            account_id="acc-order-helper-1",
            minutes=60,
        )
        self.assertEqual(scoped_buyer_order["order_id"], "order-helper-1")
        self.assertEqual(scoped_buyer_order["account_id"], "acc-order-helper-1")

        scoped_sid_order = self.db.get_recent_order_by_sid(
            "sid-helper-1@goofish",
            account_id="acc-order-helper-1",
            minutes=60,
        )
        self.assertEqual(scoped_sid_order["order_id"], "order-helper-1")
        self.assertEqual(scoped_sid_order["account_id"], "acc-order-helper-1")

        scoped_matches = self.db.find_recent_orders_by_match_context(
            sid="sid-helper-1@goofish",
            buyer_id="buyer-helper-1",
            account_id="acc-order-helper-1",
            minutes=60,
            limit=10,
        )
        self.assertEqual([row["order_id"] for row in scoped_matches], ["order-helper-1"])
        self.assertEqual(scoped_matches[0]["account_id"], "acc-order-helper-1")

    def test_update_buyer_nick_requires_account_id_scope(self):
        self._save_account("acc-order-nick-1")
        self._save_account("acc-order-nick-2")

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-nick-1",
                item_id="item-nick-1",
                buyer_id="buyer-nick-1",
                buyer_nick="buyer-nick-old-1",
                account_id="acc-order-nick-1",
                order_status="processing",
            )
        )
        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-nick-2",
                item_id="item-nick-2",
                buyer_id="buyer-nick-1",
                buyer_nick="buyer-nick-old-2",
                account_id="acc-order-nick-2",
                order_status="processing",
            )
        )

        with self.assertRaises(TypeError):
            self.db.update_buyer_nick_by_buyer_id("buyer-nick-1", "buyer-nick-new")

        order_one = self.db.get_order_by_id("order-nick-1", account_id="acc-order-nick-1")
        order_two = self.db.get_order_by_id("order-nick-2", account_id="acc-order-nick-2")
        self.assertEqual(order_one["buyer_nick"], "buyer-nick-old-1")
        self.assertEqual(order_two["buyer_nick"], "buyer-nick-old-2")

        self.assertEqual(
            self.db.update_buyer_nick_by_buyer_id(
                "buyer-nick-1",
                "buyer-nick-new-1",
                account_id="acc-order-nick-1",
            ),
            1,
        )

        order_one = self.db.get_order_by_id("order-nick-1", account_id="acc-order-nick-1")
        order_two = self.db.get_order_by_id("order-nick-2", account_id="acc-order-nick-2")
        self.assertEqual(order_one["buyer_nick"], "buyer-nick-new-1")
        self.assertEqual(order_two["buyer_nick"], "buyer-nick-old-2")

    def test_item_info_crud_uses_account_id_boundary(self):
        self._save_account("acc-item-1")

        self.assertTrue(
            self.db.save_item_info(
                "acc-item-1",
                "item-1",
                {
                    "title": "Test item",
                    "description": "desc",
                    "category": "demo",
                    "price": "88",
                    "detail": "{\"ok\": true}",
                },
            )
        )

        item = self.db.get_item_info("acc-item-1", "item-1")
        self.assertEqual(item["account_id"], "acc-item-1")
        self.assertNotIn("cookie_id", item)

        items = self.db.get_items_by_account("acc-item-1")
        self.assertEqual([row["item_id"] for row in items], ["item-1"])
        self.assertEqual(items[0]["account_id"], "acc-item-1")
        self.assertNotIn("cookie_id", items[0])

        self.assertTrue(self.db.delete_item_info("acc-item-1", "item-1"))

    def test_item_info_auxiliary_mutations_accept_account_id_keyword(self):
        self._save_account("acc-item-aux-1")

        self.assertTrue(
            self.db.save_item_basic_info(
                account_id="acc-item-aux-1",
                item_id="item-aux-1",
                item_title="aux item",
                item_description="desc",
                item_category="demo",
                item_price="18",
                item_detail='{"seed": true}',
            )
        )
        self.assertTrue(
            self.db.update_item_title_only(
                account_id="acc-item-aux-1",
                item_id="item-aux-1",
                item_title="aux item renamed",
            )
        )
        self.assertTrue(
            self.db.update_item_detail(
                account_id="acc-item-aux-1",
                item_id="item-aux-1",
                item_detail='{"updated": true}',
            )
        )
        self.assertTrue(
            self.db.update_item_multi_spec_status(
                account_id="acc-item-aux-1",
                item_id="item-aux-1",
                is_multi_spec=True,
            )
        )
        self.assertTrue(
            self.db.update_item_multi_quantity_delivery_status(
                account_id="acc-item-aux-1",
                item_id="item-aux-1",
                multi_quantity_delivery=True,
            )
        )
        self.assertTrue(
            self.db.get_item_multi_spec_status(
                account_id="acc-item-aux-1",
                item_id="item-aux-1",
            )
        )
        self.assertTrue(
            self.db.get_item_multi_quantity_delivery_status(
                account_id="acc-item-aux-1",
                item_id="item-aux-1",
            )
        )

        item = self.db.get_item_info("acc-item-aux-1", "item-aux-1")
        self.assertEqual(item["item_title"], "aux item renamed")
        self.assertEqual(item["item_detail"], '{"updated": true}')
        self.assertTrue(item["is_multi_spec"])
        self.assertTrue(item["multi_quantity_delivery"])

        items = self.db.get_items_by_account(account_id="acc-item-aux-1")
        self.assertEqual([row["item_id"] for row in items], ["item-aux-1"])
        self.assertEqual(items[0]["account_id"], "acc-item-aux-1")
        self.assertNotIn("cookie_id", items[0])

        self.assertTrue(self.db.update_item_reply("acc-item-aux-1", "item-aux-1", "hello"))
        replies = self.db.get_item_replays_by_account(account_id="acc-item-aux-1")
        self.assertEqual([row["item_id"] for row in replies], ["item-aux-1"])
        self.assertEqual(replies[0]["account_id"], "acc-item-aux-1")
        self.assertNotIn("cookie_id", replies[0])

        self.assertTrue(
            self.db.insert_or_update_order(
                order_id="order-item-aux-1",
                item_id="item-aux-1",
                buyer_id="buyer-item-aux-1",
                account_id="acc-item-aux-1",
                order_status="processing",
            )
        )
        orders = self.db.get_orders_by_account(account_id="acc-item-aux-1")
        self.assertEqual([row["order_id"] for row in orders], ["order-item-aux-1"])
        self.assertEqual(orders[0]["account_id"], "acc-item-aux-1")
        self.assertNotIn("cookie_id", orders[0])

    def test_db_manager_does_not_keep_legacy_by_cookie_alias_methods(self):
        self.assertFalse(hasattr(db_module.DBManager, "get_items_by_cookie"))
        self.assertFalse(hasattr(db_module.DBManager, "get_itemReplays_by_cookie"))
        self.assertFalse(hasattr(db_module.DBManager, "get_orders_by_cookie"))

    def test_item_reply_crud_uses_account_id_boundary(self):
        self._save_account("acc-reply-1")
        self._save_account("acc-reply-2")

        self.assertTrue(self.db.update_item_reply("acc-reply-1", "item-1", "hello"))
        self.assertTrue(self.db.update_item_reply("acc-reply-2", "item-1", "world"))

        scoped_reply = self.db.get_item_replay("acc-reply-1", "item-1")
        self.assertEqual(scoped_reply["account_id"], "acc-reply-1")
        self.assertEqual(scoped_reply["reply_content"], "hello")
        self.assertNotIn("cookie_id", scoped_reply)

        replies = self.db.get_item_replays_by_account("acc-reply-1")
        self.assertEqual([row["item_id"] for row in replies], ["item-1"])
        self.assertEqual(replies[0]["account_id"], "acc-reply-1")
        self.assertNotIn("cookie_id", replies[0])

        delete_result = self.db.batch_delete_item_replies(
            [{"account_id": "acc-reply-1", "item_id": "item-1"}]
        )
        self.assertEqual(delete_result["success_count"], 1)
        self.assertEqual(delete_result["failed_count"], 0)

    def test_item_info_batch_writers_require_account_id_field(self):
        self._save_account("acc-item-batch-1")

        save_count = self.db.batch_save_item_basic_info(
            [
                {
                    "cookie_id": "acc-item-batch-1",
                    "item_id": "item-legacy-1",
                    "item_title": "legacy item",
                }
            ]
        )
        self.assertEqual(save_count, 0)
        self.assertIsNone(self.db.get_item_info("acc-item-batch-1", "item-legacy-1"))

        update_count = self.db.batch_update_item_title_price(
            [
                {
                    "cookie_id": "acc-item-batch-1",
                    "item_id": "item-legacy-1",
                    "item_title": "legacy item",
                    "item_price": "18",
                    "item_category": "demo",
                }
            ]
        )
        self.assertEqual(update_count, 0)

    def test_keywords_with_type_joins_item_info_by_account_id(self):
        self._save_account("acc-keyword-1")
        self.assertTrue(
            self.db.save_item_info(
                "acc-keyword-1",
                "item-1",
                {
                    "title": "Linked item",
                    "description": "desc",
                    "category": "demo",
                    "price": "66",
                    "detail": "{\"ok\": true}",
                },
            )
        )
        self.assertTrue(
            self.db.save_keywords_with_item_id(
                "acc-keyword-1",
                [("hello", "world", "item-1")],
            )
        )

        keywords = self.db.get_keywords_with_type("acc-keyword-1")
        self.assertEqual(len(keywords), 1)
        self.assertEqual(keywords[0]["item_id"], "item-1")
        self.assertEqual(keywords[0]["item_title"], "Linked item")

    def test_get_all_keywords_uses_account_id_keys_and_user_scope(self):
        self.assertTrue(self.db.create_user("keyword-user-1", "keyword-user-1@example.com", "pw-1"))
        self.assertTrue(self.db.create_user("keyword-user-2", "keyword-user-2@example.com", "pw-2"))

        first_user = self.db.get_user_by_username("keyword-user-1")
        second_user = self.db.get_user_by_username("keyword-user-2")
        self.assertIsNotNone(first_user)
        self.assertIsNotNone(second_user)

        self.assertTrue(self.db.save_cookie("acc-keyword-scope-1", "a=1", user_id=first_user["id"]))
        self.assertTrue(self.db.save_cookie("acc-keyword-scope-2", "b=2", user_id=second_user["id"]))
        self.assertTrue(
            self.db.save_keywords(
                "acc-keyword-scope-1",
                [("hello", "world")],
            )
        )
        self.assertTrue(
            self.db.save_keywords(
                "acc-keyword-scope-2",
                [("bye", "moon")],
            )
        )

        all_keywords = self.db.get_all_keywords()
        self.assertEqual(
            all_keywords,
            {
                "acc-keyword-scope-1": [("hello", "world")],
                "acc-keyword-scope-2": [("bye", "moon")],
            },
        )

        scoped_keywords = self.db.get_all_keywords(user_id=first_user["id"])
        self.assertEqual(
            scoped_keywords,
            {
                "acc-keyword-scope-1": [("hello", "world")],
            },
        )

    def test_control_plane_methods_expose_account_id_contract(self):
        expected_params = {
            "get_keywords": ["account_id"],
            "get_keywords_with_item_id": ["account_id"],
            "get_keywords_with_type": ["account_id"],
            "save_keywords": ["account_id", "keywords"],
            "save_keywords_with_item_id": ["account_id", "keywords"],
            "save_text_keywords_only": ["account_id", "keywords"],
            "check_keyword_duplicate": ["account_id", "keyword", "item_id"],
            "save_image_keyword": ["account_id", "keyword", "image_url", "item_id"],
            "update_keyword_image_url": ["account_id", "keyword", "new_image_url"],
            "delete_keyword_by_index": ["account_id", "index"],
            "save_cookie_status": ["account_id", "enabled"],
            "get_cookie_status": ["account_id"],
            "save_default_reply": ["account_id", "enabled", "reply_content", "reply_once"],
            "get_default_reply": ["account_id"],
            "add_default_reply_record": ["account_id", "chat_id"],
            "has_default_reply_record": ["account_id", "chat_id"],
            "clear_default_reply_records": ["account_id"],
            "delete_default_reply": ["account_id"],
            "set_message_notification": ["account_id", "channel_id", "enabled"],
            "get_account_notifications": ["account_id"],
            "delete_account_notifications": ["account_id", "user_id"],
            "update_auto_confirm": ["account_id", "auto_confirm"],
            "update_cookie_pause_duration": ["account_id", "pause_duration"],
            "get_cookie_pause_duration": ["account_id"],
            "get_auto_confirm": ["account_id"],
            "get_auto_comment": ["account_id"],
            "update_auto_comment": ["account_id", "auto_comment"],
            "get_comment_templates": ["account_id"],
            "get_active_comment_template": ["account_id"],
            "add_comment_template": ["account_id", "name", "content", "is_active"],
            "update_comment_template": ["account_id", "template_id", "name", "content", "is_active"],
            "delete_comment_template": ["account_id", "template_id"],
            "set_active_comment_template": ["account_id", "template_id"],
            "save_ai_reply_settings": ["account_id", "settings"],
            "get_ai_reply_settings": ["account_id"],
        }

        for method_name, expected in expected_params.items():
            parameters = list(inspect.signature(getattr(db_module.DBManager, method_name)).parameters)
            self.assertEqual(parameters[1: 1 + len(expected)], expected, method_name)

    def test_default_reply_and_notification_control_plane_accept_account_id(self):
        self._save_account("acc-control-1")

        self.db.save_default_reply("acc-control-1", True, "hello", True)
        reply = self.db.get_default_reply("acc-control-1")
        self.assertEqual(reply["reply_content"], "hello")
        self.assertTrue(reply["reply_once"])

        self.assertFalse(self.db.has_default_reply_record("acc-control-1", "chat-1"))
        self.db.add_default_reply_record("acc-control-1", "chat-1")
        self.assertTrue(self.db.has_default_reply_record("acc-control-1", "chat-1"))
        self.db.clear_default_reply_records("acc-control-1")
        self.assertFalse(self.db.has_default_reply_record("acc-control-1", "chat-1"))
        self.assertTrue(self.db.delete_default_reply("acc-control-1"))
        self.assertIsNone(self.db.get_default_reply("acc-control-1"))

        self.db.save_cookie_status("acc-control-1", False)
        self.assertFalse(self.db.get_cookie_status("acc-control-1"))

        channel_id = self.db.create_notification_channel("chan-1", "webhook", "{}", user_id=1)
        self.assertTrue(self.db.set_message_notification("acc-control-1", channel_id, True))
        notifications = self.db.get_account_notifications("acc-control-1")
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["channel_id"], channel_id)
        self.assertTrue(self.db.delete_account_notifications("acc-control-1"))
        self.assertEqual(self.db.get_account_notifications("acc-control-1"), [])

    def test_account_scoped_preferences_and_ai_settings_accept_account_id(self):
        self._save_account("acc-settings-1")

        self.assertTrue(self.db.update_auto_confirm("acc-settings-1", False))
        self.assertFalse(self.db.get_auto_confirm("acc-settings-1"))

        self.assertTrue(self.db.update_cookie_pause_duration("acc-settings-1", 7))
        self.assertEqual(self.db.get_cookie_pause_duration("acc-settings-1"), 7)

        self.assertTrue(self.db.update_auto_comment("acc-settings-1", True))
        self.assertTrue(self.db.get_auto_comment("acc-settings-1"))

        first_template_id = self.db.add_comment_template(
            "acc-settings-1",
            "tmpl-1",
            "first template",
            True,
        )
        second_template_id = self.db.add_comment_template(
            "acc-settings-1",
            "tmpl-2",
            "second template",
            False,
        )
        self.assertIsNotNone(first_template_id)
        self.assertIsNotNone(second_template_id)

        templates = self.db.get_comment_templates("acc-settings-1")
        self.assertEqual(
            {template["id"] for template in templates},
            {first_template_id, second_template_id},
        )
        self.assertEqual(
            self.db.get_active_comment_template("acc-settings-1")["id"],
            first_template_id,
        )

        self.assertTrue(
            self.db.set_active_comment_template("acc-settings-1", second_template_id)
        )
        self.assertEqual(
            self.db.get_active_comment_template("acc-settings-1")["id"],
            second_template_id,
        )

        settings_payload = {
            "ai_enabled": True,
            "model_name": "demo-model",
            "api_key": "demo-key",
            "base_url": "https://api.example.com/v1",
            "api_type": "openai",
            "max_discount_percent": 5,
            "max_discount_amount": 9,
            "max_bargain_rounds": 2,
            "custom_prompts": "demo prompt",
        }
        self.assertTrue(self.db.save_ai_reply_settings("acc-settings-1", settings_payload))

        ai_settings = self.db.get_ai_reply_settings("acc-settings-1")
        self.assertTrue(ai_settings["ai_enabled"])
        self.assertEqual(ai_settings["model_name"], "demo-model")
        self.assertEqual(ai_settings["api_key"], "demo-key")
        self.assertEqual(ai_settings["base_url"], "https://api.example.com/v1")
        self.assertEqual(ai_settings["api_type"], "openai")
        self.assertEqual(ai_settings["max_discount_percent"], 5)
        self.assertEqual(ai_settings["max_discount_amount"], 9)
        self.assertEqual(ai_settings["max_bargain_rounds"], 2)
        self.assertEqual(ai_settings["custom_prompts"], "demo prompt")

    def test_comment_template_mutations_reject_cross_account_scope(self):
        self._save_account("acc-template-1")
        self._save_account("acc-template-2")

        first_account_template_id = self.db.add_comment_template(
            "acc-template-1",
            "tmpl-1",
            "first account template",
            True,
        )
        second_account_template_id = self.db.add_comment_template(
            "acc-template-2",
            "tmpl-2",
            "second account template",
            True,
        )

        self.assertIsNotNone(first_account_template_id)
        self.assertIsNotNone(second_account_template_id)

        self.assertFalse(
            self.db.update_comment_template(
                "acc-template-1",
                second_account_template_id,
                name="hijacked",
                is_active=True,
            )
        )
        self.assertFalse(
            self.db.delete_comment_template(
                "acc-template-1",
                second_account_template_id,
            )
        )
        self.assertFalse(
            self.db.set_active_comment_template(
                "acc-template-1",
                second_account_template_id,
            )
        )

        first_account_templates = {
            template["id"]: template
            for template in self.db.get_comment_templates("acc-template-1")
        }
        second_account_templates = {
            template["id"]: template
            for template in self.db.get_comment_templates("acc-template-2")
        }

        self.assertEqual(
            first_account_templates[first_account_template_id]["name"],
            "tmpl-1",
        )
        self.assertEqual(
            second_account_templates[second_account_template_id]["name"],
            "tmpl-2",
        )
        self.assertEqual(
            self.db.get_active_comment_template("acc-template-1")["id"],
            first_account_template_id,
        )
        self.assertEqual(
            self.db.get_active_comment_template("acc-template-2")["id"],
            second_account_template_id,
        )

    def test_control_plane_aggregate_readers_return_account_id_keys(self):
        self._save_account("acc-aggregate-1")

        self.db.save_cookie_status("acc-aggregate-1", False)
        self.assertTrue(
            self.db.save_keywords(
                "acc-aggregate-1",
                [("aggregate-keyword", "aggregate-reply")],
            )
        )
        self.db.save_default_reply("acc-aggregate-1", True, "hello", False)
        self.assertTrue(
            self.db.save_ai_reply_settings(
                "acc-aggregate-1",
                {
                    "ai_enabled": True,
                    "model_name": "aggregate-model",
                },
            )
        )
        channel_id = self.db.create_notification_channel(
            "chan-aggregate-1",
            "webhook",
            "{}",
            user_id=1,
        )
        self.assertTrue(self.db.set_message_notification("acc-aggregate-1", channel_id, True))

        all_status = self.db.get_all_cookie_status()
        self.assertIn("acc-aggregate-1", all_status)
        self.assertFalse(all_status["acc-aggregate-1"])

        all_keywords = self.db.get_all_keywords()
        self.assertIn("acc-aggregate-1", all_keywords)
        self.assertEqual(
            all_keywords["acc-aggregate-1"],
            [("aggregate-keyword", "aggregate-reply")],
        )

        all_replies = self.db.get_all_default_replies()
        self.assertIn("acc-aggregate-1", all_replies)
        self.assertEqual(all_replies["acc-aggregate-1"]["reply_content"], "hello")

        all_ai_settings = self.db.get_all_ai_reply_settings()
        self.assertIn("acc-aggregate-1", all_ai_settings)
        self.assertEqual(
            all_ai_settings["acc-aggregate-1"]["model_name"],
            "aggregate-model",
        )

        all_notifications = self.db.get_all_message_notifications()
        self.assertIn("acc-aggregate-1", all_notifications)
        self.assertEqual(all_notifications["acc-aggregate-1"][0]["channel_id"], channel_id)

    def test_delete_user_and_data_cleans_account_scoped_tables(self):
        self.assertTrue(self.db.create_user("delete-user-1", "delete-user-1@example.com", "pw-1"))
        user = self.db.get_user_by_username("delete-user-1")
        self.assertIsNotNone(user)
        user_id = user["id"]

        self.assertTrue(self.db.save_cookie("acc-delete-user-1", "a=1", user_id=user_id))
        self.assertTrue(
            self.db.save_keywords(
                "acc-delete-user-1",
                [("delete-keyword", "delete-reply")],
            )
        )
        self.db.save_default_reply("acc-delete-user-1", True, "delete-default", False)
        self.assertTrue(
            self.db.save_ai_reply_settings(
                "acc-delete-user-1",
                {
                    "ai_enabled": True,
                    "model_name": "delete-model",
                },
            )
        )
        channel_id = self.db.create_notification_channel(
            "delete-channel-1",
            "webhook",
            "{}",
            user_id=user_id,
        )
        self.assertTrue(self.db.set_message_notification("acc-delete-user-1", channel_id, True))

        self.assertTrue(self.db.delete_user_and_data(user_id))

        for table_name in (
            "cookies",
            "keywords",
            "default_replies",
            "ai_reply_settings",
            "message_notifications",
        ):
            with self.subTest(table_name=table_name):
                row = self.db.conn.execute(
                    f"SELECT COUNT(*) FROM {table_name} WHERE account_id = ?"
                    if table_name != "cookies"
                    else "SELECT COUNT(*) FROM cookies WHERE id = ?",
                    ("acc-delete-user-1",),
                ).fetchone()
                self.assertEqual(row[0], 0)

        user_row = self.db.conn.execute(
            "SELECT COUNT(*) FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        self.assertEqual(user_row[0], 0)

    def test_user_scoped_backup_and_restore_handle_item_info_by_account_id(self):
        self._save_account("acc-backup-1")
        self.assertTrue(
            self.db.save_item_info(
                "acc-backup-1",
                "item-1",
                {
                    "title": "backup item",
                    "description": "desc",
                    "category": "demo",
                    "price": "88",
                    "detail": "{\"ok\": true}",
                },
            )
        )

        backup = self.db.export_backup(user_id=1)
        self.assertIn("item_info", backup["data"])
        item_info_columns = backup["data"]["item_info"]["columns"]
        item_info_rows = backup["data"]["item_info"]["rows"]
        self.assertIn("account_id", item_info_columns)
        self.assertEqual(len(item_info_rows), 1)
        self.assertEqual(
            item_info_rows[0][item_info_columns.index("account_id")],
            "acc-backup-1",
        )
        self.assertEqual(
            item_info_rows[0][item_info_columns.index("item_id")],
            "item-1",
        )

        self.assertTrue(
            self.db.save_item_info(
                "acc-backup-1",
                "item-2",
                {
                    "title": "stale item",
                    "description": "desc",
                    "category": "demo",
                    "price": "99",
                    "detail": "{\"stale\": true}",
                },
            )
        )

        self.assertTrue(self.db.import_backup(backup, user_id=1))

        restored_items = self.db.get_items_by_account("acc-backup-1")
        self.assertEqual([row["item_id"] for row in restored_items], ["item-1"])
        self.assertEqual(restored_items[0]["account_id"], "acc-backup-1")

    def test_cookie_details_expose_account_id_field(self):
        self._save_account("acc-details-1")

        details = self.db.get_cookie_details("acc-details-1")
        self.assertIsNotNone(details)
        self.assertEqual(details["account_id"], "acc-details-1")
        self.assertEqual(details["id"], "acc-details-1")

    def test_account_credential_mutations_accept_account_id_keyword(self):
        self.assertTrue(
            self.db.save_cookie(
                account_id="acc-credential-1",
                cookie_value="a=1; b=2",
                user_id=1,
            )
        )
        self.assertEqual(
            self.db.get_cookie(account_id="acc-credential-1"),
            "a=1; b=2",
        )

        cookie_info = self.db.get_cookie_by_id(account_id="acc-credential-1")
        self.assertIsNotNone(cookie_info)
        self.assertEqual(cookie_info["account_id"], "acc-credential-1")
        self.assertEqual(cookie_info["id"], "acc-credential-1")
        self.assertNotIn("cookie_id", cookie_info)

        self.assertTrue(
            self.db.update_cookie_remark(
                account_id="acc-credential-1",
                remark="remark-1",
            )
        )
        self.assertTrue(
            self.db.update_cookie_account_info(
                account_id="acc-credential-1",
                username="demo-user",
                password="demo-pass",
                show_browser=True,
            )
        )
        self.assertTrue(
            self.db.update_cookie_proxy_config(
                account_id="acc-credential-1",
                proxy_type="http",
                proxy_host="127.0.0.1",
                proxy_port=7890,
                proxy_user="proxy-user",
                proxy_pass="proxy-pass",
            )
        )

        details = self.db.get_cookie_details("acc-credential-1")
        self.assertEqual(details["remark"], "remark-1")
        self.assertEqual(details["username"], "demo-user")
        self.assertEqual(details["password"], "demo-pass")
        self.assertTrue(details["show_browser"])

        proxy_config = self.db.get_cookie_proxy_config(account_id="acc-credential-1")
        self.assertEqual(proxy_config["proxy_type"], "http")
        self.assertEqual(proxy_config["proxy_host"], "127.0.0.1")
        self.assertEqual(proxy_config["proxy_port"], 7890)
        self.assertEqual(proxy_config["proxy_user"], "proxy-user")
        self.assertEqual(proxy_config["proxy_pass"], "proxy-pass")

        self.assertTrue(self.db.delete_cookie(account_id="acc-credential-1"))
        self.assertIsNone(self.db.get_cookie_by_id(account_id="acc-credential-1"))

    def test_risk_control_logs_use_account_id_boundary(self):
        self._save_account("acc-risk-1")

        log_id = self.db.add_risk_control_log(
            account_id="acc-risk-1",
            event_type="slider_captcha",
            processing_status="processing",
            event_description="unit-test",
        )
        self.assertIsNotNone(log_id)

        logs = self.db.get_risk_control_logs(account_id="acc-risk-1", limit=20)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["account_id"], "acc-risk-1")
        self.assertNotIn("cookie_id", logs[0])

        total = self.db.get_risk_control_logs_count(account_id="acc-risk-1")
        self.assertEqual(total, 1)

        stale_count = self.db.mark_stale_risk_control_logs_failed(timeout_minutes=0, account_id="acc-risk-1")
        self.assertEqual(stale_count, 1)

        stats = self.db.get_slider_verification_session_stats(account_ids=["acc-risk-1"], range_key="all")
        self.assertEqual(stats["accounts_with_sessions"], 1)
        self.assertEqual(stats["total_sessions"], 1)

    def test_risk_control_log_filter_helper_uses_account_id_column(self):
        conditions, params = self.db._build_risk_control_log_filters(
            alias="r",
            account_id="acc-risk-1",
            processing_status="processing",
        )

        self.assertIn("r.account_id = ?", conditions)
        self.assertIn("r.processing_status = ?", conditions)
        self.assertEqual(params[:2], ["acc-risk-1", "processing"])
        self.assertTrue(all("cookie_id" not in condition for condition in conditions))

    def test_delivery_logs_and_finalization_states_use_account_id_boundary(self):
        self._save_account("acc-delivery-1")

        log_id = self.db.create_delivery_log(
            user_id=1,
            account_id="acc-delivery-1",
            order_id="order-delivery-1",
            item_id="item-delivery-1",
            buyer_id="buyer-delivery-1",
            buyer_nick="buyer",
            channel="manual",
            status="failed",
            reason="unit-test",
        )
        self.assertIsNotNone(log_id)

        logs = self.db.get_recent_delivery_logs(user_id=1, limit=10)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["account_id"], "acc-delivery-1")
        self.assertNotIn("cookie_id", logs[0])

        self.assertTrue(
            self.db.upsert_delivery_finalization_state(
                order_id="order-delivery-1",
                unit_index=1,
                account_id="acc-delivery-1",
                item_id="item-delivery-1",
                buyer_id="buyer-delivery-1",
                channel="manual",
                status="sent",
                delivery_meta={"delivery_unit_index": 1, "flag": "pending"},
                last_error="pending",
            )
        )

        state = self.db.get_delivery_finalization_state(
            "order-delivery-1",
            1,
            account_id="acc-delivery-1",
        )
        self.assertEqual(state["account_id"], "acc-delivery-1")
        self.assertEqual(state["delivery_meta"]["flag"], "pending")
        self.assertNotIn("cookie_id", state)

        states = self.db.get_delivery_finalization_states(
            "order-delivery-1",
            account_id="acc-delivery-1",
        )
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["account_id"], "acc-delivery-1")
        self.assertNotIn("cookie_id", states[0])

        summary = self.db.get_delivery_progress_summary(
            "order-delivery-1",
            account_id="acc-delivery-1",
            expected_quantity=1,
        )
        self.assertEqual(summary["aggregate_status"], "partial_pending_finalize")
        self.assertEqual(summary["states"][0]["account_id"], "acc-delivery-1")
        self.assertNotIn("cookie_id", summary["states"][0])

    def test_delivery_finalization_state_methods_require_account_id_scope(self):
        self._save_account("acc-delivery-scope-1")

        self.assertTrue(
            self.db.upsert_delivery_finalization_state(
                order_id="order-delivery-scope-1",
                unit_index=1,
                account_id="acc-delivery-scope-1",
                item_id="item-delivery-scope-1",
                buyer_id="buyer-delivery-scope-1",
                channel="manual",
                status="sent",
                delivery_meta={"delivery_unit_index": 1},
            )
        )

        with self.assertRaisesRegex(ValueError, "account_id"):
            self.db.upsert_delivery_finalization_state(
                order_id="order-delivery-scope-1",
                unit_index=2,
                account_id="   ",
                item_id="item-delivery-scope-1",
                buyer_id="buyer-delivery-scope-1",
                channel="manual",
                status="sent",
                delivery_meta={"delivery_unit_index": 2},
            )

        with self.assertRaisesRegex(ValueError, "account_id"):
            self.db.get_delivery_finalization_state("order-delivery-scope-1", 1, account_id=None)

        with self.assertRaisesRegex(ValueError, "account_id"):
            self.db.get_delivery_finalization_states("order-delivery-scope-1", account_id="   ")

        with self.assertRaisesRegex(ValueError, "account_id"):
            self.db.get_delivery_progress_summary("order-delivery-scope-1", account_id="", expected_quantity=1)

    def test_delivery_finalization_state_rejects_cross_account_overwrite(self):
        self._save_account("acc-delivery-guard-1")
        self._save_account("acc-delivery-guard-2")

        self.assertTrue(
            self.db.upsert_delivery_finalization_state(
                order_id="order-delivery-guard-1",
                unit_index=1,
                account_id="acc-delivery-guard-1",
                item_id="item-delivery-guard-1",
                buyer_id="buyer-delivery-guard-1",
                channel="manual",
                status="sent",
                delivery_meta={"delivery_unit_index": 1, "flag": "owner-1"},
            )
        )

        self.assertFalse(
            self.db.upsert_delivery_finalization_state(
                order_id="order-delivery-guard-1",
                unit_index=1,
                account_id="acc-delivery-guard-2",
                item_id="item-delivery-guard-2",
                buyer_id="buyer-delivery-guard-2",
                channel="auto",
                status="finalized",
                delivery_meta={"delivery_unit_index": 1, "flag": "owner-2"},
            )
        )

        kept_state = self.db.get_delivery_finalization_state(
            "order-delivery-guard-1",
            1,
            account_id="acc-delivery-guard-1",
        )
        self.assertIsNotNone(kept_state)
        self.assertEqual(kept_state["account_id"], "acc-delivery-guard-1")
        self.assertEqual(kept_state["delivery_meta"]["flag"], "owner-1")

        self.assertIsNone(
            self.db.get_delivery_finalization_state(
                "order-delivery-guard-1",
                1,
                account_id="acc-delivery-guard-2",
            )
        )
        self.assertEqual(
            self.db.get_delivery_finalization_states(
                "order-delivery-guard-1",
                account_id="acc-delivery-guard-2",
            ),
            [],
        )
        cross_summary = self.db.get_delivery_progress_summary(
            "order-delivery-guard-1",
            account_id="acc-delivery-guard-2",
            expected_quantity=1,
        )
        self.assertEqual(cross_summary["state_count"], 0)
        self.assertEqual(cross_summary["states"], [])

    def test_data_card_reservations_use_account_id_boundary(self):
        self._save_account("acc-reserve-1")

        card_id = self.db.create_card(
            name="data-card",
            card_type="data",
            data_content="line-1\nline-2",
            user_id=1,
        )
        self.assertIsNotNone(card_id)

        reservation = self.db.reserve_batch_data(
            card_id=card_id,
            order_id="order-reserve-1",
            unit_index=1,
            account_id="acc-reserve-1",
            buyer_id="buyer-reserve-1",
            ttl_minutes=5,
        )
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation["account_id"], "acc-reserve-1")
        self.assertNotIn("cookie_id", reservation)

        table_data, columns = self.db.get_table_data("data_card_reservations")
        self.assertIn("account_id", columns)
        self.assertNotIn("cookie_id", columns)
        self.assertEqual(table_data[0]["account_id"], "acc-reserve-1")

    def test_target_batch_methods_reject_cookie_id_compat_aliases(self):
        self._save_account("acc-batch-guard-1")

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.save_cookie(
                cookie_id="acc-batch-guard-1",
                cookie_value="a=1; b=2",
                user_id=1,
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.get_cookie(cookie_id="acc-batch-guard-1")

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.get_cookie_by_id(cookie_id="acc-batch-guard-1")

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.delete_cookie(cookie_id="acc-batch-guard-1")

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.update_cookie_remark(
                cookie_id="acc-batch-guard-1",
                remark="legacy-remark",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.update_cookie_account_info(
                cookie_id="acc-batch-guard-1",
                username="legacy-user",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.update_cookie_proxy_config(
                cookie_id="acc-batch-guard-1",
                proxy_type="none",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.get_cookie_proxy_config(cookie_id="acc-batch-guard-1")

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.insert_or_update_order(
                order_id="order-legacy-1",
                item_id="item-legacy-1",
                buyer_id="buyer-legacy-1",
                cookie_id="acc-batch-guard-1",
                order_status="processing",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.save_item_info(
                cookie_id="acc-batch-guard-1",
                item_id="item-legacy-1",
                item_data={"title": "legacy"},
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.save_item_basic_info(
                cookie_id="acc-batch-guard-1",
                item_id="item-legacy-1",
                item_title="legacy",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.update_item_detail(
                cookie_id="acc-batch-guard-1",
                item_id="item-legacy-1",
                item_detail="legacy",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.update_item_title_only(
                cookie_id="acc-batch-guard-1",
                item_id="item-legacy-1",
                item_title="legacy",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.update_item_multi_spec_status(
                cookie_id="acc-batch-guard-1",
                item_id="item-legacy-1",
                is_multi_spec=True,
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.get_item_multi_spec_status(
                cookie_id="acc-batch-guard-1",
                item_id="item-legacy-1",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.update_item_multi_quantity_delivery_status(
                cookie_id="acc-batch-guard-1",
                item_id="item-legacy-1",
                multi_quantity_delivery=True,
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.get_item_multi_quantity_delivery_status(
                cookie_id="acc-batch-guard-1",
                item_id="item-legacy-1",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.update_item_reply(
                cookie_id="acc-batch-guard-1",
                item_id="item-legacy-1",
                reply_content="legacy",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.add_risk_control_log(
                cookie_id="acc-batch-guard-1",
                event_type="slider_captcha",
                processing_status="processing",
            )

        with self.assertRaisesRegex(TypeError, "cookie_ids|unexpected keyword"):
            self.db.get_slider_verification_session_stats(cookie_ids=["acc-batch-guard-1"], range_key="all")

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.create_delivery_log(
                user_id=1,
                cookie_id="acc-batch-guard-1",
                order_id="order-legacy-delivery-1",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.upsert_delivery_finalization_state(
                order_id="order-legacy-delivery-1",
                unit_index=1,
                cookie_id="acc-batch-guard-1",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.reserve_batch_data(
                card_id=1,
                order_id="order-legacy-reserve-1",
                unit_index=1,
                cookie_id="acc-batch-guard-1",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.get_recent_order_by_buyer_id(
                "buyer-legacy-1",
                cookie_id="acc-batch-guard-1",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.get_recent_order_by_sid(
                "sid-legacy-1@goofish",
                cookie_id="acc-batch-guard-1",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.find_recent_orders_by_match_context(
                buyer_id="buyer-legacy-1",
                cookie_id="acc-batch-guard-1",
            )

        with self.assertRaisesRegex(TypeError, "cookie_id|unexpected keyword"):
            self.db.update_buyer_nick_by_buyer_id(
                "buyer-legacy-1",
                "buyer-legacy-new",
                cookie_id="acc-batch-guard-1",
            )

    def test_db_manager_does_not_keep_account_id_compatibility_helpers(self):
        self.assertFalse(hasattr(db_module.DBManager, "_resolve_account_id"))
        self.assertFalse(hasattr(db_module.DBManager, "_normalize_account_scoped_record"))
        self.assertFalse(hasattr(db_module.DBManager, "_backfill_account_id_column"))


if __name__ == "__main__":
    unittest.main()
