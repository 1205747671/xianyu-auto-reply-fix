from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from db_manager import DBManager

REPO_ROOT = Path(__file__).resolve().parents[1]


class AdminDataManagementContractsTest(unittest.TestCase):
    def test_item_replay_admin_delete_uses_row_primary_key(self):
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "admin_data_management_test.db"
            manager = DBManager(str(db_path))
            try:
                cursor = manager.conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO item_replay (item_id, account_id, reply_content)
                    VALUES (?, ?, ?)
                    """,
                    ("item-abc", "acc-demo", "reply-demo"),
                )
                manager.conn.commit()
                record_id = cursor.lastrowid

                deleted = manager.delete_table_record("item_replay", str(record_id))
                remaining = cursor.execute(
                    "SELECT COUNT(*) FROM item_replay WHERE id = ?",
                    (record_id,),
                ).fetchone()[0]

                self.assertTrue(deleted)
                self.assertEqual(remaining, 0)
            finally:
                manager.close()

    def test_cookie_admin_delete_cleans_account_scoped_rows(self):
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "admin_data_management_cookie_delete.db"
            manager = DBManager(str(db_path))
            try:
                self.assertTrue(manager.create_user("cookie-delete-user", "cookie-delete-user@example.com", "pw"))
                user = manager.get_user_by_username("cookie-delete-user")
                self.assertIsNotNone(user)

                self.assertTrue(manager.save_cookie("acc-admin-delete-1", "k=v", user_id=user["id"]))
                self.assertTrue(manager.update_item_reply("acc-admin-delete-1", "item-1", "reply-content"))

                cookie_rows, _ = manager.get_table_data("cookies")
                cookie_row = next(row for row in cookie_rows if row["id"] == "acc-admin-delete-1")

                deleted = manager.delete_table_record("cookies", str(cookie_row["__admin_rowid"]))

                remaining_cookie = manager.conn.execute(
                    "SELECT COUNT(*) FROM cookies WHERE id = ?",
                    ("acc-admin-delete-1",),
                ).fetchone()[0]
                remaining_item_reply = manager.conn.execute(
                    "SELECT COUNT(*) FROM item_replay WHERE account_id = ?",
                    ("acc-admin-delete-1",),
                ).fetchone()[0]

                self.assertTrue(deleted)
                self.assertEqual(remaining_cookie, 0)
                self.assertEqual(remaining_item_reply, 0)
            finally:
                manager.close()

    def test_user_admin_delete_cleans_user_and_account_scoped_rows(self):
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "admin_data_management_user_delete.db"
            manager = DBManager(str(db_path))
            try:
                self.assertTrue(manager.create_user("user-delete-user", "user-delete-user@example.com", "pw"))
                user = manager.get_user_by_username("user-delete-user")
                self.assertIsNotNone(user)
                user_id = user["id"]

                self.assertTrue(manager.save_cookie("acc-admin-user-delete-1", "k=v", user_id=user_id))
                manager.set_user_setting(user_id, "demo-setting", "demo-value")
                self.assertTrue(manager.update_item_reply("acc-admin-user-delete-1", "item-1", "reply-content"))

                user_rows, _ = manager.get_table_data("users")
                user_row = next(row for row in user_rows if row["id"] == user_id)

                deleted = manager.delete_table_record("users", str(user_row["__admin_rowid"]))

                remaining_user = manager.conn.execute(
                    "SELECT COUNT(*) FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()[0]
                remaining_cookie = manager.conn.execute(
                    "SELECT COUNT(*) FROM cookies WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0]
                remaining_setting = manager.conn.execute(
                    "SELECT COUNT(*) FROM user_settings WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0]
                remaining_item_reply = manager.conn.execute(
                    "SELECT COUNT(*) FROM item_replay WHERE account_id = ?",
                    ("acc-admin-user-delete-1",),
                ).fetchone()[0]

                self.assertTrue(deleted)
                self.assertEqual(remaining_user, 0)
                self.assertEqual(remaining_cookie, 0)
                self.assertEqual(remaining_setting, 0)
                self.assertEqual(remaining_item_reply, 0)
            finally:
                manager.close()

    def test_cookie_admin_clear_cleans_account_scoped_rows(self):
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "admin_data_management_cookie_clear.db"
            manager = DBManager(str(db_path))
            try:
                self.assertTrue(manager.create_user("cookie-clear-user", "cookie-clear-user@example.com", "pw"))
                user = manager.get_user_by_username("cookie-clear-user")
                self.assertIsNotNone(user)

                self.assertTrue(manager.save_cookie("acc-admin-clear-1", "k=v", user_id=user["id"]))
                self.assertTrue(manager.update_item_reply("acc-admin-clear-1", "item-1", "reply-content"))

                cleared = manager.clear_table_data("cookies")

                remaining_cookie = manager.conn.execute(
                    "SELECT COUNT(*) FROM cookies WHERE id = ?",
                    ("acc-admin-clear-1",),
                ).fetchone()[0]
                remaining_item_reply = manager.conn.execute(
                    "SELECT COUNT(*) FROM item_replay WHERE account_id = ?",
                    ("acc-admin-clear-1",),
                ).fetchone()[0]

                self.assertTrue(cleared)
                self.assertEqual(remaining_cookie, 0)
                self.assertEqual(remaining_item_reply, 0)
            finally:
                manager.close()

    def test_card_admin_delete_cleans_delivery_rules_via_foreign_key_cascade(self):
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "admin_data_management_card_delete.db"
            manager = DBManager(str(db_path))
            try:
                card_id = manager.create_card(
                    name="admin-card-delete",
                    card_type="text",
                    text_content="hello",
                    user_id=1,
                )
                self.assertIsNotNone(card_id)

                rule_id = manager.create_delivery_rule(
                    keyword="admin-card-delete-rule",
                    card_id=card_id,
                    user_id=1,
                )
                self.assertIsNotNone(rule_id)

                card_rows, _ = manager.get_table_data("cards")
                card_row = next(row for row in card_rows if row["id"] == card_id)

                deleted = manager.delete_table_record("cards", str(card_row["__admin_rowid"]))
                remaining_card = manager.conn.execute(
                    "SELECT COUNT(*) FROM cards WHERE id = ?",
                    (card_id,),
                ).fetchone()[0]
                remaining_rule = manager.conn.execute(
                    "SELECT COUNT(*) FROM delivery_rules WHERE id = ?",
                    (rule_id,),
                ).fetchone()[0]

                self.assertTrue(deleted)
                self.assertEqual(remaining_card, 0)
                self.assertEqual(remaining_rule, 0)
            finally:
                manager.close()

    def test_notification_channel_admin_delete_cleans_message_notifications_via_foreign_key_cascade(self):
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "admin_data_management_channel_delete.db"
            manager = DBManager(str(db_path))
            try:
                self.assertTrue(manager.save_cookie("acc-admin-channel-delete-1", "k=v", user_id=1))
                channel_id = manager.create_notification_channel(
                    "admin-channel-delete",
                    "webhook",
                    "{}",
                    user_id=1,
                )
                self.assertIsInstance(channel_id, int)
                self.assertTrue(manager.set_message_notification("acc-admin-channel-delete-1", channel_id, True))

                channel_rows, _ = manager.get_table_data("notification_channels")
                channel_row = next(row for row in channel_rows if row["id"] == channel_id)

                deleted = manager.delete_table_record(
                    "notification_channels",
                    str(channel_row["__admin_rowid"]),
                )
                remaining_channel = manager.conn.execute(
                    "SELECT COUNT(*) FROM notification_channels WHERE id = ?",
                    (channel_id,),
                ).fetchone()[0]
                remaining_notification = manager.conn.execute(
                    "SELECT COUNT(*) FROM message_notifications WHERE channel_id = ?",
                    (channel_id,),
                ).fetchone()[0]

                self.assertTrue(deleted)
                self.assertEqual(remaining_channel, 0)
                self.assertEqual(remaining_notification, 0)
            finally:
                manager.close()

    def test_system_settings_admin_clear_preserves_admin_password_hash(self):
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "admin_data_management_system_settings.db"
            manager = DBManager(str(db_path))
            try:
                manager.set_system_setting("admin_password_hash", "hash-value")
                manager.set_system_setting("demo_setting", "demo-value")

                cleared = manager.clear_table_data("system_settings")
                remaining_settings = dict(
                    manager.conn.execute("SELECT key, value FROM system_settings").fetchall()
                )

                self.assertTrue(cleared)
                self.assertEqual({"admin_password_hash": "hash-value"}, remaining_settings)
            finally:
                manager.close()

    def test_system_settings_admin_table_data_hides_sensitive_secret_rows(self):
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "admin_data_management_system_settings_hidden.db"
            manager = DBManager(str(db_path))
            try:
                manager.set_system_setting("admin_password_hash", "hash-value")
                manager.set_system_setting("smtp_password", "smtp-secret")
                manager.set_system_setting("qq_reply_secret_key", "qq-secret")
                manager.set_system_setting("smtp_server", "smtp.example.com")

                rows, columns = manager.get_table_data("system_settings")

                self.assertIn("key", columns)
                visible_keys = {str(row.get("key")) for row in rows}
                self.assertIn("smtp_server", visible_keys)
                self.assertNotIn("admin_password_hash", visible_keys)
                self.assertNotIn("smtp_password", visible_keys)
                self.assertNotIn("qq_reply_secret_key", visible_keys)
            finally:
                manager.close()

    def test_risk_control_logs_table_is_exposed_consistently_in_admin_data_management(self):
        index_source = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        app_source = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        reply_server_source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn(
            '<option value="risk_control_logs">risk_control_logs - 风控日志表</option>',
            index_source,
        )
        self.assertIn("'risk_control_logs': '风控日志表'", app_source)
        self.assertIn("'risk_control_logs'", reply_server_source)

    def test_admin_data_management_allows_new_account_scoped_tables(self):
        reply_server_source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("ADMIN_DATA_ALLOWED_TABLES = [", reply_server_source)
        for table_name in (
            "notification_templates",
            "scheduled_tasks",
            "delivery_logs",
            "delivery_finalization_states",
            "data_card_reservations",
            "comment_templates",
            "ai_config_presets",
        ):
            with self.subTest(table_name=table_name):
                self.assertIn(f"'{table_name}'", reply_server_source)


if __name__ == "__main__":
    unittest.main()
