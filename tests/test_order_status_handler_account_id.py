import sys
import time
import types
import unittest
from unittest import mock


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


if "loguru" not in sys.modules:
    sys.modules["loguru"] = types.SimpleNamespace(logger=_LoggerStub())


import order_status_handler


class OrderStatusHandlerAccountIdTest(unittest.TestCase):
    def setUp(self):
        self.handler = order_status_handler.OrderStatusHandler()

    def test_handle_system_message_writes_account_id_to_pending_structures(self):
        with mock.patch.object(
            self.handler,
            "_resolve_system_message_status",
            return_value=(
                "completed",
                {"is_system_message": True},
                [{"status": "completed", "source": "send_message", "text": "交易成功"}],
            ),
        ):
            with mock.patch.object(self.handler, "extract_order_id", return_value=None):
                with mock.patch.object(
                    self.handler,
                    "_normalize_pending_match_context",
                    return_value={},
                ):
                    handled = self.handler.handle_system_message(
                        message={},
                        send_message="交易成功",
                        account_id="acc-system-1",
                        msg_time="10:00:00",
                    )

        self.assertTrue(handled)
        temp_order_id = next(iter(self.handler.pending_updates))
        pending_update = self.handler.pending_updates[temp_order_id][0]
        self.assertEqual(pending_update["account_id"], "acc-system-1")
        self.assertNotIn("cookie_id", pending_update)

        pending_message = self.handler._pending_system_messages["acc-system-1"][0]
        self.assertEqual(pending_message["account_id"], "acc-system-1")
        self.assertNotIn("cookie_id", pending_message)

    def test_handle_red_reminder_message_writes_account_id_to_pending_structures(self):
        with mock.patch.object(self.handler, "extract_order_id", return_value=None):
            with mock.patch.object(
                self.handler,
                "_normalize_pending_match_context",
                return_value={},
            ):
                with mock.patch.object(
                    self.handler,
                    "_try_resolve_cancelled_message_without_order_id",
                    return_value=False,
                ):
                    handled = self.handler.handle_red_reminder_message(
                        message={},
                        red_reminder="交易关闭",
                        user_id="user-1",
                        account_id="acc-red-1",
                        msg_time="11:00:00",
                    )

        self.assertTrue(handled)
        temp_order_id = next(iter(self.handler.pending_updates))
        pending_update = self.handler.pending_updates[temp_order_id][0]
        self.assertEqual(pending_update["account_id"], "acc-red-1")
        self.assertNotIn("cookie_id", pending_update)

        pending_message = self.handler._pending_red_reminder_messages["acc-red-1"][0]
        self.assertEqual(pending_message["account_id"], "acc-red-1")
        self.assertNotIn("cookie_id", pending_message)

    def test_process_pending_updates_only_reads_account_id_field(self):
        forwarded_calls = []

        def _fake_update_order_status(**kwargs):
            forwarded_calls.append(kwargs)
            return True

        self.handler.update_order_status = _fake_update_order_status
        self.handler._add_to_pending_updates(
            order_id="order-legacy",
            new_status="shipped",
            account_id="acc-pending-1",
            context="legacy pending update",
        )

        processed = self.handler.process_pending_updates(
            "order-legacy",
            account_id="acc-pending-1",
        )

        self.assertTrue(processed)
        self.assertEqual(forwarded_calls[0]["account_id"], "acc-pending-1")
        self.assertNotIn("cookie_id", forwarded_calls[0])

    def test_process_pending_updates_keeps_account_boundary_for_same_order_id(self):
        forwarded_calls = []

        def _fake_update_order_status(**kwargs):
            forwarded_calls.append(kwargs)
            return True

        self.handler.update_order_status = _fake_update_order_status
        self.handler._add_to_pending_updates(
            order_id="order-shared-pending",
            new_status="shipped",
            account_id="acc-pending-a",
            context="pending update A",
        )
        self.handler._add_to_pending_updates(
            order_id="order-shared-pending",
            new_status="completed",
            account_id="acc-pending-b",
            context="pending update B",
        )

        processed = self.handler.process_pending_updates(
            "order-shared-pending",
            account_id="acc-pending-a",
        )

        self.assertTrue(processed)
        self.assertEqual(
            forwarded_calls,
            [
                {
                    "order_id": "order-shared-pending",
                    "new_status": "shipped",
                    "account_id": "acc-pending-a",
                    "context": "待处理队列: pending update A",
                }
            ],
        )
        remaining_updates = [
            update_info
            for updates in self.handler.pending_updates.values()
            for update_info in updates
        ]
        self.assertEqual(len(remaining_updates), 1)
        self.assertEqual(remaining_updates[0]["account_id"], "acc-pending-b")
        self.assertEqual(remaining_updates[0]["new_status"], "completed")

    def test_process_pending_updates_rejects_blank_account_id_for_scoped_queue(self):
        forwarded_calls = []

        def _fake_update_order_status(**kwargs):
            forwarded_calls.append(kwargs)
            return True

        self.handler.update_order_status = _fake_update_order_status
        self.handler._add_to_pending_updates(
            order_id="order-blank-account",
            new_status="shipped",
            account_id="acc-blank-account",
            context="pending update requires account scope",
        )

        processed = self.handler.process_pending_updates(
            "order-blank-account",
            account_id="   ",
        )

        self.assertFalse(processed)
        self.assertEqual(forwarded_calls, [])
        remaining_updates = [
            update_info
            for updates in self.handler.pending_updates.values()
            for update_info in updates
        ]
        self.assertEqual(len(remaining_updates), 1)
        self.assertEqual(remaining_updates[0]["account_id"], "acc-blank-account")

    def test_add_to_pending_updates_rejects_blank_account_id_for_scoped_queue(self):
        self.handler._add_to_pending_updates(
            order_id="order-no-scope",
            new_status="shipped",
            account_id="   ",
            context="missing scope should not queue",
        )

        self.assertEqual(self.handler.pending_updates, {})

    def test_update_order_status_rejects_blank_account_id_before_db_lookup(self):
        fake_db = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db):
            result = self.handler.update_order_status(
                order_id="order-no-scope",
                new_status="shipped",
                account_id="   ",
                context="missing scope should fail fast",
            )

        self.assertFalse(result)
        fake_db.get_order_by_id.assert_not_called()
        fake_db.insert_or_update_order.assert_not_called()

    def test_on_order_details_fetched_keeps_account_boundary_for_same_order_id(self):
        forwarded_calls = []

        def _fake_update_order_status(**kwargs):
            forwarded_calls.append(kwargs)
            return True

        self.handler.update_order_status = _fake_update_order_status
        self.handler._add_to_pending_updates(
            order_id="order-detail-shared",
            new_status="shipped",
            account_id="acc-detail-a",
            context="detail update A",
        )
        self.handler._add_to_pending_updates(
            order_id="order-detail-shared",
            new_status="completed",
            account_id="acc-detail-b",
            context="detail update B",
        )

        self.handler.on_order_details_fetched(
            "order-detail-shared",
            account_id="acc-detail-a",
        )

        self.assertEqual(
            forwarded_calls,
            [
                {
                    "order_id": "order-detail-shared",
                    "new_status": "shipped",
                    "account_id": "acc-detail-a",
                    "context": "待处理队列: detail update A",
                }
            ],
        )
        remaining_updates = [
            update_info
            for updates in self.handler.pending_updates.values()
            for update_info in updates
        ]
        self.assertEqual(len(remaining_updates), 1)
        self.assertEqual(remaining_updates[0]["account_id"], "acc-detail-b")
        self.assertEqual(remaining_updates[0]["new_status"], "completed")

    def test_on_order_id_extracted_reads_account_id_from_pending_message(self):
        forwarded_calls = []

        def _fake_update_order_status(**kwargs):
            forwarded_calls.append(kwargs)
            return True

        self.handler.update_order_status = _fake_update_order_status
        self.handler._pending_system_messages["acc-queue-1"] = [
            {
                "send_message": "交易成功",
                "msg_time": "12:00:00",
                "new_status": "completed",
                "account_id": "acc-queue-1",
                "temp_order_id": "temp-order-1",
                "timestamp": time.time(),
            }
        ]

        with mock.patch.object(
            self.handler,
            "_normalize_pending_match_context",
            return_value={},
        ):
            with mock.patch.object(
                self.handler,
                "_select_pending_message_index",
                return_value=(0, "legacy"),
            ):
                with mock.patch.object(
                    self.handler,
                    "_should_bind_pending_terminal_message",
                    return_value=True,
                ):
                    self.handler.on_order_id_extracted(
                        order_id="order-legacy-message",
                        account_id="acc-queue-1",
                        message={},
                    )

        self.assertEqual(forwarded_calls[0]["account_id"], "acc-queue-1")
        self.assertNotIn("cookie_id", forwarded_calls[0])

    def test_handle_system_message_rejects_legacy_cookie_id_keyword(self):
        with self.assertRaises(TypeError):
            self.handler.handle_system_message(
                message={},
                send_message="浜ゆ槗鎴愬姛",
                cookie_id="acc-legacy-keyword",
                msg_time="13:00:00",
            )

    def test_handle_system_message_rejects_blank_account_id_before_queueing(self):
        with mock.patch.object(self.handler, "_resolve_system_message_status") as resolve_status:
            handled = self.handler.handle_system_message(
                message={},
                send_message="交易成功",
                account_id="   ",
                msg_time="13:30:00",
            )

        self.assertFalse(handled)
        resolve_status.assert_not_called()
        self.assertEqual(self.handler.pending_updates, {})
        self.assertEqual(self.handler._pending_system_messages, {})

    def test_handle_red_reminder_message_rejects_blank_account_id_before_queueing(self):
        with mock.patch.object(self.handler, "extract_order_id") as extract_order_id:
            handled = self.handler.handle_red_reminder_message(
                message={},
                red_reminder="交易关闭",
                user_id="user-blank-scope",
                account_id="   ",
                msg_time="13:40:00",
            )

        self.assertFalse(handled)
        extract_order_id.assert_not_called()
        self.assertEqual(self.handler.pending_updates, {})
        self.assertEqual(self.handler._pending_red_reminder_messages, {})


    def test_try_resolve_cancelled_message_without_order_id_rejects_ambiguous_candidates(self):
        match_context = {
            "has_strong_match_key": True,
            "sid": "sid-ambiguous",
            "buyer_id": "buyer-ambiguous",
            "item_id": "item-ambiguous",
        }

        with mock.patch.object(
            self.handler,
            "_find_recent_orders_for_match_context",
            return_value=[
                {"order_id": "order-ambiguous-a", "order_status": "pending_ship"},
                {"order_id": "order-ambiguous-b", "order_status": "shipped"},
            ],
        ) as find_recent_orders, mock.patch.object(
            self.handler,
            "update_order_status",
        ) as update_order_status:
            resolved = self.handler._try_resolve_cancelled_message_without_order_id(
                account_id="acc-fail-close-1",
                msg_time="14:00:00",
                new_status="cancelled",
                context_label="交易关闭",
                match_context=match_context,
            )

        self.assertFalse(resolved)
        find_recent_orders.assert_called_once()
        update_order_status.assert_not_called()

    def test_handle_red_reminder_message_resolves_unique_candidate_without_queueing(self):
        match_context = {
            "has_strong_match_key": True,
            "sid": "sid-unique",
            "buyer_id": "buyer-unique",
            "item_id": "item-unique",
        }

        with mock.patch.object(self.handler, "extract_order_id", return_value=None), mock.patch.object(
            self.handler,
            "_normalize_pending_match_context",
            return_value=match_context,
        ), mock.patch.object(
            self.handler,
            "_find_recent_orders_for_match_context",
            return_value=[
                {"order_id": "order-unique-1", "order_status": "pending_ship"},
            ],
        ), mock.patch.object(
            self.handler,
            "update_order_status",
            return_value=True,
        ) as update_order_status:
            handled = self.handler.handle_red_reminder_message(
                message={},
                red_reminder="交易关闭",
                user_id="user-unique",
                account_id="acc-fail-close-2",
                msg_time="14:10:00",
            )

        self.assertTrue(handled)
        update_order_status.assert_called_once()
        call_kwargs = update_order_status.call_args.kwargs
        self.assertEqual(call_kwargs["order_id"], "order-unique-1")
        self.assertEqual(call_kwargs["new_status"], "cancelled")
        self.assertEqual(call_kwargs["account_id"], "acc-fail-close-2")
        self.assertIn("按匹配键即时回填", call_kwargs["context"])
        self.assertEqual(self.handler.pending_updates, {})
        self.assertEqual(self.handler._pending_red_reminder_messages, {})

    def test_handle_red_reminder_message_keeps_pending_queue_when_candidates_are_ambiguous(self):
        match_context = {
            "has_strong_match_key": True,
            "sid": "sid-queue",
            "buyer_id": "buyer-queue",
            "item_id": "item-queue",
        }

        with mock.patch.object(self.handler, "extract_order_id", return_value=None), mock.patch.object(
            self.handler,
            "_normalize_pending_match_context",
            return_value=match_context,
        ), mock.patch.object(
            self.handler,
            "_find_recent_orders_for_match_context",
            return_value=[
                {"order_id": "order-queue-a", "order_status": "pending_ship"},
                {"order_id": "order-queue-b", "order_status": "completed"},
            ],
        ), mock.patch.object(
            self.handler,
            "update_order_status",
        ) as update_order_status:
            handled = self.handler.handle_red_reminder_message(
                message={},
                red_reminder="交易关闭",
                user_id="user-queue",
                account_id="acc-fail-close-3",
                msg_time="14:20:00",
            )

        self.assertTrue(handled)
        update_order_status.assert_not_called()
        self.assertEqual(len(self.handler.pending_updates), 1)
        pending_key = next(iter(self.handler.pending_updates))
        self.assertEqual(pending_key[0], "acc-fail-close-3")
        pending_update = self.handler.pending_updates[pending_key][0]
        self.assertEqual(pending_update["account_id"], "acc-fail-close-3")
        self.assertEqual(pending_update["new_status"], "cancelled")
        self.assertEqual(len(self.handler._pending_red_reminder_messages["acc-fail-close-3"]), 1)
        pending_message = self.handler._pending_red_reminder_messages["acc-fail-close-3"][0]
        self.assertEqual(pending_message["account_id"], "acc-fail-close-3")
        self.assertEqual(pending_message["sid"], "sid-queue")

    def test_on_order_id_extracted_keeps_pending_queue_when_match_is_ambiguous(self):
        temp_order_id = "temp-ambiguous-order"
        pending_key = self.handler._build_scoped_order_key(temp_order_id, "acc-ambiguous-queue")
        self.handler.pending_updates[pending_key] = [
            {
                "new_status": "completed",
                "account_id": "acc-ambiguous-queue",
                "context": "temp ambiguous pending update",
                "timestamp": time.time(),
            }
        ]
        self.handler._pending_system_messages["acc-ambiguous-queue"] = [
            {
                "send_message": "浜ゆ槗鎴愬姛",
                "msg_time": "15:00:00",
                "new_status": "completed",
                "account_id": "acc-ambiguous-queue",
                "temp_order_id": temp_order_id,
                "timestamp": time.time(),
            }
        ]

        with mock.patch.object(
            self.handler,
            "_normalize_pending_match_context",
            return_value={},
        ), mock.patch.object(
            self.handler,
            "_select_pending_message_index",
            return_value=(None, "ambiguous_message_hash"),
        ), mock.patch.object(
            self.handler,
            "update_order_status",
        ) as update_order_status:
            self.handler.on_order_id_extracted(
                order_id="order-bound-ambiguous",
                account_id="acc-ambiguous-queue",
                message={},
            )

        update_order_status.assert_not_called()
        self.assertIn(pending_key, self.handler.pending_updates)
        self.assertEqual(len(self.handler._pending_system_messages["acc-ambiguous-queue"]), 1)

    def test_update_order_status_publishes_order_update_event_with_account_id(self):
        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-event-1",
            "order_status": "pending_ship",
            "account_id": "acc-event-1",
        }
        fake_db.insert_or_update_order.return_value = True

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("order_event_hub.publish_order_update_event") as publish_event:
            result = self.handler.update_order_status(
                order_id="order-event-1",
                new_status="shipped",
                account_id="acc-event-1",
                context="unit test publish event",
            )

        self.assertTrue(result)
        publish_event.assert_called_once_with(
            "order-event-1",
            account_id="acc-event-1",
            source="order_status_handler",
        )

    def test_publish_order_update_event_scopes_lookup_by_account_id(self):
        import order_event_hub

        fake_db = mock.Mock()
        fake_db.get_order_by_id.return_value = {
            "order_id": "order-hub-1",
            "account_id": "acc-hub-1",
        }
        fake_db.get_cookie_details.return_value = {"user_id": 1}

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(order_event_hub.order_event_hub, "publish") as publish:
            event = order_event_hub.publish_order_update_event(
                "order-hub-1",
                account_id="acc-hub-1",
                source="unit-test",
            )

        fake_db.get_order_by_id.assert_called_once_with(
            "order-hub-1",
            account_id="acc-hub-1",
        )
        publish.assert_called_once()
        self.assertIsNotNone(event)

    def test_publish_order_update_event_rejects_missing_account_id(self):
        import order_event_hub

        fake_db = mock.Mock()

        with mock.patch("db_manager.db_manager", fake_db):
            event = order_event_hub.publish_order_update_event(
                "order-hub-missing-account",
                source="unit-test",
            )

        self.assertIsNone(event)
        fake_db.get_order_by_id.assert_not_called()

    def test_on_order_id_extracted_clears_only_matching_account_temp_pending_update(self):
        forwarded_calls = []
        temp_order_id = "temp-shared-order"
        account_a_key = self.handler._build_scoped_order_key(temp_order_id, "acc-queue-a")
        account_b_key = self.handler._build_scoped_order_key(temp_order_id, "acc-queue-b")

        def _fake_update_order_status(**kwargs):
            forwarded_calls.append(kwargs)
            return True

        self.handler.update_order_status = _fake_update_order_status
        self.handler.pending_updates[account_a_key] = [
            {
                "new_status": "completed",
                "account_id": "acc-queue-a",
                "context": "temp pending update A",
                "timestamp": time.time(),
            }
        ]
        self.handler.pending_updates[account_b_key] = [
            {
                "new_status": "cancelled",
                "account_id": "acc-queue-b",
                "context": "temp pending update B",
                "timestamp": time.time(),
            }
        ]
        self.handler._pending_system_messages["acc-queue-a"] = [
            {
                "send_message": "浜ゆ槗鎴愬姛",
                "msg_time": "12:30:00",
                "new_status": "completed",
                "account_id": "acc-queue-a",
                "temp_order_id": temp_order_id,
                "timestamp": time.time(),
            }
        ]

        with mock.patch.object(
            self.handler,
            "_normalize_pending_match_context",
            return_value={},
        ):
            with mock.patch.object(
                self.handler,
                "_select_pending_message_index",
                return_value=(0, "legacy"),
            ):
                with mock.patch.object(
                    self.handler,
                    "_should_bind_pending_terminal_message",
                    return_value=True,
                ):
                    self.handler.on_order_id_extracted(
                        order_id="order-bound-a",
                        account_id="acc-queue-a",
                        message={},
                    )

        self.assertEqual(forwarded_calls[0]["account_id"], "acc-queue-a")
        self.assertNotIn(account_a_key, self.handler.pending_updates)
        self.assertIn(account_b_key, self.handler.pending_updates)
        self.assertEqual(
            self.handler.pending_updates[account_b_key][0]["account_id"],
            "acc-queue-b",
        )


if __name__ == "__main__":
    unittest.main()
