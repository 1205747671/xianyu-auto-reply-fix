import queue
import threading
import time
from collections import defaultdict
from typing import Any, Dict, Optional

from loguru import logger


class OrderEventHub:
    """进程内订单事件中心，按 user_id 广播订单更新。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._subscribers = defaultdict(set)

    def subscribe(self, user_id: int, maxsize: int = 100):
        subscriber = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers[user_id].add(subscriber)
        return subscriber

    def unsubscribe(self, user_id: int, subscriber):
        with self._lock:
            subscribers = self._subscribers.get(user_id)
            if not subscribers:
                return
            subscribers.discard(subscriber)
            if not subscribers:
                self._subscribers.pop(user_id, None)

    def publish(self, user_id: int, event: Dict[str, Any]):
        with self._lock:
            subscribers = list(self._subscribers.get(user_id, set()))

        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass

                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    logger.warning(f"订单事件队列仍然已满，丢弃事件: user_id={user_id}")


order_event_hub = OrderEventHub()


def build_order_update_event(order: Dict[str, Any], source: str = "unknown") -> Dict[str, Any]:
    return {
        "type": "order.updated",
        "source": source,
        "timestamp": int(time.time() * 1000),
        "order": order,
    }


def publish_order_update_event(
    order_id: str,
    account_id: str = None,
    source: str = "unknown",
) -> Optional[Dict[str, Any]]:
    from db_manager import db_manager

    normalized_account_id = str(account_id or "").strip() or None
    if normalized_account_id is None:
        logger.warning(f"拒绝未带 account_id 的订单事件发布查询: {order_id}")
        return None

    order = db_manager.get_order_by_id(order_id, account_id=normalized_account_id)
    if not order:
        return None

    account_id = order.get('account_id')
    if not account_id:
        return None

    cookie_info = db_manager.get_cookie_details(account_id)
    user_id = cookie_info.get('user_id') if cookie_info else None
    if user_id is None:
        return None

    event = build_order_update_event(order, source=source)
    order_event_hub.publish(user_id, event)
    return event
