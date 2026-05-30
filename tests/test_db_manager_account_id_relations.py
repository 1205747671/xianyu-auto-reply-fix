import os
import inspect
import sqlite3
import tempfile
import unittest
from unittest import mock
import ast
import json
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

    def _count_rows(self, table_name, column_name, value):
        row = self.db.conn.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {column_name} = ?",
            (value,),
        ).fetchone()
        return row[0]

    def _seed_cleanup_fixture(self, account_id, user_id, prefix):
        self.assertTrue(self.db.save_cookie(account_id, f"{prefix}=1", user_id=user_id))
        self.assertTrue(
            self.db.save_keywords(
                account_id,
                [(f"{prefix}-keyword", f"{prefix}-reply")],
            )
        )
        self.assertTrue(self.db.save_default_reply(account_id, True, f"{prefix}-default", False))
        self.assertTrue(self.db.save_cookie_status(account_id, False))
        self.assertTrue(self.db.add_default_reply_record(account_id, f"{prefix}-chat"))
        self.assertTrue(
            self.db.save_item_info(
                account_id,
                f"{prefix}-item",
                {
                    "title": f"{prefix}-title",
                    "description": f"{prefix}-description",
                    "category": "cleanup",
                    "price": "88",
                    "detail": "{\"cleanup\": true}",
                },
            )
        )
        self.assertTrue(self.db.update_item_reply(account_id, f"{prefix}-item", f"{prefix}-item-reply"))
        self.assertIsNotNone(
            self.db.add_comment_template(
                account_id,
                f"{prefix}-template",
                f"{prefix}-comment",
                True,
            )
        )
        self.assertIsNotNone(
            self.db.add_risk_control_log(
                account_id,
                event_type="slider_captcha",
                processing_status="processing",
                event_description=f"{prefix}-risk",
            )
        )
        self.assertTrue(
            self.db.save_ai_reply_settings(
                account_id,
                {
                    "ai_enabled": True,
                    "model_name": f"{prefix}-model",
                },
            )
        )
        channel_id = self.db.create_notification_channel(
            f"{prefix}-channel",
            "webhook",
            "{}",
            user_id=user_id,
        )
        self.assertTrue(self.db.set_message_notification(account_id, channel_id, True))
        self.assertTrue(
            self.db.insert_or_update_order(
                order_id=f"{prefix}-order",
                item_id=f"{prefix}-item",
                buyer_id=f"{prefix}-buyer",
                buyer_nick=f"{prefix}-buyer-nick",
                account_id=account_id,
                order_status="processing",
            )
        )
        self.assertIsNotNone(
            self.db.create_scheduled_task(
                name=f"{prefix}-task",
                task_type="item_polish",
                account_id=account_id,
                user_id=user_id,
                next_run_at="2026-01-01 00:00:00",
            )
        )
        card_id = self.db.create_card(
            name=f"{prefix}-data-card",
            card_type="data",
            data_content=f"{prefix}-line-1\n{prefix}-line-2",
            user_id=user_id,
        )
        self.assertIsNotNone(card_id)
        self.assertIsNotNone(
            self.db.create_delivery_rule(
                keyword=f"{prefix}-rule",
                card_id=card_id,
                description=f"{prefix}-rule-description",
                user_id=user_id,
            )
        )
        self.assertIsNotNone(
            self.db.create_delivery_log(
                user_id=user_id,
                account_id=account_id,
                order_id=f"{prefix}-order",
                item_id=f"{prefix}-item",
                buyer_id=f"{prefix}-buyer",
                buyer_nick=f"{prefix}-buyer-nick",
                channel="manual",
                status="failed",
                reason=f"{prefix}-delivery-log",
            )
        )
        self.assertTrue(
            self.db.upsert_delivery_finalization_state(
                order_id=f"{prefix}-order",
                unit_index=1,
                account_id=account_id,
                item_id=f"{prefix}-item",
                buyer_id=f"{prefix}-buyer",
                channel="manual",
                status="sent",
                delivery_meta={"source": prefix},
            )
        )
        reservation = self.db.reserve_batch_data(
            card_id=card_id,
            order_id=f"{prefix}-reservation-order",
            unit_index=1,
            account_id=account_id,
            buyer_id=f"{prefix}-buyer",
            ttl_minutes=5,
        )
        self.assertIsNotNone(reservation)
        self.assertTrue(
            self.db.set_user_setting(
                user_id,
                f"{prefix}-setting",
                f"{prefix}-value",
                f"{prefix}-description",
            )
        )
        self.db.conn.execute(
            """
            INSERT INTO ai_conversations (
                account_id, chat_id, user_id, item_id, role, content, intent, bargain_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                f"{prefix}-chat",
                f"user-{user_id}",
                f"{prefix}-item",
                "assistant",
                f"{prefix}-conversation",
                "reply",
                0,
            ),
        )
        self.db.conn.commit()

    def test_update_card_clears_stale_multi_spec_fields_when_switching_to_plain_card(self):
        card_id = self.db.create_card(
            name="multi-card",
            card_type="text",
            text_content="hello",
            is_multi_spec=True,
            spec_name="面额",
            spec_value="10元",
            spec_name_2="版本",
            spec_value_2="A",
            user_id=1,
        )
        self.assertIsNotNone(card_id)

        self.assertTrue(
            self.db.update_card(
                card_id=card_id,
                name="plain-card",
                is_multi_spec=False,
                user_id=1,
            )
        )

        card = self.db.get_card_by_id(card_id, user_id=1)
        self.assertIsNotNone(card)
        self.assertEqual(card["name"], "plain-card")
        self.assertFalse(card["is_multi_spec"])
        self.assertIsNone(card["spec_name"])
        self.assertIsNone(card["spec_value"])
        self.assertIsNone(card["spec_name_2"])
        self.assertIsNone(card["spec_value_2"])

    def test_update_card_rejects_duplicate_plain_name_for_same_user(self):
        original_id = self.db.create_card(
            name="plain-card-a",
            card_type="text",
            text_content="a",
            user_id=1,
        )
        duplicate_target_id = self.db.create_card(
            name="plain-card-b",
            card_type="text",
            text_content="b",
            user_id=1,
        )
        self.assertIsNotNone(original_id)
        self.assertIsNotNone(duplicate_target_id)

        with self.assertRaisesRegex(ValueError, "卡券名称已存在：plain-card-a"):
            self.db.update_card(
                card_id=duplicate_target_id,
                name="plain-card-a",
                user_id=1,
            )

    def test_update_card_rejects_duplicate_multi_spec_combination_for_same_user(self):
        original_id = self.db.create_card(
            name="multi-card",
            card_type="text",
            text_content="a",
            is_multi_spec=True,
            spec_name="面额",
            spec_value="10元",
            user_id=1,
        )
        duplicate_target_id = self.db.create_card(
            name="multi-card-2",
            card_type="text",
            text_content="b",
            is_multi_spec=True,
            spec_name="面额",
            spec_value="20元",
            user_id=1,
        )
        self.assertIsNotNone(original_id)
        self.assertIsNotNone(duplicate_target_id)

        with self.assertRaisesRegex(ValueError, "卡券已存在：multi-card - 面额:10元"):
            self.db.update_card(
                card_id=duplicate_target_id,
                name="multi-card",
                is_multi_spec=True,
                spec_name="面额",
                spec_value="10元",
                user_id=1,
            )

    def test_card_methods_reject_blank_name_after_trimming(self):
        with self.assertRaisesRegex(ValueError, "卡券名称不能为空"):
            self.db.create_card(
                name="   ",
                card_type="text",
                text_content="hello",
                user_id=1,
            )

        card_id = self.db.create_card(
            name="  demo-card  ",
            card_type="text",
            text_content="hello",
            user_id=1,
        )
        self.assertIsNotNone(card_id)
        created_card = self.db.get_card_by_id(card_id, user_id=1)
        self.assertEqual("demo-card", created_card["name"])

        with self.assertRaisesRegex(ValueError, "卡券名称不能为空"):
            self.db.update_card(
                card_id=card_id,
                name="   ",
                user_id=1,
            )

        self.assertTrue(
            self.db.update_card(
                card_id=card_id,
                name="  updated-card  ",
                user_id=1,
            )
        )
        updated_card = self.db.get_card_by_id(card_id, user_id=1)
        self.assertEqual("updated-card", updated_card["name"])

    def test_card_methods_reject_invalid_card_type(self):
        with self.assertRaisesRegex(ValueError, "卡券类型无效"):
            self.db.create_card(
                name="demo-card",
                card_type="bad-type",
                text_content="hello",
                user_id=1,
            )

        card_id = self.db.create_card(
            name="valid-card",
            card_type="text",
            text_content="hello",
            user_id=1,
        )
        self.assertIsNotNone(card_id)

        with self.assertRaisesRegex(ValueError, "卡券类型无效"):
            self.db.update_card(
                card_id=card_id,
                card_type="bad-type",
                user_id=1,
            )

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

    def test_import_backup_rewrites_notification_templates_to_target_user_scope(self):
        cursor = self.db.conn.cursor()
        cursor.execute(
            """
            INSERT INTO users (username, password_hash, email)
            VALUES (?, ?, ?)
            """,
            ("target-user", "hashed-password", "target-user@example.com"),
        )
        self.db.conn.commit()
        target_user_id = cursor.lastrowid

        self.assertTrue(self.db.save_cookie("target-account", "k=v", user_id=target_user_id))
        source_template = self.db.get_notification_template("message", user_id=1)
        self.assertIsNotNone(source_template)

        backup_payload = {
            "data": {
                "notification_templates": {
                    "columns": ["id", "user_id", "type", "template", "created_at", "updated_at"],
                    "rows": [[
                        source_template["id"],
                        1,
                        "message",
                        "user-1 imported template",
                        source_template["created_at"],
                        source_template["updated_at"],
                    ]],
                },
            },
        }

        self.assertTrue(self.db.import_backup(json.loads(json.dumps(backup_payload)), user_id=target_user_id))

        target_template = self.db.get_notification_template("message", user_id=target_user_id)
        self.assertIsNotNone(target_template)
        self.assertEqual("user-1 imported template", target_template["template"])

        source_template_after = self.db.get_notification_template("message", user_id=1)
        self.assertIsNotNone(source_template_after)
        self.assertNotEqual("user-1 imported template", source_template_after["template"])

    def test_save_cookie_rejects_default_or_invalid_account_id(self):
        for invalid_account_id in ("default", "bad scope!", "中文账号"):
            with self.subTest(account_id=invalid_account_id):
                with self.assertRaisesRegex(ValueError, "account_id"):
                    self.db.save_cookie(invalid_account_id, "a=1; b=2", user_id=1)

    def test_db_manager_source_has_no_mojibake_account_messages(self):
        source = Path("db_manager.py").read_text(encoding="utf-8")
        expected_messages = (
            "保存关键词失败",
            "获取账号启用状态失败",
            "暂无滑块验证记录",
            "处理超时，自动标记失败",
            "备份数据格式无效",
            "已绑定用户",
        )
        unexpected_mojibake = (
            "淇濆瓨鍏抽敭瀛楀け璐",
            "鑾峰彇璐﹀彿鍚敤鐘舵€佸け璐",
            "鏆傛棤婊戝潡楠岃瘉璁板綍",
            "澶勭悊瓒呮椂锛岃嚜鍔ㄦ爣璁板け璐",
            "澶囦唤鏁版嵁鏍煎紡鏃犳晥",
            "宸茬粦瀹氱敤鎴",
        )

        for message in expected_messages:
            self.assertIn(message, source)
        for mojibake in unexpected_mojibake:
            self.assertNotIn(mojibake, source)

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

    def test_get_orders_by_account_can_load_more_than_one_thousand_rows_when_uncapped(self):
        account_id = "acc-order-all"
        self._save_account(account_id)

        cursor = self.db.conn.cursor()
        cursor.executemany(
            """
            INSERT INTO orders (order_id, item_id, buyer_id, buyer_nick, order_status, account_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"order-all-{index}",
                    f"item-all-{index}",
                    f"buyer-all-{index}",
                    f"buyer-all-{index}",
                    "processing",
                    account_id,
                )
                for index in range(1001)
            ],
        )
        self.db.conn.commit()

        limited_orders = self.db.get_orders_by_account(account_id, limit=1000)
        all_orders = self.db.get_orders_by_account(account_id, limit=None)

        self.assertEqual(len(limited_orders), 1000)
        self.assertEqual(len(all_orders), 1001)
        self.assertIn("order-all-0", {row["order_id"] for row in all_orders})
        self.assertIn("order-all-1000", {row["order_id"] for row in all_orders})

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
                cursor.execute("PRAGMA foreign_keys = OFF")
                try:
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
                finally:
                    cursor.execute("PRAGMA foreign_keys = ON")

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

    def test_count_items_by_account_is_scoped_by_account_id(self):
        self._save_account("acc-item-count-1")
        self._save_account("acc-item-count-2")

        self.assertTrue(
            self.db.save_item_info(
                "acc-item-count-1",
                "item-1",
                {"title": "one", "description": "", "category": "", "price": "1", "detail": "{}"},
            )
        )
        self.assertTrue(
            self.db.save_item_info(
                "acc-item-count-1",
                "item-2",
                {"title": "two", "description": "", "category": "", "price": "2", "detail": "{}"},
            )
        )
        self.assertTrue(
            self.db.save_item_info(
                "acc-item-count-2",
                "item-3",
                {"title": "three", "description": "", "category": "", "price": "3", "detail": "{}"},
            )
        )

        self.assertEqual(2, self.db.count_items_by_account("acc-item-count-1"))
        self.assertEqual(1, self.db.count_items_by_account("acc-item-count-2"))

    def test_delivery_rules_list_exposes_card_enabled_state_for_current_rule(self):
        card_id = self.db.create_card(
            "card-disabled-for-rule",
            "text",
            text_content="hello",
            enabled=False,
            user_id=1,
        )
        rule_id = self.db.create_delivery_rule(
            keyword="demo-keyword",
            card_id=card_id,
            enabled=True,
            description="demo rule",
            user_id=1,
        )

        rules = self.db.get_all_delivery_rules(user_id=1)
        target_rule = next(rule for rule in rules if rule["id"] == rule_id)
        self.assertFalse(target_rule["card_enabled"])
        self.assertEqual(card_id, target_rule["card_id"])

    def test_card_db_helpers_do_not_leave_debug_spec_logs(self):
        source = Path(db_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("[DEBUG DB] create_card", source)
        self.assertNotIn("[DEBUG DB] update_card", source)
        self.assertNotIn("[DEBUG DB] 执行SQL", source)

    def test_get_all_cards_summary_only_omits_bulk_payload_fields_but_keeps_data_count(self):
        card_id = self.db.create_card(
            "summary-card",
            "data",
            data_content="line-1\nline-2\n\n",
            enabled=True,
            user_id=1,
        )
        self.assertIsInstance(card_id, int)

        cards = self.db.get_all_cards(user_id=1, summary_only=True)
        target_card = next(card for card in cards if card["id"] == card_id)
        self.assertNotIn("api_config", target_card)
        self.assertNotIn("text_content", target_card)
        self.assertNotIn("data_content", target_card)
        self.assertEqual(2, target_card["data_count"])

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

    def test_count_keywords_by_image_url_is_scoped_by_account_id(self):
        self._save_account("acc-keyword-image-1")
        self._save_account("acc-keyword-image-2")

        self.assertTrue(
            self.db.save_image_keyword(
                "acc-keyword-image-1",
                "img-1",
                "/static/uploads/images/shared-demo.jpg",
            )
        )
        self.assertTrue(
            self.db.save_image_keyword(
                "acc-keyword-image-1",
                "img-2",
                "/static/uploads/images/shared-demo.jpg",
                "item-1",
            )
        )
        self.assertTrue(
            self.db.save_image_keyword(
                "acc-keyword-image-2",
                "img-3",
                "/static/uploads/images/shared-demo.jpg",
            )
        )

        self.assertEqual(
            self.db.count_keywords_by_image_url(
                "acc-keyword-image-1",
                "/static/uploads/images/shared-demo.jpg",
            ),
            2,
        )
        self.assertEqual(
            self.db.count_keywords_by_image_url(
                "acc-keyword-image-2",
                "/static/uploads/images/shared-demo.jpg",
            ),
            1,
        )
        self.assertEqual(
            self.db.count_keywords_by_image_url(
                "acc-keyword-image-1",
                "/static/uploads/images/missing-demo.jpg",
            ),
            0,
        )

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

    def test_get_keyword_counts_uses_user_scope_and_account_id_keys(self):
        self.assertTrue(self.db.create_user("keyword-count-user-1", "keyword-count-user-1@example.com", "pw-1"))
        self.assertTrue(self.db.create_user("keyword-count-user-2", "keyword-count-user-2@example.com", "pw-2"))

        first_user = self.db.get_user_by_username("keyword-count-user-1")
        second_user = self.db.get_user_by_username("keyword-count-user-2")
        self.assertIsNotNone(first_user)
        self.assertIsNotNone(second_user)

        self.assertTrue(self.db.save_cookie("acc-keyword-count-1", "a=1", user_id=first_user["id"]))
        self.assertTrue(self.db.save_cookie("acc-keyword-count-2", "b=2", user_id=second_user["id"]))
        self.assertTrue(self.db.save_keywords("acc-keyword-count-1", [("hello", "world"), ("price", "99")]))
        self.assertTrue(self.db.save_image_keyword("acc-keyword-count-1", "image-hello", "/static/uploads/images/demo-1.png"))
        self.assertTrue(self.db.save_keywords("acc-keyword-count-2", [("bye", "moon")]))

        all_counts = self.db.get_keyword_counts()
        self.assertEqual(
            all_counts,
            {
                "acc-keyword-count-1": 3,
                "acc-keyword-count-2": 1,
            },
        )

        scoped_counts = self.db.get_keyword_counts(user_id=first_user["id"])
        self.assertEqual(
            scoped_counts,
            {
                "acc-keyword-count-1": 3,
            },
        )

    def test_control_plane_methods_expose_account_id_contract(self):
        expected_params = {
            "get_account_ids": ["user_id"],
            "get_cookie_list_metadata": ["account_id"],
            "count_items_by_account": ["account_id"],
            "get_keywords": ["account_id"],
            "get_keywords_with_item_id": ["account_id"],
            "get_keywords_with_type": ["account_id"],
            "get_keyword_counts": ["user_id"],
            "count_keywords_by_image_url": ["account_id", "image_url"],
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

    def test_message_notifications_keep_disabled_channel_rows_visible_for_management(self):
        self._save_account("acc-notify-disabled-1")

        channel_id = self.db.create_notification_channel(
            "chan-disabled-visible-1",
            "webhook",
            "{}",
            user_id=1,
            enabled=False,
        )
        self.assertTrue(self.db.set_message_notification("acc-notify-disabled-1", channel_id, True))

        account_notifications = self.db.get_account_notifications("acc-notify-disabled-1")
        self.assertEqual(1, len(account_notifications))
        self.assertEqual(channel_id, account_notifications[0]["channel_id"])
        self.assertFalse(account_notifications[0]["channel_enabled"])

        all_notifications = self.db.get_all_message_notifications()
        self.assertIn("acc-notify-disabled-1", all_notifications)
        self.assertEqual(channel_id, all_notifications["acc-notify-disabled-1"][0]["channel_id"])
        self.assertFalse(all_notifications["acc-notify-disabled-1"][0]["channel_enabled"])

    def test_get_all_message_notifications_can_be_scoped_by_user_id(self):
        self.assertTrue(self.db.create_user("notify-owner-a", "notify-owner-a@example.com", "pw-a"))
        self.assertTrue(self.db.create_user("notify-owner-b", "notify-owner-b@example.com", "pw-b"))
        owner_a = self.db.get_user_by_username("notify-owner-a")
        owner_b = self.db.get_user_by_username("notify-owner-b")
        self.assertIsNotNone(owner_a)
        self.assertIsNotNone(owner_b)

        owner_a_id = owner_a["id"]
        owner_b_id = owner_b["id"]
        self.assertTrue(self.db.save_cookie("acc-notify-scope-a", "cookie=a", user_id=owner_a_id))
        self.assertTrue(self.db.save_cookie("acc-notify-scope-b", "cookie=b", user_id=owner_b_id))

        channel_a = self.db.create_notification_channel("chan-a", "webhook", "{}", user_id=owner_a_id)
        channel_b = self.db.create_notification_channel("chan-b", "webhook", "{}", user_id=owner_b_id)
        self.assertTrue(self.db.set_message_notification("acc-notify-scope-a", channel_a, True))
        self.assertTrue(self.db.set_message_notification("acc-notify-scope-b", channel_b, True))

        scoped_notifications = self.db.get_all_message_notifications(user_id=owner_a_id)
        self.assertEqual(["acc-notify-scope-a"], list(scoped_notifications.keys()))
        self.assertEqual(channel_a, scoped_notifications["acc-notify-scope-a"][0]["channel_id"])

    def test_default_reply_and_ai_settings_collections_can_be_scoped_by_user_id(self):
        self.assertTrue(self.db.create_user("scope-owner-a", "scope-owner-a@example.com", "pw-a"))
        self.assertTrue(self.db.create_user("scope-owner-b", "scope-owner-b@example.com", "pw-b"))
        owner_a = self.db.get_user_by_username("scope-owner-a")
        owner_b = self.db.get_user_by_username("scope-owner-b")
        self.assertIsNotNone(owner_a)
        self.assertIsNotNone(owner_b)

        owner_a_id = owner_a["id"]
        owner_b_id = owner_b["id"]
        self.assertTrue(self.db.save_cookie("acc-default-scope-a", "cookie=a", user_id=owner_a_id))
        self.assertTrue(self.db.save_cookie("acc-default-scope-b", "cookie=b", user_id=owner_b_id))
        self.db.save_default_reply("acc-default-scope-a", True, "reply-a", False)
        self.db.save_default_reply("acc-default-scope-b", True, "reply-b", False)
        self.db.save_ai_reply_settings("acc-default-scope-a", {"model_name": "model-a"})
        self.db.save_ai_reply_settings("acc-default-scope-b", {"model_name": "model-b"})

        scoped_replies = self.db.get_all_default_replies(user_id=owner_a_id)
        scoped_ai_settings = self.db.get_all_ai_reply_settings(user_id=owner_a_id)
        self.assertEqual(["acc-default-scope-a"], list(scoped_replies.keys()))
        self.assertEqual("reply-a", scoped_replies["acc-default-scope-a"]["reply_content"])
        self.assertEqual(["acc-default-scope-a"], list(scoped_ai_settings.keys()))
        self.assertEqual("model-a", scoped_ai_settings["acc-default-scope-a"]["model_name"])

    def test_get_cookie_list_metadata_exposes_lightweight_account_fields(self):
        self._save_account("acc-cookie-meta-1")
        self.assertTrue(
            self.db.update_cookie_account_info(
                "acc-cookie-meta-1",
                username="seller-demo",
                password="pw-demo",
            )
        )
        self.assertTrue(self.db.update_cookie_remark("acc-cookie-meta-1", "主账号"))
        self.assertTrue(self.db.update_cookie_pause_duration("acc-cookie-meta-1", 17))

        metadata = self.db.get_cookie_list_metadata("acc-cookie-meta-1")
        self.assertEqual(
            {
                "id": "acc-cookie-meta-1",
                "account_id": "acc-cookie-meta-1",
                "remark": "主账号",
                "pause_duration": 17,
                "username": "seller-demo",
                "has_password": True,
            },
            metadata,
        )

    def test_create_notification_channel_preserves_initial_enabled_state(self):
        channel_id = self.db.create_notification_channel(
            "chan-disabled-1",
            "webhook",
            "{}",
            user_id=1,
            enabled=False,
        )

        channel = self.db.get_notification_channel(channel_id, user_id=1)
        self.assertIsNotNone(channel)
        self.assertFalse(channel["enabled"])

    def test_delete_notification_channel_cleans_message_notifications(self):
        self._save_account("acc-channel-delete-1")

        channel_id = self.db.create_notification_channel(
            "chan-delete-1",
            "webhook",
            "{}",
            user_id=1,
        )
        self.assertTrue(self.db.set_message_notification("acc-channel-delete-1", channel_id, True))
        self.assertEqual(self._count_rows("message_notifications", "channel_id", channel_id), 1)

        self.assertTrue(self.db.delete_notification_channel(channel_id, user_id=1))
        self.assertEqual(self._count_rows("notification_channels", "id", channel_id), 0)
        self.assertEqual(self._count_rows("message_notifications", "channel_id", channel_id), 0)

    def test_delete_notification_channel_with_wrong_owner_keeps_message_notifications(self):
        self.assertTrue(self.db.create_user("channel-owner", "channel-owner@example.com", "pw-owner"))
        self.assertTrue(self.db.create_user("channel-intruder", "channel-intruder@example.com", "pw-intruder"))

        owner_user = self.db.get_user_by_username("channel-owner")
        intruder_user = self.db.get_user_by_username("channel-intruder")
        self.assertIsNotNone(owner_user)
        self.assertIsNotNone(intruder_user)

        owner_user_id = owner_user["id"]
        intruder_user_id = intruder_user["id"]
        self.assertTrue(self.db.save_cookie("acc-channel-owner-1", "owner=1", user_id=owner_user_id))

        channel_id = self.db.create_notification_channel(
            "chan-owner-1",
            "webhook",
            "{}",
            user_id=owner_user_id,
        )
        self.assertTrue(self.db.set_message_notification("acc-channel-owner-1", channel_id, True))
        self.assertEqual(self._count_rows("message_notifications", "channel_id", channel_id), 1)

        self.assertFalse(self.db.delete_notification_channel(channel_id, user_id=intruder_user_id))
        self.assertEqual(self._count_rows("notification_channels", "id", channel_id), 1)
        self.assertEqual(self._count_rows("message_notifications", "channel_id", channel_id), 1)

    def test_notification_channel_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("notification channel exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_notification_channels(user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_notification_channel(1, user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_notification_channel(1, "chan", "{}", True, user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_notification_channel(1, user_id=1)

    def test_notification_channel_methods_reject_blank_name_and_invalid_type(self):
        with self.assertRaisesRegex(ValueError, "通知渠道名称不能为空"):
            self.db.create_notification_channel("   ", "webhook", "{}", user_id=1)

        with self.assertRaisesRegex(ValueError, "通知渠道类型无效"):
            self.db.create_notification_channel("demo", "bad-type", "{}", user_id=1)

        channel_id = self.db.create_notification_channel("  demo-channel  ", "ding_talk", "{}", user_id=1)
        self.assertIsInstance(channel_id, int)
        channel = self.db.get_notification_channel(channel_id, user_id=1)
        self.assertEqual("demo-channel", channel["name"])
        self.assertEqual("dingtalk", channel["type"])

        with self.assertRaisesRegex(ValueError, "通知渠道名称不能为空"):
            self.db.update_notification_channel(channel_id, "   ", "{}", True, user_id=1)

    def test_create_notification_channel_accepts_tg_alias_and_normalizes_to_telegram(self):
        channel_id = self.db.create_notification_channel("telegram-alias", "tg", "{}", user_id=1)
        self.assertIsInstance(channel_id, int)

        channel = self.db.get_notification_channel(channel_id, user_id=1)
        self.assertIsNotNone(channel)
        self.assertEqual("telegram", channel["type"])

    def test_create_notification_channel_accepts_dingding_alias_and_normalizes_to_dingtalk(self):
        channel_id = self.db.create_notification_channel("dingding-alias", "dingding", "{}", user_id=1)
        self.assertIsInstance(channel_id, int)

        channel = self.db.get_notification_channel(channel_id, user_id=1)
        self.assertIsNotNone(channel)
        self.assertEqual("dingtalk", channel["type"])

    def test_create_notification_channel_accepts_weixin_alias_and_normalizes_to_wechat(self):
        channel_id = self.db.create_notification_channel("weixin-alias", "weixin", "{}", user_id=1)
        self.assertIsInstance(channel_id, int)

        channel = self.db.get_notification_channel(channel_id, user_id=1)
        self.assertIsNotNone(channel)
        self.assertEqual("wechat", channel["type"])

    def test_message_notification_and_template_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("message notification exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.set_message_notification("acc-demo-1", 5, True)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_account_notifications("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_all_message_notifications()

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_account_notifications("acc-demo-1", user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_message_notification(11, user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_all_notification_templates(user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_notification_template("message", user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_notification_template("message", "demo template", user_id=1)

    def test_update_notification_template_rejects_blank_content(self):
        with self.assertRaisesRegex(ValueError, "通知模板内容不能为空"):
            self.db.update_notification_template("message", "   ", user_id=1)

    def test_replace_account_notifications_replaces_rows_atomically_for_same_account(self):
        self._save_account("acc-notify-1")
        first_channel_id = self.db.create_notification_channel("chan-1", "webhook", "{}", user_id=1)
        second_channel_id = self.db.create_notification_channel("chan-2", "email", "{}", user_id=1)

        replaced_count = self.db.replace_account_notifications(
            "acc-notify-1",
            [first_channel_id, second_channel_id],
            enabled=True,
            user_id=1,
        )
        self.assertEqual(2, replaced_count)
        notifications = self.db.get_account_notifications("acc-notify-1")
        self.assertEqual(
            {notification["channel_id"] for notification in notifications},
            {first_channel_id, second_channel_id},
        )

        replaced_count = self.db.replace_account_notifications(
            "acc-notify-1",
            [second_channel_id],
            enabled=False,
            user_id=1,
        )
        self.assertEqual(1, replaced_count)
        notifications = self.db.conn.execute(
            """
            SELECT channel_id, enabled
            FROM message_notifications
            WHERE account_id = ?
            ORDER BY channel_id
            """,
            ("acc-notify-1",),
        ).fetchall()
        self.assertEqual([(second_channel_id, 0)], notifications)

    def test_replace_account_notifications_rejects_empty_or_foreign_channel_ids(self):
        self._save_account("acc-notify-2")
        self.assertTrue(self.db.create_user("notify-other", "notify-other@example.com", "pw"))
        foreign_owner = self.db.get_user_by_username("notify-other")
        self.assertIsNotNone(foreign_owner)
        foreign_owner_id = foreign_owner["id"]
        foreign_channel_id = self.db.create_notification_channel("foreign-chan", "webhook", "{}", user_id=foreign_owner_id)

        with self.assertRaisesRegex(ValueError, "请选择通知渠道"):
            self.db.replace_account_notifications("acc-notify-2", [], enabled=True, user_id=1)

        with self.assertRaisesRegex(ValueError, "通知渠道不存在"):
            self.db.replace_account_notifications("acc-notify-2", [foreign_channel_id], enabled=True, user_id=1)

    def test_replace_user_menu_settings_updates_visibility_and_order_atomically(self):
        replaced_count = self.db.replace_user_menu_settings(
            1,
            '{"orders":false,"items":true}',
            '["dashboard","orders"]',
        )
        self.assertEqual(2, replaced_count)
        settings = self.db.get_user_settings(1)
        self.assertEqual('{"orders":false,"items":true}', settings["menu_visibility"]["value"])
        self.assertEqual('["dashboard","orders"]', settings["menu_order"]["value"])

        replaced_count = self.db.replace_user_menu_settings(
            1,
            '{"orders":true}',
            '["dashboard","system-settings"]',
        )
        self.assertEqual(2, replaced_count)
        settings = self.db.get_user_settings(1)
        self.assertEqual('{"orders":true}', settings["menu_visibility"]["value"])
        self.assertEqual('["dashboard","system-settings"]', settings["menu_order"]["value"])

    def test_system_and_user_setting_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("settings exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_system_setting("theme_color")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.set_system_setting("theme_color", "#0f172a", "主题色")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_all_system_settings()

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_user_settings(1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_user_setting(1, "theme_color")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.set_user_setting(1, "theme_color", "#0f172a", "主题色")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.replace_user_menu_settings(1, '{"orders":false}', '["dashboard"]')

    def test_user_management_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("user management exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_all_users()

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_user_by_id(1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_user_admin_status(1, True)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_user_and_data(1)

    def test_risk_control_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("risk control exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_risk_control_logs(account_id="acc-demo-1", limit=20)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_risk_control_logs_count(account_id="acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_slider_verification_session_stats(account_ids=["acc-demo-1"], range_key="all")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_risk_control_log(1)

    def test_admin_data_management_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("admin data management exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_table_data("orders")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_table_record("orders", "1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.clear_table_data("orders")

    def test_account_cookie_read_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("account cookie read exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_all_cookies(user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_cookie_details("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_cookie("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_cookie_by_id("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_cookie_binding_info("acc-demo-1")

    def test_account_cookie_mutation_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("account cookie mutation exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.save_cookie("acc-demo-1", "a=1; b=2", user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_cookie_account_info("acc-demo-1", cookie_value="a=1; b=2")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_cookie("acc-demo-1")

    def test_risk_control_log_mutation_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("risk control mutation exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.add_risk_control_log(
                    "acc-demo-1",
                    event_type="slider_captcha",
                    processing_status="processing",
                    event_description="unit-test",
                )

            with self.assertRaises(sqlite3.OperationalError):
                self.db.mark_stale_risk_control_logs_failed(timeout_minutes=15, account_id="acc-demo-1")

    def test_card_read_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("card read exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_all_cards(user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_card_by_id(1, user_id=1)

    def test_import_backup_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("backup import exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.import_backup({"data": {"cookies": {"columns": ["id"], "rows": []}}}, user_id=1)

    def test_ai_config_preset_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("ai preset exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_ai_config_presets(1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.save_ai_config_preset(1, "demo", "gpt-4o-mini", "", "", "openai")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_ai_config_preset(1, 11)

    def test_ai_reply_settings_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("ai settings exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.save_ai_reply_settings("acc-demo-1", {"ai_enabled": True})

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_ai_reply_settings("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_all_ai_reply_settings()

    def test_default_reply_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("default reply exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.save_default_reply("acc-demo-1", True, "hello", True)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_default_reply("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_all_default_replies()

            with self.assertRaises(sqlite3.OperationalError):
                self.db.clear_default_reply_records("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_default_reply("acc-demo-1")

    def test_cookie_manager_cache_source_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("cookie manager cache exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_all_keywords()

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_all_cookie_status()

    def test_account_settings_and_comment_template_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("account settings exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_cookie_status("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_auto_confirm("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_auto_comment("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_cookie_pause_duration("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_cookie_proxy_config("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_comment_templates("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_active_comment_template("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_auto_confirm("acc-demo-1", True)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_auto_comment("acc-demo-1", False)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_cookie_pause_duration("acc-demo-1", 10)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_cookie_remark("acc-demo-1", "remark")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_cookie_proxy_config("acc-demo-1", proxy_type="http")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.add_comment_template("acc-demo-1", "tmpl", "content", False)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_comment_template("acc-demo-1", 1, name="tmpl")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_comment_template("acc-demo-1", 1)

    def test_item_info_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("item info exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.save_item_info("acc-demo-1", "item-1", {"title": "demo"})

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_item_info("acc-demo-1", "item-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_items_by_account("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.batch_save_item_basic_info(
                    [{"account_id": "acc-demo-1", "item_id": "item-1", "item_title": "demo"}]
                )

            with self.assertRaises(sqlite3.OperationalError):
                self.db.batch_update_item_title_price(
                    [{"account_id": "acc-demo-1", "item_id": "item-1", "item_title": "demo"}]
                )

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_item_detail("acc-demo-1", "item-1", '{"demo": true}')

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_item_info("acc-demo-1", "item-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.batch_delete_item_info(
                    [{"account_id": "acc-demo-1", "item_id": "item-1"}]
                )

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_item_multi_spec_status("acc-demo-1", "item-1", True)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_item_multi_spec_status("acc-demo-1", "item-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_item_multi_quantity_delivery_status("acc-demo-1", "item-1", True)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_item_multi_quantity_delivery_status("acc-demo-1", "item-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.set_active_comment_template("acc-demo-1", 1)

    def test_order_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("order info exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.insert_or_update_order(
                    order_id="order-1",
                    item_id="item-1",
                    buyer_id="buyer-1",
                    account_id="acc-demo-1",
                    order_status="processing",
                )

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_order_by_id("order-1", account_id="acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_order_pre_refund_status("order-1", account_id="acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_orders_by_account("acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.delete_order("order-1", account_id="acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_order_yifan_status(
                    "order-1",
                    account_id="acc-demo-1",
                    yifan_orderno="yf-1",
                )

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_order_by_yifan_orderno("yf-1", account_id="acc-demo-1")

            with self.assertRaises(sqlite3.OperationalError):
                self.db.update_order_chat_id("order-1", "chat-1", account_id="acc-demo-1")

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
        self.assertTrue(self.db.create_user("delete-user-keep", "delete-user-keep@example.com", "pw-keep"))
        keep_user = self.db.get_user_by_username("delete-user-keep")
        self.assertIsNotNone(keep_user)
        keep_user_id = keep_user["id"]

        deleted_account_ids = ("acc-delete-user-1a", "acc-delete-user-1b")
        for account_id, prefix in (
            ("acc-delete-user-1a", "delete-user-1a"),
            ("acc-delete-user-1b", "delete-user-1b"),
            ("acc-delete-user-keep", "delete-user-keep"),
        ):
            owner_user_id = user_id if account_id in deleted_account_ids else keep_user_id
            self._seed_cleanup_fixture(account_id, owner_user_id, prefix)

        self.assertTrue(self.db.delete_user_and_data(user_id))

        account_scoped_tables = (
            "keywords",
            "cookie_status",
            "default_replies",
            "default_reply_records",
            "item_info",
            "item_replay",
            "comment_templates",
            "risk_control_logs",
            "ai_reply_settings",
            "ai_conversations",
            "orders",
            "scheduled_tasks",
            "delivery_logs",
            "delivery_finalization_states",
            "data_card_reservations",
            "message_notifications",
        )
        for account_id in deleted_account_ids:
            for table_name in account_scoped_tables:
                with self.subTest(table_name=table_name, account_id=account_id):
                    self.assertEqual(self._count_rows(table_name, "account_id", account_id), 0)

        for table_name in account_scoped_tables:
            with self.subTest(table_name=table_name, account_id="acc-delete-user-keep"):
                self.assertGreater(self._count_rows(table_name, "account_id", "acc-delete-user-keep"), 0)

        for table_name in ("user_settings", "cards", "delivery_rules", "notification_channels"):
            with self.subTest(table_name=table_name, user_id=user_id):
                self.assertEqual(self._count_rows(table_name, "user_id", user_id), 0)
            with self.subTest(table_name=table_name, user_id=keep_user_id):
                self.assertGreater(self._count_rows(table_name, "user_id", keep_user_id), 0)

        for table_name in ("scheduled_tasks", "delivery_logs"):
            with self.subTest(table_name=table_name, user_id=user_id):
                self.assertEqual(self._count_rows(table_name, "user_id", user_id), 0)
            with self.subTest(table_name=table_name, user_id=keep_user_id):
                self.assertGreater(self._count_rows(table_name, "user_id", keep_user_id), 0)

        self.assertEqual(self._count_rows("cookies", "user_id", user_id), 0)
        self.assertGreater(self._count_rows("cookies", "user_id", keep_user_id), 0)

        user_row = self.db.conn.execute(
            "SELECT COUNT(*) FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        self.assertEqual(user_row[0], 0)
        keep_user_row = self.db.conn.execute(
            "SELECT COUNT(*) FROM users WHERE id = ?",
            (keep_user_id,),
        ).fetchone()
        self.assertEqual(keep_user_row[0], 1)

    def test_delete_cookie_cleans_account_scoped_tables(self):
        self.assertTrue(self.db.create_user("delete-cookie-user", "delete-cookie-user@example.com", "pw-cookie"))
        user = self.db.get_user_by_username("delete-cookie-user")
        self.assertIsNotNone(user)
        user_id = user["id"]

        self._seed_cleanup_fixture("acc-delete-cookie-1", user_id, "delete-cookie-1")
        self._seed_cleanup_fixture("acc-delete-cookie-keep", user_id, "delete-cookie-keep")

        self.assertTrue(self.db.delete_cookie(account_id="acc-delete-cookie-1"))

        account_scoped_tables = (
            "keywords",
            "cookie_status",
            "default_replies",
            "default_reply_records",
            "message_notifications",
            "item_info",
            "item_replay",
            "comment_templates",
            "risk_control_logs",
            "ai_reply_settings",
            "ai_conversations",
            "orders",
            "scheduled_tasks",
            "delivery_logs",
            "delivery_finalization_states",
            "data_card_reservations",
        )
        for table_name in account_scoped_tables:
            with self.subTest(table_name=table_name, account_id="acc-delete-cookie-1"):
                self.assertEqual(self._count_rows(table_name, "account_id", "acc-delete-cookie-1"), 0)
            with self.subTest(table_name=table_name, account_id="acc-delete-cookie-keep"):
                self.assertGreater(self._count_rows(table_name, "account_id", "acc-delete-cookie-keep"), 0)

        for table_name in ("user_settings", "cards", "delivery_rules", "notification_channels"):
            with self.subTest(table_name=table_name):
                self.assertEqual(self._count_rows(table_name, "user_id", user_id), 2)

        self.assertEqual(self._count_rows("cookies", "user_id", user_id), 1)
        self.assertIsNone(self.db.get_cookie_by_id(account_id="acc-delete-cookie-1"))
        self.assertIsNotNone(self.db.get_cookie_by_id(account_id="acc-delete-cookie-keep"))

    def test_delete_cookie_rejects_cross_user_delete_when_user_id_mismatches(self):
        self.assertTrue(self.db.create_user("delete-owner", "delete-owner@example.com", "pw-owner"))
        self.assertTrue(self.db.create_user("delete-other", "delete-other@example.com", "pw-other"))
        owner = self.db.get_user_by_username("delete-owner")
        other = self.db.get_user_by_username("delete-other")
        self.assertIsNotNone(owner)
        self.assertIsNotNone(other)

        owner_id = owner["id"]
        other_id = other["id"]
        account_id = "acc-delete-guard-1"

        self._seed_cleanup_fixture(account_id, owner_id, "delete-guard-1")

        self.assertFalse(self.db.delete_cookie(account_id=account_id, user_id=other_id))
        self.assertIsNotNone(self.db.get_cookie_by_id(account_id=account_id))
        self.assertGreater(self._count_rows("keywords", "account_id", account_id), 0)

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

    def test_user_backup_roundtrip_preserves_user_scoped_configs_and_delivery_state(self):
        account_id = "acc-backup-scope-1"
        prefix = "backup-scope"
        self._seed_cleanup_fixture(account_id, 1, prefix)
        preset_id = self.db.save_ai_config_preset(
            1,
            f"{prefix}-preset",
            f"{prefix}-model",
            "sk-live",
            "https://api.example.com",
            "openai",
        )
        self.assertIsInstance(preset_id, int)

        backup = self.db.export_backup(user_id=1)
        for table_name in (
            "user_settings",
            "cards",
            "delivery_rules",
            "ai_config_presets",
            "orders",
            "scheduled_tasks",
            "delivery_logs",
            "delivery_finalization_states",
            "data_card_reservations",
        ):
            with self.subTest(table_name=table_name):
                self.assertIn(table_name, backup["data"])
                self.assertIn("columns", backup["data"][table_name])
                self.assertIn("rows", backup["data"][table_name])
                self.assertGreater(len(backup["data"][table_name]["rows"]), 0)

        stale_card_id = self.db.create_card(
            name=f"{prefix}-stale-card",
            card_type="text",
            text_content="stale-card-content",
            user_id=1,
        )
        self.assertIsNotNone(stale_card_id)
        self.assertIsNotNone(
            self.db.create_delivery_rule(
                keyword=f"{prefix}-stale-rule",
                card_id=stale_card_id,
                description="stale rule",
                user_id=1,
            )
        )
        self.assertTrue(
            self.db.set_user_setting(
                1,
                f"{prefix}-stale-setting",
                "stale-value",
                "stale-description",
            )
        )
        stale_preset_id = self.db.save_ai_config_preset(
            1,
            f"{prefix}-stale-preset",
            "stale-model",
            "sk-stale",
            "https://stale.example.com",
            "openai",
        )
        self.assertIsInstance(stale_preset_id, int)
        self.assertIsNotNone(
            self.db.create_scheduled_task(
                name=f"{prefix}-stale-task",
                task_type="item_polish",
                account_id=account_id,
                user_id=1,
                next_run_at="2026-01-02 00:00:00",
            )
        )
        self.assertTrue(
            self.db.insert_or_update_order(
                order_id=f"{prefix}-stale-order",
                item_id=f"{prefix}-item",
                buyer_id=f"{prefix}-buyer-stale",
                buyer_nick=f"{prefix}-buyer-stale",
                account_id=account_id,
                order_status="processing",
            )
        )
        self.assertIsNotNone(
            self.db.create_delivery_log(
                user_id=1,
                account_id=account_id,
                order_id=f"{prefix}-stale-order",
                item_id=f"{prefix}-item",
                buyer_id=f"{prefix}-buyer-stale",
                buyer_nick=f"{prefix}-buyer-stale",
                channel="manual",
                status="failed",
                reason=f"{prefix}-stale-log",
            )
        )
        self.assertTrue(
            self.db.upsert_delivery_finalization_state(
                order_id=f"{prefix}-stale-order",
                unit_index=1,
                account_id=account_id,
                item_id=f"{prefix}-item",
                buyer_id=f"{prefix}-buyer-stale",
                channel="manual",
                status="sent",
                delivery_meta={"source": "stale"},
            )
        )
        stale_data_card_id = self.db.create_card(
            name=f"{prefix}-stale-data-card",
            card_type="data",
            data_content="stale-line-1\nstale-line-2",
            user_id=1,
        )
        self.assertIsNotNone(stale_data_card_id)
        self.assertIsNotNone(
            self.db.reserve_batch_data(
                card_id=stale_data_card_id,
                order_id=f"{prefix}-stale-reservation-order",
                unit_index=1,
                account_id=account_id,
                buyer_id=f"{prefix}-buyer-stale",
                ttl_minutes=5,
            )
        )

        self.assertTrue(self.db.import_backup(backup, user_id=1))

        restored_cards = self.db.get_all_cards(user_id=1)
        restored_card_names = {card["name"] for card in restored_cards}
        self.assertIn(f"{prefix}-data-card", restored_card_names)
        self.assertNotIn(f"{prefix}-stale-card", restored_card_names)
        self.assertNotIn(f"{prefix}-stale-data-card", restored_card_names)

        restored_rules = self.db.get_all_delivery_rules(user_id=1)
        restored_rule_keywords = {rule["keyword"] for rule in restored_rules}
        self.assertIn(f"{prefix}-rule", restored_rule_keywords)
        self.assertNotIn(f"{prefix}-stale-rule", restored_rule_keywords)

        restored_settings = self.db.get_user_settings(1)
        self.assertIn(f"{prefix}-setting", restored_settings)
        self.assertEqual(restored_settings[f"{prefix}-setting"]["value"], f"{prefix}-value")
        self.assertNotIn(f"{prefix}-stale-setting", restored_settings)

        restored_presets = self.db.get_ai_config_presets(1)
        restored_preset_names = {preset["preset_name"] for preset in restored_presets}
        self.assertIn(f"{prefix}-preset", restored_preset_names)
        self.assertNotIn(f"{prefix}-stale-preset", restored_preset_names)

        restored_tasks = self.db.get_scheduled_tasks(user_id=1)
        restored_task_names = {task["name"] for task in restored_tasks}
        self.assertIn(f"{prefix}-task", restored_task_names)
        self.assertNotIn(f"{prefix}-stale-task", restored_task_names)

        restored_orders = self.db.get_orders_by_account(account_id, limit=50)
        restored_order_ids = {order["order_id"] for order in restored_orders}
        self.assertIn(f"{prefix}-order", restored_order_ids)
        self.assertNotIn(f"{prefix}-stale-order", restored_order_ids)

        restored_delivery_logs = self.db.get_recent_delivery_logs(1, limit=50)
        restored_delivery_log_reasons = {log["reason"] for log in restored_delivery_logs}
        self.assertIn(f"{prefix}-delivery-log", restored_delivery_log_reasons)
        self.assertNotIn(f"{prefix}-stale-log", restored_delivery_log_reasons)

        restored_states = self.db.get_delivery_finalization_states(f"{prefix}-order", account_id=account_id)
        self.assertEqual(len(restored_states), 1)
        self.assertEqual(restored_states[0]["buyer_id"], f"{prefix}-buyer")
        self.assertEqual(
            self.db.get_delivery_finalization_states(f"{prefix}-stale-order", account_id=account_id),
            [],
        )

        reservation_rows = self.db.conn.execute(
            """
            SELECT order_id
            FROM data_card_reservations
            WHERE account_id = ?
            ORDER BY order_id
            """,
            (account_id,),
        ).fetchall()
        self.assertEqual(
            [row[0] for row in reservation_rows],
            [f"{prefix}-reservation-order"],
        )

    def test_delivery_rule_read_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("delivery read exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_all_delivery_rules(user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_delivery_rule_by_id(1, user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_today_delivery_count(user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_recent_delivery_logs(user_id=1, limit=20)

    def test_delivery_rule_methods_reject_non_positive_delivery_count(self):
        card_id = self.db.create_card(
            name="delivery-card",
            card_type="text",
            text_content="hello",
            user_id=1,
        )
        self.assertIsNotNone(card_id)

        with self.assertRaisesRegex(ValueError, "发货数量必须为大于等于 1 的整数"):
            self.db.create_delivery_rule(
                keyword="demo",
                card_id=card_id,
                delivery_count=0,
                user_id=1,
            )

        rule_id = self.db.create_delivery_rule(
            keyword="demo",
            card_id=card_id,
            delivery_count=1,
            user_id=1,
        )
        self.assertIsNotNone(rule_id)

        with self.assertRaisesRegex(ValueError, "发货数量必须为大于等于 1 的整数"):
            self.db.update_delivery_rule(
                rule_id=rule_id,
                delivery_count=-1,
                user_id=1,
            )

    def test_delivery_rule_methods_reject_blank_keyword_after_trimming(self):
        card_id = self.db.create_card(
            name="delivery-keyword-card",
            card_type="text",
            text_content="hello",
            user_id=1,
        )
        self.assertIsNotNone(card_id)

        with self.assertRaisesRegex(ValueError, "发货规则关键词不能为空"):
            self.db.create_delivery_rule(
                keyword="   ",
                card_id=card_id,
                delivery_count=1,
                user_id=1,
            )

        rule_id = self.db.create_delivery_rule(
            keyword="  demo keyword  ",
            card_id=card_id,
            delivery_count=1,
            user_id=1,
        )
        self.assertIsNotNone(rule_id)
        created_rule = self.db.get_delivery_rule_by_id(rule_id, user_id=1)
        self.assertEqual("demo keyword", created_rule["keyword"])

        self.assertTrue(
            self.db.update_delivery_rule(
                rule_id=rule_id,
                keyword="  updated keyword  ",
                user_id=1,
            )
        )
        updated_rule = self.db.get_delivery_rule_by_id(rule_id, user_id=1)
        self.assertEqual("updated keyword", updated_rule["keyword"])

    def test_user_backup_import_remaps_autoincrement_ids_and_internal_relations_across_databases(self):
        source_db = db_module.DBManager(os.path.join(self.temp_dir.name, "backup-source.db"))
        target_db = db_module.DBManager(os.path.join(self.temp_dir.name, "backup-target.db"))
        try:
            self.assertTrue(
                source_db.create_user(
                    "backup-source-user",
                    "backup-source-user@example.com",
                    "pw-source",
                )
            )
            source_user = source_db.get_user_by_username("backup-source-user")
            self.assertIsNotNone(source_user)
            source_user_id = source_user["id"]
            source_account_id = "acc-backup-source-1"
            self.assertTrue(source_db.save_cookie(source_account_id, "src=1", user_id=source_user_id))

            source_channel_id = source_db.create_notification_channel(
                "source-channel",
                "webhook",
                '{"url":"https://source.example.com"}',
                user_id=source_user_id,
            )
            self.assertTrue(source_db.set_message_notification(source_account_id, source_channel_id, True))

            source_card_id = source_db.create_card(
                name="source-card",
                card_type="data",
                data_content="source-line-1\nsource-line-2",
                user_id=source_user_id,
            )
            self.assertIsNotNone(source_card_id)

            source_rule_id = source_db.create_delivery_rule(
                keyword="source-rule",
                card_id=source_card_id,
                description="source rule description",
                user_id=source_user_id,
            )
            self.assertIsNotNone(source_rule_id)
            self.assertIsNotNone(
                source_db.create_delivery_log(
                    user_id=source_user_id,
                    account_id=source_account_id,
                    order_id="source-order-1",
                    item_id="source-item-1",
                    buyer_id="source-buyer-1",
                    buyer_nick="source-buyer",
                    rule_id=source_rule_id,
                    rule_keyword="source-rule",
                    channel="manual",
                    status="failed",
                    reason="source-delivery-log",
                )
            )
            self.assertTrue(
                source_db.upsert_delivery_finalization_state(
                    order_id="source-order-1",
                    unit_index=1,
                    account_id=source_account_id,
                    item_id="source-item-1",
                    buyer_id="source-buyer-1",
                    channel="manual",
                    status="sent",
                    delivery_meta={"source": "backup-test"},
                )
            )
            self.assertIsNotNone(
                source_db.reserve_batch_data(
                    card_id=source_card_id,
                    order_id="source-reservation-order-1",
                    unit_index=1,
                    account_id=source_account_id,
                    buyer_id="source-buyer-1",
                    ttl_minutes=5,
                )
            )
            self.assertTrue(
                source_db.set_user_setting(
                    source_user_id,
                    "source-setting",
                    "source-value",
                    "source-description",
                )
            )
            self.assertIsInstance(
                source_db.save_ai_config_preset(
                    source_user_id,
                    "source-preset",
                    "source-model",
                    "sk-source",
                    "https://source-api.example.com",
                    "openai",
                ),
                int,
            )
            self.assertIsNotNone(
                source_db.create_scheduled_task(
                    name="source-task",
                    task_type="item_polish",
                    account_id=source_account_id,
                    user_id=source_user_id,
                    next_run_at="2026-01-03 00:00:00",
                )
            )

            backup = source_db.export_backup(user_id=source_user_id)

            admin_user = target_db.get_user_by_username("admin")
            self.assertIsNotNone(admin_user)
            admin_user_id = admin_user["id"]
            self.assertTrue(
                target_db.create_user(
                    "backup-target-user",
                    "backup-target-user@example.com",
                    "pw-target",
                )
            )
            target_user = target_db.get_user_by_username("backup-target-user")
            self.assertIsNotNone(target_user)
            target_user_id = target_user["id"]

            admin_account_id = "acc-backup-admin-1"
            self.assertTrue(target_db.save_cookie(admin_account_id, "admin=1", user_id=admin_user_id))
            admin_channel_id = target_db.create_notification_channel(
                "admin-channel",
                "webhook",
                '{"url":"https://admin.example.com"}',
                user_id=admin_user_id,
            )
            admin_card_id = target_db.create_card(
                name="admin-card",
                card_type="text",
                text_content="admin-card-content",
                user_id=admin_user_id,
            )
            admin_rule_id = target_db.create_delivery_rule(
                keyword="admin-rule",
                card_id=admin_card_id,
                description="admin rule description",
                user_id=admin_user_id,
            )

            self.assertEqual(source_channel_id, admin_channel_id)
            self.assertEqual(source_card_id, admin_card_id)
            self.assertEqual(source_rule_id, admin_rule_id)

            self.assertTrue(target_db.import_backup(backup, user_id=target_user_id))

            imported_cookie = target_db.get_cookie_by_id(source_account_id)
            self.assertIsNotNone(imported_cookie)
            imported_cookie_row = target_db.conn.execute(
                "SELECT user_id FROM cookies WHERE id = ?",
                (source_account_id,),
            ).fetchone()
            self.assertIsNotNone(imported_cookie_row)
            self.assertEqual(imported_cookie_row[0], target_user_id)

            imported_channels = target_db.get_notification_channels(target_user_id)
            self.assertEqual([channel["name"] for channel in imported_channels], ["source-channel"])
            imported_channel_id = imported_channels[0]["id"]
            self.assertNotEqual(imported_channel_id, admin_channel_id)

            imported_notifications = target_db.get_account_notifications(source_account_id)
            self.assertEqual(len(imported_notifications), 1)
            self.assertEqual(imported_notifications[0]["channel_name"], "source-channel")
            self.assertEqual(imported_notifications[0]["channel_id"], imported_channel_id)

            imported_cards = target_db.get_all_cards(user_id=target_user_id)
            self.assertEqual([card["name"] for card in imported_cards], ["source-card"])
            imported_card_id = imported_cards[0]["id"]
            self.assertNotEqual(imported_card_id, admin_card_id)

            imported_rules = target_db.get_all_delivery_rules(user_id=target_user_id)
            self.assertEqual([rule["keyword"] for rule in imported_rules], ["source-rule"])
            self.assertEqual(imported_rules[0]["card_name"], "source-card")
            self.assertEqual(imported_rules[0]["card_id"], imported_card_id)
            imported_rule_id = imported_rules[0]["id"]
            self.assertNotEqual(imported_rule_id, admin_rule_id)

            delivery_log_row = target_db.conn.execute(
                """
                SELECT rule_id, user_id
                FROM delivery_logs
                WHERE account_id = ?
                """,
                (source_account_id,),
            ).fetchone()
            self.assertIsNotNone(delivery_log_row)
            self.assertEqual(delivery_log_row[0], imported_rule_id)
            self.assertEqual(delivery_log_row[1], target_user_id)

            reservation_row = target_db.conn.execute(
                """
                SELECT card_id
                FROM data_card_reservations
                WHERE account_id = ?
                """,
                (source_account_id,),
            ).fetchone()
            self.assertIsNotNone(reservation_row)
            self.assertEqual(reservation_row[0], imported_card_id)

            imported_settings = target_db.get_user_settings(target_user_id)
            self.assertIn("source-setting", imported_settings)
            self.assertEqual(imported_settings["source-setting"]["value"], "source-value")

            imported_presets = target_db.get_ai_config_presets(target_user_id)
            self.assertEqual([preset["preset_name"] for preset in imported_presets], ["source-preset"])

            imported_tasks = target_db.get_scheduled_tasks(user_id=target_user_id)
            self.assertEqual([task["name"] for task in imported_tasks], ["source-task"])

            admin_channels = target_db.get_notification_channels(admin_user_id)
            self.assertEqual([channel["name"] for channel in admin_channels], ["admin-channel"])
            admin_cards = target_db.get_all_cards(user_id=admin_user_id)
            self.assertEqual([card["name"] for card in admin_cards], ["admin-card"])
            admin_rules = target_db.get_all_delivery_rules(user_id=admin_user_id)
            self.assertEqual([rule["keyword"] for rule in admin_rules], ["admin-rule"])
        finally:
            source_db.close()
            target_db.close()

    def test_user_backup_import_normalizes_legacy_notification_channel_type_aliases(self):
        for legacy_alias, canonical_type in (("tg", "telegram"), ("dingding", "dingtalk"), ("weixin", "wechat")):
            with self.subTest(legacy_alias=legacy_alias, canonical_type=canonical_type):
                source_db = db_module.DBManager(os.path.join(self.temp_dir.name, f"backup-alias-source-{legacy_alias}.db"))
                target_db = db_module.DBManager(os.path.join(self.temp_dir.name, f"backup-alias-target-{legacy_alias}.db"))
                try:
                    self.assertTrue(
                        source_db.create_user(
                            f"backup-alias-source-{legacy_alias}",
                            f"backup-alias-source-{legacy_alias}@example.com",
                            "pw-source",
                        )
                    )
                    source_user = source_db.get_user_by_username(f"backup-alias-source-{legacy_alias}")
                    self.assertIsNotNone(source_user)
                    source_user_id = source_user["id"]
                    source_account_id = f"acc-backup-alias-{legacy_alias}-1"
                    self.assertTrue(source_db.save_cookie(source_account_id, "src=1", user_id=source_user_id))

                    source_channel_type = (
                        "telegram"
                        if canonical_type == "telegram"
                        else "wechat"
                        if canonical_type == "wechat"
                        else "dingtalk"
                    )
                    source_channel_id = source_db.create_notification_channel(
                        f"source-channel-{legacy_alias}",
                        source_channel_type,
                        "{}",
                        user_id=source_user_id,
                    )
                    self.assertIsInstance(source_channel_id, int)
                    self.assertTrue(source_db.set_message_notification(source_account_id, source_channel_id, True))

                    backup = source_db.export_backup(user_id=source_user_id)
                    channel_columns = backup["data"]["notification_channels"]["columns"]
                    channel_type_index = channel_columns.index("type")
                    for row in backup["data"]["notification_channels"]["rows"]:
                        row[channel_type_index] = legacy_alias

                    self.assertTrue(
                        target_db.create_user(
                            f"backup-alias-target-{legacy_alias}",
                            f"backup-alias-target-{legacy_alias}@example.com",
                            "pw-target",
                        )
                    )
                    target_user = target_db.get_user_by_username(f"backup-alias-target-{legacy_alias}")
                    self.assertIsNotNone(target_user)
                    target_user_id = target_user["id"]

                    self.assertTrue(target_db.import_backup(backup, user_id=target_user_id))

                    imported_channels = target_db.get_notification_channels(target_user_id)
                    self.assertEqual(len(imported_channels), 1)
                    self.assertEqual(imported_channels[0]["type"], canonical_type)

                    imported_notifications = target_db.get_account_notifications(source_account_id)
                    self.assertEqual(len(imported_notifications), 1)
                    self.assertEqual(imported_notifications[0]["channel_type"], canonical_type)
                finally:
                    source_db.close()
                    target_db.close()

    def test_notification_templates_are_user_scoped_and_user_backup_roundtrip(self):
        self.assertTrue(self.db.create_user("notify-scope-user-2", "notify-scope-user-2@example.com", "pw-2"))
        second_user = self.db.get_user_by_username("notify-scope-user-2")
        self.assertIsNotNone(second_user)
        second_user_id = second_user["id"]

        self.assertTrue(
            self.db.update_notification_template(
                "message",
                "user-1 message template {account_id}",
                user_id=1,
            )
        )
        self.assertTrue(
            self.db.update_notification_template(
                "message",
                "user-2 message template {account_id}",
                user_id=second_user_id,
            )
        )
        self.assertTrue(
            self.db.update_notification_template(
                "delivery",
                "user-2 delivery template {result}",
                user_id=second_user_id,
            )
        )

        user_one_template = self.db.get_notification_template("message", user_id=1)
        user_two_template = self.db.get_notification_template("message", user_id=second_user_id)
        self.assertEqual(user_one_template["template"], "user-1 message template {account_id}")
        self.assertEqual(user_one_template["user_id"], 1)
        self.assertEqual(user_two_template["template"], "user-2 message template {account_id}")
        self.assertEqual(user_two_template["user_id"], second_user_id)

        backup = self.db.export_backup(user_id=1)
        self.assertIn("notification_templates", backup["data"])
        template_columns = backup["data"]["notification_templates"]["columns"]
        template_rows = backup["data"]["notification_templates"]["rows"]
        self.assertIn("user_id", template_columns)
        self.assertTrue(
            any(
                row[template_columns.index("type")] == "message"
                and row[template_columns.index("template")] == "user-1 message template {account_id}"
                for row in template_rows
            )
        )
        self.assertTrue(
            all(row[template_columns.index("user_id")] == 1 for row in template_rows)
        )

        self.assertTrue(
            self.db.update_notification_template(
                "message",
                "stale user-1 template",
                user_id=1,
            )
        )
        self.assertTrue(self.db.import_backup(backup, user_id=1))

        restored_user_one_template = self.db.get_notification_template("message", user_id=1)
        preserved_user_two_template = self.db.get_notification_template("message", user_id=second_user_id)
        self.assertEqual(
            restored_user_one_template["template"],
            "user-1 message template {account_id}",
        )
        self.assertEqual(
            preserved_user_two_template["template"],
            "user-2 message template {account_id}",
        )

    def test_delete_user_and_data_cleans_user_scoped_notification_templates(self):
        self.assertTrue(self.db.create_user("notify-delete-user", "notify-delete-user@example.com", "pw-delete"))
        deleted_user = self.db.get_user_by_username("notify-delete-user")
        self.assertIsNotNone(deleted_user)
        deleted_user_id = deleted_user["id"]

        self.assertTrue(self.db.create_user("notify-keep-user", "notify-keep-user@example.com", "pw-keep"))
        keep_user = self.db.get_user_by_username("notify-keep-user")
        self.assertIsNotNone(keep_user)
        keep_user_id = keep_user["id"]

        self.assertTrue(
            self.db.update_notification_template(
                "message",
                "delete user template",
                user_id=deleted_user_id,
            )
        )
        self.assertTrue(
            self.db.update_notification_template(
                "message",
                "keep user template",
                user_id=keep_user_id,
            )
        )

        self.assertTrue(self.db.delete_user_and_data(deleted_user_id))
        self.assertIsNone(self.db.get_notification_template("message", user_id=deleted_user_id))
        kept_template = self.db.get_notification_template("message", user_id=keep_user_id)
        self.assertIsNotNone(kept_template)
        self.assertEqual(kept_template["template"], "keep user template")

    def test_notification_templates_legacy_global_schema_is_rebuilt_with_user_scope(self):
        legacy_db_path = os.path.join(self.temp_dir.name, "legacy-notification-templates.db")

        bootstrap_db = db_module.DBManager(legacy_db_path)
        try:
            self.assertTrue(
                bootstrap_db.create_user(
                    "legacy-notify-user",
                    "legacy-notify-user@example.com",
                    "pw-legacy",
                )
            )
        finally:
            bootstrap_db.close()

        conn = sqlite3.connect(legacy_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("DROP TABLE notification_templates")
            cursor.execute(
                """
                CREATE TABLE notification_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL UNIQUE CHECK (type IN ('message', 'token_refresh', 'delivery', 'slider_success', 'face_verify', 'password_login_success')),
                    template TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "INSERT INTO notification_templates (type, template) VALUES (?, ?)",
                ("message", "legacy message template {account_id}"),
            )
            conn.commit()
        finally:
            conn.close()

        migrated_db = db_module.DBManager(legacy_db_path)
        try:
            cursor = migrated_db.conn.cursor()
            cursor.execute("PRAGMA table_info(notification_templates)")
            columns = [row[1] for row in cursor.fetchall()]
            self.assertIn("user_id", columns)

            admin_user = migrated_db.get_user_by_username("admin")
            legacy_user = migrated_db.get_user_by_username("legacy-notify-user")
            self.assertIsNotNone(admin_user)
            self.assertIsNotNone(legacy_user)

            for scoped_user in (admin_user, legacy_user):
                with self.subTest(user_id=scoped_user["id"]):
                    message_template = migrated_db.get_notification_template(
                        "message",
                        user_id=scoped_user["id"],
                    )
                    self.assertIsNotNone(message_template)
                    self.assertEqual(
                        message_template["template"],
                        "legacy message template {account_id}",
                    )
                    self.assertEqual(message_template["user_id"], scoped_user["id"])
                    self.assertIsNotNone(
                        migrated_db.get_notification_template(
                            "cookie_refresh_success",
                            user_id=scoped_user["id"],
                        )
                    )
        finally:
            migrated_db.close()

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

        proxy_config = self.db.get_cookie_proxy_config(account_id="acc-credential-1")
        self.assertEqual(proxy_config["proxy_type"], "http")
        self.assertEqual(proxy_config["proxy_host"], "127.0.0.1")
        self.assertEqual(proxy_config["proxy_port"], 7890)
        self.assertEqual(proxy_config["proxy_user"], "proxy-user")
        self.assertEqual(proxy_config["proxy_pass"], "proxy-pass")

        self.assertTrue(self.db.delete_cookie(account_id="acc-credential-1"))
        self.assertIsNone(self.db.get_cookie_by_id(account_id="acc-credential-1"))

    def test_delete_pending_cookie_placeholder_only_removes_empty_pending_records(self):
        self.assertTrue(
            self.db.create_cookie_account_placeholder(
                account_id="acc-placeholder-empty-1",
                user_id=1,
                bind_status="pending_bind",
            )
        )
        self.assertTrue(
            self.db.delete_pending_cookie_placeholder(
                account_id="acc-placeholder-empty-1",
                user_id=1,
            )
        )
        self.assertIsNone(self.db.get_cookie_binding_info("acc-placeholder-empty-1"))

        self.assertTrue(
            self.db.create_cookie_account_placeholder(
                account_id="acc-placeholder-live-1",
                user_id=1,
                bind_status="pending_bind",
            )
        )
        self.assertTrue(
            self.db.save_cookie(
                account_id="acc-placeholder-live-1",
                cookie_value="a=1; b=2",
                user_id=1,
            )
        )
        self.assertFalse(
            self.db.delete_pending_cookie_placeholder(
                account_id="acc-placeholder-live-1",
                user_id=1,
            )
        )
        self.assertIsNotNone(self.db.get_cookie_binding_info("acc-placeholder-live-1"))

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

    def test_risk_control_logs_legacy_schema_is_rebuilt_with_account_id(self):
        legacy_db_path = os.path.join(self.temp_dir.name, "legacy-risk.db")

        bootstrap_db = db_module.DBManager(legacy_db_path)
        try:
            self.assertTrue(bootstrap_db.save_cookie("acc-risk-legacy-1", "a=1; b=2", user_id=1))
        finally:
            bootstrap_db.close()

        legacy_link_column = "".join(["cookie", "_id"])
        conn = sqlite3.connect(legacy_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("DROP TABLE risk_control_logs")
            cursor.execute(
                f"""
                CREATE TABLE risk_control_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    {legacy_link_column} TEXT NOT NULL,
                    event_type TEXT NOT NULL DEFAULT 'slider_captcha',
                    session_id TEXT,
                    trigger_scene TEXT,
                    result_code TEXT,
                    event_description TEXT,
                    event_meta TEXT,
                    processing_result TEXT,
                    processing_status TEXT DEFAULT 'processing',
                    error_message TEXT,
                    duration_ms INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    account_id TEXT,
                    FOREIGN KEY ({legacy_link_column}) REFERENCES cookies(id) ON DELETE CASCADE
                )
                """
            )
            cursor.execute(
                f"""
                INSERT INTO risk_control_logs (
                    {legacy_link_column}, event_type, session_id, event_description, processing_status, account_id
                ) VALUES (?, ?, ?, ?, ?, NULL)
                """,
                ("acc-risk-legacy-1", "slider_captcha", "legacy-session-1", "legacy-row", "processing"),
            )
            conn.commit()
        finally:
            conn.close()

        migrated_db = db_module.DBManager(legacy_db_path)
        try:
            cursor = migrated_db.conn.cursor()
            cursor.execute("PRAGMA table_info(risk_control_logs)")
            columns = [row[1] for row in cursor.fetchall()]
            self.assertIn("account_id", columns)
            self.assertNotIn(legacy_link_column, columns)

            logs = migrated_db.get_risk_control_logs(account_id="acc-risk-legacy-1", limit=10)
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0]["account_id"], "acc-risk-legacy-1")
            self.assertEqual(logs[0]["event_description"], "legacy-row")

            log_id = migrated_db.add_risk_control_log(
                account_id="acc-risk-legacy-1",
                event_type="slider_captcha",
                processing_status="processing",
                event_description="post-migration-row",
            )
            self.assertIsNotNone(log_id)
        finally:
            migrated_db.close()

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

    def test_delivery_finalization_read_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("delivery finalization exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_delivery_finalization_state(
                    "order-delivery-exploded-1",
                    1,
                    account_id="acc-delivery-exploded-1",
                )

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_delivery_finalization_states(
                    "order-delivery-exploded-1",
                    account_id="acc-delivery-exploded-1",
                )

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_delivery_progress_summary(
                    "order-delivery-exploded-1",
                    account_id="acc-delivery-exploded-1",
                    expected_quantity=2,
                )

    def test_scheduled_task_methods_re_raise_database_failures(self):
        broken_conn = mock.Mock()
        broken_conn.cursor.side_effect = sqlite3.OperationalError("scheduled task exploded")

        with mock.patch.object(self.db, "conn", broken_conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_scheduled_tasks(user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_scheduled_task(1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_scheduled_task_by_account("acc-scheduled-exploded-1", user_id=1)

            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_due_tasks()

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

    def test_delete_table_record_preserves_admin_password_hash_system_setting(self):
        self.db.conn.execute(
            """
            INSERT OR REPLACE INTO system_settings (key, value, description)
            VALUES (?, ?, ?)
            """,
            ("admin_password_hash", "legacy-hash", "管理员密码哈希"),
        )
        self.db.conn.execute(
            """
            INSERT OR REPLACE INTO system_settings (key, value, description)
            VALUES (?, ?, ?)
            """,
            ("theme_color", "#111827", "主题颜色"),
        )
        self.db.conn.commit()

        protected_rowid = self.db.conn.execute(
            "SELECT rowid FROM system_settings WHERE key = ?",
            ("admin_password_hash",),
        ).fetchone()[0]
        normal_rowid = self.db.conn.execute(
            "SELECT rowid FROM system_settings WHERE key = ?",
            ("theme_color",),
        ).fetchone()[0]

        self.assertEqual("admin_password_hash", self.db.get_system_setting_key_by_rowid(str(protected_rowid)))
        self.assertFalse(self.db.delete_table_record("system_settings", str(protected_rowid)))
        self.assertEqual(
            1,
            self.db.conn.execute(
                "SELECT COUNT(*) FROM system_settings WHERE key = ?",
                ("admin_password_hash",),
            ).fetchone()[0],
        )

        self.assertTrue(self.db.delete_table_record("system_settings", str(normal_rowid)))
        self.assertEqual(
            0,
            self.db.conn.execute(
                "SELECT COUNT(*) FROM system_settings WHERE key = ?",
                ("theme_color",),
            ).fetchone()[0],
        )

    def test_get_table_data_hides_admin_password_hash_system_setting(self):
        self.db.set_system_setting("admin_password_hash", "hash-value", "管理员密码哈希")
        self.db.set_system_setting("smtp_password", "smtp-secret", "SMTP密码")
        self.db.set_system_setting("qq_reply_secret_key", "qq-secret", "QQ秘钥")
        self.db.set_system_setting("site_name", "闲鱼助手", "站点名称")

        table_data, columns = self.db.get_table_data("system_settings")

        self.assertIn("key", columns)
        visible_keys = {str(row.get("key")) for row in table_data}
        self.assertIn("site_name", visible_keys)
        self.assertNotIn("admin_password_hash", visible_keys)
        self.assertNotIn("smtp_password", visible_keys)
        self.assertNotIn("qq_reply_secret_key", visible_keys)

    def test_import_backup_restores_missing_default_system_settings_on_full_restore(self):
        self.db.set_system_setting("admin_password_hash", "hash-value", "管理员密码哈希")
        self.db.set_system_setting("theme_color", "#111827", "主题颜色")
        self.db.set_system_setting("smtp_server", "smtp.example.com", "SMTP服务器地址")
        self.db.set_system_setting("auto_comment_api_url", "https://api.example.com/comment", "自动好评辅助API地址")

        legacy_backup = {
            "data": {
                "system_settings": {
                    "columns": ["key", "value", "description"],
                    "rows": [
                        ["theme_color", "#0f172a", "主题颜色"],
                    ],
                }
            }
        }

        self.assertTrue(self.db.import_backup(legacy_backup))

        restored_settings = self.db.get_all_system_settings()
        self.assertEqual("hash-value", restored_settings["admin_password_hash"])
        self.assertEqual("#0f172a", restored_settings["theme_color"])
        self.assertIn("smtp_server", restored_settings)
        self.assertEqual("", restored_settings["smtp_server"])
        self.assertIn("auto_comment_api_url", restored_settings)
        self.assertEqual("", restored_settings["auto_comment_api_url"])


if __name__ == "__main__":
    unittest.main()
