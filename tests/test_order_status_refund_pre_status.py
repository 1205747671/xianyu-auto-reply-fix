import unittest
from unittest import mock

import order_status_handler


class _FakeDBManager:
    def __init__(self, order):
        self.order = dict(order)
        self.pre_refund_status_calls = []

    def get_order_by_id(self, order_id, account_id=None, user_id=None):
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
        pre_refund_status=...,
        clear_pre_refund_status=False,
        **_kwargs,
    ):
        if self.order.get("order_id") != order_id:
            return False

        if order_status is not None:
            self.order["order_status"] = order_status
        if account_id is not None:
            self.order["account_id"] = account_id

        if clear_pre_refund_status:
            self.order["pre_refund_status"] = None
        elif pre_refund_status is not ...:
            self.order["pre_refund_status"] = pre_refund_status

        return True

    def get_order_pre_refund_status(self, order_id, account_id=None):
        self.pre_refund_status_calls.append(
            {"order_id": order_id, "account_id": account_id}
        )
        if self.order.get("order_id") != order_id:
            return None
        if account_id is not None and self.order.get("account_id") != account_id:
            return None
        return self.order.get("pre_refund_status")

    def get_cookie_details(self, account_id):
        if self.order.get("account_id") != account_id:
            return None
        return {"user_id": 1}


class _MultiAccountOrderDBManager:
    def __init__(self, orders):
        self.orders = {
            (order["account_id"], order["order_id"]): dict(order)
            for order in orders
        }
        self.pre_refund_status_calls = []

    def get_order_by_id(self, order_id, account_id=None, user_id=None):
        if account_id is None:
            return None
        order = self.orders.get((account_id, order_id))
        return dict(order) if order else None

    def insert_or_update_order(
        self,
        order_id,
        order_status=None,
        account_id=None,
        pre_refund_status=...,
        clear_pre_refund_status=False,
        **_kwargs,
    ):
        if account_id is None:
            return False

        key = (account_id, order_id)
        order = self.orders.get(key)
        if not order:
            return False

        if order_status is not None:
            order["order_status"] = order_status

        if clear_pre_refund_status:
            order["pre_refund_status"] = None
        elif pre_refund_status is not ...:
            order["pre_refund_status"] = pre_refund_status

        return True

    def get_order_pre_refund_status(self, order_id, account_id=None):
        self.pre_refund_status_calls.append(
            {"order_id": order_id, "account_id": account_id}
        )
        if account_id is None:
            return None
        order = self.orders.get((account_id, order_id))
        if not order:
            return None
        return order.get("pre_refund_status")

    def get_cookie_details(self, account_id):
        return {"user_id": 1} if any(key[0] == account_id for key in self.orders) else None


class OrderStatusRefundPreStatusTest(unittest.TestCase):
    def test_regular_status_update_does_not_clear_existing_pre_refund_status(self):
        fake_db = _FakeDBManager(
            {
                "order_id": "order_keep_pre_refund_status",
                "order_status": "pending_ship",
                "pre_refund_status": "processing",
                "account_id": "acc-keep-pre-refund-status",
            }
        )
        handler = order_status_handler.OrderStatusHandler()

        with mock.patch("db_manager.db_manager", fake_db):
            result = handler.update_order_status(
                order_id="order_keep_pre_refund_status",
                new_status="shipped",
                account_id="acc-keep-pre-refund-status",
                context="unit test regular transition",
            )

        self.assertTrue(result)
        self.assertEqual(fake_db.order["order_status"], "shipped")
        self.assertEqual(fake_db.order["pre_refund_status"], "processing")

    def test_leaving_refunding_clears_pre_refund_status(self):
        fake_db = _FakeDBManager(
            {
                "order_id": "order_clear_pre_refund_status",
                "order_status": "refunding",
                "pre_refund_status": "pending_ship",
                "account_id": "acc-clear-pre-refund-status",
            }
        )
        handler = order_status_handler.OrderStatusHandler()

        with mock.patch("db_manager.db_manager", fake_db):
            result = handler.update_order_status(
                order_id="order_clear_pre_refund_status",
                new_status="completed",
                account_id="acc-clear-pre-refund-status",
                context="unit test refund exit",
            )

        self.assertTrue(result)
        self.assertEqual(fake_db.order["order_status"], "completed")
        self.assertIsNone(fake_db.order["pre_refund_status"])

    def test_refund_cancelled_uses_scoped_pre_refund_status(self):
        fake_db = _FakeDBManager(
            {
                "order_id": "order_refund_cancelled",
                "order_status": "refunding",
                "pre_refund_status": "pending_ship",
                "account_id": "acc-refund-cancelled",
            }
        )
        handler = order_status_handler.OrderStatusHandler()

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch.object(handler, "_get_previous_status", return_value=None), \
             mock.patch("order_event_hub.publish_order_update_event"):
            result = handler.update_order_status(
                order_id="order_refund_cancelled",
                new_status="refund_cancelled",
                account_id="acc-refund-cancelled",
                context="unit test refund cancelled",
            )

        self.assertTrue(result)
        self.assertEqual(
            fake_db.pre_refund_status_calls,
            [
                {
                    "order_id": "order_refund_cancelled",
                    "account_id": "acc-refund-cancelled",
                }
            ],
        )
        self.assertEqual(fake_db.order["order_status"], "pending_ship")
        self.assertIsNone(fake_db.order["pre_refund_status"])

    def test_refund_cancelled_ignores_other_account_history_and_uses_scoped_db_status(self):
        fake_db = _MultiAccountOrderDBManager(
            [
                {
                    "order_id": "order-shared-history",
                    "order_status": "processing",
                    "account_id": "acc-history-a",
                    "pre_refund_status": None,
                },
                {
                    "order_id": "order-shared-history",
                    "order_status": "refunding",
                    "account_id": "acc-history-b",
                    "pre_refund_status": "shipped",
                },
            ]
        )
        handler = order_status_handler.OrderStatusHandler()

        with mock.patch("db_manager.db_manager", fake_db), \
             mock.patch("order_event_hub.publish_order_update_event"):
            seeded = handler.update_order_status(
                order_id="order-shared-history",
                new_status="pending_ship",
                account_id="acc-history-a",
                context="seed other account history",
            )
            result = handler.update_order_status(
                order_id="order-shared-history",
                new_status="refund_cancelled",
                account_id="acc-history-b",
                context="refund cancelled should stay scoped",
            )

        self.assertTrue(seeded)
        self.assertTrue(result)
        self.assertEqual(
            fake_db.pre_refund_status_calls,
            [
                {
                    "order_id": "order-shared-history",
                    "account_id": "acc-history-b",
                }
            ],
        )
        self.assertEqual(
            fake_db.orders[("acc-history-b", "order-shared-history")]["order_status"],
            "shipped",
        )


if __name__ == "__main__":
    unittest.main()
